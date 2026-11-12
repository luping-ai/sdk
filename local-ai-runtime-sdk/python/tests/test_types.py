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
    ChatRequest,
    Client,
    Config,
    ErrInvalidRequest,
    ErrNotFound,
    ImportRequest,
    Message,
    SearchRequest,
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
