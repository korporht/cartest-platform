# -*- coding: utf-8 -*-
"""高频行回调批量合并器 — UI 上屏吞吐优化（供 Logcat 实时抓取使用）

背景（2026-09-23，35 号文档 §六）：
  - 逐行回调 → 每行一次 UI 线程调度，消费上限被压到 ~16 行/秒；
  - 改为批量后实测仍只有 ~630 行/秒（设备洪峰 ~2000 行/秒时进一步落后），
    真机排查定位：旧版 ``feed()`` 在缓冲积满时**在调用方线程同步做重活**
    （200 行 UI 模型/控件构建），把日志读线程拖住了。

本类约定（2026-09-23 修订）：
  - :meth:`feed` **永不做重活**（仅入缓冲 + 越限裁剪计数），可被读线程高频调用;
  - 重活（合并 + 上屏回调）只在**定时线程**（每 ``interval`` 秒）或
    :meth:`stop` 时发生；
  - 洪峰保护：缓冲超过 ``max_batch`` 行时仅保留**最新** ``max_batch`` 行，
    被省略行数会在下次冲刷时以一行说明呈现（不静默丢弃；文件落盘不受影响）；
  - 内存有界：缓冲最多 ``max_batch`` 行。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

_log = logging.getLogger("line_batcher")


class LineBatcher:
    """线程安全的行批量合并器（读线程只投递，重活走定时线程）。

    - :meth:`feed` 由工作线程调用（高频、非阻塞、无重活）；
    - 内部定时线程每 ``interval`` 秒把缓冲合并为一次 ``on_flush(list[str])``；
    - 洪峰时缓冲裁剪为最新 ``max_batch`` 行，省略数计入下一次冲刷的提示行；
    - :meth:`stop` 停止并冲刷残余（收尾不丢行）。
    """

    def __init__(self, on_flush: Callable[[list[str]], None],
                 interval: float = 0.2, max_batch: int = 50) -> None:
        """默认参数面向「UI 可承受的上屏速率」：≤ 50 行 / 200ms ≈ 250 行/秒。

        真机实测（2026-09-23）：设备洪峰 ~1700-2600 行/秒时，若按 200 行/100ms
        上屏（≈2000 行/秒），flet 面板被拖死（点击 2 分钟才被处理）。250 行/秒
        的上屏速率下 UI 保持可交互；常规速率（≤250 行/秒）不受影响不丢行。
        """
        self._on_flush = on_flush
        self._interval = interval
        self._max_batch = max_batch
        self._buf: list[str] = []
        self._dropped = 0
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动定时冲刷线程（重复调用无副作用）。"""
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()

    def feed(self, line: str) -> None:
        """投递一行（工作线程调用；**仅入缓冲，永不做重活**）。

        洪峰保护：超过 ``max_batch`` 时裁剪最旧行并计数，避免内存膨胀、
        避免拖慢调用方（日志读线程）。
        """
        with self._lock:
            self._buf.append(line)
            over = len(self._buf) - self._max_batch
            if over > 0:
                del self._buf[:over]
                self._dropped += over

    def flush(self) -> None:
        """把当前缓冲合并为一次回调（定时线程 / stop 调用；无内容时为空操作）。"""
        with self._lock:
            if not self._buf and not self._dropped:
                return
            batch, self._buf = self._buf, []
            dropped, self._dropped = self._dropped, 0
        if dropped:
            batch.insert(0, f"[洪峰省略 {dropped} 行未上屏；完整内容见落盘文件]")
        try:
            self._on_flush(batch)
        except Exception:  # noqa: BLE001
            _log.warning("[flush] on_flush 异常（已丢弃该批 %d 行）", len(batch),
                         exc_info=True)

    def stop(self) -> None:
        """停止定时线程并冲刷残余（幂等）。"""
        self._running.clear()
        self.flush()

    def _tick(self) -> None:
        while self._running.is_set():
            time.sleep(self._interval)
            self.flush()
