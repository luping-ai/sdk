# Local AI Hub Go SDK

Runtime 的官方 Go 客户端。**零外部依赖**（只用标准库），因此引入它不会
给你的项目带来任何传递依赖。

```bash
go get github.com/luping-ai/sdk/local-ai-runtime-sdk/go
```

```go
import "github.com/luping-ai/sdk/local-ai-runtime-sdk/go/localai"
```

> SDK 是**独立 Go 模块**，位于多语言 monorepo
> [github.com/luping-ai/sdk](https://github.com/luping-ai/sdk) 的
> `local-ai-runtime-sdk/go/` 目录，不依赖 Runtime 主模块。
> 这一点是刻意的：第三方项目引入 SDK 时不该连带拉进 SQLite、PDF 解析
> 这些只在服务端用得上的库。
>
> 本模块只包含客户端（`localai` 包）与示例。Runtime 服务端本体是私有仓库，
> 客户端不需要它也能编译——SDK 只通过 HTTP 与本机 Runtime 通信。

---

## 一、它不做什么

先明确边界，避免误用：

| 不负责 | 原因 |
| --- | --- |
| **启动 Runtime** | 任何自动拉起必须对用户可见（ADR 0002）。启动交给安装器或系统自启项 |
| **下载模型** | 那是运维动作。用 `localai-cli models download` 或管理界面 |
| **管理配置** | `/api/v1/admin/*` 需要 `-enable-admin`，且当前与推理共用 token，不适合暴露给业务代码 |
| **重试策略定制** | 内置了够用的策略（见第五章），要更复杂请用 `Chat` 之外的路径自行控制 |

---

## 二、三分钟上手

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"log"

	"github.com/luping-ai/sdk/local-ai-runtime-sdk/go/localai"
)

func main() {
	// 零配置：自动从 discovery 文件找到地址与 token
	client, err := localai.NewClient(localai.Config{})
	if err != nil {
		log.Fatal(err)
	}

	ctx := context.Background()

	// 先问"这台机器能做什么"——见第三章
	cap, err := client.Capability(ctx)
	if err != nil {
		log.Fatal(err)
	}
	if !cap.ChatEnabled {
		log.Fatalf("本机无法进行 AI 对话：%s", cap.Reason)
	}

	res, err := client.Chat(ctx, &localai.ChatRequest{
		Messages: []localai.Message{
			{Role: "user", Content: "把订单导出成 Excel"},
		},
		MaxTokens: 200,
	})
	if err != nil {
		// 类型化错误：见第五章
		if errors.Is(err, localai.ErrRuntimeUnavailable) {
			log.Fatal("本地 AI 未运行，请先启动 Local AI Hub")
		}
		log.Fatal(err)
	}

	fmt.Println(res.Content)

	// 降级不是错误，但要告诉用户
	if res.Degraded() {
		log.Printf("已降级：%s", res.DegradeReason())
	}
}
```

可运行的完整示例见 [`examples/sdkdemo`](examples/sdkdemo/main.go)，
它把本文所有能力串了一遍，同时兼作端到端冒烟测试。

---

## 三、连接：不要写死端口

端口是**动态分配**的（18765 被占用时会换）。SDK 按以下顺序查找 Runtime：

| 顺序 | 来源 | 用途 |
| --- | --- | --- |
| 1 | `Config{BaseURL, Token}` | 单元测试、连远程实例 |
| 2 | 环境变量 `LAIH_URL` / `LAIH_TOKEN` | 容器、CI |
| 3 | **discovery 文件** | 正常情况走这里 |
| 4 | `http://127.0.0.1:18765` | 兜底（无 token，通常调不通） |

discovery 文件位置（与 Runtime 侧一致）：

| 系统 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\LocalAIHub\runtime.json` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/local-ai-hub/runtime.json` |
| macOS | `~/.local-ai-hub/runtime.json` |

文件内容形如：

```json
{
  "url": "http://127.0.0.1:49213",
  "token": "laih_xxxxxxxxxxxxxxxx",
  "pid": 12345,
  "version": "0.2.0",
  "api": "v1",
  "started_at": 1762900000,
  "state": "ready"
}
```

**token 会变**：管理后台"重置 token"或 Runtime 重启后重新生成。SDK 在
`NewClient` 时读一次，所以你应当：

