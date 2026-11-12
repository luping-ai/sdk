package localai

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// newTestClient 创建指向测试服务器的客户端。
func newTestClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)

	c, err := NewClient(Config{
		BaseURL: srv.URL,
		Token:   "test-token",
		// 测试里不要重试，避免把断言搞复杂
		NoRetry: true,
	})
	if err != nil {
		t.Fatalf("创建客户端: %v", err)
	}
	return c
}

// okEnvelope 构造成功响应。
func okEnvelope(data any) []byte {
	b, _ := json.Marshal(map[string]any{
		"success":    true,
		"request_id": "req_test",
		"data":       data,
	})
	return b
}

// errEnvelope 构造失败响应。
func errEnvelope(code, msg string, retryable bool, retryAfterMs int64) []byte {
	b, _ := json.Marshal(map[string]any{
		"success":    false,
		"request_id": "req_err",
		"error": map[string]any{
			"code":           code,
			"message":        msg,
			"retryable":      retryable,
			"retry_after_ms": retryAfterMs,
			"degradable":     false,
		},
	})
	return b
}

// ---------------------------------------------------------------------------
// 发现
// ---------------------------------------------------------------------------

func TestDiscoverExplicit(t *testing.T) {
	d, err := Discover("http://127.0.0.1:12345", "tok")
	if err != nil {
		t.Fatalf("发现失败: %v", err)
	}
	if d.URL != "http://127.0.0.1:12345" {
		t.Errorf("URL = %q", d.URL)
	}
	if d.Token != "tok" {
		t.Errorf("Token = %q", d.Token)
	}
}

// 尾部斜杠应被去掉（否则拼路径会变成双斜杠）。
func TestDiscoverTrimsTrailingSlash(t *testing.T) {
	d, _ := Discover("http://127.0.0.1:12345/", "")
	if strings.HasSuffix(d.URL, "/") {
		t.Errorf("尾部斜杠未去除: %q", d.URL)
	}
}

func TestDiscoverEnv(t *testing.T) {
	t.Setenv(EnvURL, "http://127.0.0.1:9999")
	t.Setenv(EnvToken, "env-token")

	d, err := Discover("", "")
	if err != nil {
		t.Fatalf("发现失败: %v", err)
	}
	if d.URL != "http://127.0.0.1:9999" {
		t.Errorf("URL = %q, 环境变量未生效", d.URL)
	}
	if d.Token != "env-token" {
		t.Errorf("Token = %q", d.Token)
	}
}

// 显式配置优先于环境变量。
func TestDiscoverExplicitOverridesEnv(t *testing.T) {
	t.Setenv(EnvURL, "http://127.0.0.1:9999")

	d, _ := Discover("http://127.0.0.1:1111", "")
	if d.URL != "http://127.0.0.1:1111" {
		t.Errorf("显式地址应优先, got %q", d.URL)
	}
}

func TestDiscoveryPath(t *testing.T) {
	p, err := DiscoveryPath()
	if err != nil {
		t.Fatalf("获取路径失败: %v", err)
	}
	if !strings.HasSuffix(p, "runtime.json") {
		t.Errorf("路径 = %q, 应以 runtime.json 结尾", p)
	}
}

// ---------------------------------------------------------------------------
// 三层超时
// ---------------------------------------------------------------------------

func TestTimeoutsDefaults(t *testing.T) {
	tt := Timeouts{}.withDefaults()
	if tt.Connect != time.Second {
		t.Errorf("Connect = %v, want 1s", tt.Connect)
	}
	if tt.TTFT != 15*time.Second {
		t.Errorf("TTFT = %v, want 15s", tt.TTFT)
	}
	if tt.Total != 60*time.Second {
		t.Errorf("Total = %v, want 60s", tt.Total)
	}
}

func TestTimeoutsCustom(t *testing.T) {
	tt := Timeouts{Connect: 500 * time.Millisecond, Total: 5 * time.Second}.withDefaults()
	if tt.Connect != 500*time.Millisecond {
		t.Errorf("Connect = %v", tt.Connect)
	}
	if tt.TTFT != 15*time.Second {
		t.Errorf("未设置的 TTFT 应取默认值, got %v", tt.TTFT)
	}
}

