"""面板：对话（聊天框样式，多轮）。

## 为什么改成聊天框

原来的形态是「上面填问题，下面看回答」，每次发送把回答区清空。这种
布局暴露不出 SDK 真正的能力：**多轮上下文**。聊天框下历史一直可见，
用户能直观看到「我问过什么、它答了什么」，也就理解了 `messages`
数组是在累积——这是 SDK 里最容易讲错的概念（很多人以为要自己拼 prompt）。

## 为什么用 Canvas 而不是 Text

最初用 `Text` 的 tag 做气泡（背景色 + lmargin/rmargin 缩进）。**做不到**：
Text 的 tag 背景覆盖整个逻辑行，`lmargin`/`rmargin` 只移动行边界，
背景依然铺满窗口宽度。结果是「整行换色的日志」，不是气泡。

截图验证过这一点后才改用 Canvas：每条消息是一个 `tk.Label`，放在
`tk.Frame` 里用 `pack(side="right"/"left")` 决定左右，`wraplength`
控制最大宽度。这才是真的气泡（虽然圆角需要额外画，这里用平角）。

## 流式怎么追加

助手气泡是一个 Label，`wraplength` 固定、`text` 逐段追加即可。
tkinter 会自动重新计算高度，帧布局跟着调整。生成中末尾显示 `▌`，
结束时去掉。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Optional

from localai import APIError, ChatRequest, Message, StreamEventType

if TYPE_CHECKING:  # pragma: no cover
    from app import App

# 配色
BG_CHAT = "#ffffff"
BG_USER = "#c8e6c9"      # 浅绿：我方消息
BG_AI = "#eceff1"        # 浅灰：助手消息
BG_ERROR = "#ffebee"
BG_SYS = "#fff8e1"
FG_ROLE = "#5f6368"
FG_META = "#78909c"
FG_ERR = "#b00020"
FG_WARN = "#a15c00"
CURSOR = "▌"

# 气泡最大宽度占可视区域的比例
BUBBLE_RATIO = 0.72

PRESETS = [
    "把订单导出成 Excel，用一句话说怎么做",
    "我叫李娜。",
    "我叫什么名字？",
    "数一到十，用空格分隔",
]


class ChatPanel(ttk.Frame):
    """聊天框式的多轮对话面板。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app

        # 对话历史。每轮成功后追加 user + assistant，下次请求整段带上。
        # 服务端不保存会话状态，history 由客户端维护——这就是多轮的全部。
        self._history: list[Message] = []

        # 流式状态
        self._stop = threading.Event()
        self._running = False
        self._deltas: queue.Queue[str] = queue.Queue()
        self._finished: queue.Queue[tuple] = queue.Queue()
        self._pending_prompt = ""

        # 当前正在生长的助手气泡
        self._live_bubble: Optional[tk.Label] = None
        self._live_text = ""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_header()
        self._build_chat()
        self._build_input()

        self._drain()

    # -- 布局 ----------------------------------------------------------

    def _build_header(self) -> None:
        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.columnconfigure(1, weight=1)

        ttk.Label(head, text="对话（多轮）", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w")

        self._lbl_rounds = ttk.Label(head, text="尚无历史", style="Hint.TLabel")
        self._lbl_rounds.grid(row=0, column=1, sticky="w", padx=(12, 0))

        ttk.Button(head, text="清空对话", command=self._clear_chat).grid(
            row=0, column=2, sticky="e")

    def _build_chat(self) -> None:
        wrap = ttk.Frame(self)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(wrap, bg=BG_CHAT, highlightthickness=0,
                                 borderwidth=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # 所有消息放在这个 Frame 里，由 Canvas 承载以获得滚动能力
        self._body = tk.Frame(self._canvas, bg=BG_CHAT)
        self._body_id = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")

        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._body.bind("<Configure>", self._on_body_resize)

        # 鼠标滚轮
        self._canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

        # 空状态提示
        self._empty_hint = tk.Label(
            self._body,
            text="还没有对话。\n在下面输入问题开始，或用「预设」快速试一个。",
            bg=BG_CHAT, fg=FG_META, justify="center",
            font=self._font(10),
        )
        self._empty_hint.pack(pady=40)

    def _build_input(self) -> None:
        bottom = ttk.Frame(self)
        bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)

        entry_wrap = ttk.Frame(bottom)
        entry_wrap.grid(row=0, column=0, sticky="ew")
        entry_wrap.columnconfigure(0, weight=1)

        self._input = tk.Text(entry_wrap, height=3, wrap="word",
                              relief="solid", borderwidth=1,
                              padx=8, pady=6, font=self._font(10))
        self._input.grid(row=0, column=0, sticky="ew")
        self._input.bind("<Return>", self._on_return)

        btns = ttk.Frame(entry_wrap)
        btns.grid(row=0, column=1, sticky="ns", padx=(8, 0))

        self._btn_send = ttk.Button(btns, text="发送", width=8,
                                    command=self._do_send)
        self._btn_send.pack(fill="x")

        self._btn_stop = ttk.Button(btns, text="停止", width=8,
                                    command=self._do_stop, state="disabled")
        self._btn_stop.pack(fill="x", pady=(4, 0))

        opts = ttk.Frame(bottom)
        opts.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        ttk.Label(opts, text="预设", style="Hint.TLabel").pack(side="left")
        self._preset = ttk.Combobox(opts, values=PRESETS, width=32,
                                    state="readonly")
        self._preset.pack(side="left", padx=(4, 12))
        self._preset.bind("<<ComboboxSelected>>", self._on_preset)

        ttk.Label(opts, text="max_tokens", style="Hint.TLabel").pack(side="left")
        self._var_max = tk.StringVar(value="160")
        ttk.Spinbox(opts, from_=8, to=2048, increment=8, width=6,
                    textvariable=self._var_max).pack(side="left", padx=(4, 12))

        ttk.Label(opts, text="temperature", style="Hint.TLabel").pack(side="left")
        self._var_temp = tk.StringVar(value="0.0")
        ttk.Spinbox(opts, from_=0.0, to=2.0, increment=0.1, width=5,
                    textvariable=self._var_temp).pack(side="left", padx=(4, 12))

        self._var_degrade = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="允许降级", variable=self._var_degrade).pack(
            side="left")

        ttk.Label(opts, text="Enter 发送 · Shift+Enter 换行",
                  style="Hint.TLabel").pack(side="right")

    # -- 自适应 --------------------------------------------------------

    @staticmethod
    def _font(size: int) -> tuple:
        import sys
        if sys.platform == "win32":
            return ("Microsoft YaHei UI", size)
        return ("TkDefaultFont", size)

    def _on_canvas_resize(self, event: tk.Event) -> None:
        """窗口变宽时让消息区与气泡跟着变。"""
        self._canvas.itemconfigure(self._body_id, width=event.width)
        wrap = max(160, int(event.width * BUBBLE_RATIO))
        for lbl in self._bubble_labels():
            lbl.configure(wraplength=wrap)

    def _on_body_resize(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_wheel(self, event: tk.Event) -> None:
        """只在鼠标位于聊天区时滚动。"""
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if widget is None:
            return
        w = widget
        while w is not None:
            if w is self._canvas:
                self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
                return
            w = w.master

    def _bubble_labels(self) -> list[tk.Label]:
        """收集所有气泡 Label（用于统一调整换行宽度）。"""
        out: list[tk.Label] = []

        def walk(w: tk.Misc) -> None:
            for child in w.winfo_children():
                if isinstance(child, tk.Label) and getattr(
                        child, "_is_bubble", False):
                    out.append(child)
                walk(child)

        walk(self._body)
        return out

    def _bubble_width(self) -> int:
        width = self._canvas.winfo_width()
        if width <= 1:
            width = max(400, self.winfo_width())
        return max(160, int(width * BUBBLE_RATIO))

    def _at_bottom(self) -> bool:
        """用户是否已在底部。往上翻历史时不该被强行拽回底部。"""
        try:
            return self._canvas.yview()[1] > 0.98
        except tk.TclError:
            return True

    def _to_bottom(self, was_at_bottom: bool) -> None:
        if was_at_bottom:
            self._canvas.update_idletasks()
            self._canvas.yview_moveto(1.0)

    # -- 消息渲染 ------------------------------------------------------

    def _hide_empty_hint(self) -> None:
        if self._empty_hint is not None:
            self._empty_hint.destroy()
            self._empty_hint = None

    def _add_row(self, *, side: str) -> tk.Frame:
        """新建一行容器。气泡在里面按 side 靠左或靠右。"""
        row = tk.Frame(self._body, bg=BG_CHAT)
        row.pack(fill="x", padx=10, pady=4)
        return row

    def _add_bubble(
        self,
        text: str,
        *,
        side: str,
        bg: str,
        fg: str = "#202124",
        role: str = "",
        meta: str = "",
    ) -> tk.Label:
        """添加一条消息（角色名 + 气泡 + 可选脚注）。"""
        self._hide_empty_hint()
        at_bottom = self._at_bottom()

        row = self._add_row(side=side)
        anchor = "e" if side == "right" else "w"

        # 角色名在气泡上方，靠同一侧
        if role:
            tk.Label(row, text=role, bg=BG_CHAT, fg=FG_ROLE,
                     font=self._font(8), anchor=anchor).pack(
                fill="x", anchor=anchor)

        lbl = tk.Label(
            row, text=text, bg=bg, fg=fg,
            justify="left", anchor="w",
            padx=12, pady=8,
            wraplength=self._bubble_width(),
            font=self._font(10),
        )
        lbl._is_bubble = True          # 供 _bubble_labels 识别
        lbl.pack(anchor=anchor)

        if meta:
            tk.Label(row, text=meta, bg=BG_CHAT, fg=FG_META,
                     font=self._font(8), anchor=anchor).pack(
                fill="x", anchor=anchor)

        self._to_bottom(at_bottom)
        return lbl

    def _add_system(self, text: str, fg: str = FG_META) -> None:
        """居中系统提示（不属于任何一方）。"""
        self._hide_empty_hint()
        at_bottom = self._at_bottom()
        row = self._add_row(side="left")
        tk.Label(row, text=text, bg=BG_SYS, fg=fg, font=self._font(8),
                 padx=10, pady=4, justify="center").pack(anchor="center")
        self._to_bottom(at_bottom)

    def _update_rounds(self) -> None:
        n = len(self._history) // 2
        if n == 0:
            self._lbl_rounds.configure(text="尚无历史")
        else:
            self._lbl_rounds.configure(
                text=f"已进行 {n} 轮 · 下次请求会带上全部 "
                     f"{len(self._history)} 条消息")

    def _clear_chat(self) -> None:
        if self._running:
            self._app.status.warn("生成中，请先停止")
            return
        for child in self._body.winfo_children():
            child.destroy()
        self._history.clear()
        self._live_bubble = None
        self._update_rounds()
        self._empty_hint = tk.Label(
            self._body,
            text="还没有对话。\n在下面输入问题开始，或用「预设」快速试一个。",
            bg=BG_CHAT, fg=FG_META, justify="center",
            font=self._font(10),
        )
        self._empty_hint.pack(pady=40)
        self._app.status.info("已清空对话")

    # -- 输入 ----------------------------------------------------------

    def _on_preset(self, _event: object = None) -> None:
        text = self._preset.get()
        if text:
            self._input.delete("1.0", "end")
            self._input.insert("1.0", text)
            self._input.focus_set()

    def _on_return(self, event: tk.Event) -> Optional[str]:
        """Enter 发送，Shift+Enter 换行。"""
        if event.state & 0x1:
            return None
        self._do_send()
        return "break"

    # -- 发送 / 停止 ---------------------------------------------------

    def _do_send(self) -> None:
        if self._running:
            return
        client = self._app.require_client()
        if client is None:
            return

        prompt = self._input.get("1.0", "end-1c").strip()
        if not prompt:
            self._app.status.warn("请先输入内容")
            return

        try:
            max_tokens = int(self._var_max.get())
        except ValueError:
            max_tokens = 160
        try:
            temperature = float(self._var_temp.get())
        except ValueError:
            temperature = 0.0

        self._input.delete("1.0", "end")
        self._pending_prompt = prompt
        self._add_bubble(prompt, side="right", bg=BG_USER, role="我")

        messages = [*self._history, Message("user", prompt)]
        req = ChatRequest(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            allow_degrade=self._var_degrade.get(),
        )

        self._running = True
        self._stop.clear()
        self._drain_queue()
        self._btn_send.configure(state="disabled")
        self._btn_stop.configure(state="normal")

        # 先放一个空的助手气泡，流式增量往它的 text 上追加
        self._live_text = ""
        self._live_bubble = self._add_bubble(
            CURSOR, side="left", bg=BG_AI, role="助手")

        self._app.runner.begin("生成中")
        threading.Thread(target=self._stream_worker,
                         args=(client, req), daemon=True).start()

    def _stream_worker(self, client: object, req: ChatRequest) -> None:
        parts: list[str] = []
        finish = ""
        degraded = False
        model = ""
        warnings: list[str] = []
        usage = None
        try:
            with client.stream_events(req) as it:   # type: ignore[attr-defined]
                for ev in it:
                    if self._stop.is_set():
                        break
                    if ev.type == StreamEventType.DELTA and ev.text:
                        parts.append(ev.text)
                        self._deltas.put(ev.text)
                    elif ev.type == StreamEventType.META:
                        degraded = ev.degraded
                        model = ev.model
                    elif ev.type == StreamEventType.USAGE:
                        usage = ev.usage
                    elif ev.type == StreamEventType.WARNING:
                        warnings.extend(ev.warnings)
                    elif ev.type == StreamEventType.DONE:
                        finish = ev.finish_reason
                    if self._stop.is_set():
                        break
            self._finished.put(("ok", {
                "content": "".join(parts),
                "finish": finish or "stop",
                "degraded": degraded,
                "model": model,
                "warnings": warnings,
                "usage": usage,
            }))
        except APIError as exc:
            self._finished.put(("api_error", {"exc": exc,
                                              "partial": "".join(parts)}))
        except BaseException as exc:
            self._finished.put(("error", {"exc": exc,
                                          "partial": "".join(parts)}))

    def _do_stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        self._btn_stop.configure(state="disabled")
        self._app.status.warn("正在停止…")

    # -- 主线程消费 ----------------------------------------------------

    def _drain(self) -> None:
        try:
            while True:
                self._append_delta(self._deltas.get_nowait())
        except queue.Empty:
            pass

        try:
            kind, payload = self._finished.get_nowait()
        except queue.Empty:
            pass
        else:
            self._on_finish(kind, payload)

        self.after(40, self._drain)

    def _append_delta(self, delta: str) -> None:
        if self._live_bubble is None:
            return
        self._live_text += delta
        at_bottom = self._at_bottom()
        self._live_bubble.configure(text=self._live_text + CURSOR)
        self._to_bottom(at_bottom)

    def _finish_live_bubble(self, *, remove_cursor: bool = True,
                            fg: Optional[str] = None) -> None:
        if self._live_bubble is None:
            return
        text = self._live_text
        if remove_cursor:
            self._live_bubble.configure(text=text or "(无输出)")
        else:
            self._live_bubble.configure(text=text + CURSOR)
        if fg:
            self._live_bubble.configure(fg=fg)
        self._live_bubble = None

    def _on_finish(self, kind: str, payload: dict) -> None:
        self._running = False
        self._btn_send.configure(state="normal")
        self._btn_stop.configure(state="disabled")
        self._app.runner.end("生成中")

        stopped = self._stop.is_set()
        content = payload.get("content") or ""

        if kind == "ok":
            if stopped and not content:
                # 一个字都没产出：直接把空气泡去掉
                if self._live_bubble is not None:
                    self._live_bubble.master.destroy()
                    self._live_bubble = None
            else:
                self._finish_live_bubble()

            # 只有完整成功的一轮才进历史——被停止或失败时不进，
            # 否则残缺的助手回答会污染后续几轮的上下文。
            if not stopped and content:
                self._history.append(Message("user", self._pending_prompt))
                self._history.append(Message("assistant", content))
                self._update_rounds()

            if stopped:
                self._add_system("已停止（本次内容不进历史）", FG_WARN)
                self._app.status.warn("已停止")
            elif payload.get("degraded") or payload.get("finish") == "length" \
                    or payload.get("warnings"):
                notes = []
                if payload.get("degraded"):
                    notes.append("已降级：质量可能下降")
                if payload.get("finish") == "length":
                    notes.append("输出达到长度上限，被截断")
                for w in payload.get("warnings") or []:
                    notes.append(w)
                self._add_system(" · ".join(notes), FG_WARN)
                self._app.status.ok("生成完成（有提示）")
            else:
                self._app.status.ok("生成完成")
            return

        # 失败
        self._finish_live_bubble(fg=FG_ERR)
        exc = payload.get("exc")
        if isinstance(exc, APIError):
            detail = exc.kind.name
            if exc.message:
                detail += f"：{exc.message}"
            if exc.retryable:
                detail += "（可重试）"
            else:
                detail += "（不可重试）"
            self._add_system(detail, FG_ERR)
            self._app.status.error("生成失败")
        else:
            self._add_system(f"意外错误：{exc}", FG_ERR)
            self._app.status.error("意外错误")
        if isinstance(exc, BaseException):
            self._app.notify_error(exc)

    def _drain_queue(self) -> None:
        while not self._deltas.empty():
            try:
                self._deltas.get_nowait()
            except queue.Empty:
                break
        while not self._finished.empty():
            try:
                self._finished.get_nowait()
            except queue.Empty:
                break
