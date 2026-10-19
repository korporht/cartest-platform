# -*- coding: utf-8 -*-
"""BUGFIX 验收 — 工作线程控件更新必须回主线程（34 号：Flet ObjectPatch 竞态崩溃）

2026-09-23 崩溃：列包输出还在刷日志列表时点「查询版本」→ Flet 0.85
``ObjectPatch._compare_lists`` ``IndexError: list index out of range`` → 整应用
崩溃（工作线程 ``controls.append`` + ``update()`` 与点击事件后的自动 update
diff 并发竞争）。

修复口径：``ToolContext.on_main`` 把控件树变更调度回主线程
（``page.run_task`` = ``run_coroutine_threadsafe``，任何线程可安全投递）。
本测试钉死：

  [1] 主线程调用 on_main → 立即执行（inline），不排队
  [2] 工作线程调用 → 不立即执行，经 page.run_task 排队；驱动后执行
  [3] append_log_to_panel（工作线程）→ 面板控件列表延后变更，驱动后才入列
  [4] 源码护栏：context / log_tab / push_tab / apk_tab / monkey_tab 的
      工作线程 UI 更新都接了 on_main 通道

用法:
    python tests/test_ui_main_dispatch.py
"""
import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class _FakePage:
    """记录 run_task 投递的协程（不执行，由测试驱动）

    口径同 flet：``run_task(handler)`` 要求协程函数，内部 ``handler()`` 生成协程。
    """

    def __init__(self) -> None:
        self.tasks: list = []
        self.web = False

    def run_task(self, handler, *args, **kwargs):  # noqa: ANN001
        self.tasks.append(handler(*args, **kwargs))
        return None


class _StubState:
    def __init__(self) -> None:
        from cartest.app.theme import AppTheme
        self.theme = AppTheme()
        self.tool_state: dict = {}


def _drain(page: _FakePage) -> int:
    """驱动排队协程（等价主循环消化 run_task）"""
    n = 0
    while page.tasks:
        asyncio.run(page.tasks.pop(0))
        n += 1
    return n


def main() -> int:
    print("=" * 70)
    print("BUGFIX 验收 — UI 更新竞态（工作线程 → 主线程调度）")
    print("=" * 70)

    from cartest.core import config_center
    tmp = Path(mkdtemp(prefix="uimain_"))
    config_center.set_settings_file(tmp / "settings.json")

    from cartest.app.tools.context import ToolContext

    page = _FakePage()
    ctx = ToolContext(page, _StubState(), "logcat")
    ctx.build_log_panel()

    # ── [1] 主线程 → inline ──
    flag: dict = {"v": False}
    ctx.on_main(lambda: flag.__setitem__("v", True))
    check("[1] 主线程 on_main 立即执行（inline）", flag["v"] is True and not page.tasks)

    # ── [2] 工作线程 → 排队，驱动后执行 ──
    t = threading.Thread(
        target=lambda: ctx.on_main(lambda: flag.__setitem__("v", "worker")))
    t.start()
    t.join()
    check("[2] 工作线程 on_main 不立即执行，已排队",
          flag["v"] is True and len(page.tasks) == 1,
          f"tasks={len(page.tasks)}")
    _drain(page)
    check("[2] 驱动队列后执行", flag["v"] == "worker")

    # ── [3] append_log_to_panel（工作线程）→ 面板延后变更 ──
    lst = ctx._log_panel_list
    before = len(lst.controls)
    t = threading.Thread(target=lambda: ctx.append_log_to_panel("竞态护栏测试行"))
    t.start()
    t.join()
    check("[3] 工作线程追加日志不直接改控件列表（关键护栏）",
          len(lst.controls) == before, f"before={before} now={len(lst.controls)}")
    check("[3] 已排队待主线程应用", len(page.tasks) >= 1)
    _drain(page)
    check("[3] 驱动后行已入面板", len(lst.controls) == before + 1)

    # ── [4] 源码护栏：各文件工作线程 UI 更新接了 on_main ──
    src = (ROOT / "cartest/app/tools/context.py").read_text(encoding="utf-8")
    check("[4] context.py：_safe_on_main + ToolContext.on_main 存在",
          "def _safe_on_main" in src and "def on_main" in src)
    for rel in ("cartest/app/tools/log_tab.py",
                "cartest/app/tools/push_tab.py",
                "cartest/app/tools/apk_tab.py",
                "cartest/app/tools/monkey_tab.py"):
        s = (ROOT / rel).read_text(encoding="utf-8")
        check(f"[4] {rel} 接 on_main 通道", "on_main" in s)
    check("[4] log_tab 不再用 page.run_thread 执行控件更新",
          "page.run_thread(_update)" not in
          (ROOT / "cartest/app/tools/log_tab.py").read_text(encoding="utf-8"))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("UI 主线程调度护栏验收通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