// 健康检查应受连接超时约束，不该等到总超时。
func TestHealthUsesConnectTimeout(t *testing.T) {
	// 服务器故意慢响应
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
		_, _ = w.Write(okEnvelope(map[string]any{"state": "ready"}))
	})
	c.timeouts.Connect = 100 * time.Millisecond

	start := time.Now()
	_, err := c.Health(context.Background())
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("应超时")
	}
	if elapsed > time.Second {
		t.Errorf("耗时 %v，未受连接超时约束", elapsed)
	}
	if !errors.Is(err, ErrTimeout) {
		t.Errorf("错误应为 ErrTimeout, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 类型化错误（文档 24.1）
// ---------------------------------------------------------------------------

func TestTypedErrors(t *testing.T) {
	cases := []struct {
		code     string
		wantKind error
	}{
		{"resource_limited", ErrResourceLimited},
		{"queue_full", ErrQueueFull},
		{"client_quota_exceeded", ErrClientQuota},
		{"model_unavailable", ErrModelUnavailable},
		{"capability_unmet", ErrCapabilityUnmet},
		{"invalid_request", ErrInvalidRequest},
		{"unauthorized", ErrUnauthorized},
	}

	for _, c := range cases {
		client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write(errEnvelope(c.code, "测试错误", false, 0))
		})

		_, err := client.Chat(context.Background(), &ChatRequest{
			Messages: []Message{{Role: "user", Content: "hi"}},
		})
		if err == nil {
			t.Errorf("%s: 应返回错误", c.code)
			continue
		}
		if !errors.Is(err, c.wantKind) {
			t.Errorf("%s: 错误类型不对，got %v", c.code, err)
		}
	}
}

// 未知错误码应归为 ErrRuntimeUnavailable（保守）。
func TestUnknownErrorCode(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write(errEnvelope("some_future_code", "未知", false, 0))
	})

	_, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if !errors.Is(err, ErrRuntimeUnavailable) {
		t.Errorf("未知码应归为 ErrRuntimeUnavailable, got %v", err)
	}
}

// APIError 应保留服务端的原始信息。
func TestAPIErrorDetails(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write(errEnvelope("queue_full", "队列已满，请稍后重试", true, 3000))
	})

	_, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})

	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("应能取到 APIError, got %v", err)
	}
	if apiErr.Code != "queue_full" {
		t.Errorf("Code = %q", apiErr.Code)
	}
	if apiErr.Message != "队列已满，请稍后重试" {
		t.Errorf("Message = %q", apiErr.Message)
	}
	if !apiErr.Retryable {
		t.Error("Retryable 应为 true")
	}
	if apiErr.RetryAfter != 3*time.Second {
		t.Errorf("RetryAfter = %v, want 3s", apiErr.RetryAfter)
	}
	if apiErr.RequestID != "req_err" {
		t.Errorf("RequestID = %q", apiErr.RequestID)
	}
	// 错误信息应包含关键要素，便于日志排查
	msg := err.Error()
	for _, want := range []string{"queue full", "queue_full", "队列已满"} {
		if !strings.Contains(msg, want) {
			t.Errorf("错误信息缺少 %q: %s", want, msg)
		}
	}
}

// ---------------------------------------------------------------------------
// 降级不是错误（文档 24.5）
// ---------------------------------------------------------------------------

func TestDegradedIsNotError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := json.Marshal(map[string]any{
			"success":    true,
			"request_id": "req_deg",
			"data": map[string]any{
				"content":       "降级后的回答",
				"finish_reason": "stop",
				"truncated":     false,
			},
			"model": map[string]any{
				"requested":      "qwen3-0.6b",
				"actual":         "granite-350m",
				"degraded":       true,
				"degrade_reason": "ram_pressure",
			},
			"warnings": []string{"context reduced"},
		})
		_, _ = w.Write(b)
	})

	res, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if err != nil {
		t.Fatalf("降级不应返回错误: %v", err)
	}

	// 必须能判断降级
	if !res.Degraded() {
		t.Error("Degraded() 应为 true")
	}
	if res.DegradeReason() != "ram_pressure" {
		t.Errorf("DegradeReason() = %q", res.DegradeReason())
	}
	// 内容照常返回
	if res.Content != "降级后的回答" {
		t.Errorf("Content = %q", res.Content)
	}
	// 模型信息完整
	if res.Model.Actual != "granite-350m" {
		t.Errorf("Actual = %q", res.Model.Actual)
	}
	if len(res.Warnings) == 0 {
		t.Error("warnings 应被保留")
	}
}