- 收到 `ErrUnauthorized` 后**重新 `NewClient`**，而不是无限重试同一个 client
- 长驻进程定期（或收到 401 时）重建 client

```go
func (a *App) ensureClient() (*localai.Client, error) {
	if a.client != nil {
		return a.client, nil
	}
	c, err := localai.NewClient(localai.Config{})
	if err != nil {
		return nil, err
	}
	a.client = c
	return c, nil
}

// 在调用处：401 就清掉，让下次重建
if errors.Is(err, localai.ErrUnauthorized) {
	a.client = nil
}
```

想自己读文件诊断：

```go
path, _ := localai.DiscoveryPath()
d := client.Discovery()      // 实际使用的 url/token/version
fmt.Println(path, d.URL, d.Version)
```

---

## 四、先查能力，再决定功能开关

`Health` 回答"服务活着吗"，`Capability` 回答"活着但能做什么"。
**本地 Runtime 可能运行在内存很小的机器上**，此时服务正常但能力受限。

```go
cap, err := client.Capability(ctx)
```

| 字段 | 含义 |
| --- | --- |
| `Level` | `full` 对话+知识库 / `chat_only` 只有对话 / `rule_only` 只有规则引擎 |
| `ChatEnabled` | 能否做模型对话。false 时 `Chat` 会走规则引擎或报错 |
| `RAGEnabled` | 知识库检索是否可用。**false 时不要传 `KBIDs`** |
| `Reason` | 一句话说明"为什么是这个等级"，适合直接展示给用户 |
| `BaselineNeedBytes` | 基准模型需要多少内存，用于向用户解释"还差多少" |
| `Warnings` | 必须展示的提示，**不要静默丢弃** |

建议**启动时查一次并缓存**：能力在 Runtime 启动时确定，不随请求变化
（与 `/resources` 的实时压力不同）。

`Health` 的语义：

```go
rep, err := client.Health(ctx)
if err != nil {
	// 只有网络层失败才是 error
}
if rep.State == localai.HealthStarting {
	// 正在加载模型，UI 显示"准备中"
}
// ready 与 degraded 都算可用
if rep.Ready() { /* 可以发请求 */ }
```

> `degraded` **不是故障**：它表示能力受限（如只能走规则引擎），服务仍在。

---

## 五、错误处理：类型化错误是核心

不要用字符串匹配判断错误——换语言、改措辞就失效，而误判会让你在
该重试时放弃、不该重试时死循环。

```go
var apiErr *localai.APIError
if errors.As(err, &apiErr) {
	log.Printf("code=%s status=%d retryable=%v request_id=%s",
		apiErr.Code, apiErr.StatusCode, apiErr.Retryable, apiErr.RequestID)
}
```

### 哨兵错误一览

| 哨兵错误 | 服务端 code | 可重试 | 该怎么处理 |
| --- | --- | --- | --- |
| `ErrRuntimeUnavailable` | `internal_error` / 网络层 | 是 | 提示"本地 AI 未运行"，稍后重试 |
| `ErrUnauthorized` | `unauthorized` / `forbidden` | **否** | 重建 client（重读 discovery） |
| `ErrResourceLimited` | `resource_limited` | 是 | 按 `RetryAfter` 退避；同时提示用户关掉占内存的程序 |
| `ErrQueueFull` | `queue_full` | 是 | 请求太多，退避重试 |
| `ErrClientQuota` | `client_quota_exceeded` | 是 | **你自己**请求太多，降低频率 |
| `ErrModelUnavailable` | `model_unavailable` | 是 | 模型可能在加载，等一下 |
| `ErrCapabilityUnmet` | `capability_unmet` | **否** | 有模型能装下但被白名单/能力要求挡住 → 改配置或换模型（关程序没用） |
| `ErrInputTooLong` | — | **否** | 缩短输入或换更大上下文的模型 |
| `ErrNotFound` | `model_not_found` / `not_found` | **否** | 模型要下载/换；知识库要检查 ID |
| `ErrInvalidRequest` | `invalid_request` | **否** | 代码 bug，改参数 |
| `ErrTaskCancelled` | — | **否** | 调用方主动取消或 ctx 结束 |
| `ErrTimeout` | — | 是 | 调大 `Timeouts.Total` |

`ErrQueueFull` 与 `ErrClientQuota`、`ErrResourceLimited` 的区别很重要：

