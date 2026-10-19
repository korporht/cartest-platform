# -*- coding: utf-8 -*-
"""单条快灌服务 — 一句话 → 灌入 → 判定（复用 ``core.Injector``）

背景（``30-功能分类与可配置分离.md`` §六 第 2 项）：``EOV_Voice_Tool`` 的日常高频
用法是"随手灌一句、立刻看结果"，平台原先只有整表流水线，没有这个入口。

**不重写灌入逻辑**：本服务用**一条用例**的 ``DataSource`` 驱动 ``core.Injector``——
与流水线 inject 阶段同一套 adb / judger / logcat 装配（含超时、拒识识别、
判定优先级），只把"批量"换成"单发"。

**循环灌入（2026-10-15 用户诉求）**：``repeat`` 轮，每轮**全新** ``Injector``（含
全新日志监控窗口）——同一句话重复灌时，上一轮的残留日志不会串进本轮判定；
轮间隔 ``interval`` 秒可被 ``stop()`` 打断；``repeat=0`` ＝ 不限（直到停止）。

**失败即停（2026-10-19 用户诉求）**：``fail_fast=True`` 时任一轮判定失败
（含发送失败 / 异常——Injector 对两者都回调 ``passed=False`` 的 Judgment）即
结束循环，不再灌下一轮。停止原因透出在 :attr:`stop_reason`（``"fail_fast"`` /
``"manual"`` / ``""``），供页面收尾提示区分「失败即停」与「手动停止」。

**顺序执行（2026-10-19 用户诉求）**：``queries`` 非空时按列表**逐条**灌入——
轮数 = 用例数（忽略 ``repeat``），每轮取 ``queries[n-1]``；``interval`` /
``fail_fast`` / 停止机制与循环灌入**完全共用**同一通路。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ....core import config
from ....core.injector import InjectMode, InjectState, Injector
from ....core.judger import default_timeout as judge_default_timeout
from ....core.source import DataSource, TestCase

#: 回调签名
OnLog = Callable[[str, str], None]
OnDone = Callable[[TestCase, object], None]
OnState = Callable[[InjectState], None]
OnRound = Callable[[int, int], None]      # (本轮序号从 1 开始, 总轮次；0=不限)


class SingleInjectService:
    """单条快灌（非阻塞；同一时刻只允许一次；支持循环灌入）"""

    def __init__(self, *, adb=None, judger_factory=None, monitor_factory=None) -> None:
        self._adb = adb
        self._judger_factory = judger_factory      # 测试可注入（离线跑通路）
        self._monitor_factory = monitor_factory
        self._inj: Injector | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()          # 循环灌入：轮间/轮内停止信号
        self._stop_reason = ""                      # 停止原因："" | "manual" | "fail_fast"
        self._round = 0                             # 已完成轮次（循环用）

    @property
    def busy(self) -> bool:
        th = self._thread
        return th is not None and th.is_alive()

    @property
    def rounds_done(self) -> int:
        return self._round

    @property
    def stop_reason(self) -> str:
        """本次循环为何结束：``"manual"``（手动停止）/ ``"fail_fast"`` / ``""``（灌满/未停）"""
        return self._stop_reason

    def run(self, query: str, *, device: str = "", timeout: float | None = None,
            repeat: int = 1, interval: float = 0.0, fail_fast: bool = True,
            queries: list[str] | None = None, voice_mode: int = 3,
            on_log: OnLog | None = None, on_done: OnDone | None = None,
            on_state: OnState | None = None, on_round: OnRound | None = None) -> bool:
        """启动快灌：``repeat`` 轮（0＝不限，直到 :meth:`stop`），轮间隔 ``interval`` 秒

        ``fail_fast``：**默认开启**（2026-10-19 用户裁决：失败即停是循环/顺序执行的
        默认行为）——任一轮判定失败即结束执行；传 False 可显式关闭（测试/特殊场景）。
        ``queries``：顺序执行——按列表逐条灌入（轮数＝用例数，**忽略 ``repeat``**），
        与循环灌入共用 ``interval`` / ``fail_fast`` / 停止通路。
        ``voice_mode``：VR ``getSemantic`` 的 mode 参数（3＝语义直入，默认）。
        每轮独立：新建 ``Injector`` + 日志监控 → ``on_done`` 逐轮回调（页面逐条落卡）。
        """
        q = (query or "").strip()
        qs = [str(x).strip() for x in (queries or []) if str(x).strip()]
        if self.busy or (not qs and not q):
            return False
        wait = float(timeout or judge_default_timeout())
        if qs:                                      # 顺序执行：轮数＝用例数
            rounds = len(qs)
        else:
            rounds = int(repeat or 0)               # 0 / 负数 → 不限
        gap = max(0.0, float(interval or 0.0))
        fail_fast = bool(fail_fast)
        self._stop_evt = threading.Event()
        self._stop_reason = ""
        self._round = 0

        def _work() -> None:
            if on_state:
                on_state(InjectState.RUNNING)
            try:
                while True:
                    if self._stop_evt.is_set():
                        break
                    if rounds and self._round >= rounds:
                        break
                    self._round += 1
                    if on_round:
                        on_round(self._round, rounds)
                    cur = qs[self._round - 1] if qs else q
                    category = "顺序灌入" if qs else "单条快灌"

                    inj = Injector(adb=self._adb, judger_factory=self._judger_factory,
                                   monitor_factory=self._monitor_factory)
                    with self._lock:
                        self._inj = inj
                    # 单发：间隔 0（不排队等待），超时与流水线同口径
                    inj.set_params(interval=0.0, timeout=wait)
                    inj.set_voice_mode(voice_mode)
                    inj.set_device(device)
                    ds = DataSource()
                    ds.cases.append(TestCase(id=f"快灌{self._round}", query=cur,
                                             category=category))
                    inj.set_callbacks(
                        on_log=(lambda m, t="info": on_log(m, t)) if on_log else None,
                        # 服务层判定捕获（失败即停）与页面 on_done 链式并存：
                        # Injector 对 判定 fail / 发送失败 / 异常 都回调 passed=False，
                        # 三种都算"灌入失败"，fail_fast 一视同仁
                        on_case_done=(lambda c, j: self._track_done(
                            c, j, fail_fast, on_log, on_done)) if (fail_fast or on_done) else None)
                    try:
                        inj.start_batch(ds, mode=InjectMode.SEQUENTIAL)
                        deadline = time.time() + wait + 20.0   # 兜底（判定超时后收尾）
                        while inj.state != InjectState.IDLE and time.time() < deadline:
                            if self._stop_evt.is_set():
                                inj.stop()                     # 轮内停止：交给判定收尾
                            time.sleep(0.1)
                    except Exception as ex:  # noqa: BLE001 - 回调上报，页面决定怎么显示
                        if on_log:
                            on_log(f"[语音灌入] 异常：{ex}", "error")
                    with self._lock:
                        self._inj = None

                    if self._stop_evt.is_set():
                        break
                    if rounds and self._round >= rounds:
                        break
                    # 轮间隔（可被 stop() 打断，不干等）
                    end = time.time() + gap
                    while time.time() < end and not self._stop_evt.is_set():
                        time.sleep(0.05)
            finally:
                with self._lock:
                    self._inj = None
                if on_state:
                    on_state(InjectState.IDLE)

        self._thread = threading.Thread(target=_work, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """请求停止（轮内：交给当前判定收尾；轮间：立即结束循环）

        停止原因先到先得：手动 ``stop()`` 记 ``"manual"``；若失败即停已先触发
        （``_stop_reason="fail_fast"``）则不覆盖——用户随后点停止不应篡改原因。
        """
        if not self._stop_reason:
            self._stop_reason = "manual"
        self._stop_evt.set()
        with self._lock:
            inj = self._inj
        if inj is not None:
            try:
                inj.stop()
            except Exception:  # noqa: BLE001
                pass

    def _track_done(self, case, judgment, fail_fast: bool,  # noqa: ANN001
                    on_log: OnLog | None, on_done: OnDone | None) -> None:
        """轮结果捕获：fail_fast 命中 → 结束循环（原因透出给页面收尾提示）

        调用线程 = Injector 工作线程（``_execute_single`` 末尾），与轮等待循环
        的 100ms 轮询兼容；已有人工停止原因时不覆盖（用户停止优先）。
        """
        if fail_fast and not getattr(judgment, "passed", False) \
                and not self._stop_reason:
            self._stop_reason = "fail_fast"
            self._stop_evt.set()
            if on_log:
                on_log(f"[语音灌入] 第 {self._round} 轮失败，"
                       f"「失败即停」生效，本次执行结束", "warn")
        if on_done:
            on_done(case, judgment)
