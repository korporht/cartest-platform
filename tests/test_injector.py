# -*- coding: utf-8 -*-
"""Injector 编排层自测（**完全无需设备**）

这是 P1 的最终验收点：整条编排链路（用例 → 灌入 → 日志消费 → 判定 →
进度记录 → 回调通知）在没有任何真机的情况下可完整跑通。

用替身（Fake）注入 adb / judger / monitor 三个依赖。

覆盖：
  1) 状态机流转：IDLE → RUNNING → IDLE
  2) 6 个回调全部被触发且顺序正确
  3) 用例结果正确回填（pass / fail / 发送失败）
  4) 跳过策略三档：skip_executed / retry_failed / redo_all
  5) 进度记录（store.append 的字段与 send_ok 语义）
  6) 异常不中断批处理
  7) pause / resume / stop

用法:
    python tests/test_injector.py
    pytest tests/test_injector.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.injector import InjectMode, Injector, InjectState   # noqa: E402
from cartest.core.judger import Judgment                              # noqa: E402
from cartest.core.source import DataSource, TestCase                  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


# ── 替身 ──────────────────────────────────────
class FakeAdb:
    """记录发送内容；可配置发送结果序列"""

    def __init__(self, results=None):
        self.sent: list[str] = []
        self._results = results or [(True, "OK")]
        self.raise_on: set[str] = set()

    def send_voice(self, text, device_id="", mode=3, on_log=None):
        self.sent.append(text)
        if text in self.raise_on:
            raise RuntimeError(f"模拟发送异常: {text}")
        idx = min(len(self.sent) - 1, len(self._results) - 1)
        return self._results[idx]

    def device_timestamp(self):
        return "09-15 10:00:00.000"


class FakeJudger:
    """按 query 决定判定结果"""

    def __init__(self, timeout: float = 20.0, pass_set=None, complete: bool = True):
        self.timeout = timeout
        self._pass_set = pass_set or set()
        self._complete = complete

    def set_timeout(self, t):
        self.timeout = t

    def start_collection(self, send_time=0.0):
        pass

    def feed_log(self, line):
        pass

    def is_complete(self):
        return self._complete

    def judge(self, case):
        ok = case.query in self._pass_set
        return Judgment(passed=ok, reason=("fake pass" if ok else "fake fail"),
                        asr_text=case.query, intent_type="fake_domain")

    def get_progress_info(self):
        return ""


class FakeMonitor:
    def __init__(self, q):
        self.q = q
        self.started = False
        self.stopped = False

    def start(self, device_id=""):
        self.started = True

    def stop(self):
        self.stopped = True

    def restart(self, reason=""):
        return reason

    def health_check(self, idle_timeout=25.0):
        return None

    @property
    def epoch(self):
        return 1

    @property
    def is_running(self):
        return self.started and not self.stopped


class FakeStore:
    """记录 append 的内容"""

    def __init__(self):
        self.recs: list[dict] = []

    def append(self, rec):
        self.recs.append(rec)


def make_data(queries):
    ds = DataSource()
    ds._cases = [TestCase(id=str(i + 1), query=q) for i, q in enumerate(queries)]
    return ds


def run_injector(injector, data, mode=InjectMode.SEQUENTIAL, start_from=0, timeout=15.0):
    """启动并等待完成"""
    injector.start_batch(data, mode=mode, start_from=start_from)
    t0 = time.time()
    while injector.state != InjectState.IDLE:
        if time.time() - t0 > timeout:
            raise TimeoutError("注入器未在预期时间内结束")
        time.sleep(0.02)


# ── [1] 状态机 + 基本流程 ──
def test_state_machine():
    print("[1] 状态机流转与基本流程")
    events = []
    adb = FakeAdb()
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t, pass_set={"q1"}),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.05, timeout=2.0)
    inj.set_callbacks(
        on_case_start=lambda c: events.append(("start", c.id)),
        on_case_done=lambda c, r: events.append(("done", c.id, r.passed)),
        on_all_done=lambda: events.append(("all_done",)),
        on_state_change=lambda s: events.append(("state", s.value)),
    )

    data = make_data(["q1", "q2"])
    check("初始状态 IDLE", inj.state == InjectState.IDLE)
    run_injector(inj, data)

    states = [e[1] for e in events if e[0] == "state"]
    check("经历 RUNNING 最终回到 IDLE",
          states[0] == "running" and states[-1] == "idle", str(states))
    check("on_case_start 覆盖两条", [e[1] for e in events if e[0] == "start"] == ["1", "2"],
          str([e for e in events if e[0] == "start"]))
    check("on_case_done 覆盖两条",
          [e[2] for e in events if e[0] == "done"] == [True, False],
          str([e for e in events if e[0] == "done"]))
    check("on_all_done 被调用", ("all_done",) in events)
    check("灌入内容正确", adb.sent == ["q1", "q2"], str(adb.sent))
    check("用例状态回填", [c.status for c in data.cases] == ["pass", "fail"],
          str([c.status for c in data.cases]))
    check("判定字段回填", data.cases[0].asr_text == "q1" and data.cases[0].intent_type == "fake_domain")


# ── [2] 发送失败不计入已灌入 ──
def test_send_failure():
    print("[2] 发送失败：status=error、send_ok=False、不中断批处理")
    store = FakeStore()
    adb = FakeAdb(results=[(False, "rc=1: 模拟失败"), (True, "OK")])
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t, pass_set={"q2"}),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.05, timeout=2.0)
    inj.set_progress_store(store, "skip_executed")
    done = []
    inj.set_callbacks(on_case_done=lambda c, r: done.append((c.id, c.status, r.passed)))

    data = make_data(["q1", "q2"])
    run_injector(inj, data)

    check("第一条 error", data.cases[0].status == "error", data.cases[0].status)
    check("第二条继续执行且 pass", data.cases[1].status == "pass", data.cases[1].status)
    check("on_case_done 都被调用", len(done) == 2, str(done))
    rec1 = [r for r in store.recs if r["run_id"] == "1"]
    check("失败条 send_ok=False", rec1 and rec1[0]["send_ok"] is False, str(rec1))
    check("失败条 status=send_fail", rec1 and rec1[0]["status"] == "send_fail", str(rec1))


# ── [3] 异常不中断 ──
def test_exception_does_not_break():
    print("[3] 单条异常不中断批处理")
    adb = FakeAdb()
    adb.raise_on = {"q1"}
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t, pass_set={"q2"}),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.05, timeout=2.0)

    data = make_data(["q1", "q2"])
    run_injector(inj, data)

    check("异常条 status=error", data.cases[0].status == "error", data.cases[0].status)
    check("后续条正常执行", data.cases[1].status == "pass", data.cases[1].status)


# ── [4] 跳过策略三档 ──
def test_resume_modes():
    print("[4] 跳过策略：skip_executed / retry_failed / redo_all")

    def run_with(mode, prev):
        store = FakeStore()
        adb = FakeAdb()
        inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t),
                       monitor_factory=lambda q: FakeMonitor(q))
        inj.set_params(interval=0.02, timeout=2.0)
        inj.set_progress_store(store, mode, prev)
        data = make_data(["q1", "q2", "q3"])
        run_injector(inj, data)
        return adb.sent, inj.skipped_count

    sent, skipped = run_with("skip_executed", {"1": "pass", "2": "fail"})
    check("skip_executed：已执行的都跳过", sent == ["q3"], str(sent))
    check("skip_executed：跳过计数=2", skipped == 2, str(skipped))

    sent, skipped = run_with("retry_failed", {"1": "pass", "2": "fail"})
    check("retry_failed：只跳过 pass", sent == ["q2", "q3"], str(sent))
    check("retry_failed：跳过计数=1", skipped == 1, str(skipped))

    sent, skipped = run_with("redo_all", {"1": "pass", "2": "fail"})
    check("redo_all：全部重灌", sent == ["q1", "q2", "q3"], str(sent))
    check("redo_all：无跳过", skipped == 0, str(skipped))

    # 无 store 时行为与旧版一致（不跳过）
    adb = FakeAdb()
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.02, timeout=2.0)
    data = make_data(["q1", "q2"])
    run_injector(inj, data)
    check("未设置 store → 不跳过", adb.sent == ["q1", "q2"], str(adb.sent))


# ── [5] start_from ──
def test_start_from():
    print("[5] start_from 从指定下标开始")
    adb = FakeAdb()
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.02, timeout=2.0)
    data = make_data(["q1", "q2", "q3"])
    run_injector(inj, data, start_from=1)
    check("从下标 1 开始", adb.sent == ["q2", "q3"], str(adb.sent))

    # 与跳过策略组合（2026-10-15「从指定序号起重灌」= redo_all + start_index）：
    # 前段**不进入执行循环**（故不计入 skipped），第 N 条起按策略判定
    store = FakeStore()
    adb2 = FakeAdb()
    inj2 = Injector(adb=adb2, judger_factory=lambda t: FakeJudger(t),
                    monitor_factory=lambda q: FakeMonitor(q))
    inj2.set_params(interval=0.02, timeout=2.0)
    inj2.set_progress_store(store, "redo_all", {"1": "pass", "2": "pass"})
    run_injector(inj2, make_data(["q1", "q2", "q3"]), start_from=2)
    check("start_from 与跳过策略组合：仅第 3 条执行",
          adb2.sent == ["q3"], str(adb2.sent))
    check("start_from：前段不计入跳过计数", inj2.skipped_count == 0,
          str(inj2.skipped_count))


# ── [6] pause / resume ──
def test_pause_resume():
    print("[6] pause / resume")
    adb = FakeAdb()
    inj = Injector(adb=adb, judger_factory=lambda t: FakeJudger(t),
                   monitor_factory=lambda q: FakeMonitor(q))
    inj.set_params(interval=0.3, timeout=2.0)   # 拉长间隔便于观察暂停
    inj.set_callbacks(on_case_done=lambda c, r: None)

    data = make_data(["q1", "q2", "q3"])
    inj.start_batch(data, mode=InjectMode.INTERVAL)

    # 等第一条完成
    t0 = time.time()
    while len(adb.sent) < 1 and time.time() - t0 < 5:
        time.sleep(0.02)

    inj.pause()
    check("暂停后状态 PAUSED", inj.state == InjectState.PAUSED, inj.state.value)
    n_at_pause = len(adb.sent)
    time.sleep(0.5)
    check("暂停期间不继续灌入", len(adb.sent) == n_at_pause,
          f"{n_at_pause} → {len(adb.sent)}")

    inj.resume()
    check("恢复后状态 RUNNING", inj.state == InjectState.RUNNING, inj.state.value)
    t0 = time.time()
    while inj.state != InjectState.IDLE and time.time() - t0 < 15:
        time.sleep(0.02)
    check("最终完成全部灌入", len(adb.sent) == 3, str(adb.sent))


# ── [7] monitor 生命周期 ──
def test_monitor_lifecycle():
    print("[7] 日志监控随批次启停")
    created = []

    def factory(q):
        m = FakeMonitor(q)
        created.append(m)
        return m

    inj = Injector(adb=FakeAdb(), judger_factory=lambda t: FakeJudger(t),
                   monitor_factory=factory)
    inj.set_params(interval=0.02, timeout=2.0)
    data = make_data(["q1"])
    run_injector(inj, data)

    check("监控被创建并启动", created and created[0].started, str(created))
    check("批次结束后监控被停止", created and created[0].stopped, str(created))
    check("批次结束后 monitor 引用清空", inj._monitor is None)


ALL_TESTS = (test_state_machine, test_send_failure, test_exception_does_not_break,
             test_resume_modes, test_start_from, test_pause_resume, test_monitor_lifecycle)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("Injector 编排自测全部通过（全程无设备）")
