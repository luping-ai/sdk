package localai

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// sseServer 返回一个模拟 SSE 流的服务器。
func sseServer(t *testing.T, frames []string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		fl, ok := w.(http.Flusher)
		for _, f := range frames {
			_, _ = w.Write([]byte(f + "\n\n"))
			if ok {
				fl.Flush()
			}
		}
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Config{BaseURL: srv.URL, NoRetry: true})
	if err != nil {
		t.Fatalf("创建客户端: %v", err)
	}
	return c
}

// 标准事件序列应被正确聚合。
func TestStreamChatBasic(t *testing.T) {
	mode := ""
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"request_id":"req_1","model":"m1","degraded":false}}`,
		`data: {"event":"delta","data":{"text":"你好"}}`,
		`data: {"event":"delta","data":{"text":"，世界"}}`,
		`data: {"event":"usage","data":{"prompt_tokens":5,"completion_tokens":4,"ttft_ms":50,"total_ms":200,"queue_wait_ms":0,"prompt_tokens_may_be_incremental":true}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	var events []StreamEventType
	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}},
		func(ev StreamEvent) error {
			events = append(events, ev.Type)
			if ev.Type == EventMeta && ev.Meta != nil {
				mode = ev.Meta.Model
			}
			return nil
		})
	if err != nil {
		t.Fatalf("流式失败: %v", err)
	}

	if res.Content != "你好，世界" {
		t.Errorf("Content = %q", res.Content)
	}
	if res.FinishReason != "stop" {
		t.Errorf("FinishReason = %q", res.FinishReason)
	}
	if mode != "m1" {
		t.Errorf("meta 中的 model = %q", mode)
	}
	if res.Usage == nil || res.Usage.CompletionTokens != 4 {
		t.Errorf("usage 未解析: %+v", res.Usage)
	}
	// 不确定性标记必须透传：prompt_tokens 可能是增量，
	// 当完整长度算会严重低估上下文占用
	if res.Usage != nil && !res.Usage.PromptTokensMayBeIncremental {
		t.Error("PromptTokensMayBeIncremental 丢失")
	}
	if res.RequestID != "req_1" {
		t.Errorf("RequestID = %q", res.RequestID)
	}

	// 事件顺序应包含 meta / delta / usage / done
	want := []StreamEventType{EventMeta, EventDelta, EventDelta, EventUsage, EventDone}
	if len(events) != len(want) {
		t.Fatalf("事件数 = %d, want %d: %v", len(events), len(want), events)
	}
	for i := range want {
		if events[i] != want[i] {
			t.Errorf("事件[%d] = %q, want %q", i, events[i], want[i])
		}
	}
}

// 简化接口 ChatStream 只回调文本。
func TestChatStreamSimple(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"model":"m1"}}`,
		`data: {"event":"delta","data":{"text":"A"}}`,
		`data: {"event":"delta","data":{"text":"B"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	var sb strings.Builder
	res, err := c.ChatStream(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}},
		func(text string) error {
			sb.WriteString(text)
			return nil
		})
	if err != nil {
		t.Fatalf("失败: %v", err)
	}
	if sb.String() != "AB" {
		t.Errorf("拼接结果 = %q", sb.String())
	}
	if res.Content != "AB" {
		t.Errorf("Content = %q", res.Content)
	}
}

// 调用方返回 error 应中止流。
func TestStreamChatCallerAborts(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"delta","data":{"text":"A"}}`,
		`data: {"event":"delta","data":{"text":"B"}}`,
		`data: {"event":"delta","data":{"text":"C"}}`,
		`data: [DONE]`,
	})

	stopErr := errors.New("调用方主动停止")
	calls := 0
	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}},
		func(ev StreamEvent) error {
			calls++
			if calls >= 2 {
				return stopErr
			}
			return nil
		})

	if !errors.Is(err, stopErr) {
		t.Errorf("应返回调用方的错误, got %v", err)
	}
	// 已收到的内容应保留（部分结果也有价值）
	if res.Content != "AB" {
		t.Errorf("已累积内容 = %q, want AB", res.Content)
	}
	if calls != 2 {
		t.Errorf("回调次数 = %d, want 2（中止后不应继续）", calls)
	}
}

// ctx 取消应中断流。
func TestStreamChatContextCancel(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		fl := w.(http.Flusher)
		_, _ = w.Write([]byte(`data: {"event":"delta","data":{"text":"A"}}` + "\n\n"))
		fl.Flush()
		// 之后停住不发送，等 ctx 取消
		<-r.Context().Done()
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, NoRetry: true})
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := c.StreamChat(ctx,
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("应因超时失败")
	}
	if elapsed > 3*time.Second {
		t.Errorf("耗时 %v，取消后应尽快返回", elapsed)
	}
}

// error 事件应转为错误。
func TestStreamChatErrorEvent(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"model":"m1"}}`,
		`data: {"event":"error","data":{"code":"resource_limited","message":"资源不足"}}`,
	})

	_, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err == nil {
		t.Fatal("应返回错误")
	}
	if !errors.Is(err, ErrResourceLimited) {
		t.Errorf("错误类型不对: %v", err)
	}
}

// warning 事件应进入 Warnings。
func TestStreamChatWarning(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"model":"m1"}}`,
		`data: {"event":"warning","data":{"warnings":["知识库未检索到资料"]}}`,
		`data: {"event":"delta","data":{"text":"回答"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatalf("失败: %v", err)
	}
	if len(res.Warnings) == 0 {
		t.Error("warning 应被保留")
	}
	if !strings.Contains(res.Warnings[0], "知识库") {
		t.Errorf("Warnings = %v", res.Warnings)
	}
}

