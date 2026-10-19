"""CarTest core — 语音灌入编排（UI 与 core 的契约层）

批量灌入状态机 + 单条执行 + 进度记录。

来源：``CarVoiceTestTool/voice_injector.py`` 平移（2026-09-14）。

**这是 UI 与 core 的标准隔离层**——上层（GUI / CLI）只通过 6 个回调和
3 个控制方法与它交互，不直接触碰 adb / 判定器 / 日志监控。

相对原实现的变更（均为"使编排逻辑可脱离真实设备测试"）：
  1. 类名 ``VoiceInjector`` → ``Injector``
  2. **依赖注入**：``adb`` / ``judger_factory`` / ``monitor_factory`` 均可由构造传入，
     默认分别走 ``AdbSession`` 单例 / ``Judger`` / ``LogcatMonitor``。
     测试时传入替身即可在无设备环境下跑通整条编排链路。
  3. 参数名统一：``adb.send_voice(..., log_cb=)`` → ``on_log=``（与 core 其余模块一致）
  4. ``JudgmentResult`` → ``Judgment``，并提到模块顶部导入

行为未变的部分：状态机（IDLE/RUNNING/PAUSED/STOPPING）、跳过策略三档、
发送失败不计入"已灌入"、判定前排空队列残留（否则误 FAIL）、异常不中断批处理。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .config import DEFAULT_INTERVAL, DEFAULT_RESULT_TIMEOUT
from .judger import Judgment
from .logcat import pump as pump_logs
from .source import DataSource, TestCase


class InjectMode(Enum):
    """灌入模式"""
    SINGLE = "single"           # 单条灌入
    SEQUENTIAL = "sequential"   # 顺序批量灌入
    INTERVAL = "interval"       # 间隔灌入


class InjectState(Enum):
    """灌入状态"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


@dataclass
class InjectProgress:
    """灌入进度"""
    current_index: int = 0
    total: int = 0
    current_query: str = ""
    pass_count: int = 0
    fail_count: int = 0
    error_count: int = 0
    skip_count: int = 0
    elapsed: float = 0.0


