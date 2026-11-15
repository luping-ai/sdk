"""请求与响应的类型定义。

## 字段来源

严格对照 `local-ai-runtime-sdk/docs/openapi.yaml`。**不要凭印象加
字段**——服务端没有的字段在 JSON 里恒为缺失，会造成"看起来有字段但
永远是 None"的错觉。

Go SDK 开发时踩过这个坑：曾假设 `Model` 有 `installed` 字段，实际
服务端只有 `loaded`，于是所有模型显示"未安装"而非"无此字段"。

`tools/fieldcheck` 负责做契约 ↔ 服务端的字段对照，本文件的字段
应与契约一致。

## 为什么用 dataclass 而不是 dict

- IDE 补全与类型检查能用上
- 拼错字段名时立刻报错，而不是在运行时静默得到一个 None
- 序列化逻辑集中在一处（`_to_wire`），空值规则只写一遍

## 空值规则

请求侧：值为 None 的字段**不进 JSON**（服务端用"字段缺失"表示
"用默认值"，显式传 null 语义不同）。

响应侧：JSON 里没有的字段给 None（或声明的默认值），不编造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "Capability",
    "ChatRequest",
    "ChatResult",
    "CheckResult",
    "DocumentSummary",
    "HealthReport",
    "ImportRequest",
    "ImportResult",
    "KBStat",
    "KBSummary",
    "KnowledgeBaseRef",
    "KnowledgeInfo",
    "KnowledgeSource",
    "Message",
    "Model",
    "ModelInfo",
    "Priority",
    "Quality",
    "RAGContext",
    "SearchHit",
    "SearchRequest",
    "SearchResult",
    "Timeouts",
    "Usage",
    "VectorizeResult",
]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class Timeouts:
    """三层超时（文档 24.2）。

    为什么需要三层：本地推理的首字节延迟可能很长（冷启动要加载模型，
    可能几十秒），单一超时无法兼顾"快速发现 Runtime 死了"与
    "允许模型慢慢加载"。

    值为 None 时用默认值（1 / 15 / 60 秒）。显式传 0 或负数也视为
    "用默认值"——0 秒超时没有意义，只可能是"没设置"。
    """

    connect: Optional[float] = None
    ttft: Optional[float] = None
    total: Optional[float] = None

    # 默认值放在类属性上而不是 __post_init__ 里改字段：调用方可能
    # 想区分"我设了 15"与"没设"，保留 None 更诚实。
    DEFAULT_CONNECT = 1.0
    DEFAULT_TTFT = 15.0
    DEFAULT_TOTAL = 60.0

    def resolved(self) -> Timeouts:
        """返回填好默认值的副本。"""
        return Timeouts(
            connect=_positive(self.connect, self.DEFAULT_CONNECT),
            ttft=_positive(self.ttft, self.DEFAULT_TTFT),
            total=_positive(self.total, self.DEFAULT_TOTAL),
        )


def _positive(v: Optional[float], default: float) -> float:
    if v is None or v <= 0:
        return default
    return float(v)


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """一条对话消息。"""

    role: str
    content: str

    def to_wire(self) -> dict:
        return {"role": self.role, "content": self.content}


class Quality:
    """质量档位，影响模型选择（文档 19.5）。

    用类属性而非 Enum：调用方常直接传字符串 `quality="fast"`，
    Enum 会迫使它们 import，收益不抵摩擦。
    """

    FAST = "fast"
    BALANCED = "balanced"
    BEST = "best"


class Priority:
    """任务优先级（文档 3.5）。

    交互任务有预留槽位；batch / background 在资源紧张时先被抢占。
    """

    INTERACTIVE = "interactive"
    NORMAL = "normal"
    BATCH = "batch"
    BACKGROUND = "background"


@dataclass
class ChatRequest:
    """对话请求。

    字段与契约的 `ChatRequest` 一一对应。
    """

    messages: list[Message]

    max_tokens: Optional[int] = None
    """输出上限。None 表示用服务端默认值。"""

    temperature: Optional[float] = None
    """0 时用贪心解码（结果可复现）。"""

    quality: Optional[str] = None
    """质量档位，见 `Quality`。"""

    deadline_ms: Optional[int] = None
    """本次请求的时间预算。"""

    allow_degrade: Optional[bool] = None
    """None 表示按服务端默认（允许降级）。

    涉及金额、权限、删除类操作应显式设为 False 并配合
    `mission_critical=True`（文档 27）。
    """

    mission_critical: Optional[bool] = None
    """该任务不允许降级。"""

    json_schema: Optional[dict] = None
    """启用约束解码（结构化输出）。

    是"约束"而非"事后校验"：模型在生成时就只能产出符合 schema 的
    token，因此不会出现"生成完再打回重试"的浪费（ADR 0006）。
    """

    enable_thinking: Optional[bool] = None
    """Qwen3 系列的思考模式。"""

    priority: Optional[str] = None
    """任务优先级，见 `Priority`。"""

    client_id: Optional[str] = None
    """调用方标识，用于 per-client 配额。"""

    # ---- 知识库（RAG）----

    kb_ids: Optional[list[str]] = None
    """检索的知识库。None 或空表示不用知识库。"""

    knowledge_query: Optional[str] = None
    """覆盖默认检索查询（默认用最后一条 user 消息）。"""

    knowledge_mode: Optional[str] = None
    """检索模式：hybrid（默认）/ keyword / semantic。"""

    context_tokens: Optional[int] = None
    """知识库上下文的 token 预算。None 表示自动。"""

    max_knowledge_chunks: Optional[int] = None
    """注入的片段数上限。"""

    knowledge_top_k: Optional[int] = None
    """检索候选条数（默认 8）。

    比注入条数大：先多召回，再由拼接按预算筛选。
    """

    # ---- 多轮 ----

    history_tokens: Optional[int] = None
    """历史消息的 token 预算。None 表示自动。"""

    query_strategy: Optional[str] = None
    """检索查询构造策略。默认 auto（自动判断是否结合上一轮提问）。"""

    def to_wire(self) -> dict:
        """序列化为服务端期望的 JSON。

        值为 None 的字段一律不出现。**不要改成发 null**：服务端用
        "缺失"表示默认值，而显式的 null 在部分字段上语义不同
        （例如 `allow_degrade` 的 None 是"按服务端默认"，不是"false"）。
        """
        out: dict[str, Any] = {
            "messages": [m.to_wire() for m in self.messages],
        }

        # 显式列字段而不是遍历 __dict__：这样新增字段时必须在两处都
        # 写上，漏掉会立刻被下面的注释提醒，而不是静默不发。
        scalars = (
            "max_tokens", "temperature", "quality", "deadline_ms",
            "allow_degrade", "mission_critical", "enable_thinking",
            "priority", "client_id", "knowledge_query", "knowledge_mode",
            "context_tokens", "max_knowledge_chunks", "knowledge_top_k",
            "history_tokens", "query_strategy",
        )
        for name in scalars:
            v = getattr(self, name)
            if v is not None:
                out[name] = v

        if self.json_schema is not None:
            out["json_schema"] = self.json_schema

        if self.kb_ids:
            out["kb_ids"] = list(self.kb_ids)

        return out


@dataclass
class ModelInfo:
    """本次请求实际用了哪个模型、是否降级（文档 19.1）。

    `requested` 与 `actual` 分开是必要的：降级时两者不同，业务方
    据此决定是否提示用户"结果质量可能下降"。
    """

    requested: str = ""
    actual: str = ""
    degraded: bool = False
    degrade_reason: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional[ModelInfo]:
        if not d:
            return None
        return cls(
            requested=str(d.get("requested", "")),
            actual=str(d.get("actual", "")),
            degraded=bool(d.get("degraded", False)),
            degrade_reason=str(d.get("degrade_reason", "") or ""),
        )


@dataclass
class Usage:
    """资源消耗（文档 19.1）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_ms: int = 0
    total_ms: int = 0
    queue_wait_ms: int = 0

    prompt_tokens_may_be_incremental: bool = False
    """`prompt_tokens` **可能**只是本轮新增部分（prompt cache 命中）。

    流式下恒为 True：引擎不报缓存命中数，服务端无法区分"首次请求的
    完整值"（实测 856）与"后续请求的增量值"（实测 4）。

    为什么是"可能"而不是"是"：断言它一定是增量，对首次请求就是错的。
    但缺失或 False 也不能假设它是完整长度。
    """

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional[Usage]:
        if not d:
            return None
        return cls(
            prompt_tokens=int(d.get("prompt_tokens", 0) or 0),
            completion_tokens=int(d.get("completion_tokens", 0) or 0),
            ttft_ms=int(d.get("ttft_ms", 0) or 0),
            total_ms=int(d.get("total_ms", 0) or 0),
            queue_wait_ms=int(d.get("queue_wait_ms", 0) or 0),
            prompt_tokens_may_be_incremental=bool(
                d.get("prompt_tokens_may_be_incremental", False)
            ),
        )


