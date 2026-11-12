"""Local AI Hub Runtime 的官方 Python 客户端。

## 设计目标

让业务代码无需了解底层模型即可调用 AI 能力：

- **自动发现**：不写死端口。端口是动态分配的，见 `discover`
- **类型化错误**：区分"稍后重试"与"不可能成功"，不靠字符串匹配
- **三层超时**：连接 / 首字节 / 总时长（本地推理冷启动可能很慢）
- **降级不是错误**：降级结果照常返回，通过字段标记
- **零依赖**：只用标准库

## 为什么不负责启动 Runtime

任何自动拉起行为必须对用户可见，否则会被误判为恶意程序。
启动交给安装器或系统自启项。

## 契约来源

`local-ai-runtime-sdk/docs/openapi.yaml` 是唯一真相。本包的类型定义
与它保持一致，但**不是**从它生成的——生成器产出不了类型化错误、
三层超时与 SSE 解析，而那三项恰好是 SDK 的全部价值。类型层用
`tools/fieldcheck` 做字段对照，运行时层与 Go SDK 语义对齐。

## 三分钟上手

    from localai import Client, ChatRequest, Message

    client = Client()                     # 自动发现
    res = client.chat(ChatRequest(messages=[Message("user", "你好")]))
    print(res.content)

    for delta in client.chat_stream(ChatRequest(
            messages=[Message("user", "讲个故事")])):
        print(delta, end="")

"""

from __future__ import annotations

from ._client import Client, Config
from ._discovery import Discovery, DiscoveryPath, discover
from ._errors import (
    APIError,
    ErrCapabilityUnmet,
    ErrClientQuota,
    ErrInputTooLong,
    ErrInvalidRequest,
    ErrModelUnavailable,
    ErrNotFound,
    ErrQueueFull,
    ErrResourceLimited,
    ErrRuntimeUnavailable,
    ErrTaskCancelled,
    ErrTimeout,
    ErrUnauthorized,
    is_retryable,
    retry_after_of,
)
from ._stream import StreamEvent, StreamEventType
from ._types import (
    Capability,
    ChatRequest,
    ChatResult,
    CheckResult,
    DocumentSummary,
    HealthReport,
    ImportRequest,
    ImportResult,
    KBStat,
    KBSummary,
    KnowledgeBaseRef,
    KnowledgeInfo,
    KnowledgeSource,
    Message,
    Model,
    ModelInfo,
    Priority,
    Quality,
    RAGContext,
    SearchHit,
    SearchRequest,
    SearchResult,
    Timeouts,
    Usage,
    VectorizeResult,
)

__version__ = "0.1.0"

__all__ = [
    # 错误
    "APIError",
    # 类型
    "Capability",
    "ChatRequest",
    "ChatResult",
    "CheckResult",
    # 客户端
    "Client",
    "Config",
    # 发现
    "Discovery",
    "DiscoveryPath",
    "DocumentSummary",
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
    # 流式
    "StreamEvent",
    "StreamEventType",
    "Timeouts",
    "Usage",
    "VectorizeResult",
    "discover",
    "is_retryable",
    "retry_after_of",
]
