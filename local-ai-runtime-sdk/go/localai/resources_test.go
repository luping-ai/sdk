package localai

import (
	"context"
	"net/http"
	"testing"
)

// Resources 应能解析完整响应，包括嵌套的 capability 与 idle。
//
// 这两个嵌套对象是本方法最容易出错的地方：它们是可选字段，
// 解析漏了不会报错，只会让调用方拿到 nil 并以为"没有这个信息"。
func TestResources(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/resources" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_, _ = w.Write(okEnvelope(map[string]any{
			"pressure":          "BUSY",
			"available_bytes":   int64(900 << 20),
			"total_bytes":       int64(16 << 30),
			"available_percent": 5,
			"reason":            "可用内存占总量不足 8%",
			"consumers": []map[string]any{
				{"pid": 1234, "name": "chrome.exe",
					"bytes": int64(3 << 30), "readable_size": "3.0 GiB"},
			},
			"advice": map[string]any{
				"message": "内存紧张",
				"actions": []map[string]any{
					{"kind": "close_app", "text": "关闭 chrome.exe",
						"detail": "可释放 3.0 GiB"},
				},
			},
			"enforcement":      "enforced",
			"enforcement_note": "引擎上限 4.8 GiB",
			"capability": map[string]any{
				"level":          "full",
				"rag_enabled":    true,
				"chat_enabled":   true,
				"schema_version": 4,
			},
			"idle": map[string]any{
				"enabled":                 true,
				"model_idle_seconds":      120,
				"unload_after_seconds":    600,
				"user_idle_after_seconds": 300,
				"user_idle_detectable":    true,
				"user_idle_seconds":       400,
				"user_idle_source":        "GetLastInputInfo",
				"last_reason":             "user is active (2s)",
				"unloads_total":           int64(3),
			},
		}))
	})

	st, err := c.Resources(context.Background())
	if err != nil {
		t.Fatalf("查询资源状态失败: %v", err)
	}

	if st.Pressure != PressureBusy {
		t.Errorf("Pressure = %q", st.Pressure)
	}
	if st.AvailableBytes != 900<<20 || st.TotalBytes != 16<<30 {
		t.Errorf("内存字段 = %d/%d", st.AvailableBytes, st.TotalBytes)
	}
	if st.AvailablePercent != 5 {
		t.Errorf("AvailablePercent = %d", st.AvailablePercent)
	}
	if len(st.Consumers) != 1 {
		t.Fatalf("consumers = %+v", st.Consumers)
	}
	// readable_size 的 json 名与 Go 字段名不同，是最容易写错的一处
	if st.Consumers[0].Readable != "3.0 GiB" {
		t.Errorf("Readable = %q（应为 readable_size）", st.Consumers[0].Readable)
	}
	if st.Advice == nil || len(st.Advice.Actions) != 1 {
		t.Fatalf("advice = %+v", st.Advice)
	}
	if st.Advice.Actions[0].Kind != "close_app" {
		t.Errorf("action.kind = %q", st.Advice.Actions[0].Kind)
	}
	if st.Enforcement != "enforced" {
		t.Errorf("Enforcement = %q", st.Enforcement)
	}

	// 嵌套的 capability
	if st.Capability == nil {
		t.Fatal("capability 未解析（用户就看不出「能用什么」了）")
	}
	if !st.Capability.RAGEnabled || st.Capability.SchemaVersion != 4 {
		t.Errorf("capability = %+v", st.Capability)
	}

	// 嵌套的 idle
	if st.Idle == nil {
		t.Fatal("idle 未解析")
	}
	if !st.Idle.UserIdleDetectable || st.Idle.UserIdleSource != "GetLastInputInfo" {
		t.Errorf("idle = %+v", st.Idle)
	}
	if st.Idle.UnloadsTotal != 3 {
		t.Errorf("UnloadsTotal = %d", st.Idle.UnloadsTotal)
	}

	// Strained 的判据
	if !st.Strained() {
		t.Error("BUSY 应判为 strained")
	}
}

// S1-2：capability 与 idle 缺省时不报错，字段为 nil。
//
// 未启用推理的部署就是这样：没有能力评估，也没有需要卸载的模型。
// 抛异常会让"只看内存"的调用方在一个完全合法的部署上失败。
func TestResourcesWithoutCapabilityOrIdle(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"pressure":          "NORMAL",
			"available_bytes":   int64(8 << 30),
			"total_bytes":       int64(16 << 30),
			"available_percent": 50,
		}))
	})

	st, err := c.Resources(context.Background())
	if err != nil {
		t.Fatalf("缺省字段不该报错: %v", err)
	}
	if st.Capability != nil {
		t.Errorf("未提供时应为 nil，实际 %+v", st.Capability)
	}
	if st.Idle != nil {
		t.Errorf("未提供时应为 nil，实际 %+v", st.Idle)
	}
	if st.Advice != nil {
		t.Errorf("资源充足时不该有 advice，实际 %+v", st.Advice)
	}
	if st.Strained() {
		t.Error("NORMAL 不该判为 strained")
	}
}

// 所有压力等级到 Strained 的映射。
//
// 判据必须与"服务端自己会不会给警告"一致：LIMITED 是服务端开始
// ShouldWarn 的档位，所以它也算 strained——否则客户端要等到 BUSY
// 才让路，而那时服务端已经在提示用户关程序了。
func TestResourceStatusStrained(t *testing.T) {
	cases := []struct {
		p    PressureLevel
		want bool
	}{
		{PressureNormal, false},
		{PressureLimited, true},
		{PressureBusy, true},
		{PressureCritical, true},
		{"", false},
	}
	for _, tc := range cases {
		st := ResourceStatus{Pressure: tc.p}
		if got := st.Strained(); got != tc.want {
			t.Errorf("pressure=%q Strained() = %v, want %v",
				tc.p, got, tc.want)
		}
	}
}

// 健康报告必须解析 schema_version（S4-2）。
func TestHealthSchemaVersion(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"state":          "ready",
			"schema_version": 4,
		}))
	})

	rep, err := c.Health(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.SchemaVersion != 4 {
		t.Errorf("SchemaVersion = %d, want 4", rep.SchemaVersion)
	}
}

// 旧版 Runtime 不返回 schema_version 时应为 0 且不报错。
//
// 0 是"未知/过旧"的哨兵值，客户端据此决定只读浏览。
// 若这里抛错，升级前的部署会连健康检查都过不了。
func TestHealthSchemaVersionAbsent(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{"state": "ready"}))
	})

	rep, err := c.Health(context.Background())
	if err != nil {
		t.Fatalf("缺字段不该报错: %v", err)
	}
	if rep.SchemaVersion != 0 {
		t.Errorf("SchemaVersion = %d, want 0", rep.SchemaVersion)
	}
}

// Capability 也要解析 schema_version（客户端通常只查这一处）。
func TestCapabilitySchemaVersion(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(okEnvelope(map[string]any{
			"level":          "full",
			"rag_enabled":    true,
			"chat_enabled":   true,
			"schema_version": 4,
		}))
	})

	cap, err := c.Capability(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if cap.SchemaVersion != 4 {
		t.Errorf("SchemaVersion = %d, want 4", cap.SchemaVersion)
	}
}
