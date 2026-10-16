# -*- coding: utf-8 -*-
"""AdbSession 离线自测（不连接真实设备）

覆盖：
  1) _base_args 设备前缀拼接
  2) device_timestamp 的 TTL 缓存（批量灌入时避免每条的 adb 往返）
  3) check_device 的缓存命中不重复探测
  4) send_voice 的 exit 255 → 重连 → 重试一次（关键可靠性逻辑）
  5) send_voice 的失败分支不误报成功
  6) shell 便捷方法的返回语义
  7) push/pull 的本地目录自动创建

用法:
    python tests/test_adb.py
    pytest tests/test_adb.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.adb import AdbSession                       # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeResult:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ── [1] _base_args ──
def test_base_args():
    print("[1] _base_args 设备前缀")
    check("无设备", AdbSession._base_args() == ["adb"])
    check("指定设备", AdbSession._base_args("ABC") == ["adb", "-s", "ABC"])


# ── [2] device_timestamp TTL ──
def test_device_timestamp_ttl():
    print("[2] device_timestamp 的 TTL 缓存")
    s = AdbSession()
    calls = []
    s._query_device_timestamp = lambda: (calls.append(1), "09-15 10:00:00.123")[1]

    t1 = s.device_timestamp()
    check("首次查询返回设备时间", t1 == "09-15 10:00:00.123", f"t1={t1!r}")
    check("查询执行了 1 次", len(calls) == 1, f"calls={len(calls)}")

    s.device_timestamp()
    check("TTL 内复用缓存（不重复查询）", len(calls) == 1, f"calls={len(calls)}")

    # 模拟缓存过期
    s._dev_ts_cache = (time.time() - s.DEV_TS_TTL - 1, "old")
    s.device_timestamp()
    check("TTL 过期后重新查询", len(calls) == 2, f"calls={len(calls)}")


def test_device_timestamp_empty_on_failure():
    print("[2b] 设备时间查询失败返回空串（不抛异常）")
    s = AdbSession()
    s._query_device_timestamp = lambda: ""
    check("返回空串", s.device_timestamp() == "")


# ── [3] check_device 缓存 ──
def test_check_device_cache():
    print("[3] check_device 缓存命中不重复探测")
    s = AdbSession()
    s._cached_device_id = "DEV1"
    s._cached_device_time = time.time()

    def _boom():
        raise AssertionError("缓存命中时不应探测设备")

    s._probe_devices = _boom
    ok, dev = s.check_device()
    check("缓存命中返回设备", ok is True and dev == "DEV1", f"({ok}, {dev!r})")

    s.invalidate_cache()
    s._probe_devices = lambda: (True, "DEV2", False)
    ok2, dev2 = s.check_device()
    check("缓存失效后重新探测", ok2 is True and dev2 == "DEV2", f"({ok2}, {dev2!r})")


def test_check_device_daemon_repair():
    print("[3b] daemon 故障 → 自动修复 → 重试成功")
    s = AdbSession()
    seq = [(False, "ADB 服务异常", True), (True, "DEV9", False)]
    s._probe_devices = lambda: seq.pop(0)
    repaired = []
    s._repair_daemon = lambda: repaired.append(1)

    ok, dev = s.check_device(force=True)
    check("修复后成功", ok is True and dev == "DEV9", f"({ok}, {dev!r})")
    check("执行了 daemon 修复", len(repaired) == 1)


# ── [4] send_voice 255 重连重试 ──
def test_send_voice_255_retry():
    print("[4] send_voice exit 255 → 重连 → 重试一次")
    s = AdbSession()
    calls = []
    results = [FakeResult(255), FakeResult(0, "OK")]

    def fake_run(cmd, timeout=None):
        calls.append(list(cmd))
        return results.pop(0)

    s.run = fake_run
    s.reconnect = lambda device_id="": (True, "重连成功")

    ok, msg = s.send_voice("测试", "DEV1")
    check("重试后成功", ok is True, f"({ok}, {msg!r})")
    check("命令执行了 2 次", len(calls) == 2, f"calls={len(calls)}")
    check("两次命令完全相同", calls[0] == calls[1])


def test_send_voice_255_reconnect_failed():
    print("[4b] send_voice 255 后重连失败 → 返回失败")
    s = AdbSession()
    s.run = lambda cmd, timeout=None: FakeResult(255)
    s.reconnect = lambda device_id="": (False, "重连超时")

    ok, msg = s.send_voice("测试", "DEV1")
    check("返回失败", ok is False, f"({ok}, {msg!r})")
    check("失败原因含重连信息", "重连" in msg, f"msg={msg!r}")


# ── [5] 失败分支 ──
def test_send_voice_failure():
    print("[5] send_voice 失败分支不误报成功")
    s = AdbSession()
    s.run = lambda cmd, timeout=None: FakeResult(1, "", "Error: not found")
    ok, msg = s.send_voice("测试", "DEV1")
    check("返回失败", ok is False, f"({ok}, {msg!r})")
    check("含返回码", "rc=1" in msg, f"msg={msg!r}")


# ── [6] shell 便捷方法 ──
def test_shell():
    print("[6] shell 便捷方法的返回语义")
    s = AdbSession()
    s.run = lambda cmd, timeout=None: FakeResult(0, "hello\n", "")
    ok, out = s.shell("echo hello", "DEV1")
    check("成功返回 True + 输出", ok is True and out == "hello", f"({ok}, {out!r})")

    s.run = lambda cmd, timeout=None: FakeResult(1, "", "not found")
    ok2, out2 = s.shell("badcmd", "DEV1")
    check("失败返回 False + 错误输出", ok2 is False and out2 == "not found", f"({ok2}, {out2!r})")


# ── [7] 文件传输 ──
def test_pull_creates_local_dir(tmp=None):
    print("[7] pull 自动创建本地目标目录")
    import tempfile
    s = AdbSession()
    captured = []

    def fake_run(cmd, timeout=None):
        captured.append(list(cmd))
        return FakeResult(0, "1 file pulled", "")

    s.run = fake_run
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "sub" / "dir" / "file.txt"
        ok, _ = s.pull("/sdcard/file.txt", target, "DEV1")
        check("返回成功", ok is True)
        check("本地父目录已创建", target.parent.is_dir())
        check("命令含 pull 与路径", captured and "pull" in captured[0], f"cmd={captured[0] if captured else None}")


# ── [8] 全局设备（ANDROID_SERIAL）──
def _with_env_serial(value, fn):
    """在 ANDROID_SERIAL=value 下执行 fn（结束后恢复原值）"""
    import os
    old = os.environ.get("ANDROID_SERIAL")
    try:
        if value is None:
            os.environ.pop("ANDROID_SERIAL", None)
        else:
            os.environ["ANDROID_SERIAL"] = value
        fn()
    finally:
        if old is None:
            os.environ.pop("ANDROID_SERIAL", None)
        else:
            os.environ["ANDROID_SERIAL"] = old


def test_env_serial_preferred():
    print("[8] 环境变量设备优先（探测选设备）")

    def body():
        s = AdbSession()
        s.run = lambda cmd, timeout=None: FakeResult(
            0, "List of devices attached\nDEV1\tdevice\nDEV2\tdevice\n", "")
        ok, dev, _ = s._probe_devices()
        check("期望设备在线时优先返回", ok is True and dev == "DEV2", f"({ok}, {dev!r})")

        import os
        os.environ["ANDROID_SERIAL"] = "DEV9"
        ok2, dev2, _ = s._probe_devices()
        check("期望设备离线时回退第一台", ok2 is True and dev2 == "DEV1", f"({ok2}, {dev2!r})")

    _with_env_serial("DEV2", body)


def test_env_change_invalidates_cache():
    print("[8b] 期望设备与缓存不一致时不走缓存（切换后不吃旧设备）")

    def body():
        import os
        s = AdbSession()
        s._cached_device_id = "DEV1"
        s._cached_device_time = time.time()
        ok, dev = s.check_device()
        check("缓存与期望一致时命中", ok is True and dev == "DEV1", f"({ok}, {dev!r})")

        os.environ["ANDROID_SERIAL"] = "DEV2"
        s._probe_devices = lambda: (True, "DEV2", False)
        ok2, dev2 = s.check_device()
        check("期望变化后重探测", ok2 is True and dev2 == "DEV2", f"({ok2}, {dev2!r})")

    _with_env_serial("DEV1", body)


def test_timestamp_uses_device():
    print("[8c] 设备时钟查询带 -s（多设备不静默空串）")
    import cartest.core.adb as adb_mod

    class FakeCP:
        returncode = 0
        stdout = "'09-15 10:00:00.123'\n"
        stderr = ""

    orig_run = adb_mod.subprocess.run
    calls: list[list[str]] = []

    def body():
        s = AdbSession()
        adb_mod.subprocess.run = lambda cmd, **kw: (calls.append(list(cmd)), FakeCP())[1]
        ts = s._query_device_timestamp()
        check("返回时间戳", ts == "09-15 10:00:00.123", f"ts={ts!r}")
        check("命令带 -s DEV7", calls and calls[0][:3] == ["adb", "-s", "DEV7"],
              f"cmd={calls[0][:3] if calls else None}")

        # 环境变量为空时回退探测缓存设备
        import os
        os.environ.pop("ANDROID_SERIAL", None)
        s._cached_device_id = "DEV5"
        s._query_device_timestamp()
        check("回退缓存设备", len(calls) == 2 and calls[1][:3] == ["adb", "-s", "DEV5"],
              f"cmd={calls[1][:3] if len(calls) > 1 else None}")

    try:
        _with_env_serial("DEV7", body)
    finally:
        adb_mod.subprocess.run = orig_run


ALL_TESTS = (test_base_args, test_device_timestamp_ttl, test_device_timestamp_empty_on_failure,
             test_check_device_cache, test_check_device_daemon_repair,
             test_send_voice_255_retry, test_send_voice_255_reconnect_failed,
             test_send_voice_failure, test_shell, test_pull_creates_local_dir,
             test_env_serial_preferred, test_env_change_invalidates_cache,
             test_timestamp_uses_device)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("AdbSession 自测全部通过")
