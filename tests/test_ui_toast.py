# -*- coding: utf-8 -*-
"""轻提示（Toast）验收 — 顶部浮层 / 自动隐藏 / 顶替 / 回退 / 线程调度

2026-10-12 用户裁决：配置页保存反馈由底部 ``SnackBar`` 改为**顶部浮层 Toast**，
并抽公共组件（``cartest/app/ui/toast.py``）全局统一。

覆盖：
  [1] 显示：可见 + 完全不透明 + 归位 + 文案 / 图标 / 配色
  [2] 自动隐藏：到时淡出并隐藏（定时器驱动）
  [3] 顶替：连续调用只保留最后一条（旧定时器不误关新提示）
  [4] 回退：未装配 host（单测 / 无壳）→ SnackBar，且沿用主题配色
  [5] 线程调度：工作线程调用经 ``page.run_task`` 回主线程（34 号竞态修复）
  [6] 装配与护栏：attach 挂 overlay + 写 page 属性；调用点统一无散落 SnackBar

用法:
    python tests/test_ui_toast.py
"""
import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flet as ft  # noqa: E402

from cartest.app.ui import toast as toast_mod  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeTheme:
    COLORS = {"success": "#00AA00", "danger": "#CC0000", "text_on_accent": "#FFFFFF"}

    def c(self, key: str, default: str = "#333333") -> str:
        return self.COLORS.get(key, default)


class FakePage:
    def __init__(self) -> None:
        self.overlay: list = []
        self.tasks: list = []          # 待办定时任务（协程函数）
        self.dialogs: list = []        # 回退路径（SnackBar）
        self.updates = 0

    def update(self) -> None:
        self.updates += 1

    def show_dialog(self, dialog) -> None:  # noqa: ANN001
        self.dialogs.append(dialog)

    def run_task(self, handler, *args, **kwargs) -> None:  # noqa: ANN001
        self.tasks.append(handler)


def run_pending(page: FakePage, idx: int = -1) -> None:
    """执行第 idx 个待办定时任务（把 ``asyncio.sleep`` 换成立即返回以跳过等待）"""
    handler = page.tasks[idx]
    orig = asyncio.sleep

    async def _instant(_seconds: float) -> None:
        return None

    asyncio.sleep = _instant
    try:
        asyncio.run(handler())
    finally:
        asyncio.sleep = orig


