# -*- coding: utf-8 -*-
"""动作完成反馈 — 强提示档 + 全局操作日志（notify_done）

2026-10-14：工具页耗时动作完成后给出**明显的完成反馈**——① 顶部**强提示**浮层
（更大、停留 4s）；② 记入**全局操作日志**（顶栏「操作日志」抽屉可回看）。
见 ``39-操作完成反馈与全局操作日志设计.md``。

覆盖：
  [1] 强提示档：strong=True 显示更大胶囊（成功绿）+ 排入 4000ms 定时器 + 到时隐藏
  [2] 强 / 轻两档互不顶替（各持 token）
  [3] notify_done：一次触发强提示 + 写 state.activity
  [4] 失败：红色 + 入库 ok=False
  [5] ActivityLog：new→old 顺序 + maxlen 自动裁剪
  [6] 兼容：snack(text, ok=False) 走轻档红色；BaseTab._done 转发
  [7] 静态护栏：main 装配抽屉 + 顶栏入口；各工具页已接线

用法:
    python tests/test_notify_done.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flet as ft  # noqa: E402

from cartest.app.activity import ActivityLog  # noqa: E402
from cartest.app.tools.base_tab import BaseTab  # noqa: E402
from cartest.app.tools.context import ToolContext  # noqa: E402
from cartest.app.ui import toast as toast_mod  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeTheme:
    COLORS = {"success": "#00AA00", "danger": "#CC0000", "text_on_accent": "#FFFFFF",
              "accent_text": "#8FB6FF", "text_primary": "#E5E9F0", "text_hint": "#5B6B85",
              "border": "#303A52", "bg_panel": "#232B3D", "accent": "#3B82F6"}

    def c(self, key: str, default: str = "#333333") -> str:
        return self.COLORS.get(key, default)


class FakeState:
    """ToolContext 所需的最小 state（theme + activity + oplog_models 兜底）"""

    def __init__(self, theme) -> None:  # noqa: ANN001
        self.theme = theme
        self.activity = ActivityLog()
        self.oplog_models: dict = {}


class FakePage:
    def __init__(self) -> None:
        self.overlay: list = []
        self.tasks: list = []
        self.dialogs: list = []
        self.updates = 0

    def update(self) -> None:
        self.updates += 1

    def show_dialog(self, dialog) -> None:  # noqa: ANN001
        self.dialogs.append(dialog)

    def run_task(self, handler, *args, **kwargs) -> None:  # noqa: ANN001
        self.tasks.append(handler)


def run_pending(page: FakePage, idx: int = -1) -> None:
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
    print("动作完成反馈 — 强提示档 + 全局操作日志（notify_done）")
    print("=" * 70)

    print("\n[1] 强提示档：更大胶囊 + 4000ms 定时器")
    page = FakePage()
    theme = FakeTheme()
    host = toast_mod.attach(page, theme)
    host.show(page, "抓日志完成", True, strong=True)
    check("强档可见且不透明",
          host._strong.pill.visible is True and host._strong.pill.opacity == 1.0,
          f"visible={host._strong.pill.visible}")
    check("外层浮层随之可见（任一模档可见）", host.control.visible is True)
    check("成功配色 + 对勾", host._strong.pill.bgcolor == "#00AA00"
          and host._strong.icon.name == ft.Icons.CHECK_CIRCLE_OUTLINE)
    check("字号大于轻提示（强=15 / 轻=12）",
          host._strong.text.size == toast_mod._STRONG_STYLE["size"]
          and host._strong.text.size > host._light.text.size,
          f"{host._strong.text.size} vs {host._light.text.size}")
    check("只排入一个定时器", len(page.tasks) == 1, f"{len(page.tasks)} 个")
    run_pending(page)
    check("到时隐藏（强档 + 外层）",
          host._strong.pill.visible is False and host.control.visible is False)

    print("\n[2] 强 / 轻两档互不顶替（各持 token）")
    page = FakePage()
    host = toast_mod.attach(page, FakeTheme())
    host.show(page, "已复制", True)                 # 轻档 → task0
    host.show(page, "抓日志完成", True, strong=True)  # 强档 → task1
    check("两档同时可见",
          host._light.pill.visible is True and host._strong.pill.visible is True)
    check("各排入一个定时器", len(page.tasks) == 2, f"{len(page.tasks)} 个")
    run_pending(page, 0)                            # 轻档定时器先到点
    check("轻档到点隐藏不影响强档",
          host._light.pill.visible is False and host._strong.pill.visible is True
          and host.control.visible is True)
    run_pending(page, 1)
    check("强档到点后整块隐藏",
          host._strong.pill.visible is False and host.control.visible is False)

    print("\n[3] notify_done：强提示 + 写全局操作日志")
    page = FakePage()
    state = FakeState(FakeTheme())
    host = toast_mod.attach(page, state.theme)
    ctx = ToolContext(page, state, "logcat")
    ctx.notify_done("抓日志完成")
    check("弹强提示（成功绿）",
          host._strong.pill.visible is True and host._strong.pill.bgcolor == "#00AA00")
    check("记入全局操作日志", len(state.activity) == 1, f"{len(state.activity)} 条")
    item = state.activity.items()[0]
    check("记录字段（来源/动作/成败）",
          item.action == "抓日志完成" and item.ok is True and bool(item.source),
          f"{item.source} / {item.action} / ok={item.ok}")
    check("时间戳非空", bool(item.ts), item.ts)

    print("\n[4] 失败：红色 + 入库 ok=False")
    ctx.notify_done("抓日志失败", ok=False)
    check("失败红色", host._strong.pill.bgcolor == "#CC0000")
    check("失败图标", host._strong.icon.name == ft.Icons.ERROR_OUTLINE)
    check("入库 ok=False 且为最新",
          len(state.activity) == 2 and state.activity.items()[0].ok is False,
          f"{len(state.activity)} 条")

    print("\n[5] ActivityLog：new→old 顺序 + maxlen 裁剪")
    log = ActivityLog(maxlen=3)
    for i in range(5):
        log.add("工具", f"动作{i}", ok=(i % 2 == 0))
    its = log.items()
    check("超限裁剪至 maxlen", len(log) == 3, f"{len(log)} 条")
    check("新 → 旧 排列",
          [x.action for x in its] == ["动作4", "动作3", "动作2"],
          str([x.action for x in its]))
    log.clear()
    check("清空", len(log) == 0)

    print("\n[6] 兼容：snack(ok=False) 走轻档；BaseTab._done 转发")
    page = FakePage()
    state = FakeState(FakeTheme())
    host = toast_mod.attach(page, state.theme)
    ctx = ToolContext(page, state, "touch")
    ctx.snack("已复制", ok=False)
    check("snack 默认走轻档（不占强档）",
          host._light.pill.visible is True and host._light.pill.bgcolor == "#CC0000"
          and host._strong.pill.visible is False)

    class _Tab(BaseTab):
        def build(self):
            return ft.Text("x")

    tab = _Tab(ctx)
    n_before = len(state.activity)
    tab._done("截图完成")
    check("BaseTab._done 经 notify_done 写库", len(state.activity) == n_before + 1)
    check("_done 同时弹强提示", host._strong.pill.visible is True)

    print("\n[7] 静态护栏：装配 / 顶栏入口 / 各页接线")
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
    toast_src = (ROOT / "cartest" / "app" / "ui" / "toast.py").read_text(encoding="utf-8")
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")
    check("main 装配操作日志抽屉（挂主树根 Stack）",
          "activity_panel.attach(page, state, host=root_stack.controls)" in main_src)
    check("顶栏有操作日志入口（HISTORY）", "ft.Icons.HISTORY" in main_src)
    check("toast.py 有强提示档常量", "_STRONG_DURATION_MS" in toast_src)
    check("state 挂 activity", "self.activity" in state_src)

    tools_dir = ROOT / "cartest" / "app" / "tools"
    expect = ["log_tab.py", "apk_tab.py", "push_tab.py", "logextract_tab.py",
              "recorder_tab.py", "monkey_tab.py", "touch_tab.py",
              "single_inject_tab.py", "widgets/offline_extract.py"]
    missing: list[str] = []
    for rel in expect:
        src = (tools_dir / rel).read_text(encoding="utf-8")
        if "_done(" not in src and "notify_done(" not in src:
            missing.append(rel)
    check("各工具页均已接线完成反馈", not missing, "; ".join(missing))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("动作完成反馈检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
