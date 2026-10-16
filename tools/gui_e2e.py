# -*- coding: utf-8 -*-
"""GUI 冒烟 E2E（试点）— Playwright 直驱 flet Web（替身）

用法（先起替身服务器，另开终端）：
    python tools/gui_demo.py --web --port 8582
    python tools/gui_e2e.py --port 8582

覆盖（UI-4 后的壳与合并页）：
  · 壳：侧栏条目（配置置顶 / 通用工具单组 8 项；2026-10-12 取消「项目定制」）
  · 应用与传输：子工具切换（APK 管理 → 大文件推送）
  · 屏幕与触控：子工具 chips
  · 日志终端：时间戳折叠区展开 + 真实转换
  · 测试流程：三视图切换（执行/结果/历史）+ 空态联动

说明：
  · Flet Web 需先激活语义树（``flt-semantics-placeholder``），脚本已内置；
  · CanvasKit 端偶发重绘滞后 → 断言带等待（默认 8s）；
  · 本脚本为**可选验收**，不纳入 ``check_all`` 硬门槛（见 18-工具箱与导航重构设计.md 讨论）。
"""
from __future__ import annotations

import argparse
import logging
import re
import sys

logging.getLogger("asyncio").setLevel(logging.CRITICAL)

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8582)
    args = ap.parse_args()
    url = f"http://localhost:{args.port}"

    print("=" * 70)
    print(f"GUI 冒烟 E2E（试点）— {url}")
    print("=" * 70)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_default_timeout(8000)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(url)
        page.wait_for_function("document.title.includes('CarTest')", timeout=30000)
        page.wait_for_timeout(2500)

        activated = page.evaluate(
            "() => { const ph = document.querySelector('flt-semantics-placeholder');"
            " if (ph) { ph.click(); return true; } return false; }")
        check("语义树激活", bool(activated))
        page.wait_for_timeout(1000)

        def see(pattern: str, timeout: int = 8000) -> bool:
            try:
                page.get_by_text(re.compile(pattern)).first.wait_for(
                    state="visible", timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                return False

        def see_box(label: str, timeout: int = 8000) -> bool:
            """textbox 的 accessible name（get_by_text 不覆盖输入框 aria-label）"""
            try:
                page.get_by_role("textbox", name=re.compile(label)).first.wait_for(
                    state="visible", timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                return False

        def click_btn(name: str, exact: bool = True) -> bool:
            try:
                page.get_by_role("button", name=name, exact=exact).first.click(timeout=8000)
                page.wait_for_timeout(600)
                return True
            except Exception:  # noqa: BLE001
                return False

        # ── [1] 壳 v4 ──
        check("侧栏：配置/日志终端", see("配置") and see("日志终端"))
        check("侧栏：应用与传输 / 屏幕与触控", see("应用与传输") and see("屏幕与触控"))
        check("侧栏：测试流程", see("测试流程"))
        check("侧栏：VR 日志已移除（2026-10-12）", not see("VR 日志"))

        # ── [2] 应用与传输（子切换）──
        check("进入应用与传输", click_btn("应用与传输", exact=False))
        check("APK 子工具内容", see("APK") and (see("浏览") or see("安装")))
        check("切换大文件推送", click_btn("大文件推送", exact=False))
        check("推送子工具内容（开始推送）", see("开始推送"))

        # ── [3] 屏幕与触控 ──
        check("进入屏幕与触控", click_btn("屏幕与触控", exact=False))
        check("子工具 chips（触控映射 / PC 录屏）", see("触控映射") and see("PC 录屏"))

        # ── [4] 日志终端 + 时间戳折叠 ──
        check("进入日志终端", click_btn("日志终端", exact=False))
        check("展开时间戳转换", click_btn("时间戳转换", exact=False))
        try:
            tb = page.get_by_role("textbox", name=re.compile("输入时间戳"))
            tb.fill("1700000000")
            check("时间戳输入", True)
        except Exception as ex:  # noqa: BLE001
            check("时间戳输入", False, repr(ex))
        check("执行转换", click_btn("🔄 转换", exact=False))
        check("转换结果（2023-11-15）", see("2023-11-15"))

        # ── [5] 测试流程三视图 ──
        check("进入测试流程", click_btn("测试流程", exact=False))
        check("执行视图（用例文件）", see_box("用例文件"))
        check("切到结果视图", click_btn("结果"))
        check("结果视图（导出 CSV / 空态）", see("导出 CSV") and see("去流水线跑一次"))
        check("空态联动 → 执行视图", click_btn("去流水线跑一次") and see_box("用例文件"))
        check("切到历史视图", click_btn("历史"))
        check("历史视图（清理 + chips）", see("清理") and see("keep"))

        check("无页面 JS 错误", not page_errors, "; ".join(page_errors[:2]))

        browser.close()

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("GUI 冒烟 E2E 通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
