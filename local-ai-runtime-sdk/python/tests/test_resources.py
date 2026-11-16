"""资源状态（resources）与契约版本的测试。

对应 S1 / S4：
- ``client.resources()`` 在 Python 与 Go 都可用，字段与
  ``GET /api/v1/resources`` 一致
- ``schema_version`` 在 HealthReport 与 Capability 上都可读，
  未提供时为 0 且不报错
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakeruntime import FakeRuntime, envelope  # noqa: E402

from localai import (  # noqa: E402
    Capability,
    Client,
    Config,
    ResourceStatus,
)

BASE = "http://127.0.0.1:1"


def make_client(rt: FakeRuntime) -> Client:
    return Client(Config(base_url=rt.url, token="t", max_retries=0))


# ---------------------------------------------------------------------------
# S1：resources()
# ---------------------------------------------------------------------------


def test_resources_full_payload():
    """完整响应必须逐字段解析，含嵌套的 capability 与 idle。

    嵌套对象是这里最容易漏的部分：漏了不会报错，只会让调用方拿到
    None 并以为"服务端没给这个信息"。
    """
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/resources", lambda req: envelope({
            "pressure": "BUSY",
            "available_bytes": 900 << 20,
            "total_bytes": 16 << 30,
            "available_percent": 5,
            "reason": "可用内存占总量不足 8%",
            "consumers": [
                {"pid": 1234, "name": "chrome.exe",
                 "bytes": 3 << 30, "readable_size": "3.0 GiB"},
            ],
            "advice": {
                "message": "内存紧张",
                "actions": [
                    {"kind": "close_app", "text": "关闭 chrome.exe",
                     "detail": "可释放 3.0 GiB"},
                ],
            },
            "enforcement": "enforced",
            "enforcement_note": "引擎上限 4.8 GiB",
            "capability": {
                "level": "full", "rag_enabled": True, "chat_enabled": True,
                "schema_version": 4,
            },
            "idle": {
                "enabled": True, "model_idle_seconds": 120,
                "unload_after_seconds": 600,
                "user_idle_after_seconds": 300,
                "user_idle_detectable": True,
                "user_idle_seconds": 400,
                "user_idle_source": "GetLastInputInfo",
                "last_reason": "user is active (2s)",
                "unloads_total": 3,
            },
        }))
        st = make_client(rt).resources()

    assert isinstance(st, ResourceStatus)
    assert st.pressure == "BUSY"
    assert st.available_bytes == 900 << 20
    assert st.total_bytes == 16 << 30
    assert st.available_percent == 5

    assert len(st.consumers) == 1
    # readable_size 是 json 名，Python 字段叫 readable——写错就永远是空串
    assert st.consumers[0].readable == "3.0 GiB"
    assert st.consumers[0].pid == 1234

    assert st.advice is not None
    assert st.advice.actions[0].kind == "close_app"

    assert st.capability is not None
    assert st.capability.rag_enabled is True
    assert st.capability.schema_version == 4

    assert st.idle is not None
    assert st.idle.user_idle_detectable is True
    assert st.idle.user_idle_source == "GetLastInputInfo"
    assert st.idle.unloads_total == 3

    assert st.strained is True


def test_resources_optional_nested_are_none():
    """未启用推理的部署：capability 与 idle 缺省，不报错（S1-2）。

    这是完全合法的部署形态（只跑模型、没开知识库，或只做健康监控），
    抛异常会让"只看内存"的调用方在不该失败的地方失败。
    """
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/resources", lambda req: envelope({
            "pressure": "NORMAL",
            "available_bytes": 8 << 30,
            "total_bytes": 16 << 30,
            "available_percent": 50,
        }))
        st = make_client(rt).resources()

    assert st.capability is None
    assert st.idle is None
    assert st.advice is None
    assert st.consumers == []
    assert st.strained is False


def test_resources_without_advice_when_normal():
    """资源充足时不该有 advice——返回"关闭程序"的建议纯属打扰。"""
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/resources", lambda req: envelope({
            "pressure": "NORMAL",
            "available_bytes": 8 << 30,
            "total_bytes": 16 << 30,
            "available_percent": 50,
            "advice": None,
        }))
        st = make_client(rt).resources()
    assert st.advice is None


def test_strained_thresholds():
    """Strained 的判据必须与"服务端何时开始警告"一致。

    LIMITED 是服务端 ShouldWarn 的起点，因此它也 strained——
    否则客户端要等到 BUSY 才让路，而那时服务端已经在提示用户关程序了。
    """
    cases = [
        ("NORMAL", False),
        ("LIMITED", True),
        ("BUSY", True),
        ("CRITICAL", True),
        ("", False),
    ]
    for pressure, want in cases:
        st = ResourceStatus(pressure=pressure)
        assert st.strained is want, f"{pressure} 的判断不符"


def test_resources_unknown_endpoint_is_clear_error():
    """旧版 Runtime 没有该端点时应给出 404 类错误，而不是解析成空对象。

    空对象会让调用方以为"压力等级是空字符串"，进而把"取不到信息"
    误当成"资源很好"。
    """
    with FakeRuntime() as rt:
        # 不注册路由 —— FakeRuntime 返回 404 信封
        try:
            make_client(rt).resources()
        except Exception as e:  # noqa: BLE001
            assert "no such endpoint" in str(e)
        else:
            raise AssertionError("未知端点应抛错")


# ---------------------------------------------------------------------------
# S4：契约版本
# ---------------------------------------------------------------------------


def test_health_schema_version():
    with FakeRuntime() as rt:
        rt.on("GET", "/health/ready", lambda req: envelope({
            "state": "ready", "schema_version": 4,
        }))
        rep = make_client(rt).health()
    assert rep.schema_version == 4


def test_health_schema_version_absent_is_zero():
    """旧版 Runtime 不返回该字段时为 0，且不报错（S4-2）。

    0 是"未知/过旧"的哨兵值，客户端据此进入只读浏览。
    若这里抛错，升级前的部署连健康检查都过不了。
    """
    with FakeRuntime() as rt:
        rt.on("GET", "/health/ready", lambda req: envelope({"state": "ready"}))
        rep = make_client(rt).health()
    assert rep.schema_version == 0


def test_capability_schema_version():
    """客户端通常只查 capability，所以它也必须带版本。"""
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/capability", lambda req: envelope({
            "level": "full", "rag_enabled": True,
            "chat_enabled": True, "schema_version": 4,
        }))
        cap = make_client(rt).capability()
    assert cap.schema_version == 4


def test_capability_schema_version_absent_is_zero():
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/capability", lambda req: envelope({
            "level": "full", "rag_enabled": True, "chat_enabled": True,
        }))
        cap = make_client(rt).capability()
    assert cap.schema_version == 0


def test_capability_clamps_unknown_level_to_declared_default():
    """未知 level 时保留服务端原值，不擅自改写成 rule_only。

    这里锁定的是"不要自作聪明"：未来服务端加一个 level 值，
    旧 SDK 应原样暴露而不是把它洗成最保守的那个——
    改写会让调用方看不到"服务端比 SDK 新"这个事实。
    """
    with FakeRuntime() as rt:
        rt.on("GET", "/api/v1/capability", lambda req: envelope({
            "level": "future_level",
            "rag_enabled": False, "chat_enabled": False,
        }))
        cap = make_client(rt).capability()
    assert isinstance(cap, Capability)
    assert cap.level == "future_level"