- **队列满** = 整体请求量过大，谁都会受影响
- **客户端配额** = 只有你受影响，其他客户端正常 → 该降你自己的频率
- **资源不足** = 机器能力不够（内存紧张）→ 关程序才有用

`ErrCapabilityUnmet` 与 `ErrResourceLimited` 也是刻意分开的：前者是
"关掉其他程序没用，得换模型或改配置"。

### 自己控制重试

```go
if localai.IsRetryable(err) {
	time.Sleep(localai.RetryAfterOf(err))   // 服务端建议的等待
	// 重试
}
```

SDK 内部已按默认策略重试（最多 2 次，见第六章）。想完全接管：

```go
client, _ := localai.NewClient(localai.Config{NoRetry: true})
```

---

## 六、超时：三层而不是一个

本地推理的首字节延迟可能很长（冷启动要加载模型，几十秒），单一超时
无法兼顾"快速发现 Runtime 死了"与"允许模型慢慢加载"。

| 层 | 默认 | 覆盖什么 |
| --- | --- | --- |
| `Connect` | 1s | 判断 Runtime 是否活着（`Health` 用这个） |
| `TTFT` | 15s | 距**第一个 delta** 的时间，允许加载与排队 |
| `Total` | 60s | 整条流/整个请求的上限 |

```go
client, _ := localai.NewClient(localai.Config{
	Timeouts: localai.Timeouts{
		Connect: time.Second,
		TTFT:    20 * time.Second,
		Total:   5 * time.Minute,   // 大模型 + 长输出
	},
})
```

**加载模型**这种操作建议直接给一个长 ctx：

```go
ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
defer cancel()
err := client.LoadModel(ctx, "qwen2.5-1.5b-instruct-q4")
```

> `MaxRetries` 的零值有歧义（"没设置"还是"不重试"），所以关闭重试是
> 排除式命名：`NoRetry: true` 或 `MaxRetries: -1`。

---

## 七、对话

### 非流式

```go
res, err := client.Chat(ctx, &localai.ChatRequest{
	Messages: []localai.Message{
		{Role: "system", Content: "你是企业客服助手，回答简短。"},
		{Role: "user", Content: "保修期多久？"},
	},
	MaxTokens:   200,
	Temperature: 0,          // 0 = 贪心解码，结果可复现
	Quality:     localai.QualityBalanced,
})
```

`ChatResult` 里几个容易忽略的字段：

| 字段 | 说明 |
| --- | --- |
| `Truncated` | `FinishReason == "length"`，输出被截断。**不检查它会把半截结果当完整结果** |
| `Warnings` | 服务端提示（如上下文被裁剪），应展示给用户 |
| `RequestID` | 用户报问题时提供它，能直接定位日志 |
| `Usage.QueueWaitMs` | 排队耗时。总耗时 = 排队 + 推理，区分后才知道该优化哪边 |

### 流式

```go
// 只要文本：简单版
_, err := client.ChatStream(ctx, req, func(text string) error {
	fmt.Print(text)
	return nil
})

// 要完整事件（排队位置、usage）：用 StreamChat
res, err := client.StreamChat(ctx, req, func(ev localai.StreamEvent) error {
	switch ev.Type {
	case localai.EventQueued:
		fmt.Printf("\r排队中，前面 %d 个…", ev.Queued.Ahead)
	case localai.EventDelta:
		fmt.Print(ev.Text)
	case localai.EventWarning:
		fmt.Printf("\n[提示] %s\n", ev.Text)
	case localai.EventUsage:
		fmt.Printf("\n[耗时] TTFT %dms 总 %dms\n", ev.Usage.TTFTMs, ev.Usage.TotalMs)
	case localai.EventDone:
		fmt.Println()
	}
	return nil
})
```

事件顺序：`queued`（可能多次）→ `meta` → `warning`（可选）→ `delta`（多次）
→ `usage` → `done`。出错时替换为 `error`。

**中止流**：回调返回非 nil 即中止，同时取消对 Runtime 的请求，引擎侧也会
停止生成（不只是丢结果）。

```go
if userPressedStop {
	return errStopRequested   // 传给 onEvent
}
```

`ChatStream`/`StreamChat` 会强制 `Stream: true`，**不要自己设**。反过来，
用 `Chat` 时必须保持 `Stream == false`——否则服务端返回 SSE 而 `Chat`
期望普通 JSON，解析会失败。

