# Local AI Hub Python SDK

Runtime 的官方 Python 客户端。**零外部依赖**——只用标准库，引入它不会
给你的项目带来任何传递依赖。

```bash
pip install luping-localai
```

```python
from localai import Client, ChatRequest, Message

client = Client()                       # 自动发现，不写死端口
res = client.chat(ChatRequest(messages=[Message("user", "你好")]))
print(res.content)
```

> **包名为什么是 `luping-localai` 而不是 `localai`**：`localai` 与
> `local-ai-runtime` 在 PyPI 上都已被他人占用（前者是一个 AutoGPT
> 项目，后者是另一个同名 runtime）。导入名仍是 `localai`：
>
> ```python
> import localai          # ✓
> ```

---

## 零、先跑演示看效果

装好、Runtime 也启动后，不必读文档——直接跑演示：

```bash
python examples/demo.py
```

### 先确认环境（30 秒）

跑不通时通常不是代码问题，而是解释器选错了。一条命令诊断：

```bash
python scripts/check_env.py       # 若 python 不可用，改用:
py scripts/check_env.py           # Windows 启动器
.venv\Scripts\python scripts/check_env.py   # venv 内的解释器
```

它会检查 Python 版本、`python` 命令是否指向真的解释器、包能否导入、
Runtime 是否在线，并对每个问题给出修复步骤。

> **Windows 上 `python` 可能是个空壳**
>
> 如果你敲 `python examples/demo.py` 后**什么都没打印、退出码 9009**，
> 那么 `python` 指向的是 Windows 应用商店的占位符（0 字节），
> 而不是真正的 Python。验证：
>
> ```powershell
> Get-Command python
> # 路径含 Microsoft\WindowsApps 就是它
> ```
>
> 三种处理方式（任选）：
>
> ```powershell
> py examples\demo.py                        # 1. 用 py 启动器
> .venv\Scripts\python examples\demo.py      # 2. 用 venv 内的解释器
> ```
>
> ```
> 3. 设置 → 应用 → 高级应用设置 → 应用执行别名，
>    关闭 python.exe 与 python3.exe，再从 PATH 里去掉 WindowsApps
> ```
>
> 这种情况**连 Python 代码都执行不到**，所以 demo 自己没法提示你——
> 只能靠 `check_env.py` 或本文档。

### 三种跑法

| 场景 | 命令 |
| --- | --- |
| 项目里已建 `.venv` | `.venv\Scripts\python examples\demo.py`（Windows）<br>`./.venv/bin/python examples/demo.py`（Linux/macOS） |
| 已激活 venv | `python examples/demo.py` |
| 只装了 PyPI 包 | `python demo.py`（在 demo.py 所在目录） |

它会列出 10 项功能，输入编号单独运行：

```
 1. 连接与发现（不写死端口）
 2. 这台机器能做什么（能力分级）
 3. 模型清单与加载状态
 4. 对话（非流式、多轮、可复现）
 5. 结构化输出（json_schema 约束解码）
 6. 流式对话（SSE、提前中断）
 7. 知识库全流程（建库→导入→检索→RAG→清理）
 8. 错误处理（12 种可判定类别）
 9. 三层超时（实测触发）
10. 并发调用（线程共享 Client）
```

也可以直接指定某一项，或全部跑一遍：

```bash
python examples/demo.py chat      # 只看对话
python examples/demo.py kb        # 只看知识库
python examples/demo.py all       # 全部

python examples/demo.py --list    # 只列编号，不连 Runtime
```

> 演示是**从 PyPI 装的那个包**跑起来的，不需要源码目录——
> 把 `examples/demo.py` 拷到任何地方都能用。
>
> 每项独立：某一项因本机能力不足（如没启用 RAG）会跳过并说明原因，
> 不影响其他项。

---

## 一、它不做什么

先明确边界，避免误用：

