"""面板：知识库（RAG）。

演示从建库到 RAG 问答的完整链路，并把这些"正常但反直觉"的状态
明确标出来：

  · duplicate=True 不是错误（内容 SHA-256 重复）
  · model_unavailable 不是配置错误（没配 Embedding，关键词检索仍可用）
  · 检索 degraded=True 不是失败（只走了关键词通道）
  · used_chunks=0 可能是因为 context_tokens 小于固定开销
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tkinter as tk
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING, Optional

from widgets import BusyButton, FieldGrid, labeled_entry, scrollable_text, set_text

from localai import ChatRequest, ImportRequest, Message, SearchRequest

if TYPE_CHECKING:  # pragma: no cover
    from app import App


class KnowledgePanel(ttk.Frame):
    """知识库全流程。"""

    def __init__(self, parent: tk.Misc, *, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self._kb_id: Optional[str] = None
        self._sample_path: Optional[str] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, text="知识库（RAG）", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w")

        ttk.Label(
            self,
            text="导入文档后，检索会自动完成「关键词 + 语义」混合召回。"
                 "对话时传 kb_ids 即自动检索并注入，不必自己拼 prompt。",
            style="Hint.TLabel", wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        # -- 步骤 1：建库 ----------------------------------------------
        step1 = ttk.LabelFrame(self, text="1. 建库", padding=(10, 6))
        step1.grid(row=2, column=0, sticky="ew")
        step1.columnconfigure(1, weight=1)

        row, self._var_name, _ = labeled_entry(
            step1, "库名", initial="demo-gui", width=30)
        row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)

        btns1 = ttk.Frame(step1)
        btns1.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        BusyButton(btns1, runner=app.runner, text="创建",
                   command=self._do_create).pack(side="left")
        BusyButton(btns1, runner=app.runner, text="列出已有库",
                   command=self._do_list).pack(side="left", padx=(8, 0))
        BusyButton(btns1, runner=app.runner, text="删除当前库",
                   command=self._do_delete).pack(side="left", padx=(8, 0))

        self._lbl_kb = ttk.Label(btns1, text="当前：未创建", style="Hint.TLabel")
        self._lbl_kb.pack(side="left", padx=(12, 0))

        # -- 步骤 2：导入 ----------------------------------------------
        step2 = ttk.LabelFrame(self, text="2. 导入文档", padding=(10, 6))
        step2.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        step2.columnconfigure(0, weight=1)

        import_row = ttk.Frame(step2)
        import_row.grid(row=0, column=0, sticky="ew")
        BusyButton(import_row, runner=app.runner, text="导入示例文本",
                   command=self._do_import_sample).pack(side="left")
        BusyButton(import_row, runner=app.runner, text="选择文件导入…",
                   command=self._do_import_file).pack(side="left", padx=(8, 0))
        BusyButton(import_row, runner=app.runner, text="列出文档",
                   command=self._do_list_docs).pack(side="left", padx=(8, 0))

        ttk.Label(
            step2,
            text="导入传的是文件路径——Runtime 与本程序在同一台机器上，"
                 "传路径比传内容省内存。支持 .txt / .md / .pdf / .docx / .xlsx。",
            style="Hint.TLabel", wraplength=700, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # -- 步骤 3：检索 ----------------------------------------------
        step3 = ttk.LabelFrame(self, text="3. 检索与问答", padding=(10, 6))
        step3.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        step3.columnconfigure(1, weight=1)
        step3.rowconfigure(2, weight=1)

        ttk.Label(step3, text="查询", style="Hint.TLabel", width=10,
                  anchor="w").grid(row=0, column=0, sticky="w")
        self._var_query = tk.StringVar(value="保修期多久？")
        ttk.Entry(step3, textvariable=self._var_query).grid(
            row=0, column=1, sticky="ew")

        btns3 = ttk.Frame(step3)
        btns3.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 6))
        BusyButton(btns3, runner=app.runner, text="仅检索",
                   command=self._do_search).pack(side="left")
        BusyButton(btns3, runner=app.runner, text="检索 + RAG 上下文",
                   command=self._do_search_ctx).pack(side="left", padx=(8, 0))
        BusyButton(btns3, runner=app.runner, text="用知识库回答",
                   command=self._do_rag_chat).pack(side="left", padx=(8, 0))

        out = ttk.Panedwindow(step3, orient="vertical")
        out.grid(row=2, column=0, columnspan=2, sticky="nsew")

        top = ttk.Frame(out)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        tbox, self._result = scrollable_text(top, height=10)
        tbox.grid(row=0, column=0, sticky="nsew")
        out.add(top, weight=3)

        bottom = ttk.LabelFrame(out, text="状态", padding=(8, 4))
        bottom.columnconfigure(0, weight=1)
        self._fields = FieldGrid(bottom, label_width=16)
        self._fields.grid(row=0, column=0, sticky="ew")
        out.add(bottom, weight=1)

    # -- 工具 ----------------------------------------------------------

    def _need_kb(self) -> Optional[str]:
        if self._kb_id is None:
            self._app.status.warn("请先创建或选择一个知识库")
            return None
        return self._kb_id

    def _set_kb(self, kb_id: Optional[str], name: str = "") -> None:
        self._kb_id = kb_id
        if kb_id:
            self._lbl_kb.configure(text=f"当前：{name or '(无名)'}　{kb_id}")
        else:
            self._lbl_kb.configure(text="当前：未创建")

    def _write_sample(self) -> str:
        """写一个示例文档，返回路径。"""
        if self._sample_path and os.path.exists(self._sample_path):
            return self._sample_path
        fd, path = tempfile.mkstemp(suffix=".md", prefix="laih-gui-")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# 保修政策\n\n"
                "本产品保修期为十二个月，自购买之日起计算。\n\n"
                "保修期内非人为损坏免费维修，人为损坏需收取材料费。\n\n"
                "# 退货流程\n\n"
                "先提交退货申请，审核通过后三天内寄回。\n\n"
                "# 联系人\n\n"
                "客户李娜的联系电话是一三八一二三四五六七八。\n"
            )
        self._sample_path = path
        return path

    # -- 步骤 1 操作 ---------------------------------------------------

    def _do_create(self) -> None:
        client = self._app.require_client()
        if client is None:
            return
        name = self._var_name.get().strip() or "demo-gui"

        def work() -> object:
            return client.create_kb(name, description="界面演示创建")

        def done(kb: object) -> None:
            self._set_kb(kb.id, kb.name)          # type: ignore[attr-defined]
            self._app.status.ok(f"已创建 {kb.id}")  # type: ignore[attr-defined]

        def error(exc: BaseException) -> None:
            self._app.status.error("创建失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    def _do_list(self) -> None:
        client = self._app.require_client()
        if client is None:
            return

        def work() -> object:
            return client.list_kb()

        def done(bases: object) -> None:
            items = list(bases)                    # type: ignore[arg-type]
            if not items:
                set_text(self._result, "（还没有知识库）")
                self._app.status.info("0 个知识库")
                return
            lines = [f"{len(items)} 个知识库：", ""]
            for b in items:
                lines.append(f"· {b.id}")
                lines.append(f"  {b.name}　文档 {b.document_count}　"
                             f"切片 {b.chunk_count}　向量 {b.embedded_count}")
                if b.description:
                    lines.append(f"  {b.description}")
            set_text(self._result, "\n".join(lines))
            # 只有一个时顺手选中，省一次点击
            if len(items) == 1:
                self._set_kb(items[0].id, items[0].name)
            self._app.status.ok(f"{len(items)} 个知识库")

        def error(exc: BaseException) -> None:
            self._app.status.error("列出失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    def _do_delete(self) -> None:
        kb_id = self._need_kb()
        client = self._app.require_client()
        if kb_id is None or client is None:
            return

        def work() -> object:
            client.delete_kb(kb_id)
            return None

        def done(_: object) -> None:
            self._set_kb(None)
            set_text(self._result, f"已删除 {kb_id}")
            self._app.status.ok("已删除")

        def error(exc: BaseException) -> None:
            self._app.status.error("删除失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    # -- 步骤 2 操作 ---------------------------------------------------

    def _do_import_sample(self) -> None:
        self._import(self._write_sample(), "示例文档")

    def _do_import_file(self) -> None:
        kb_id = self._need_kb()
        if kb_id is None:
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="选择要导入的文档",
            filetypes=[
                ("支持的文档", "*.txt *.md *.pdf *.docx *.xlsx"),
                ("文本", "*.txt *.md"),
                ("PDF", "*.pdf"),
                ("Office", "*.docx *.xlsx"),
                ("全部文件", "*.*"),
            ],
        )
        if not path:
            return
        self._import(path, os.path.basename(path))

    def _import(self, path: str, label: str) -> None:
        kb_id = self._need_kb()
        client = self._app.require_client()
        if kb_id is None or client is None:
            return

        def work() -> object:
            return client.import_document(
                kb_id, ImportRequest(path=os.path.abspath(path)))

        def done(res: object) -> None:
            lines = [f"导入 {label}", ""]
            lines.append(f"文档 ID：{res.document_id}")     # type: ignore[attr-defined]
            lines.append(f"切片数：{res.chunk_count}")      # type: ignore[attr-defined]
            lines.append(f"字符数：{res.content_runes}")    # type: ignore[attr-defined]
            if res.duplicate:                              # type: ignore[attr-defined]
                lines.append("")
                lines.append("内容已存在，未重复导入（这不是错误）。")
                lines.append(f"已有文档：{res.existing_id}")  # type: ignore[attr-defined]
            if res.diff:                                   # type: ignore[attr-defined]
                lines.append(f"增量变更：{res.diff}")
            set_text(self._result, "\n".join(lines))
            self._app.status.ok(
                f"已导入，{res.chunk_count} 个切片")        # type: ignore[attr-defined]

        def error(exc: BaseException) -> None:
            self._app.status.error("导入失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error,
                                busy_text="导入中（解析 + 切片）")

    def _do_list_docs(self) -> None:
        kb_id = self._need_kb()
        client = self._app.require_client()
        if kb_id is None or client is None:
            return

        def work() -> object:
            return client.list_documents(kb_id), client.kb_stat(kb_id)

        def done(value: object) -> None:
            docs, stat = value                       # type: ignore[misc]
            lines = [f"文档 {len(docs)} 个", ""]
            for d in docs:
                lines.append(f"· {d.name}（{d.file_type}）")
                lines.append(f"  {d.chunk_count} 切片　"
                             f"状态 {d.status}　索引 {d.index_status}")
                if d.error:
                    lines.append(f"  错误：{d.error}")
            lines.append("")
            lines.append(f"统计：文档 {stat.document_count}　"
                         f"切片 {stat.chunk_count}　"
                         f"向量 {stat.embedded_count}　"
                         f"失效 {stat.stale_count}")
            if stat.vectorize:
                v = stat.vectorize
                model = f"　模型 {v.model}" if v.model else ""
                running = "（运行中）" if v.running else ""
                lines.append(f"向量化进度：{v.embedded_chunks}/{v.total_chunks}"
                             f"{running}{model}")
            set_text(self._result, "\n".join(lines))
            self._app.status.ok(f"{len(docs)} 个文档")

        def error(exc: BaseException) -> None:
            self._app.status.error("查询失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    # -- 步骤 3 操作 ---------------------------------------------------

    def _do_search(self) -> None:
        self._search(build_context=False)

    def _do_search_ctx(self) -> None:
        self._search(build_context=True)

    def _search(self, *, build_context: bool) -> None:
        kb_id = self._need_kb()
        client = self._app.require_client()
        if kb_id is None or client is None:
            return

        query = self._var_query.get().strip()
        if not query:
            self._app.status.warn("请先输入查询")
            return

        def work() -> object:
            return client.search(SearchRequest(
                query=query, kb_ids=[kb_id], top_k=5,
                build_context=build_context, context_tokens=800))

        def done(sr: object) -> None:
            self._render_search(sr)

        def error(exc: BaseException) -> None:
            self._app.status.error("检索失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error)

    def _render_search(self, sr: object) -> None:
        lines = [f"命中 {len(sr.hits)} 条　模式 {sr.mode}　"          # type: ignore[attr-defined]
                 f"耗时 {sr.total_ms} ms", ""]
        if sr.degraded:                                              # type: ignore[attr-defined]
            lines.append("（已降级：可能只走了关键词通道）")
            if sr.note:                                              # type: ignore[attr-defined]
                lines.append(f"服务端说明：{sr.note}")                # type: ignore[attr-defined]
            lines.append("")

        for i, h in enumerate(sr.hits, 1):                            # type: ignore[attr-defined]
            lines.append(f"[{i}] score={h.score:.4f}　"
                         f"关键词排名={h.keyword_rank}　语义排名={h.semantic_rank}")
            text = h.text.replace("\n", " ")
            lines.append(f"    {text[:160]}{'…' if len(text) > 160 else ''}")
            lines.append("")

        if sr.context:                                               # type: ignore[attr-defined]
            ctx = sr.context
            lines.append("—— RAG 上下文 ——")
            lines.append(f"注入 {ctx.used_chunks} 切片，丢弃 {ctx.dropped_chunks}，"
                         f"估算 {ctx.estimated_tokens} tokens，"
                         f"预算 {ctx.budget_tokens}，固定开销 "
                         f"{ctx.fixed_overhead_tokens}")
            if ctx.used_chunks == 0 and ctx.fixed_overhead_tokens:
                lines.append("注意：一条都没注入——很可能是 context_tokens "
                             "小于固定开销。")
            lines.append("")
            lines.append(ctx.text[:1200])

        set_text(self._result, "\n".join(lines))

        f = self._fields
        f.clear()
        f.set("命中数", len(sr.hits))                                # type: ignore[attr-defined]
        f.set("模式", sr.mode)                                       # type: ignore[attr-defined]
        f.set("降级", "是" if sr.degraded else "否")                   # type: ignore[attr-defined]
        if sr.context:                                               # type: ignore[attr-defined]
            f.set("注入切片", sr.context.used_chunks)
            f.set("固定开销", f"{sr.context.fixed_overhead_tokens} tokens")
        self._app.status.ok(f"检索完成，{len(sr.hits)} 条命中")        # type: ignore[attr-defined]

    def _do_rag_chat(self) -> None:
        kb_id = self._need_kb()
        client = self._app.require_client()
        if kb_id is None or client is None:
            return

        query = self._var_query.get().strip()
        if not query:
            self._app.status.warn("请先输入查询")
            return

        def work() -> object:
            return client.chat(ChatRequest(
                messages=[Message("user", query)],
                kb_ids=[kb_id], max_tokens=120))

        def done(res: object) -> None:
            lines = ["回答：", "", res.content, ""]                   # type: ignore[attr-defined]
            if res.knowledge:                                        # type: ignore[attr-defined]
                k = res.knowledge
                lines.append(f"检索使用 {k.used_chunks} 个切片，"
                             f"{k.context_tokens} tokens，"
                             f"耗时 {k.retrieval_ms} ms")
                for src in k.sources:
                    lines.append(f"  来源 {src.index}：{src.document_id} "
                                 f"score={src.score:.4f}")
            set_text(self._result, "\n".join(lines))
            self._fields.clear()
            if res.knowledge:                                        # type: ignore[attr-defined]
                self._fields.set("注入切片", res.knowledge.used_chunks)
                self._fields.set("上下文 tokens", res.knowledge.context_tokens)
            self._app.status.ok("问答完成")

        def error(exc: BaseException) -> None:
            self._app.status.error("问答失败")
            self._app.notify_error(exc)

        self._app.runner.submit(work, on_done=done, on_error=error,
                                busy_text="检索并生成")

    # -- 清理 ----------------------------------------------------------

    def cleanup(self) -> None:
        if self._sample_path:
            with contextlib.suppress(OSError):
                os.unlink(self._sample_path)
            self._sample_path = None
