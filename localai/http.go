package localai

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Config 是客户端配置。
//
// 全部字段可选：零值 Config{} 就能工作（自动发现 + 默认超时）。
type Config struct {
	// BaseURL 显式指定 Runtime 地址。
	// 留空则按文档 18.4 的顺序自动发现。
	BaseURL string
	// Token 显式指定访问凭证。
	// 留空则从环境变量或 discovery 文件取。
	Token string
	// Timeouts 是三层超时。零值用文档 24.2 的默认值。
	Timeouts Timeouts
	// HTTPClient 可替换底层 HTTP 客户端（如注入自定义 Transport）。
	// 为零值时用内部默认客户端。
	HTTPClient *http.Client
	// MaxRetries 是自动重试的最大次数（不含首次）。
	//
	// 零值表示用默认策略（文档 24.4：最多 2 次）。
	// 显式设为负值可完全关闭重试。
	MaxRetries int
	// NoRetry 为 true 时完全关闭自动重试。
	//
	// 为什么用排除式：MaxRetries 的零值有歧义（"没设置"还是"不重试"），
	// 而重试是**危险行为**——它会在服务端已过载时放大压力
	// （文档 24.4 的原话）。因此"关闭"必须是显式的。
	NoRetry bool
}

// Client 是 Runtime 的客户端。
//
// 并发安全：可在多个 goroutine 中共享。
type Client struct {
	baseURL  string
	token    string
	timeouts Timeouts
	http     *http.Client
	maxRetry int
	// discovery 记录实际使用的发现结果（便于诊断）
	discovery *Discovery
}

// NewClient 创建客户端。
//
// 自动发现失败不阻止创建：可能调用方只用 Health 探活而不需要 token，
// 或者稍后才会启动 Runtime。真正的失败在首次请求时暴露。
func NewClient(cfg Config) (*Client, error) {
	d, err := Discover(cfg.BaseURL, cfg.Token)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrRuntimeUnavailable, err)
	}

	timeouts := cfg.Timeouts.withDefaults()

	hc := cfg.HTTPClient
	if hc == nil {
		hc = &http.Client{
			// 不设全局 Timeout：它会把整个请求（含流式输出）算进去，
			// 而流式请求的合法时长取决于输出长度。
			// 超时控制交给 context（三层超时由 SDK 派生）。
			Transport: &http.Transport{
				DialContext: (&net.Dialer{
					Timeout: timeouts.Connect,
				}).DialContext,
				MaxIdleConns:        8,
				MaxIdleConnsPerHost: 8,
				IdleConnTimeout:     90 * time.Second,
			},
		}
	}

	maxRetry := 2 // 文档 24.4 默认
	switch {
	case cfg.NoRetry:
		maxRetry = 0
	case cfg.MaxRetries < 0:
		maxRetry = 0
	case cfg.MaxRetries > 0:
		maxRetry = cfg.MaxRetries
	}

	return &Client{
		baseURL:   d.URL,
		token:     d.Token,
		timeouts:  timeouts,
		http:      hc,
		maxRetry:  maxRetry,
		discovery: d,
	}, nil
}

// BaseURL 返回实际使用的地址（已做发现解析）。
func (c *Client) BaseURL() string { return c.baseURL }

// Discovery 返回发现结果（便于诊断连接的是哪个实例）。
func (c *Client) Discovery() Discovery {
	if c.discovery == nil {
		return Discovery{}
	}
	return *c.discovery
}

// ---------------------------------------------------------------------------
// 底层请求
// ---------------------------------------------------------------------------

// envelope 是 Runtime 的统一响应信封（文档 19.1）。
type envelope struct {
	Success   bool            `json:"success"`
	RequestID string          `json:"request_id"`
	Data      json.RawMessage `json:"data"`
	Model     *ModelInfo      `json:"model"`
	Usage     *Usage          `json:"usage"`
	Warnings  []string        `json:"warnings"`
	Error     *errorBody      `json:"error"`
	Knowledge *KnowledgeInfo  `json:"knowledge"`
}

// errorBody 是结构化错误（文档 19.2）。
type errorBody struct {
	Code         string `json:"code"`
	Message      string `json:"message"`
	Retryable    bool   `json:"retryable"`
	RetryAfterMs int64  `json:"retry_after_ms"`
	Degradable   bool   `json:"degradable"`
}

// ModelInfo 说明本次请求实际用了哪个模型以及是否降级（文档 19.1）。
type ModelInfo struct {
	Requested     string `json:"requested"`
	Actual        string `json:"actual"`
	Degraded      bool   `json:"degraded"`
	DegradeReason string `json:"degrade_reason,omitempty"`
}

