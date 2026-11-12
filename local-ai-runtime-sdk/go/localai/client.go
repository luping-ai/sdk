// Package localai 是 Local AI Hub Runtime 的 Go 客户端 SDK。
//
// ## 设计目标（文档第六章、第二十四章）
//
// 让业务软件无需了解底层模型即可调用 AI 能力：
//
//   - **自动发现**：不写死端口。端口是动态分配的（ADR 0004）
//   - **类型化错误**：区分"稍后重试"与"不可能成功"，不靠字符串匹配
//   - **三层超时**：连接 / 首字节 / 总时长（本地推理冷启动可能很慢）
//   - **降级不等于错误**：降级结果照常返回，通过字段标记
//
// ## 为什么不负责启动 Runtime
//
// 文档 18.5 明确 V1 采用模式 D：**SDK 只连接，不负责启动**。
// 理由：任何自动拉起行为必须对用户可见，否则会被误判为恶意程序。
// 启动交给安装器或系统自启项。
//
// ## 契约来源
//
// 文档 24.3 规定 `docs/openapi.yaml` 是唯一真相，Go SDK 为手写
// 薄封装。本包的类型定义应与该文件保持一致——但它们不是从文件
// 生成的（手写便于提供 Context、类型化错误、发现逻辑）。
//
// ## 零依赖
//
// 只依赖标准库。SDK 会被大量第三方项目引入，任何依赖都会
// 传染给它们。
package localai

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// 类型化错误（文档 24.1）
// ---------------------------------------------------------------------------

// 这些都是可判定的哨兵错误，用 errors.Is 判断。
//
// 业务代码需要区分"稍后重试"和"不可能成功"——字符串匹配是灾难：
// 换个语言、改个措辞就失效，而错误的判断会让用户看到"请重试"
// 却永远不成功，或者该重试时直接放弃。
var (
	// ErrRuntimeUnavailable 无法连接 Runtime。
	//
	// 常见原因：Runtime 未启动、discovery 文件指向已退出的进程。
	// 业务方应提示用户"本地 AI 未运行"。
	ErrRuntimeUnavailable = errors.New("localai: runtime unavailable")

	// ErrUnauthorized token 无效。
	//
	// 通常意味着 discovery 文件是旧的（Runtime 重启后 token 变了）。
	// 业务方应重新读取 discovery 文件。
	ErrUnauthorized = errors.New("localai: unauthorized")

	// ErrResourceLimited 资源不足（内存/CPU/压力）。
	//
	// 可重试：等资源恢复后可能成功。
	ErrResourceLimited = errors.New("localai: resource limited")

	// ErrQueueFull 队列已满。
	//
	// 可重试：按 RetryAfter 等待后可能成功。
	// 与 ErrResourceLimited 的区别：这是"请求太多"，不是"能力不够"。
	ErrQueueFull = errors.New("localai: queue full")

	// ErrClientQuota 该客户端配额用尽。
	//
	// 与 ErrQueueFull 的区别：这只影响当前调用方，其他客户端正常。
	// 业务方应降低自己的请求频率，而不是等。
	ErrClientQuota = errors.New("localai: client quota exceeded")

	// ErrInputTooLong 输入超出上下文长度。
	//
	// 不可重试：必须缩短输入或换更大上下文的模型。
	ErrInputTooLong = errors.New("localai: input too long")

	// ErrModelUnavailable 无可用模型。
	//
	// 可重试：模型可能正在加载。
	ErrModelUnavailable = errors.New("localai: model unavailable")

	// ErrCapabilityUnmet 有模型能装下，但不满足能力或白名单要求。
	//
	// **不可重试**：与 ErrResourceLimited 的区别就在这里。
	// resource_limited 是"关掉其他程序再试"，而这个是
	// "关程序没用，得换模型或改配置"——重试必然再次失败。
	ErrCapabilityUnmet = errors.New("localai: capability unmet")

	// ErrNotFound 请求的资源不存在（模型、知识库、文档、文件）。
	//
	// 与服务端错误码的关系：
	//   - model_not_found → 这个（模型场景）
	//   - not_found       → 也是这个（知识库/文档/文件场景）
	//
	// 两者在 SDK 层合并成一个哨兵错误是刻意的：对调用方而言
	// "要的东西不存在"是同一类判断，且都不可重试。需要区分时
	// 读 APIError.Code。
	ErrNotFound = errors.New("localai: not found")

	// ErrInvalidRequest 请求参数非法。
	//
	// 不可重试。
	ErrInvalidRequest = errors.New("localai: invalid request")

	// ErrTaskCancelled 任务被取消（含超时与客户端断开）。
	ErrTaskCancelled = errors.New("localai: task cancelled")

	// ErrTimeout 超过设定超时。
	//
	// 与 ErrTaskCancelled 区分：这是"等太久"，可以调大超时重试。
	ErrTimeout = errors.New("localai: timeout")
)

