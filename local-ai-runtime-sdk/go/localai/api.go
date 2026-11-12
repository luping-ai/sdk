package localai

import (
	"context"
	"encoding/json"
)

// ---------------------------------------------------------------------------
// 健康检查（文档 26.2）
// ---------------------------------------------------------------------------

// HealthState 是 Runtime 的可服务状态。
type HealthState string

const (
	HealthStarting    HealthState = "starting"
	HealthReady       HealthState = "ready"
	HealthDegraded    HealthState = "degraded"
	HealthUnavailable HealthState = "unavailable"
)

// HealthReport 是一次健康检查的结果。
type HealthReport struct {
	State         HealthState            `json:"state"`
	Since         int64                  `json:"since"`
	UptimeSeconds int64                  `json:"uptime_seconds"`
	Reason        string                 `json:"reason,omitempty"`
	Checks        map[string]CheckResult `json:"checks,omitempty"`
}

// CheckResult 是单项检查结果。
type CheckResult struct {
	OK      bool   `json:"ok"`
	Message string `json:"message,omitempty"`
}

// CapabilityLevel 是这台机器能提供的能力等级。
type CapabilityLevel string

const (
	// CapabilityFull 可加载实测达标的模型，对话与 RAG 都可用。
	CapabilityFull CapabilityLevel = "full"
	// CapabilityChatOnly 对话可用，但没有实测达标的 RAG 模型。
	CapabilityChatOnly CapabilityLevel = "chat_only"
	// CapabilityRuleOnly 连基准模型都装不下，只有规则引擎。
	CapabilityRuleOnly CapabilityLevel = "rule_only"
)

// Capability 是这台机器能做什么。
//
// 判据是模型清单里本项目实测的 rag.qualified，不是厂商声明——
// 实测发现厂商声明含 "rag" 的模型（Granite-350M/1B）准确率只有
// 50%~75%，而厂商没标的 Qwen3-0.6B 实测 16/16。
type Capability struct {
	Level CapabilityLevel `json:"level"`
	// Reason 是一句话说明当前为何是该等级。
	Reason string `json:"reason"`
	// RAGEnabled 表示知识库检索是否可用。
	// 为 false 时不要传 KBIDs——服务端会忽略。
	RAGEnabled bool `json:"rag_enabled"`
	// ChatEnabled 表示模型对话是否可用。
	ChatEnabled bool `json:"chat_enabled"`
	// AvailableBytes 是评估时的可用内存。
	AvailableBytes int64 `json:"available_bytes"`
	// BaselineNeedBytes 是基准模型的内存需求，用于向用户解释"差多少"。
	BaselineNeedBytes int64 `json:"baseline_need_bytes,omitempty"`
	// RAGModel 是当前将用于 RAG 的模型 ID（空表示没有）。
	RAGModel string `json:"rag_model,omitempty"`
	// Warnings 需要展示给用户的提示，不要静默丢弃。
	Warnings []string `json:"warnings,omitempty"`
}

// Ready 表示可以发起推理请求。
//
// ready 与 degraded 都算"可用"——degraded 意味着能力受限
// （如只能走规则引擎），但服务仍在。业务方可以进一步检查
// Model.Actual 判断是否降级到了非 AI 路径。
func (h HealthReport) Ready() bool {
	return h.State == HealthReady || h.State == HealthDegraded
}

// Health 检查 Runtime 是否就绪。
//
// 用 /health/ready 而不是 /health/live：后者只说明进程活着，
// 而业务方关心的是"能不能推理"。
//
// 返回值即使 State 不是 ready 也不作为错误——调用方可以据此
// 展示"正在加载模型"之类的提示。只有网络层失败才返回 error。
func (c *Client) Health(ctx context.Context) (*HealthReport, error) {
	ctx, cancel := c.withConnectTimeout(ctx)
	defer cancel()

	env, err := c.doWithoutRetry(ctx, "GET", "/health/ready", nil)
	if err != nil {
		// 503 时服务端仍返回了 HealthReport（在 data 字段）
		var apiErr *APIError
		if ok := asAPIError(err, &apiErr); ok && apiErr.StatusCode == 503 {
			// 无法取到 body，只能返回状态
			return &HealthReport{
				State:  HealthStarting,
				Reason: apiErr.Message,
			}, nil
		}
		return nil, err
	}

	var rep HealthReport
	if err := unmarshalJSON(env.Data, &rep); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &rep, nil
}