### 选质量档位

```go
localai.QualityFast       // 小模型，最快
localai.QualityBalanced   // 默认
localai.QualityBest       // 可加载的最强模型，慢
```

---

## 八、结构化输出（约束解码）

**首选手段是 `JSONSchema`**，不是"生成完再校验"（ADR 0006）。
约束解码在采样阶段就排除非法 token，所以输出一定符合 schema。

```go
schema := json.RawMessage(`{
  "type": "object",
  "properties": {
    "intent": {"type": "string", "enum": ["export", "delete", "query"]},
    "order_id": {"type": "string"}
  },
  "required": ["intent"]
}`)

res, err := client.Chat(ctx, &localai.ChatRequest{
	Messages:   []localai.Message{{Role: "user", Content: "把订单 A123 导出"}},
	JSONSchema: schema,
	MaxTokens:  200,
})
// 此时 res.Content 一定是合法 JSON
var out struct {
	Intent  string `json:"intent"`
	OrderID string `json:"order_id"`
}
json.Unmarshal([]byte(res.Content), &out)
```

面向小模型写 schema 的注意事项：

1. **字段越少越好**。小模型在 5 个以上字段时容易串位
2. **用 `enum` 而不是自由文本**表达分类
3. **不要嵌套超过 2 层**
4. `required` 只列真正必需的——把可选项设为必填会让模型编造值

> `enable_thinking` **默认 false**。Qwen3 系列开启后会把 token 预算先花在
> 思考内容上，在常见的 `MaxTokens` 设置下输出为空，用户会以为模型坏了。
> 只有在你明确给了大预算（如 2000+）且需要推理链时才开。

---

## 九、知识库与 RAG

### 推荐做法：把检索交给 Runtime

```go
res, err := client.Chat(ctx, &localai.ChatRequest{
	Messages: []localai.Message{{Role: "user", Content: "保修期多久？"}},
	KBIDs:    []string{"support"},
})
// res.Knowledge 说明本次用了哪些资料
if res.Knowledge != nil {
	for _, s := range res.Knowledge.Sources {
		fmt.Printf("[%d] %s (%.2f)\n", s.Index, s.Source, s.Score)
	}
}
```

传了 `KBIDs` 后 Runtime 自动完成"检索 → 拼接 → 注入"，你不必先调
`/search` 再自己拼 prompt。这也是唯一能保证预算裁剪与防注入标记正确的做法。

前置条件：`cap.RAGEnabled == true`。为 false 时 `KBIDs` 会被**忽略**——
用户会以为"检索过了只是没找到"，而不是"知识库根本没启用"。

### 管理知识库

```go
kb, err := client.CreateKB(ctx, &localai.CreateKBRequest{
	Name: "售后支持库",
	// ID 可省略；指定时只能含字母数字短横下划线（会拼进目录路径）
})

// 只传路径：Runtime 是本机服务，不必把文件内容塞进 HTTP
imp, err := client.Import(ctx, kb.ID, &localai.ImportRequest{
	Path: `D:\docs\保修条例.pdf`,
})
if imp.Duplicate {
	log.Printf("内容已存在，未重复导入: %s", imp.ExistingID)
}

// 切片在导入时已入库，关键词检索立即可用；
// 这里补齐语义检索所需的向量
vec, err := client.Vectorize(ctx, kb.ID, nil)
if errors.Is(err, localai.ErrModelUnavailable) {
	log.Print("未配置 Embedding 模型，只能关键词检索")
}
```

支持的格式：`.txt` / `.md` / `.pdf` / `.docx` / `.xlsx`。
**不支持** `.doc` / `.xls` / `.pptx`。

导入的性质：

| 性质 | 说明 |
| --- | --- |
| **幂等** | 同库内相同内容（SHA-256）只保留一份，重复导入返回 `Duplicate` 而非错误 |
| **可重试** | 失败的文档允许重试；补上密码后重新导入会真正重新解析 |
| **增量更新** | 传 `UpdateDocumentID` 时做 diff，未变更切片保留向量 |

```go
// 增量更新：只重算变更的切片
imp, err := client.Import(ctx, kb.ID, &localai.ImportRequest{
	Path:             `D:\docs\保修条例-v2.pdf`,
	UpdateDocumentID: oldDocID,
})
if imp.Diff != nil {
	log.Printf("%s（新增 %d / 变更 %d / 删除 %d / 未变 %d）",
		imp.Diff.Summary, imp.Diff.Added, imp.Diff.Updated,
		imp.Diff.Deleted, imp.Diff.Unchanged)
}
// 只有 NeedsEmbedding 为 true 时才需要触发向量化（纯删除不需要）
```

