"""迁移一致性验证 —— adb 模块

对比新旧 ADB 封装在**相同输入**下构造的命令是否逐字节一致。
不连接真实设备：monkeypatch ``run()`` 捕获命令。

用法：
    python tools/verify_adb.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from _legacy import require as _require_legacy   # noqa: E402  同目录定位器
CVT_DIR = _require_legacy("CarVoiceTestTool")     # 缺失时打印 [SKIP] 并退出 0
CARTEST_DIR = Path(__file__).resolve().parents[1]

PASS, FAIL = "[PASS]", "[FAIL]"
_results: list[tuple[bool, str]] = []


def check(name: str, old, new) -> None:
    ok = old == new
    _results.append((ok, name))
    print(f"{PASS if ok else FAIL} {name}")
    if not ok:
        print(f"      旧实现: {old}")
        print(f"      新实现: {new}")


class FakeResult:
    """subprocess.CompletedProcess 的最小替身"""

    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def capture_commands(session, fn) -> list[list[str]]:
    """执行 fn 并捕获其产生的所有命令行"""
    box: list[list[str]] = []
    original_run = session.run

    def fake_run(cmd, timeout=None):
        box.append(list(cmd))
        return FakeResult(0, "OK", "")

    session.run = fake_run
    try:
        fn()
    finally:
        session.run = original_run
    return box


def main() -> int:
    print("=" * 68)
    print("迁移一致性验证：adb 模块")
    print("=" * 68)

    # ── 旧实现 ──
    sys.path.insert(0, str(CVT_DIR))
    from adb_manager import ADBManager                    # noqa: E402

    old = ADBManager()

    # ── 新实现 ──
    sys.path.insert(0, str(CARTEST_DIR))
    # 环境隔离（2026-10-10）：对照以「内置 e0v 默认」为前提——
    # 断开真实 settings.json 与项目档案，免受本机多平台切换（t28/t1ma）影响
    import tempfile as _tf
    from cartest.core import config_center as _cc
    from cartest.core import project as _pj
    _iso = Path(_tf.mkdtemp(prefix="cartest_verify_adb_iso_"))
    _cc.set_settings_file(_iso / "settings.json")
    _pj.set_projects_dir(_iso / "projects")
    from cartest.core.adb import AdbSession               # noqa: E402

    new = AdbSession()

    print("\n[1] _base_args（设备前缀）")
    for dev in ("", "ABC123"):
        check(f"  device_id={dev!r}", ADBManager._base_args(dev), AdbSession._base_args(dev))

    print("\n[2] send_voice 命令构造（语义注入，含中文/空格/特殊字符）")
    for text in ("最近的新闻", "你好 世界", "A&B?C#D", "导航到南京南站"):
        oc = capture_commands(old, lambda t=text: old.send_voice(t, "DEV1"))
        nc = capture_commands(new, lambda t=text: new.send_voice(t, "DEV1"))
        check(f"  query={text!r}", oc, nc)

    print("\n[3] send_voice 的 mode 参数传递")
    for mode in (1, 3):
        oc = capture_commands(old, lambda m=mode: old.send_voice("测试", "DEV1", mode=m))
        nc = capture_commands(new, lambda m=mode: new.send_voice("测试", "DEV1", mode=m))
        check(f"  mode={mode}", oc, nc)

    print("\n[4] send_voice 无 device_id（自动检测模式）")
    oc = capture_commands(old, lambda: old.send_voice("测试", ""))
    nc = capture_commands(new, lambda: new.send_voice("测试", ""))
    check("  device_id=''", oc, nc)

    print("\n[5] clean_logs 命令")
    oc = capture_commands(old, lambda: old.clean_logs("DEV1"))
    nc = capture_commands(new, lambda: new.clean_logs("DEV1"))
    check("  clean_logs", oc, nc)

    print("\n[6] send_voice 返回值（成功路径）")
    old.run = lambda cmd, timeout=None: FakeResult(0, "Starting service: Intent ok", "")
    new.run = lambda cmd, timeout=None: FakeResult(0, "Starting service: Intent ok", "")
    check("  ok/msg 一致", old.send_voice("测试", "DEV1"), new.send_voice("测试", "DEV1"))

    print("\n[7] send_voice 返回值（失败路径 rc=1）")
    old.run = lambda cmd, timeout=None: FakeResult(1, "", "Error: not found")
    new.run = lambda cmd, timeout=None: FakeResult(1, "", "Error: not found")
    check("  失败信息一致", old.send_voice("测试", "DEV1"), new.send_voice("测试", "DEV1"))

    # ── 汇总 ──
    print()
    print("=" * 68)
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"结果: {passed}/{total} 通过")
    for ok, name in _results:
        if not ok:
            print(f"  {FAIL}{name}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