// meta 事件中的知识库信息应被解析，含 sources。
//
// sources 曾只存在于非流式路径：服务端的流式 meta 手挑字段时漏了它，
// 导致流式下前端做"引用来源"永远是空列表。SDK 这一层也必须解出来，
// 否则即使服务端修好了，SDK 仍会丢。
func TestStreamChatKnowledgeMeta(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"model":"m1","knowledge":{"kb_ids":["kb1"],"used_chunks":2,"context_tokens":300,"retrieval_ms":12,"sources":[{"index":1,"chunk_id":"c1","document_id":"d1","source":"手册.md","score":0.9}]}}}`,
		`data: {"event":"delta","data":{"text":"x"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Knowledge == nil {
		t.Fatal("knowledge 未解析")
	}
	if res.Knowledge.UsedChunks != 2 || res.Knowledge.ContextTokens != 300 {
		t.Errorf("knowledge = %+v", res.Knowledge)
	}
	if res.Knowledge.RetrievalMs != 12 {
		t.Errorf("retrieval_ms = %d", res.Knowledge.RetrievalMs)
	}
	// 流式必须与非流式一样能拿到引用来源
	if len(res.Knowledge.Sources) == 0 {
		t.Fatal("sources 未解析——流式下的引用来源不该丢失")
	}
	if res.Knowledge.Sources[0].ChunkID != "c1" ||
		res.Knowledge.Sources[0].Source != "手册.md" {
		t.Errorf("sources = %+v", res.Knowledge.Sources[0])
	}
}

// 降级信息应可从流式结果读取。
func TestStreamChatDegraded(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"meta","data":{"model":"granite-350m","degraded":true}}`,
		`data: {"event":"delta","data":{"text":"降级回答"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Degraded() {
		t.Error("应标记降级")
	}
	if res.Model.Actual != "granite-350m" {
		t.Errorf("Actual = %q", res.Model.Actual)
	}
}

// 非 200 的流式请求应返回结构化错误。
func TestStreamChatHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write(errEnvelope("model_unavailable", "没有模型", true, 0))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, NoRetry: true})
	_, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)

	if !errors.Is(err, ErrModelUnavailable) {
		t.Errorf("错误类型不对: %v", err)
	}
}

// 排队事件应被透传（不累积进结果）。
func TestStreamChatQueuedEvent(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"queued","data":{"ahead":2,"ahead_same_priority":1,"priority":"normal","estimated_wait_ms":3000}}`,
		`data: {"event":"meta","data":{"model":"m1"}}`,
		`data: {"event":"delta","data":{"text":"x"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	var sawQueued bool
	var queued *QueuedInfo
	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}},
		func(ev StreamEvent) error {
			if ev.Type == EventQueued {
				sawQueued = true
				queued = ev.Queued
			}
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if !sawQueued {
		t.Error("排队事件应被透传")
	}
	// 排队位置必须真的解析出来：QueuedInfo 曾是个只声明、从未接入
	// 解析的死类型，调用方拿到 nil 却看不出哪里错了
	if queued == nil {
		t.Fatal("ev.Queued 为 nil —— 排队位置被丢弃")
	}
	if queued.Ahead != 2 || queued.Priority != "normal" {
		t.Errorf("排队位置未解析: %+v", queued)
	}
	if res.Content != "x" {
		t.Errorf("Content = %q", res.Content)
	}
}

// 同一请求可能收到多次 queued 事件，各自都要能解析。
func TestStreamChatQueuedRepeated(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"queued","data":{"ahead":5,"priority":"batch"}}`,
		`data: {"event":"queued","data":{"ahead":2,"priority":"batch"}}`,
		`data: {"event":"queued","data":{"ahead":0,"priority":"batch"}}`,
		`data: {"event":"delta","data":{"text":"ok"}}`,
		`data: [DONE]`,
	})

	var aheads []int
	_, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}},
		func(ev StreamEvent) error {
			if ev.Type == EventQueued && ev.Queued != nil {
				aheads = append(aheads, ev.Queued.Ahead)
			}
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if len(aheads) != 3 {
		t.Fatalf("收到 %d 次排队事件，期望 3", len(aheads))
	}
	// 调用方应以最后一次为准
	if aheads[2] != 0 {
		t.Errorf("最后一次 ahead = %d, 期望 0", aheads[2])
	}
}

// 畸形帧应被跳过而不是终止整个流。
func TestStreamChatMalformedFrame(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"delta","data":{"text":"A"}}`,
		`data: {this is not valid json`, // 畸形
		`data: {"event":"delta","data":{"text":"B"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatalf("畸形帧不应终止流: %v", err)
	}
	if res.Content != "AB" {
		t.Errorf("Content = %q（跳过了畸形帧）", res.Content)
	}
}

// 心跳注释行应被忽略。
func TestStreamChatHeartbeat(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"delta","data":{"text":"A"}}`,
		`: keep-alive`,
		`data: {"event":"delta","data":{"text":"B"}}`,
		`data: {"event":"done","data":{"finish_reason":"stop"}}`,
		`data: [DONE]`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Content != "AB" {
		t.Errorf("Content = %q", res.Content)
	}
}

// 没有 done 事件时应默认 finish_reason=stop（避免业务方读到空值）。
func TestStreamChatMissingDone(t *testing.T) {
	c := sseServer(t, []string{
		`data: {"event":"delta","data":{"text":"x"}}`,
	})

	res, err := c.StreamChat(context.Background(),
		&ChatRequest{Messages: []Message{{Role: "user", Content: "hi"}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.FinishReason != "stop" {
		t.Errorf("FinishReason = %q, want stop（缺省值）", res.FinishReason)
	}
}

var _ = fmt.Sprint
