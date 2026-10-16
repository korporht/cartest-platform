# -*- coding: utf-8 -*-
"""40 号批 2 验收 — 任务跨页存活（离页不停）+ 顶栏任务指示器 + 任务抽屉

覆盖：
  [1] 源码口径：log_tab / single_inject 已无「离页停任务」（on_hide 移除），
      pages.py 离页改派 on_detach，base_tab 有 tick 订阅/退订
  [2] 行为级 LogTab：登记 handle → on_detach 后任务仍 running、stop_fn 未被调、
      tick 钩子已退订；进度回调只写 handle；tick 刷新同步控件值
  [3] 行为级 SingleInjectTab：登记 → on_detach 存活；IDLE 回调注销 handle
  [4] 顶栏聚合 _run_chip_value：运行中 / 排队中任务 + 流水线共聚一根胶囊
  [5] 任务抽屉 TaskPanel：行槽填充（运行/排队）、停止调 registry.stop

用法:
    python tests/test_leave_survive.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class _StubPage:
    """替身 page（tab/panel 会用 run_thread / run_task / overlay / update）"""

    web = False

    def __init__(self) -> None:
        self.overlay: list = []

    def run_thread(self, handler, *a, **kw) -> None:  # noqa: ANN001
        pass

    def run_task(self, handler, *a, **kw) -> None:  # noqa: ANN001
        pass

    def update(self) -> None:
        pass


def _make_state() -> object:
    """带 tasks/tick_hooks/device 的替身 state（不依赖 AppState 全量初始化）"""
    from cartest.app.tasks import TaskRegistry

    class _StubState:
        def __init__(self) -> None:
            from cartest.app.theme import AppTheme
            self.theme = AppTheme()
            self.tool_state: dict = {}
            self.before_page_leave = None
            self.tasks = TaskRegistry()
            self.tick_hooks: list = []
            self.device = ""
            self.activity = None

        def device_acquire(self, _token: str) -> None:
            pass

        def device_release(self, _token: str) -> None:
            pass

    return _StubState()


def main() -> int:
    print("=" * 70)
    print("40 号批 2 — 任务跨页存活 + 顶栏指示器 + 任务抽屉")
    print("=" * 70)

    # ── [1] 源码口径 ──
    log_src = (ROOT / "cartest" / "app" / "tools" / "log_tab.py").read_text(encoding="utf-8")
    inj_src = (ROOT / "cartest" / "app" / "tools" / "single_inject_tab.py").read_text(encoding="utf-8")
    pages_src = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
    base_src = (ROOT / "cartest" / "app" / "tools" / "base_tab.py").read_text(encoding="utf-8")
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")

    check("log_tab 已无 on_hide（离页停任务口径移除）", "def on_hide" not in log_src)
    check("log_tab 有 on_detach 且不停抓取",
          "def on_detach" in log_src
          and "self._logcat.stop()" not in log_src.split("def on_detach", 1)[1].split("def ", 2)[1])
    check("log_tab 登记全局任务（logcat）", '_task_register("logcat"' in log_src)
    check("log_tab 进度回调只写 handle（不碰控件）",
          "_task_update(" in log_src and "_logcat_progress.value" not in
          log_src.split("def _on_logcat_progress", 1)[1].split("def ", 2)[1])
    check("log_tab build 订阅 tick", "_tick_subscribe(self._tick_refresh)" in log_src)
    check("single_inject 已无 on_hide", "def on_hide" not in inj_src)
    check("single_inject 登记全局任务（inject）", '_task_register("inject"' in inj_src)
    check("single_inject IDLE 注销任务", '_task_unregister("inject"' in inj_src)
    check("pages 离页先派 on_detach",
          'getattr(tab, "on_detach"' in pages_src
          and 'hasattr(tab, "on_detach")' in pages_src)
    check("base_tab 有 tick 订阅/退订", "_tick_subscribe" in base_src and "def on_detach" in base_src)
    check("main 聚合任务到运行胶囊", "reg.snapshot()" in main_src and "task_panel.attach" in main_src)

    # ── [2] 行为级 LogTab ──
    ok2, err2 = True, ""
    try:
        from cartest.app.tools.context import ToolContext
        from cartest.app.tools.log_tab import LogTab

        st = _make_state()
        tab = LogTab(ToolContext(_StubPage(), st, "logcat"))
        tab.build()
        check("LogTab build 订阅了 tick 钩子",
              len(st.tick_hooks) == 1 and st.tick_hooks[0].__name__ == "_tick_refresh")

        stops: list[int] = []
        tab._task_register("logcat", "日志抓取", stop_fn=lambda: stops.append(1))
        tab.on_detach()
        h = st.tasks.get("logcat")
        check("on_detach 后任务仍 running", h is not None and h.running is True)
        check("on_detach 未调 stop_fn", stops == [])
        check("on_detach 已退订 tick 钩子", st.tick_hooks == [])

        tab._on_logcat_progress(0.5, 10, 20)
        h = st.tasks.get("logcat")
        check("进度回调写 handle（detail/data）",
              h is not None and "已运行 10" in h.detail and h.data.get("remaining") == 20)

        tab._logcat._running.set()             # 模拟服务运行中（tick 刷新前置条件）
        tab._tick_refresh()                    # 已退订 → 直接调（对等切回页面后的刷新）
        check("tick 刷新同步控件值（进度/计时）",
              tab._logcat_progress.value == 0.5 and "已运行 10" in tab._logcat_timer_label.value)

        tab._on_logcat_stopped("/tmp/x")
        check("停止回调注销任务", st.tasks.get("logcat") is None)
    except Exception as ex:  # noqa: BLE001
        ok2, err2 = False, f"{type(ex).__name__}: {ex}"
    check("LogTab 跨页存活行为", ok2, err2)

    # ── [3] 行为级 SingleInjectTab ──
    ok3, err3 = True, ""
    try:
        from cartest.core.injector import InjectState
        from cartest.app.tools.context import ToolContext
        from cartest.app.tools.single_inject_tab import SingleInjectTab

        st = _make_state()
        tab = SingleInjectTab(ToolContext(_StubPage(), st, "inject"))
        tab.build()
        stops3: list[int] = []
        tab._task_register("inject", "单条灌入", stop_fn=lambda: stops3.append(1))
        tab.on_detach()
        h = st.tasks.get("inject")
        check("on_detach 后灌入任务仍 running 且未停",
              h is not None and h.running is True and stops3 == [])

        tab._on_state(InjectState.IDLE)
        check("IDLE 回调注销任务", st.tasks.get("inject") is None)
    except Exception as ex:  # noqa: BLE001
        ok3, err3 = False, f"{type(ex).__name__}: {ex}"
    check("SingleInjectTab 跨页存活行为", ok3, err3)

    # ── [4] 顶栏聚合 ──
    ok4, err4 = True, ""
    try:
        import importlib
        from cartest.app.tasks import TaskHandle

        main_mod = importlib.import_module("cartest.app.main")  # 包属性被 main() 函数遮蔽

        st = _make_state()
        st.tasks.register(TaskHandle(key="logcat", label="日志抓取", running=True,
                                     detail="已运行 3s", kind="device"))
        st.tasks.register(TaskHandle(key="inject", label="单条灌入", running=False))
        st.tasks.update("inject", queued=True)
        st.running = False
        st.stage_status: dict = {}
        st.stage_detail: dict = {}
        chip = main_mod._run_chip_value(st)
        check("胶囊含运行中任务", "日志抓取" in chip and "已运行 3s" in chip, chip)
        check("胶囊含排队任务（⏸ 排队中 N 格式）",
              "⏸ 排队中 1（单条灌入）" in chip, chip)

        st2 = _make_state()
        st2.tasks.register(TaskHandle(key="logcat", label="日志抓取", running=True))
        st2.running = True
        st2.stage_status = {"prepare": "running"}
        st2.stage_detail = {"prepare": "准备中"}
        chip2 = main_mod._run_chip_value(st2)
        check("胶囊聚合任务与流水线", "日志抓取" in chip2 and "准备 准备中" in chip2, chip2)

        st3 = _make_state()
        st3.running = False
        st3.stage_status = {}
        st3.stage_detail = {}
        check("无任务时胶囊为空", main_mod._run_chip_value(st3) == "")
    except Exception as ex:  # noqa: BLE001
        ok4, err4 = False, f"{type(ex).__name__}: {ex}"
    check("顶栏运行胶囊聚合", ok4, err4)

    # ── [5] 任务抽屉 ──
    ok5, err5 = True, ""
    try:
        from cartest.app.tasks import TaskHandle
        from cartest.app.ui import task_panel as tp_mod

        st = _make_state()
        stops5: list[int] = []
        st.tasks.register(TaskHandle(key="logcat", label="日志抓取", running=True,
                                     detail="已运行 3s", kind="device", device="5ad9194b",
                                     stop_fn=lambda: stops5.append(1)))
        st.tasks.register(TaskHandle(key="inject", label="单条灌入", running=False))
        st.tasks.update("inject", queued=True)
        panel = tp_mod.TaskPanel(_StubPage(), st)
        panel._fill()
        check("行 1：运行中任务（名称/设备/详情）",
              panel._rows[0]["name"].value == "日志抓取"
              and "5ad9194b" in panel._rows[0]["detail"].value
              and panel._rows[0]["btn"].disabled is False)
        check("行 2：排队任务显示取消（按钮互斥切换）",
              panel._rows[1]["btn_cancel"].visible is True
              and panel._rows[1]["btn"].visible is False
              and "排队" in panel._rows[1]["detail"].value)
        check("其余行隐藏", all(panel._rows[i]["row"].visible is False
                                for i in range(2, tp_mod._MAX_ROWS)))

        panel._on_stop(0)
        check("停止按钮调 registry.stop → stop_fn", stops5 == [1])
    except Exception as ex:  # noqa: BLE001
        ok5, err5 = False, f"{type(ex).__name__}: {ex}"
    check("任务抽屉 TaskPanel", ok5, err5)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("任务跨页存活检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