| 不负责 | 原因 |
| --- | --- |
| **启动 Runtime** | 任何自动拉起必须对用户可见，否则会被误判为恶意程序。启动交给安装器或系统自启项 |
| **下载模型** | 那是运维动作。用 `localai-cli models download` 或管理界面 |
| **管理配置** | `/api/v1/admin/*` 需要 `--enable-admin`，且当前与推理共用 token，不适合暴露给业务代码 |
| **重试策略定制** | 内置了够用的策略（见第五章），要更复杂请自行控制 |

---

## 二、三分钟上手

```python
from localai import Client, ChatRequest, Message, ErrRuntimeUnavailable, APIError

client = Client()      # 零配置：自动从 discovery 文件找到地址与 token

# 先问"这台机器能做什么"——见第三章
cap = client.capability()
if not cap.chat_enabled:
    raise SystemExit(f"本机无法进行 AI 对话：{cap.reason}")

try:
    res = client.chat(ChatRequest(
        messages=[Message("user", "把订单导出成 Excel")],
        max_tokens=200,
    ))
except APIError as e:
    if e.kind is ErrRuntimeUnavailable:
        raise SystemExit("本地 AI 未运行，请先启动 Local AI Hub")
    raise

print(res.content)

# 降级不是错误，但要告诉用户
if res.degraded:
    print(f"注意：已降级（{res.degrade_reason}），结果质量可能下降")
```

流式（最常用的形态）：

```python
for delta in client.chat_stream(ChatRequest(
        messages=[Message("user", "讲个技术故事")])):
    print(delta, end="", flush=True)
```

---

## 三、连接：不要写死端口

端口是**动态分配**的（18765 被占用时会换）。SDK 按以下顺序查找：

| 顺序 | 来源 | 用途 |
| --- | --- | --- |
| 1 | `Config(base_url=..., token=...)` | 单元测试、连远程实例 |
| 2 | 环境变量 `LAIH_URL` / `LAIH_TOKEN` | 容器、CI |
| 3 | **discovery 文件** | 正常情况走这里 |
| 4 | `http://127.0.0.1:18765` | 兜底（无 token，通常调不通） |

discovery 文件位置（与 Runtime 侧一致）：

| 系统 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\LocalAIHub\runtime.json` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/local-ai-hub/runtime.json` |
| macOS | `~/.local-ai-hub/runtime.json` |

**token 会变**：管理后台"重置 token"或 Runtime 重启后重新生成。SDK 在
`Client()` 构造时读一次，所以：

```python
# 收到 401 就重建 client，而不是无限重试同一个
try:
    client.models()
except APIError as e:
    if e.kind is ErrUnauthorized:
        client = Client()      # 重新读 discovery 文件
```

### 代理环境变量

连 `127.0.0.1` 时 SDK **显式禁用代理**。企业内网普遍设置
`HTTP_PROXY`，而让本地回环请求走代理必然失败，报错还是
"407 Proxy Authentication Required" 这种与本地 AI 毫无关系的信息。

若 `LAIH_URL` 指向远程实例，代理设置照常生效——判据是 host 是否回环。

---

## 四、能力分级：为什么必须查 `capability()`

本地 Runtime 可能运行在内存很小的机器上。此时服务**活着**但能力受限：

| 字段 | False 时的含义 |
| --- | --- |
| `rag_enabled` | 传 `kb_ids` 会被**静默忽略**。用户会以为"检索过了只是没找到"，而不是"知识库根本没启用" |
| `chat_enabled` | 只能走规则引擎（关键词匹配）。这不是"AI 降级"而是"没有 AI" |

只看 `health()` 是不够的：它回答"服务是否活着"，`capability()` 回答
"活着但能做什么"。

建议启动时查一次并缓存——能力在 Runtime 启动时确定，不随请求变化。

---

## 五、错误：区分"稍后重试"与"不可能成功"