@dataclass
class KnowledgeSource:
    """一个引用来源。"""

    index: int = 0
    chunk_id: str = ""
    document_id: str = ""
    source: str = ""
    score: float = 0.0

    page_start: int = 0
    """起始页（1 基）。**0 表示未知**——客户端应显示"页码未知"，
    不能显示"第 0 页"。"""

    page_end: int = 0
    """结束页。0 表示未知。"""

    heading_path: str = ""
    """章节路径，用于展示"这段来自哪一节"。"""

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeSource:
        return cls(
            index=int(d.get("index", 0) or 0),
            chunk_id=str(d.get("chunk_id", "")),
            document_id=str(d.get("document_id", "")),
            source=str(d.get("source", "") or ""),
            score=float(d.get("score", 0.0) or 0.0),
            page_start=int(d.get("page_start", 0) or 0),
            page_end=int(d.get("page_end", 0) or 0),
            heading_path=str(d.get("heading_path", "") or ""),
        )


@dataclass
class KnowledgeInfo:
    """本次调用如何使用知识库。"""

    kb_ids: list[str] = field(default_factory=list)
    """实际检索的知识库。请求传 ``[ALL_BASES]`` 时这里是**展开后的
    具体 ID**，不会残留 ``"all"``。"""

    all_bases: bool = False
    """本次用了 ``[ALL_BASES]`` 展开，UI 据此说明"已检索全部 N 个库"。"""

    base_count: int = 0
    """实际参与检索的库数。"""

    used_chunks: int = 0
    context_tokens: int = 0
    retrieval_ms: int = 0
    degraded: bool = False
    sources: list[KnowledgeSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional[KnowledgeInfo]:
        if not d:
            return None
        return cls(
            kb_ids=[str(x) for x in (d.get("kb_ids") or [])],
            all_bases=bool(d.get("all_bases", False)),
            base_count=int(d.get("base_count", 0) or 0),
            used_chunks=int(d.get("used_chunks", 0) or 0),
            context_tokens=int(d.get("context_tokens", 0) or 0),
            retrieval_ms=int(d.get("retrieval_ms", 0) or 0),
            degraded=bool(d.get("degraded", False)),
            sources=[KnowledgeSource.from_dict(s)
                     for s in (d.get("sources") or [])],
            warnings=[str(w) for w in (d.get("warnings") or [])],
        )


@dataclass
class ChatResult:
    """对话结果。

    关键设计：**降级不是错误**（文档 24.5）。降级结果照常返回，通过
    `degraded` / `degrade_reason` 标记，业务方据此决定是否提示用户。
    把它当错误抛出会迫使调用方在 except 里做业务判断。
    """

    content: str = ""
    finish_reason: str = ""
    truncated: bool = False
    model: Optional[ModelInfo] = None
    usage: Optional[Usage] = None
    knowledge: Optional[KnowledgeInfo] = None
    warnings: list[str] = field(default_factory=list)
    request_id: str = ""

    @property
    def degraded(self) -> bool:
        """本次结果是否降级。"""
        return bool(self.model and self.model.degraded)

    @property
    def degrade_reason(self) -> str:
        """降级原因（未降级时为空串）。"""
        return self.model.degrade_reason if self.model else ""


# ---------------------------------------------------------------------------
# 健康与能力
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """单项健康检查结果。"""

    ok: bool = False
    message: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> CheckResult:
        return cls(ok=bool(d.get("ok", False)),
                   message=str(d.get("message", "") or ""))


class HealthState:
    """Runtime 的可服务状态。"""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class HealthReport:
    """一次健康检查的结果。"""

    state: str = ""
    since: int = 0
    uptime_seconds: int = 0
    reason: str = ""
    checks: dict[str, CheckResult] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """是否可以发起推理请求。

        ready 与 degraded 都算"可用"——degraded 意味着能力受限
        （如只能走规则引擎），但服务仍在。业务方可以进一步检查
        `Client.capability()` 判断是否降级到了非 AI 路径。
        """
        return self.state in (HealthState.READY, HealthState.DEGRADED)

    @classmethod
    def from_dict(cls, d: dict) -> HealthReport:
        return cls(
            state=str(d.get("state", "")),
            since=int(d.get("since", 0) or 0),
            uptime_seconds=int(d.get("uptime_seconds", 0) or 0),
            reason=str(d.get("reason", "") or ""),
            checks={k: CheckResult.from_dict(v)
                    for k, v in (d.get("checks") or {}).items()},
        )


class CapabilityLevel:
    """这台机器能提供的能力等级。"""

    FULL = "full"
    CHAT_ONLY = "chat_only"
    RULE_ONLY = "rule_only"


@dataclass
class Capability:
    """这台机器能做什么。

    判据是模型清单里本项目实测的 `rag.qualified`，不是厂商声明——
    实测发现厂商声明含 "rag" 的模型准确率只有 50%~75%，而厂商没标的
    Qwen3-0.6B 实测 16/16。
    """

    level: str = CapabilityLevel.RULE_ONLY
    reason: str = ""
    rag_enabled: bool = False
    """知识库检索是否可用。False 时传 kb_ids 会被忽略。"""

    chat_enabled: bool = False
    """模型对话是否可用。"""

    available_bytes: int = 0
    baseline_need_bytes: int = 0
    """基准模型的内存需求，用于向用户解释"差多少"。"""

    rag_model: str = ""
    warnings: list[str] = field(default_factory=list)
    """需要展示给用户的提示，不要静默丢弃。"""

    @classmethod
    def from_dict(cls, d: dict) -> Capability:
        return cls(
            level=str(d.get("level", CapabilityLevel.RULE_ONLY)),
            reason=str(d.get("reason", "") or ""),
            rag_enabled=bool(d.get("rag_enabled", False)),
            chat_enabled=bool(d.get("chat_enabled", False)),
            available_bytes=int(d.get("available_bytes", 0) or 0),
            baseline_need_bytes=int(d.get("baseline_need_bytes", 0) or 0),
            rag_model=str(d.get("rag_model", "") or ""),
            warnings=[str(w) for w in (d.get("warnings") or [])],
        )


@dataclass
class Model:
    """模型清单的对外视图。"""

    id: str = ""
    name: str = ""
    type: str = ""
    arch: str = ""
    quantization: str = ""
    context_length: int = 0
    """模型**原生**上下文上限。

    实际运行时用的上下文通常远小于它（按内存预算缩减）。
    """

    languages: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    license: str = ""
    """许可证标识。与 `commercial_use` 必须核对：部分模型有商用营收门槛。"""

    commercial_use: str = ""
    loaded: bool = False
    """当前是否已加载（同一时刻只有一个）。"""

    size_bytes: int = 0
    present: bool = False
    """模型文件已在本地（可加载）。

    与 `loaded` 的区别：`present` 是"文件在磁盘上"，`loaded` 是
    "已加载进内存"。下载完但未加载时 `present=True, loaded=False`。
    """

    @classmethod
    def from_dict(cls, d: dict) -> Model:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            type=str(d.get("type", "")),
            arch=str(d.get("arch", "")),
            quantization=str(d.get("quantization", "")),
            context_length=int(d.get("context_length", 0) or 0),
            languages=[str(x) for x in (d.get("languages") or [])],
            capabilities=[str(x) for x in (d.get("capabilities") or [])],
            license=str(d.get("license", "")),
            commercial_use=str(d.get("commercial_use", "")),
            loaded=bool(d.get("loaded", False)),
            size_bytes=int(d.get("size_bytes", 0) or 0),
            present=bool(d.get("present", False)),
        )


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