### 直接检索

```go
sr, err := client.Search(ctx, &localai.SearchRequest{
	Query:        "联系方式",
	KBIDs:        []string{"support"},   // 必填，不支持全库检索
	TopK:         5,
	Mode:         "hybrid",              // hybrid / keyword / semantic
	BuildContext: true,
	ContextTokens: 1024,                 // 建议设为模型上下文的 40~50%
})

for _, hit := range sr.Hits {
	fmt.Printf("%.3f %s\n", hit.Score, hit.Text)
}
```

三个要点：

1. `Score` 是 RRF 融合分数，**跨查询不可比**——不同查询的候选集不同，
   绝对值没有意义，只用于同一次结果内排序
2. `keyword_rank` / `semantic_rank` 说明这条是怎么被召回的（0 = 该通道
   未命中）。排查"为什么没召回"时看这两个
3. `Mode` 可能被降级（如没有 Embedding 模型时 semantic → keyword），
   实际用的模式看 `SearchResult.Mode` 与 `Degraded`

`hybrid` 是默认且推荐的：实测查询"联系方式"时文档写的是"联系电话"，
关键词 0 命中而语义正确命中；反过来带专有名词的查询关键词更准。两路都要。

---

## 十、多轮对话

把**完整历史**传进来，Runtime 按预算裁剪：

```go
res, err := client.Chat(ctx, &localai.ChatRequest{
	Messages: []localai.Message{
		{Role: "user", Content: "保修期多久？"},
		{Role: "assistant", Content: "保修期为两年。"},
		{Role: "user", Content: "那它包含哪些部件？"},
	},
	KBIDs: []string{"support"},
})
```

默认预算是上下文的三段划分：system 与当前问题不可裁，历史 40%，
知识库片段 35%。裁剪时**从最旧的开始丢**，但始终保留 system 与最近若干轮。

`QueryStrategy` 控制检索查询怎么构造：

| 值 | 行为 | 何时用 |
| --- | --- | --- |
| `auto`（默认） | 两路评分取更高者 | 推荐。实测 21 个用例全对 |
| `last_user` | 只用最后一条 user 消息 | 你知道追问总是完整的 |
| `concat` | 拼接上一轮与当前 | **有风险**：追问本身完整时会被上一轮主题带偏（实测 5 例只中 1 例） |
| `llm` | 小模型改写 | **实验特性**，实测 0.6B 在 8 例中错 4 例，不建议生产使用 |

---

## 十一、优先级与配额

```go
res, err := client.Chat(ctx, &localai.ChatRequest{
	Messages: []localai.Message{{Role: "user", Content: "..."}},
	Priority: localai.PriorityInteractive,   // P0，有预留槽位
	ClientID: "crm-app",                     // 参与 per-client 配额
})
```

| 优先级 | 用途 |
| --- | --- |
| `PriorityInteractive` | 用户主动触发（点击后等结果） |
| `PriorityNormal` | 普通文本处理 |
| `PriorityBatch` | 向量化、批量任务 |
| `PriorityBackground` | 后台索引更新，最先被抢占 |

**P0 不能抢占正在执行的任务**（引擎不支持 token 级中断），只能跳过排队。
所以本地单引擎场景下吞吐恒定在 1.1~1.3 req/s，并发只增加排队——
别指望用并发提升吞吐。

`ClientID` 留空则不参与 per-client 限制。多模块共用 Runtime 时应当设置，
这样一个模块出问题不会挤掉其他模块。

---

## 十二、模型管理

```go
models, err := client.Models(ctx)        // 全部清单
active, err := client.ActiveModel(ctx)   // 当前加载（空串 = 未加载）

// 未加载模型不是错误——这是正常的初始状态
if active == "" {
	err = client.LoadModel(ctx, "qwen2.5-1.5b-instruct-q4")
}
```

`Models()` 返回的 `Model` 里几个易错点：

