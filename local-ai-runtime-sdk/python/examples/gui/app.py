"""Local AI Hub Python SDK —— 图形界面演示。

一个窗口，左侧选功能，右侧操作，底部状态栏。

## 为什么用 tkinter

它是标准库自带的，所以这个演示**不引入任何依赖**——与 SDK 本身的
零依赖约束一致。用户 `pip install luping-localai` 之后就能跑。

PyQt/PySide 界面更好看，但要额外装几十 MB；对一个"看 SDK 能做什么"
的演示程序来说，代价不值。

## 界面结构与代码组织

    gui/
    ├── app.py          主窗口：左侧导航 + 右侧面板 + 状态栏
    ├── tasks.py        后台线程 → 主线程的结果回传（tkinter 是单线程的）
    ├── widgets.py      复用的控件：可滚动文本、字段表、忙碌遮罩
    └── panels/         每个功能一个面板，互不依赖
        ├── connect.py      连接与健康
        ├── capability.py   能力分级 + 模型清单
        ├── chat.py         对话（流式实时显示）
        ├── structured.py   结构化输出
        └── knowledge.py    知识库全流程

## 运行

    python examples/gui/app.py
"""

from __future__ import annotations

# E402 在本文件是刻意的：为了让 `python examples/gui/app.py` 直接可跑，
# 必须在 import localai 之前先把仓库根目录塞进 sys.path。正常情况下
# （pip 装过包）那段代码是空操作。
# ruff: noqa: E402
import os
import sys

# 允许直接 `python examples/gui/app.py` 运行：把 SDK 根目录加进 sys.path。
# 正常安装（pip install luping-localai）时这个目录里没有 localai/，
# 因此该行无副作用；开发态则免去设 PYTHONPATH 的麻烦。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if os.path.isdir(os.path.join(_ROOT, "localai")) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from panels.capability import CapabilityPanel
from panels.chat import ChatPanel
from panels.connect import ConnectPanel
from panels.knowledge import KnowledgePanel
from panels.structured import StructuredPanel
from tasks import TaskRunner
from widgets import StatusBar

from localai import APIError, Client

APP_TITLE = "Local AI Hub —— Python SDK 演示"
APP_SIZE = "1080x720"
MIN_SIZE = (900, 600)


