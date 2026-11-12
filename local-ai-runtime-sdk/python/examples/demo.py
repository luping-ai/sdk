"""可运行的完整示例，兼作端到端冒烟测试。

把本文件里的所有能力串一遍：发现、能力查询、非流式对话、流式对话、
知识库全流程、错误处理。

运行：

    python examples/demo.py

前提：Local AI Hub Runtime 已启动（安装器会加自启项，或手动
`localai-cli serve`）。不需要设置任何环境变量——SDK 自动读
discovery 文件找到端口与 token。

## 为什么示例要写这么细

示例是文档里唯一"一定被运行过"的代码。README 的片段可能过期，
示例跑不通会立刻被发现。因此这里刻意覆盖边界：降级检查、
能力受限提示、错误分类。
"""

from __future__ import annotations

import os
import sys
import tempfile

from localai import (
    APIError,
    ChatRequest,
    Client,
    Config,
    ImportRequest,
    Message,
    SearchRequest,
    StreamEventType,
    is_retryable,
)


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main() -> int:
    # ------------------------------------------------------------------
    section("1. 连接（自动发现，不写死端口）")

    try:
        client = Client()
    except APIError as e:
        print(f"无法连接：{e}")
        return 1

    print(f"地址     {client.base_url}")
    print(f"版本     {client.discovery.version or '(未知)'}")

    try:
        health = client.health()
    except APIError as e:
        print(f"\n本地 AI 未运行：{e}")
        print("请先启动 Local AI Hub（或用安装器设为开机自启）。")
        return 1

    print(f"状态     {health.state}（ready={health.ready}）")
    if health.reason:
        print(f"说明     {health.reason}")

    # ------------------------------------------------------------------
    section("2. 这台机器能做什么")

    cap = client.capability()
    print(f"等级     {cap.level}")
    print(f"对话     {cap.chat_enabled}")
    print(f"知识库   {cap.rag_enabled}")
    print(f"说明     {cap.reason}")

    for w in cap.warnings:
        # 服务端给的提示不要静默丢弃——它可能解释了"为什么结果不对"
        print(f"提示     {w}")

    if not cap.chat_enabled:
        print("\n本机未启用模型对话，跳过对话部分。")
        print("（这不是错误：低内存机器只跑规则引擎是预期的降级）")
        return 0

    # ------------------------------------------------------------------
    section("3. 模型")

    models = client.models()
    print(f"共 {len(models)} 个模型，当前加载：{client.active_model() or '(无)'}")
    for m in models[:5]:
        flags = []
        if m.loaded:
            flags.append("已加载")
        if m.present:
            flags.append("已下载")
        tag = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {m.id:<40} {m.name}{tag}")
    if len(models) > 5:
        print(f"  …还有 {len(models) - 5} 个")

    # ------------------------------------------------------------------
    section("4. 非流式对话")

    try:
        res = client.chat(ChatRequest(
            messages=[Message("user", "把订单导出成 Excel，用一句话说怎么做")],
            max_tokens=120,
        ))
    except APIError as e:
        print(f"对话失败：{e}")
        print(f"可重试：{is_retryable(e)}")
        return 1

    print(f"回答     {res.content}")
    print(f"结束     {res.finish_reason}" +
          ("（达到长度上限，输出被截断）" if res.truncated else ""))
    if res.model:
        print(f"模型     {res.model.actual}")
    if res.usage:
        print(f"耗时     TTFT {res.usage.ttft_ms}ms / "
              f"总计 {res.usage.total_ms}ms"
              f"（排队 {res.usage.queue_wait_ms}ms）")
    print(f"追踪     {res.request_id}")

    # 降级不是错误，但要告诉用户
    if res.degraded:
        print(f"注意     已降级：{res.degrade_reason}")

    for w in res.warnings:
        print(f"提示     {w}")

    # ------------------------------------------------------------------
    section("5. 流式对话")

    print("回答     ", end="", flush=True)
    try:
        for ev in client.stream_events(ChatRequest(
                messages=[Message("user", "数一到五，只写数字")],
                max_tokens=32)):
            if ev.type == StreamEventType.QUEUED and ev.ahead > 0:
                print(f"\n[排队中，前面还有 {ev.ahead} 个任务]", end="")
            elif ev.type == StreamEventType.DELTA:
                print(ev.text, end="", flush=True)
            elif ev.type == StreamEventType.WARNING:
                print(f"\n[提示] {ev.text}")
    except APIError as e:
        print(f"\n流中断：{e}")
        return 1
    print()

    # ------------------------------------------------------------------
    section("6. 知识库：建库 → 导入 → 检索 → 对话增强 → 清理")

    kb = None
    path = None
    try:
        kb = client.create_kb("sdk-demo", description="示例知识库")
        print(f"建库     {kb.id}")

        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 保修政策\n\n"
                    "本产品保修期为十二个月，自购买之日起计算。\n"
                    "保修期内非人为损坏免费维修。\n")

        r = client.import_document(kb.id, ImportRequest(path=path))
        print(f"导入     {r.document_id}，{r.chunk_count} 个切片")

        # 同一内容再导一次不会产生新文档
        r2 = client.import_document(kb.id, ImportRequest(path=path))
        print(f"去重     duplicate={r2.duplicate}")

        try:
            v = client.vectorize(kb.id)
            print(f"向量化   {v.vectorized} 个切片")
        except APIError as e:
            # 未配置 Embedding 模型是**正常状态**：关键词检索仍可用
            print(f"向量化   跳过（{e.code}）—— 关键词检索仍可用")

        sr = client.search(SearchRequest(
            query="保修期", kb_ids=[kb.id], top_k=3))
        print(f"检索     模式 {sr.mode}，{len(sr.hits)} 条命中"
              + ("（已降级）" if sr.degraded else ""))
        for h in sr.hits[:2]:
            text = h.text.replace("\n", " ")[:50]
            print(f"  {h.score:.3f}  {text}…")

        if cap.rag_enabled:
            res = client.chat(ChatRequest(
                messages=[Message("user", "保修期多久？")],
                kb_ids=[kb.id], max_tokens=48))
            print(f"问答     {res.content}")
            if res.knowledge:
                print(f"检索使用 {res.knowledge.used_chunks} 个切片，"
                      f"{res.knowledge.context_tokens} tokens")
        else:
            print("问答     跳过（本机未启用 RAG）")

    finally:
        if kb is not None:
            client.delete_kb(kb.id)
            print(f"清理     已删除 {kb.id}")
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    section("7. 错误处理")

    # 客户端侧校验：不发请求就能报错
    try:
        client.create_kb("   ")
    except APIError as e:
        print(f"空名称   {e.kind.name}：{e.message}")
        print(f"可重试   {is_retryable(e)}")

    # 服务端错误：不存在的知识库
    try:
        client.kb_stat("no-such-kb")
    except APIError as e:
        print(f"不存在   {e.kind.name}（HTTP {e.status_code}）")

    # 连一个没有服务的端口
    dead = Client(Config(
        base_url="http://127.0.0.1:1", max_retries=0))
    try:
        dead.health()
    except APIError as e:
        print(f"未运行   {e.kind.name}：{e.message}")

    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
