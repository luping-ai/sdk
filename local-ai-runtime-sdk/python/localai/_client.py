"""客户端与全部端点。

## 与 Go SDK 的对应关系

本文件的每个方法都对应 Go SDK `localai` 包里的同名方法（Python 用
snake_case）。**语义必须一致**：同样的错误分类、同样的重试策略、
同样的默认超时。否则同一个业务在两个语言里行为不同，排查时无从
下手。

## 自动重试为什么必须是"选择性"的

它会在服务端已过载时放大压力。因此只重试"压力类"错误
（`resource_limited` / `queue_full`），并且带抖动——多个客户端同时
收到 `resource_limited` 后若在同一时刻重试，会再次把服务端打满。

`input_too_long` / `invalid_request` / `capability_unmet` **绝不重试**：
它们重试必然再次失败，只会浪费用户时间。
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional

from ._discovery import Discovery, discover
from ._errors import (
    APIError,
    ErrInvalidRequest,
    ErrModelUnavailable,
    ErrQueueFull,
    ErrResourceLimited,
    ErrRuntimeUnavailable,
)
from ._stream import (
    StreamEvent,
    StreamEventType,
    decode_event,
    parse_sse,
)
from ._transport import ClosingIterator, Transport, build_url
from ._types import (
    Capability,
    ChatRequest,
    ChatResult,
    DocumentSummary,
    HealthReport,
    ImportRequest,
    ImportResult,
    KBStat,
    KBSummary,
    KnowledgeBaseRef,
    KnowledgeInfo,
    Model,
    ModelInfo,
    RAGContext,
    SearchHit,
    SearchRequest,
    SearchResult,
    Timeouts,
    Usage,
    VectorizeProgress,
    VectorizeResult,
)

__all__ = ["Client", "Config"]

# 自动重试的默认次数（不含首次），文档 24.4
DEFAULT_MAX_RETRIES = 2


@dataclass
class Config:
    """客户端配置。

    全部字段可选：`Config()` 就能工作（自动发现 + 默认超时）。
    """

    base_url: Optional[str] = None
    """显式指定 Runtime 地址。None 则自动发现。"""

    token: Optional[str] = None
    """显式指定访问凭证。None 则从环境变量或 discovery 文件取。"""

    timeouts: Optional[Timeouts] = None
    """三层超时。None 用文档 24.2 的默认值。"""

    max_retries: Optional[int] = None
    """自动重试的最大次数（不含首次）。None 用默认策略（2 次）。

    显式设 0 可完全关闭重试。注意与 Go SDK 的区别：Go 那里零值有
    歧义（"没设置"还是"不重试"）所以额外加了 NoRetry 开关；Python
    的 None / 0 能区分，不需要那样。
    """


class Client:
    """Runtime 的客户端。

    线程安全：内部无可变共享状态（除了 discovery 缓存，只读），
    可在多线程中共享同一个实例。

    典型用法：

        client = Client()
        cap = client.capability()
        if not cap.chat_enabled:
            raise SystemExit(f"本机无法对话：{cap.reason}")

        res = client.chat(ChatRequest(messages=[Message("user", "你好")]))
        print(res.content)
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        cfg = config or Config()

        disc = discover(cfg.base_url, cfg.token)
        self._base_url = disc.url
        self._token = disc.token
        self._discovery = disc

        self._timeouts = (cfg.timeouts or Timeouts()).resolved()
        self._transport = Transport(self._timeouts)

        if cfg.max_retries is None:
            self._max_retries = DEFAULT_MAX_RETRIES
        else:
            self._max_retries = max(0, int(cfg.max_retries))

    # -- 只读属性 ------------------------------------------------------

    @property
    def base_url(self) -> str:
        """实际使用的地址（已做发现解析）。"""
        return self._base_url

    @property
    def discovery(self) -> Discovery:
        """发现结果（便于诊断连接的是哪个实例）。"""
        return self._discovery

    def __repr__(self) -> str:
        # 不回显 token：日志里出现凭证是最常见的泄露途径
        return f"<Client base_url={self._base_url!r}>"

    # -- 底层请求 ------------------------------------------------------

    def _do(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: Optional[float] = None,
        retry: bool = True,
    ) -> dict:
        """执行请求并解析信封，返回整个信封 dict。"""
        payload = None
        headers = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}

        url = build_url(self._base_url, path)
        attempts = self._max_retries if retry else 0
        last: Optional[APIError] = None

        for attempt in range(attempts + 1):
            try:
                raw = self._transport.request(
                    method, url, self._token,
                    body=payload, headers=headers, timeout=timeout,
                )
                return self._decode_envelope(raw)
            except APIError as e:
                last = e
                wait = self._retry_wait(e, attempt)
                if wait is None:
                    raise
                time.sleep(wait)

        # 循环结束仍未成功（attempts 用尽）
        assert last is not None
        raise last

    def _decode_envelope(self, raw: bytes) -> dict:
        """解析响应信封（文档 19.1）。"""
        try:
            env = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise APIError(
                ErrRuntimeUnavailable,
                message=f"无法解析响应（可能版本不兼容）: {e}",
            ) from None

        if not isinstance(env, dict):
            raise APIError(
                ErrRuntimeUnavailable, message="响应不是对象"
            )

        if not env.get("success", False) or env.get("error"):
            err = env.get("error") or {}
            code = str(err.get("code", ""))
            from ._errors import kind_for_code
            raise APIError(
                kind_for_code(code),
                code=code,
                message=str(err.get("message", "")) or "服务端返回失败",
                retryable=bool(err.get("retryable", False)),
                retry_after=float(err.get("retry_after_ms", 0) or 0) / 1000.0,
                request_id=str(env.get("request_id", "")),
            )

        return env

    def _retry_wait(self, e: APIError, attempt: int) -> Optional[float]:
        """判断该错误是否值得重试，返回等待秒数（None 表示不重试）。

        规则与文档 24.4 及 Go SDK 的 shouldRetry 一致。
        """
        if attempt >= self._max_retries:
            return None

        if e.kind is ErrResourceLimited or e.kind is ErrQueueFull:
            base = e.retry_after if e.retry_after > 0 else (attempt + 1) * 1.0
            return _jitter(base)

        if e.kind is ErrModelUnavailable:
            # 模型可能在加载中，给一次机会
            return _jitter(2.0)

        if e.kind is ErrRuntimeUnavailable:
            # 网络层失败：Runtime 可能刚启动
            return _jitter(1.0)

        # input_too_long / invalid_request / unauthorized / quota：
        # 重试无意义，甚至有害（增加负载）
        return None

    # -- 健康 ----------------------------------------------------------

    def health(self) -> HealthReport:
        """检查 Runtime 是否就绪。

        用 `/health/ready` 而不是 `/health/live`：后者只说明进程活着，
        而业务方关心的是"能不能推理"。

        State 不是 ready 时**不抛异常**——调用方可以据此展示"正在加载
        模型"之类的提示。只有网络层失败才抛。
        """
        try:
            env = self._do(
                "GET", "/health/ready",
                timeout=self._timeouts.connect, retry=False,
            )
        except APIError as e:
            if e.status_code == 503:
                # 503 时服务端仍返回了 HealthReport（在 data 字段）
                return HealthReport(
                    state="starting", reason=e.message)
            raise
        return HealthReport.from_dict(env.get("data") or {})

    def live(self) -> bool:
        """只检查进程是否存活（用于进程守护）。"""
        try:
            self._do("GET", "/health/live",
                     timeout=self._timeouts.connect, retry=False)
            return True
        except APIError:
            return False

    def capability(self) -> Capability:
        """这台机器能做什么（对话 / RAG / 仅规则引擎）。

        ## 为什么调用方应该先查它

        本地 Runtime 可能运行在内存很小的机器上。此时服务**活着**但
        能力受限：

        - `rag_enabled=False` 时传 kb_ids 会被忽略——用户会以为
          "检索过了，只是没找到"，而不是"知识库根本没启用"
        - `chat_enabled=False` 时只能走规则引擎（关键词匹配），
          这不是"AI 降级"而是"没有 AI"

        建议启动时查一次并缓存：能力在 Runtime 启动时确定，不会随
        请求变化。
        """
        env = self._do("GET", "/api/v1/capability",
                       timeout=self._timeouts.connect)
        return Capability.from_dict(env.get("data") or {})

    # -- 模型 ----------------------------------------------------------

    def models(self) -> list[Model]:
        """列出所有已注册的模型。"""
        models, _active = self._models_and_active()
        return models

    def active_model(self) -> str:
        """返回当前加载的模型 ID，未加载时返回空串。

        **未加载模型不是错误**：这是正常的初始状态。返回错误会迫使
        调用方额外判断，而"没有模型"本来就是一种合法的答案。

        实现上读 `/models` 的 active 字段，而不是 `/models/active`
        ——后者在无模型时返回 503，把"正常状态"当成了错误。
        """
        _models, active = self._models_and_active()
        return active

    def _models_and_active(self) -> tuple[list[Model], str]:
        env = self._do("GET", "/api/v1/models")
        data = env.get("data") or {}
        models = [Model.from_dict(m) for m in (data.get("models") or [])]
        return models, str(data.get("active", "") or "")

    def load_model(self, model_id: str) -> None:
        """加载指定模型（会卸载当前模型）。

        这是耗时操作（可能数十秒）。服务端默认用 5 分钟超时。
        """
        if not model_id:
            raise APIError(ErrInvalidRequest, message="model_id 不能为空")
        self._do("POST", "/api/v1/models/load",
                 {"model_id": model_id}, timeout=None)

    def unload_model(self) -> None:
        """卸载当前模型。"""
        self._do("POST", "/api/v1/models/unload")

    # -- 对话 ----------------------------------------------------------

    def chat(self, req: ChatRequest) -> ChatResult:
        """执行一次对话（非流式）。"""
        if not isinstance(req, ChatRequest):
            raise APIError(ErrInvalidRequest, message="req 必须是 ChatRequest")

        env = self._do("POST", "/api/v1/chat", req.to_wire(),
                       timeout=self._timeouts.total)
        data = env.get("data") or {}

        return ChatResult(
            content=str(data.get("content", "")),
            finish_reason=str(data.get("finish_reason", "")),
            truncated=bool(data.get("truncated", False)),
            model=ModelInfo.from_dict(env.get("model")),
            usage=Usage.from_dict(env.get("usage")),
            knowledge=KnowledgeInfo.from_dict(env.get("knowledge")),
            warnings=[str(w) for w in (env.get("warnings") or [])],
            request_id=str(env.get("request_id", "")),
        )

    def chat_stream(
        self, req: ChatRequest
    ) -> Iterator[str]:
        """流式对话，只产出文本增量。

        这是最常用的形态。需要排队位置、usage 等完整事件时用
        `stream_events`。

        返回的迭代器支持 `with` 与 `close()`：提前 break 会关闭连接，
        服务端感知断开后停止生成（文档 19.4）——否则引擎会继续生成
        到结束，白烧 CPU。
        """
        return self._stream(req, only_text=True)

    def stream_events(self, req: ChatRequest) -> Iterator[StreamEvent]:
        """流式对话，产出全部事件（queued / meta / delta / usage / ...）。"""
        return self._stream(req, only_text=False)

    def _stream(
        self, req: ChatRequest, *, only_text: bool
    ) -> Iterator[Any]:
        if not isinstance(req, ChatRequest):
            raise APIError(ErrInvalidRequest, message="req 必须是 ChatRequest")

        wire = req.to_wire()
        # 强制 stream=true：调用方可能忘了设
        wire["stream"] = True
        body = json.dumps(wire).encode("utf-8")

        url = build_url(self._base_url, "/api/v1/chat")

        def gen() -> Iterator[Any]:
            lines = self._transport.stream(url, self._token, body)
            finished = False
            for payload in parse_sse(lines):
                if payload.strip() == "[DONE]":
                    finished = True
                    break

                ev = decode_event(payload)
                if ev is None:
                    # 单帧坏掉不中断整条流：继续读后续内容，
                    # 用户至少能看到部分输出
                    continue

                if ev.type == StreamEventType.ERROR:
                    if ev.error is not None:
                        raise ev.error
                    raise APIError(ErrRuntimeUnavailable,
                                   message="流中断")

                if only_text:
                    if ev.type == StreamEventType.DELTA and ev.text:
                        yield ev.text
                else:
                    yield ev

            # 正常结束（收到 [DONE] 或流自然关闭）
            _ = finished

        it = gen()
        closed = {"done": False}

        def on_close() -> None:
            closed["done"] = True

        return ClosingIterator(it, on_close)

    def chat_sync(self, req: ChatRequest) -> ChatResult:
        """流式请求但同步返回完整结果（累积全部 delta）。

        用途：想复用流式端点（首字节更快、可感知排队）但不需要
        逐字显示的场景。
        """
        content: list[str] = []
        finish_reason = ""
        model = None
        usage = None
        knowledge = None
        warnings: list[str] = []
        request_id = ""

        for ev in self._stream(req, only_text=False):
            if ev.type == StreamEventType.DELTA:
                content.append(ev.text)
            elif ev.type == StreamEventType.DONE:
                finish_reason = ev.finish_reason
            elif ev.type == StreamEventType.META:
                model = ModelInfo(
                    actual=ev.model, degraded=ev.degraded)
                request_id = ev.request_id
                if ev.knowledge:
                    knowledge = KnowledgeInfo.from_dict(ev.knowledge)
            elif ev.type == StreamEventType.USAGE:
                usage = Usage.from_dict(ev.usage)
            elif ev.type == StreamEventType.WARNING:
                warnings.extend(ev.warnings)

        text = "".join(content)
        return ChatResult(
            content=text,
            finish_reason=finish_reason or "stop",
            truncated=(finish_reason == "length"),
            model=model,
            usage=usage,
            knowledge=knowledge,
            warnings=warnings,
            request_id=request_id,
        )

    # -- 检索 ----------------------------------------------------------

    def search(self, req: SearchRequest) -> SearchResult:
        """知识库检索（关键词 + 语义 + RRF 混合）。"""
        if not isinstance(req, SearchRequest):
            raise APIError(ErrInvalidRequest, message="req 必须是 SearchRequest")
        if not req.query:
            raise APIError(ErrInvalidRequest, message="query 不能为空")
        if not req.kb_ids:
            # 不允许全库检索（文档 22.9 的隔离要求）。在客户端就拦住，
            # 省一次往返，也让错误更早暴露。
            raise APIError(ErrInvalidRequest,
                           message="kb_ids 不能为空（不允许全库检索）")

        env = self._do("POST", "/api/v1/search", req.to_wire(),
                       timeout=self._timeouts.total)
        data = env.get("data") or {}

        ctx = None
        raw_ctx = data.get("context")
        if isinstance(raw_ctx, dict):
            ctx = RAGContext.from_dict(raw_ctx)

        return SearchResult(
            hits=[SearchHit.from_dict(h) for h in (data.get("results") or [])],
            mode=str(data.get("mode", "")),
            degraded=bool(data.get("degraded", False)),
            note=str(data.get("note", "") or ""),
            context=ctx,
            total_ms=int(data.get("total_ms", 0) or 0),
        )

    # -- 知识库管理 ----------------------------------------------------

    def create_kb(
        self, name: str, description: str = "", kb_id: str = ""
    ) -> KnowledgeBaseRef:
        """创建知识库。

        `kb_id` 可省略（服务端自动生成）。若指定，只能包含字母、
        数字、短横线与下划线——ID 会拼进托管目录路径。
        """
        if not name or not name.strip():
            # 与服务端一致地校验空白名：只检查空串会放过 "  "，
            # 让无效请求跑到服务端再被打回，浪费一次往返
            raise APIError(ErrInvalidRequest, message="name 不能为空")

        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if kb_id:
            body["id"] = kb_id

        env = self._do("POST", "/api/v1/knowledge/bases", body)
        return KnowledgeBaseRef.from_dict(env.get("data") or {})

    def list_kb(self) -> list[KBSummary]:
        """列出所有知识库。"""
        env = self._do("GET", "/api/v1/knowledge/bases")
        data = env.get("data") or {}
        return [KBSummary.from_dict(b) for b in (data.get("bases") or [])]

    def delete_kb(self, kb_id: str) -> None:
        """删除知识库及其全部数据（文档、切片、向量、全文索引）。

        **不可恢复。** 托管副本会一并删除；EXTERNAL 模式的原文件不受影响。
        """
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")
        self._do("DELETE", "/api/v1/knowledge/bases/" + _seg(kb_id))

    def kb_stat(self, kb_id: str) -> KBStat:
        """返回知识库的统计与向量化进度。"""
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")

        env = self._do(
            "GET", "/api/v1/knowledge/bases/" + _seg(kb_id) + "/stats")
        d = env.get("data") or {}

        vec = d.get("vectorize")
        return KBStat(
            kb_id=str(d.get("kb_id", "")),
            document_count=int(d.get("document_count", 0) or 0),
            chunk_count=int(d.get("chunk_count", 0) or 0),
            embedded_count=int(d.get("embedded_count", 0) or 0),
            stale_count=int(d.get("stale_count", 0) or 0),
            vectorize=VectorizeProgress.from_dict(vec)
            if isinstance(vec, dict) else None,
        )

    def vectorize(self, kb_id: str, *, is_async: bool = False
                  ) -> VectorizeResult:
        """为尚无向量的切片生成向量。

        文档导入时切片已入库，因此**关键词检索立即可用**；本方法补齐
        语义检索所需的向量。

        默认同步：小知识库只需几百毫秒，同步返回能让调用方立刻知道
        结果。大知识库应设 `is_async=True`，否则请求会持续数分钟。

        （参数名用 `is_async` 而不是 `async`：后者是 Python 关键字。）
        """
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")

        body = {"async": True} if is_async else None
        env = self._do(
            "POST", "/api/v1/knowledge/bases/" + _seg(kb_id) + "/vectorize",
            body, timeout=self._timeouts.total)
        return VectorizeResult.from_dict(env.get("data") or {})

    def list_documents(self, kb_id: str) -> list[DocumentSummary]:
        """列出知识库中的文档。"""
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")

        env = self._do(
            "GET", "/api/v1/knowledge/bases/" + _seg(kb_id) + "/documents")
        data = env.get("data") or {}
        return [DocumentSummary.from_dict(d)
                for d in (data.get("documents") or [])]

    def import_document(self, kb_id: str, req: ImportRequest) -> ImportResult:
        """导入文档（或更新已有文档）。

        支持 .txt / .md / .pdf / .docx / .xlsx。

        幂等：同一知识库内相同内容（SHA-256）只保留一份，重复导入
        返回 `duplicate=True` 而不是错误。

        导入是耗时操作（解析 + 切片），大文件可能数十秒。**不套用
        默认总超时**——那会把正常的慢导入误判为超时。
        """
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")
        if not isinstance(req, ImportRequest):
            raise APIError(ErrInvalidRequest, message="req 必须是 ImportRequest")
        if not req.path or not req.path.strip():
            raise APIError(ErrInvalidRequest, message="path 不能为空")

        env = self._do(
            "POST", "/api/v1/knowledge/bases/" + _seg(kb_id) + "/documents",
            req.to_wire(), timeout=None)
        return ImportResult.from_dict(env.get("data") or {})

    def delete_document(
        self, kb_id: str, doc_id: str, *, keep_file: bool = False
    ) -> None:
        """删除文档及其切片、向量、全文索引。

        `keep_file=True` 时保留托管副本。EXTERNAL 模式恒不删除原文件
        ——那可能毁掉用户唯一的资料。
        """
        if not kb_id or not doc_id:
            raise APIError(ErrInvalidRequest,
                           message="kb_id 与 doc_id 都不能为空")

        path = ("/api/v1/knowledge/bases/" + _seg(kb_id)
                + "/documents/" + _seg(doc_id))
        if keep_file:
            path += "?keep_file=1"
        self._do("DELETE", path)

    def upload_document(
        self,
        kb_id: str,
        filename: str,
        content: bytes,
        *,
        name: str = "",
        password: str = "",
        update_document_id: str = "",
    ) -> ImportResult:
        """上传文件内容并导入（multipart/form-data）。

        与 `import_document` 的区别：这里传**文件内容**，那边传路径。
        给 GUI / Web 用：用户选中的文件在应用进程里，没有"Runtime 能
        直接读的路径"这个概念——尤其应用跑在容器、远程桌面或沙箱里时。

        `filename` 必须带扩展名：服务端按扩展名选解析器，传 "data"
        会被拒绝（不支持的文件类型）。

        恒为 MANAGED 存储模式：临时文件导入后删除，外部引用无从谈起。
        """
        if not kb_id:
            raise APIError(ErrInvalidRequest, message="kb_id 不能为空")
        if not filename or not filename.strip():
            raise APIError(
                ErrInvalidRequest,
                message="filename 不能为空（服务端按扩展名选解析器）")
        if content is None:
            raise APIError(ErrInvalidRequest, message="content 不能为空")

        boundary = f"----lupinglocalai{random.randrange(1 << 48):012x}"
        body = _multipart(
            boundary,
            fields={
                "name": name,
                "password": password,
                "update_document_id": update_document_id,
            },
            file_field="file",
            filename=filename,
            file_bytes=content,
        )

        url = build_url(
            self._base_url,
            "/api/v1/knowledge/bases/" + _seg(kb_id) + "/documents/upload")

        # 上传不能重试：multipart 请求体是一次性的字节串，重试虽然
        # 能重发（这里是 bytes 不是流），但会对同一文件重复导入——
        # 服务端虽然去重，仍白白消耗一次解析。因此 retry=False。
        raw = self._transport.request(
            "POST", url, self._token,
            body=body,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"},
            timeout=None,
        )
        env = self._decode_envelope(raw)
        return ImportResult.from_dict(env.get("data") or {})


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _seg(s: str) -> str:
    """把路径片段做 URL 转义。

    必须转义：kb_id 可能含 `/`（虽然服务端限制为字母数字短横线
    下划线，但 SDK 不该依赖那个限制）——不转义会变成路径穿越，
    请求打到完全不同的端点。

    ## 为什么要在 quote 之后额外处理 `.`

    `urllib.parse.quote` 遵循 RFC 3986，把 `.` 视为 unreserved 字符
    而**不转义**。于是 `_seg("..")` 返回 `..`，拼出的路径是

        /api/v1/knowledge/bases/../documents

    服务端（或中间的 HTTP 库）做路径规范化时，这会退化成
    `/api/v1/knowledge/documents`——完全不同的端点。

    单个 `.` / `..` 这种**整段等于点号**的情况没有合法用途，直接
    拒绝而不是转义：`%2E%2E` 在部分服务端仍会被解码成 `..`，
    与其赌它不解码，不如让调用方立刻知道参数错了。
    """
    if s in (".", ".."):
        raise APIError(
            ErrInvalidRequest,
            message=f"路径片段不能是 {s!r}（会被路径规范化解释）",
        )
    return urllib.parse.quote(s, safe="")


