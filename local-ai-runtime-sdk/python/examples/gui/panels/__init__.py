"""功能面板集合。

每个面板是一个 ttk.Frame 子类，构造签名统一为：

    Panel(parent, *, app: App)

app 提供：client / runner / status / require_client / notify_error。
面板只依赖这些，彼此之间不互相引用。
"""

from __future__ import annotations

from .capability import CapabilityPanel
from .chat import ChatPanel
from .connect import ConnectPanel
from .knowledge import KnowledgePanel
from .structured import StructuredPanel

__all__ = [
    "CapabilityPanel",
    "ChatPanel",
    "ConnectPanel",
    "KnowledgePanel",
    "StructuredPanel",
]
