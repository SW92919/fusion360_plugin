from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class BoundedExecutor:
    """
    Thread pool for network / APS work. Fusion document APIs must stay on the main thread.
    """

    def __init__(self, max_workers: int) -> None:
        w = max(1, min(32, int(max_workers)))
        self._pool = ThreadPoolExecutor(max_workers=w)

    def submit(self, fn: Callable[..., T], *args, **kwargs) -> Future:
        return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        try:
            self._pool.shutdown(wait=wait, cancel_futures=False)
        except Exception:
            pass


def pump_future(fut: Future, pump_fn: Optional[Callable[[], None]], timeout_s: float = 0.05) -> None:
    """Poll a Future while optionally pumping Fusion UI (caller should pass viewport_render.pump_ui)."""
    import time

    while not fut.done():
        if pump_fn:
            try:
                pump_fn()
            except Exception:
                pass
        time.sleep(timeout_s)