// APIError 是来自 Runtime 的结构化错误（文档 19.2）。
//
// 它包装一个哨兵错误，同时保留服务端返回的原始信息。
// 业务方既可以用 errors.Is(err, localai.ErrResourceLimited) 判断类别，
// 也可以读 Code/Message 做更细的处理或日志。
type APIError struct {
	// Kind 是对应的哨兵错误
	Kind error
	// Code 是服务端的错误码（如 "resource_limited"）
	Code string
	// Message 是服务端的中文说明
	Message string
	// Retryable 是服务端建议的"是否值得重试"
	Retryable bool
	// RetryAfter 是建议的等待时长
	RetryAfter time.Duration
	// RequestID 便于向 Runtime 开发者报问题
	RequestID string
	// StatusCode 是 HTTP 状态码
	StatusCode int
}

func (e *APIError) Error() string {
	var sb strings.Builder
	sb.WriteString(e.Kind.Error())
	if e.Code != "" {
		sb.WriteString(" [")
		sb.WriteString(e.Code)
		sb.WriteString("]")
	}
	if e.Message != "" {
		sb.WriteString(": ")
		sb.WriteString(e.Message)
	}
	if e.RetryAfter > 0 {
		sb.WriteString(fmt.Sprintf("（建议 %s 后重试）", e.RetryAfter))
	}
	if e.RequestID != "" {
		sb.WriteString(" (request_id=")
		sb.WriteString(e.RequestID)
		sb.WriteString(")")
	}
	return sb.String()
}

// Unwrap 让 errors.Is(err, ErrResourceLimited) 生效。
func (e *APIError) Unwrap() error { return e.Kind }

// errorCodeKinds 把服务端错误码映射到哨兵错误。
//
// 这个映射必须与 Runtime 侧 internal/api/errors.go 的常量一致。
// 不一致会导致业务方判断错误——例如把"队列满"当成"资源不足"，
// 从而朝错误方向优化。
var errorCodeKinds = map[string]error{
	"invalid_request":       ErrInvalidRequest,
	"unauthorized":          ErrUnauthorized,
	"forbidden":             ErrUnauthorized,
	"model_not_found":       ErrNotFound,
	"not_found":             ErrNotFound,
	"method_not_allowed":    ErrInvalidRequest,
	"queue_full":            ErrQueueFull,
	"client_quota_exceeded": ErrClientQuota,
	"resource_limited":      ErrResourceLimited,
	"model_unavailable":     ErrModelUnavailable,
	"capability_unmet":      ErrCapabilityUnmet,
	"internal_error":        ErrRuntimeUnavailable,
}

// kindForCode 返回错误码对应的哨兵错误。未知码归为 ErrRuntimeUnavailable——
// 保守选择：至少让调用方知道"这次没成功"。
func kindForCode(code string) error {
	if k, ok := errorCodeKinds[code]; ok {
		return k
	}
	return ErrRuntimeUnavailable
}

// ---------------------------------------------------------------------------
// 发现（文档 18.4）
// ---------------------------------------------------------------------------

// Discovery 是 discovery 文件的内容（文档 18.3）。
type Discovery struct {
	URL       string `json:"url"`
	Token     string `json:"token"`
	PID       int    `json:"pid"`
	Version   string `json:"version"`
	API       string `json:"api"`
	StartedAt int64  `json:"started_at"`
	State     string `json:"state"`
}

// 环境变量名（文档 18.4）。
const (
	EnvURL   = "LAIH_URL"
	EnvToken = "LAIH_TOKEN"
)

// 默认端口（文档 18.3）：优先尝试，被占用则回退随机端口。
const defaultPort = 18765

