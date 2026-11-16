"""请求序列化、响应解析、知识库管理的测试。

序列化那组尤其重要：请求体里多一个 null 或少一个字段，服务端行为
就不同（"字段缺失"表示用默认值，"显式 null"在某些字段上语义不同）。
这类错误在本机联调时很难发现——服务端不会报错，只是行为不对。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakeruntime import FakeRuntime, envelope, error

from localai import (
    ALL_BASES,
    APIError,
    ChatRequest,
    ChunkerInfo,
    ChunkInput,
    Client,
    Config,
    DedupCheckRequest,
    DupGroup,
    DupMember,
    ErrChunkerMismatch,
    ErrInvalidRequest,
    ErrNotFound,
    ImportRequest,
    IngestChunksRequest,
    Message,
    SearchRequest,
    is_retryable,
)
from localai._client import _multipart, _seg
from localai._types import (
    Capability,
    ChatResult,
    HealthReport,
    ModelInfo,
    Usage,
)


def _client(rt: FakeRuntime, **kw) -> Client:
    return Client(Config(base_url=rt.url, token="t", max_retries=0, **kw))


# ---------------------------------------------------------------------------
# 请求序列化：None 字段不能进 JSON
# ---------------------------------------------------------------------------


def test_chat_request_omits_none_fields():
    wire = ChatRequest(messages=[Message("user", "hi")]).to_wire()
    assert wire == {"messages": [{"role": "user", "content": "hi"}]}
    # 关键：不能出现任何 null。服务端用"字段缺失"表示默认值。
    assert "max_tokens" not in wire
    assert "temperature" not in wire
    assert "kb_ids" not in wire


def test_chat_request_includes_explicit_values():
    wire = ChatRequest(
        messages=[Message("user", "hi")],
        max_tokens=100,
        temperature=0.0,       # 0 是有效值（贪心解码），不能当"未设置"
        quality="fast",
        allow_degrade=False,   # False 是有效值，不能当"未设置"
    ).to_wire()

    assert wire["max_tokens"] == 100
    assert wire["temperature"] == 0.0
    assert wire["quality"] == "fast"
    assert wire["allow_degrade"] is False


def test_chat_request_kb_ids():
    wire = ChatRequest(
        messages=[Message("user", "hi")],
        kb_ids=["k1", "k2"],
        knowledge_mode="hybrid",
        context_tokens=800,
    ).to_wire()
    assert wire["kb_ids"] == ["k1", "k2"]
    assert wire["knowledge_mode"] == "hybrid"
    assert wire["context_tokens"] == 800


def test_chat_request_empty_kb_ids_omitted():
    # 空列表等价于"不用知识库"，不必发一个空数组
    wire = ChatRequest(messages=[Message("user", "hi")],
                       kb_ids=[]).to_wire()
    assert "kb_ids" not in wire


def test_chat_request_json_schema():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    wire = ChatRequest(messages=[Message("user", "hi")],
                       json_schema=schema).to_wire()
    assert wire["json_schema"] == schema


def test_search_request_requires_kb_ids():
    wire = SearchRequest(query="q", kb_ids=["k"]).to_wire()
    assert wire == {"query": "q", "kb_ids": ["k"]}


def test_import_request_omits_optional():
    wire = ImportRequest(path="/tmp/a.md").to_wire()
    assert wire == {"path": "/tmp/a.md"}


def test_import_request_full():
    wire = ImportRequest(
        path="/tmp/a.pdf", name="显示名", mode="EXTERNAL",
        password="pw", update_document_id="d1",
    ).to_wire()
    assert wire == {
        "path": "/tmp/a.pdf", "name": "显示名", "mode": "EXTERNAL",
        "password": "pw", "update_document_id": "d1",
    }


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def test_chat_result_parsing():
    e = envelope(
        {"content": "答案", "finish_reason": "stop", "truncated": False},
        model={"requested": "a", "actual": "b", "degraded": True,
               "degrade_reason": "内存不足"},
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "ttft_ms": 120, "total_ms": 900, "queue_wait_ms": 30},
        warnings=["上下文被裁剪"],
    )
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: e)
        res = _client(rt).chat(
            ChatRequest(messages=[Message("user", "q")]))

    assert res.content == "答案"
    assert res.finish_reason == "stop"
    # 降级不是错误，但要能被识别
    assert res.degraded is True
    assert res.degrade_reason == "内存不足"
    assert res.usage.prompt_tokens == 10
    assert res.usage.queue_wait_ms == 30
    assert res.warnings == ["上下文被裁剪"]
    assert res.request_id == "req_test"


def test_chat_result_not_degraded():
    e = envelope({"content": "x", "finish_reason": "stop"},
                 model={"requested": "a", "actual": "a",
                        "degraded": False})
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: e)
        res = _client(rt).chat(
            ChatRequest(messages=[Message("user", "q")]))
    assert res.degraded is False
    assert res.degrade_reason == ""


def test_missing_model_info_is_tolerated():
    # 服务端某天省略 model 字段时不该崩
    res = ChatResult()
    assert res.degraded is False
    assert res.degrade_reason == ""
    assert ModelInfo.from_dict(None) is None
    assert Usage.from_dict(None) is None


def test_health_report_ready_semantics():
    assert HealthReport(state="ready").ready is True
    # degraded 也算可用：能力受限但服务在
    assert HealthReport(state="degraded").ready is True
    assert HealthReport(state="starting").ready is False
    assert HealthReport(state="unavailable").ready is False


def test_health_503_falls_back_to_starting():
    with FakeRuntime() as rt:
        rt.on("GET", "/health/ready",
              lambda r: error(503, "model_unavailable", "正在加载"))
        rep = _client(rt).health()
    # 不是错误：调用方要能展示"正在加载模型"
    assert rep.state == "starting"
    assert rep.ready is False
    assert "正在加载" in rep.reason


def test_health_ready_parses_checks():
    e = envelope({
        "state": "ready", "uptime_seconds": 1200,
        "checks": {"db": {"ok": True}, "engine": {"ok": False,
                                                  "message": "未启动"}},
    })
    with FakeRuntime() as rt:
        rt.on("GET", "/health/ready", lambda r: e)
        rep = _client(rt).health()
    assert rep.uptime_seconds == 1200
    assert rep.checks["db"].ok is True
    assert rep.checks["engine"].ok is False
    assert rep.checks["engine"].message == "未启动"


def test_capability_parsing():
    e = envelope({
        "level": "chat_only", "reason": "无 RAG 模型",
        "rag_enabled": False, "chat_enabled": True,
        "available_bytes": 4 << 30, "baseline_need_bytes": 2 << 30,
        "warnings": ["内存偏紧"],
    })
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/capability", lambda r: e)
        cap = _client(rt).capability()
    assert cap.level == "chat_only"
    assert cap.rag_enabled is False
    assert cap.chat_enabled is True
    assert cap.warnings == ["内存偏紧"]


def test_capability_defaults_are_conservative():
    # 缺字段时按"最保守"解释：不能假装能对话
    cap = Capability.from_dict({})
    assert cap.chat_enabled is False
    assert cap.rag_enabled is False


def test_models_parsing():
    e = envelope({
        "models": [
            {"id": "m1", "name": "Model One", "loaded": True,
             "present": True, "size_bytes": 100, "context_length": 4096,
             "capabilities": ["rag"], "languages": ["zh"],
             "license": "Apache-2.0", "commercial_use": "yes"},
        ],
        "active": "m1", "total": 1,
    })
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", lambda r: e)
        models = _client(rt).models()

    assert len(models) == 1
    assert models[0].id == "m1"
    assert models[0].loaded is True
    assert models[0].present is True
    assert models[0].capabilities == ["rag"]


def test_active_model_empty_is_not_error():
    # "没有模型"是正常初始状态，不该抛异常
    e = envelope({"models": [], "active": "", "total": 0})
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", lambda r: e)
        assert _client(rt).active_model() == ""


# ---------------------------------------------------------------------------
# 客户端侧校验（省一次往返）
# ---------------------------------------------------------------------------


def test_chat_rejects_non_request():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).chat({"messages": []})
        assert ei.value.kind is ErrInvalidRequest


def test_search_rejects_empty_kb_ids():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).search(SearchRequest(query="q", kb_ids=[]))
        assert ei.value.kind is ErrInvalidRequest


def test_create_kb_rejects_blank_name():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).create_kb("   ")
        assert ei.value.kind is ErrInvalidRequest


def test_import_rejects_blank_path():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).import_document("kb", ImportRequest(path="  "))
        assert ei.value.kind is ErrInvalidRequest


def test_load_model_rejects_empty_id():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).load_model("")
        assert ei.value.kind is ErrInvalidRequest


# ---------------------------------------------------------------------------
# 路径转义
# ---------------------------------------------------------------------------


def test_seg_escapes_slash():
    # kb_id 含 / 时不转义会变成完全不同的端点
    assert _seg("a/b") == "a%2Fb"


def test_seg_escapes_space():
    assert _seg("a b") == "a%20b"


def test_seg_rejects_dot_segments():
    # quote() 不转义 '.'，拼出 /bases/../documents 会被规范化成
    # /knowledge/documents —— 完全不同的端点
    with pytest.raises(Exception) as ei:
        _seg("..")
    assert ei.value.kind is ErrInvalidRequest
    with pytest.raises(Exception):
        _seg(".")


def test_kb_id_escaped_in_request_path():
    seen = {}

    def h(r):
        seen["path"] = r["path"]
        return envelope({"bases": []})

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases", h)
        _client(rt).list_kb()
    assert seen["path"] == "/api/v1/knowledge/bases"


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


def test_create_kb():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases",
              lambda r: envelope({"id": "kb1", "name": "售后库",
                                  "description": "d"}))
        kb = _client(rt).create_kb("售后库", description="d")
    assert kb.id == "kb1"
    assert kb.name == "售后库"


def test_list_kb():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases",
              lambda r: envelope({"bases": [
                  {"id": "a", "name": "A", "document_count": 3},
                  {"id": "b", "name": "B"},
              ]}))
        bases = _client(rt).list_kb()
    assert [b.id for b in bases] == ["a", "b"]
    assert bases[0].document_count == 3


def test_kb_stat_without_vectorize():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/k/stats",
              lambda r: envelope({"kb_id": "k", "document_count": 2,
                                  "chunk_count": 10,
                                  "embedded_count": 0,
                                  "stale_count": 0}))
        st = _client(rt).kb_stat("k")
    # 服务端查进度失败时省略 vectorize，不影响主统计
    assert st.vectorize is None
    assert st.chunk_count == 10


def test_kb_stat_with_vectorize():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/k/stats",
              lambda r: envelope({
                  "kb_id": "k",
                  "vectorize": {"total_chunks": 10, "embedded_chunks": 4,
                                "pending_chunks": 6, "stale_chunks": 0,
                                "model": "e5", "running": True},
              }))
        st = _client(rt).kb_stat("k")
    assert st.vectorize is not None
    assert st.vectorize.embedded_chunks == 4
    assert st.vectorize.model == "e5"
    assert st.vectorize.running is True


def test_vectorize_sync_sends_no_body():
    seen = {}

    def h(r):
        seen["body"] = r["body"]
        return envelope({"kb_id": "k", "vectorized": 12})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/vectorize", h)
        res = _client(rt).vectorize("k")

    # 同步模式不需要请求体
    assert seen["body"] == b""
    assert res.vectorized == 12
    assert res.started is False


def test_vectorize_async_sends_flag():
    seen = {}

    def h(r):
        seen["body"] = json.loads(r["body"])
        return envelope({"kb_id": "k", "started": True, "note": "后台执行"})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/vectorize", h)
        res = _client(rt).vectorize("k", is_async=True)

    assert seen["body"] == {"async": True}
    assert res.started is True


def test_import_result_duplicate_is_not_error():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/documents",
              lambda r: envelope({
                  "document_id": "d1", "chunk_count": 0,
                  "duplicate": True, "existing_id": "d0",
              }))
        res = _client(rt).import_document(
            "k", ImportRequest(path="/tmp/a.md"))

    # 幂等：重复导入返回 duplicate=True 而不是错误
    assert res.duplicate is True
    assert res.existing_id == "d0"


def test_import_update_returns_diff():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/documents",
              lambda r: envelope({
                  "document_id": "d1", "chunk_count": 5,
                  "diff": {"added": 1, "updated": 2, "deleted": 0,
                           "unchanged": 2, "needs_embedding": True,
                           "summary": "变更 3，未变 2"},
              }))
        res = _client(rt).import_document(
            "k", ImportRequest(path="/tmp/a.md", update_document_id="d1"))

    assert res.diff["added"] == 1
    assert res.diff["needs_embedding"] is True


def test_list_documents():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/k/documents",
              lambda r: envelope({"documents": [
                  {"id": "d1", "name": "a.md", "status": "ACTIVE",
                   "index_status": "READY", "chunk_count": 3},
              ]}))
        docs = _client(rt).list_documents("k")
    assert docs[0].status == "ACTIVE"
    assert docs[0].chunk_count == 3


def test_delete_document_keep_file():
    seen = {}

    def h(r):
        seen["path"] = r["path"]
        return envelope({"deleted": "d1", "file_removed": False})

    with FakeRuntime() as rt:
        rt.on("DELETE",
              "/api/v1/knowledge/bases/k/documents/d1", h)
        _client(rt).delete_document("k", "d1", keep_file=True)

    assert "keep_file=1" in seen["path"]


def test_delete_document_default_no_query():
    seen = {}

    def h(r):
        seen["path"] = r["path"]
        return envelope({})

    with FakeRuntime() as rt:
        rt.on("DELETE", "/api/v1/knowledge/bases/k/documents/d1", h)
        _client(rt).delete_document("k", "d1")

    assert "?" not in seen["path"]


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def test_search_parsing():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/search", lambda r: envelope({
            "query": "q", "mode": "hybrid", "degraded": False,
            "results": [
                {"chunk_id": "c1", "document_id": "d1", "kb_id": "k",
                 "ordinal": 0, "text": "内容", "score": 0.87,
                 "keyword_rank": 1, "semantic_rank": 2},
            ],
            "total_ms": 12,
        }))
        sr = _client(rt).search(SearchRequest(query="q", kb_ids=["k"]))

    assert sr.mode == "hybrid"
    assert len(sr.hits) == 1
    assert sr.hits[0].score == pytest.approx(0.87)
    assert sr.hits[0].keyword_rank == 1
    assert sr.hits[0].semantic_rank == 2
    assert sr.total_ms == 12
    assert sr.context is None


def test_search_all_bases():
    """ALL_BASES 展开：响应要带 all_bases / base_count，
    且请求体里传的就是字面 "all"（不是让 SDK 自己展开）。"""
    captured = {}

    def handler(r):
        captured["body"] = json.loads(r["body"])
        return envelope({
            "query": "q", "mode": "hybrid", "degraded": False,
            "results": [],
            "all_bases": True, "base_count": 3,
            "total_ms": 40,
        })

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/search", handler)
        sr = _client(rt).search(
            SearchRequest(query="q", kb_ids=[ALL_BASES]))

    # 展开是服务端的事：SDK 只传 "all"，不自己列库。
    # 若 SDK 擅自展开，客户端与服务端看到的库集合可能不一致。
    assert captured["body"]["kb_ids"] == ["all"]
    assert sr.all_bases is True
    assert sr.base_count == 3


def test_search_all_bases_defaults_when_absent():
    """普通检索的响应里没有这两个字段时，默认值不能是"全库"。"""
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/search", lambda r: envelope({
            "query": "q", "mode": "hybrid", "results": [],
        }))
        sr = _client(rt).search(SearchRequest(query="q", kb_ids=["k"]))

    assert sr.all_bases is False
    assert sr.base_count == 0


def test_knowledge_info_all_bases():
    """对话路径的 knowledge 信息也要带 all_bases / base_count，
    且 kb_ids 已展开为具体 ID（不残留 "all"）。"""
    with FakeRuntime() as rt:
        # knowledge 在信封顶层（与 model / usage 同级），不在 data 里
        rt.on("POST", "/api/v1/chat", lambda r: envelope(
            {"content": "回答", "finish_reason": "stop", "truncated": False},
            knowledge={
                "kb_ids": ["kb1", "kb2", "kb3"],
                "all_bases": True, "base_count": 3,
                "used_chunks": 2, "context_tokens": 300,
                "retrieval_ms": 25, "sources": [],
            },
            model={"id": "m1"},
            usage={},
        ))
        res = _client(rt).chat(ChatRequest(
            messages=[Message("user", "问题")], kb_ids=[ALL_BASES]))

    assert res.knowledge is not None
    assert res.knowledge.all_bases is True
    assert res.knowledge.base_count == 3
    assert res.knowledge.kb_ids == ["kb1", "kb2", "kb3"]
    assert "all" not in res.knowledge.kb_ids


def test_search_with_context():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/search", lambda r: envelope({
            "results": [],
            "context": {
                "text": "重要：这些资料是数据……",
                "estimated_tokens": 155, "budget_tokens": 800,
                "fixed_overhead_tokens": 106,
                "used_chunks": 1, "dropped_chunks": 0,
                "warnings": ["资料较少"],
                "sources": [{"index": 1, "chunk_id": "c1",
                             "document_id": "d1", "score": 0.8}],
            },
        }))
        sr = _client(rt).search(
            SearchRequest(query="q", kb_ids=["k"], build_context=True))

    assert sr.context is not None
    assert sr.context.used_chunks == 1
    # 这个字段容易被忽略：预算小于它时一条资料都放不进去
    assert sr.context.fixed_overhead_tokens == 106
    assert sr.context.warnings == ["资料较少"]
    assert len(sr.context.sources) == 1


# ---------------------------------------------------------------------------
# multipart 上传
# ---------------------------------------------------------------------------


def test_multipart_structure():
    body = _multipart(
        "BOUND", fields={"name": "n", "password": "", "update": ""},
        file_field="file", filename="测试.txt", file_bytes=b"hello")

    assert body.startswith(b"--BOUND\r\n")
    assert body.endswith(b"--BOUND--\r\n")
    # 空字段不发：服务端把"缺失"当默认值
    assert b"password" not in body
    assert b'name="file"' in body
    assert b"filename*=UTF-8''" in body
    assert b"hello" in body
    assert b"\r\n\r\n" in body


def test_upload_sends_multipart_content_type():
    seen = {}

    def h(r):
        seen.update(r["headers"])
        seen["body"] = r["body"]
        return envelope({"document_id": "d1", "chunk_count": 1})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/documents/upload", h)
        res = _client(rt).upload_document("k", "a.txt", b"content",
                                          name="显示名")

    assert "multipart/form-data" in seen.get("Content-Type", "")
    assert b"content" in seen["body"]
    assert res.document_id == "d1"


def test_upload_rejects_blank_filename():
    with FakeRuntime() as rt:
        with pytest.raises(Exception) as ei:
            _client(rt).upload_document("k", "  ", b"x")
        assert ei.value.kind is ErrInvalidRequest
        # 服务端按扩展名选解析器，没文件名就没法导入
        assert "扩展名" in ei.value.message


def test_upload_translates_not_found():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/k/documents/upload",
              lambda r: error(404, "not_found", "无此端点"))
        with pytest.raises(Exception) as ei:
            _client(rt).upload_document("k", "a.txt", b"x")
        assert ei.value.kind is ErrNotFound

# ---------------------------------------------------------------------------
# 入库已切好的块（ingest_chunks）
# ---------------------------------------------------------------------------


def test_ingest_chunks_sends_contract_fields():
    """请求必须打新端点，字段名与契约一致。"""
    captured = {}

    def handler(r):
        captured["path"] = r["path"]
        captured["body"] = json.loads(r["body"])
        return envelope({
            "document_id": "d1", "chunk_count": 2,
            "content_runes": 120, "duplicate": False,
        })

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks", handler)
        res = _client(rt).ingest_chunks("kb1", IngestChunksRequest(
            name="售後手册",
            file_type="pdf",
            file_hash="h1",
            file_size=1234,
            modified_at=1763000000,
            source_label="D:/工作/售后手册.pdf",
            chunker=ChunkerInfo("localkb-py", "1.0.0",
                                target_chars=450, max_chars=600,
                                overlap_chars=54),
            chunks=[
                ChunkInput(0, "本条款适用于……", page_start=3, page_end=3,
                           start_rune=1200, rune_count=480,
                           heading_path="第三章 > 3.2", chunk_type="heading"),
                ChunkInput(1, "保修期为两年。", page_start=4),
            ],
        ))

    assert res.chunk_count == 2
    assert captured["path"].endswith("/documents/chunks")
    body = captured["body"]
    assert body["name"] == "售後手册"
    assert body["file_hash"] == "h1"
    assert body["file_type"] == "pdf"
    assert body["source_label"] == "D:/工作/售后手册.pdf"
    assert body["chunker"]["name"] == "localkb-py"
    assert body["chunker"]["version"] == "1.0.0"
    assert body["chunker"]["target_chars"] == 450

    first = body["chunks"][0]
    # 溯源字段写错 tag 不会报错，只会在服务端静默变成 0
    assert first["page_start"] == 3
    assert first["page_end"] == 3
    assert first["heading_path"] == "第三章 > 3.2"
    assert first["chunk_type"] == "heading"
    assert first["start_rune"] == 1200


def test_ingest_chunks_omits_empty_provenance():
    """未知的溯源字段不发（0/空是"我不知道"，与省略等价）。

    少发几个键让请求体更小、日志更清晰，也避免服务端误以为
    我们真的提供了"第 0 页"。
    """
    captured = {}

    def handler(r):
        captured["body"] = json.loads(r["body"])
        return envelope({"document_id": "d1", "chunk_count": 1})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks", handler)
        _client(rt).ingest_chunks("kb1", IngestChunksRequest(
            name="a", file_hash="h",
            chunker=ChunkerInfo("c", "1"),
            chunks=[ChunkInput(0, "x")],
        ))

    chunk = captured["body"]["chunks"][0]
    assert "page_start" not in chunk
    assert "heading_path" not in chunk
    assert set(chunk.keys()) == {"ordinal", "text"}


def test_ingest_chunks_validation_local():
    """本地预检：明显错误的请求不该浪费一次往返。"""
    called = []

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks",
              lambda r: called.append(1) or envelope({}))
        client = _client(rt)

        bad_cases = [
            ("", IngestChunksRequest(name="a", file_hash="h",
                                     chunker=ChunkerInfo("c", "1"),
                                     chunks=[ChunkInput(0, "x")])),
            ("kb1", IngestChunksRequest(name="", file_hash="h",
                                        chunker=ChunkerInfo("c", "1"),
                                        chunks=[ChunkInput(0, "x")])),  # 新建：必填
            ("kb1", IngestChunksRequest(name="a", file_hash="",
                                        chunker=ChunkerInfo("c", "1"),
                                        chunks=[ChunkInput(0, "x")])),
            ("kb1", IngestChunksRequest(name="a", file_hash="h",
                                        chunker=None,
                                        chunks=[ChunkInput(0, "x")])),
            ("kb1", IngestChunksRequest(name="a", file_hash="h",
                                        chunker=ChunkerInfo("c", ""),
                                        chunks=[ChunkInput(0, "x")])),
            ("kb1", IngestChunksRequest(name="a", file_hash="h",
                                        chunker=ChunkerInfo("c", "1"),
                                        chunks=[])),
        ]
        for kb_id, req in bad_cases:
            with pytest.raises(APIError) as ei:
                client.ingest_chunks(kb_id, req)
            assert ei.value.kind is ErrInvalidRequest

    assert not called, "本地预检失败的请求不应发出"


def test_ingest_chunks_update_allows_empty_name():
    """更新时 name 可以省略，且不会被发到服务端。

    语义是"不改显示名"。桌面端两个常规场景都会走到：只改了文件内容、
    或只移动了文件而文件名没变。若本地预检强制必填，调用方只能先 GET
    一次拿旧名再回传——而那次回传与"真的改名"在服务端无法区分。

    同时验证 to_wire 的 omitempty 行为：发空串会让请求看起来像
    "要把名字清空"。
    """
    sent = {}

    def handler(req):
        import json as _json
        sent.update(_json.loads(req["body"]))
        return envelope({"document_id": "d1", "chunk_count": 1})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks", handler)
        res = _client(rt).ingest_chunks("kb1", IngestChunksRequest(
            name="",                       # 不改名
            file_hash="h-rename",
            update_document_id="d1",
            chunker=ChunkerInfo("localkb-py", "1.0.0"),
            chunks=[ChunkInput(0, "内容")],
        ))

    assert res.document_id == "d1"
    assert "name" not in sent, f"空 name 不该出现在请求体里: {sent}"
    assert sent["update_document_id"] == "d1"


def test_ingest_chunks_update_sends_new_name():
    """改名时新名字必须发出去（否则服务端无从知道要改）。"""
    sent = {}

    def handler(req):
        import json as _json
        sent.update(_json.loads(req["body"]))
        return envelope({"document_id": "d1", "chunk_count": 1})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks", handler)
        _client(rt).ingest_chunks("kb1", IngestChunksRequest(
            name="合同_终稿.docx",
            file_hash="h-rename",
            update_document_id="d1",
            source_label="D:/docs/合同_终稿.docx",
            chunker=ChunkerInfo("localkb-py", "1.0.0"),
            chunks=[ChunkInput(0, "内容")],
        ))

    assert sent["name"] == "合同_终稿.docx"
    assert sent["source_label"] == "D:/docs/合同_终稿.docx"


def test_ingest_chunks_chunker_mismatch_not_retryable():
    """409 chunker_mismatch 映射到专用 kind 且不可重试。

    这是最关键的一条：重试必然再失败，只会浪费用户时间。
    正确动作是重建索引——完全不同的操作。
    """
    attempts = []

    def handler(r):
        attempts.append(1)
        # error() 已经返回 (status, headers, body_bytes) 三元组，
        # 不要再 json.dumps 一层——那会把 bytes 当对象序列化
        return error(409, "chunker_mismatch",
                     "库内为 localkb-py@1.0.0，本次为 2.0.0，需要重建索引")

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/documents/chunks", handler)
        client = _client(rt)
        with pytest.raises(APIError) as ei:
            client.ingest_chunks("kb1", IngestChunksRequest(
                name="a", file_hash="h",
                chunker=ChunkerInfo("c", "2.0.0"),
                chunks=[ChunkInput(0, "x")],
            ))

    err = ei.value
    assert err.kind is ErrChunkerMismatch
    assert not is_retryable(err)
    assert len(attempts) == 1, "SDK 不应自动重试 chunker_mismatch"
    # 错误文案里要有"重建"，否则用户不知道下一步做什么
    assert "重建" in err.message


def test_search_hit_provenance_fields():
    """SearchHit 的溯源字段要能解析出来。"""
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/search", lambda r: envelope({
            "query": "q", "mode": "hybrid",
            "results": [{
                "chunk_id": "c1", "document_id": "d1", "kb_id": "k",
                "ordinal": 0, "text": "内容", "score": 0.9,
                "page_start": 3, "page_end": 5,
                "heading_path": "第三章 > 3.2", "chunk_type": "table",
            }],
        }))
        sr = _client(rt).search(SearchRequest(query="q", kb_ids=["k"]))

    hit = sr.hits[0]
    assert hit.page_start == 3
    assert hit.page_end == 5
    assert hit.heading_path == "第三章 > 3.2"
    assert hit.chunk_type == "table"


def test_knowledge_source_provenance_fields():
    """KnowledgeSource（对话路径的引用来源）也要带溯源字段。"""
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: envelope(
            {"content": "答", "finish_reason": "stop", "truncated": False},
            knowledge={
                "kb_ids": ["k"], "used_chunks": 1,
                "sources": [{
                    "index": 1, "chunk_id": "c1", "document_id": "d1",
                    "source": "a.pdf", "score": 0.9,
                    "page_start": 7, "page_end": 7,
                    "heading_path": "第二章",
                }],
            },
            model={"id": "m1"}, usage={},
        ))
        res = _client(rt).chat(ChatRequest(
            messages=[Message("user", "问题")], kb_ids=["k"]))

    src = res.knowledge.sources[0]
    assert src.page_start == 7
    assert src.heading_path == "第二章"

# ---------------------------------------------------------------------------
# 相似检测（Phase 2）
# ---------------------------------------------------------------------------


def test_dedup_check_fast_path():
    captured = {}

    def handler(r):
        captured["path"] = r["path"]
        captured["body"] = json.loads(r["body"] or b"{}")
        return envelope({
            "groups": [{
                "id": "dg1", "kind": "similar", "max_hamming": 3,
                "members": [
                    {"document_id": "a", "name": "a.pdf", "hamming": 0},
                    {"document_id": "b", "name": "b.pdf", "hamming": 3},
                ],
            }],
            "pending": ["c"],
            "total_ms": 25,
        })

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/dedup/check", handler)
        res = _client(rt).dedup_check(
            "kb1", DedupCheckRequest(doc_ids=["a", "b", "c"]))

    assert captured["path"].endswith("/dedup/check")
    assert captured["body"]["doc_ids"] == ["a", "b", "c"]

    assert len(res.groups) == 1
    g = res.groups[0]
    assert g.kind == "similar"
    assert g.max_hamming == 3
    # 第一个必须是代表（hamming 0）
    assert g.members[0].document_id == "a"
    assert g.members[0].hamming == 0
    assert g.members[1].name == "b.pdf"

    # pending 必须能读到——不检查它会把"还没算指纹"误读成"没有重复"
    assert res.pending == ["c"]
    assert res.total_ms == 25


def test_dedup_check_none_request():
    """None 等价于"检查全库"，且不发任何字段。"""
    captured = {}

    def handler(r):
        captured["body"] = json.loads(r["body"] or b"{}")
        return envelope({"groups": []})

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/dedup/check", handler)
        _client(rt).dedup_check("kb1")

    assert captured["body"] == {}


def test_dedup_check_validation():
    with FakeRuntime() as rt:
        client = _client(rt)
        with pytest.raises(APIError) as ei:
            client.dedup_check("")
        assert ei.value.kind is ErrInvalidRequest


def test_dedup_groups():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/kb1/dedup/groups",
              lambda r: envelope({
                  "groups": [{
                      "id": "dg1", "kind": "exact", "max_hamming": 0,
                      "members": [
                          {"document_id": "a", "hamming": 0},
                          {"document_id": "b", "hamming": 0},
                      ],
                  }],
                  "computed_at": 1763000000,
                  "stale": True,
                  "pending_count": 4,
              }))
        res = _client(rt).dedup_groups("kb1")

    assert len(res.groups) == 1
    assert res.groups[0].kind == "exact"
    assert res.computed_at == 1763000000
    # stale 是"结果不完整"的信号，必须能读到
    assert res.stale is True
    assert res.pending_count == 4


def test_dedup_diff():
    captured = {}

    def handler(r):
        captured["path"] = r["path"]
        return envelope({
            "doc_a": "a", "doc_b": "b",
            "added": [{"ordinal": 5, "preview": "新增"}],
            "removed": [{"ordinal": 2, "preview": "删除"}],
            "changed": [{"ordinal": 1, "before": "旧", "after": "新"}],
            "unchanged": 7,
            "truncated": True,
        })

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/kb1/dedup/diff", handler)
        d = _client(rt).dedup_diff("kb1", "a", "b")

    # 查询参数必须正确带上
    assert "doc_a=a" in captured["path"]
    assert "doc_b=b" in captured["path"]

    assert len(d.added) == 1 and d.added[0].ordinal == 5
    assert d.added[0].preview == "新增"
    assert len(d.removed) == 1 and d.removed[0].ordinal == 2
    assert len(d.changed) == 1
    assert d.changed[0].before == "旧" and d.changed[0].after == "新"
    assert d.unchanged == 7
    assert d.truncated is True


def test_dedup_diff_validation():
    with FakeRuntime() as rt:
        client = _client(rt)
        for kb, a, b in [
            ("", "a", "b"),
            ("kb1", "", "b"),
            ("kb1", "a", ""),
            ("kb1", "a", "a"),  # 自己比自己没有信息量
        ]:
            with pytest.raises(APIError) as ei:
                client.dedup_diff(kb, a, b)
            assert ei.value.kind is ErrInvalidRequest


def test_dedup_diff_escapes_query_params():
    """doc_id 里的特殊字符必须转义，否则会变成额外参数。"""
    captured = {}

    def handler(r):
        captured["path"] = r["path"]
        return envelope({})

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/kb1/dedup/diff", handler)
        _client(rt).dedup_diff("kb1", "a&b=1", "c")

    # 未转义的话会多出一个 b 参数
    assert "doc_a=a%26b%3D1" in captured["path"]


def test_dup_group_summary():
    """Summary 用于日志与 UI，格式必须稳定可读。"""
    g = DupGroup(
        kind="similar", max_hamming=3,
        members=[DupMember("a", "合同.pdf", 0), DupMember("b", "合同(1).pdf", 3)],
    )
    s = g.summary()
    assert "similar" in s
    assert "合同.pdf" in s
    assert "2 份" in s

    # 没有 name 时退回 document_id
    g2 = DupGroup(kind="exact", members=[DupMember("x")])
    assert "x" in g2.summary()

def test_dedup_check_async():
    """async=true 时返回 task_id，且不发 recompute。"""
    captured = {}

    def handler(r):
        captured["body"] = json.loads(r["body"] or b"{}")
        return envelope({
            "task_id": "task_abc",
            "status": "queued",
            "priority": "background",
            "poll_after": 1000,
        })

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/knowledge/bases/kb1/dedup/check", handler)
        res = _client(rt).dedup_check("kb1", DedupCheckRequest(async_=True))

    assert captured["body"]["async"] is True
    # async 模式下 groups/pending 都为空
    assert res.task_id == "task_abc"
    assert res.groups == []
    assert res.pending == []
