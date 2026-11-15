package localai

// 本文件实现相似检测的三个只读端点（方案 §5.3）。
//
// ## 都是只读
//
// 检测只"告诉"哪几份看起来是同一份，不替用户决定保留哪一份。
// 用户的选择通过已有方法表达：
//
//	client.DeleteDocument(ctx, kb, b, false)
//	client.IngestChunks(ctx, kb, a)
//
// 因此没有 Apply / Move 方法。加这些会让 Runtime 承担"判断哪份
// 是权威版本"的责任——那个信息只存在于用户的脑子里。

import (
	"context"
	"fmt"
	"net/url"
	"strings"
)

// DedupCheckRequest 是相似检测的请求。
type DedupCheckRequest struct {
	// DocIDs 限定参与分组的文档。空表示该库全部。
	//
	// 注意语义：这是"哪些文档参与"，不是"只查这些"——
	// 相似是相对的，只给一份文档无法判断它是否重复。
	DocIDs []string `json:"doc_ids,omitempty"`
	// Recompute 为 true 时先补算缺失的指纹（完整但慢）。
	//
	// 默认 false 走快路径，缺席的文档在 Pending 里返回。
	Recompute bool `json:"recompute,omitempty"`
	// Async 为 true 时立即返回任务 ID，检测在后台以 P3 优先级运行。
	//
	// ## 什么时候该用
	//
	// 全库检测在 5000 篇规模下约 10 秒且纯 CPU 密集。同步执行会让
	// 客户端卡住，更糟的是会与用户提问抢 CPU——后台模式走调度器的
	// PriorityBackground，资源紧张时会被暂停甚至被 P0 抢占。
	//
	// 设置后返回的 DedupCheckResult.TaskID 非空、Groups 为空；
	// 用 Task(taskID) 轮询结果。
	Async bool `json:"async,omitempty"`
}

// DedupStats 是检测统计。
type DedupStats struct {
	Scanned    int `json:"scanned"`
	Reused     int `json:"reused"`
	Groups     int `json:"groups"`
	Candidates int `json:"candidates"`
}

// DupGroup 是一个相似组。
type DupGroup struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	// MaxHamming 是组内成员与代表的最大汉明距离
	MaxHamming int `json:"max_hamming"`
	// Members 是成员，**第一个是代表**（Hamming = 0）。
	//
	// "代表"只是"组内最中间的那份"，不是权威版本——
	// 那个决定权在用户。
	Members []DupMember `json:"members"`
}

// DupMember 是组成员。
type DupMember struct {
	DocumentID string `json:"document_id"`
	// Name 是显示名。快路径（未 Recompute）时可能为空。
	Name string `json:"name,omitempty"`
	// Hamming 是与代表的距离
	Hamming int `json:"hamming"`
}

// DedupCheckResult 是检测结果。
type DedupCheckResult struct {
	Groups []DupGroup
	// Pending 是还没有指纹、未参与本次检查的文档 ID。
	//
	// **非空意味着结果不完整**。不检查它就会把"刚导入的那份
	// 还没算指纹"误读成"没有重复"。
	Pending []string
	// Stats 仅在 Recompute = true 时有值
	Stats *DedupStats
	// TotalMs 是耗时
	TotalMs int64
	// TaskID 非空表示这是 Async 模式，检测在后台运行。
	//
	// 此时 Groups 与 Pending 都为空——用 Task(TaskID) 查进度与结果。
	TaskID string
}

// DedupGroupsResult 是列出已有分组的结果。
type DedupGroupsResult struct {
	Groups []DupGroup
	// ComputedAt 是上次检测时间（Unix 秒），0 表示从未检测过
	ComputedAt int64
	// Stale 表示库里有文档还没参与检测，结果**不完整**
	Stale bool
	// PendingCount 是没有指纹的文档数
	PendingCount int
}

// DiffBlock 是一个差异块。
type DiffBlock struct {
	Ordinal int    `json:"ordinal"`
	Preview string `json:"preview"`
}

// DiffChanged 是一处变更。
type DiffChanged struct {
	Ordinal int    `json:"ordinal"`
	Before  string `json:"before"`
	After   string `json:"after"`
}

// DupDiff 是两份文档的差异。
type DupDiff struct {
	DocA      string
	DocB      string
	Added     []DiffBlock
	Removed   []DiffBlock
	Changed   []DiffChanged
	Unchanged int
	// Truncated 表示结果被截断（单侧超过 200 块预览）
	Truncated bool
}