// 未降级时 Degraded() 应为 false 且不 panic。
func TestNotDegraded(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := json.Marshal(map[string]any{
			"success": true,
			"data":    map[string]any{"content": "ok", "finish_reason": "stop"},
			"model":   map[string]any{"requested": "m1", "actual": "m1", "degraded": false},
		})
		_, _ = w.Write(b)
	})

	res, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Degraded() {
		t.Error("不应标记降级")
	}
	if res.DegradeReason() != "" {
		t.Errorf("DegradeReason() = %q", res.DegradeReason())
	}
}

// ---------------------------------------------------------------------------
// 重试策略（文档 24.4）
// ---------------------------------------------------------------------------

// resource_limited 应自动重试。
func TestRetryOnResourceLimited(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write(errEnvelope("resource_limited", "资源不足", true, 10))
			return
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"content": "重试成功", "finish_reason": "stop",
		}))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, MaxRetries: 2})
	res, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if err != nil {
		t.Fatalf("重试后应成功: %v", err)
	}
	if res.Content != "重试成功" {
		t.Errorf("Content = %q", res.Content)
	}
	if calls.Load() != 2 {
		t.Errorf("调用次数 = %d, want 2（1 次失败 + 1 次重试）", calls.Load())
	}
}

// invalid_request 不应重试（重试必然再失败）。
func TestNoRetryOnInvalidRequest(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write(errEnvelope("invalid_request", "参数错误", false, 0))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, MaxRetries: 3})
	_, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Errorf("错误类型不对: %v", err)
	}
	if calls.Load() != 1 {
		t.Errorf("调用次数 = %d, want 1（不应重试）", calls.Load())
	}
}

// NoRetry 应完全关闭重试。
func TestNoRetryFlag(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write(errEnvelope("resource_limited", "资源不足", true, 10))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, NoRetry: true})
	_, _ = c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if calls.Load() != 1 {
		t.Errorf("NoRetry 下调用次数 = %d, want 1", calls.Load())
	}
}

// 重试次数应有上限。
func TestRetryLimit(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write(errEnvelope("resource_limited", "资源不足", true, 1))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, MaxRetries: 2})
	_, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if err == nil {
		t.Fatal("应最终失败")
	}
	// 1 次首次 + 2 次重试 = 3
	if calls.Load() != 3 {
		t.Errorf("调用次数 = %d, want 3", calls.Load())
	}
}

// ctx 取消时不应继续重试。
func TestRetryStopsOnCancel(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write(errEnvelope("resource_limited", "资源不足", true, 5000))
	}))
	defer srv.Close()

	c, _ := NewClient(Config{BaseURL: srv.URL, MaxRetries: 5})
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := c.Chat(ctx, &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("应返回错误")
	}
	if elapsed > 2*time.Second {
		t.Errorf("耗时 %v，取消后应尽快返回", elapsed)
	}
}

func TestJitter(t *testing.T) {
	base := time.Second
	for i := 0; i < 20; i++ {
		d := jitter(base)
		if d < 700*time.Millisecond || d > 1300*time.Millisecond {
			t.Errorf("jitter(%v) = %v，超出 ±30%% 范围", base, d)
		}
	}
	if jitter(0) != 0 {
		t.Error("jitter(0) 应为 0")
	}
}

// ---------------------------------------------------------------------------
// 连接失败
// ---------------------------------------------------------------------------