| 字段 | 陷阱 |
| --- | --- |
| `Present` | 文件**在磁盘上**，不代表已加载 |
| `Loaded` | **已加载进内存**（同一时刻只有一个） |
| `ContextLength` | 模型**原生**上限。实际运行时远小于它（按内存预算缩减） |
| `CommercialUse` | 必须核对。LFM 系列有营收门槛 |
| `Capabilities` | 来自清单的能力标记，`rag` 只认**本项目实测**的结论 |

> `LoadModel` 是同步的，大模型会挂几十秒。生产代码建议给长 ctx，
> 或用运行时的异步任务接口（`/api/v1/models/load` 传 `async: true`）。

---

## 十三、并发与资源

`Client` **并发安全**，可在多个 goroutine 共享。但要注意 Runtime 侧的限制：

- `MaxConcurrent` 默认 1（单引擎），并发请求排队而非并行
- 队列满返回 `ErrQueueFull`（429）
- 模型加载后常驻内存，空闲 90s 且用户离开 180s 后会卸载

对业务代码的含义：

```go
// 不要：以为并发能加速
for i := 0; i < 20; i++ {
	go client.Chat(ctx, req)   // 实测 20 并发时 p50 排队 9.3 秒
}

// 应该：控制并发 + 显示排队状态
sem := make(chan struct{}, 2)
```

流式接口的 `queued` 事件给了你展示排队状态的机会，别浪费。

---

## 十四、用 OpenAI 官方 SDK 接入

如果你已经在用 OpenAI 的客户端，可以不改代码：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:49213/v1",   # 端口从 discovery 文件读
    api_key="laih_xxxxxxxx",                 # token 从 discovery 文件读
)