class App(tk.Tk):
    """主窗口。

    持有全局的 Client 与 TaskRunner，各面板只负责自己的界面与交互。
    """

    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry(APP_SIZE)
        self.minsize(*MIN_SIZE)

        # 状态栏要能显示任何面板的消息，所以在这里建
        self.client: Optional[Client] = None
        self.runner = TaskRunner(self)

        self._build_style()
        self._build_layout()

        self.runner.on_busy_change(self._on_busy_change)
        self.runner.poll()                # 启动主线程轮询

        # 启动时自动尝试连接，省掉用户一次点击
        self.after(100, self._auto_connect)

    # -- 外观 ----------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self)
        # vista 在 Windows 上最接近系统原生；其它平台退回 clam
        for name in ("vista", "clam", "default"):
            if name in style.theme_names():
                style.theme_use(name)
                break

        base = 10
        # 中文字体：Windows 用微软雅黑（TkDefaultFont 对中文的回退不稳），
        # 其它平台交给系统默认
        ui_font = (("Microsoft YaHei UI", base) if sys.platform == "win32"
                   else ("TkDefaultFont", base))
        head_font = (("Microsoft YaHei UI", base + 4, "bold")
                     if sys.platform == "win32"
                     else ("TkDefaultFont", base + 4, "bold"))
        style.configure(".", font=ui_font)
        style.configure("Heading.TLabel", font=head_font)
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Warn.TLabel", foreground="#a15c00")
        style.configure("Error.TLabel", foreground="#b00020")
        style.configure("OK.TLabel", foreground="#2e7d32")
        style.configure("Mono.TText", font=("Consolas", base)
                        if sys.platform == "win32" else ("TkFixedFont", base))

    def _build_layout(self) -> None:
        root = ttk.Frame(self, padding=0)
        root.pack(fill="both", expand=True)

        # 顶部标题条
        header = ttk.Frame(root, padding=(12, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Local AI Hub Python SDK",
                  style="Heading.TLabel").pack(side="left")
        self.lbl_target = ttk.Label(header, text="未连接", style="Hint.TLabel")
        self.lbl_target.pack(side="right")

        ttk.Separator(root).pack(fill="x")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)

        # 左侧导航
        nav = ttk.Frame(body, padding=(8, 8), width=190)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        ttk.Label(nav, text="功能", style="Hint.TLabel").pack(anchor="w", pady=(0, 6))

        self._nav = ttk.Treeview(nav, show="tree", selectmode="browse")
        self._nav.pack(fill="both", expand=True)
        self._nav.column("#0", width=170)

        ttk.Separator(body, orient="vertical").pack(side="left", fill="y")

        # 右侧内容区
        content = ttk.Frame(body, padding=(12, 8))
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self._panels: dict[str, ttk.Frame] = {}
        self._current: Optional[str] = None

        specs = [
            ("connect", "连接与健康", ConnectPanel),
            ("capability", "能力与模型", CapabilityPanel),
            ("chat", "对话（流式）", ChatPanel),
            ("structured", "结构化输出", StructuredPanel),
            ("knowledge", "知识库（RAG）", KnowledgePanel),
        ]

        for key, label, cls in specs:
            frame = cls(content, app=self)
            frame.grid(row=0, column=0, sticky="nsew")
            self._panels[key] = frame
            self._nav.insert("", "end", iid=key, text=f"  {label}")

        self._nav.bind("<<TreeviewSelect>>", self._on_nav_select)

        # 底部状态栏
        ttk.Separator(root).pack(fill="x")
        self.status = StatusBar(root)
        self.status.pack(fill="x")

        self._nav.selection_set("connect")

    # -- 事件 ----------------------------------------------------------

    def _on_nav_select(self, _event: object = None) -> None:
        sel = self._nav.selection()
        if not sel:
            return
        key = sel[0]
        if key == self._current:
            return
        self._current = key
        self._panels[key].tkraise()
        # 切到需要连接的页面时，若还没连上就提示
        if key != "connect" and self.client is None:
            self.status.warn("尚未连接 Runtime，请先到「连接与健康」点击连接")

    def _on_busy_change(self, pending: int) -> None:
        if pending > 0:
            self.status.busy(f"运行中（{pending} 个后台任务）…")
        else:
            self.status.idle()

    def _auto_connect(self) -> None:
        panel = self._panels["connect"]
        if isinstance(panel, ConnectPanel):
            panel.auto_connect()

    # -- 供面板调用 ----------------------------------------------------

    def set_client(self, client: Optional[Client]) -> None:
        self.client = client
        if client is None:
            self.lbl_target.configure(text="未连接")
        else:
            self.lbl_target.configure(text=client.base_url)

    def require_client(self) -> Optional[Client]:
        """取出客户端；未连接时弹提示并返回 None。

        各面板统一用它，避免每个操作都写一遍空值检查。
        """
        if self.client is None:
            messagebox.showinfo(
                "尚未连接",
                "请先到左侧「连接与健康」建立连接。\n\n"
                "通常是点一下「连接」按钮即可——SDK 会自动读 discovery 文件，"
                "不需要手动填端口和 token。",
                parent=self,
            )
            self._nav.selection_set("connect")
            return None
        return self.client

    def notify_error(self, exc: BaseException) -> None:
        """统一展示错误。

        这里刻意做得比命令行版更细：GUI 用户看不到 traceback，所以
        要把「哪一类错误」「服务端说了什么」「可不可重试」都摆出来。
        """
        if isinstance(exc, APIError):
            lines = [f"{exc.kind.name.lower()}", ""]
            if exc.message:
                lines.append(exc.message)
            lines.append("")
            if exc.code:
                lines.append(f"错误码：{exc.code}")
            if exc.status_code:
                lines.append(f"HTTP 状态：{exc.status_code}")
            if exc.request_id:
                lines.append(f"request_id：{exc.request_id}")
            lines.append("")
            if exc.retryable:
                wait = f"，建议 {exc.retry_after:g} 秒后重试" if exc.retry_after else ""
                lines.append(f"这是可重试错误{wait}。")
            else:
                lines.append("这不可重试——重试会再次失败。")
            messagebox.showerror("调用失败", "\n".join(lines), parent=self)
        else:
            messagebox.showerror("意外错误",
                                 f"{type(exc).__name__}: {exc}", parent=self)

    def on_close(self) -> None:
        self.destroy()


def main() -> int:
    try:
        app = App()
    except tk.TclError as exc:
        print("无法创建窗口：", exc, file=sys.stderr)
        print("如果是无图形环境（SSH、容器、CI），请用命令行版：", file=sys.stderr)
        print("    python examples/demo.py", file=sys.stderr)
        return 1

    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