// 连不上时应是 ErrRuntimeUnavailable，而不是笼统的网络错误。
func TestConnectionRefused(t *testing.T) {
	// 用一个必然连不上的地址
	c, err := NewClient(Config{
		BaseURL: "http://127.0.0.1:1",
		NoRetry: true,
	})
	if err != nil {
		t.Fatal(err)
	}

	_, err = c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if !errors.Is(err, ErrRuntimeUnavailable) {
		t.Errorf("错误应为 ErrRuntimeUnavailable, got %v", err)
	}
}

// 非 JSON 响应（连到了别的服务）应可识别。
func TestNonJSONResponse(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("<html>this is not the runtime</html>"))
	})

	_, err := c.Chat(context.Background(), &ChatRequest{
		Messages: []Message{{Role: "user", Content: "hi"}},
	})
	if err == nil {
		t.Fatal("应返回错误")
	}
	if !errors.Is(err, ErrRuntimeUnavailable) {
		t.Errorf("错误应为 ErrRuntimeUnavailable, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 健康检查
// ---------------------------------------------------------------------------

func TestHealthReady(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"state":  "ready",
			"since":  time.Now().Unix(),
			"checks": map[string]any{"model_loaded": map[string]any{"ok": true}},
		}))
	})

	rep, err := c.Health(context.Background())
	if err != nil {
		t.Fatalf("健康检查失败: %v", err)
	}
	if rep.State != HealthReady {
		t.Errorf("State = %q", rep.State)
	}
	if !rep.Ready() {
		t.Error("Ready() 应为 true")
	}
}

// degraded 也算可用（只是能力受限）。
func TestHealthDegradedIsReady(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"state":  "degraded",
			"reason": "no model loaded",
		}))
	})

	rep, err := c.Health(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Ready() {
		t.Error("degraded 应算可服务")
	}
}

// ---------------------------------------------------------------------------
// 模型
// ---------------------------------------------------------------------------

func TestModels(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"models": []any{
				map[string]any{
					"id": "m1", "name": "模型一", "type": "llm",
					"capabilities":   []string{"qa", "rag"},
					"context_length": 2048,
					"loaded":         true,
					"active":         true,
				},
				map[string]any{
					"id": "m2", "name": "模型二", "type": "llm",
					"capabilities": []string{"extraction"},
					"loaded":       false,
				},
			},
		}))
	})

	models, err := c.Models(context.Background())
	if err != nil {
		t.Fatalf("获取模型列表失败: %v", err)
	}
	if len(models) != 2 {
		t.Fatalf("模型数 = %d", len(models))
	}
	if models[0].ID != "m1" || !models[0].Loaded {
		t.Errorf("模型一信息不对: %+v", models[0])
	}
	if models[1].Loaded {
		t.Error("模型二应未加载")
	}
	if len(models[0].Capabilities) != 2 {
		t.Errorf("capabilities 未解析: %v", models[0].Capabilities)
	}
}

// ---------------------------------------------------------------------------
// 检索
// ---------------------------------------------------------------------------

func TestSearch(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"query":    "保修期",
			"mode":     "hybrid",
			"total_ms": 12,
			"results": []any{
				map[string]any{
					"chunk_id": "c1", "document_id": "d1", "kb_id": "kb1",
					"text": "保修期为两年", "score": 0.89,
					"keyword_rank": 1, "semantic_rank": 2,
				},
			},
			"context": map[string]any{
				"text":                  "防注入声明...",
				"estimated_tokens":      200,
				"budget_tokens":         1024,
				"fixed_overhead_tokens": 60,
				"used_chunks":           1,
				"warnings":              []string{"裁剪了 1 条"},
				"sources": []any{
					map[string]any{"index": 1, "chunk_id": "c1", "source": "a.md"},
				},
			},
		}))
	})

	res, err := c.Search(context.Background(), &SearchRequest{
		Query: "保修期", KBIDs: []string{"kb1"}, BuildContext: true,
	})
	if err != nil {
		t.Fatalf("检索失败: %v", err)
	}
	if len(res.Hits) != 1 {
		t.Fatalf("结果数 = %d", len(res.Hits))
	}
	if res.Hits[0].KeywordRank != 1 || res.Hits[0].SemanticRank != 2 {
		t.Errorf("召回排名未解析: %+v", res.Hits[0])
	}
	if res.Context == nil {
		t.Fatal("context 应为非 nil")
	}
	if res.Context.EstimatedTokens != 200 {
		t.Errorf("EstimatedTokens = %d", res.Context.EstimatedTokens)
	}
	// 固定开销必须透传：调用方要靠它判断"预算是否小到放不下任何资料"
	if res.Context.FixedOverheadTokens != 60 {
		t.Errorf("FixedOverheadTokens = %d, 期望 60（字段丢失？）",
			res.Context.FixedOverheadTokens)
	}
	if len(res.Context.Warnings) == 0 {
		t.Error("warnings 应被保留")
	}
	if len(res.Context.Sources) != 1 || res.Context.Sources[0].Source != "a.md" {
		t.Errorf("sources 未解析: %+v", res.Context.Sources)
	}
}

