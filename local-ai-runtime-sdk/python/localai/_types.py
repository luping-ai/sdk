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

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeSource:
        return cls(
            index=int(d.get("index", 0) or 0),
            chunk_id=str(d.get("chunk_id", "")),
            document_id=str(d.get("document_id", "")),
            source=str(d.get("source", "") or ""),
            score=float(d.get("score", 0.0) or 0.0),
        )


@dataclass
class KnowledgeInfo:
    """本次调用如何使用知识库。"""

    kb_ids: list[str] = field(default_factory=list)
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


@dataclass
class SearchRequest:
    """知识库检索请求。"""

    query: str
    kb_ids: list[str]
    """必填。不允许全库检索（文档 22.9 的隔离要求）。"""

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
    model: str = ""
    running: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> VectorizeProgress:
        return cls(
            total_chunks=int(d.get("total_chunks", 0) or 0),
            embedded_chunks=int(d.get("embedded_chunks", 0) or 0),
            pending_chunks=int(d.get("pending_chunks", 0) or 0),
            stale_chunks=int(d.get("stale_chunks", 0) or 0),
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