// Discover 按文档 18.4 的顺序查找 Runtime。
//
// 顺序：
//
//	显式 BaseURL → 环境变量 → discovery 文件 → 默认端口
//
// 返回的 Discovery 中 URL 与 Token 均已填充。
// 找不到时返回错误，调用方应提示用户"本地 AI 未运行"。
//
// 注意：本函数**不验证**连接是否可用。文档 18.3 要求 SDK 通过
// /health 探活判定失效——那需要网络请求，应由调用方在需要时做
// （见 Client.Health）。
func Discover(explicitURL, explicitToken string) (*Discovery, error) {
	// 1. 显式指定
	if explicitURL != "" {
		return &Discovery{
			URL:   strings.TrimRight(explicitURL, "/"),
			Token: explicitToken,
		}, nil
	}

	// 2. 环境变量
	if u := os.Getenv(EnvURL); u != "" {
		return &Discovery{
			URL:   strings.TrimRight(u, "/"),
			Token: os.Getenv(EnvToken),
		}, nil
	}

	// 3. discovery 文件
	if d, err := readDiscovery(); err == nil {
		return d, nil
	}

	// 4. 默认端口
	//
	// 没有 token 无法调用任何接口，但仍返回地址——
	// 调用方可能会从别的渠道拿到 token（如自己的配置）。
	return &Discovery{
		URL: fmt.Sprintf("http://127.0.0.1:%d", defaultPort),
	}, nil
}

// DiscoveryPath 返回 discovery 文件路径（与 Runtime 侧一致的规则）。
//
// 这个逻辑必须与 internal/discovery/info.go 保持同步。
// 复制而不是 import：SDK 不能依赖 Runtime 的内部包，否则
// 第三方项目引入 SDK 时会连带拉进整个 Runtime 的依赖树。
func DiscoveryPath() (string, error) {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			d, err := os.UserConfigDir()
			if err != nil {
				return "", err
			}
			base = d
		}
		return filepath.Join(base, "LocalAIHub", "runtime.json"), nil
	case "linux":
		base := os.Getenv("XDG_DATA_HOME")
		if base == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			base = filepath.Join(home, ".local", "share")
		}
		return filepath.Join(base, "local-ai-hub", "runtime.json"), nil
	default:
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, ".local-ai-hub", "runtime.json"), nil
	}
}

// readDiscovery 读取 discovery 文件。
func readDiscovery() (*Discovery, error) {
	path, err := DiscoveryPath()
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var d Discovery
	if err := jsonUnmarshal(b, &d); err != nil {
		return nil, err
	}
	if d.URL == "" {
		return nil, fmt.Errorf("discovery 文件缺少 url")
	}
	d.URL = strings.TrimRight(d.URL, "/")
	return &d, nil
}

// ---------------------------------------------------------------------------
// 三层超时（文档 24.2）
// ---------------------------------------------------------------------------

// Timeouts 是三层超时配置。
//
// 为什么需要三层：本地推理的首字节延迟可能很长（冷启动要加载
// 模型，可能要几十秒），单一超时无法兼顾"快速发现 Runtime 死了"
// 与"允许模型慢慢加载"。
type Timeouts struct {
	// Connect 判断 Runtime 进程是否存活。
	// 默认 1 秒（文档 24.2）。
	Connect time.Duration
	// TTFT 是首字节超时，允许模型加载与排队。
	// 默认 15 秒（文档 24.2）。
	TTFT time.Duration
	// Total 是总时长兜底。
	// 默认 60 秒（文档 24.2）。
	Total time.Duration
}

// DefaultTimeouts 返回文档 24.2 规定的默认值。
func DefaultTimeouts() Timeouts {
	return Timeouts{
		Connect: time.Second,
		TTFT:    15 * time.Second,
		Total:   60 * time.Second,
	}
}

func (t Timeouts) withDefaults() Timeouts {
	d := DefaultTimeouts()
	if t.Connect <= 0 {
		t.Connect = d.Connect
	}
	if t.TTFT <= 0 {
		t.TTFT = d.TTFT
	}
	if t.Total <= 0 {
		t.Total = d.Total
	}
	return t
}

// ---------------------------------------------------------------------------
// Context 辅助
// ---------------------------------------------------------------------------

// ContextWithTimeout 按三层超时中的总时长派生 context。
//
// 用途：调用方不想自己管超时时，用它一次性设置。
// 若调用方的 ctx 已有更短的 deadline，以调用方的为准
// （context 的语义天然如此）。
func (c *Client) ContextWithTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, c.timeouts.Total)
}

// jsonUnmarshal 是 json.Unmarshal 的间接调用（便于替换实现）。
func jsonUnmarshal(b []byte, v any) error {
	return unmarshalJSON(b, v)
}
