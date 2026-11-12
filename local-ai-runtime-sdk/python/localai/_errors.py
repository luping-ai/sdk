"""类型化错误。

## 为什么需要

业务代码必须能区分"稍后重试"和"不可能成功"。字符串匹配是灾难：
换个措辞就失效，而判断错误会让用户看到"请重试"却永远不成功，
或者该重试时直接放弃。

## 用法

    from localai import Client, ErrResourceLimited, is_retryable

    try:
        res = client.chat(req)
    except APIError as e:
        if not is_retryable(e):
            raise
        if e.kind is ErrResourceLimited:
            show_hint("关掉其他程序再试")

`kind` 是哨兵对象（`ErrResourceLimited` 等），用 `is` 比较——比
字符串或数字枚举更快，也不会因拼写错误静默失效。
"""

from __future__ import annotations

import enum
from typing import Any

__all__ = [
    "KIND_FOR_CODE",
    "APIError",
    "ErrCapabilityUnmet",
    "ErrClientQuota",
    "ErrInputTooLong",
    "ErrInvalidRequest",
    "ErrModelUnavailable",
    "ErrNotFound",
    "ErrQueueFull",
    "ErrResourceLimited",
    "ErrRuntimeUnavailable",
    "ErrTaskCancelled",
    "ErrTimeout",
    "ErrUnauthorized",
    "ErrorKind",
    "is_retryable",
    "retry_after_of",
]


class ErrorKind(enum.Enum):
    """错误类别的哨兵值。

    用 `enum.Enum` 而不是裸字符串：`e.kind is ErrQueueFull` 是恒等
    比较，打字错误会立刻报 AttributeError，而不是静默地永不相等。
    """

    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    UNAUTHORIZED = "unauthorized"
    RESOURCE_LIMITED = "resource_limited"
    QUEUE_FULL = "queue_full"
    CLIENT_QUOTA = "client_quota"
    INPUT_TOO_LONG = "input_too_long"
    MODEL_UNAVAILABLE = "model_unavailable"
    CAPABILITY_UNMET = "capability_unmet"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    TASK_CANCELLED = "task_cancelled"
    TIMEOUT = "timeout"

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试
        return f"<ErrorKind.{self.name}>"


# 模块级别的短别名：调用方写 `e.kind is ErrQueueFull`。
#
# 这不是"再定义一遍"，而是引用同一个枚举成员——`is` 比较仍然成立。
ErrRuntimeUnavailable = ErrorKind.RUNTIME_UNAVAILABLE
ErrUnauthorized = ErrorKind.UNAUTHORIZED
ErrResourceLimited = ErrorKind.RESOURCE_LIMITED
ErrQueueFull = ErrorKind.QUEUE_FULL
ErrClientQuota = ErrorKind.CLIENT_QUOTA
ErrInputTooLong = ErrorKind.INPUT_TOO_LONG
ErrModelUnavailable = ErrorKind.MODEL_UNAVAILABLE
ErrCapabilityUnmet = ErrorKind.CAPABILITY_UNMET
ErrNotFound = ErrorKind.NOT_FOUND
ErrInvalidRequest = ErrorKind.INVALID_REQUEST
ErrTaskCancelled = ErrorKind.TASK_CANCELLED
ErrTimeout = ErrorKind.TIMEOUT

# 与 Go SDK 的 errorCodeKinds 一一对应，必须与 Runtime 侧
# internal/api/errors.go 的常量保持一致。
#
# 不一致会让业务方朝错误方向优化——例如把"队列满"当成"资源不足"，
# 于是去关程序而不是降低请求频率。
KIND_FOR_CODE: dict[str, ErrorKind] = {
    "invalid_request": ErrInvalidRequest,
    "unauthorized": ErrUnauthorized,
    "forbidden": ErrUnauthorized,
    "model_not_found": ErrNotFound,
    "not_found": ErrNotFound,
    "method_not_allowed": ErrInvalidRequest,
    "queue_full": ErrQueueFull,
    "client_quota_exceeded": ErrClientQuota,
    "resource_limited": ErrResourceLimited,
    "model_unavailable": ErrModelUnavailable,
    "capability_unmet": ErrCapabilityUnmet,
    "internal_error": ErrRuntimeUnavailable,
}

_MESSAGES = {
    ErrRuntimeUnavailable: "localai: runtime unavailable",
    ErrUnauthorized: "localai: unauthorized",
    ErrResourceLimited: "localai: resource limited",
    ErrQueueFull: "localai: queue full",
    ErrClientQuota: "localai: client quota exceeded",
    ErrInputTooLong: "localai: input too long",
    ErrModelUnavailable: "localai: model unavailable",
    ErrCapabilityUnmet: "localai: capability unmet",
    ErrNotFound: "localai: not found",
    ErrInvalidRequest: "localai: invalid request",
    ErrTaskCancelled: "localai: task cancelled",
    ErrTimeout: "localai: timeout",
}


def kind_for_code(code: str) -> ErrorKind:
    """把服务端错误码映射到哨兵。

    未知码归为 `ErrRuntimeUnavailable`——保守选择：至少让调用方知道
    "这次没成功"，而不是让它以为请求被正常处理了。
    """
    return KIND_FOR_CODE.get(code, ErrRuntimeUnavailable)


class APIError(Exception):
    """来自 Runtime 的结构化错误。

    同时保留类别（`kind`，可 `is` 比较）与服务端原始信息（`code` /
    `message` / `retry_after` / `request_id`）。

    继承 `Exception` 而非 `IOError`：它不是操作系统层面的 I/O 问题，
    用 `except OSError` 意外捕获它会掩盖真正的文件/网络故障。
    """

    def __init__(
        self,
        kind: ErrorKind,
        *,
        code: str = "",
        message: str = "",
        retryable: bool = False,
        retry_after: float = 0.0,
        request_id: str = "",
        status_code: int = 0,
    ) -> None:
        self.kind = kind
        self.code = code
        self.message = message
        self.retryable = retryable
        # 建议等待秒数（来自服务端 retry_after_ms / Retry-After 头）
        self.retry_after = retry_after
        self.request_id = request_id
        self.status_code = status_code
        super().__init__(self._format())

    def _format(self) -> str:
        parts = [_MESSAGES.get(self.kind, str(self.kind))]
        if self.code:
            parts.append(f"[{self.code}]")
        s = " ".join(parts)
        if self.message:
            s += f": {self.message}"
        if self.retry_after > 0:
            s += f"（建议 {self.retry_after:g} 秒后重试）"
        if self.request_id:
            s += f" (request_id={self.request_id})"
        return s


def is_retryable(err: Any) -> bool:
    """判断该错误是否值得重试。

    与服务端返回的 `retryable` 一致，并补上网络层错误的判断。

    注意：SDK 内部已自动重试（退避 + 抖动）。本函数供需要自定义
    重试策略的调用方使用。
    """
    if err is None:
        return False
    if isinstance(err, APIError):
        return err.retryable
    # 网络层失败可重试：Runtime 可能刚启动
    return isinstance(err, (ConnectionError, TimeoutError))


def retry_after_of(err: Any) -> float:
    """返回错误建议的等待秒数（未提供时返回 0.0）。"""
    if isinstance(err, APIError):
        return err.retry_after
    return 0.0
