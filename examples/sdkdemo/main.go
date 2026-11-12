// Command sdkdemo 演示 Go SDK 的完整用法，并验证真实 Runtime。
//
// 它同时起到两个作用：
//  1. 作为 SDK 的可用示例（用户可以直接抄）
//  2. 作为端到端冒烟测试（连接真实 Runtime 跑通全链路）
//
// 运行前需先启动 Runtime：
//
//	localai-cli serve -token sdk-token -llama-server ./llama-server.exe
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/luping-ai/local-ai-sdk-go/localai"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "失败: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	// 1. 创建客户端 —— 不写死地址，自动从 discovery 文件发现
	client, err := localai.NewClient(localai.Config{
		// 留空 BaseURL 与 Token：自动按文档 18.4 的顺序发现
		Timeouts: localai.Timeouts{
			Connect: time.Second,
			TTFT:    15 * time.Second,
			Total:   120 * time.Second,
		},
	})
	if err != nil {
		return fmt.Errorf("创建客户端: %w", err)
	}

	d := client.Discovery()
	fmt.Printf("连接 %s（version=%s）\n\n", d.URL, d.Version)

	ctx := context.Background()

	// 2. 健康检查
	fmt.Println("=== 健康检查 ===")
	rep, err := client.Health(ctx)
	if err != nil {
		return fmt.Errorf("健康检查: %w", err)
	}
	fmt.Printf("  状态=%s 可服务=%v", rep.State, rep.Ready())
	if rep.Reason != "" {
		fmt.Printf(" 原因=%s", rep.Reason)
	}
	fmt.Println()

	// 3. 模型列表
	fmt.Println("\n=== 模型列表 ===")
	models, err := client.Models(ctx)
	if err != nil {
		return fmt.Errorf("获取模型: %w", err)
	}
	for _, m := range models {
		mark := " "
		if m.Loaded {
			mark = "*"
		}
		fmt.Printf("  %s %-32s 已加载=%-5v 能力=%s\n",
			mark, m.ID, m.Loaded, strings.Join(m.Capabilities, ","))
	}

	// 4. 加载模型（若未加载）
	active, err := client.ActiveModel(ctx)
	if err != nil {
		return fmt.Errorf("查询当前模型: %w", err)
	}
	if active == "" {
		fmt.Println("\n=== 加载模型 ===")
		if err := client.LoadModel(ctx, "granite-4.0-h-350m-q4"); err != nil {
			return fmt.Errorf("加载模型: %w", err)
		}
		active = "granite-4.0-h-350m-q4"
		fmt.Printf("  已加载 %s\n", active)
	}

	// 5. 非流式对话
	fmt.Printf("\n=== 非流式对话（当前模型 %s）===\n", active)
	res, err := client.Chat(ctx, &localai.ChatRequest{
		Messages: []localai.Message{
			{Role: "user", Content: "用一句话说明什么是本地 AI"},
		},
		MaxTokens: 60,
		Quality:   localai.QualityBalanced,
	})
	if err != nil {
		return fmt.Errorf("对话: %w", err)
	}
	fmt.Printf("  回答: %s\n", strings.TrimSpace(res.Content))
	fmt.Printf("  结束原因=%s 耗时=%dms TTFT=%dms\n",
		res.FinishReason, res.Usage.TotalMs, res.Usage.TTFTMs)
	// 降级不是错误——业务方据此决定是否提示用户
	if res.Degraded() {
		fmt.Printf("  ⚠ 已降级: %s\n", res.DegradeReason())
	}

	// 6. 流式对话
	fmt.Println("\n=== 流式对话 ===")
	fmt.Print("  ")
	var chunks int
	streamRes, err := client.ChatStream(ctx, &localai.ChatRequest{
		Messages: []localai.Message{
			{Role: "user", Content: "列举三种编程语言，简短"},
		},
		MaxTokens: 60,
	}, func(text string) error {
		chunks++
		fmt.Print(text)
		return nil
	})
	if err != nil {
		return fmt.Errorf("流式对话: %w", err)
	}
	fmt.Printf("\n  收到 %d 个增量，总字符 %d，TTFT=%dms\n",
		chunks, len([]rune(streamRes.Content)), streamRes.Usage.TTFTMs)

	// 7. 知识库全链路：建库 → 导入 → 向量化 → 检索 → 带 RAG 对话
	//
	// 先查能力：RAG 不可用时不该继续——传了 kb_ids 会被服务端忽略，
	// 用户会以为"检索过了只是没找到"，而实际是知识库没启用。
	if err := capabilityDemo(ctx, client); err != nil {
		return err
	}
	if err := knowledgeDemo(ctx, client); err != nil {
		return err
	}

	// 8. 超时与取消演示
	fmt.Println("\n=== 超时控制 ===")
	shortCtx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancel()
	_, err = client.Chat(shortCtx, &localai.ChatRequest{
		Messages:  []localai.Message{{Role: "user", Content: "写一篇长文章"}},
		MaxTokens: 500,
	})
	if err != nil {
		fmt.Printf("  预期超时: %v\n", err)
		if !errors.Is(err, localai.ErrTimeout) && !errors.Is(err, localai.ErrTaskCancelled) {
			fmt.Printf("  ⚠ 但错误类型意外: %v\n", err)
		}
	}

	// 8. 错误分类演示
	fmt.Println("\n=== 错误分类 ===")
	badClient, _ := localai.NewClient(localai.Config{
		BaseURL: "http://127.0.0.1:1", // 必然连不上
		NoRetry: true,
	})
	_, err = badClient.Chat(ctx, &localai.ChatRequest{
		Messages: []localai.Message{{Role: "user", Content: "hi"}},
	})
	if errors.Is(err, localai.ErrRuntimeUnavailable) {
		fmt.Println("  连接失败可被识别为 ErrRuntimeUnavailable（可提示用户启动 Runtime）")
	} else {
		fmt.Printf("  ⚠ 错误分类意外: %v\n", err)
	}

	fmt.Println("\n全部通过")
	return nil
}

