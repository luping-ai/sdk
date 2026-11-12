"""面板：对话（流式实时显示）。

这是 GUI 相对命令行最有价值的地方：流式输出逐字出现，用户能立刻看到
第一个字，而不是对着空白等待整段生成完。

## 难点：怎么在流式过程中更新界面

SDK 的 chat_stream 是阻塞迭代器，必须在后台线程里跑。但每收到一段文本
都要更新 Text 控件——那是主线程的事。

做法：后台线程把增量塞进 queue，主线程用 after() 定时取出并插入。
这样既不阻塞界面，也不违反 tkinter 的单线程约束。

## 难点：怎么"停止"

正在迭代的生成器不能被外部强行打断，只能让它自己检查一个标志。
所以停止按钮只设置 self._stop，后台循环每收一段检查一次；一旦为真就
close() 迭代器（这会断开 HTTP 连接，服务端感知断开后停止生成）。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from widgets import FieldGrid, scrollable_text, set_text

from localai import APIError, ChatRequest, Message

if TYPE_CHECKING:  # pragma: no cover
    from app import App

PRESETS = {
    "普通问答": "把订单导出成 Excel，用一句话说怎么做",
    "多轮对话（记上文）": "我叫李娜。",
    "要求逐字输出": "数一到十，用空格分隔",
}


class ChatPanel(ttk.Frame):
    """对话面板。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app

        # 流式状态
        self._stop = threading.Event()
        self._running = False
        self._deltas: queue.Queue[str] = queue.Queue()
        self._finished: queue.Queue[tuple] = queue.Queue()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, text="对话（流式）", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w")

        ttk.Label(
            self,
            text="流式不只是「边生成边显示」：还能看到排队位置、实际用的模型、"
                 "是否降级。点「停止」会断开连接，服务端随即停止生成。",
            style="Hint.TLabel", wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        # -- 输入区 ----------------------------------------------------
        form = ttk.LabelFrame(self, text="输入", padding=(10, 6))
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="提问", style="Hint.TLabel", width=10,
                  anchor="nw").grid(row=0, column=0, sticky="nw")
        self._input = tk.Text(form, height=4, wrap="word", relief="solid",
                              borderwidth=1, padx=6, pady=4)
        self._input.grid(row=0, column=1, sticky="ew")
        self._input.insert("1.0", PRESETS["普通问答"])

        opts = ttk.Frame(form)
        opts.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        ttk.Label(opts, text="预设", style="Hint.TLabel").pack(side="left")
        self._preset = ttk.Combobox(opts, values=list(PRESETS), width=18,
                                    state="readonly")
        self._preset.set("普通问答")
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

        # -- 按钮 ------------------------------------------------------
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 6))

        self._btn_send = ttk.Button(actions, text="发送", command=self._do_send)
        self._btn_send.pack(side="left")

        self._btn_stop = ttk.Button(actions, text="停止", command=self._do_stop,
                                   state="disabled")
        self._btn_stop.pack(side="left", padx=(8, 0))

        ttk.Button(actions, text="清空输出",
                   command=lambda: set_text(self._output, "")).pack(
            side="left", padx=(8, 0))

        # -- 输出区 ----------------------------------------------------
        out = ttk.Panedwindow(self, orient="vertical")
        out.grid(row=4, column=0, sticky="nsew")

        top = ttk.LabelFrame(out, text="回答", padding=(10, 6))
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        box, self._output = scrollable_text(top, height=12)
        box.grid(row=0, column=0, sticky="nsew")
        out.add(top, weight=3)

        bottom = ttk.LabelFrame(out, text="本次结果", padding=(10, 6))
        bottom.columnconfigure(0, weight=1)
        self._fields = FieldGrid(bottom, label_width=14)
        self._fields.grid(row=0, column=0, sticky="ew")
        out.add(bottom, weight=1)

        # 主线程定时把后台产出的增量搬到界面
        self._drain()

    # -- 预设 ----------------------------------------------------------

    def _on_preset(self, _event: object = None) -> None:
        text = PRESETS.get(self._preset.get())
        if text:
            self._input.delete("1.0", "end")
            self._input.insert("1.0", text)

    # -- 发送 / 停止 ---------------------------------------------------

    def _do_send(self) -> None:
        client = self._app.require_client()
        if client is None or self._running:
            return

        prompt = self._input.get("1.0", "end-1c").strip()
        if not prompt:
            self._app.status.warn("请先输入问题")
            return

        try:
            max_tokens = int(self._var_max.get())
        except ValueError:
            max_tokens = 160
        try:
            temperature = float(self._var_temp.get())
        except ValueError:
            temperature = 0.0

        # 「多轮对话」预设：带上历史，验证 SDK 能记住上文
        is_multi = self._preset.get() == "多轮对话（记上文）"
        if is_multi:
            messages = [
                Message("user", prompt),
                Message("assistant", "你好，李娜。"),
                Message("user", "我叫什么名字？"),
            ]
        else:
            messages = [Message("user", prompt)]

        req = ChatRequest(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            allow_degrade=self._var_degrade.get(),
        )

        self._running = True
        self._stop.clear()
        self._drain_queue()
        set_text(self._output, "")
        self._fields.clear()
        self._btn_send.configure(state="disabled")
        self._btn_stop.configure(state="normal")

        # 流式不走 runner.submit（那会在忙碌期间禁用所有按钮，而这里
        # 必须让「停止」可用）。用 runner.begin/end 手动配对，这样它
        # 与其它后台任务共享同一套 pending 计数，不会互相覆盖忙碌提示。
        self._app.runner.begin("生成中")

        threading.Thread(target=self._stream_worker,
                         args=(client, req), daemon=True).start()

    def _stream_worker(self, client: object, req: ChatRequest) -> None:
        """后台线程：跑流式请求，把增量与最终状态放进队列。"""
        parts: list[str] = []
        meta: dict = {}
        usage = None
        warnings: list[str] = []
        finish = ""
        try:
            with client.chat_stream(req) as it:   # type: ignore[attr-defined]
                for delta in it:
                    if self._stop.is_set():
                        break
                    parts.append(delta)
                    self._deltas.put(delta)
                    if self._stop.is_set():
                        break
            self._finished.put(("ok", {
                "content": "".join(parts),
                "finish": finish or "stop",
                "meta": meta,
                "usage": usage,
                "warnings": warnings,
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
        self._app.status.warn("已请求停止…")

    # -- 主线程消费 ----------------------------------------------------

    def _drain(self) -> None:
        """每 40ms 把队列里的增量搬进 Text。

        比 50ms 略快：流式的「跟手」感直接取决于它。
        """
        try:
            while True:
                delta = self._deltas.get_nowait()
                self._output.insert("end", delta)
                self._output.see("end")
        except queue.Empty:
            pass

        try:
            kind, payload = self._finished.get_nowait()
        except queue.Empty:
            pass
        else:
            self._on_finish(kind, payload)

        self.after(40, self._drain)

    def _on_finish(self, kind: str, payload: dict) -> None:
        self._running = False
        self._btn_send.configure(state="normal")
        self._btn_stop.configure(state="disabled")
        self._app.runner.end("生成中")

        if kind == "ok":
            self._render_summary(payload)
            stopped = self._stop.is_set()
            if stopped:
                self._app.status.warn("已停止（连接已断开，服务端停止生成）")
                self._fields.set("状态", "已由用户停止", style="Warn.TLabel")
            else:
                self._app.status.ok("生成完成")
            return

        exc = payload.get("exc")
        partial = payload.get("partial") or ""
        if partial and not self._output.get("1.0", "end-1c"):
            self._output.insert("1.0", partial)

        if isinstance(exc, APIError):
            self._fields.set("状态", f"失败：{exc.kind.name}",
                             style="Error.TLabel")
            self._fields.set("原因", exc.message)
            self._app.status.error("生成失败")
        else:
            self._app.status.error("意外错误")
        if isinstance(exc, BaseException):
            self._app.notify_error(exc)

    def _render_summary(self, payload: dict) -> None:
        f = self._fields
        content = payload.get("content") or ""
        finish = payload.get("finish") or ""
        f.set("字符数", len(content))
        f.set("结束原因", finish + ("（达到上限，被截断）"
                                    if finish == "length" else ""))
        if payload.get("warnings"):
            f.set("服务端提示", "；".join(payload["warnings"]))

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