ALL_BASES = "all"
"""``kb_ids`` 里的全库标识：表示"本实例的全部知识库"。

用法::

    client.search(SearchRequest(query="保修期多久", kb_ids=[ALL_BASES]))
    # 响应 all_bases=True、base_count=N

与"省略 kb_ids"有本质区别：

- 省略 / 空列表 → 服务端返回 400，**不会**退化为全库
- ``[ALL_BASES]`` → 服务端展开为全部库，并在响应里留痕

不要与具体 ID 混用（``[ALL_BASES, "kb_1"]`` 会返回 400）：混用有歧义，
服务端不做"或"的猜测。

对话路径（``ChatRequest.kb_ids``）里空列表是**合法**的（普通对话），
与 ``/search`` 不同。
"""


@dataclass
class SearchRequest:
    """知识库检索请求。"""

    query: str
    kb_ids: list[str]
    """必填，不支持隐式全库检索（文档 22.9 的隔离要求）。

    三种写法：

    - 具体 ID 列表：只查这些，最多 30 个
    - ``[ALL_BASES]``：本实例全部知识库（显式声明），响应带
      ``all_bases`` / ``base_count``
    - 空列表：服务端返回 400，**不会**退化为全库

    不要与具体 ID 混用（``[ALL_BASES, "kb_1"]`` 会返回 400）。
    """

    top_k: Optional[int] = None
    mode: Optional[str] = None
    """hybrid（默认）/ keyword / semantic。"""

    build_context: Optional[bool] = None
    """为 True 时额外返回拼好的 RAG 上下文。"""

    context_tokens: Optional[int] = None
    max_chunks: Optional[int] = None

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "query": self.query,
            "kb_ids": list(self.kb_ids),
        }
        for name in ("top_k", "mode", "build_context",
                     "context_tokens", "max_chunks"):
            v = getattr(self, name)
            if v is not None:
                out[name] = v
        return out