// capabilityDemo 演示"先查能力再决定功能开关"的正确做法。
//
// 这是本地部署特有的问题：同一份代码可能跑在 32GB 工作站和
// 4GB 办公机上，能力完全不同。硬编码"知识库一定可用"会在低配机上
// 静默产生错误结果。
func capabilityDemo(ctx context.Context, client *localai.Client) error {
	fmt.Println("\n=== 能力检查 ===")

	cap, err := client.Capability(ctx)
	if err != nil {
		return fmt.Errorf("能力查询: %w", err)
	}

	fmt.Printf("  等级=%s  RAG=%v  对话=%v\n",
		cap.Level, cap.RAGEnabled, cap.ChatEnabled)
	if cap.Reason != "" {
		fmt.Printf("  说明: %s\n", cap.Reason)
	}
	if cap.RAGModel != "" {
		fmt.Printf("  RAG 将使用: %s\n", cap.RAGModel)
	}
	// 差多少要说清楚——只说"内存不足"用户无法判断该做什么
	if cap.BaselineNeedBytes > cap.AvailableBytes {
		fmt.Printf("  内存缺口: 需 %.1f GiB，可用 %.1f GiB\n",
			float64(cap.BaselineNeedBytes)/(1<<30),
			float64(cap.AvailableBytes)/(1<<30))
	}
	// 提示必须展示，不能静默丢弃
	for _, w := range cap.Warnings {
		fmt.Printf("  ⚠ %s\n", w)
	}

	if !cap.RAGEnabled {
		fmt.Println("  知识库检索不可用，跳过 RAG 演示")
		return nil
	}
	return nil
}

