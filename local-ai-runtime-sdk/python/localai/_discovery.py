"""Runtime 自动发现。

## 为什么不能写死端口

端口是动态分配的：18765 被占用时会回退到随机端口。写死端口的代码
在"用户同时开了两个实例"或"18765 被别的程序占了"时会连不上，而
这种失败只在特定机器上复现，排查成本极高。

## 查找顺序

    显式 BaseURL → 环境变量 LAIH_URL/LAIH_TOKEN → discovery 文件 → 默认端口

与 Go SDK 完全一致（文档 18.4）。顺序不能改：显式参数必须最优先，
否则测试和"连远程实例"的场景无从表达。

## discovery 文件的位置必须与 Runtime 侧一致

这里的路径规则复制自 `internal/discovery/info.go`，**不是 import**——
SDK 不能依赖 Runtime 的内部包，否则第三方引入 SDK 时会连带拉进
SQLite、PDF 解析这些只在服务端用得上的依赖。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Discovery", "DiscoveryPath", "EnvToken", "EnvURL", "discover"]

# 环境变量名，与 Go SDK 及 Runtime 文档一致。
EnvURL = "LAIH_URL"
EnvToken = "LAIH_TOKEN"

# 默认端口：Runtime 优先尝试它，被占用则回退随机端口。
DEFAULT_PORT = 18765


@dataclass(frozen=True)
class Discovery:
    """discovery 文件的内容。

    `frozen=True`：这是"连接到哪个实例"的记录，运行期被改动只可能
    是 bug。需要换实例就新建一个。
    """

    url: str
    token: str = ""
    pid: int = 0
    version: str = ""
    api: str = ""
    started_at: int = 0
    state: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Discovery:
        return cls(
            url=str(d.get("url", "")).rstrip("/"),
            token=str(d.get("token", "")),
            pid=int(d.get("pid", 0) or 0),
            version=str(d.get("version", "")),
            api=str(d.get("api", "")),
            started_at=int(d.get("started_at", 0) or 0),
            state=str(d.get("state", "")),
        )


def DiscoveryPath() -> Path:
    """返回 discovery 文件路径（与 Runtime 侧规则一致）。

    三平台不同，且 Windows 优先用 LOCALAPPDATA 而不是 USERPROFILE——
    与 Runtime 的实现保持一致，改一处必须同步另一处。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            # 环境变量缺失时的回退（服务账户、精简环境）
            base = str(Path.home() / "AppData" / "Local")
        return Path(base) / "LocalAIHub" / "runtime.json"

    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_DATA_HOME")
        if not base:
            base = str(Path.home() / ".local" / "share")
        return Path(base) / "local-ai-hub" / "runtime.json"

    # macOS 及其它：与 Go SDK 的 default 分支一致
    return Path.home() / ".local-ai-hub" / "runtime.json"


def _read_discovery_file() -> Discovery | None:
    """读 discovery 文件。任何失败都返回 None（不是错误）。

    文件不存在是**正常状态**（Runtime 还没启动过），不该让调用方
    在这里处理异常——真正的失败会在首次请求时暴露，且那时有更
    准确的错误可给（连接被拒绝 = 没在运行）。
    """
    try:
        path = DiscoveryPath()
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None

    if not isinstance(d, dict) or not d.get("url"):
        return None
    return Discovery.from_dict(d)


def discover(
    base_url: str | None = None,
    token: str | None = None,
) -> Discovery:
    """按固定顺序查找 Runtime，返回地址与 token。

    不验证连接是否可用——那需要网络请求，应由调用方在需要时做
    （见 `Client.health`）。这样 `Client()` 的构造是纯粹的、不会
    因为 Runtime 未启动而抛异常，也不会阻塞 1 秒。

    找不到任何来源时返回默认端口的兜底地址（token 为空）。此时
    调用必然失败，但错误会是清晰的 `ErrRuntimeUnavailable`
    （connect refused），而不是"构造 client 时抛了个奇怪的异常"。
    """
    # 1. 显式指定
    if base_url:
        return Discovery(url=base_url.rstrip("/"), token=token or "")

    # 2. 环境变量
    env_url = os.environ.get(EnvURL)
    if env_url:
        return Discovery(
            url=env_url.rstrip("/"),
            token=token or os.environ.get(EnvToken, ""),
        )

    # 3. discovery 文件
    d = _read_discovery_file()
    if d is not None:
        if token:
            return Discovery(url=d.url, token=token, pid=d.pid,
                             version=d.version, api=d.api,
                             started_at=d.started_at, state=d.state)
        return d

    # 4. 默认端口兜底
    #
    # 没有 token 无法调用任何接口，但仍返回地址——调用方可能从
    # 别的渠道拿到 token（自己的配置、环境变量之外的管理系统）。
    return Discovery(url=f"http://127.0.0.1:{DEFAULT_PORT}", token=token or "")
