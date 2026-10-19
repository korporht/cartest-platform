# -*- coding: utf-8 -*-
"""LogcatService / LineBatcher 自测（离线，无需真机）

覆盖 2026-09-23 实时抓取修复（35 号文档 §六，用户实测"抓到的全是旧日志"）：
  1) 命令构造：``-T 1`` 实时起流 / ``-s 设备序列号`` / 列表式（无 shell）/ 缓冲参数拆分
  2) 「抓取前清空」分支：仅勾选时才执行 ``logcat -c``
  3) 停止确定性：terminate 直杀 adb → 读管道 EOF → on_stopped 必达（回归旧实现卡死窗口）
  4) 最大时长自动停止：计时器到点 → stop → 收尾
  5) LineBatcher：定时合并上屏 / 积满阈值即时冲刷 / stop 冲刷残余

用法:
    python tests/test_logcat_service.py
    pytest tests/test_logcat_service.py
"""
import subprocess as _real_subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.app.tools.services import logcat_service as svc_mod          # noqa: E402
from cartest.app.tools.services.line_batcher import LineBatcher           # noqa: E402

FAILED = []
_ORIG_SUBPROCESS = svc_mod.subprocess

POPEN_CALLS: list = []
RUN_CALLS: list = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeProc:
    """可控假子进程：先吐 lines，再阻塞直到 terminate（模拟真实管道 EOF 语义）"""

    def __init__(self, lines=None, block_after=True):
        self._lines = lines or []
        self._block = block_after
        self.terminated = False

    @property
    def stdout(self):
        for ln in self._lines:
            yield ln
        if self._block:
            while not self.terminated:
                time.sleep(0.01)

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def install_fakes(proc):
    """把 service 模块内的 subprocess 换成不真跑 adb 的假实现"""
    POPEN_CALLS.clear()
    RUN_CALLS.clear()
    svc_mod.subprocess = types.SimpleNamespace(
        Popen=lambda cmd, **kw: (POPEN_CALLS.append((cmd, kw)), proc)[1],
        run=lambda cmd, **kw: (RUN_CALLS.append(cmd),
                               types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1],
        TimeoutExpired=_real_subprocess.TimeoutExpired,
        PIPE=_real_subprocess.PIPE,
        STDOUT=_real_subprocess.STDOUT,
    )


def uninstall_fakes():
    svc_mod.subprocess = _ORIG_SUBPROCESS


def test_command_build():
    print("[1] 命令构造：-T 1 实时起流 + -s 序列号 + 列表式（无 shell）")
    install_fakes(FakeProc(lines=["09-23 18:00:00.000  1  2 I tag: hello"],
                           block_after=False))
    svc = svc_mod.LogcatService(on_log=lambda s: None)
    with tempfile.TemporaryDirectory() as td:
        svc.start(save_dir=td, buffer_label="-b main -b events -b crash",
                  device_id="ABC123")
    check("Popen 恰好 1 次", len(POPEN_CALLS) == 1, f"calls={len(POPEN_CALLS)}")
    cmd, kw = POPEN_CALLS[0]
    expect_prefix = ["adb", "-s", "ABC123", "logcat", "-T", "1", "-v", "threadtime"]
    check("命令前缀正确（-s / -T 1）", cmd[:8] == expect_prefix, f"cmd={cmd}")
    check("缓冲参数拆分正确", cmd[8:] == ["-b", "main", "-b", "events", "-b", "crash"],
          f"cmd={cmd}")
    check("列表式（未用 shell=True）", kw.get("shell") in (None, False),
          f"kwargs={list(kw)}")
    check("传 startupinfo（隐藏窗口）", "startupinfo" in kw, f"kwargs={list(kw)}")


def test_clear_branch():
    print("[2] 「抓取前清空」分支：勾选才执行 logcat -c")
    install_fakes(FakeProc(lines=[], block_after=False))
    svc = svc_mod.LogcatService(on_log=lambda s: None)
    with tempfile.TemporaryDirectory() as td:
        svc.start(save_dir=td, buffer_label="-b all", clear_enabled=True,
                  device_id="ABC123")
    check("勾选 → 执行 -c（含 -s）",
          RUN_CALLS and RUN_CALLS[0] == ["adb", "-s", "ABC123", "logcat", "-c"],
          f"runs={RUN_CALLS}")

    install_fakes(FakeProc(lines=[], block_after=False))
    svc2 = svc_mod.LogcatService(on_log=lambda s: None)
    with tempfile.TemporaryDirectory() as td2:
        svc2.start(save_dir=td2, buffer_label="-b all", clear_enabled=False)
    check("未勾选 → 不清空", RUN_CALLS == [], f"runs={RUN_CALLS}")


