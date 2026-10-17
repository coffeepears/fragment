"""asyncio ⇄ GLib 主循环桥。

GTK 必须在主线程跑；网络 IO 想用 asyncio 的并发（多平台同时发、gather 收集结果），
于是把 asyncio 事件循环放到一个守护线程里，需要碰 UI 的时候用 GLib.idle_add 派回主线程。

用法::

    runner = AsyncRunner()

    def on_ok(result): ...      # 主线程执行
    def on_err(exc): ...        # 主线程执行

    runner.submit(client.publish(...), on_ok, on_err)

    # 需要往 UI 推进度时，先把回调包一层：
    emit = runner.dispatcher(progress_cb)   # 可以在任意线程安全调用
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from concurrent.futures import Future
from typing import Any, Callable, Coroutine, TypeVar

from gi.repository import GLib

T = TypeVar("T")

__all__ = ["AsyncRunner", "Cancelled"]


class Cancelled(Exception):
    """任务被取消（窗口关闭 / 用户点了取消）。"""


def _idle_call(fn: Callable[..., Any], *args: Any) -> bool:
    """GLib.idle_add 的包装：保证回调返回 False（否则会被反复调用）。"""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - UI 回调出错不能拖死主循环
        traceback.print_exc()
    return False


class AsyncRunner:
    """单后台线程 + 单事件循环。进程内一个实例就够。"""

    def __init__(self, name: str = "gnome-poster-async") -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        self._closed = False

    # -- 后台线程 ---------------------------------------------------------- #
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            self._loop.close()

    # -- 公开 API ---------------------------------------------------------- #
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(
        self,
        coro: Coroutine[Any, Any, T],
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Future:
        """把协程丢到后台循环，结果通过 GLib.idle_add 回到主线程。

        返回 concurrent.futures.Future，可用于取消。
        """
        if self._closed:
            raise RuntimeError("AsyncRunner 已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    exc = Cancelled("任务已取消")
                if on_error is not None:
                    GLib.idle_add(_idle_call, on_error, exc)
                else:
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            else:
                if on_success is not None:
                    GLib.idle_add(_idle_call, on_success, result)

        future.add_done_callback(_done)
        return future

    def dispatcher(self, callback: Callable[..., None]) -> Callable[..., None]:
        """把一个主线程回调包成“任意线程都能安全调用”的版本。

        发布进度回调是在 asyncio 线程里被触发的，直接碰 GTK 会崩，必须过这一层。
        """

        def _wrapper(*args: Any) -> None:
            GLib.idle_add(_idle_call, callback, *args)

        return _wrapper

    def call_soon(self, fn: Callable[..., None], *args: Any) -> None:
        """从后台线程往主线程派活。"""
        GLib.idle_add(_idle_call, fn, *args)

    def shutdown(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        # 取消所有还没跑完的任务，然后停掉循环
        try:
            pending = asyncio.all_tasks(self._loop)
        except RuntimeError:
            pending = set()
        for task in pending:
            self._loop.call_soon_threadsafe(task.cancel)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
