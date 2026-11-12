"""后台任务与线程安全的结果回传。

## 为什么需要这一层

tkinter 是**单线程**的：所有控件操作必须在主线程（创建 Tk 的那个线程）
里做。在后台线程里直接调 `text.insert(...)` 会导致界面卡死、随机崩溃，
或者更糟——在某些平台上静默丢事件。

而 SDK 的每个调用都可能阻塞几百毫秒到几十秒（模型冷启动、文档解析），
放在主线程里会让窗口「未响应」，Windows 甚至会给你盖上白雾。

因此统一约定：

    后台线程做事 → 结果放进 queue → 主线程定时取出并更新界面

## 为什么用 queue 而不是直接回调

`threading.Thread` 回调里必须切换回主线程才能碰控件，而 Python 没有
内建的「投递到主线程」原语（不像 Qt 的 signal/slot）。用 queue +
`after()` 轮询是这个约束下最简单可靠的做法：

  · 生产者（任意线程）只管 put
  · 消费者（主线程）只在 after() 回调里 get 并更新界面

轮询间隔取 50ms：人眼几乎感知不到，又不会空转烧 CPU。
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable, Optional


class TaskRunner:
    """把耗时调用挪到后台线程，结果通过队列回主线程。

    用法：

        runner = TaskRunner(root)          # root 是 Tk 实例
        runner.poll()                      # 必须在主线程启动轮询
        runner.submit(work, on_done=..., on_error=...)
    """

    def __init__(self, root: Any, interval_ms: int = 50) -> None:
        self._root = root
        self._interval = interval_ms
        self._results: queue.Queue[tuple] = queue.Queue()
        # 忙碌状态也必须经队列回主线程。曾在这里直接调监听器，
        # 而 _bump(-1) 是在后台线程的 finally 里执行的——于是监听器
        # 里的 Progressbar.stop() 在非主线程碰了 tkinter，抛
        # RuntimeError: main thread is not in main loop。
        self._busy_events: queue.Queue[int] = queue.Queue()
        self._pending = 0
        self._reported = 0
        self._listeners: list[Callable[[int], None]] = []
        self._lock = threading.Lock()

    # -- 订阅繁忙状态 --------------------------------------------------

    def on_busy_change(self, fn: Callable[[int], None]) -> None:
        """注册「未完成任务数变化」的监听。

        界面据此禁用按钮、显示进度，避免用户重复点击同一操作。

        监听器**保证在主线程**被调用（见 poll），可以直接操作控件。
        """
        self._listeners.append(fn)

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def _bump(self, delta: int) -> None:
        """调整未完成计数。可从任意线程调用。"""
        with self._lock:
            self._pending += delta
            n = self._pending
        # 不在当前线程回调，只投递；由主线程的 poll 派发
        self._busy_events.put(n)

    # -- 提交任务 ------------------------------------------------------

    def submit(
        self,
        work: Callable[[], Any],
        *,
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """在后台线程执行 work，完成或出错时回到主线程回调。"""
        self._bump(+1)

        def target() -> None:
            try:
                value = work()
            except BaseException as exc:
                # 捕获一切：后台线程里未捕获的异常不会传到主线程，
                # 界面会永远停在「运行中」。必须自己收下并转交。
                self._results.put(("error", on_error, exc))
            else:
                self._results.put(("ok", on_done, value))
            finally:
                self._bump(-1)

        threading.Thread(target=target, daemon=True).start()

    # -- 主线程轮询 ----------------------------------------------------

    def poll(self) -> None:
        """在主线程取结果并派发。必须在主线程调用。"""
        # 先派发忙碌状态变化（它们决定按钮可用性，优先级高于内容）
        latest: Optional[int] = None
        while True:
            try:
                latest = self._busy_events.get_nowait()
            except queue.Empty:
                break
        if latest is not None and latest != self._reported:
            self._reported = latest
            for fn in self._listeners:
                try:
                    fn(latest)
                except Exception:
                    traceback.print_exc()

        while True:
            try:
                _kind, cb, payload = self._results.get_nowait()
            except queue.Empty:
                break

            if cb is None:
                continue
            try:
                cb(payload)
            except Exception:
                # 回调里出错同样不该杀掉轮询循环，否则后续结果永远
                # 不会被派发，界面就卡在最后一步了。
                traceback.print_exc()

        self._root.after(self._interval, self.poll)


def run_in_thread(fn: Callable[[], Any]) -> threading.Thread:
    """便捷函数：跑一个不关心结果的短任务（如复制到剪贴板）。"""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t