// knowledgeDemo 跑通知识库全链路。
//
// 为什么要放进 demo：0033 新增的这批方法此前只在单元测试里用
// 模拟服务端验证过。模拟数据是人写的，可能与人写的 SDK 结构体
// 犯同一个错误（两边都写错字段名，测试照样通过）。只有连真实
// Runtime 才能发现。
//
// 用临时文件而非仓库内文件：demo 可能在任何目录下运行。
func knowledgeDemo(ctx context.Context, client *localai.Client) error {
	fmt.Println("\n=== 知识库全链路 ===")

	// 建库：ID 带时间戳避免与已有库冲突（重复的 ID 会报错）
	kbID := fmt.Sprintf("sdkdemo-%d", time.Now().Unix())
	kb, err := client.CreateKB(ctx, &localai.CreateKBRequest{
		ID:          kbID,
		Name:        "SDK 演示库",
		Description: "sdkdemo 自动创建",
	})
	if err != nil {
		return fmt.Errorf("建库: %w", err)
	}
	fmt.Printf("  建库: %s\n", kb.ID)

	// 演示结束就删库，避免在用户的 Runtime 里留垃圾。
	// 放在 defer 里保证中途失败也会清理。
	defer func() {
		if err := client.DeleteKB(context.WithoutCancel(ctx), kbID); err != nil {
			fmt.Printf("  ⚠ 清理知识库失败: %v\n", err)
		} else {
			fmt.Printf("  已清理知识库 %s\n", kbID)
		}
	}()

	// 写一个临时文档：内容刻意包含一处"联系方式"与一处数字，
	// 便于验证检索确实命中了正确片段
	dir, err := os.MkdirTemp("", "sdkdemo")
	if err != nil {
		return fmt.Errorf("建临时目录: %w", err)
	}
	defer os.RemoveAll(dir)

	docPath := filepath.Join(dir, "guide.md")
	content := "# 售后服务指南\n\n" +
		"产品享受两年保修。保修期内非人为损坏免费维修。\n\n" +
		"如需帮助，请拨打客服电话 400-820-1234，服务时间为工作日九点到六点。\n\n" +
		"若设备无法联网，长按电源键十秒可恢复出厂设置。\n"
	if err := os.WriteFile(docPath, []byte(content), 0o600); err != nil {
		return fmt.Errorf("写临时文档: %w", err)
	}

	// 导入：只传路径，不传内容
	res, err := client.Import(ctx, kbID, &localai.ImportRequest{Path: docPath})
	if err != nil {
		return fmt.Errorf("导入: %w", err)
	}
	fmt.Printf("  导入: %d 个切片，%d 字符（去重=%v）\n",
		res.ChunkCount, res.ContentRunes, res.Duplicate)

	// 关键词检索在导入后立即可用（切片已入库，向量尚未生成）
	kws, err := client.Search(ctx, &localai.SearchRequest{
		Query: "客服电话", KBIDs: []string{kbID}, Mode: "keyword", TopK: 3,
	})
	if err != nil {
		return fmt.Errorf("关键词检索: %w", err)
	}
	fmt.Printf("  关键词检索: 命中 %d 条（向量化之前就能用）\n", len(kws.Hits))

	// 向量化：补齐语义检索能力
	vec, err := client.Vectorize(ctx, kbID, nil)
	if err != nil {
		// 未配置 Embedding 模型时是可判定错误，不是"坏了"
		if errors.Is(err, modelUnavailable()) {
			fmt.Printf("  向量化: 跳过（未配置 Embedding 模型，只能做关键词检索）\n")
			return nil
		}
		return fmt.Errorf("向量化: %w", err)
	}
	fmt.Printf("  向量化: %d 个切片\n", vec.Vectorized)

	// 统计
	st, err := client.KBStat(ctx, kbID)
	if err != nil {
		return fmt.Errorf("统计: %w", err)
	}
	fmt.Printf("  统计: 文档=%d 切片=%d 已向量化=%d\n",
		st.DocumentCount, st.ChunkCount, st.EmbeddedCount)

	// 完整问句检索：这是实测发现 AND 语义缺陷的场景
	// （"客服电话"命中，"客服电话是多少"曾 0 命中）
	q := "客服电话是多少"
	hy, err := client.Search(ctx, &localai.SearchRequest{
		Query: q, KBIDs: []string{kbID}, TopK: 3, BuildContext: true,
		ContextTokens: 400,
	})
	if err != nil {
		return fmt.Errorf("问句检索: %w", err)
	}
	if len(hy.Hits) == 0 {
		return fmt.Errorf("问句检索 %q 命中 0 条——关键词通道对完整问句失效", q)
	}
	hit := hy.Hits[0]
	fmt.Printf("  问句检索 %q: 命中 %d 条，top1 的 kw_rank=%d sem_rank=%d\n",
		q, len(hy.Hits), hit.KeywordRank, hit.SemanticRank)

	if hy.Context == nil {
		return fmt.Errorf("请求了 build_context 却没有 context")
	}
	// 上下文必须真的含答案——这是端到端的实质检查
	if !strings.Contains(hy.Context.Text, "400-820-1234") {
		return fmt.Errorf("RAG 上下文里没有电话号码，检索命中了错误的片段")
	}
	fmt.Printf("  RAG 上下文: %d token（其中固定开销 %d），含 %d 条来源\n",
		hy.Context.EstimatedTokens, hy.Context.FixedOverheadTokens,
		len(hy.Context.Sources))

	// 带知识库的对话：模型应能答出具体号码
	answer, err := client.Chat(ctx, &localai.ChatRequest{
		Messages:      []localai.Message{{Role: "user", Content: "客服电话是多少？"}},
		MaxTokens:     80,
		KBIDs:         []string{kbID},
		KnowledgeTopK: 3,
		ContextTokens: 400,
	})
	if err != nil {
		return fmt.Errorf("RAG 对话: %w", err)
	}
	fmt.Printf("  RAG 回答: %s\n", strings.TrimSpace(answer.Content))
	if answer.Knowledge == nil {
		return fmt.Errorf("用了知识库却没回传 knowledge")
	}
	fmt.Printf("  知识库信息: 用 %d 个切片，%d ms\n",
		answer.Knowledge.UsedChunks, answer.Knowledge.RetrievalMs)

	// 文档列表与删除
	docs, err := client.ListDocuments(ctx, kbID)
	if err != nil {
		return fmt.Errorf("文档列表: %w", err)
	}
	if len(docs) != 1 {
		return fmt.Errorf("文档数 = %d, 期望 1", len(docs))
	}
	fmt.Printf("  文档列表: 1 个（status=%s, index=%s）\n",
		docs[0].Status, docs[0].IndexStatus)

	return nil
}

// modelUnavailable 返回"无 Embedding 模型"对应的哨兵错误。
//
// 单独抽出来是为了让 demo 的依赖不散落在调用点——
// 服务端用 model_unavailable 表示"该能力未启用"。
func modelUnavailable() error { return localai.ErrModelUnavailable }
