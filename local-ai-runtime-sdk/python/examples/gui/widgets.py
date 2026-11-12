"""可复用控件。

GUI 里最容易写乱的是"文本框 + 滚动条 + 只读/可编辑切换 + 自动滚到底"
这套组合，每个面板都抄一遍必然出现不一致（有的能复制、有的不能）。
集中到这里一份。
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from tkinter import ttk
from typing import ClassVar, Optional


def scrollable_text(
    parent: tk.Misc,
    *,
    height: int = 12,
    readonly: bool = True,
    mono: bool = False,
    wrap: str = "word",
) -> tuple[ttk.Frame, tk.Text]:
    """带纵向滚动条的文本框。返回 (容器, Text)。

    为什么返回容器：Text 必须和 Scrollbar 一起打包，调用方通常只想
    把"这一整块"放进布局，所以把打包细节收在内部。
    """
    frame = ttk.Frame(parent)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    text = tk.Text(frame, height=height, wrap=wrap, undo=not readonly,
                   relief="solid", borderwidth=1, padx=6, pady=4)
    if mono:
        text.configure(font=("Consolas", 10)
                       if tk.TkVersion and _is_windows() else ("TkFixedFont", 10))
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)

    text.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")

    if readonly:
        # 只读但仍可选中复制：把 state 设成 disabled 会让用户连选中都
        # 做不到，这是 tkinter 的常见坑。正确做法是拦截按键。
        _make_readonly(text)

    return frame, text


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"


def _make_readonly(text: tk.Text) -> None:
    """让 Text 只读，但保留选中与复制。"""
    def block(event: tk.Event) -> str:
        # 放行复制、全选、以及纯粹的导航键
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return ""
        if event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End",
            "Prior", "Next", "Shift_L", "Shift_R", "Control_L",
            "Control_R", "Tab", "ISO_Left_Tab",
        ):
            return ""
        return "break"

    text.bind("<Key>", block)
    # 鼠标右键菜单里提供复制，方便用户取走输出
    text.bind("<Button-3>", lambda e: _popup_copy(e, text))


def _popup_copy(event: tk.Event, text: tk.Text) -> None:
    menu = tk.Menu(text, tearoff=0)
    menu.add_command(label="复制",
                     command=lambda: _copy_selection(text))
    menu.add_command(label="全选并复制",
                     command=lambda: _copy_all(text))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


def _copy_selection(text: tk.Text) -> None:
    try:
        sel = text.get("sel.first", "sel.last")
    except tk.TclError:
        return
    if sel:
        text.clipboard_clear()
        text.clipboard_append(sel)


def _copy_all(text: tk.Text) -> None:
    content = text.get("1.0", "end-1c")
    text.clipboard_clear()
    text.clipboard_append(content)


def set_text(text: tk.Text, content: str, *, scroll_end: bool = False) -> None:
    """整体替换 Text 内容。"""
    text.configure(state="normal")
    text.delete("1.0", "end")
    text.insert("1.0", content)
    if scroll_end:
        text.see("end")


def append_text(text: tk.Text, content: str, *, scroll_end: bool = True) -> None:
    """追加内容，默认滚到底（流式输出需要）。"""
    text.insert("end", content)
    if scroll_end:
        text.see("end")


class FieldGrid(ttk.Frame):
    """两列「标签 : 值」网格，用于展示结构化结果。

    比把结果拼成文本好：字段名与值分列，长值自动换行，且能单独更新
    某一个字段（状态面板每次刷新只改动变化的项）。
    """

    def __init__(self, parent: tk.Misc, *, label_width: int = 14) -> None:
        super().__init__(parent)
        self._label_width = label_width
        self.columnconfigure(1, weight=1)
        self._rows: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        self._row = 0

    def set(self, name: str, value: object, *, style: str = "") -> None:
        """设置（或新建）一行。"""
        text = "" if value is None else str(value)
        if name in self._rows:
            lbl_name, lbl_val = self._rows[name]
            lbl_val.configure(text=text, style=style or "TLabel")
            return

        lbl_name = ttk.Label(self, text=name, style="Hint.TLabel",
                             width=self._label_width, anchor="nw")
        lbl_val = ttk.Label(self, text=text, style=style or "TLabel",
                            wraplength=520, anchor="nw", justify="left")
        lbl_name.grid(row=self._row, column=0, sticky="nw", pady=1)
        lbl_val.grid(row=self._row, column=1, sticky="nw", pady=1)
        self._rows[name] = (lbl_name, lbl_val)
        self._row += 1

    def clear(self) -> None:
        for name, (lbl_name, lbl_val) in list(self._rows.items()):
            lbl_name.destroy()
            lbl_val.destroy()
            del self._rows[name]
        self._row = 0


class StatusBar(ttk.Frame):
    """底部状态栏：左侧消息，右侧忙碌指示。

    三级严重度用颜色区分，因为 GUI 用户往往只看一眼颜色就决定要不要
    细看文字。
    """

    IDLE = "就绪"
    # ClassVar 标注：这是不可变的样式映射表，不是每个实例各自持有的状态
    _STYLES: ClassVar[dict[str, str]] = {
        "info": "Hint.TLabel",
        "ok": "OK.TLabel",
        "warn": "Warn.TLabel",
        "error": "Error.TLabel",
    }

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, padding=(10, 4))
        self.columnconfigure(0, weight=1)

        self._msg = ttk.Label(self, text=self.IDLE, style="Hint.TLabel",
                              anchor="w")
        self._msg.grid(row=0, column=0, sticky="ew")

        self._progress = ttk.Progressbar(self, mode="indeterminate", length=120)
        self._progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self._progress.grid_remove()

        self._busy_text = ttk.Label(self, text="", style="Hint.TLabel")
        self._busy_text.grid(row=0, column=2, sticky="e", padx=(6, 0))

        self._pending = 0

    def _set(self, text: str, kind: str = "info") -> None:
        if self._pending > 0:
            # 有后台任务时不要覆盖忙碌提示，否则用户会以为操作结束了
            return
        self._msg.configure(text=text, style=self._STYLES.get(kind, "Hint.TLabel"))

    def info(self, text: str) -> None:
        self._set(text, "info")

    def ok(self, text: str) -> None:
        self._set(text, "ok")

    def warn(self, text: str) -> None:
        self._set(text, "warn")

    def error(self, text: str) -> None:
        self._set(text, "error")

    def busy(self, text: str = "运行中…") -> None:
        self._pending += 1
        self._busy_text.configure(text=text)
        self._msg.configure(text="")
        self._progress.grid()
        self._progress.start(12)

    def idle(self) -> None:
        self._pending = max(0, self._pending - 1)
        if self._pending > 0:
            return
        self._progress.stop()
        self._progress.grid_remove()
        self._busy_text.configure(text="")


class BusyButton(ttk.Button):
    """忙碌期间自动禁用的按钮。

    防止用户在模型加载的十几秒里反复点同一个按钮——那会堆积请求，
    最后以 queue_full 结束，看起来像"程序坏了"。
    """

    def __init__(self, parent: tk.Misc, *, runner: object,
                 command, **kwargs: object) -> None:
        self._runner = runner
        self._command = command
        super().__init__(parent, command=self._on_click, **kwargs)
        runner.on_busy_change(self._on_busy)   # type: ignore[attr-defined]

    def _on_click(self) -> None:
        self._command()

    def _on_busy(self, pending: int) -> None:
        with contextlib.suppress(tk.TclError):
            self.state(["disabled"] if pending > 0 else ["!disabled"])


def labeled_entry(
    parent: tk.Misc,
    label: str,
    *,
    initial: str = "",
    width: int = 40,
    show: Optional[str] = None,
) -> tuple[ttk.Frame, tk.StringVar, ttk.Entry]:
    """一行「标签 + 输入框」。"""
    frame = ttk.Frame(parent)
    ttk.Label(frame, text=label, style="Hint.TLabel", width=14,
              anchor="w").pack(side="left")
    var = tk.StringVar(value=initial)
    entry = ttk.Entry(frame, textvariable=var, width=width,
                      show=show or "")
    entry.pack(side="left", fill="x", expand=True)
    return frame, var, entry