def test_stop_deterministic():
    print("[3] 停止确定性：terminate → 读循环立即收尾（on_stopped 必达）")
    proc = FakeProc(lines=["L1", "L2"], block_after=True)
    install_fakes(proc)
    stopped: list = []
    logs: list = []
    svc = svc_mod.LogcatService(on_log=logs.append)
    with tempfile.TemporaryDirectory() as td:
        t = threading.Thread(
            target=lambda: svc.start(save_dir=td, buffer_label="-b all",
                                     on_stopped=stopped.append, device_id="ABC123"),
            daemon=True)
        t.start()
        time.sleep(0.3)                      # 让 L1/L2 落盘、读循环进入阻塞段
        check("运行中", svc.is_running)
        t0 = time.time()
        svc.stop()
        t.join(timeout=3)
        cost = time.time() - t0
        check("读线程迅速收尾（旧实现可能卡到下一行）",
              (not t.is_alive()) and cost < 2.0, f"cost={cost:.2f}s")
        check("停止路径无异常上报（回归：close 与迭代竞态）",
              not any("[ERR]" in x for x in logs), f"logs={logs[-3:]}")
        check("正常收尾行出现（read loop/已保存）",
              any("已保存" in x for x in logs), f"logs={logs[-3:]}")
        check("on_stopped 已回调", len(stopped) == 1, f"stopped={stopped}")
        check("子进程已终止", proc.terminated)
        check("服务状态已复位", not svc.is_running)
        if stopped:
            content = Path(stopped[0]).read_text(encoding="utf-8").strip().splitlines()
            check("文件已落盘 2 行", content == ["L1", "L2"], f"content={content}")


def test_max_duration_autostop():
    print("[4] 最大时长自动停止（计时器 → stop → 收尾）")
    proc = FakeProc(lines=["X"], block_after=True)
    install_fakes(proc)
    stopped: list = []
    svc = svc_mod.LogcatService(on_log=lambda s: None, max_duration=1)
    with tempfile.TemporaryDirectory() as td:
        t0 = time.time()
        svc.start(save_dir=td, buffer_label="-b all", on_stopped=stopped.append)
        cost = time.time() - t0
    check("约 1s 后自动停止", 1.0 <= cost < 3.0, f"cost={cost:.2f}s")
    check("on_stopped 回调", len(stopped) == 1, f"stopped={stopped}")
    check("子进程已终止", proc.terminated)


def test_line_batcher():
    print("[5] LineBatcher：定时合并 / 洪峰窗口省略 / stop 收尾")
    batches: list = []
    b = LineBatcher(on_flush=batches.append, interval=0.05, max_batch=1000)
    b.start()
    for i in range(10):
        b.feed(f"l{i}")
    time.sleep(0.25)
    check("10 行合并为 1 批（不再逐行上屏）",
          len(batches) == 1 and len(batches[0]) == 10,
          f"batches={[len(x) for x in batches]}")
    b.feed("tail")
    b.stop()
    check("stop 冲刷残余", bool(batches) and batches[-1] == ["tail"],
          f"last={batches[-1] if batches else None}")

    batches2: list = []
    b2 = LineBatcher(on_flush=batches2.append, interval=60.0, max_batch=5)
    b2.start()
    for i in range(1, 7):
        b2.feed(str(i))
    check("洪峰裁剪不在调用方线程做重活（未冲刷）", batches2 == [],
          f"batches={batches2}")
    b2.flush()
    ok = (len(batches2) == 1 and batches2[0][1:] == ["2", "3", "4", "5", "6"]
          and "省略 1 行" in batches2[0][0])
    check("窗口保留最新 5 行 + 省略提示", ok, f"batch={batches2}")
    b2.stop()


ALL_TESTS = (test_command_build, test_clear_branch, test_stop_deterministic,
             test_max_duration_autostop, test_line_batcher)


if __name__ == "__main__":
    try:
        for fn in ALL_TESTS:
            fn()
    finally:
        uninstall_fakes()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("LogcatService / LineBatcher 自测全部通过")
