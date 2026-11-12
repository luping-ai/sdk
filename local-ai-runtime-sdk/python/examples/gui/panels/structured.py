"""面板：结构化输出（约束解码）。

## 这个面板为什么值得单独做

约束解码是本地小模型上最容易踩坑的功能，而且失败方式很隐蔽：
JSON 断在字符串中间，报错信息与真实原因（模型在数组里无限重复）
毫无关系。

所以面板里放了两个可切换的 schema：

  · 有上限（推荐）——带 maxItems / maxLength
  · 无上限（对照）——只写 type，没有长度约束

切换着跑一次，就能亲眼看到后者卡二十几秒然后产出非法 JSON。
这比读十遍文档都直接。
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from widgets import BusyButton, FieldGrid, scrollable_text, set_text

from localai import ChatRequest, Message

if TYPE_CHECKING:  # pragma: no cover
    from app import App

# 带约束的 schema：能稳定产出合法 JSON
SCHEMA_BOUNDED = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "maxLength": 40,
            "description": "用户意图的机器可读标识",
        },
        "confidence": {"type": "number"},
        "keywords": {
            "type": "array",
            "items": {"type": "string", "maxLength": 20},
            "maxItems": 4,
        },
    },
    "required": ["intent", "confidence", "keywords"],
}

# 对照用的 schema：故意不设上限，会复现"重复到截断"
SCHEMA_UNBOUNDED = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "confidence", "keywords"],
}


class StructuredPanel(ttk.Frame):
    """结构化输出演示。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        ttk.Label(self, text="结构化输出（约束解码）",
                  style="Heading.TLabel").grid(row=0, column=0, sticky="w")

        ttk.Label(
            self,
            text="传 json_schema 后，模型在生成时只能产出符合 schema 的 token，"
                 "不需要「生成完再校验、不合格就重试」。",
            style="Hint.TLabel", wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        # -- 输入 ------------------------------------------------------
        form = ttk.LabelFrame(self, text="输入", padding=(10, 6))
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="文本", style="Hint.TLabel", width=10,
                  anchor="nw").grid(row=0, column=0, sticky="nw")
        self._input = tk.Text(form, height=2, wrap="word", relief="solid",
                              borderwidth=1, padx=6, pady=4)
        self._input.grid(row=0, column=1, sticky="ew")
        self._input.insert("1.0", "帮我把这个月的销售订单导出成 Excel 报表")

        opts = ttk.Frame(form)
        opts.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        ttk.Label(opts, text="Schema", style="Hint.TLabel").pack(side="left")
        self._var_mode = tk.StringVar(value="bounded")
        ttk.Radiobutton(opts, text="有上限（推荐）", value="bounded",
                        variable=self._var_mode,
                        command=self._refresh_schema).pack(side="left", padx=(4, 8))
        ttk.Radiobutton(opts, text="无上限（对照，会失败）", value="unbounded",
                        variable=self._var_mode,
                        command=self._refresh_schema).pack(side="left")

        ttk.Label(opts, text="max_tokens", style="Hint.TLabel").pack(
            side="left", padx=(16, 4))
        self._var_max = tk.StringVar(value="300")
        ttk.Spinbox(opts, from_=16, to=2048, increment=16, width=6,
                    textvariable=self._var_max).pack(side="left")

        # -- 按钮 ------------------------------------------------------
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        BusyButton(actions, runner=app.runner, text="运行",
                   command=self._do_run).pack(side="left")
        ttk.Button(actions, text="清空",
                   command=self._clear).pack(side="left", padx=(8, 0))

        hint = ttk.Label(
            actions,
            text="切到「无上限」再跑一次，能看到模型在数组里重复到被截断",
            style="Warn.TLabel")
        hint.pack(side="left", padx=(12, 0))

        # -- Schema 预览 -----------------------------------------------
        schema_box = ttk.LabelFrame(self, text="当前 Schema", padding=(10, 6))
        schema_box.grid(row=4, column=0, sticky="ew")
        schema_box.columnconfigure(0, weight=1)
        sbox, self._schema_text = scrollable_text(schema_box, height=6, mono=True)
        sbox.grid(row=0, column=0, sticky="ew")

        # -- 结果 ------------------------------------------------------
        out = ttk.Panedwindow(self, orient="vertical")
        out.grid(row=5, column=0, sticky="nsew", pady=(6, 0))

        raw_box = ttk.LabelFrame(out, text="模型原始输出", padding=(10, 6))
        raw_box.rowconfigure(0, weight=1)
        raw_box.columnconfigure(0, weight=1)
        rbox, self._raw = scrollable_text(raw_box, height=8, mono=True)
        rbox.grid(row=0, column=0, sticky="nsew")
        out.add(raw_box, weight=2)

        parsed_box = ttk.LabelFrame(out, text="解析结果", padding=(10, 6))
        parsed_box.columnconfigure(0, weight=1)
        self._fields = FieldGrid(parsed_box, label_width=14)
        self._fields.grid(row=0, column=0, sticky="ew")
        out.add(parsed_box, weight=1)

        self._refresh_schema()

    # -- Schema --------------------------------------------------------

    def _current_schema(self) -> dict:
        return (SCHEMA_BOUNDED if self._var_mode.get() == "bounded"
                else SCHEMA_UNBOUNDED)

    def _refresh_schema(self) -> None:
        set_text(self._schema_text,
                 json.dumps(self._current_schema(), ensure_ascii=False,
                            indent=2))

    def _clear(self) -> None:
        set_text(self._raw, "")
        self._fields.clear()

    # -- 运行 ----------------------------------------------------------

    def _do_run(self) -> None:
        client = self._app.require_client()
        if client is None:
            return

        text = self._input.get("1.0", "end-1c").strip()
        if not text:
            self._app.status.warn("请先输入文本")
            return

        try:
            max_tokens = int(self._var_max.get())
        except ValueError:
            max_tokens = 300

        schema = self._current_schema()
        mode = self._var_mode.get()

        def work() -> object:
            return client.chat(ChatRequest(
                messages=[Message("user", text)],
                json_schema=schema,
                max_tokens=max_tokens,
                temperature=0.0,
            ))

        def done(res: object) -> None:
            self._render(res, mode)

        def error(exc: BaseException) -> None:
            self._app.status.error("运行失败")
            self._app.notify_error(exc)

        # 忙碌状态由 runner 统一管，这里只声明"在等什么"
        self._app.runner.submit(work, on_done=done, on_error=error,
                                busy_text="结构化生成中")

    def _render(self, res: object, mode: str) -> None:
        content = res.content                      # type: ignore[attr-defined]
        set_text(self._raw, content)

        f = self._fields
        f.clear()
        f.set("字符数", len(content))
        finish = res.finish_reason                 # type: ignore[attr-defined]
        f.set("结束原因", finish)
        if res.truncated:                          # type: ignore[attr-defined]
            f.set("截断", "是（撞上 max_tokens）", style="Warn.TLabel")

        try:
            data = json.loads(content)
        except ValueError as exc:
            f.set("JSON 合法性", "不是合法 JSON", style="Error.TLabel")
            f.set("解析错误", str(exc))
            self._explain_failure(mode, res)
            self._app.status.error("解析失败——见下方说明")
            return

        f.set("JSON 合法性", "合法", style="OK.TLabel")
        for key, value in data.items():
            if isinstance(value, list):
                f.set(key, f"{value}（{len(value)} 项）")
            else:
                f.set(key, value)

        if mode == "unbounded":
            f.set("备注", "这次侥幸成功了——但无上限时不保证，重跑几次很容易失败")
        self._app.status.ok("约束解码产出合法 JSON")

    def _explain_failure(self, mode: str, res: object) -> None:
        """给出针对性解释，而不是让用户对着报错发呆。"""
        if mode == "unbounded":
            self._app.status.warn(
                "这是「无上限」schema 的预期结果：模型在数组里重复合法内容，"
                "一直撞到 max_tokens 被截断")
            self._fields.set(
                "说明",
                "把 Schema 切到「有上限」再跑一次作对比。"
                "约束解码能阻止模型输出 schema 外的内容，"
                "但挡不住它无限重复合法内容。")
        else:
            self._fields.set(
                "说明",
                "有上限的 schema 理论上不该失败。若仍失败，检查引擎是否"
                "启用了约束解码，或 schema 里是否有 oneOf 等不支持的关键字。")