// Live 只检查进程是否存活（用于进程守护）。
func (c *Client) Live(ctx context.Context) error {
	ctx, cancel := c.withConnectTimeout(ctx)
	defer cancel()

	_, err := c.doWithoutRetry(ctx, "GET", "/health/live", nil)
	return err
}

// Capability 返回这台机器能做什么（对话 / RAG / 仅规则引擎）。
//
// ## 为什么调用方应该先查它
//
// 本地 Runtime 可能运行在内存很小的机器上。此时服务**活着**
// 但能力受限：
//
//   - RAGEnabled=false 时传 kb_ids 会被忽略——用户会以为
//     "检索过了，只是没找到"，而不是"知识库根本没启用"
//   - ChatEnabled=false 时只能走规则引擎（关键词匹配），
//     这不是"AI 降级"而是"没有 AI"
//
// 只看 Health 是不够的：Health 回答"服务是否活着"，
// Capability 回答"活着但能做什么"。
//
// 建议在启动时查一次并缓存：能力在 Runtime 启动时确定，
// 不会随请求变化（与 /resources 的实时压力不同）。
func (c *Client) Capability(ctx context.Context) (*Capability, error) {
	ctx, cancel := c.withConnectTimeout(ctx)
	defer cancel()

	env, err := c.doWithoutRetry(ctx, "GET", "/api/v1/capability", nil)
	if err != nil {
		return nil, err
	}
	var out Capability
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// ---------------------------------------------------------------------------
// 模型（文档 5.1）
// ---------------------------------------------------------------------------

// Model 是模型清单的对外视图。
//
// 字段与服务端 api.ModelSummary 严格对应。**不要凭印象加字段**——
// 服务端没有的字段在 JSON 里恒为零值，会造成"看起来有字段但永远
// 是 false"的错觉（开发中踩过：曾假设有 installed 字段，实际
// 服务端只有 loaded）。
type Model struct {
	ID string `json:"id"`
	// Name 是展示名（含参数量等，如 "Granite-4.0-H-350M Q4_K_M"）
	Name string `json:"name"`
	Type string `json:"type"`
	// Arch 是 gguf 中的架构标识（如 granitehybrid、qwen3）
	Arch         string `json:"arch"`
	Quantization string `json:"quantization"`
	// ContextLength 是模型**原生**上下文上限。
	//
	// 注意：实际运行时用的上下文通常远小于它（文档 21.3 要求
	// 按内存预算缩减）。要查实际值请读 ChatResult.Usage 或
	// Runtime 的 /api/v1/models/active 详细字段。
	ContextLength int      `json:"context_length"`
	Languages     []string `json:"languages"`
	// Capabilities 用于判断该模型能否承担某类任务
	// （如 rag、extraction、tool_calling）
	Capabilities []string `json:"capabilities"`
	// License 与 CommercialUse 必须核对：
	// 部分模型（如 LFM）有商用营收门槛（文档 4.1）
	License       string `json:"license"`
	CommercialUse string `json:"commercial_use"`
	// Loaded 表示当前是否已加载（同一时刻只有一个）
	Loaded bool `json:"loaded"`
	// SizeBytes 是模型文件总大小，便于判断磁盘与内存开销
	SizeBytes int64 `json:"size_bytes"`
	// Present 表示模型文件已在本地（可加载）。
	//
	// 与 Loaded 的区别：Present 是"文件在磁盘上"，
	// Loaded 是"已加载进内存"。
	Present bool `json:"present"`
}

// Models 列出所有已注册的模型。
//
// 用途：业务方据此选择可用模型，或检查某个能力（如 rag）是否
// 有模型支持。
//
// 返回第二个值是当前加载的模型 ID（可能为空）。
func (c *Client) Models(ctx context.Context) ([]Model, error) {
	list, _, err := c.modelsAndActive(ctx)
	return list, err
}

// modelsAndActive 一次请求同时取列表与当前模型。
func (c *Client) modelsAndActive(ctx context.Context) ([]Model, string, error) {
	env, err := c.do(ctx, "GET", "/api/v1/models", nil)
	if err != nil {
		return nil, "", err
	}

	var payload struct {
		Models []Model `json:"models"`
		Active string  `json:"active"`
		Total  int     `json:"total"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, "", wrapDecodeErr(err)
	}
	return payload.Models, payload.Active, nil
}

// ActiveModel 返回当前加载的模型 ID，未加载时返回空字符串。
//
// **未加载模型不是错误**：这是正常的初始状态（Runtime 刚启动、
// 模型刚被卸载）。返回 error 会迫使调用方额外判断——
// 而"没有模型"本来就是一种合法的答案。
//
// 实现上优先用 /models 的 active 字段，而不是 /models/active
// 端点——后者在无模型时返回 503，语义上把"正常状态"当成了错误。
// 这样 SDK 的行为与服务端的实际语义解耦。
func (c *Client) ActiveModel(ctx context.Context) (string, error) {
	_, active, err := c.modelsAndActive(ctx)
	if err != nil {
		return "", err
	}
	return active, nil
}

// LoadModel 加载指定模型（会卸载当前模型）。
//
// 这是耗时操作（可能数十秒）。ctx 应设置足够长的超时。
// 文档 19.9 规定当前是同步语义。
func (c *Client) LoadModel(ctx context.Context, modelID string) error {
	_, err := c.do(ctx, "POST", "/api/v1/models/load",
		map[string]string{"model_id": modelID})
	return err
}

// UnloadModel 卸载当前模型。
func (c *Client) UnloadModel(ctx context.Context) error {
	_, err := c.do(ctx, "POST", "/api/v1/models/unload", nil)
	return err
}

// ---------------------------------------------------------------------------
// 对话（文档 19.3）
// ---------------------------------------------------------------------------

// Quality 是质量档位，影响模型选择（文档 19.5）。
type Quality string

const (
	QualityFast     Quality = "fast"
	QualityBalanced Quality = "balanced"
	QualityBest     Quality = "best"
)

// Message 是一条对话消息。
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ChatRequest 是对话请求。
type ChatRequest struct {
	Messages []Message `json:"messages"`
	// MaxTokens 是输出上限。0 表示用服务端默认值。
	MaxTokens int `json:"max_tokens,omitempty"`
	// Temperature 为 0 时用贪心解码（结果可复现）。
	Temperature float64 `json:"temperature,omitempty"`
	// Quality 影响模型选择。
	Quality Quality `json:"quality,omitempty"`
	// DeadlineMs 是本次请求的时间预算。
	DeadlineMs int `json:"deadline_ms,omitempty"`
	// AllowDegrade 为 nil 时按服务端默认（允许降级）。
	//
	// 涉及金额、权限、删除类操作应显式设为 false
	// 并配合 MissionCritical（文档 27）。
	AllowDegrade *bool `json:"allow_degrade,omitempty"`
	// MissionCritical 表示该任务不允许降级。
	MissionCritical bool `json:"mission_critical,omitempty"`
	// JSONSchema 启用约束解码（结构化输出）。
	JSONSchema json.RawMessage `json:"json_schema,omitempty"`
	// EnableThinking 控制 Qwen3 系列的思考模式。
	EnableThinking *bool `json:"enable_thinking,omitempty"`
	// Priority 是任务优先级（文档 3.5）。
	Priority Priority `json:"priority,omitempty"`
	// ClientID 是调用方标识，用于 per-client 配额。
	ClientID string `json:"client_id,omitempty"`

	// Stream 表示使用 SSE 流式输出。
	//
	// **通常不需要手动设置**：用 Client.StreamChat / ChatStream
	// 时会自动置为 true。用 Client.Chat 时应保持 false，
	// 否则服务端返回 SSE 而 Chat 期望普通 JSON，解析会失败。
	Stream bool `json:"stream,omitempty"`

	// ---- 知识库（RAG）----

	// KBIDs 是检索的知识库。空表示不用知识库。
	KBIDs []string `json:"kb_ids,omitempty"`
	// KnowledgeQuery 覆盖默认检索查询（默认用最后一条 user 消息）。
	KnowledgeQuery string `json:"knowledge_query,omitempty"`
	// KnowledgeMode 是检索模式：hybrid / keyword / semantic。
	KnowledgeMode string `json:"knowledge_mode,omitempty"`
	// ContextTokens 是知识库上下文的 token 预算。0 表示自动。
	ContextTokens int `json:"context_tokens,omitempty"`
	// MaxKnowledgeChunks 是注入的片段数上限。
	MaxKnowledgeChunks int `json:"max_knowledge_chunks,omitempty"`
	// KnowledgeTopK 是检索候选条数（默认 8）。
	//
	// 比注入条数大：先多召回，再由拼接按预算筛选。
	KnowledgeTopK int `json:"knowledge_top_k,omitempty"`

	// ---- 多轮 ----

	// HistoryTokens 是历史消息的 token 预算。0 表示自动。
	HistoryTokens int `json:"history_tokens,omitempty"`
	// QueryStrategy 是检索查询构造策略。
	// 默认 auto（自动判断是否需要结合上一轮提问）。
	QueryStrategy string `json:"query_strategy,omitempty"`
}

// Priority 是任务优先级。
type Priority string

const (
	// PriorityInteractive 是用户主动触发的交互任务（P0），有预留槽位。
	PriorityInteractive Priority = "interactive"
	// PriorityNormal 是普通 AI 文本处理（P1）。
	PriorityNormal Priority = "normal"
	// PriorityBatch 是向量化、批量 Embedding（P2）。
	PriorityBatch Priority = "batch"
	// PriorityBackground 是后台索引更新（P3），最先被抢占。
	PriorityBackground Priority = "background"
)

// ChatResult 是对话结果。
//
// 关键设计：**降级不是错误**（文档 24.5）。降级结果照常返回，
// 通过 Degraded / DegradeReason 标记，业务方据此决定是否提示用户。
type ChatResult struct {
	// Content 是模型输出
	Content string
	// FinishReason 说明为何结束："stop" 正常、"length" 达到上限
	FinishReason string
	// Truncated 是 FinishReason == "length" 的便捷判断
	Truncated bool
	// Model 说明实际用了哪个模型
	Model *ModelInfo
	// Usage 是资源消耗
	Usage *Usage
	// Knowledge 说明如何使用知识库（未用时为 nil）
	Knowledge *KnowledgeInfo
	// Warnings 是服务端的提示（如上下文被裁剪）
	Warnings []string
	// RequestID 便于报问题时定位
	RequestID string
}

// Degraded 表示本次结果是否降级。
//
// 便捷方法：避免业务方每次判断 Model != nil && Model.Degraded。
func (r *ChatResult) Degraded() bool {
	return r.Model != nil && r.Model.Degraded
}

// DegradeReason 返回降级原因（未降级时为空）。
func (r *ChatResult) DegradeReason() string {
	if r.Model == nil {
		return ""
	}
	return r.Model.DegradeReason
}

// Chat 执行一次对话。
//
// 超时策略：用 TTFT 与 Total 两层。TTFT 是"首个响应字节"，
// 这里用 Total 作为整体上限（非流式请求无法单独控制首字节，
// 服务端返回即代表完整结果）。
func (c *Client) Chat(ctx context.Context, req *ChatRequest) (*ChatResult, error) {
	if req == nil {
		return nil, ErrInvalidRequest
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "POST", "/api/v1/chat", req)
	if err != nil {
		return nil, err
	}

	var payload struct {
		Content      string `json:"content"`
		FinishReason string `json:"finish_reason"`
		Truncated    bool   `json:"truncated"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}

	return &ChatResult{
		Content:      payload.Content,
		FinishReason: payload.FinishReason,
		Truncated:    payload.Truncated,
		Model:        env.Model,
		Usage:        env.Usage,
		Knowledge:    env.Knowledge,
		Warnings:     env.Warnings,
		RequestID:    env.RequestID,
	}, nil
}

// ---------------------------------------------------------------------------
// 检索（文档 22）
// ---------------------------------------------------------------------------

// SearchRequest 是知识库检索请求。
type SearchRequest struct {
	Query string `json:"query"`
	// KBIDs 必填。不允许全库检索（文档 22.9 的隔离要求）。
	KBIDs []string `json:"kb_ids"`
	// TopK 是返回条数，0 表示服务端默认（5）。
	TopK int `json:"top_k,omitempty"`
	// Mode 是检索模式：hybrid（默认）/ keyword / semantic。
	Mode string `json:"mode,omitempty"`
	// BuildContext 为 true 时额外返回拼好的 RAG 上下文。
	BuildContext bool `json:"build_context,omitempty"`
	// ContextTokens 是上下文预算，0 表示自动。
	ContextTokens int `json:"context_tokens,omitempty"`
	// MaxChunks 是注入的片段数上限。
	MaxChunks int `json:"max_chunks,omitempty"`
}

// SearchHit 是一条检索结果。
type SearchHit struct {
	ChunkID    string  `json:"chunk_id"`
	DocumentID string  `json:"document_id"`
	KBID       string  `json:"kb_id"`
	Ordinal    int     `json:"ordinal"`
	Text       string  `json:"text"`
	Score      float32 `json:"score"`
	// KeywordRank / SemanticRank 说明这条是怎么被召回的
	KeywordRank  int `json:"keyword_rank,omitempty"`
	SemanticRank int `json:"semantic_rank,omitempty"`
}

// SearchResult 是检索结果。
type SearchResult struct {
	Hits     []SearchHit
	Mode     string
	Degraded bool
	Note     string
	// Context 是拼好的 RAG 上下文（仅 BuildContext=true 时非 nil）
	Context *RAGContext
	// TotalMs 是检索耗时
	TotalMs int64
}

// RAGContext 是拼装好的上下文。
type RAGContext struct {
	// Text 是可直接放入 prompt 的完整文本（含防注入标记）
	Text            string
	EstimatedTokens int
	BudgetTokens    int
	// FixedOverheadTokens 是防注入声明 + 标题 + 指令占用的 token。
	//
	// 预算再小也省不掉。调用方应保证 ContextTokens 明显大于它，
	// 否则一条资料都放不进去（表现为 UsedChunks 恒为 0）。
	FixedOverheadTokens int
	UsedChunks          int
	DroppedChunks       int
	// Warnings 是裁剪与空结果提示，**应展示给用户**
	Warnings []string
	Sources  []KnowledgeSource
}

// Search 执行知识库检索。
func (c *Client) Search(ctx context.Context, req *SearchRequest) (*SearchResult, error) {
	if req == nil || req.Query == "" {
		return nil, ErrInvalidRequest
	}
	if len(req.KBIDs) == 0 {
		return nil, ErrInvalidRequest
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "POST", "/api/v1/search", req)
	if err != nil {
		return nil, err
	}

	var payload struct {
		Query    string      `json:"query"`
		Results  []SearchHit `json:"results"`
		Mode     string      `json:"mode"`
		Degraded bool        `json:"degraded"`
		Note     string      `json:"note"`
		TotalMs  int64       `json:"total_ms"`
		Context  *struct {
			Text                string            `json:"text"`
			EstimatedTokens     int               `json:"estimated_tokens"`
			BudgetTokens        int               `json:"budget_tokens"`
			FixedOverheadTokens int               `json:"fixed_overhead_tokens"`
			UsedChunks          int               `json:"used_chunks"`
			DroppedChunks       int               `json:"dropped_chunks"`
			Warnings            []string          `json:"warnings"`
			Sources             []KnowledgeSource `json:"sources"`
		} `json:"context"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}

	out := &SearchResult{
		Hits:     payload.Results,
		Mode:     payload.Mode,
		Degraded: payload.Degraded,
		Note:     payload.Note,
		TotalMs:  payload.TotalMs,
	}
	if payload.Context != nil {
		out.Context = &RAGContext{
			Text:                payload.Context.Text,
			EstimatedTokens:     payload.Context.EstimatedTokens,
			BudgetTokens:        payload.Context.BudgetTokens,
			FixedOverheadTokens: payload.Context.FixedOverheadTokens,
			UsedChunks:          payload.Context.UsedChunks,
			DroppedChunks:       payload.Context.DroppedChunks,
			Warnings:            payload.Context.Warnings,
			Sources:             payload.Context.Sources,
		}
	}
	return out, nil
}