def _jitter(seconds: float) -> float:
    """给等待时间加 ±30% 的随机抖动。

    为什么必须抖动：多个客户端同时收到 resource_limited 后若在同一
    时刻重试，会再次把服务端打满（惊群）。
    """
    if seconds <= 0:
        return 0.0
    return seconds * random.uniform(0.7, 1.3)


def _multipart(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
) -> bytes:
    """构造 multipart/form-data 请求体。

    手写而不是用 `email` 或 `requests`：标准库没有现成的 multipart
    编码器，而为一个上传功能引入依赖不值得。
    """
    parts: list[bytes] = []
    b = boundary.encode()

    for k, v in fields.items():
        if not v:
            # 空值不发：服务端把"字段缺失"当默认值，发空串语义不同
            continue
        parts.append(b"--" + b)
        parts.append(
            f'Content-Disposition: form-data; name="{k}"'.encode())
        parts.append(b"")
        parts.append(str(v).encode("utf-8"))

    parts.append(b"--" + b)
    # filename 用 RFC 2231 的 filename* 编码，避免中文名乱码
    disp = (f"Content-Disposition: form-data; name=\"{file_field}\"; "
            f"filename*=UTF-8''{urllib.parse.quote(filename)}")
    parts.append(disp.encode("utf-8"))
    parts.append(b"Content-Type: application/octet-stream")
    parts.append(b"")
    parts.append(file_bytes)

    parts.append(b"--" + b + b"--")
    parts.append(b"")

    return b"\r\n".join(parts)
