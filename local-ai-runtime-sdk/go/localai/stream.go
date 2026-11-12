package localai

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// 流式对话（文档 19.3）
// ---------------------------------------------------------------------------

// StreamEvent 是流式输出的一个事件。
//
// Runtime 的事件顺序：queued(可能多次) → meta → warning(可选)
// → delta(多次) → usage → done。出错时会有 error 事件。
//
// 用指针字段 + Type 区分：同一时刻只有一个非 nil，因为 SSE 的
// 事件是异质的，Go 没有联合类型。调用方应先看 Type 再取字段。
type StreamEvent struct {
	// Type 是事件类型
	Type StreamEventType
	// Text 是增量文本（仅 delta 事件）
	Text string
	// Meta 是首帧信息（仅 meta 事件）
	Meta *StreamMeta
	// Queued 是排队位置（仅 queued 事件）
	Queued *QueuedInfo
	// Usage 是 token 统计与延迟（仅 usage 事件）
	Usage *Usage
	// FinishReason 是结束原因（仅 done 事件）
	FinishReason string
	// Err 是错误信息（仅 error 事件）
	Err error
}

// StreamEventType 是事件类型。
type StreamEventType string

const (
	// EventQueued 表示请求在排队（含排队位置）
	EventQueued StreamEventType = "queued"
	// EventMeta 表示流已建立，带模型与降级信息
	EventMeta StreamEventType = "meta"
	// EventDelta 是增量文本
	EventDelta StreamEventType = "delta"
	// EventUsage 是 token 统计
	EventUsage StreamEventType = "usage"
	// EventDone 表示正常结束
	EventDone StreamEventType = "done"
	// EventWarning 是服务端提示（如知识库未检索到资料）
	EventWarning StreamEventType = "warning"
	// EventError 是流中断
	EventError StreamEventType = "error"
)

// StreamMeta 是 meta 事件的内容。
type StreamMeta struct {
	RequestID string         `json:"request_id"`
	Model     string         `json:"model"`
	Degraded  bool           `json:"degraded"`
	Stream    bool           `json:"stream"`
	Knowledge *KnowledgeInfo `json:"knowledge,omitempty"`
}

// QueuedInfo 是排队事件的内容。
//
// 同一请求可能收到多次 queued 事件（位置变化时重推），
// 应以最后一次为准。
type QueuedInfo struct {
	Ahead             int    `json:"ahead"`
	AheadSamePriority int    `json:"ahead_same_priority"`
	Priority          string `json:"priority"`
	EstimatedWaitMs   int64  `json:"estimated_wait_ms"`
}

// StreamChat 执行流式对话。
//
// onEvent 每收到一个事件就被调用一次。返回 error 会中止流
// （同时取消对 Runtime 的请求，引擎侧也会停止生成——文档 19.4）。
//
// ## 两层超时在这里的分工
//
//   - TTFT：从发起到收到**第一个 delta 事件**（允许模型加载与排队）
//   - Total：整条流的上限
//
// 为什么 TTFT 必须单独控制：非流式请求只有"整个响应"一个时间点，
// 而流式能感知首字节。若只用 Total，模型加载慢时调用方无法
// 区分"卡住了"与"正在加载"。
//
// ## 关于流式请求不用 http.Client.Timeout
//
// 它会把整个流的时长算进去，而流式输出的合法时长取决于
// 输出长度。超时全部交给 context。
func (c *Client) StreamChat(ctx context.Context,
	req *ChatRequest, onEvent func(StreamEvent) error) (*ChatResult, error) {

	if req == nil {
		return nil, ErrInvalidRequest
	}

	// 强制 stream=true（调用方可能忘了设）
	streamReq := *req
	streamReq.Stream = true

	payload, err := json.Marshal(streamReq)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}

	// Total 超时
	totalCtx, cancelTotal := context.WithTimeout(ctx, c.timeouts.Total)
	defer cancelTotal()

	httpReq, err := http.NewRequestWithContext(totalCtx, "POST",
		c.baseURL+"/api/v1/chat", strings.NewReader(string(payload)))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")
	if c.token != "" {
		httpReq.Header.Set("Authorization", "Bearer "+c.token)
	}

	start := time.Now()
	resp, err := c.http.Do(httpReq)
	if err != nil {
		return nil, translateTransportErr(totalCtx, err)
	}
	defer resp.Body.Close()

	// 非 200：错误响应不是 SSE，可整体读取
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		// 流式路径的错误响应仍是 Envelope 格式
		_, perr := parseEnvelope(resp.StatusCode, raw, resp.Header.Get("X-Request-ID"))
		if perr != nil {
			return nil, perr
		}
		return nil, &APIError{
			Kind:       ErrRuntimeUnavailable,
			Message:    fmt.Sprintf("HTTP %d", resp.StatusCode),
			StatusCode: resp.StatusCode,
		}
	}

	return c.consumeStream(totalCtx, resp.Body, start, onEvent)
}

