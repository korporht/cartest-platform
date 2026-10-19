"""CarTest core — 日志采集

后台 ``adb logcat`` 监控线程 + 全量落盘 + 统一消费端 ``pump()``。

来源：``CarVoiceTestTool/log_monitor.py`` 平移（2026-09-14）。

可靠性约定（2026-09-14 缺陷修复成果，**不可回退**）：
  - 队列满时丢行会**计数并通过 ("warn", ...) 上报**，不再静默丢弃；
  - 记录「最近一次读到原始日志」的墙钟，``health_check()`` 据此判断
    logcat 流是否停摆/进程是否退出，并自动重启日志流（不是重启语音服务）；
  - 每次起流有 **epoch 编号**，``("stopped", "<epoch>")`` 只代表该轮流的结束，
    消费者可用 ``monitor.epoch`` 判断是否为当前流的生命周期事件
    （避免重启后收到旧流的 stopped 而误判）；
  - 监控线程内**严禁阻塞投递**（``_put_nowait``）——原实现用阻塞 ``q.put()``
    上报生命周期事件，队列满时会把监控线程永久卡死，表现为"日志监控静默失效、
    后续用例全部超时"。
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from typing import Callable

from .adb import AdbSession
from .config import FULL_LOG_KEYWORDS, MONITOR_KEYWORDS
from . import project as _project

#: 消费端健康检查节流间隔（秒）——避免每个循环都做检查
HEALTH_CHECK_INTERVAL = 2.0


def monitor_keywords() -> list[str]:
    """实时监控关键词（项目档案 → ``config.MONITOR_KEYWORDS`` 兜底）"""
    return list(_project.get("log.monitor_keywords", MONITOR_KEYWORDS) or [])


def fulllog_keywords() -> list[str]:
    """全量落盘关键词（项目档案 → ``config.FULL_LOG_KEYWORDS`` 兜底）"""
    return list(_project.get("log.fulllog_keywords", FULL_LOG_KEYWORDS) or [])



class LogcatMonitor:
    """实时日志监控（关键词过滤后投递到队列）"""

    #: 触发 warn 上报的丢行数（首次 + 每累计该数量一次）
    DROP_WARN_STEP = 500

    def __init__(self, q: queue.Queue, device_id: str = "") -> None:
        self.q = q
        self._proc: subprocess.Popen | None = None
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._device_id: str = device_id
        # 可靠性统计（2026-09-14）
        self._epoch: int = 0
        self._restarts: int = 0
        self._dropped: int = 0
        self._lines_read: int = 0            # 原始 logcat 行数（过滤前）
        self._lines_matched: int = 0         # 通过关键词过滤并入队的行数
        self._last_read_wall: float = 0.0    # 最近读到任意原始行的时刻
        self._last_line_wall: float = 0.0    # 最近一条有效行入队的时刻
        self._health_last: float = 0.0       # 最近一次健康检查时刻（pump 节流用）

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def epoch(self) -> int:
        """当前日志流的世代号（重启 +1）；用于识别生命周期事件是否属于本轮流。"""
        return self._epoch

    @property
    def dropped(self) -> int:
        return self._dropped

    def stats(self) -> dict:
        return {
            "epoch": self._epoch,
            "restarts": self._restarts,
            "dropped": self._dropped,
            "lines_read": self._lines_read,
            "lines_matched": self._lines_matched,
            "idle_s": round(time.time() - self._last_read_wall, 1) if self._last_read_wall else -1.0,
        }

    def start(self, device_id: str = "") -> None:
        if self._running:
            return
        if device_id:
            self._device_id = device_id
        self._running = True
        self._epoch += 1
        now = time.time()
        self._last_read_wall = now
        self._last_line_wall = now
        self._thread = threading.Thread(target=self._run, args=(self._epoch,), daemon=True)
        self._thread.start()
        # 等待日志流稳定
        time.sleep(2)

    def restart(self, reason: str = "") -> str:
        """重启日志流（停掉旧 logcat 进程并重新起流），返回重启原因供调用方记录。"""
        self._restarts += 1
        self.stop()
        time.sleep(0.3)
        self.start(self._device_id)
        # 给新流一个冷却窗口，避免"重启→立刻重判→再重启"的抖动
        self._health_last = time.time()
        return reason or "手动重启"

    def health_check(self, idle_timeout: float = 25.0) -> str | None:
        """健康检查：正常返回 None；异常时自动重启日志流并返回原因字符串。

        判定依据是「原始 logcat 输出是否还在流动」，与业务关键词无关——
        设备空闲时关键词行可能长时间没有，但 logcat 原始流始终有内容。
        """
        if not self._running:
            return self.restart("监控未运行")
        if self._proc is None:
            # start() 后起流有一小段窗口
            if time.time() - self._last_read_wall > max(idle_timeout, 10.0):
                return self.restart("logcat 进程未建立")
            return None
        code = self._proc.poll()
        if code is not None:
            return self.restart(f"logcat 进程已退出(code={code})")
        idle = time.time() - self._last_read_wall
        if idle > idle_timeout:
            return self.restart(f"logcat {idle:.0f}s 无任何输出")
        return None

    def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    # ── 健康检查节流（供 pump 使用，避免其触碰私有字段）──
    def should_health_check(self, interval: float = HEALTH_CHECK_INTERVAL) -> bool:
        """距上次健康检查是否已超过节流间隔；是则更新时刻并返回 True。

        设计说明（2026-09-14）：原 ``pump()`` 直接读写 ``monitor._health_last``，
        使任何"仅实现公共接口"的监视器替身都会让 pump 抛 AttributeError，
        进而静默杀死日志收集线程（表现为"设备正常但判定全超时"）。
        节流状态属监视器自身职责，故封装于此。
        """
        now = time.time()
        if now - self._health_last >= interval:
            self._health_last = now
            return True
        return False

    def clear_logcat(self) -> None:
        """清空 logcat 缓冲区，用于新 query 开始前（只清 RAM 环形缓冲，不影响设备落盘日志）"""
        try:
            base = AdbSession._base_args(self._device_id)
            subprocess.run(
                base + ["logcat", "-c"],
                capture_output=True, timeout=5,
                startupinfo=AdbSession._startupinfo()
            )
        except Exception:
            pass

    def _run(self, epoch: int) -> None:
        try:
            base = AdbSession._base_args(self._device_id)
            # 先清空 logcat 缓冲区，确保获取全新日志
            try:
                subprocess.run(
                    base + ["logcat", "-c"],
                    capture_output=True, timeout=5,
                    startupinfo=AdbSession._startupinfo()
                )
            except Exception:
                pass

            # 使用更宽松的过滤，捕获所有语音相关日志
            cmd = base + ["logcat", "-v", "threadtime"]

            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                startupinfo=AdbSession._startupinfo(),
            )
            for line in self._proc.stdout:
                if not self._running:
                    break
                # 原始输出心跳：只要有输出就说明 logcat 流是活的（与关键词无关）
                self._lines_read += 1
                self._last_read_wall = time.time()
                line = line.rstrip()
                # 关键词二次过滤
                if not self._line_matches(line):
                    continue
                try:
                    self.q.put_nowait(("log", line))
                    self._lines_matched += 1
                    self._last_line_wall = time.time()
                except queue.Full:
                    # 不再静默丢弃：计数 + 周期性告警（丢行会让判定失真）
                    self._dropped += 1
                    if self._dropped == 1 or self._dropped % self.DROP_WARN_STEP == 0:
                        try:
                            self.q.put_nowait((
                                "warn",
                                f"日志队列已满，累计丢弃 {self._dropped} 行"
                                f"（消费端跟不上，判定结果可能失真，建议降低灌入频率）"
                            ))
                        except queue.Full:
                            pass
        except Exception as e:  # noqa: BLE001
            self._put_nowait(("err", f"[监控异常] {e}"))
        finally:
            # 仅当本轮流仍是当前流时才上报 stopped（重启后旧线程的收尾不应干扰消费端）
            if epoch == self._epoch:
                self._put_nowait(("stopped", str(epoch)))

    def _put_nowait(self, msg) -> bool:
        """生命周期/告警消息的非阻塞投递。

        修复（2026-09-14）：原实现用 q.put()（阻塞）上报 err/stopped——
        队列满时会把监控线程永久卡死，表现为"日志监控静默失效、后续用例全部超时"。
        监控线程内严禁阻塞投递。
        """
        try:
            self.q.put_nowait(msg)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    @staticmethod
    def _line_matches(line: str) -> bool:
        ll = line.lower()
        return any(kw in ll for kw in monitor_keywords())


class FullLogMonitor:
    """全量日志监控（文件记录用）"""

    def __init__(self, file_path, callback: Callable[[str], None] | None = None) -> None:
        self.file_path = file_path
        self.callback = callback
        self._proc: subprocess.Popen | None = None
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._device_id: str = ""
        self._line_count: int = 0
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def line_count(self) -> int:
        with self._lock:
            return self._line_count

    def start(self, device_id: str = "") -> None:
        if self._running:
            return
        if device_id:
            self._device_id = device_id
        self._running = True
        self._line_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def _run(self) -> None:
        try:
            base = AdbSession._base_args(self._device_id)
            filter_tags = [
                "AgentAssistantServiceAdapter:V",
                "issse_log:I", "isssr_log:I",
                "TtsServiceAgent:D", "TtsManager:D",
                "ASS-SemixProcessor:I", "ASS-*",
                "VoiceProcessor:D", "VoiceRecognition:D",
                "lioncarmind:D", "CarMind:D",
                "PlatformService:D", "SceneEngine:D",
                "MsManagerAdapter:D", "AIKIT:D",
                "iflytek*:D", "autofly*:D",
                # 内容审核/评论拦截类候选 tag
                "CommentAudit:D", "ContentAudit:D",
                "PolicyEngine:D", "BrandStrategy:D",
                "ContentFilter:D", "ReviewFilter:D",
                "SafeGuard:D", "LionLog:I",
            ]
            cmd = base + ["logcat", "-v", "threadtime"] + filter_tags

            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                startupinfo=AdbSession._startupinfo(),
            )

            with open(self.file_path, "a", encoding="utf-8") as f:
                for line in self._proc.stdout:
                    if not self._running:
                        break
                    line = line.rstrip()
                    if not self._full_line_matches(line):
                        continue
                    f.write(line + "\n")
                    with self._lock:
                        self._line_count += 1
                    if self._line_count % 100 == 0:
                        f.flush()
                    if self.callback:
                        self.callback(line)
        except Exception:
            pass

    @staticmethod
    def _full_line_matches(line: str) -> bool:
        ll = line.lower()
        return any(kw.lower() in ll for kw in fulllog_keywords())


def pump(q: queue.Queue, judger, monitor: "LogcatMonitor | None" = None,
         idle_timeout: float = 25.0, on_message: Callable[[str, str], None] | None = None,
         include_health: bool = True) -> bool:
    """消费日志队列：喂给 judger，并处理监控的告警/生命周期事件与健康检查。

    统一所有消费端（GUI 注入器 / CLI runner）的行为，避免各自实现导致
    "监控失活却无人发现"。返回本次是否触发了日志流重启。

    - ("log", line)      → judger.feed_log
    - ("warn", msg)      → on_message("warn", msg)   （队列丢行等）
    - ("err", msg)       → on_message("error", msg)
    - ("stopped", epoch) → 若仍是当前流则重启，并通知
    - 其余               → monitor.health_check()（按 HEALTH_CHECK_INTERVAL 节流）
    """
    restarted = False
    while True:
        try:
            msg_type, msg = q.get_nowait()
        except queue.Empty:
            break
        if msg_type == "log":
            judger.feed_log(msg)
        elif msg_type == "warn":
            if on_message:
                on_message("warn", msg)
        elif msg_type == "err":
            if on_message:
                on_message("error", msg)
        elif msg_type == "stopped":
            # 只处理当前世代的结束事件（重启后旧线程的收尾不算）
            if monitor is None:
                if on_message:
                    on_message("warn", "日志流已结束（未持有监控对象，无法自动重启）")
            elif str(monitor.epoch) == (msg or "").strip():
                monitor.restart("日志流结束")
                restarted = True
                if on_message:
                    on_message("warn", "日志流已结束，已自动重启监控")
    if include_health and monitor is not None:
        # 节流交由监视器自身管理（见 should_health_check 的设计说明）
        if monitor.should_health_check():
            reason = monitor.health_check(idle_timeout)
            if reason:
                restarted = True
                if on_message:
                    on_message("warn", f"日志监控异常（{reason}），已自动重启日志流")
    return restarted