```python
from localai import APIError, ErrResourceLimited, ErrCapabilityUnmet, is_retryable

try:
    res = client.chat(req)
except APIError as e:
    if not is_retryable(e):
        raise
    if e.kind is ErrResourceLimited:
        show_hint("内存不足，请关闭其他程序后重试")
    elif e.kind is ErrCapabilityUnmet:
        show_hint("当前模型不满足要求，请换模型或改配置")   # 重试无用
```

`kind` 是枚举成员，用 `is` 比较——比字符串匹配快，且打字错误会立刻
报 `AttributeError` 而不是静默地永不相等。

| `kind` | 可重试 | 业务方该做什么 |
| --- | --- | --- |
| `ErrRuntimeUnavailable` | ✓ | 提示用户启动 Local AI Hub |
| `ErrUnauthorized` | ✗ | 重建 client（token 变了） |
| `ErrResourceLimited` | ✓ | 关掉其他程序后重试 |
| `ErrQueueFull` | ✓ | 按 `e.retry_after` 退避 |
| `ErrClientQuota` | ✗ | 降低自己的请求频率（其他客户端正常） |
| `ErrInputTooLong` | ✗ | 缩短输入或换更大上下文的模型 |
| `ErrModelUnavailable` | ✓ | 模型可能正在加载 |
| `ErrCapabilityUnmet` | **✗** | **关程序没用，得换模型或改配置** |
| `ErrNotFound` | ✗ | 检查 ID / 先下载模型 |
| `ErrInvalidRequest` | ✗ | 修参数 |
| `ErrTaskCancelled` | ✗ | 被取消（含超时与主动放弃） |
| `ErrTimeout` | ✓ | 调大超时重试 |

### 内置重试

SDK **自动重试**压力类错误（`resource_limited` / `queue_full` /
`model_unavailable`），默认最多 2 次，带 ±30% 抖动。

抖动是必需的：多个客户端同时收到 `resource_limited` 后若在同一时刻
重试，会再次把服务端打满（惊群）。

`invalid_request` / `input_too_long` / `capability_unmet` **绝不重试**
——重试必然再次失败，只会浪费用户时间。

关闭重试：

```python
client = Client(Config(max_retries=0))
```

### 降级不是错误

降级结果照常返回（HTTP 200），通过字段标记：

```python
res = client.chat(req)
if res.degraded:
    print(f"已降级：{res.degrade_reason}")
```

把它当错误抛出会迫使调用方在 `except` 里做业务判断——那是本末倒置。

---

## 六、三层超时

本地推理的首字节延迟可能很长（冷启动要加载模型，可能几十秒），
单一超时无法兼顾"快速发现 Runtime 死了"与"允许模型慢慢加载"。

| 超时 | 默认 | 作用 |
| --- | --- | --- |
| `connect` | 1s | 判断 Runtime 是否存活 |
| `ttft` | 15s | 首字节：允许模型加载与排队 |
| `total` | 60s | 总时长兜底 |

```python
from localai import Client, Config, Timeouts

client = Client(Config(timeouts=Timeouts(connect=2, ttft=30, total=180)))
```

> **导入文档不套用 `total` 超时**。解析 + 切片大文件可能数十秒，
> 套用 60 秒会把正常的大文件导入误判为超时。

---

## 七、流式

```python
# 只要文本增量
for delta in client.chat_stream(req):
    print(delta, end="")

# 要完整事件（排队位置、usage、知识库信息）
for ev in client.stream_events(req):
    if ev.type == "queued":
        print(f"前面还有 {ev.ahead} 个任务")
    elif ev.type == "delta":
        print(ev.text, end="")
    elif ev.type == "done":
        print(f"\n结束原因：{ev.finish_reason}")
```

事件顺序：`queued`（可能多次）→ `meta` → `warning`（可选）→
`delta`（多次）→ `usage` → `done`。出错时可能替换为 `error`。

**提前 `break` 会停止生成**：返回的迭代器在关闭时断开 HTTP 连接，
服务端感知断开后停止——否则引擎会继续生成到结束，白烧 CPU。