@dataclass
class SearchHit:
    """一条检索结果。"""

    chunk_id: str = ""
    document_id: str = ""
    kb_id: str = ""
    ordinal: int = 0
    text: str = ""
    score: float = 0.0
    keyword_rank: int = 0
    """关键词通道的排名（0 表示未被该通道召回）。"""

    semantic_rank: int = 0
    """语义通道的排名（0 表示未被该通道召回）。"""

    page_start: int = 0
    """起始页（1 基）。**0 表示未知**（旧数据或客户端未提供）。

    不要显示成"第 0 页"。
    """

    page_end: int = 0
    """结束页。0 表示未知。"""

    heading_path: str = ""
    """章节路径，如"第三章 保修 > 3.2 期限"。

    不同解析器（PDF 靠字号、DOCX 靠样式、PPT 靠页标题）产物形态
    不同，**不要按固定格式解析**，只做展示与传参。
    """

    chunk_type: str = ""
    """块类型：paragraph / heading / table / list / code。"""

    @classmethod
    def from_dict(cls, d: dict) -> SearchHit:
        return cls(
            chunk_id=str(d.get("chunk_id", "")),
            document_id=str(d.get("document_id", "")),
            kb_id=str(d.get("kb_id", "")),
            ordinal=int(d.get("ordinal", 0) or 0),
            text=str(d.get("text", "")),
            score=float(d.get("score", 0.0) or 0.0),
            keyword_rank=int(d.get("keyword_rank", 0) or 0),
            semantic_rank=int(d.get("semantic_rank", 0) or 0),
            page_start=int(d.get("page_start", 0) or 0),
            page_end=int(d.get("page_end", 0) or 0),
            heading_path=str(d.get("heading_path", "") or ""),
            chunk_type=str(d.get("chunk_type", "") or ""),
        )


