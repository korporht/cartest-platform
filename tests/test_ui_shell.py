# -*- coding: utf-8 -*-
"""UI-1/UI-2b 验收 — 工作台壳（无 UI）

覆盖：组件样式（导航按钮突出显示 + 内边距）/ 页面注册表（直切模式）/ 状态辅助。

设计依据：14-UI重构设计.md §5（含 2026-09-16 用户反馈修订：
删除标签栏、侧栏按复用性重排、按钮加大内边距）。

用法:
    python tests/test_ui_shell.py
"""
import json
import os
import sys
from pathlib import Path

import flet as ft

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("UI-1/UI-2b 验收 — 工作台壳（无 UI）")
    print("=" * 70)

    # ── [1] 壳组件样式（重点：导航按钮突出显示 + 内边距）──
    from cartest.app.theme import AppTheme
    from cartest.app.ui import components as ui_c

    t = AppTheme()
    selected = ui_c.nav_button(t, None, "流水线", True, lambda e: None)
    check("选中导航：实心主色底", selected.bgcolor == t.c("accent"), str(selected.bgcolor))
    inner_row = selected.content
    txt = inner_row.controls[1]
    check("选中导航：白字 + 加粗",
          txt.color == t.c("text_on_accent") and str(txt.weight).endswith("BOLD"),
          f"{txt.color} / {txt.weight}")
    check("导航按钮内边距加大（用户反馈：高 38 / 左 12）",
          selected.height == 38 and selected.padding.left == 12,
          f"h={selected.height} pad={selected.padding.left}")
    normal = ui_c.nav_button(t, None, "结果", False, lambda e: None)
    check("未选中导航：无实心底色", normal.bgcolor is None)
    check("未选中导航：次级文本色", normal.content.controls[1].color == t.c("text_secondary"))

    chip = ui_c.chip(t, "● 设备 c0fdae2f")
    check("胶囊：圆角边框渲染", chip.border_radius == 999)
    chip_active = ui_c.chip(t, "全部 12", on_click=lambda e: None, active=True)
    check("chip 选中态：sel_bg 底 + accent 文本加粗",
          chip_active.bgcolor == t.c("sel_bg")
          and chip_active.content.color == t.c("accent_text")
          and str(chip_active.content.weight).endswith("BOLD"),
          f"{chip_active.bgcolor} / {chip_active.content.color}")

    # ── [2] 页面注册表（侧栏直切模式）──
    import importlib
    main_mod = importlib.import_module("cartest.app.main")
    from cartest.app import pages as app_pages

    SRC = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")

    meta_keys = set(main_mod.PAGE_META)
    check("注册表与 BUILDERS 一致", meta_keys == set(app_pages.BUILDERS),
          f"仅注册表={sorted(meta_keys - set(app_pages.BUILDERS))} "
          f"仅BUILDERS={sorted(set(app_pages.BUILDERS) - meta_keys)}")
    check("相册已彻底删除（2026-09-22 用户裁决）",
          "tool_gallery" not in meta_keys and "tool_gallery" not in app_pages.BUILDERS)
    check("测试流程页已整页删除（2026-10-19 用户裁决）",
          "testflow" not in app_pages.BUILDERS
          and not ({"pipeline", "results", "history"} & set(app_pages.BUILDERS))
          and not (ROOT / "cartest" / "app" / "pages" / "testflow.py").exists())

    groups = {g: [k for k, _, _ in entries] for g, entries in main_mod.PAGE_GROUPS}
    check("单组「通用工具」（2026-10-12 取消「项目定制」分组）",
          list(groups) == ["通用工具"], str(list(groups)))
    check("通用工具 7 项（2026-10-19 删除测试流程）",
          groups["通用工具"] == ["tool_logcat", "tool_apptrans", "tool_screen",
                              "tool_monkey", "tool_logextract", "tool_offline",
                              "tool_inject"],
          str(groups["通用工具"]))
    check("dlogcat 仍待接入 / VR 日志已整页删除（2026-10-12）",
          "tool_dlogcat" not in meta_keys and "tool_vr" not in meta_keys)
    check("对话还原在 PAGE_META / 测试流程已移除",
          "tool_offline" in main_mod.PAGE_META
          and "testflow" not in main_mod.PAGE_META)
    check("配置入口在 PAGE_META（不归组）", "settings" in main_mod.PAGE_META)

    check("顶部标签栏已移除（无残留）",
          "_render_tabs" not in SRC and "PopupMenuButton" not in SRC and "tabs_holder" not in SRC)
    check("无底部状态栏（用户反馈）", "status_bar" not in SRC and "_status_text" not in SRC)

    # ── [3] state 壳辅助（recent_runs / disk_summary / active_page）──
    from cartest.pipeline.context import RunContext
    from cartest.app.state import AppState

    tmp = mkdtemp(prefix="ui1_runs_")
    os.environ["CARTEST_RUNS_DIR"] = tmp
    try:
        ctx1 = RunContext.create("cases_demo.json", keep=True)
        ctx1.results_json.write_text(json.dumps({
            "run_id": ctx1.run_id, "total": 4, "pass": 3, "fail": 1,
            "conflicts": 2, "results": []}, ensure_ascii=False), encoding="utf-8")
        RunContext.create("cases_demo.json")   # 未跑完 → 中断

        state = AppState()
        check("active_page 初始为日志终端（tool_logcat）", state.active_page == "tool_logcat")
        check("测试流程遗留属性已清理（active_view/goto_view/nav_view）",
              not hasattr(state, "active_view") and not hasattr(state, "goto_view")
              and not hasattr(state, "nav_view"))
        runs = state.recent_runs()
        check("recent_runs 返回 2 条", len(runs) == 2, str(len(runs)))
        by_id = {r["run_id"]: r for r in runs}
        done = by_id[ctx1.run_id]
        check("完成运行：状态含计数", "✓3" in done["status_text"] and "✗1" in done["status_text"],
              done["status_text"])
        check("完成运行：有颜色键", done["status_key"] in ("success", "danger"))
        other = next(r for r in runs if r["run_id"] != ctx1.run_id)
        check("中断运行：状态=中断/warning",
              other["status_text"] == "中断" and other["status_key"] == "warning",
              f'{other["status_text"]}/{other["status_key"]}')
        check("名称已去日期前缀", done["name"] == "cases_demo", done["name"])

        check("底部状态栏已删除（无 status_target/disk_summary）",
              not hasattr(state, "status_target") and not hasattr(state, "disk_summary"))

        state.select_run(ctx1.run_id)
        check("select_run 记录选中运行", state.selected_run_id == ctx1.run_id)
        check("state 含 tick_hooks 且初始为空", isinstance(state.tick_hooks, list)
              and state.tick_hooks == [])
    finally:
        os.environ.pop("CARTEST_RUNS_DIR", None)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("工作台壳检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