// capability 端点应能解析，且能力受限不算错误。
func TestCapability(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/capability" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"level":               "rule_only",
			"reason":              "可用内存 400 MiB，连最小的可用模型都需要 1.0 GiB",
			"rag_enabled":         false,
			"chat_enabled":        false,
			"available_bytes":     400 << 20,
			"baseline_need_bytes": 1024 << 20,
			"warnings":            []string{"内存不足", "关闭其他程序后重启"},
		}))
	})

	cap, err := c.Capability(context.Background())
	if err != nil {
		t.Fatalf("能力查询不该因受限而报错: %v", err)
	}
	if cap.Level != CapabilityRuleOnly {
		t.Errorf("Level = %q", cap.Level)
	}
	if cap.RAGEnabled || cap.ChatEnabled {
		t.Error("rule_only 下两个开关都应为 false")
	}
	if cap.BaselineNeedBytes <= cap.AvailableBytes {
		t.Error("需要能看出差多少")
	}
	if len(cap.Warnings) == 0 {
		t.Error("warnings 不该丢失")
	}
}

// full 能力时的解析。
func TestCapabilityFull(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"level":        "full",
			"rag_enabled":  true,
			"chat_enabled": true,
			"rag_model":    "qwen2.5-3b-instruct-q4",
		}))
	})

	cap, err := c.Capability(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if cap.Level != CapabilityFull || !cap.RAGEnabled {
		t.Errorf("capability = %+v", cap)
	}
	if cap.RAGModel == "" {
		t.Error("应告知用哪个模型做 RAG")
	}
}
func TestSearchValidation(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{}))
	})

	if _, err := c.Search(context.Background(), nil); err == nil {
		t.Error("nil 请求应报错")
	}
	if _, err := c.Search(context.Background(), &SearchRequest{}); err == nil {
		t.Error("空 query 应报错")
	}
	// 没有 kb_ids 也不允许（文档 22.9 隔离要求）
	if _, err := c.Search(context.Background(), &SearchRequest{Query: "x"}); err == nil {
		t.Error("缺 kb_ids 应报错")
	}
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

func TestIsRetryable(t *testing.T) {
	if IsRetryable(nil) {
		t.Error("nil 不应可重试")
	}
	if !IsRetryable(ErrRuntimeUnavailable) {
		t.Error("连接失败应可重试")
	}
	if !IsRetryable(ErrTimeout) {
		t.Error("超时应可重试")
	}
	if IsRetryable(ErrInvalidRequest) {
		t.Error("参数错误不应可重试")
	}
}

func TestRetryAfterOf(t *testing.T) {
	err := &APIError{Kind: ErrQueueFull, RetryAfter: 5 * time.Second}
	if got := RetryAfterOf(err); got != 5*time.Second {
		t.Errorf("RetryAfterOf = %v", got)
	}
	if got := RetryAfterOf(ErrInvalidRequest); got != 0 {
		t.Errorf("非 APIError 应返回 0, got %v", got)
	}
}

func TestParseRetryAfter(t *testing.T) {
	if d := parseRetryAfter("3"); d != 3*time.Second {
		t.Errorf("秒数格式 = %v", d)
	}
	if d := parseRetryAfter(""); d != 0 {
		t.Errorf("空值 = %v", d)
	}
	if d := parseRetryAfter("garbage"); d != 0 {
		t.Errorf("非法值 = %v", d)
	}
}