@dataclass
class RAGContext:
    """拼装好的上下文。"""

    text: str = ""
    """可直接放入 prompt 的完整文本（含防注入标记）。"""

    estimated_tokens: int = 0
    budget_tokens: int = 0

    fixed_overhead_tokens: int = 0
    """防注入声明 + 标题 + 指令占用的 token。

    预算再小也省不掉。调用方应保证 `context_tokens` 明显大于它，
    否则一条资料都放不进去（表现为 `used_chunks` 恒为 0）。
    """

    used_chunks: int = 0
    dropped_chunks: int = 0
    warnings: list[str] = field(default_factory=list)
    """裁剪与空结果提示，**应展示给用户**。"""

    sources: list[KnowledgeSource] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> RAGContext:
        return cls(
            text=str(d.get("text", "")),
            estimated_tokens=int(d.get("estimated_tokens", 0) or 0),
            budget_tokens=int(d.get("budget_tokens", 0) or 0),
            fixed_overhead_tokens=int(
                d.get("fixed_overhead_tokens", 0) or 0),
            used_chunks=int(d.get("used_chunks", 0) or 0),
            dropped_chunks=int(d.get("dropped_chunks", 0) or 0),
            warnings=[str(w) for w in (d.get("warnings") or [])],
            sources=[KnowledgeSource.from_dict(s)
                     for s in (d.get("sources") or [])],
        )


@dataclass
class SearchResult:
    """检索结果。"""

    hits: list[SearchHit] = field(default_factory=list)
    mode: str = ""
    degraded: bool = False
    note: str = ""
    all_bases: bool = False
    """本次请求用了 ``[ALL_BASES]`` 展开。"""

    base_count: int = 0
    """实际参与检索的库数（仅 all_bases 时非零）。"""

    context: Optional[RAGContext] = None
    """拼好的 RAG 上下文（仅 build_context=True 时非 None）。"""

    total_ms: int = 0


# ---------------------------------------------------------------------------
# 知识库管理
# ---------------------------------------------------------------------------


@dataclass
class KBSummary:
    """一个知识库的概要。"""

    id: str = ""
    name: str = ""
    description: str = ""
    document_count: int = 0
    chunk_count: int = 0
    embedded_count: int = 0
    created_at: int = 0
    """文档/切片/向量计数由服务端聚合。

    服务端查单个库的统计失败时不影响列表（可能正在索引），此时
    这三个字段为 0——**不要把 0 当成"库里什么都没有"**。
    """

    @classmethod
    def from_dict(cls, d: dict) -> KBSummary:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "") or ""),
            document_count=int(d.get("document_count", 0) or 0),
            chunk_count=int(d.get("chunk_count", 0) or 0),
            embedded_count=int(d.get("embedded_count", 0) or 0),
            created_at=int(d.get("created_at", 0) or 0),
        )


@dataclass
class KnowledgeBaseRef:
    """创建成功后的引用。"""

    id: str = ""
    name: str = ""
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeBaseRef:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "") or ""),
        )


@dataclass
class VectorizeProgress:
    """向量化进度。"""

    total_chunks: int = 0
    embedded_chunks: int = 0
    pending_chunks: int = 0
    stale_chunks: int = 0
    """失效向量数（换了 Embedding 模型或前缀，待重建）。"""

    stale_reason: str = ""
    """向量为何失效的可读说明，空表示无已知原因。

    用户看到 stale_chunks 等于 total_chunks 时唯一的疑问是"为什么"。
    换模型、换前缀、还是系统出错？处理方式相同（重新向量化），
    但用户需要知道发生了什么才愿意去做。
    """

    model: str = ""
    running: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> VectorizeProgress:
        return cls(
            total_chunks=int(d.get("total_chunks", 0) or 0),
            embedded_chunks=int(d.get("embedded_chunks", 0) or 0),
            pending_chunks=int(d.get("pending_chunks", 0) or 0),
            stale_chunks=int(d.get("stale_chunks", 0) or 0),
            stale_reason=str(d.get("stale_reason", "") or ""),
            model=str(d.get("model", "") or ""),
            running=bool(d.get("running", False)),
        )


