"""面板：能力分级与模型清单。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from widgets import BusyButton, FieldGrid, scrollable_text, set_text

if TYPE_CHECKING:  # pragma: no cover
    from app import App


class CapabilityPanel(ttk.Frame):
    """展示 capability() 与 models() 的结果。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        ttk.Label(self, text="能力与模型", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w")

        ttk.Label(
            self,
            text="health 回答「服务是否活着」，capability 回答「活着但能做什么」。"
                 "低内存机器上 chat_enabled / rag_enabled 可能为假，此时传 kb_ids "
                 "会被静默忽略——所以业务方应先查这里。",
            style="Hint.TLabel", wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        BusyButton(actions, runner=app.runner, text="刷新能力与模型",
                   command=self.refresh).pack(side="left")

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.grid(row=3, column=0, sticky="nsew")

        # 上半：能力
        top = ttk.LabelFrame(pane, text="能力分级", padding=(10, 6))
        top.columnconfigure(0, weight=1)
        self._cap = FieldGrid(top, label_width=16)
        self._cap.grid(row=0, column=0, sticky="ew")
        box, self._cap_warn = scrollable_text(top, height=4)
        box.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        pane.add(top, weight=1)

        # 下半：模型表
        bottom = ttk.LabelFrame(pane, text="模型清单", padding=(10, 6))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)

        cols = ("id", "name", "size", "loaded", "present", "license")
        self._tree = ttk.Treeview(bottom, columns=cols, show="headings",
                                  height=10)
        heads = {
            "id": ("ID", 210), "name": ("名称", 230), "size": ("大小", 80),
            "loaded": ("已加载", 70), "present": ("已下载", 70),
            "license": ("许可证", 110),
        }
        for c, (text, width) in heads.items():
            self._tree.heading(c, text=text)
            self._tree.column(c, width=width, anchor="w")
        self._tree.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(bottom, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        pane.add(bottom, weight=2)

        self._models: list = []

    # -- 加载 ----------------------------------------------------------

    def refresh(self) -> None:
        client = self._app.require_client()
        if client is None:
            return

        def work() -> tuple:
            return client.capability(), client.models(), client.active_model()

        def done(value: object) -> None:
            cap, models, active = value            # type: ignore[misc]
            self._render_cap(cap)
            self._render_models(models, active)
            tail = f"，当前加载 {active}" if active else "，当前无模型"
            self._app.status.ok(
                f"能力 {cap.level}，{len(models)} 个模型{tail}")  # type: ignore[attr-defined]

        def error(exc: BaseException) -> None:
            self._app.status.error("读取能力失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    # -- 渲染 ----------------------------------------------------------

    def _render_cap(self, cap: object) -> None:
        f = self._cap
        f.clear()
        style = "OK.TLabel" if cap.level == "full" else "Warn.TLabel"  # type: ignore[attr-defined]
        f.set("等级", cap.level, style=style)                          # type: ignore[attr-defined]
        f.set("对话可用", "是" if cap.chat_enabled else "否")            # type: ignore[attr-defined]
        f.set("知识库可用", "是" if cap.rag_enabled else "否")            # type: ignore[attr-defined]
        f.set("可用内存", f"{cap.available_bytes / (1 << 30):.1f} GiB")   # type: ignore[attr-defined]
        if cap.baseline_need_bytes:                                    # type: ignore[attr-defined]
            f.set("基准需求",
                  f"{cap.baseline_need_bytes / (1 << 30):.1f} GiB")     # type: ignore[attr-defined]
        if cap.rag_model:                                              # type: ignore[attr-defined]
            f.set("RAG 模型", cap.rag_model)                             # type: ignore[attr-defined]
        f.set("说明", cap.reason)                                       # type: ignore[attr-defined]

        warns = getattr(cap, "warnings", []) or []
        if warns:
            set_text(self._cap_warn, "\n".join(f"· {w}" for w in warns))
        else:
            set_text(self._cap_warn, "（无提示）")

    def _render_models(self, models: list, active: str) -> None:
        self._tree.delete(*self._tree.get_children())
        self._models = list(models)
        for i, m in enumerate(models):
            size = (f"{m.size_bytes / (1 << 20):.0f} MiB"
                    if m.size_bytes else "-")
            self._tree.insert(
                "", "end", iid=str(i),
                values=(m.id, m.name, size,
                        "是" if m.loaded else "",
                        "是" if m.present else "",
                        m.license or "-"),
            )
        for i, m in enumerate(models):
            if m.id == active:
                self._tree.item(str(i), tags=("active",))
        self._tree.tag_configure("active", background="#e8f5e9")

    def _on_select(self, _event: object = None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if not (0 <= idx < len(self._models)):
            return
        m = self._models[idx]
        self._app.status.info(
            f"{m.id}｜{m.arch or '?'}｜{m.quantization or '?'}｜"
            f"上下文 {m.context_length or '?'}｜"
            f"能力 {', '.join(m.capabilities) or '无'}｜"
            f"可商用 {m.commercial_use or '未标注'}")
