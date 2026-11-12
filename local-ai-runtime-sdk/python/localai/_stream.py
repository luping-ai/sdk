"""SSE 流式解析。

## 为什么必须手写

OpenAPI 生成器把 `text/event-stream` 当普通 JSON 响应处理，产出的
客户端在 `/api/v1/chat?stream=true` 上完全不可用。而流式恰好是
对话体验的核心——等完整响应再显示，用户会以为程序卡死。

## 事件顺序

    queued（可能多次）→ meta → warning（可选）→ delta（多次）
    → usage → done

任一步失败可能替换为 `error`。心跳是以 `:` 开头的注释行，不是事件。

## 为什么不依赖第三方 SSE 库

零依赖是设计约束（与 Go SDK 一致）。SSE 的解析规则本身很简单：
按行读，`data:` 前缀累积，空行分帧。真正的复杂度不在协议，而在
"帧内是 JSON、最后一帧是 [DONE]、心跳要跳过"这些本项目特有的细节。

## 分帧的坑

SSE 允许一帧有多行 `data:`（按规范应以 `\\n` 拼接）。本项目的服务端
每帧只发一行，但解析器按规范实现——否则服务端哪天改成多行
（或中间插入一个 `id:` 字段），客户端会静默拼错 JSON。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Optional

from ._errors import APIError, kind_for_code

__all__ = ["StreamEvent", "StreamEventType", "decode_event", "parse_sse"]


class StreamEventType:
    """事件类型常量。

    用字符串常量而不是 Enum：调用方常写 `if ev.type == "delta"`，
    Enum 会迫使它们 import 比较对象，收益不抵摩擦。
    """

    QUEUED = "queued"
    META = "meta"
    DELTA = "delta"
    USAGE = "usage"
    DONE = "done"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class StreamEvent:
    """流式输出的一个事件。

    与 Go SDK 的 `StreamEvent` 结构对齐：同一时刻只有 `type` 对应的
    那个字段有值。Python 没有联合类型，但可以用 `type` 做判别后直接
    取字段——调用方应先看 `type`。

    额外的便利属性（`text` / `error`）把各事件里的文本统一暴露出来，
    因为"只关心文本增量"是最常见的用法。
    """

    type: str = ""

    # delta 事件的增量文本
    text: str = ""

    # meta 事件
    request_id: str = ""
    model: str = ""
    degraded: bool = False
    knowledge: Optional[dict] = None

    # queued 事件
    ahead: int = 0
    ahead_same_priority: int = 0
    priority: str = ""
    estimated_wait_ms: int = 0

    # usage 事件
    usage: Optional[dict] = None

    # done 事件
    finish_reason: str = ""

    # warning 事件的提示列表
    warnings: list[str] = field(default_factory=list)

    # error 事件
    error: Optional[APIError] = None

    def __str__(self) -> str:
        if self.type == StreamEventType.DELTA:
            return self.text
        if self.type == StreamEventType.ERROR and self.error:
            return str(self.error)
        return f"<StreamEvent {self.type}>"


def parse_sse(lines: Iterator[str]) -> Iterator[str]:
    """把 SSE 行流切成一个个 data 负载。

    产出的是每帧 `data:` 的内容（多行按规范用 `\\n` 拼接）。
    `[DONE]` 也会产出，由调用方决定如何终止——不在这一层 break，
    因为"看到 [DONE] 就断"是调用方的策略，不是协议的一部分。

    跳过：空行（分帧符）、以 `:` 开头的注释（心跳）、
    其它字段（`event:` / `id:` / `retry:`）——本项目的服务端把事件
    类型放在 JSON 的 `event` 字段里，SSE 层的 `event:` 用不到。
    """
    buf: list[str] = []

    for raw in lines:
        line = raw.rstrip("\r\n")

        if line == "":
            # 空行 = 一帧结束
            if buf:
                yield "\n".join(buf)
                buf.clear()
            continue

        if line.startswith(":"):
            # 心跳注释，不是事件
            continue

        if line.startswith("data:"):
            # 规范规定 `data:` 后可选一个空格，要去掉
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            buf.append(value)
            continue

        # 其它字段（event: / id: / retry:）忽略
        continue

    # 流结束时若还有未分帧的内容，产出它——服务端异常断连时
    # 最后一帧可能没有空行收尾，丢掉等于丢数据。
    if buf:
        yield "\n".join(buf)


def decode_event(payload: str) -> Optional[StreamEvent]:
    """把一个 data 负载解析成 StreamEvent。

    解析失败返回 None 而不是抛异常：单帧坏掉不该中断整条流——
    继续读后续内容，用户至少能看到部分输出。这与 Go SDK 的行为一致。

    但 `error` 事件里的错误是**有效载荷**，不是解析失败，必须
    构造成 APIError 让调用方能按类别处理。
    """
    try:
        wire = json.loads(payload)
    except ValueError:
        return None

    if not isinstance(wire, dict):
        return None

    etype = str(wire.get("event", ""))
    data = wire.get("data")
    if not isinstance(data, dict):
        data = {}

    ev = StreamEvent(type=etype)

    if etype == StreamEventType.QUEUED:
        ev.ahead = int(data.get("ahead", 0) or 0)
        ev.ahead_same_priority = int(data.get("ahead_same_priority", 0) or 0)
        ev.priority = str(data.get("priority", ""))
        ev.estimated_wait_ms = int(data.get("estimated_wait_ms", 0) or 0)

    elif etype == StreamEventType.META:
        ev.request_id = str(data.get("request_id", ""))
        ev.model = str(data.get("model", ""))
        ev.degraded = bool(data.get("degraded", False))
        knowledge = data.get("knowledge")
        ev.knowledge = knowledge if isinstance(knowledge, dict) else None

    elif etype == StreamEventType.DELTA:
        ev.text = str(data.get("text", ""))

    elif etype == StreamEventType.USAGE:
        ev.usage = data

    elif etype == StreamEventType.DONE:
        ev.finish_reason = str(data.get("finish_reason", ""))

    elif etype == StreamEventType.WARNING:
        raw = data.get("warnings")
        if isinstance(raw, list):
            ev.warnings = [str(w) for w in raw]
        # 同时把合并后的文本放进 text，方便"只想知道有什么提示"的调用方
        ev.text = "；".join(ev.warnings)

    elif etype == StreamEventType.ERROR:
        code = str(data.get("code", ""))
        ev.error = APIError(
            kind_for_code(code),
            code=code,
            message=str(data.get("message", "")),
        )

    else:
        # 未知事件类型：保留下来让调用方能看见，而不是静默丢弃。
        # 服务端将来加新事件时，旧客户端至少不会崩。
        return ev

    return ev


def event_from_line(line: str) -> Optional[StreamEvent]:
    """便利函数：从单行 `data:` 内容解析事件（供测试与自定义传输用）。"""
    return decode_event(line)


def decode_usage_event(ev: StreamEvent) -> Any:
    """把 usage 事件转成 Usage 对象。

    单独一个函数是为了避免 `_stream` 与 `_types` 循环 import：
    `_stream` 只依赖 `_errors`，类型转换放在调用方做。
    """
    if ev.type != StreamEventType.USAGE or not ev.usage:
        return None
    return ev.usage