def main() -> int:
    print("=" * 70)
    print("轻提示（Toast）验收 — 顶部浮层 / 自动隐藏 / 顶替 / 回退 / 线程调度")
    print("=" * 70)

    print("\n[1] 显示：可见 + 完全不透明 + 归位 + 文案/图标/配色")
    page = FakePage()
    host = toast_mod.attach(page, FakeTheme())
    check("装配到 page.overlay（浮层不占布局；未传 host 回退 overlay）",
          len(page.overlay) == 1 and page.overlay[0] is host.control,
          f"overlay {len(page.overlay)} 项")
    # 2026-10-14 真机点验：flet 0.85 Web 端 overlay 子树 toggling visible 不重绘
    # → main.py 改传主树根 Stack.controls；此处补验 host 透传（独立 page 防串扰）
    page_b = FakePage()
    host2 = toast_mod.attach(page_b, FakeTheme(), host=page_b.overlay)
    check("装配支持主树 Stack host（host 透传）",
          len(page_b.overlay) == 1 and page_b.overlay[-1] is host2.control,
          f"overlay {len(page_b.overlay)} 项")
    check("写入 page 属性（供各页面取用）",
          getattr(page, "_cartest_toast", None) is host,
          str(getattr(page, "_cartest_toast", None)))
    check("初始不可见", host.control.visible is False)
    check("定位贴顶居中（top/left/right + TOP_CENTER）",
          host.control.top == toast_mod._TOP
          and host.control.left == 0 and host.control.right == 0
          and host.control.alignment == ft.Alignment.TOP_CENTER,
          f"top={host.control.top}")
    host.show(page, "已保存到方案「默认（内置）」", True)
    check("显示且完全不透明",
          host.control.visible is True and host._pill.opacity == 1.0,
          f"visible={host.control.visible}, opacity={host._pill.opacity}")
    check("归位（offset 回零）", host._pill.offset == toast_mod._OFFSET_SHOWN,
          str(host._pill.offset))
    check("文案正确", host._text.value == "已保存到方案「默认（内置）」", host._text.value)
    check("成功配色 + 对勾图标",
          host._pill.bgcolor == "#00AA00" and host._icon.name == ft.Icons.CHECK_CIRCLE_OUTLINE,
          f"{host._pill.bgcolor} / {host._icon.name}")
    check("排入自动隐藏定时器", len(page.tasks) == 1, f"{len(page.tasks)} 个")

    print("\n[2] 自动隐藏：到时淡出并隐藏")
    run_pending(page)
    check("隐藏后不可见", host.control.visible is False)
    check("隐藏后透明且上移",
          host._pill.opacity == 0.0 and host._pill.offset == toast_mod._OFFSET_HIDDEN,
          f"opacity={host._pill.opacity}, offset={host._pill.offset}")

    print("\n[3] 顶替：连续调用只保留最后一条")
    page = FakePage()
    host = toast_mod.attach(page, FakeTheme())
    host.show(page, "第一条", True)
    host.show(page, "第二条", False)
    check("两次调用排入两个定时器", len(page.tasks) == 2, f"{len(page.tasks)} 个")
    check("失败配色即时切换", host._pill.bgcolor == "#CC0000", str(host._pill.bgcolor))
    run_pending(page, 0)                       # 旧定时器先到点
    check("旧定时器不误关新提示",
          host.control.visible is True and host._text.value == "第二条",
          f"visible={host.control.visible}, text={host._text.value}")
    run_pending(page, 1)
    check("新定时器正常隐藏", host.control.visible is False)

    print("\n[4] 回退：未装配 host → SnackBar（沿用主题配色）")
    page = FakePage()
    toast_mod.toast(page, "保存失败：输入校验未通过", False, FakeTheme())
    check("回退为 SnackBar",
          len(page.dialogs) == 1 and isinstance(page.dialogs[0], ft.SnackBar),
          f"{len(page.dialogs)} 条")
    check("回退沿用主题失败色",
          bool(page.dialogs) and page.dialogs[0].bgcolor == "#CC0000",
          str(page.dialogs[0].bgcolor) if page.dialogs else "无")
    page2 = FakePage()
    toast_mod.toast(page2, "已保存", True, FakeTheme())
    check("回退成功色", bool(page2.dialogs) and page2.dialogs[0].bgcolor == "#00AA00",
          str(page2.dialogs[0].bgcolor) if page2.dialogs else "无")

    print("\n[5] 线程调度：工作线程调用回主线程执行")
    page = FakePage()
    host = toast_mod.attach(page, FakeTheme())
    before = len(page.tasks)
    worker = threading.Thread(target=lambda: host.show(page, "来自工作线程", True))
    worker.start()
    worker.join()
    check("经 page.run_task 投递（未在工作线程直接改控件）",
          len(page.tasks) == before + 1 and host.control.visible is False,
          f"tasks={len(page.tasks)}, visible={host.control.visible}")
    run_pending(page)
    check("投递的协程执行后正常显示",
          host.control.visible is True and host._text.value == "来自工作线程",
          f"visible={host.control.visible}, text={host._text.value}")

    print("\n[6] 装配与护栏")
    toast_src = (ROOT / "cartest" / "app" / "ui" / "toast.py").read_text(encoding="utf-8")
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
    check("toast.py 支持主树 Stack host 装配（overlay 为回退）",
          "host if host is not None else page.overlay" in toast_src)
    check("toast.py 有自动隐藏（run_task + asyncio.sleep）",
          "run_task" in toast_src and "asyncio.sleep" in toast_src)
    check("toast.py 有工作线程调度（_on_main）", "_on_main" in toast_src)
    check("main.py 启动时装配 Toast（挂主树根 Stack）",
          "toast_ui.attach(page, state.theme, host=root_stack.controls)" in main_src)

    bad: list[str] = []
    scan_dirs = [ROOT / "cartest" / "app" / "pages", ROOT / "cartest" / "app" / "tools"]
    for d in scan_dirs:
        for p in sorted(d.rglob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "SnackBar(" in line:
                    bad.append(f"{p.relative_to(ROOT)}:{i}")
    check("页面/工具无散落 SnackBar（统一走 toast()）", not bad, "; ".join(bad))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("轻提示（Toast）检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