// Usage 是资源消耗（文档 19.1）。
type Usage struct {
	// PromptTokens 是提示词 token 数。
	//
	// 流式场景下**可能为 0 而表示"引擎未上报"**，而非真的是 0：
	// 引擎在极端情况下（产出首个 token 前中断但流本身正常收尾）
	// 不给 timings，此时服务端省略该字段。
	//
	// ⚠️ **不要直接当作完整 prompt 长度**：流式下它可能是本轮的
	// 增量（prompt cache 命中后只算新增）。见
	// PromptTokensMayBeIncremental。
	PromptTokens int `json:"prompt_tokens"`
	// CompletionTokens 是生成 token 数。同上，可能为 0 表示未上报。
	CompletionTokens int   `json:"completion_tokens"`
	TTFTMs           int64 `json:"ttft_ms"`
	TotalMs          int64 `json:"total_ms"`
	QueueWaitMs      int64 `json:"queue_wait_ms"`
	// PromptTokensMayBeIncremental 表示 PromptTokens **可能**只是
	// 本轮新增部分（prompt cache 命中），不是完整 prompt 长度。
	//
	// 流式下恒为 true：引擎不报缓存命中数，服务端无法区分
	// "首次请求的完整值"（实测 856）与"后续请求的增量值"（实测 4）。
	//
	// 为什么是"可能"而不是"是"：断言它一定是增量，对首次请求就是
	// 错的，调用方会误以为上下文占用只有一半。缺失或 false 也不能
	// 假设它是完整长度——要精确值需要引擎提供 cache_n。
	PromptTokensMayBeIncremental bool `json:"prompt_tokens_may_be_incremental,omitempty"`
}

// KnowledgeInfo 说明本次调用如何使用知识库。
type KnowledgeInfo struct {
	KBIDs         []string          `json:"kb_ids"`
	UsedChunks    int               `json:"used_chunks"`
	ContextTokens int               `json:"context_tokens"`
	RetrievalMs   int64             `json:"retrieval_ms"`
	Degraded      bool              `json:"degraded,omitempty"`
	Sources       []KnowledgeSource `json:"sources,omitempty"`
	Warnings      []string          `json:"warnings,omitempty"`
}

// KnowledgeSource 是一个引用来源。
type KnowledgeSource struct {
	Index      int     `json:"index"`
	ChunkID    string  `json:"chunk_id"`
	DocumentID string  `json:"document_id"`
	Source     string  `json:"source,omitempty"`
	Score      float32 `json:"score"`
}

// do 执行一次请求并解析信封。内部处理重试（文档 24.4）。
func (c *Client) do(ctx context.Context, method, path string, body any) (*envelope, error) {
	payload, err := marshalBody(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}

	var lastErr error
	for attempt := 0; attempt <= c.maxRetry; attempt++ {
		env, err := c.doOnce(ctx, method, path, payload)
		if err == nil {
			return env, nil
		}
		lastErr = err

		// 判断是否值得重试（文档 24.4）
		wait, retry := shouldRetry(err, attempt, c.maxRetry)
		if !retry {
			return nil, err
		}

		// 加抖动避免多个客户端同时重试（惊群）
		select {
		case <-time.After(wait):
		case <-ctx.Done():
			return nil, wrapCtxErr(ctx.Err())
		}
	}
	return nil, lastErr
}

// doOnce 执行单次 HTTP 请求。
func (c *Client) doOnce(ctx context.Context, method, path string, payload []byte) (*envelope, error) {
	var rdr io.Reader
	if payload != nil {
		rdr = bytes.NewReader(payload)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, rdr)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, translateTransportErr(ctx, err)
	}
	defer resp.Body.Close()

	// 限制读取量：正常响应几十 KB，异常时可能巨大
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return nil, translateTransportErr(ctx, err)
	}

	return parseEnvelope(resp.StatusCode, raw, resp.Header.Get("X-Request-ID"))
}

// doRaw 发送任意 Content-Type 的请求体（用于 multipart 上传）。
//
// ## 为什么不复用 do
//
// do 固定按 application/json 序列化，且带自动重试。上传不能重试：
// 请求体是一个只能读一次的 io.Reader，重试时内容已经发完，第二次
// 会发出一个空文件——用户看到"导入成功 0 切片"，比直接失败更难查。
//
// 因此这里只做单次请求。调用方需要重试时自己重新构造 UploadRequest
// （它持有的是可重新打开的 io.Reader）。
func (c *Client) doRaw(ctx context.Context, method, path, contentType string,
	body io.Reader) (*envelope, error) {

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	req.Header.Set("Content-Type", contentType)
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, translateTransportErr(ctx, err)
	}
	defer resp.Body.Close()

	// 与服务端 16 MiB 响应上限对齐；导入响应只有几百字节
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return nil, translateTransportErr(ctx, err)
	}
	return parseEnvelope(resp.StatusCode, raw, resp.Header.Get("X-Request-ID"))
}