@dataclass
class VectorizeResult:
    """向量化的结果。"""

    kb_id: str = ""
    vectorized: int = 0
    """本次向量化的切片数（同步模式）。"""

    started: bool = False
    """已启动后台任务（异步模式）。"""

    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> VectorizeResult:
        return cls(
            kb_id=str(d.get("kb_id", "")),
            vectorized=int(d.get("vectorized", 0) or 0),
            started=bool(d.get("started", False)),
            note=str(d.get("note", "") or ""),
        )


@dataclass
class KBStat:
    """一个知识库的统计。"""

    kb_id: str = ""
    document_count: int = 0
    chunk_count: int = 0
    embedded_count: int = 0
    stale_count: int = 0
    """失效向量数（换了 Embedding 模型后待重建）。"""

    vectorize: Optional[VectorizeProgress] = None
    """None 表示服务端查进度失败（不影响主统计）。"""


@dataclass
class DocumentSummary:
    """知识库中的一个文档。"""

    id: str = ""
    name: str = ""
    file_type: str = ""
    storage_mode: str = ""
    file_size: int = 0
    status: str = ""
    """ACTIVE / MISSING / MODIFIED / PENDING / PROCESSING / FAILED。"""

    index_status: str = ""
    """PENDING / READY / PARTIAL / FAILED。"""

    chunk_count: int = 0
    error: str = ""
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> DocumentSummary:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            file_type=str(d.get("file_type", "")),
            storage_mode=str(d.get("storage_mode", "")),
            file_size=int(d.get("file_size", 0) or 0),
            status=str(d.get("status", "")),
            index_status=str(d.get("index_status", "")),
            chunk_count=int(d.get("chunk_count", 0) or 0),
            error=str(d.get("error", "") or ""),
            created_at=int(d.get("created_at", 0) or 0),
            updated_at=int(d.get("updated_at", 0) or 0),
        )


@dataclass
class ImportRequest:
    """导入文档的请求（传路径）。

    只传**路径**而不传文件内容：Runtime 是本机服务，SDK 与应用在
    同一台机器上，传路径比传整个文件内容省内存也省带宽。路径必须
    是绝对路径——相对路径的基准取决于服务进程的工作目录，那是
    用户无法预期的。
    """

    path: str
    name: Optional[str] = None
    mode: Optional[str] = None
    """MANAGED（默认）/ EXTERNAL。

    MANAGED 复制文件进 Runtime 数据目录，原文件变动不影响知识库；
    EXTERNAL 只记录路径，原文件失效会导致 MISSING。
    """

    password: Optional[str] = None
    """加密 PDF 的密码。

    **不传也可以先试**："只限制打印"类 PDF 是空用户密码 + 非空
    所有者密码，任何阅读器都能直接打开。密码不写入数据库，只在
    本次导入的内存中使用；之后需要重建索引时需再次提供。
    """

    update_document_id: Optional[str] = None
    """非空表示更新已有文档而非新建（做增量 diff）。"""

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"path": self.path}
        for name in ("name", "mode", "password", "update_document_id"):
            v = getattr(self, name)
            if v is not None:
                out[name] = v
        return out


@dataclass
class ChunkInput:
    """一个待入库的块。"""

    ordinal: int
    """块在文档内的序号（0 基），**必须唯一**——增量 diff 的定位依据。"""

    text: str
    page_start: int = 0
    """起始页（1 基）。0 或省略表示未知。"""

    page_end: int = 0
    """结束页。0 或省略表示未知。"""

    start_rune: int = 0
    rune_count: int = 0
    heading_path: str = ""
    """章节路径，如"第三章 保修 > 3.2 期限"。超过 512 字符会被截断。"""

    chunk_type: str = ""
    """paragraph / heading / table / list / code。省略或非法值归一为 paragraph。"""

    def to_wire(self) -> dict:
        out: dict = {"ordinal": self.ordinal, "text": self.text}
        # 只发有值的字段：0 与空串在服务端语义是"未知"，
        # 显式发过去与省略等价，但少发能让请求体更小、日志更清晰
        if self.page_start:
            out["page_start"] = self.page_start
        if self.page_end:
            out["page_end"] = self.page_end
        if self.start_rune:
            out["start_rune"] = self.start_rune
        if self.rune_count:
            out["rune_count"] = self.rune_count
        if self.heading_path:
            out["heading_path"] = self.heading_path
        if self.chunk_type:
            out["chunk_type"] = self.chunk_type
        return out