// consumeStream 解析 SSE 流并累积结果。
func (c *Client) consumeStream(ctx context.Context, body io.Reader,
	start time.Time, onEvent func(StreamEvent) error) (*ChatResult, error) {

	result := &ChatResult{}
	var sb strings.Builder

	// TTFT 计时：首个 delta 到达时记录
	var firstDeltaAt time.Time

	scanner := bufio.NewScanner(body)
	// 单帧可能较长，放宽上限
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)

	for scanner.Scan() {
		// 取消检查：调用方放弃后不必继续解析
		select {
		case <-ctx.Done():
			return result, wrapCtxErr(ctx.Err())
		default:
		}

		line := scanner.Text()
		if line == "" || strings.HasPrefix(line, ":") {
			continue // 空行或心跳注释
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}

		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "[DONE]" {
			break
		}
		if data == "" {
			continue
		}

		ev, err := decodeStreamEvent(data)
		if err != nil {
			// 单帧解析失败不终止：继续读后续内容
			continue
		}

		switch ev.Type {
		case EventMeta:
			result.Model = &ModelInfo{
				Actual:   ev.Meta.Model,
				Degraded: ev.Meta.Degraded,
			}
			result.RequestID = ev.Meta.RequestID
			result.Knowledge = ev.Meta.Knowledge

		case EventDelta:
			if firstDeltaAt.IsZero() {
				firstDeltaAt = time.Now()
				// TTFT 超时检查：若首帧超过 TTFT 配置，视为超时
				if ttft := firstDeltaAt.Sub(start); ttft > c.timeouts.TTFT {
					return result, fmt.Errorf("%w: 首字节耗时 %v 超过限制 %v",
						ErrTimeout, ttft.Round(time.Millisecond), c.timeouts.TTFT)
				}
			}
			sb.WriteString(ev.Text)
			result.Content += ev.Text

		case EventUsage:
			result.Usage = ev.Usage

		case EventDone:
			result.FinishReason = ev.FinishReason
			result.Truncated = ev.FinishReason == "length"

		case EventWarning:
			// 服务端提示：合并到 Warnings
			if ev.Text != "" {
				result.Warnings = append(result.Warnings, ev.Text)
			}

		case EventError:
			return result, ev.Err

		case EventQueued:
			// 排队事件只透传给调用方，不累积
		}

		if onEvent != nil {
			if err := onEvent(ev); err != nil {
				// 调用方主动中止：返回已累积的内容
				return result, err
			}
		}
	}

	if err := scanner.Err(); err != nil {
		return result, translateTransportErr(ctx, err)
	}

	if result.FinishReason == "" {
		result.FinishReason = "stop"
	}
	return result, nil
}

// streamWire 是单个 SSE 帧的结构。
type streamWire struct {
	Event string          `json:"event"`
	Data  json.RawMessage `json:"data"`
}

// decodeStreamEvent 解析一个事件。
func decodeStreamEvent(raw string) (StreamEvent, error) {
	var wire streamWire
	if err := json.Unmarshal([]byte(raw), &wire); err != nil {
		return StreamEvent{}, err
	}

	ev := StreamEvent{Type: StreamEventType(wire.Event)}

	switch ev.Type {
	case EventQueued:
		var q QueuedInfo
		if err := json.Unmarshal(wire.Data, &q); err == nil {
			ev.Queued = &q
		}
	case EventMeta:
		var m StreamMeta
		if err := json.Unmarshal(wire.Data, &m); err == nil {
			ev.Meta = &m
		}
	case EventDelta:
		var d struct {
			Text string `json:"text"`
		}
		if err := json.Unmarshal(wire.Data, &d); err == nil {
			ev.Text = d.Text
		}
	case EventUsage:
		var u Usage
		if err := json.Unmarshal(wire.Data, &u); err == nil {
			ev.Usage = &u
		}
	case EventDone:
		var d struct {
			FinishReason string `json:"finish_reason"`
		}
		if err := json.Unmarshal(wire.Data, &d); err == nil {
			ev.FinishReason = d.FinishReason
		}
	case EventWarning:
		// warning 事件的 data 是 {warnings: [...]}
		var w struct {
			Warnings []string `json:"warnings"`
		}
		if err := json.Unmarshal(wire.Data, &w); err == nil && len(w.Warnings) > 0 {
			ev.Text = strings.Join(w.Warnings, "；")
		}
	case EventError:
		var e struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		}
		if err := json.Unmarshal(wire.Data, &e); err == nil {
			ev.Err = &APIError{
				Kind:    kindForCode(e.Code),
				Code:    e.Code,
				Message: e.Message,
			}
		} else {
			ev.Err = fmt.Errorf("流中断")
		}
	}

	return ev, nil
}

// ChatStream 是 StreamChat 的简化形式：只关心文本增量。
//
// 内部把 delta 事件转成 onText 回调，其余事件忽略。
// 需要完整事件（如排队位置、usage）时用 StreamChat。
func (c *Client) ChatStream(ctx context.Context, req *ChatRequest,
	onText func(text string) error) (*ChatResult, error) {

	return c.StreamChat(ctx, req, func(ev StreamEvent) error {
		if ev.Type == EventDelta && ev.Text != "" && onText != nil {
			return onText(ev.Text)
		}
		return nil
	})
}
