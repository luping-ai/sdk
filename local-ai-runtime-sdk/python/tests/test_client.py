"""发现、客户端构造、错误分类与重试的测试。"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakeruntime import FakeRuntime, envelope, error

from localai import (
    ChatRequest,
    Client,
    Config,
    ErrCapabilityUnmet,
    ErrModelUnavailable,
    ErrNotFound,
    ErrQueueFull,
    ErrResourceLimited,
    ErrRuntimeUnavailable,
    ErrUnauthorized,
    Message,
    Timeouts,
    is_retryable,
    retry_after_of,
)
from localai._discovery import Discovery, DiscoveryPath, discover
from localai._errors import kind_for_code

# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def test_discover_explicit():
    d = discover("http://127.0.0.1:1234/", "tok")
    assert d.url == "http://127.0.0.1:1234"
    assert d.token == "tok"


def test_discover_trims_trailing_slashes():
    d = discover("http://127.0.0.1:1234///")
    assert d.url == "http://127.0.0.1:1234"


def test_discover_env(monkeypatch):
    monkeypatch.setenv("LAIH_URL", "http://127.0.0.1:7777")
    monkeypatch.setenv("LAIH_TOKEN", "envtok")
    d = discover()
    assert d.url == "http://127.0.0.1:7777"
    assert d.token == "envtok"


def test_discover_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("LAIH_URL", "http://127.0.0.1:7777")
    d = discover("http://127.0.0.1:8888", "t")
    assert d.url == "http://127.0.0.1:8888"


def test_discover_explicit_token_overrides_env(monkeypatch):
    monkeypatch.setenv("LAIH_URL", "http://127.0.0.1:7777")
    monkeypatch.setenv("LAIH_TOKEN", "envtok")
    d = discover(None, "explicittok")
    assert d.token == "explicittok"


def test_discovery_path_ends_with_runtime_json():
    assert DiscoveryPath().name == "runtime.json"


def test_discovery_from_dict_ignores_unknown_keys():
    d = Discovery.from_dict({"url": "http://x/", "extra": 1})
    assert d.url == "http://x"


def test_discovery_from_dict_trims_url():
    d = Discovery.from_dict({"url": "http://x:1/"})
    assert d.url == "http://x:1"


# ---------------------------------------------------------------------------
# 三层超时
# ---------------------------------------------------------------------------


def test_timeouts_defaults():
    t = Timeouts().resolved()
    assert t.connect == 1.0
    assert t.ttft == 15.0
    assert t.total == 60.0


def test_timeouts_custom():
    t = Timeouts(connect=2, ttft=30, total=120).resolved()
    assert (t.connect, t.ttft, t.total) == (2.0, 30.0, 120.0)


def test_timeouts_non_positive_falls_back():
    # 0 秒超时没有意义，只可能是"没设置"
    t = Timeouts(connect=0, ttft=-1, total=0).resolved()
    assert t.connect == 1.0
    assert t.ttft == 15.0
    assert t.total == 60.0


# ---------------------------------------------------------------------------
# 错误码映射
# ---------------------------------------------------------------------------


def test_error_code_mapping():
    assert kind_for_code("queue_full") is ErrQueueFull
    assert kind_for_code("resource_limited") is ErrResourceLimited
    assert kind_for_code("capability_unmet") is ErrCapabilityUnmet
    assert kind_for_code("model_not_found") is ErrNotFound
    assert kind_for_code("not_found") is ErrNotFound
    assert kind_for_code("forbidden") is ErrUnauthorized


def test_unknown_code_is_conservative():
    # 未知码至少要让调用方知道"这次没成功"
    assert kind_for_code("some_future_code") is ErrRuntimeUnavailable


def test_kind_sentinels_are_distinct():
    kinds = [ErrRuntimeUnavailable, ErrUnauthorized, ErrResourceLimited,
             ErrQueueFull, ErrModelUnavailable, ErrNotFound]
    assert len(set(kinds)) == len(kinds)


# ---------------------------------------------------------------------------
# 真实请求路径上的错误分类
# ---------------------------------------------------------------------------


def _client(rt: FakeRuntime, **kw) -> Client:
    return Client(Config(base_url=rt.url, token="t",
                         max_retries=0, **kw))


def test_401_maps_to_unauthorized():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models",
              lambda r: error(401, "unauthorized", "token 无效"))
        with pytest.raises(Exception) as ei:
            _client(rt).models()
        e = ei.value
        assert e.kind is ErrUnauthorized
        assert e.status_code == 401
        assert not is_retryable(e)


def test_404_kb_maps_to_not_found():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/knowledge/bases/x/stats",
              lambda r: error(404, "not_found", "没有这个库"))
        with pytest.raises(Exception) as ei:
            _client(rt).kb_stat("x")
        assert ei.value.kind is ErrNotFound


def test_500_maps_to_runtime_unavailable():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models",
              lambda r: error(500, "internal_error", "崩了"))
        with pytest.raises(Exception) as ei:
            _client(rt).models()
        assert ei.value.kind is ErrRuntimeUnavailable


def test_non_json_response_is_runtime_unavailable():
    # 连到了别的服务（端口占用、配错地址）
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models",
              lambda r: (200, {}, b"<html>not our service</html>"))
        with pytest.raises(Exception) as ei:
            _client(rt).models()
        assert ei.value.kind is ErrRuntimeUnavailable


def test_error_message_includes_request_id():
    with FakeRuntime() as rt:
        def h(r):
            status, headers, raw = error(429, "queue_full", "队列满",
                                         retryable=True)
            import json
            d = json.loads(raw)
            d["request_id"] = "req_abc123"
            return status, headers, json.dumps(d).encode()
        rt.on("POST", "/api/v1/chat", h)
        with pytest.raises(Exception) as ei:
            _client(rt).chat(ChatRequest(messages=[Message("user", "hi")]))
        assert "req_abc123" in str(ei.value)


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------


def test_retry_on_resource_limited():
    calls = []

    def h(r):
        calls.append(1)
        if len(calls) < 3:
            return error(503, "resource_limited", "内存不足",
                         retryable=True, retry_after_ms=10)
        return envelope({"models": [], "active": "", "total": 0})

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", h)
        c = Client(Config(base_url=rt.url, token="t", max_retries=2))
        t0 = time.monotonic()
        c.models()
        elapsed = time.monotonic() - t0

    assert len(calls) == 3        # 首次 + 2 次重试
    # 两次等待各约 10ms（含抖动），不应是秒级
    assert elapsed < 1.0


def test_no_retry_on_invalid_request():
    calls = []

    def h(r):
        calls.append(1)
        return error(400, "invalid_request", "参数错")

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", h)
        c = Client(Config(base_url=rt.url, token="t", max_retries=2))
        with pytest.raises(Exception):
            c.models()

    # 重试必然再次失败，只会浪费用户时间
    assert len(calls) == 1


def test_no_retry_on_capability_unmet():
    calls = []

    def h(r):
        calls.append(1)
        return error(503, "capability_unmet", "模型不符合策略")

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", h)
        c = Client(Config(base_url=rt.url, token="t", max_retries=2))
        with pytest.raises(Exception) as ei:
            c.models()
        assert ei.value.kind is ErrCapabilityUnmet

    # "关程序没用，得换模型"——重试一定再失败
    assert len(calls) == 1


def test_max_retries_zero_disables_retry():
    calls = []

    def h(r):
        calls.append(1)
        return error(503, "resource_limited", "满", retryable=True,
                     retry_after_ms=1)

    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models", h)
        c = Client(Config(base_url=rt.url, token="t", max_retries=0))
        with pytest.raises(Exception):
            c.models()

    assert len(calls) == 1


def test_retry_after_honored_from_body():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/models",
              lambda r: error(429, "queue_full", "满", retryable=True,
                              retry_after_ms=2500))
        c = _client(rt)
        with pytest.raises(Exception) as ei:
            c.models()
        assert retry_after_of(ei.value) == pytest.approx(2.5)


def test_retry_after_honored_from_header():
    with FakeRuntime() as rt:
        def h(r):
            return error(429, "queue_full", "满", retryable=True,
                         headers={"Retry-After": "7"})
        rt.on("GET", "/api/v1/models", h)
        c = _client(rt)
        with pytest.raises(Exception) as ei:
            c.models()
        assert retry_after_of(ei.value) == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# 连接层
# ---------------------------------------------------------------------------


def test_connection_refused_is_runtime_unavailable():
    # 用一个几乎不可能有服务的端口
    c = Client(Config(
        base_url="http://127.0.0.1:1", token="t", max_retries=0,
        timeouts=Timeouts(connect=5, ttft=5, total=10),
    ))
    with pytest.raises(Exception) as ei:
        c.models()
    # 关键：必须是"没运行"而不是"超时"——两者的应对完全不同
    assert ei.value.kind is ErrRuntimeUnavailable


def test_client_repr_hides_token():
    c = Client(Config(base_url="http://127.0.0.1:1",
                      token="laih_supersecret"))
    assert "supersecret" not in repr(c)
    assert "127.0.0.1:1" in repr(c)


def test_default_config_does_not_raise():
    # 自动发现失败不阻止创建：可能只用 health 探活，或稍后才启动
    c = Client(Config(base_url="http://127.0.0.1:1"))
    assert c.base_url == "http://127.0.0.1:1"
