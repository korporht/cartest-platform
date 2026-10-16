# -*- coding: utf-8 -*-
"""LogcatMonitor 可靠性自测（无需真机）

覆盖 2026-09-14 修复点：
  1) 队列满丢行计数 + ("warn", ...) 告警（不再静默丢弃）
  2) 监控线程内所有投递均非阻塞 —— 满队列下线程不会被卡死（回归项）
  3) health_check(): logcat 进程退出 / 长时间无原始输出 / 监控未运行 → 自动重启日志流
  4) 重启后旧流的 ("stopped", "<epoch>") 不上报（epoch 守卫）
  5) 原始输出心跳统计（未命中关键词的日志也算"流还活着"）

来源：CarVoiceTestTool/self_test_monitor.py 平移（2026-09-14）。

用法:
    python tests/test_logcat.py
    pytest tests/test_logcat.py
"""
import queue
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core import logcat as log_monitor                       # noqa: E402
from cartest.core.logcat import LogcatMonitor as LogMonitor           # noqa: E402

FAILED = []
MATCHING_LINE = "01-01 00:00:00.000  100  200 I isssr_log: cbm something"

#: 原始 subprocess 模块（patch 后用于恢复，避免污染其它测试）
_ORIG_SUBPROCESS = log_monitor.subprocess


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeProc:
    """模拟 logcat 子进程"""

    def __init__(self, lines=None, block_forever=False, exit_code=None):
        self._lines = lines or []
        self._block = block_forever
        self._code = exit_code
        self.terminated = False

    @property
    def stdout(self):
        for line in self._lines:
            yield line
        if self._block:
            while not self.terminated:
                yield ""
                time.sleep(0.05)

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class FlakyQueue:
    """第一次收到 ("log", ...) 时抛 Full，其余正常接收（用于确定性地触发丢行告警）"""

    def __init__(self):
        self.items = []
        self._failed_once = False

    def put_nowait(self, item):
        if item[0] == "log" and not self._failed_once:
            self._failed_once = True
            raise queue.Full
        self.items.append(item)

    def empty(self):
        return not self.items

    def get(self):
        return self.items.pop(0)


def patch(fake_proc):
    """把 logcat 模块内的 subprocess 换成不真跑 adb 的假实现"""
    log_monitor.subprocess = types.SimpleNamespace(
        Popen=lambda *a, **k: fake_proc,
        run=lambda *a, **k: types.SimpleNamespace(returncode=0),
        TimeoutExpired=_ORIG_SUBPROCESS.TimeoutExpired,
        PIPE=_ORIG_SUBPROCESS.PIPE,
    )


def unpatch():
    log_monitor.subprocess = _ORIG_SUBPROCESS


def test_warn_on_drop():
    print("[1] 队列满丢行计数 + warn 告警")
    q = FlakyQueue()
    patch(FakeProc(lines=[MATCHING_LINE]))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._run(1)
    kinds = [x[0] for x in q.items]
    check("丢行计数 1", m.dropped == 1, f"dropped={m.dropped}")
    check("有 warn 告警", kinds.count("warn") == 1, f"kinds={kinds}")
    check("stopped 带 epoch", ("stopped", "1") in q.items, f"items={q.items}")


def test_no_block_on_full_queue():
    print("[2] 满队列下监控线程不被卡死（回归：原实现 q.put 阻塞）")
    q = queue.Queue(maxsize=1)
    q.put(("log", "占位"))
    patch(FakeProc(lines=[MATCHING_LINE, MATCHING_LINE]))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    t0 = time.time()
    m._run(1)                      # 原实现在此处会永久阻塞
    cost = time.time() - t0
    check("_run 迅速返回", cost < 2.0, f"耗时 {cost:.2f}s")
    check("丢弃已计数（2 条日志 + 1 条 stopped）", m.dropped == 3, f"dropped={m.dropped}")


def test_stale_stopped_suppressed():
    print("[3] 重启后旧流不上报 stopped（epoch 守卫）")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(lines=[MATCHING_LINE]))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 2                   # 当前流已是第 2 代
    m._run(1)                      # 旧线程（epoch=1）收尾
    kinds = []
    while not q.empty():
        kinds.append(q.get()[0])
    check("无 stopped 上报", "stopped" not in kinds, f"kinds={kinds}")


def test_health_proc_exited():
    print("[4] logcat 进程退出 → 自动重启")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=1))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._proc = FakeProc(block_forever=True, exit_code=1)
    m._last_read_wall = time.time()
    reason = m.health_check(idle_timeout=25)
    check("返回退出原因", bool(reason) and "已退出" in reason, f"reason={reason!r}")
    check("重启计数 +1", m._restarts == 1, f"restarts={m._restarts}")