class Injector:
    """语音灌入编排器

    构造参数（全部可注入，默认走真实实现）:
        adb:              ADB 会话（默认 ``AdbSession`` 单例）
        judger_factory:   ``(timeout) -> Judger``
        monitor_factory:  ``(queue) -> LogcatMonitor``
        log_q:            日志队列（默认新建）；注入后可与外部产生日志的一端共用同一队列，
                          这是"整条链路脱离真实设备测试"的必要条件
    """

    def __init__(self, adb=None,
                 judger_factory: Callable[[float], object] | None = None,
                 monitor_factory: Callable[[queue.Queue], object] | None = None,
                 log_q: queue.Queue | None = None) -> None:
        self._adb = adb
        self._judger_factory = judger_factory
        self._monitor_factory = monitor_factory

        self._state = InjectState.IDLE
        self._mode = InjectMode.SEQUENTIAL
        self._interval = DEFAULT_INTERVAL
        self._result_timeout = DEFAULT_RESULT_TIMEOUT
        self._voice_mode = 3          # VR getSemantic 的 mode 参数（默认 3＝语义直入）
        self._thread: threading.Thread | None = None
        self._device_id: str = ""
        self._log_q: queue.Queue = log_q if log_q is not None else queue.Queue(maxsize=1000)
        self._monitor = None

        # 回调（core 对外契约）
        self._on_progress: Callable[[InjectProgress], None] | None = None
        self._on_case_start: Callable[[TestCase], None] | None = None
        self._on_case_done: Callable[[TestCase, Judgment], None] | None = None
        self._on_log: Callable[[str, str], None] | None = None
        self._on_all_done: Callable[[], None] | None = None
        self._on_state_change: Callable[[InjectState], None] | None = None

        # 当前进度
        self._progress = InjectProgress()

        # 断点续跑（可选注入，未设置时行为与旧版一致）
        self._store = None                    # ProgressStore
        self._resume_mode = "skip_executed"   # skip_executed | retry_failed | redo_all
        self._prev_map: dict[str, str] = {}   # run_id → 上次状态
        self._skipped = 0

    # ── 依赖解析 ──────────────────────────────
    def _adb_session(self):
        if self._adb is None:
            from .adb import session
            self._adb = session()
        return self._adb

    def _make_judger(self, timeout: float):
        if self._judger_factory is not None:
            return self._judger_factory(timeout)
        from .judger import Judger
        return Judger(clock=self._adb_session().device_timestamp, timeout=timeout)

    def _make_monitor(self, q: queue.Queue):
        if self._monitor_factory is not None:
            return self._monitor_factory(q)
        from .logcat import LogcatMonitor
        return LogcatMonitor(q)

    # ── 只读属性 ──────────────────────────────
    @property
    def state(self) -> InjectState:
        return self._state

    @property
    def progress(self) -> InjectProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._state == InjectState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._state == InjectState.PAUSED

    @property
    def skipped_count(self) -> int:
        """本次被跳过的已执行用例数（断点续跑）"""
        return self._skipped

    # ── 配置 ─────────────────────────────────
    def set_progress_store(self, store, resume_mode: str = "skip_executed",
                           prev_map: dict | None = None) -> None:
        """设置进度存储与重灌范围。

        resume_mode:
          skip_executed → 上次灌过的都跳过（真断点续跑，默认）
          retry_failed  → 只跳过上次通过的，失败/无回复的重灌
          redo_all      → 全部重灌（进度文件仍继续记录）
        prev_map: run_id → 上次状态（pass/fail/...），用于 retry_failed 判定
        """
        self._store = store
        self._resume_mode = resume_mode or "skip_executed"
        self._prev_map = prev_map or {}

    def _should_skip(self, case: TestCase) -> bool:
        if self._store is None or self._resume_mode == "redo_all":
            return False
        prev_status = self._prev_map.get(case.id)
        if prev_status is None:
            return False
        if self._resume_mode == "retry_failed":
            return prev_status == "pass"      # 只重灌非通过项
        return True                            # skip_executed

    def set_callbacks(
        self,
        on_progress: Callable[[InjectProgress], None] | None = None,
        on_case_start: Callable[[TestCase], None] | None = None,
        on_case_done: Callable[[TestCase, Judgment], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        on_all_done: Callable[[], None] | None = None,
        on_state_change: Callable[[InjectState], None] | None = None,
    ) -> None:
        self._on_progress = on_progress
        self._on_case_start = on_case_start
        self._on_case_done = on_case_done
        self._on_log = on_log
        self._on_all_done = on_all_done
        self._on_state_change = on_state_change

    def set_params(self, interval: float = DEFAULT_INTERVAL,
                   timeout: float = DEFAULT_RESULT_TIMEOUT) -> None:
        self._interval = interval
        self._result_timeout = timeout

    def set_device(self, device_id: str) -> None:
        self._device_id = device_id

    def set_voice_mode(self, mode: int) -> None:
        """VR ``getSemantic`` 的 ``mode`` 参数（2026-10-19：页面语义模式下拉接线）

        默认 3（语义直入）。流水线不设置 → 保持 3，与改造前命令逐字节一致。
        """
        try:
            self._voice_mode = int(mode)
        except (TypeError, ValueError):
            self._voice_mode = 3

    # ── 控制操作 ──────────────────────────────
    def start_batch(self, data: DataSource, mode: InjectMode = InjectMode.SEQUENTIAL,
                    start_from: int = 0) -> None:
        """启动批量灌入"""
        if self._state == InjectState.RUNNING:
            return

        self._mode = mode
        self._state = InjectState.RUNNING
        self._notify_state()

        self._thread = threading.Thread(
            target=self._batch_worker,
            args=(data, start_from),
            daemon=True,
        )
        self._thread.start()

    def pause(self) -> None:
        if self._state == InjectState.RUNNING:
            self._state = InjectState.PAUSED
            self._notify_state()

    def resume(self) -> None:
        if self._state == InjectState.PAUSED:
            self._state = InjectState.RUNNING
            self._notify_state()

    def stop(self) -> None:
        if self._state in (InjectState.RUNNING, InjectState.PAUSED):
            self._state = InjectState.STOPPING
            self._notify_state()

    # ── 核心工作线程 ──────────────────────────
    def _batch_worker(self, data: DataSource, start_from: int) -> None:
        cases = data.cases
        total = len(cases)
        start_time = time.time()

        self._progress = InjectProgress(total=total)
        self._start_log_monitor()
        self._skipped = 0

        try:
            for i in range(start_from, total):
                if self._state == InjectState.STOPPING:
                    break

                # 暂停等待
                while self._state == InjectState.PAUSED:
                    time.sleep(0.2)
                    if self._state == InjectState.STOPPING:
                        break

                if self._state == InjectState.STOPPING:
                    break

                case = cases[i]

                # 断点续跑：按重灌范围跳过已执行用例
                if self._should_skip(case):
                    self._skipped += 1
                    self._progress.current_index = i
                    self._progress.skip_count = self._skipped
                    self._update_stats(data)
                    if self._on_progress:
                        self._on_progress(self._progress)
                    continue

                self._progress.current_index = i
                self._progress.current_query = case.query
                self._progress.elapsed = time.time() - start_time

                # 执行单条灌入
                self._execute_single(case)

                # 更新统计
                self._update_stats(data)
                if self._on_progress:
                    self._on_progress(self._progress)

                # 间隔等待
                if i < total - 1 and self._mode == InjectMode.INTERVAL:
                    self._interval_wait()

        finally:
            self._stop_log_monitor()
            self._progress.elapsed = time.time() - start_time
            self._state = InjectState.IDLE
            self._notify_state()
            if self._on_all_done:
                self._on_all_done()

    def _execute_single(self, case: TestCase) -> None:
        """执行单条测试用例"""
        try:
            case.status = "running"
            case.reset()
            case.status = "running"

            if self._on_case_start:
                self._on_case_start(case)

            self._log(f"[测试] {case.id}: {case.query}", "info")

            # 创建判定器
            judger = self._make_judger(self._result_timeout)
            judger.set_timeout(self._result_timeout)
            judger.start_collection()

            # 启动日志收集线程
            log_collector_running = threading.Event()
            log_collector_running.set()

            def collect_logs():
                while log_collector_running.is_set():
                    # 统一走 pump：喂判定器 + 处理 warn/err/stopped + 监控健康检查
                    # （监控失活时自动重启日志流，避免"设备正常但判定全超时"）
                    # 注意 pump 回调是 (tag, msg)，本类 _log 是 (msg, tag)，需转序
                    pump_logs(self._log_q, judger, self._monitor,
                              on_message=lambda tag, m: self._log(m, tag))
                    time.sleep(0.05)

            collector_thread = threading.Thread(target=collect_logs, daemon=True)
            collector_thread.start()

            # 发送语音
            case.inject_time = time.strftime("%m-%d %H:%M:%S")
            send_start = time.time()
            ok, msg = self._adb_session().send_voice(
                case.query, self._device_id, mode=self._voice_mode, on_log=self._log)
            _ = send_start

            if not ok:
                log_collector_running.clear()
                case.status = "error"
                case.error_msg = msg
                case.actual_result = f"发送失败: {msg}"
                self._log(f"[失败] {case.id}: {msg}", "error")
                # 发送失败不计入"已灌入"，下次续跑会自动补发
                self._record_progress(case, "send_fail", False, reason=msg)
                if self._on_case_done:
                    self._on_case_done(case, Judgment(passed=False, reason=msg))
                return

            # 等待结果
            wait_start = time.time()
            while (time.time() - wait_start) < self._result_timeout:
                if self._state == InjectState.STOPPING:
                    break
                if judger.is_complete():
                    break
                time.sleep(0.1)

            log_collector_running.clear()
            collector_thread.join(timeout=1.0)
            # 排空队列残留，避免 is_complete 返回后仍在途的日志被丢弃
            # （run_batch.py 有同款 drain，此前 UI 路径缺失会误 FAIL）
            pump_logs(self._log_q, judger, self._monitor,
                      on_message=lambda tag, m: self._log(m, tag), include_health=False)

            # 判定结果
            result = judger.judge(case)
            case.response_time = result.response_time
            case.asr_response_time = result.asr_response_time
            case.sys_response_time = result.sys_response_time
            case.intent_response_time = result.intent_response_time
            case.tts_response_time = result.tts_response_time
            case.e2e_response_time = result.e2e_response_time
            case.asr_text = result.asr_text
            case.intent_type = result.intent_type
            case.log_snippet = result.log_snippet
            case.actual_result = result.reason

            if result.passed:
                case.status = "pass"
                self._log(f"[通过] {case.id}: {result.reason}", "pass")
            else:
                case.status = "fail"
                case.error_msg = result.reason
                self._log(f"[失败] {case.id}: {result.reason}", "fail")

            self._record_progress(case, case.status, True,
                                  asr=result.asr_text or "",
                                  domain=result.intent_type or "",
                                  reason=result.reason or "")

            if self._on_case_done:
                self._on_case_done(case, result)

        except Exception as e:  # noqa: BLE001
            # 捕获所有异常，确保不会中断自动化流程
            case.status = "error"
            case.error_msg = str(e)
            case.actual_result = f"异常: {str(e)}"
            self._log(f"[错误] {case.id}: {str(e)}", "error")
            # 异常中断不计入"已灌入"，续跑时会重试
            self._record_progress(case, "error", False, reason=str(e))
            if self._on_case_done:
                self._on_case_done(case, Judgment(passed=False, reason=str(e)))

    def _record_progress(self, case: TestCase, status: str, send_ok: bool,
                         asr: str = "", domain: str = "", reason: str = "") -> None:
        """把单条结果写入进度文件（断点续跑的真相来源）"""
        if self._store is None:
            return
        try:
            self._store.append({
                "run_id": case.id,
                "src_id": case.id.split("-Q")[0],
                "query": case.query,
                "device": self._device_id,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "send_ok": bool(send_ok),
                "status": status,
                "asr": (asr or "")[:60],
                "domain": (domain or "")[:60],
                "reason": (reason or "")[:120],
            })
        except Exception as e:  # noqa: BLE001
            self._log(f"[进度] 写入失败（不影响灌入）: {e}", "warn")

    def _interval_wait(self) -> None:
        """间隔等待"""
        remaining = self._interval
        while remaining > 0 and self._state == InjectState.RUNNING:
            time.sleep(min(0.5, remaining))
            remaining -= 0.5

    def _start_log_monitor(self) -> None:
        """启动日志监控"""
        if self._monitor:
            self._monitor.stop()
        self._monitor = self._make_monitor(self._log_q)
        self._monitor.start(self._device_id)

    def _stop_log_monitor(self) -> None:
        """停止日志监控"""
        if self._monitor:
            self._monitor.stop()
            self._monitor = None

    def _update_stats(self, data: DataSource) -> None:
        """更新统计"""
        stats = data.get_stats()
        self._progress.pass_count = stats["pass"]
        self._progress.fail_count = stats["fail"]
        self._progress.error_count = stats["error"]
        self._progress.skip_count = stats["skip"]

    def _log(self, msg: str, tag: str = "info") -> None:
        if self._on_log:
            self._on_log(msg, tag)

    def _notify_state(self) -> None:
        if self._on_state_change:
            self._on_state_change(self._state)
