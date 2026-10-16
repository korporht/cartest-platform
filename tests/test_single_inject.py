# -*- coding: utf-8 -*-
"""33 验收 — 单条灌入（快灌）：内核通路 + 页面

来源：``EOV_Voice_Tool`` 的"随手灌一句看结果"；内核复用 ``core.Injector``
（``30-功能分类与可配置分离.md`` §六 第 2 项）。

覆盖：
  [1] 服务层离线通路：替身 adb/judger/monitor → run() 一次 → on_done 收到判定
  [2] 结果字段：query / passed / domain / reply / 耗时（供页面卡片）
  [3] 忙时拒绝、空输入拒绝、stop() 不炸
  [4] 页面：构建不炸（flet kwargs 有效）+ 卡片生成 + 历史回填 + 状态行
  [5] 注册：BUILDERS/S 侧栏含 tool_inject；事件窗口（with_log）
  [6] 循环灌入（2026-10-15）：repeat=N 轮 + on_round + 间隔可打断 + repeat=0 不限 +
      页面次数透传 / _parse_repeat / 多次落卡

用法:
    python tests/test_single_inject.py
"""
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class _FakeAdb:
    """替身 adb：send_voice 成功（不碰真机）"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_voice(self, text, device_id="", mode=3, on_log=None):  # noqa: ANN001
        self.sent.append(text)
        if on_log:
            on_log(f"$ adb shell am startservice ... text={text}", "cmd")
        return True, "ok"


class _FakeJudger:
    """替身判定器：立即完成，返回固定判定"""

    instances: list = []

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._done = False
        _FakeJudger.instances.append(self)

    def set_timeout(self, t: float) -> None:
        self.timeout = t

    def start_collection(self, send_time: float = 0.0) -> None:
        pass

    def is_complete(self) -> bool:
        return True

    def judge(self, case):  # noqa: ANN001, ANN201
        from cartest.core.judger import Judgment
        return Judgment(passed=True, reason="关键词命中 1/1", intent_type="导航",
                        reply_text="好的，正在为您导航去公司", e2e_response_time=1.23)


class _FakeMonitor:
    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self.started = False

    def start(self, device_id: str = "") -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def should_health_check(self) -> bool:      # pump_logs 会问
        return False


class _StubCtx:
    """ToolContext 替身（页面只用到 theme/cfg/page/state/set_status/append_log_to_panel）"""

    def __init__(self) -> None:
        from cartest.app.theme import AppTheme
        from cartest.app.state import AppState

        class _Cfg:
            def __init__(self) -> None:
                self.d: dict = {}

            def get(self, k, default=None):  # noqa: ANN001
                return self.d.get(k, default)

            def set(self, k, v) -> None:  # noqa: ANN001
                self.d[k] = v

        self.theme = AppTheme()
        self.cfg = _Cfg()
        self.state = AppState()
        self.status: list[tuple] = []
        self.logs: list[str] = []
        self.done: list[tuple] = []          # notify_done 记录（循环收尾/单发提示）

        class _Page:
            web = False

            def update(self) -> None:
                pass

            def set_clipboard(self, text: str) -> None:
                pass

            def run_thread(self, h, *a, **kw) -> None:  # noqa: ANN001
                h(*a, **kw)

            def run_task(self, h, *a, **kw) -> None:  # noqa: ANN001
                pass

            def show_dialog(self, d) -> None:  # noqa: ANN001
                pass

        self.page = _Page()

    def set_status(self, text: str, color_key: str = "text_secondary") -> None:
        self.status.append((text, color_key))

    def append_log_to_panel(self, text: str) -> None:
        self.logs.append(str(text))

    def notify_done(self, text: str, ok: bool = True) -> None:
        self.done.append((str(text), ok))


def main() -> int:
    print("=" * 70)
    print("33 验收 — 单条灌入（快灌）")
    print("=" * 70)

    from cartest.app.tools.services.single_inject_service import SingleInjectService

    # ── [1] 服务层离线通路 ──
    adb = _FakeAdb()
    svc = SingleInjectService(adb=adb, judger_factory=_FakeJudger,
                              monitor_factory=_FakeMonitor)
    got: dict = {}
    states: list = []
    logs: list = []

    def _on_done(case, judgment) -> None:  # noqa: ANN001
        got["case"] = case
        got["judgment"] = judgment

    ok = svc.run("导航去公司", device="FAKE-DEV", timeout=5.0,
                 on_log=lambda m, t="info": logs.append((m, t)),
                 on_done=_on_done, on_state=states.append)
    check("run() 受理", ok is True)
    deadline = time.time() + 10
    while "judgment" not in got and time.time() < deadline:
        time.sleep(0.05)
    check("on_done 回调收到判定", "judgment" in got, str(got.keys()))
    check("adb.send_voice 收到 query", adb.sent == ["导航去公司"], str(adb.sent))
    # 等收尾（IDLE 在 finally 里回调；与 on_done 有竞态）
    deadline = time.time() + 8
    while svc.busy and time.time() < deadline:
        time.sleep(0.05)
    check("状态回调 RUNNING → IDLE", len(states) >= 2
          and str(states[0]).endswith("RUNNING") and str(states[-1]).endswith("IDLE"),
          str(states))
    check("日志回调有内容（命令/过程）", len(logs) >= 1, str(logs[:2]))
    check("结束后 busy=False", svc.busy is False)

    # ── [2] 结果字段（页面卡片所需）──
    j = got["judgment"]
    c = got["case"]
    check("case.query / status", c.query == "导航去公司" and c.status == "pass", c.status)
    check("judgment: passed/domain/reply/耗时",
          j.passed is True and j.intent_type == "导航"
          and j.reply_text.startswith("好的") and j.e2e_response_time == 1.23,
          f"{j.passed}/{j.intent_type}/{j.reply_text[:8]}/{j.e2e_response_time}")

    # ── [3] 边界：空输入 / 忙 / stop ──
    check("空输入拒绝", svc.run("   ") is False)
    adb2 = _FakeAdb()
    svc2 = SingleInjectService(adb=adb2, judger_factory=_FakeJudger,
                               monitor_factory=_FakeMonitor)
    started: list = []

    def _slow_done(case, judgment) -> None:  # noqa: ANN001
        time.sleep(0.6)
        started.append(1)

    svc2.run("第一条", on_done=_slow_done)
    time.sleep(0.05)
    check("灌入中再次 run 被拒", svc2.run("第二条") is False)
    svc2.stop()
    time.sleep(1.0)
    check("stop() 不抛异常", True)

    # ── [4] 页面 ──
    from cartest.app.tools.single_inject_tab import SingleInjectTab
    tmp = Path(mkdtemp(prefix="single_inject_"))
    import cartest.app.tools.single_inject_tab as sit
    sit._HISTORY_FILE = tmp / "hist.json"
    ctx = _StubCtx()
    ok4, err4 = True, ""
    try:
        tab = SingleInjectTab(ctx)
        tab._svc = SingleInjectService(adb=adb, judger_factory=_FakeJudger,
                                       monitor_factory=_FakeMonitor)
        ctl = tab.build()
        check("页面构建（flet kwargs 有效）", ctl is not None)
        tab._field.value = "打开空调"
        tab._run()
        deadline = time.time() + 8
        while not tab._records and time.time() < deadline:
            time.sleep(0.05)
        check("结果卡片生成", len(tab._records) == 1 and len(tab._result_list.controls) == 1,
              str(tab._records[:1]))
        rec = tab._records[0]
        check("卡片字段：query/passed/domain/耗时",
              rec["query"] == "打开空调" and rec["passed"] is True
              and rec["domain"] == "导航" and rec["cost"] >= 0, str(rec))
        check("结果卡片可构造（控件 kwargs）",
              tab._record_card(1, rec) is not None)
        check("状态行已更新（成功色）",
              any(c == "success" for _, c in ctx.status), str(ctx.status[-1:]))
        check("历史落盘 + 下拉回填",
              (tmp / "hist.json").is_file()
              and any(o.key == "打开空调" for o in tab._history_dd.options))
        tab._history_dd.value = "打开空调"
        tab._field.value = ""
        tab._pick_history()
        check("历史选中回填输入框", tab._field.value == "打开空调")
        check("save/restore_state 不丢记录",
              len(tab.save_state()["records"]) == 1
              and (tab.restore_state(tab.save_state()) or True))

        # 2026-09-24（全按钮点验）：停止路径按钮复位（on_state IDLE 兜底）
        from cartest.core.injector import InjectState as _IS
        tab._set_busy(True)
        check("busy 态按钮翻转（灌入禁用/停止可用）",
              tab._btn_run.disabled is True and tab._btn_stop.disabled is False)
        tab._on_state(_IS.IDLE)
        check("IDLE 状态回调复位按钮（停止中止不卡死）",
              tab._btn_run.disabled is False and tab._btn_stop.disabled is True)
        tab_src = (ROOT / "cartest" / "app" / "tools"
                   / "single_inject_tab.py").read_text(encoding="utf-8")
        check("run 接线 on_state（兜底复位）", "on_state=self._on_state" in tab_src)
    except Exception as ex:  # noqa: BLE001
        ok4, err4 = False, f"{type(ex).__name__}: {ex}"
    check("页面整体（构建/灌入/卡片/历史/状态）", ok4, err4)

    # ── [5] 注册与事件窗口 ──
    import importlib
    from cartest.app.tools import pages as tp
    main_mod = importlib.import_module("cartest.app.main")
    groups = {g: [k for k, _, _ in e] for g, e in main_mod.PAGE_GROUPS}
    check("BUILDERS 含 tool_inject", "tool_inject" in tp.BUILDERS)
    check("侧栏「通用工具」含 语音灌入（2026-10-15 改名；2026-10-12 并入通用工具）",
          "tool_inject" in groups.get("通用工具", []),
          str(groups))
    src = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
    seg = src.split("def build_single_inject", 1)[1].split("\n\n\n", 1)[0]
    check("单条灌入页无底部事件窗口（2026-10-12：只留日志终端页）",
          "with_log=False" in seg)
    from cartest.app import pages as app_pages
    check("注册表与 BUILDERS 一致",
          set(main_mod.PAGE_META) == set(app_pages.BUILDERS))

    # ── [6] 循环灌入（2026-10-15 用户诉求）：次数 / on_round / 间隔 / 不限 + stop ──
    adb3 = _FakeAdb()
    svc3 = SingleInjectService(adb=adb3, judger_factory=_FakeJudger,
                               monitor_factory=_FakeMonitor)
    rounds_seen: list[tuple] = []
    done3: list = []
    svc3.run("循环测试", device="FAKE-DEV", timeout=5.0, repeat=3, interval=0.05,
             on_done=lambda c, j: done3.append(c.query),
             on_round=lambda i, total: rounds_seen.append((i, total)))
    deadline = time.time() + 25
    while svc3.busy and time.time() < deadline:
        time.sleep(0.05)
    check("repeat=3 → 灌入 3 轮 / 3 条结果", len(done3) == 3, str(done3))
    check("on_round 逐轮回调 (idx,total)",
          [r[0] for r in rounds_seen] == [1, 2, 3]
          and all(r[1] == 3 for r in rounds_seen), str(rounds_seen))
    check("每轮独立 Injector（同句 3 次发送，轮间不串判）",
          adb3.sent == ["循环测试"] * 3, str(adb3.sent))
    check("轮次计数 rounds_done=3", svc3.rounds_done == 3, str(svc3.rounds_done))

    # repeat=0（不限）→ stop() 结束
    adb4 = _FakeAdb()
    svc4 = SingleInjectService(adb=adb4, judger_factory=_FakeJudger,
                               monitor_factory=_FakeMonitor)
    done4: list = []
    svc4.run("不限测试", repeat=0, interval=0.15,
             on_done=lambda c, j: done4.append(1))
    deadline = time.time() + 8
    while len(done4) < 2 and time.time() < deadline:
        time.sleep(0.05)
    svc4.stop()
    deadline = time.time() + 8
    while svc4.busy and time.time() < deadline:
        time.sleep(0.05)
    check("repeat=0 不限 → 多轮后 stop 结束（不卡死）",
          len(done4) >= 2 and svc4.busy is False, f"done={len(done4)}")

    # 轮间隔可被 stop 打断（不干等满 5s）
    adb5 = _FakeAdb()
    svc5 = SingleInjectService(adb=adb5, judger_factory=_FakeJudger,
                               monitor_factory=_FakeMonitor)
    done5: list = []
    svc5.run("间隔测试", repeat=5, interval=5.0,
             on_done=lambda c, j: done5.append(1))
    deadline = time.time() + 8
    while len(done5) < 1 and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)                              # 已进入第 1→2 轮的长间隔
    t0 = time.time()
    svc5.stop()
    deadline = time.time() + 8
    while svc5.busy and time.time() < deadline:
        time.sleep(0.05)
    check("轮间隔内 stop 立即结束（不等满 5s）",
          svc5.busy is False and (time.time() - t0) < 3.0 and len(done5) == 1,
          f"elapsed={time.time() - t0:.2f}s done={len(done5)}")

    # 页面：次数/间隔输入 + 透传 + 解析 + 轮次回调接线
    tab_src6 = (ROOT / "cartest" / "app" / "tools"
                / "single_inject_tab.py").read_text(encoding="utf-8")
    check("页面含「次数」「循环间隔(秒)」输入",
          "_repeat_field" in tab_src6 and "_interval_field" in tab_src6
          and "次数" in tab_src6 and "循环间隔(秒)" in tab_src6)
    check("_run 透传 repeat/interval",
          "repeat=repeat, interval=interval" in tab_src6)
    check("轮次回调接线 on_round",
          "on_round=lambda idx, total: self._on_round(q, idx, total)" in tab_src6
          and "def _on_round" in tab_src6)
    check("按钮翻转显式刷新（防视觉滞后 ~2s）",
          "_safe_update(self._btn_run)" in tab_src6
          and "_safe_update(self._btn_stop)" in tab_src6)
    tab3 = SingleInjectTab(ctx)
    tab3._repeat_field.value = ""
    check("_parse_repeat：留空 → 不限(0)", tab3._parse_repeat() == 0)
    tab3._repeat_field.value = "0"
    check("_parse_repeat：'0' → 不限(0)", tab3._parse_repeat() == 0)
    tab3._repeat_field.value = "3"
    check("_parse_repeat：'3' → 3", tab3._parse_repeat() == 3)
    tab3._repeat_field.value = "-2"
    check("_parse_repeat：负数 → 不限(0)", tab3._parse_repeat() == 0)
    tab3._repeat_field.value = "abc"
    check("_parse_repeat：非法 → None（已提示）", tab3._parse_repeat() is None)

    # 页面级：次数=2 → 2 张结果卡片
    ok6, err6 = True, ""
    try:
        ctx2 = _StubCtx()
        tab2 = SingleInjectTab(ctx2)
        tab2._svc = SingleInjectService(adb=_FakeAdb(), judger_factory=_FakeJudger,
                                        monitor_factory=_FakeMonitor)
        tab2.build()
        tab2._field.value = "循环卡片"
        tab2._repeat_field.value = "2"
        tab2._interval_field.value = "0"
        tab2._run()
        deadline = time.time() + 20
        while len(tab2._records) < 2 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)                          # 等 IDLE 收尾汇总
        check("页面 次数=2 → 2 张结果卡片",
              len(tab2._records) == 2 and len(tab2._result_list.controls) == 2,
              str(len(tab2._records)))
        check("灌入偏好落盘（repeat/interval）",
              ctx2.cfg.get("single_inject_repeat") == "2"
              and ctx2.cfg.get("single_inject_interval") == "0",
              f"{ctx2.cfg.d}")
        check("循环 2 轮 → 收尾汇总提示（非逐轮刷屏）",
              any("循环灌入完成 2 次" in tx for tx, _ in ctx2.done)
              and not any(tx == "灌入完成" for tx, _ in ctx2.done),
              str(ctx2.done))
        check("单发路径仍逐次提示（灌入完成）",
              any(tx == "灌入完成" and ok for tx, ok in ctx.done), str(ctx.done))
    except Exception as ex:  # noqa: BLE001
        ok6, err6 = False, f"{type(ex).__name__}: {ex}"
    check("页面循环灌入整体（多次/卡片/偏好/提示）", ok6, err6)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("单条灌入检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