```python
it = client.chat_stream(req)
try:
    for delta in it:
        if user_cancelled():
            break          # 服务端会停止生成
finally:
    it.close()             # 也可用 with 语句
```

单帧解析失败**不中断整条流**——继续读后续内容，用户至少能看到部分
输出。

---

## 七之二、结构化输出（约束解码）

传 `json_schema` 启用约束解码：模型在生成时只能产出符合 schema 的
token，而不是"生成完再校验、不合格就重试"。后者在本地小模型上几乎
必然反复失败。

```python
schema = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "maxLength": 40},
        "confidence": {"type": "number"},
        "keywords": {
            "type": "array",
            "items": {"type": "string", "maxLength": 20},
            "maxItems": 4,
        },
    },
    "required": ["intent", "confidence", "keywords"],
}

res = client.chat(ChatRequest(
    messages=[Message("user", "把这个月的销售订单导出成 Excel")],
    json_schema=schema,
    max_tokens=300,
    temperature=0.0,
))

data = json.loads(res.content)     # 不需要 try/except 兜底
```

> ### ⚠ 数组和字符串必须写上限
>
> 约束解码能阻止模型输出 schema 之外的内容，但**挡不住它无限重复
> 合法内容**。
>
> 实测（qwen2.5-1.5b Q4，本机）：schema 里 `keywords` 数组不设
> `maxItems` 时，模型把 `"automation tool"` 重复几十遍，一直撞到
> `max_tokens` 上限被截断——21.8 秒、1562 字符、JSON 在字符串中间
> 断掉、解析失败。复测三次完全一致，说明是稳定复现而非偶发。
>
> 加上 `maxItems: 4` 后：**2.8 秒返回、`finish_reason=stop`、JSON 合法**。
>
> 把 `max_tokens` 当作防重复手段是无效的——它只会把输出截断在更难
> 诊断的位置。**正确的做法是给 schema 加约束**：
>
> | 约束 | 作用 |
> | --- | --- |
> | `maxItems` | 数组长度上限（最关键） |
> | `maxLength` | 字符串长度上限 |
> | `enum` | 用枚举代替自由字符串 |
>
> 若解析失败且 `res.truncated` 为真，先怀疑这个；若没截断却失败，
> 才怀疑引擎未启用约束解码，或 schema 里有不支持的 `oneOf` / `anyOf`。

---

## 八、知识库（RAG）

```python
from localai import ImportRequest, SearchRequest, ChatRequest, Message

kb = client.create_kb("售后库", description="客服话术与常见问题")

# 导入（传路径，Runtime 直接读盘——比传文件内容省内存）
r = client.import_document(kb.id, ImportRequest(path=r"C:\docs\manual.md"))
print(f"{r.chunk_count} 个切片")

# 内容未变时重复导入不会产生新文档
r2 = client.import_document(kb.id, ImportRequest(path=r"C:\docs\manual.md"))
assert r2.duplicate and r2.existing_id == r.document_id

# 向量化（补齐语义检索；不向量化也能做关键词检索）
client.vectorize(kb.id)             # 大库用 is_async=True

# 检索
sr = client.search(SearchRequest(query="怎么退货", kb_ids=[kb.id], top_k=5))
for h in sr.hits:
    print(f"{h.score:.3f} {h.text[:50]}")

# 对话时自动检索并注入
res = client.chat(ChatRequest(
    messages=[Message("user", "退货要几天？")],
    kb_ids=[kb.id],
))
print(res.content)
print(f"用了 {res.knowledge.used_chunks} 个切片")
```

支持 `.txt` / `.md` / `.pdf` / `.docx` / `.xlsx`。

### 上传文件内容（GUI / Web 场景）

用户选中的文件在应用进程里，没有"Runtime 能直接读的路径"这个概念
（尤其应用跑在容器、远程桌面或沙箱里时）：

```python
r = client.upload_document(kb.id, "manual.pdf", open("manual.pdf", "rb").read())
```

