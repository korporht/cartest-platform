# -*- coding: utf-8 -*-
"""P3d/UI-2b/UI-4 验收 — 侧栏分组（2026-10-12 终态：单组「通用工具」）

设计依据：12-P3d设计.md §4.1；18-工具箱与导航重构设计.md（侧栏 v4）；
2026-10-12 用户裁决：删除 VR 日志采集、取消「项目定制」分组。

用法:
    python tests/test_sidebar_split.py
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("验收 — 侧栏分组（复用性排序）")
    print("=" * 70)

    m = importlib.import_module("cartest.app.main")
    from cartest.app import pages as app_pages

    groups = {g: [k for k, _, _ in entries] for g, entries in m.PAGE_GROUPS}

    check("单组「通用工具」（2026-10-12 取消「项目定制」分组）",
          list(groups) == ["通用工具"], str(list(groups)))
    check("通用工具 7 项（2026-10-19 删除测试流程页）",
          groups["通用工具"] == ["tool_logcat", "tool_apptrans", "tool_screen",
                              "tool_monkey", "tool_logextract", "tool_offline",
                              "tool_inject"],
          str(groups["通用工具"]))
    check("dlogcat 仍未接入（保留待定）", "tool_dlogcat" not in m.PAGE_META)
    check("VR 日志已整页删除（2026-10-12 用户裁决）",
          "tool_vr" not in m.PAGE_META and "tool_vr" not in app_pages.BUILDERS
          and not (ROOT / "cartest" / "app" / "tools" / "vr_log_tab.py").exists(),
          f"meta={('tool_vr' in m.PAGE_META)} build={('tool_vr' in app_pages.BUILDERS)}")
    check("配置入口在 PAGE_META（不归组）", "settings" in m.PAGE_META)
    check("相册已彻底删除（2026-09-22 用户裁决）",
          "tool_gallery" not in m.PAGE_META and "tool_gallery" not in app_pages.BUILDERS
          and "gallery" not in (ROOT / "cartest" / "app" / "tools" / "pages.py")
          .read_text(encoding="utf-8").lower())

    check("侧栏渲染含组标题", "通用工具" in SRC)
    check("配置置顶（按钮块在分组循环之前）",
          SRC.find("SETTINGS_LABEL, active == SETTINGS_KEY") != -1
          and SRC.find("SETTINGS_LABEL, active == SETTINGS_KEY")
          < SRC.find("for group, entries in PAGE_GROUPS"))
    check("导航按钮组件接入（突出显示）", "components.nav_button" in SRC)
    check("侧栏不含最近运行列表（2026-09-16 用户反馈）", "最近运行" not in SRC)
    check("直切导航（无标签栏残留）", "_render_tabs" not in SRC and "打开页面" not in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("侧栏分组检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
