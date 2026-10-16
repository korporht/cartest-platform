# -*- coding: utf-8 -*-
"""P3d 验收 — APK「查询版本」通用化（源码断言 + 命令构造）

设计依据：12-P3d设计.md §4.2。

用法:
    python tests/test_apk_query_version.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "cartest" / "app" / "tools" / "apk_tab.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("P3d 验收 — APK「查询版本」通用化")
    print("=" * 70)

    from cartest.app.tools.apk_tab import ApkTab

    expected = ('adb shell "dumpsys package com.example.cameraapp '
                "| grep -m1 -E 'versionName|Unable to find package'\"")
    check("命令构造：整串引号（管道在设备侧执行）",
          ApkTab._version_cmd("com.example.cameraapp") == expected,
          ApkTab._version_cmd("com.example.cameraapp"))

    check("无旧「相机版本」按钮", "相机版本" not in SRC)
    check("无旧「相册版本」按钮", "相册版本" not in SRC)
    check("无硬编码相机包名", "com.android.camera" not in SRC)
    check("无硬编码相册包名", "com.android.gallery3d" not in SRC)
    check("有「查询版本」按钮", "查询版本" in SRC)
    check("作用于所选包名（_pkg_name）", "pkg = self._pkg_name()" in SRC)
    check("空包名守卫", "请先选择或输入包名" in SRC)
    check("未找到包分支", "未找到包" in SRC)
    check("非法字符守卫", "非法字符" in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("查询版本检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
