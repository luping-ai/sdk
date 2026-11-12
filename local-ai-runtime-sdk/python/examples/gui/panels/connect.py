"""面板：连接与健康。

演示 SDK 的自动发现与三态健康检查。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from widgets import BusyButton, FieldGrid, labeled_entry, scrollable_text, set_text

from localai import APIError, Client, Config, DiscoveryPath

if TYPE_CHECKING:  # pragma: no cover
    from app import App


class ConnectPanel(ttk.Frame):
    """连接与健康检查。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        title = ttk.Label(self, text="连接与健康", style="Heading.TLabel")
        title.grid(row=0, column=0, sticky="w", pady=(0, 4))

        ttk.Label(
            self,
            text="SDK 自动读取 discovery 文件找到端口与 token，不需要手填。"
                 "只有连远程实例或排障时才需要覆盖。",
            style="Hint.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))

        # -- 可选的显式覆盖 --------------------------------------------
        override = ttk.LabelFrame(self, text="可选：显式指定（留空则自动发现）",
                                  padding=(10, 6))
        override.grid(row=2, column=0, sticky="ew")
        override.columnconfigure(0, weight=1)

        row1, self._var_url, _ = labeled_entry(
            override, "地址", initial="", width=46)
        row1.grid(row=0, column=0, sticky="ew", pady=2)
        row2, self._var_token, _ = labeled_entry(
            override, "Token", initial="", width=46, show="•")
        row2.grid(row=1, column=0, sticky="ew", pady=2)

        # 自动发现的结果单独显示，不写进上面两个输入框。
        #
        # 关键区别：输入框里的值是"显式指定"，会覆盖自动发现；而这个
        # 标签只是展示。早期版本把地址回填进输入框，导致第二次连接时
        # 变成"显式地址 + 空 token"→ 401。详见 _render 里的注释。
        self._lbl_resolved = ttk.Label(override, text="尚未连接",
                                       style="Hint.TLabel", wraplength=660,
                                       justify="left")
        self._lbl_resolved.grid(row=2, column=0, sticky="w", pady=(4, 0))

        # -- 按钮 ------------------------------------------------------
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 6))

        self._btn_connect = BusyButton(
            actions, runner=self._app.runner, text="连接",
            command=self._do_connect)
        self._btn_connect.pack(side="left")

        self._btn_probe = BusyButton(
            actions, runner=self._app.runner, text="刷新状态",
            command=self._do_probe)
        self._btn_probe.pack(side="left", padx=(8, 0))

        BusyButton(actions, runner=self._app.runner, text="断开",
                   command=self._do_disconnect).pack(side="left", padx=(8, 0))

        ttk.Label(actions, text=f"discovery 文件：{DiscoveryPath()}",
                  style="Hint.TLabel").pack(side="right")

        # -- 结果区 ----------------------------------------------------
        result = ttk.LabelFrame(self, text="状态", padding=(10, 6))
        result.grid(row=4, column=0, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(1, weight=1)

        self._fields = FieldGrid(result, label_width=16)
        self._fields.grid(row=0, column=0, sticky="ew")

        ttk.Label(result, text="检查项", style="Hint.TLabel").grid(
            row=1, column=0, sticky="nw", pady=(8, 2))

        box, self._checks = scrollable_text(result, height=8, mono=True)
        box.grid(row=2, column=0, sticky="nsew")

    # -- 操作 ----------------------------------------------------------

    def auto_connect(self) -> None:
        """启动时自动连一次。

        失败不弹窗——用户可能就是要看"没启动时界面长什么样"。
        只在状态栏提示。
        """
        self._do_connect(silent=True)

    def _build_client(self) -> Client:
        url = self._var_url.get().strip()
        token = self._var_token.get().strip()
        cfg = Config(base_url=url or None, token=token or None)
        return Client(cfg)

    def _do_connect(self, silent: bool = False) -> None:
        def work() -> tuple[Client, object]:
            c = self._build_client()
            # 连接与探活放在一起：能拿到 health 才算真的连上
            return c, c.health()

        def done(value: object) -> None:
            client, rep = value                    # type: ignore[misc]
            self._app.set_client(client)
            self._render(client, rep)
            if rep.ready:                          # type: ignore[attr-defined]
                self._app.status.ok(
                    f"已连接 {client.base_url}（状态 {rep.state}）")  # type: ignore[attr-defined]
            else:
                self._app.status.warn(
                    f"已连接但暂不可服务：{rep.reason or rep.state}")  # type: ignore[attr-defined]

        def error(exc: BaseException) -> None:
            self._app.set_client(None)
            self._checks.configure(state="normal")
            set_text(self._checks, f"{type(exc).__name__}: {exc}")
            self._render_failed(exc)
            self._app.status.error("连接失败")
            if not silent:
                self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    def _do_probe(self) -> None:
        client = self._app.require_client()
        if client is None:
            return

        def work() -> object:
            return client.health()

        def done(rep: object) -> None:
            self._render(client, rep)
            state = rep.state                       # type: ignore[attr-defined]
            if rep.ready:                           # type: ignore[attr-defined]
                self._app.status.ok(f"状态正常：{state}")
            else:
                self._app.status.warn(f"状态：{state}")

        def error(exc: BaseException) -> None:
            self._render_failed(exc)
            self._app.status.error("探活失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    def _do_disconnect(self) -> None:
        self._app.set_client(None)
        self._fields.clear()
        set_text(self._checks, "")
        self._lbl_resolved.configure(text="尚未连接")
        self._app.status.info("已断开")

    # -- 渲染 ----------------------------------------------------------

    def _render(self, client: Client, rep: object) -> None:
        f = self._fields
        f.clear()
        f.set("地址", client.base_url)
        f.set("状态", rep.state)                        # type: ignore[attr-defined]
        f.set("可服务", "是" if rep.ready else "否",      # type: ignore[attr-defined]
              style="OK.TLabel" if rep.ready else "Warn.TLabel")  # type: ignore[attr-defined]
        f.set("已运行", f"{rep.uptime_seconds} 秒")      # type: ignore[attr-defined]
        f.set("版本", client.discovery.version or "(未知)")
        f.set("PID", client.discovery.pid or "(未知)")
        if rep.reason:                                  # type: ignore[attr-defined]
            f.set("说明", rep.reason)                    # type: ignore[attr-defined]

        checks = getattr(rep, "checks", {}) or {}
        if checks:
            lines = []
            for name, chk in checks.items():
                mark = "✓" if chk.ok else "✗"
                lines.append(f"{mark} {name}"
                             + (f"    {chk.message}" if chk.message else ""))
            set_text(self._checks, "\n".join(lines))
        else:
            set_text(self._checks, "（服务端未返回检查项）")

        # 连上后把实际地址显示出来，但**不回填到输入框**。
        #
        # 曾经这么做，结果第二次连接时输入框里的地址被当成"显式指定"，
        # 而 token 框是空的 → 拼出一个没有 token 的客户端 → 401
        # unauthorized。自动发现的优先级是"显式 > 环境变量 > discovery"，
        # 所以只要往输入框写了值，discovery 就不再被读取。
        if not self._var_url.get().strip():
            self._lbl_resolved.configure(
                text=f"自动发现：{client.base_url}（token "
                     f"{'已获取' if client.discovery.token else '缺失'}）")

    def _render_failed(self, exc: BaseException) -> None:
        f = self._fields
        f.clear()
        f.set("状态", "未连接", style="Error.TLabel")
        self._lbl_resolved.configure(text="尚未连接")
        if isinstance(exc, APIError):
            f.set("原因", exc.message)
            f.set("类别", exc.kind.name)
            f.set("可重试", "是" if exc.retryable else "否")
        else:
            f.set("原因", f"{type(exc).__name__}: {exc}")