@dataclass
class ChunkerInfo:
    """客户端切块算法的标识。

    ``version`` **必填**：它标识算法而不只是参数。参数相同但算法改了
    （如句末判定逻辑变化）时，参数无法表达"这不是同一种切法"，混用会
    导致用户点来源时页码错位，而系统无法事后区分。

    版本与库内不一致时服务端返回 409 → SDK 抛 ``ErrChunkerMismatch``。
    **不要重试**（重试必然再失败），正确处理是重建该库索引。
    """

    name: str
    version: str
    target_chars: int = 0
    max_chars: int = 0
    overlap_chars: int = 0

    def to_wire(self) -> dict:
        out: dict = {"name": self.name, "version": self.version}
        if self.target_chars:
            out["target_chars"] = self.target_chars
        if self.max_chars:
            out["max_chars"] = self.max_chars
        if self.overlap_chars:
            out["overlap_chars"] = self.overlap_chars
        return out


@dataclass
class IngestChunksRequest:
    """入库已解析、已切好的文本块（解析下沉后的主路径）。

    Runtime 不再解析文档、不再切块。客户端用本地库解析并切块，把结果
    连同结构元数据一起提交。换来的是格式覆盖率从约 70% 接近 100%，
    且**页码溯源与标题路径才变得可行**。
    """

    name: str
    """显示名（必填），用于来源卡片。"""

    file_hash: str
    """内容的 SHA-256 十六进制串（必填）。同库内去重与幂等的依据。"""

    chunks: list[ChunkInput]
    """已切好的块（必填，最多 2000 项）。"""

    chunker: Optional[ChunkerInfo] = None
    """切块算法（必填）。"""

    file_type: Optional[str] = None
    """文件类型，如 "pdf"。仅用于筛选与统计——Runtime 不据此选解析器。"""

    file_size: int = 0
    modified_at: int = 0
    """源文件修改时间（Unix 秒）。"""

    source_label: Optional[str] = None
    """来源的可读描述，只存不解析（可给本地路径）。"""

    update_document_id: Optional[str] = None
    """非空表示增量更新已有文档。"""

    def to_wire(self) -> dict:
        out: dict = {
            "name": self.name,
            "file_hash": self.file_hash,
            "chunks": [c.to_wire() for c in self.chunks],
        }
        if self.chunker is not None:
            out["chunker"] = self.chunker.to_wire()
        if self.file_type:
            out["file_type"] = self.file_type
        if self.file_size:
            out["file_size"] = self.file_size
        if self.modified_at:
            out["modified_at"] = self.modified_at
        if self.source_label:
            out["source_label"] = self.source_label
        if self.update_document_id:
            out["update_document_id"] = self.update_document_id
        return out


@dataclass
class ImportResult:
    """导入文档的结果。"""
    document_id: str = ""
    chunk_count: int = 0
    content_runes: int = 0
    duplicate: bool = False
    """内容与已有文档完全相同，本次未重复导入。

    这**不是错误**——同一知识库内相同内容只保留一份。
    """

    existing_id: str = ""
    """`duplicate=True` 时指向已有文档。"""

    note: str = ""
    diff: Optional[dict] = None
    """仅在 `update_document_id` 非空时出现，含 added/updated/deleted 等。"""

    @classmethod
    def from_dict(cls, d: dict) -> ImportResult:
        return cls(
            document_id=str(d.get("document_id", "")),
            chunk_count=int(d.get("chunk_count", 0) or 0),
            content_runes=int(d.get("content_runes", 0) or 0),
            duplicate=bool(d.get("duplicate", False)),
            existing_id=str(d.get("existing_id", "") or ""),
            note=str(d.get("note", "") or ""),
            diff=d.get("diff"),
        )

# ---------------------------------------------------------------------------
# 相似检测（Phase 2）
#
# 三个方法都是只读：检测只"告诉"哪几份看起来是同一份，不替用户
# 决定保留哪一份。用户的选择通过已有方法表达：
#
#     client.delete_document(kb, b)
#     client.ingest_chunks(kb, a)
# ---------------------------------------------------------------------------