// parseEnvelope 解析响应信封并转换错误。
func parseEnvelope(status int, raw []byte, headerReqID string) (*envelope, error) {
	var env envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		// 非 JSON 响应：可能是错误的端口（连到了别的服务）
		if status != http.StatusOK {
			return nil, &APIError{
				Kind:       ErrRuntimeUnavailable,
				Message:    fmt.Sprintf("HTTP %d，响应不是 JSON: %.200s", status, raw),
				StatusCode: status,
			}
		}
		return nil, &APIError{
			Kind:       ErrRuntimeUnavailable,
			Message:    fmt.Sprintf("无法解析响应: %.200s", raw),
			StatusCode: status,
		}
	}

	// 服务端返回结构化的失败
	if !env.Success || env.Error != nil {
		reqID := env.RequestID
		if reqID == "" {
			reqID = headerReqID
		}
		if env.Error != nil {
			return nil, &APIError{
				Kind:       kindForCode(env.Error.Code),
				Code:       env.Error.Code,
				Message:    env.Error.Message,
				Retryable:  env.Error.Retryable,
				RetryAfter: time.Duration(env.Error.RetryAfterMs) * time.Millisecond,
				RequestID:  reqID,
				StatusCode: status,
			}
		}
		return nil, &APIError{
			Kind:       kindForCode("internal_error"),
			Message:    "服务端返回失败但没有错误详情",
			RequestID:  reqID,
			StatusCode: status,
		}
	}

	// 成功响应的状态码不是 2xx：异常
	if status < 200 || status >= 300 {
		return nil, &APIError{
			Kind:       ErrRuntimeUnavailable,
			Message:    fmt.Sprintf("意外的状态码 %d", status),
			RequestID:  env.RequestID,
			StatusCode: status,
		}
	}

	return &env, nil
}

// shouldRetry 判断该错误是否值得重试（文档 24.4）。
//
// 只重试"压力类"错误：resource_limited、queue_full、engine_crashed。
// **不重试** input_too_long / invalid_request / schema 失败——
// 那些重试必然再次失败，只会浪费用户时间。
func shouldRetry(err error, attempt, maxRetry int) (time.Duration, bool) {
	if attempt >= maxRetry {
		return 0, false
	}

	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		// 网络层错误可重试（Runtime 可能刚启动）
		if errors.Is(err, ErrRuntimeUnavailable) {
			return jitter(time.Second), true
		}
		return 0, false
	}

	switch {
	case errors.Is(err, ErrResourceLimited), errors.Is(err, ErrQueueFull):
		// 按 RetryAfter，没给就用退避
		base := apiErr.RetryAfter
		if base <= 0 {
			base = time.Duration(attempt+1) * time.Second
		}
		return jitter(base), true
	case errors.Is(err, ErrModelUnavailable):
		// 模型可能在加载中，给一次机会
		return jitter(2 * time.Second), true
	default:
		// input_too_long / invalid_request / unauthorized / quota：
		// 重试无意义，甚至有害（增加负载）
		return 0, false
	}
}

// jitter 给等待时间加 ±30% 的随机抖动。
//
// 为什么必须抖动：多个客户端同时收到 resource_limited 后
// 若在同一时刻重试，会再次把服务端打满（惊群）。
func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	// 用 math/rand 即可：抖动不需要密码学随机
	factor := 0.7 + rand.Float64()*0.6 // [0.7, 1.3]
	return time.Duration(float64(d) * factor)
}

// translateTransportErr 把网络层错误转为哨兵错误。
func translateTransportErr(ctx context.Context, err error) error {
	// context 取消/超时优先判断：否则会被 net 错误掩盖
	if ctx.Err() != nil {
		return wrapCtxErr(ctx.Err())
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return ErrTimeout
	}
	if errors.Is(err, context.Canceled) {
		return ErrTaskCancelled
	}

	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return fmt.Errorf("%w: %v", ErrTimeout, err)
	}

	// 连接被拒绝 = Runtime 没在跑
	return fmt.Errorf("%w: %v", ErrRuntimeUnavailable, err)
}

// wrapCtxErr 把 context 错误转成 SDK 的哨兵错误。
//
// 区分 DeadlineExceeded 与 Canceled：前者是"等太久"，调用方可以
// 调大超时重试；后者是"主动放弃"（或客户端断开），不该重试。
func wrapCtxErr(err error) error {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return ErrTimeout
	case errors.Is(err, context.Canceled):
		return ErrTaskCancelled
	default:
		return err
	}
}

// marshalBody 序列化请求体。nil 表示无 body。
func marshalBody(v any) ([]byte, error) {
	if v == nil {
		return nil, nil
	}
	return json.Marshal(v)
}

// unmarshalJSON 是 json.Unmarshal 的实现。
func unmarshalJSON(b []byte, v any) error {
	return json.Unmarshal(b, v)
}

// parseRetryAfter 解析 Retry-After 头。
//
// 支持两种格式（HTTP 规范）：秒数、HTTP 日期。
// 当前 Runtime 只用秒数，但兼容日期格式的解析对将来有用。
func parseRetryAfter(h string) time.Duration {
	if h == "" {
		return 0
	}
	if secs, err := strconv.Atoi(strings.TrimSpace(h)); err == nil && secs > 0 {
		return time.Duration(secs) * time.Second
	}
	if t, err := http.ParseTime(h); err == nil {
		if d := time.Until(t); d > 0 {
			return d
		}
	}
	return 0
}