`filename` **必须带扩展名**——服务端按扩展名选解析器，传 `"data"`
会被拒绝。

### `import_document` 与 `upload_document` 的选择

| | `import_document` | `upload_document` |
| --- | --- | --- |
| 传什么 | 路径 | 文件内容 |
| 适用 | CLI、本机脚本 | GUI、Web、容器 |
| 存储模式 | 可选 MANAGED / EXTERNAL | 恒为 MANAGED |

### RAG 上下文里一个容易忽略的字段

`RAGContext.fixed_overhead_tokens` 是防注入声明 + 标题 + 指令占用的
token，**预算再小也省不掉**。若 `context_tokens` 设得比它小，一条资料
都放不进去（表现为 `used_chunks` 恒为 0）。所以别把预算设得太紧。

---

## 九、零依赖

`pyproject.toml` 的 `dependencies = []` 是刻意的设计约束，与 Go SDK
一致——SDK 会被大量第三方项目引入，任何依赖都会传染给它们，并可能
撞版本。

只用标准库：`urllib`（HTTP）、`json`、`socket`、`threading`。
没有 `requests`，没有 `httpx`，没有 `pydantic`。

测试依赖（`pytest`、`mypy`、`ruff`）在 `[dev]` 可选组里，运行时不装。

---

## 十、开发

```bash
cd local-ai-runtime-sdk/python

python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate         # Linux / macOS

pip install -e ".[dev]"

pytest                              # 单元测试（不需要 Runtime）
ruff check localai tests
mypy localai
```

真实实例的端到端测试默认跳过：

```powershell
$env:LAIH_E2E="1"; pytest tests/test_live.py -v
```

```bash
LAIH_E2E=1 pytest tests/test_live.py -v
```

### 测试分层

| 文件 | 覆盖 | 需要 Runtime |
| --- | --- | --- |
| `test_client.py` | 发现、超时、错误分类、重试 | 否（假服务端） |
| `test_stream.py` | SSE 分帧、事件解码、提前中断 | 否 |
| `test_types.py` | 请求序列化、响应解析、知识库、multipart | 否 |
| `test_live.py` | 端到端（含真实模型对话、RAG） | **是** |

`tests/fakeruntime.py` 是可编程的假 Runtime，能构造真实实例很难触发的
边界：帧残缺、中途断连、响应非 JSON、特定错误码。

### 与服务端契约的关系

类型定义对齐 `local-ai-runtime-sdk/docs/openapi.yaml`（唯一真相），
但**不是**从它生成的——生成器产出不了类型化错误、三层超时与 SSE
解析，而那三项恰好是 SDK 的全部价值。

字段对照由主仓库的 `go run ./tools/fieldcheck` 检查。

---

## 十一、与 Go SDK 的关系

两个 SDK 的**语义必须一致**：同样的错误分类、同样的重试策略、同样的
默认超时。否则同一个业务在两个语言里行为不同，排查时无从下手。

差异只有语言惯用法：

| 概念 | Go | Python |
| --- | --- | --- |
| 方法名 | `Chat` / `CreateKB` | `chat` / `create_kb` |
| 错误判断 | `errors.Is(err, ErrQueueFull)` | `e.kind is ErrQueueFull` |
| 取消 | `context.Context` | `close()` / `with` |
| `async` 参数 | `Async` 字段 | `is_async`（`async` 是关键字） |

---

## 十二、已知限制

| 项 | 说明 |
| --- | --- |
| **同步 API** | 全部方法是阻塞的。异步需自己用 `asyncio.to_thread()` 包装，或等后续版本 |
| **单帧大小** | 未设上限（Go SDK 是 1 MiB）。正常帧只有几十字节 |
| **重试不含上传** | `upload_document` 不自动重试——重复上传会重复解析（虽然服务端会去重） |
| **文档注释引用内部文档** | 源码里有大量"文档 24.2""ADR 0006"引用，指向私有仓库。它们只是设计出处，**不是前置阅读** |
