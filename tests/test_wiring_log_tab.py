# -*- coding: utf-8 -*-
"""P3c-2 验收 — 日志终端去开关（源码断言 + import 自检）

设计依据：08-P3c设计.md 用户裁决 ⑦（接受移除「含 AIOS / 含 DLT」两开关）。

用法:
    python tests/test_wiring_log_tab.py
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "cartest" / "app" / "tools" / "log_tab.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("P3c-2 验收 — 日志终端去开关（源码断言）")
    print("=" * 70)

    check("无 mnt_aios 引用", "mnt_aios" not in SRC)
    check("无 mnt_dlt 引用", "mnt_dlt" not in SRC)
    check("无「含 AIOS」文案", "含 AIOS" not in SRC)
    check("无「含 DLT」文案", "含 DLT" not in SRC)
    check("clear_logs 无参调用", "self._adb.clear_logs()" in SRC)
    check("pull_logs 新签名调用（本地目录可配置）",
          "self._adb.pull_logs(base, ts)" in SRC
          and "config_center.output_root()" in SRC)
    check("Logcat 实时抓取走可配置子目录",
          'save_dir=config_center.output_dir("logcat")' in SRC)
    check("车机截图走同一根目录的 screenshots 子目录",
          'config_center.output_dir("screenshots")' in SRC
          and "LOCAL_SCREENSHOT_ROOT" not in SRC)
    check("「打开目录」与抓取落地同源（根目录）",
          "ADBTool.open_folder(config_center.output_root())" in SRC)
    check("截图只产出一张：无水印直接落成品名（不再有 _tmp 成品）",
          "car_{ts}.png" in SRC and "_tmp.png" not in SRC)
    check("截图水印：中间原图 _raw + 成品 _ts",
          "car_{ts}_raw.png" in SRC and "car_{ts}_ts.png" in SRC)

    try:
        importlib.import_module("cartest.app.tools.log_tab")
        check("import log_tab 成功", True)
    except Exception as ex:  # noqa: BLE001
        check("import log_tab 成功", False, repr(ex))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("日志终端去开关检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
