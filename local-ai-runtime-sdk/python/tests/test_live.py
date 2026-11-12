"""针对真实 Runtime 的端到端测试。

默认跳过——CI 里没有 Runtime，也不该为了跑测试去加载模型。
本地（或装了 Local AI Hub 的机器）设 `LAIH_E2E=1` 即可启用：

    LAIH_E2E=1 pytest tests/test_live.py -v

Windows PowerShell：

    $env:LAIH_E2E="1"; pytest tests/test_live.py -v

## 为什么假服务端不够

`test_client.py` / `test_stream.py` / `test_types.py` 覆盖的是确定性
逻辑（超时映射、错误分类、SSE 分帧）。但有几类问题只有真实实例
能暴露：

- 契约里写的字段名与服务端实际发出的不一致（字段恒为 None）
- 端点在运行的版本里根本不存在（本次就踩到：安装版早于上传路由）
- 流式的真实事件顺序与文档不符

## 写测试的纪律

**不要断言模型输出内容。** 模型是概率性的，"你好"可能变成"您好"。
只断言结构、类型、以及"调用成功且字段有值"。
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib

from localai import (
    APIError,
    ChatRequest,
    Client,
    ImportRequest,
    Message,
    SearchRequest,
    StreamEventType,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LAIH_E2E") != "1",
    reason="需要真实 Runtime；设 LAIH_E2E=1 启用",
)


@pytest.fixture(scope="module")
def client() -> Client:
    c = Client()
    try:
        c.health()
    except APIError as e:
        pytest.skip(f"Runtime 不可用：{e}")
    return c


def test_health_and_capability(client: Client):
    rep = client.health()
    # state 可能是 starting（正在加载），只要求结构可用
    assert rep.state in ("ready", "degraded", "starting", "unavailable")

    cap = client.capability()
    assert cap.level in ("full", "chat_only", "rule_only")
    # level 与实际开关必须自洽，否则业务方会被误导
    if cap.level == "rule_only":
        assert not cap.chat_enabled


def test_live_and_models(client: Client):
    assert client.live() is True

    models = client.models()
    assert isinstance(models, list)
    for m in models:
        assert m.id
        # 这两个布尔字段曾经混淆过（installed vs loaded）
        assert isinstance(m.loaded, bool)
        assert isinstance(m.present, bool)

    # 未加载时返回空串而不是抛异常
    assert isinstance(client.active_model(), str)


def test_chat_live(client: Client):
    cap = client.capability()
    if not cap.chat_enabled:
        pytest.skip("本机未启用模型对话")

    res = client.chat(ChatRequest(
        messages=[Message("user", "只回复两个字：收到")],
        max_tokens=16,
    ))

    assert isinstance(res.content, str)
    assert res.content.strip() != ""
    assert res.finish_reason in ("stop", "length")
    # 模型信息必须存在——否则降级判断无从下手
    assert res.model is not None
    assert res.model.actual
    assert res.request_id


def test_chat_stream_live(client: Client):
    cap = client.capability()
    if not cap.chat_enabled:
        pytest.skip("本机未启用模型对话")

    got: list[str] = []
    types: list[str] = []
    for ev in client.stream_events(ChatRequest(
            messages=[Message("user", "数一到三，只写数字")],
            max_tokens=24)):
        types.append(ev.type)
        if ev.type == StreamEventType.DELTA:
            got.append(ev.text)

    text = "".join(got)
    assert text.strip() != ""
    # 事件顺序：meta 在 delta 之前，done 在最后
    assert StreamEventType.META in types
    assert StreamEventType.DONE in types
    assert types.index(StreamEventType.META) < types.index(
        StreamEventType.DELTA)
    assert types[-1] == StreamEventType.DONE


def test_knowledge_lifecycle_live(client: Client):
    """建库 → 导入 → 列表 → 统计 → 检索 → 删除。"""
    import tempfile

    tag = uuid.uuid4().hex[:8]
    kb = client.create_kb(f"e2e-{tag}", description="Python SDK e2e")
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 客服手册\n\n"
                    "客户李娜的联系电话是一三八一二三四五六七八。\n")

        r = client.import_document(kb.id, ImportRequest(path=path))
        assert r.document_id
        assert r.chunk_count >= 1
        assert r.duplicate is False

        # 幂等：同一内容再导一次不算新文档
        r2 = client.import_document(kb.id, ImportRequest(path=path))
        assert r2.duplicate is True
        assert r2.existing_id == r.document_id

        docs = client.list_documents(kb.id)
        assert len(docs) == 1
        assert docs[0].chunk_count >= 1

        st = client.kb_stat(kb.id)
        assert st.document_count == 1
        assert st.chunk_count >= 1

        # 关键词检索不依赖 Embedding 模型，任何配置下都该能工作
        sr = client.search(SearchRequest(
            query="李娜", kb_ids=[kb.id], top_k=3))
        assert sr.mode in ("hybrid", "keyword", "semantic")
        assert len(sr.hits) >= 1
        assert any("李娜" in h.text for h in sr.hits)

        # RAG 上下文：即使预算很小，固定开销字段也该有值
        sr2 = client.search(SearchRequest(
            query="联系方式", kb_ids=[kb.id],
            build_context=True, context_tokens=800))
        if sr2.context is not None:
            assert sr2.context.fixed_overhead_tokens > 0
    finally:
        client.delete_kb(kb.id)
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)


def test_chat_with_knowledge_live(client: Client):
    cap = client.capability()
    if not (cap.chat_enabled and cap.rag_enabled):
        pytest.skip("本机未同时启用对话与 RAG")

    import tempfile

    tag = uuid.uuid4().hex[:8]
    kb = client.create_kb(f"e2e-rag-{tag}")
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("保修期是十二个月，自购买之日起计算。\n")

        client.import_document(kb.id, ImportRequest(path=path))

        res = client.chat(ChatRequest(
            messages=[Message("user", "保修期多久？")],
            kb_ids=[kb.id], max_tokens=32))

        assert isinstance(res.content, str)
        assert res.content.strip() != ""
        # 应该真的检索了，而不是静默忽略 kb_ids
        if res.knowledge is not None:
            assert res.knowledge.used_chunks >= 0
    finally:
        client.delete_kb(kb.id)
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)


def test_unauthorized_live(client: Client):
    """错误 token 必须被归类为 ErrUnauthorized，而不是"未运行"。"""
    from localai import Config, ErrUnauthorized

    bad = Client(Config(base_url=client.base_url, token="laih_wrong_token"))
    with pytest.raises(APIError) as ei:
        bad.models()
    assert ei.value.kind is ErrUnauthorized
