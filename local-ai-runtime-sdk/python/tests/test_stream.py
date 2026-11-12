"""SSE 流式解析的测试。

这一组是 Python SDK 最重要的测试——流式是生成器完全覆盖不了、
必须手写的部分，也是最容易在边界上出错的地方。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakeruntime import (
    FakeRuntime,
    error,
    sse,
    sse_frame,
)

from localai import (
    ChatRequest,
    Client,
    Config,
    ErrQueueFull,
    Message,
    StreamEventType,
)
from localai._stream import decode_event, parse_sse


def _client(rt: FakeRuntime, **kw) -> Client:
    return Client(Config(base_url=rt.url, token="t", max_retries=0, **kw))


def _req(text: str = "hi", **kw) -> ChatRequest:
    return ChatRequest(messages=[Message("user", text)], **kw)


# ---------------------------------------------------------------------------
# 纯解析层
# ---------------------------------------------------------------------------


def test_parse_sse_basic():
    frames = list(parse_sse(iter([
        "data: a\n", "\n",
        "data: b\n", "\n",
    ])))
    assert frames == ["a", "b"]


def test_parse_sse_skips_heartbeat():
    # 以 : 开头的是注释（心跳），不是事件
    frames = list(parse_sse(iter([
        ": ping\n", "\n",
        "data: a\n", "\n",
    ])))
    assert frames == ["a"]


def test_parse_sse_multiline_data():
    # 规范的 SSE 允许一帧多行 data:，应按 \n 拼接成一条
    frames = list(parse_sse(iter([
        'data: {"event":\n', 'data: "delta"}\n', "\n",
    ])))
    assert frames == ['{"event":\n"delta"}']


def test_parse_sse_data_without_space():
    frames = list(parse_sse(iter(["data:x\n", "\n"])))
    assert frames == ["x"]


def test_parse_sse_data_with_space():
    frames = list(parse_sse(iter(["data: x\n", "\n"])))
    assert frames == ["x"]


def test_parse_sse_ignores_other_fields():
    # event: / id: / retry: 与本项目无关（事件类型在 JSON 的 event 字段里）
    frames = list(parse_sse(iter([
        "event: delta\n", "id: 1\n", "retry: 100\n", "data: x\n", "\n",
    ])))
    assert frames == ["x"]


def test_parse_sse_flushes_trailing_frame():
    # 服务端异常断连时最后一帧可能没有空行收尾，丢掉等于丢数据
    frames = list(parse_sse(iter(["data: x\n"])))
    assert frames == ["x"]


def test_parse_sse_crlf():
    frames = list(parse_sse(iter(["data: x\r\n", "\r\n"])))
    assert frames == ["x"]


# ---------------------------------------------------------------------------
# 事件解码
# ---------------------------------------------------------------------------


def test_decode_delta():
    ev = decode_event('{"event":"delta","data":{"text":"你好"}}')
    assert ev.type == StreamEventType.DELTA
    assert ev.text == "你好"


def test_decode_meta():
    ev = decode_event(
        '{"event":"meta","data":{"request_id":"r1","model":"m",'
        '"degraded":true,"stream":true}}')
    assert ev.type == StreamEventType.META
    assert ev.request_id == "r1"
    assert ev.model == "m"
    assert ev.degraded is True


def test_decode_queued():
    ev = decode_event(
        '{"event":"queued","data":{"ahead":3,"ahead_same_priority":1,'
        '"priority":"interactive","estimated_wait_ms":1500}}')
    assert ev.ahead == 3
    assert ev.ahead_same_priority == 1
    assert ev.priority == "interactive"
    assert ev.estimated_wait_ms == 1500


def test_decode_usage():
    ev = decode_event(
        '{"event":"usage","data":{"prompt_tokens":10,'
        '"completion_tokens":5,"ttft_ms":100,"total_ms":900}}')
    assert ev.type == StreamEventType.USAGE
    assert ev.usage["prompt_tokens"] == 10


def test_decode_done():
    ev = decode_event('{"event":"done","data":{"finish_reason":"length"}}')
    assert ev.finish_reason == "length"


def test_decode_warning():
    ev = decode_event(
        '{"event":"warning","data":{"warnings":["上下文被裁剪","无检索结果"]}}')
    assert ev.warnings == ["上下文被裁剪", "无检索结果"]
    # 同时合并进 text，方便"只想知道有什么提示"的调用方
    assert ev.text == "上下文被裁剪；无检索结果"


def test_decode_error_event():
    ev = decode_event(
        '{"event":"error","data":{"code":"queue_full","message":"满了"}}')
    assert ev.type == StreamEventType.ERROR
    assert ev.error is not None
    assert ev.error.kind is ErrQueueFull
    assert ev.error.code == "queue_full"


def test_decode_malformed_returns_none():
    assert decode_event("not json") is None
    assert decode_event("[1,2,3]") is None


def test_decode_unknown_event_preserved():
    # 服务端将来加新事件时，旧客户端至少不会崩，且能看见它
    ev = decode_event('{"event":"future_thing","data":{}}')
    assert ev.type == "future_thing"


# ---------------------------------------------------------------------------
# 端到端流式
# ---------------------------------------------------------------------------


def _stream_body(*chunks: str, finish: str = "stop") -> str:
    parts = [sse_frame("queued", {"ahead": 0, "priority": "normal"})]
    parts.append(sse_frame("meta", {
        "request_id": "req_s", "model": "m1", "degraded": False,
        "stream": True}))
    for c in chunks:
        parts.append(sse_frame("delta", {"text": c}))
    parts.append(sse_frame("usage", {"prompt_tokens": 5,
                                     "completion_tokens": len(chunks)}))
    parts.append(sse_frame("done", {"finish_reason": finish}))
    parts.append("data: [DONE]\n\n")
    return "".join(parts)


def test_chat_stream_yields_text():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: sse(_stream_body("你", "好", "呀")))
        got = "".join(_client(rt).chat_stream(_req()))
    assert got == "你好呀"


def test_stream_events_yields_all():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: sse(_stream_body("a", "b")))
        types = [e.type for e in _client(rt).stream_events(_req())]

    assert types[0] == StreamEventType.QUEUED
    assert StreamEventType.META in types
    assert types.count(StreamEventType.DELTA) == 2
    assert StreamEventType.USAGE in types
    assert types[-1] == StreamEventType.DONE


def test_stream_skips_heartbeats():
    body = (": ping\n\n" + sse_frame("delta", {"text": "a"})
            + ": ping\n\n" + sse_frame("delta", {"text": "b"})
            + "data: [DONE]\n\n")
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        got = "".join(_client(rt).chat_stream(_req()))
    assert got == "ab"


def test_stream_survives_malformed_frame():
    # 单帧坏掉不该中断整条流：用户至少能看到部分输出
    body = (sse_frame("delta", {"text": "a"})
            + "data: {broken json\n\n"
            + sse_frame("delta", {"text": "b"})
            + "data: [DONE]\n\n")
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        got = "".join(_client(rt).chat_stream(_req()))
    assert got == "ab"


def test_stream_error_event_raises():
    body = (sse_frame("delta", {"text": "a"})
            + sse_frame("error", {"code": "queue_full", "message": "满了"}))
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        with pytest.raises(Exception) as ei:
            list(_client(rt).chat_stream(_req()))
    assert ei.value.kind is ErrQueueFull


def test_stream_http_error_before_sse():
    # 队列满在发出 SSE 头之前判断——一旦发头状态码就固化为 200
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: error(429, "queue_full", "队列满", retryable=True))
        with pytest.raises(Exception) as ei:
            list(_client(rt).chat_stream(_req()))
    assert ei.value.kind is ErrQueueFull


def test_stream_missing_done_still_completes():
    body = sse_frame("delta", {"text": "a"})   # 没有 done / [DONE]
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        got = "".join(_client(rt).chat_stream(_req()))
    assert got == "a"


def test_stream_forces_stream_true():
    seen = {}

    def h(r):
        import json
        seen.update(json.loads(r["body"]))
        return sse(_stream_body("x"))

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", h)
        list(_client(rt).chat_stream(_req()))

    # 调用方可能忘了设 stream，SDK 必须补上——否则服务端返回 JSON
    # 而客户端按 SSE 解析，会得到空结果
    assert seen["stream"] is True


def test_stream_sets_accept_header():
    seen = {}

    def h(r):
        seen.update(r["headers"])
        return sse(_stream_body("x"))

    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", h)
        list(_client(rt).chat_stream(_req()))

    assert "text/event-stream" in seen.get("Accept", "")


def test_stream_early_break_closes():
    body = _stream_body(*["x"] * 50)
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        c = _client(rt)
        it = c.chat_stream(_req())
        n = 0
        for _ in it:
            n += 1
            if n >= 2:
                break
        it.close()     # 必须能正常关闭，不能抛
    assert n == 2


def test_stream_context_manager():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: sse(_stream_body("a", "b")))
        with _client(rt).chat_stream(_req()) as it:
            got = "".join(it)
    assert got == "ab"


def test_stream_context_manager_early_exit():
    body = _stream_body(*["x"] * 20)
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        with _client(rt).chat_stream(_req()) as it:
            next(it)
    # 正常退出，没有异常


def test_chat_sync_accumulates():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: sse(_stream_body("he", "llo", finish="length")))
        res = _client(rt).chat_sync(_req())

    assert res.content == "hello"
    assert res.finish_reason == "length"
    assert res.truncated is True       # finish_reason == "length" 的便捷判断
    assert res.usage is not None
    # _stream_body 传了 2 个 delta 块，假服务端据此填 completion_tokens
    assert res.usage.completion_tokens == 2
    assert res.model is not None
    assert res.model.actual == "m1"


def test_chat_sync_not_truncated_on_stop():
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat",
              lambda r: sse(_stream_body("a", finish="stop")))
        res = _client(rt).chat_sync(_req())
    assert res.truncated is False


def test_chat_sync_collects_warnings():
    body = (sse_frame("warning", {"warnings": ["上下文被裁剪"]})
            + sse_frame("delta", {"text": "a"})
            + sse_frame("done", {"finish_reason": "stop"})
            + "data: [DONE]\n\n")
    with FakeRuntime() as rt:
        rt.on("POST", "/api/v1/chat", lambda r: sse(body))
        res = _client(rt).chat_sync(_req())
    assert res.warnings == ["上下文被裁剪"]
