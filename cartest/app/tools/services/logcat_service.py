# -*- coding: utf-8 -*-
"""Logcat 实时抓取 Service — 子进程管理 + 计时器 + 进度跟踪

2026-09-23 修复（35 号文档 §六，用户实测反馈"抓到的全是 20 分钟前的旧日志"）：
  - **实时起流**：命令加 ``-T 1``（从缓冲区最新一条开始）。旧实现不带 ``-T``，
    等价于"先把整个环形缓冲从最旧一条全量转储、再进入实时"——实测 180s 都
    转不完（3352 行仅覆盖约 25 小时旧日志），用户看到/存下的全是积压内容；
  - **设备序列号**：命令经 ``AdbSession._base_args(device_id)`` 构造（带 ``-s``），
    不再裸调 ``adb``，多设备环境不会打错设备；
  - **去 shell**：改为列表式 Popen（不再 ``shell=True``）——``stop()`` 的
    terminate 直杀 adb 进程、管道立即 EOF，读循环**确定性收尾**；旧实现只杀
    cmd 壳、adb 变孤儿，且读循环仅在"下一行到达"时才检查停止标志，安静场景
    可能长时间卡住不收口；
  - 逐行 ``on_log`` 契约不变，UI 层吞吐由 ``LineBatcher`` 批量合并解决。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from ....core.adb import AdbSession

_log = logging.getLogger("logcat_service")


class LogcatService:
    """Logcat 实时抓取管理器。

    职责：
    - 启动 / 停止 ``adb logcat`` 子进程
    - 实时读取 stdout 并写入本地文件
    - 进度计时器（供 UI 层的 ProgressBar / 标签使用）
    """

    def __init__(
        self,
        on_log: Callable[[str], None],
        max_duration: int = 180,
    ) -> None:
        """
        Args:
            on_log: 日志回调。
            max_duration: 最大抓取时长（秒），超过此值自动停止。
        """
        self._on_log = on_log
        self._max_duration = max_duration
        _log.info("[__init__] LogcatService created, on_log=%s, max_dur=%d",
                   type(on_log).__name__, max_duration)

        # 运行时状态
        self._proc: Optional[subprocess.Popen] = None
        self._running = threading.Event()
        self._timer_running = threading.Event()
        self._start_time: float = 0.0
        self._save_path: str = ""

    # ── 只读属性（供 UI 层查询状态）─────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def save_path(self) -> str:
        return self._save_path

    @property
    def elapsed(self) -> int:
        """已运行秒数（仅在 running 时有意义）。"""
        return int(time.time() - self._start_time) if self._start_time else 0

    @property
    def remaining(self) -> int:
        return max(0, self._max_duration - self.elapsed)

    @property
    def progress(self) -> float:
        if not self._start_time:
            return 0.0
        return min(self.elapsed / self._max_duration, 1.0)

    # ── 公开方法 ──────────────────────────────────────────

    def start(
        self,
        save_dir: str,
        buffer_label: str,
        clear_enabled: bool = False,
        on_progress: Optional[Callable[[float, int, int], None]] = None,
        on_stopped: Optional[Callable[[str], None]] = None,
        device_id: str = "",
    ) -> None:
        """启动 Logcat 抓取（**阻塞**，直到进程结束）。

        调用方应在一个后台线程中调用此方法。

        Args:
            save_dir: 日志保存目录。
            buffer_label: 缓冲区参数（如 ``-b all``；按空白拆成参数列表）。
            clear_enabled: 是否在抓取前清空缓冲区。
            on_progress: 进度回调 ``(progress, elapsed, remaining)``。
            on_stopped: 停止回调 ``(save_path)``。
            device_id: 设备序列号（空串则用 adb 默认设备）。
        """
        if self._running.is_set():
            self._on_log("[WARN] Logcat 已在运行")
            _log.warning("[start] already running, skipping")
            return

        ts = time.strftime("%Y%m%d_%H%M%S")
        logcat_dir = os.path.join(save_dir, f"logcat_{ts}")
        os.makedirs(logcat_dir, exist_ok=True)
        self._save_path = os.path.join(logcat_dir, "logcat.log")
        self._running.set()
        self._start_time = time.time()

        base = AdbSession._base_args(device_id)
        buffer_args = buffer_label.split()
        _log.info("[start] device=%s, buffer=%s, clear=%s, save=%s",
                  device_id or "(默认)", buffer_label, clear_enabled, self._save_path)

        try:
            self._on_log("=== 开始抓取 Logcat ===")
            self._on_log(f"[INFO] 保存路径: {self._save_path}")

            if clear_enabled:
                self._on_log("[INFO] 清空 Logcat 缓冲区...")
                subprocess.run(
                    base + ["logcat", "-c"], capture_output=True, timeout=10,
                    startupinfo=AdbSession._startupinfo(),
                )
                self._on_log("[OK] 缓冲区已清空")

            # 启动进度计时器
            self._timer_running.set()
            t_timer = threading.Thread(target=self._tick, args=(on_progress,), daemon=True)
            t_timer.start()

            # ``-T 1``：从缓冲区最新一条开始（真·实时；不带则先全量转储积压）
            cmd = base + ["logcat", "-T", "1", "-v", "threadtime"] + buffer_args
            self._on_log(f"[CMD] {' '.join(cmd)}")

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
                startupinfo=AdbSession._startupinfo(),
            )
            self._proc = proc

            with open(self._save_path, "w", encoding="utf-8") as f:
                line_count = 0
                for line in proc.stdout:
                    if not self._running.is_set():
                        break
                    line = line.rstrip("\n\r")
                    if line:
                        f.write(line + "\n")
                        line_count += 1
                        if line_count % 100 == 0:
                            f.flush()
                        self._on_log(line)
            _log.info("[start] logcat read loop ended, %d lines emitted via _on_log", line_count)
        except Exception as ex:
            self._on_log(f"[ERR] {ex}")
            _log.error("[start] exception: %s", ex, exc_info=True)
        finally:
            self._running.clear()
            self._timer_running.clear()
            self._release_proc()

            if os.path.exists(self._save_path):
                size = os.path.getsize(self._save_path)
                self._on_log(f"[OK] 日志已保存，大小: {size:,} 字节")

            if on_stopped:
                on_stopped(self._save_path)

    def stop(self) -> None:
        """请求停止 Logcat 抓取（非阻塞）。

        terminate 直杀 adb 进程（列表式 Popen，无 cmd 壳）→ 读管道立即 EOF，
        ``start()`` 的读循环随之收尾并触发 ``on_stopped``（确定性，无卡死窗口）。
        """
        if not self._running.is_set():
            _log.warning("[stop] not running, skip")
            return
        _log.info("[stop] stopping logcat")
        self._on_log("=== 停止抓取 Logcat ===")
        self._running.clear()
        self._timer_running.clear()
        self._release_proc()

    # ── 内部方法 ──────────────────────────────────────────

    def _release_proc(self) -> None:
        """终止并回收 logcat 子进程（terminate → 2s → kill），保证读管道 EOF。

        注意：**不要**在这里 close ``p.stdout``——读线程正迭代它，从停止线程
        关闭会触发 ``ValueError: I/O operation on closed file``（2026-09-23
        真机回归）。进程死亡后管道自然 EOF，读线程自行收尾即可。
        """
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=2)
            except Exception:  # noqa: BLE001  （含超时：升级为 kill）
                p.kill()
        except Exception:  # noqa: BLE001
            pass

    def _tick(
        self,
        on_progress: Optional[Callable[[float, int, int], None]] = None,
    ) -> None:
        """每秒更新进度并向 UI 层回调。"""
        while self._timer_running.is_set() and self._running.is_set():
            try:
                elapsed = self.elapsed
                remaining = self.remaining
                progress_value = self.progress

                if on_progress:
                    on_progress(progress_value, elapsed, remaining)

                if elapsed >= self._max_duration:
                    self._on_log(
                        f"[INFO] Logcat 已达最大时长 {self._max_duration}s，自动停止"
                    )
                    self.stop()
                    break
            except Exception:
                pass
            time.sleep(1)