stream = client.chat.completions.create(
    model="local",                           # 该字段不校验，任意值
    messages=[{"role": "user", "content": "把订单导出成 Excel"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:49213/v1",
  apiKey: process.env.LAIH_TOKEN,
});

const stream = await client.chat.completions.create({
  model: "local",
  messages: [{ role: "user", content: "把订单导出成 Excel" }],
  stream: true,
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

### OpenAI 层的已知差异

| 差异 | 说明 |
| --- | --- |
| **没有 `kb_ids`** | OpenAI 协议表达不了 RAG。需要知识库请用 Go SDK 或 `/api/v1/chat` |
| **没有优先级** | 同上，走 `/v1/*` 一律 `PriorityNormal` |
| **`usage` 是估算** | 本地引擎不报精确 token 数，`/v1/embeddings` 的 usage 是字符数估算，不要用于成本核算 |
| **流式 `prompt_tokens` 可能只是增量** | prompt cache 命中后引擎只报新增部分。服务端标了 `prompt_tokens_may_be_incremental: true`，**不要当完整长度做统计** |
| **错误格式** | 401/403/400 等返回 OpenAI 格式错误（不是 Envelope），官方 SDK 能正常解析 |
| **embeddings 语义** | `/v1/embeddings` 的输入一律按**文档**处理（内部加 `passage: ` 前缀）。需要 query 语义请用 `/api/v1/search` |

---

## 十五、API 速查

SDK 覆盖的端到端映射（契约唯一真相是 `local-ai-runtime-sdk/docs/openapi.yaml`）：

| SDK 方法 | HTTP | 说明 |
| --- | --- | --- |
| `Health(ctx)` | `GET /health/ready` | 能否服务 |
| `Live(ctx)` | `GET /health/live` | 进程存活 |
| `Capability(ctx)` | `GET /api/v1/capability` | 能做什么 |
| `Models(ctx)` | `GET /api/v1/models` | 模型清单 |
| `ActiveModel(ctx)` | `GET /api/v1/models` | 当前模型（读 `active`） |
| `LoadModel(ctx, id)` | `POST /api/v1/models/load` | 换模型（同步） |
| `UnloadModel(ctx)` | `POST /api/v1/models/unload` | 卸载 |
| `Chat(ctx, req)` | `POST /api/v1/chat` | 对话（非流式） |
| `StreamChat(ctx, req, fn)` | `POST /api/v1/chat` | 对话（SSE，全部事件） |
| `ChatStream(ctx, req, fn)` | `POST /api/v1/chat` | 对话（SSE，只要文本） |
| `Search(ctx, req)` | `POST /api/v1/search` | 检索（可带 RAG 上下文） |
| `CreateKB(ctx, req)` | `POST /api/v1/knowledge/bases` | 建库 |
| `ListKB(ctx)` | `GET /api/v1/knowledge/bases` | 列出 |
| `DeleteKB(ctx, id)` | `DELETE /api/v1/knowledge/bases/{id}` | 删库（不可恢复） |
| `KBStat(ctx, id)` | `GET .../{id}/stats` | 统计与向量化进度 |
| `Vectorize(ctx, id, req)` | `POST .../{id}/vectorize` | 生成向量 |
| `ListDocuments(ctx, id)` | `GET .../{id}/documents` | 文档列表 |
| `Import(ctx, id, req)` | `POST .../{id}/documents` | 导入/更新 |
| `DeleteDocument(...)` | `DELETE .../{id}/documents/{docId}` | 删文档 |

SDK **未封装**（需要时手写 HTTP）：`/api/v1/stats`、`/metrics`、
`/api/v1/scheduler`、`/api/v1/resources`、`/api/v1/rule/analyze`、
`/api/v1/tasks/*`、`/api/v1/admin/*`、`/v1/*`。

这些要么是运维视角（指标、诊断），要么是运维动作（配置、token），
不适合放进业务 SDK。业务代码要读运行状态请用 `Capability`，
它已经覆盖了"能不能做某件事"这个真正的决策点。

---

## 十六、常见问题

**Q：`NewClient` 失败说 runtime unavailable，但 Runtime 明明在跑。**

token 与地址读的是 discovery 文件。检查 `localai.DiscoveryPath()` 指向的
文件是否存在、内容是否为空。常见原因：Runtime 以另一个用户身份运行
（如 systemd 系统级服务），文件在别人的 home 里。

**Q：每次重启 Runtime 后我的程序就连不上了。**

token 每次启动重新生成。别缓存 client，或者缓存后在 `ErrUnauthorized`
时重建（见第三章）。

**Q：`Chat` 返回的内容是关键词匹配的结果，不像 AI 回答。**

检查 `res.Degraded()`。降级到规则引擎时 `res.Model.Actual` 会是空或
特殊标记。要禁止降级：`AllowDegrade: &false` + `MissionCritical: true`。
涉及金额、权限、删除类操作**应当禁止降级**。

**Q：中文输出乱码。**

服务端 HTTP 一律 `charset=utf-8`，问题通常在终端编码（Windows 控制台
需要 `chcp 65001`）。写文件时确认用 UTF-8。

**Q：为什么 `Usage.PromptTokens` 是 0？**

引擎在极端情况下（产出首个 token 前中断但流本身正常收尾）不上报 timings，
此时服务端省略该字段。**0 表示未知，不是"真的 0"**。

**Q：能同时连多个 Runtime 实例吗？**

不能。单实例锁（ADR 0004）保证每用户只有一个。多用户终端服务器场景
需要每个用户各自登录会话，那是"每用户实例"的自然结果。

---

## 十七、版本兼容

| 项 | 规则 |
| --- | --- |
| API 版本 | 路径前缀 `/api/v1`，破坏性变更升 `v2`，`v1` 保留至少一个大版本 |
| 新增字段 | 服务端可以随时加字段（SDK 忽略未知字段） |
| 新增事件类型 | SSE 客户端**必须忽略不认识的 `event`** |
| 错误码 | 已发布的 code 不可变更语义。新增 code 时旧 SDK 归为 `ErrRuntimeUnavailable` |
| SDK 与 Runtime | SDK 只依赖 `/api/v1` 的稳定子集，对 Runtime 版本无强校验 |

最后一条很重要：SDK 不校验 Runtime 版本。因此**不要依赖"新字段一定有值"**
——按零值安全的方式写代码。

---

## 十八、关于本文档与源码中的内部引用

你可能在源码注释和本文里看到"文档 18.4""ADR 0004"这类引用。它们指向
**Runtime 服务端仓库**的内部设计文档，该仓库不公开。

这不影响使用——这些标注只是说明"为什么这样设计"的出处，不是你需要
去读的前置材料。需要判断某个行为的契约依据时，看
[`local-ai-runtime-sdk/docs/openapi.yaml`](../docs/openapi.yaml)，那是接口的唯一真相。

## 十九、许可证

Apache License 2.0，见仓库根的 [LICENSE](../../LICENSE)。

注意：本许可证只覆盖本仓库的 SDK 代码。**Runtime 服务端本体是
独立的私有软件**，有各自的许可条款，不因本仓库开源而改变。
