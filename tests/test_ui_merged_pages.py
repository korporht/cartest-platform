# -*- coding: utf-8 -*-
"""UI-4 验收 — 合并页（应用与传输；无 UI）

覆盖：合并页壳存在性（源码断言）/ 子工具选择纯函数 / 注册表与侧栏接线。

设计依据：18-工具箱与导航重构设计.md §四。

用法:
    python tests/test_ui_merged_pages.py
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("UI-4 验收 — 合并页（应用与传输；无 UI）")
    print("=" * 70)

    from cartest.app.tools import pages as tp
    from cartest.app.tools.pages import resolve_active

    # ── [1] 合并页壳（源码断言）──
    check("合并页壳 _wrap_merged 存在", "def _wrap_merged(" in SRC)
    check("chip 组件复用（子工具切换）", "components.chip" in SRC)
    check("应用与传输两子工具", "APK 管理" in SRC and "大文件推送" in SRC)
    check("旧 build_apk/build_push 已移除", "def build_apk(" not in SRC and "def build_push(" not in SRC)
    check("子工具选择记录 _active", '"_active"' in SRC)

    # ── [2] resolve_active 纯函数 ──
    check("resolve_active：默认首项", resolve_active(None, ["apk", "push"]) == "apk")
    check("resolve_active：记住上次", resolve_active({"_active": "push"}, ["apk", "push"]) == "push")
    check("resolve_active：非法值回退首项",
          resolve_active({"_active": "zzz"}, ["apk", "push"]) == "apk")

    # ── [3] 注册表与侧栏接线 ──
    check("BUILDERS 含 tool_apptrans", "tool_apptrans" in tp.BUILDERS)
    check("BUILDERS 含 tool_screen", "tool_screen" in tp.BUILDERS)
    check("屏幕与触控两子工具", "触控映射" in SRC and "PC 录屏" in SRC)
    check("日志终端为合并页两 tab（离线提取已迁独立页）",
          '("logcat", "日志终端"' in SRC and '("timestamp", "时间戳转换"' in SRC
          and '("offline", "离线提取"' not in SRC.split("def build_logcat", 1)[1]
          .split("def build_offline", 1)[0]
          and "ft.ExpansionTile" not in SRC.split("def build_logcat", 1)[1]
          .split("def build_screen", 1)[0])
    check("日志终端叠层子工具区已删除（旧 sub_holder / logcat_sub 键）",
          "sub_holder" not in SRC and '"logcat_sub"' not in SRC)
    check("日志终端 tab 切换态记录 logtools._active", '"logtools"' in SRC)
    check("离线提取为独立页（tool_offline 注册 + ReportView）",
          "tool_offline" in tp.BUILDERS and "def build_offline(" in SRC
          and "ReportView" in SRC)
    check("时间戳独立页已移除",
          "def build_timestamp(" not in SRC and "tool_timestamp" not in tp.BUILDERS)
    check("旧键已移除", "tool_apk" not in tp.BUILDERS and "tool_push" not in tp.BUILDERS)

    main_mod = importlib.import_module("cartest.app.main")
    from cartest.app import pages as app_pages

    groups = {g: [k for k, _, _ in entries] for g, entries in main_mod.PAGE_GROUPS}
    tools = groups.get("通用工具", [])
    check("侧栏含 tool_apptrans", "tool_apptrans" in tools)
    check("侧栏含 tool_screen", "tool_screen" in tools)
    check("侧栏无旧工具键（apk/push/touch/timestamp/recorder）",
          all(k not in tools for k in ("tool_apk", "tool_push", "tool_touch",
                                       "tool_timestamp", "tool_recorder")), str(tools))
    check("注册表与 BUILDERS 一致", set(main_mod.PAGE_META) == set(app_pages.BUILDERS))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("合并页检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