def test_health_idle():
    print("[5] 长时间无原始输出 → 自动重启")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=None))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._proc = FakeProc(block_forever=True, exit_code=None)
    m._last_read_wall = time.time() - 40
    reason = m.health_check(idle_timeout=25)
    check("返回无输出原因", bool(reason) and "无任何输出" in reason, f"reason={reason!r}")


def test_health_ok_and_not_running():
    print("[6] 健康时不误报 / 未运行时重启")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=None))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._proc = FakeProc(block_forever=True, exit_code=None)
    m._last_read_wall = time.time()
    check("健康返回 None", m.health_check(idle_timeout=25) is None)
    m._running = False
    check("未运行触发重启", m.health_check(idle_timeout=25) == "监控未运行")
    st = m.stats()
    check("stats 字段齐全",
          set(st) == {"epoch", "restarts", "dropped", "lines_read", "lines_matched", "idle_s"},
          f"stats={st}")


def test_lines_read_heartbeat():
    print("[7] 原始输出心跳（未命中关键词也算流是活的）")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(lines=["I OtherApp: nothing relevant", "I OtherApp: still nothing"]))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._last_read_wall = 0.0
    m._run(1)
    check("lines_read=2", m.stats()["lines_read"] == 2, f"stats={m.stats()}")
    check("未命中行不入队", m.stats()["lines_matched"] == 0)
    check("心跳已更新", m._last_read_wall > 0)


def test_pump_stopped_restart():
    print("[8] pump：stopped（当前世代）触发重启并通知")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=None))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 3
    m._proc = FakeProc(block_forever=True, exit_code=None)
    m._last_read_wall = time.time()
    msgs = []
    q.put(("stopped", "3"))
    q.put(("log", MATCHING_LINE))
    restarted = log_monitor.pump(q, _J(), m, on_message=lambda t, x: msgs.append((t, x)))
    check("返回已重启", restarted is True)
    check("重启计数 +1", m._restarts == 1, f"restarts={m._restarts}")
    check("有重启通知", any("已自动重启监控" in x for _, x in msgs), f"msgs={msgs}")


def test_pump_stale_stopped_ignored():
    print("[9] pump：非当前世代的 stopped 不触发重启")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=None))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 5
    m._proc = FakeProc(block_forever=True, exit_code=None)
    m._last_read_wall = time.time()
    m._health_last = time.time()          # 跳过健康检查以隔离本项
    q.put(("stopped", "4"))
    restarted = log_monitor.pump(q, _J(), m)
    check("不重启", restarted is False and m._restarts == 0, f"restarts={m._restarts}")


def test_pump_health_watchdog():
    print("[10] pump：health_check 触发重启（含节流）")
    q = queue.Queue(maxsize=100)
    patch(FakeProc(block_forever=True, exit_code=1))
    m = LogMonitor(q)
    m._running = True
    m._epoch = 1
    m._proc = FakeProc(block_forever=True, exit_code=1)
    m._last_read_wall = time.time()
    msgs = []
    first = log_monitor.pump(q, _J(), m, on_message=lambda t, x: msgs.append((t, x)))
    second = log_monitor.pump(q, _J(), m, on_message=lambda t, x: msgs.append((t, x)))
    check("首次触发重启", first is True, f"restarts={m._restarts}")
    check("健康检查有节流（第二次不重复判定）", second is False)
    check("原因含'已退出'", any("已退出" in x for _, x in msgs), f"msgs={msgs}")


def test_consumer_callback_order():
    print("[11] pump 回调顺序约定为 (tag, msg)")
    q = queue.Queue(maxsize=100)
    q.put(("warn", "hello"))
    got = []
    log_monitor.pump(q, _J(), None, on_message=lambda a, b: got.append((a, b)))
    check("(tag, msg) 顺序正确", got == [("warn", "hello")], f"got={got}")


class _J:
    """最小 judger 替身"""

    def __init__(self):
        self.lines = []

    def feed_log(self, line):
        self.lines.append(line)


ALL_TESTS = (test_warn_on_drop, test_no_block_on_full_queue, test_stale_stopped_suppressed,
             test_health_proc_exited, test_health_idle, test_health_ok_and_not_running,
             test_lines_read_heartbeat, test_pump_stopped_restart, test_pump_stale_stopped_ignored,
             test_pump_health_watchdog, test_consumer_callback_order)


if __name__ == "__main__":
    try:
        for fn in ALL_TESTS:
            fn()
    finally:
        unpatch()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("LogcatMonitor 自测全部通过")