@dataclass
class DedupCheckRequest:
    """相似检测请求。"""

    doc_ids: list[str] = field(default_factory=list)
    """限定参与分组的文档。空表示该库全部。

    注意语义：这是"哪些文档参与"，不是"只查这些"——相似是相对的，
    只给一份文档无法判断它是否重复。
    """

    recompute: bool = False
    """为 True 时先补算缺失的指纹（完整但慢，5000 篇约 10 秒）。

    默认 False 走快路径，缺席的文档在 ``pending`` 里返回。
    """

    async_: bool = False
    """为 True 时立即返回任务 ID，检测在后台以 P3 优先级运行。

    全库检测在 5000 篇规模下约 10 秒且纯 CPU 密集。同步执行会让
    客户端卡住，更糟的是会与用户提问抢 CPU——后台模式走调度器的
    PriorityBackground，资源紧张时会被暂停甚至被 P0 抢占。

    设置后返回的 :class:`DedupCheckResult` 的 ``task_id`` 非空、
    ``groups`` 为空；用 ``client.task(task_id)`` 轮询。
    """

    def to_wire(self) -> dict:
        out: dict = {}
        if self.doc_ids:
            out["doc_ids"] = list(self.doc_ids)
        if self.recompute:
            out["recompute"] = True
        if self.async_:
            out["async"] = True
        return out


@dataclass
class DedupStats:
    """检测统计。"""

    scanned: int = 0
    reused: int = 0
    groups: int = 0
    candidates: int = 0

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional[DedupStats]:
        if not d:
            return None
        return cls(
            scanned=int(d.get("scanned", 0) or 0),
            reused=int(d.get("reused", 0) or 0),
            groups=int(d.get("groups", 0) or 0),
            candidates=int(d.get("candidates", 0) or 0),
        )


@dataclass
class DupMember:
    """组成员。"""

    document_id: str = ""
    name: str = ""
    """显示名。快路径（未 recompute）时可能为空。"""

    hamming: int = 0
    """与代表的汉明距离（代表自己为 0）。"""

    @classmethod
    def from_dict(cls, d: dict) -> DupMember:
        return cls(
            document_id=str(d.get("document_id", "")),
            name=str(d.get("name", "") or ""),
            hamming=int(d.get("hamming", 0) or 0),
        )


@dataclass
class DupGroup:
    """一个相似组。"""

    id: str = ""
    kind: str = ""
    """``exact``（sha256 相同）/ ``similar``（SimHash 距离在阈值内）。"""

    max_hamming: int = 0
    members: list[DupMember] = field(default_factory=list)
    """成员。**第一个是代表**（hamming = 0）。

    "代表"只是"组内最中间的那份"，不是权威版本——那个决定权在用户。
    """

    @classmethod
    def from_dict(cls, d: dict) -> DupGroup:
        return cls(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "")),
            max_hamming=int(d.get("max_hamming", 0) or 0),
            members=[DupMember.from_dict(m) for m in (d.get("members") or [])],
        )

    def summary(self) -> str:
        """便于日志/UI 展示的一行摘要。"""
        names = [m.name or m.document_id for m in self.members]
        return (f"{self.kind} 组（{len(self.members)} 份，"
                f"最大距离 {self.max_hamming}）：" + " / ".join(names))


@dataclass
class DedupCheckResult:
    """检测结果。"""

    groups: list[DupGroup] = field(default_factory=list)

    pending: list[str] = field(default_factory=list)
    """还没有指纹、未参与本次检查的文档 ID。

    **非空意味着结果不完整**。不检查它就会把"刚导入的那份还没算
    指纹"误读成"没有重复"。
    """

    stats: Optional[DedupStats] = None
    """仅在 recompute=True 时有值。"""

    total_ms: int = 0

    task_id: str = ""
    """非空表示这是 async 模式，检测在后台运行。

    此时 ``groups`` 与 ``pending`` 都为空——用
    ``client.task(task_id)`` 查进度与结果。
    """


@dataclass
class DedupGroupsResult:
    """列出已有分组的结果。"""

    groups: list[DupGroup] = field(default_factory=list)

    computed_at: int = 0
    """上次检测时间（Unix 秒）。0 表示从未检测过。"""

    stale: bool = False
    """库里有文档还没参与检测，结果**不完整**。"""

    pending_count: int = 0


@dataclass
class DiffBlock:
    """一个差异块。"""

    ordinal: int = 0
    preview: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> DiffBlock:
        return cls(
            ordinal=int(d.get("ordinal", 0) or 0),
            preview=str(d.get("preview", "") or ""),
        )


@dataclass
class DiffChanged:
    """一处变更。"""

    ordinal: int = 0
    before: str = ""
    after: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> DiffChanged:
        return cls(
            ordinal=int(d.get("ordinal", 0) or 0),
            before=str(d.get("before", "") or ""),
            after=str(d.get("after", "") or ""),
        )


@dataclass
class DupDiff:
    """两份文档的段落级差异。"""

    doc_a: str = ""
    doc_b: str = ""
    added: list[DiffBlock] = field(default_factory=list)
    removed: list[DiffBlock] = field(default_factory=list)
    changed: list[DiffChanged] = field(default_factory=list)
    unchanged: int = 0
    truncated: bool = False
    """结果被截断（单侧超过 200 块预览）。"""