// DedupCheck 对指定文档做相似检测。
//
// req 可为 nil（等价于"检查全库、不重算指纹"）。
//
// ## 必须检查 Pending
//
// 快路径只对已有指纹的文档分组。刚导入的文档可能还没有指纹，
// 它们会出现在 Pending 里——此时"没有重复"这个结论是**不完整的**。
// 要拿到完整结果，传 Recompute: true（5000 篇约 10 秒）。
func (c *Client) DedupCheck(ctx context.Context, kbID string,
	req *DedupCheckRequest) (*DedupCheckResult, error) {

	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	if req == nil {
		req = &DedupCheckRequest{}
	}

	env, err := c.do(ctx, "POST",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/dedup/check", req)
	if err != nil {
		return nil, err
	}

	var payload struct {
		TaskID  string      `json:"task_id"`
		Groups  []DupGroup  `json:"groups"`
		Pending []string    `json:"pending"`
		Stats   *DedupStats `json:"stats"`
		TotalMs int64       `json:"total_ms"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}
	// async 模式返回 {task_id, status, priority, poll_after}，
	// 与同步模式的字段完全不同——TaskID 非空即表示"去轮询"。
	return &DedupCheckResult{
		Groups: payload.Groups, Pending: payload.Pending,
		Stats: payload.Stats, TotalMs: payload.TotalMs,
		TaskID: payload.TaskID,
	}, nil
}

// DedupGroups 列出库内已有的相似组（不重新计算）。
//
// 适合页面加载时展示。**必须检查 Stale**：为 true 时结果不完整。
func (c *Client) DedupGroups(ctx context.Context,
	kbID string) (*DedupGroupsResult, error) {

	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}

	env, err := c.do(ctx, "GET",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/dedup/groups", nil)
	if err != nil {
		return nil, err
	}

	var payload struct {
		Groups       []DupGroup `json:"groups"`
		ComputedAt   int64      `json:"computed_at"`
		Stale        bool       `json:"stale"`
		PendingCount int        `json:"pending_count"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &DedupGroupsResult{
		Groups: payload.Groups, ComputedAt: payload.ComputedAt,
		Stale: payload.Stale, PendingCount: payload.PendingCount,
	}, nil
}

// DedupDiff 计算两份文档的段落级差异。
//
// 两份文档必须属于同一知识库——跨库比较返回 ErrInvalidRequest，
// 因为不同库的文档本来就不该互相去重（文档 22.9 的隔离要求）。
func (c *Client) DedupDiff(ctx context.Context, kbID, docA, docB string) (*DupDiff, error) {
	if kbID == "" || docA == "" || docB == "" {
		return nil, fmt.Errorf("%w: kb_id / doc_a / doc_b 都不能为空",
			ErrInvalidRequest)
	}
	if docA == docB {
		return nil, fmt.Errorf("%w: doc_a 与 doc_b 不能是同一份文档",
			ErrInvalidRequest)
	}

	path := "/api/v1/knowledge/bases/" + url.PathEscape(kbID) +
		"/dedup/diff?doc_a=" + url.QueryEscape(docA) +
		"&doc_b=" + url.QueryEscape(docB)

	env, err := c.do(ctx, "GET", path, nil)
	if err != nil {
		return nil, err
	}

	var payload struct {
		DocA      string        `json:"doc_a"`
		DocB      string        `json:"doc_b"`
		Added     []DiffBlock   `json:"added"`
		Removed   []DiffBlock   `json:"removed"`
		Changed   []DiffChanged `json:"changed"`
		Unchanged int           `json:"unchanged"`
		Truncated bool          `json:"truncated"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &DupDiff{
		DocA: payload.DocA, DocB: payload.DocB,
		Added: payload.Added, Removed: payload.Removed,
		Changed: payload.Changed, Unchanged: payload.Unchanged,
		Truncated: payload.Truncated,
	}, nil
}

// DupGroupSummary 返回便于日志/UI 展示的一行摘要。
//
// 放在 SDK 而不是让每个调用方自己拼：不同调用方拼出的格式不一，
// 日志检索会变得困难。
func (g DupGroup) Summary() string {
	names := make([]string, 0, len(g.Members))
	for _, m := range g.Members {
		if m.Name != "" {
			names = append(names, m.Name)
		} else {
			names = append(names, m.DocumentID)
		}
	}
	return fmt.Sprintf("%s 组（%d 份，最大距离 %d）：%s",
		g.Kind, len(g.Members), g.MaxHamming, strings.Join(names, " / "))
}
