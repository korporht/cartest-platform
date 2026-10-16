# -*- coding: utf-8 -*-
"""P3c-2 验收 — core.adb 使用配置主路径（离线：替身 run，不连设备）

覆盖：
  1) 未配置 → 回退默认 LOG_REMOTE_DIR（与旧行为一致）
  2) 配置主路径 → clean_logs 的 rm 目标用配置值
  3) pull_logs 的 ls/pull 目标同样用配置值

注：日志路径自 2026-10-12 起**属于方案**（``project.log.*``），故这里经
``project.save_values`` 写方案文件（＝配置页保存走的同一条链路），不再写 settings。

用法:
    python tests/test_wiring_core_adb.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core import config_center, project   # noqa: E402
from cartest.core.adb import AdbSession  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeResult:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def main() -> int:
    tmp = Path(mkdtemp(prefix="p3c2_core_"))
    config_center.set_settings_file(tmp / "settings.json")   # 隔离
    project.set_projects_dir(tmp / "projects")               # 隔离：不碰真实方案文件

    print("=" * 70)
    print("P3c-2 验收 — core.adb 配置主路径（离线替身）")
    print(f"临时配置: {tmp / 'settings.json'}")
    print("=" * 70)

    s = AdbSession()
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        calls.append(list(cmd))
        return FakeResult(0, "a.log\n" if "ls" in cmd else "", "")
    s.run = fake_run

    print("\n[1] 未配置 → 回退默认（旧行为）")
    calls.clear()
    s.clean_logs()
    check("rm 用默认路径",
          calls[-1] == ["adb", "shell", "rm", "-rf", "/data/log/voice/*"],
          str(calls[-1]))

    print("\n[2] 配置主路径 → clean_logs 用配置值")
    ok, why = project.save_values({"project.log.pull_paths": [
        {"path": "/custom/primary/", "alias": "Custom", "enabled": True},
    ]})
    if not ok:
        raise RuntimeError(f"写方案值失败：{why}")
    calls.clear()
    s.clean_logs()
    check("rm 用配置路径",
          calls[-1] == ["adb", "shell", "rm", "-rf", "/custom/primary/*"],
          str(calls[-1]))

    print("\n[3] pull_logs 的 ls/pull 同样用配置值")
    calls.clear()
    out = tmp / "pulled"
    ok, msg, n = s.pull_logs(out)
    check("ls 用配置路径", ["adb", "shell", "ls", "/custom/primary/"] in calls, str(calls))
    check("pull 单文件到本地",
          ["adb", "pull", "/custom/primary/a.log", str(out / "a.log")] in calls, str(calls))
    check("返回 (True, 已保存 1/1, 1)", ok is True and n == 1, f"ok={ok} n={n} msg={msg}")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("core.adb 配置主路径测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
