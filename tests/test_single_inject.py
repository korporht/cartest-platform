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
  [7] 失败即停（2026-10-19）：fail_fast 下首败即停（停因 fail_fast）；关闭时失败照常
      继续（行为不回归）；pass 不误停；手动停止原因优先；页面勾选透传 + 收尾提示
  [8] 顺序执行（2026-10-19）：queries 逐条灌入（轮数=用例数、忽略 repeat）+ on_round +
      间隔可停 + fail_fast 第 N 条即停 + 空 queries 拒绝；页面导入用例/列表/顺序执行

用法:
    python tests/test_single_inject.py
"""
import asyncio
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
    """替身 adb：send_voice 成功（不碰真机）；记录 mode 用于语义模式接线断言"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.modes: list[int] = []

    def send_voice(self, text, device_id="", mode=3, on_log=None):  # noqa: ANN001
        self.sent.append(text)
        self.modes.append(mode)
        if on_log:
            on_log(f"$ adb shell am startservice ... text={text}&mode={mode}", "cmd")
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


class _SeqJudger:
    """替身判定器：按调用序返回给定的 passed 序列（消费完保持最后一个）

    ``_SeqJudger([False])`` ＝ 全失败；``_SeqJudger([True, True, False])`` ＝
    前两次通过、第三次起失败——用于验证"第 N 次失败才触发即停"。
    **序列按引用保存**：循环灌入每轮新建 Injector（架构本意），跨轮共享同一序列
    需由工厂闭包传同一个 list（见 [7c]），否则每轮 judger 都会重置序列。
    """

    instances: list = []

    def __init__(self, results, timeout: float = 20.0) -> None:
        self._results = results
        self.timeout = timeout
        self._done = False
        _SeqJudger.instances.append(self)

    def set_timeout(self, t: float) -> None:
        self.timeout = t

    def start_collection(self, send_time: float = 0.0) -> None:
        pass

    def is_complete(self) -> bool:
        return True

    def judge(self, case):  # noqa: ANN001, ANN201
        from cartest.core.judger import Judgment
        passed = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if passed:
            return Judgment(passed=True, reason="关键词命中 1/1", intent_type="导航",
                            reply_text="好的，正在为您导航去公司", e2e_response_time=1.23)
        return Judgment(passed=False, reason="未检测到 TTS 回复", intent_type="",
                        reply_text="")


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
    check("页面含「次数」「间隔(秒)」输入（2026-10-19 改名，循环/顺序共用）",
          "_repeat_field" in tab_src6 and "_interval_field" in tab_src6
          and "次数" in tab_src6 and "间隔(秒)" in tab_src6
          and "循环间隔(秒)" not in tab_src6)
    check("_run 透传 repeat/interval",
          "repeat=repeat, interval=interval" in tab_src6)
    check("轮次回调接线 on_round（支持顺序执行 queries）",
          "on_round=lambda idx, total: self._on_round(q, idx, total, queries)"
          in tab_src6 and "def _on_round" in tab_src6)
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

    # ── [7] 失败即停（2026-10-19 用户诉求）──
    # [7a] 服务层：全失败判定 + fail_fast + repeat=5 → 首败即停（只灌 1 轮）
    # 注意：judger_factory 签名是 (timeout) -> Judger——传类会炸（_SeqJudger(5.0)）
    adb7 = _FakeAdb()
    svc7 = SingleInjectService(adb=adb7,
                               judger_factory=lambda t: _SeqJudger([False]),
                               monitor_factory=_FakeMonitor)
    done7: list = []
    svc7.run("失败即停", device="FAKE-DEV", timeout=5.0, repeat=5, interval=0.05,
             fail_fast=True, on_done=lambda c, j: done7.append(c.query))
    deadline = time.time() + 15
    while svc7.busy and time.time() < deadline:
        time.sleep(0.05)
    check("fail_fast + 首轮失败 → 只灌 1 轮即停",
          len(done7) == 1 and adb7.sent == ["失败即停"], f"done={len(done7)}")
    check("停止原因透出 fail_fast", svc7.stop_reason == "fail_fast",
          svc7.stop_reason)

    # [7b] 现状行为可关闭：显式 fail_fast=False → 失败也灌满 repeat（底层能力）
    adb8 = _FakeAdb()
    svc8 = SingleInjectService(adb=adb8,
                               judger_factory=lambda t: _SeqJudger([False]),
                               monitor_factory=_FakeMonitor)
    done8: list = []
    svc8.run("失败继续", device="FAKE-DEV", timeout=5.0, repeat=3, interval=0.05,
             fail_fast=False, on_done=lambda c, j: done8.append(c.query))
    deadline = time.time() + 15
    while svc8.busy and time.time() < deadline:
        time.sleep(0.05)
    check("fail_fast 关闭 → 失败照常灌满 3 轮（底层可关闭）",
          len(done8) == 3 and adb8.sent == ["失败继续"] * 3, f"done={len(done8)}")
    check("灌满轮次 → 停止原因为空", svc8.stop_reason == "", svc8.stop_reason)

    # [7c] 第 N 次失败才停：前 2 次通过、第 3 次失败 + repeat=5
    # 每轮新建 Injector（架构本意）→ 工厂闭包共享同一序列，跨轮不重置
    adb9 = _FakeAdb()
    _seq9 = [True, True, False]
    svc9 = SingleInjectService(adb=adb9,
                               judger_factory=lambda t: _SeqJudger(_seq9),
                               monitor_factory=_FakeMonitor)
    done9: list = []
    svc9.run("第3次失败", device="FAKE-DEV", timeout=5.0, repeat=5, interval=0.05,
             fail_fast=True, on_done=lambda c, j: done9.append(c.query))
    deadline = time.time() + 15
    while svc9.busy and time.time() < deadline:
        time.sleep(0.05)
    check("前 2 次通过 → 第 3 次失败才触发即停（不误停前两轮）",
          len(done9) == 3 and adb9.sent == ["第3次失败"] * 3, f"done={len(done9)}")
    check("停止原因透出 fail_fast（第 N 次）", svc9.stop_reason == "fail_fast",
          svc9.stop_reason)

    # [7d] pass 不误停：fail_fast 开启 + 全通过 + repeat=3 → 灌满
    adb10 = _FakeAdb()
    svc10 = SingleInjectService(adb=adb10, judger_factory=_FakeJudger,
                                monitor_factory=_FakeMonitor)
    done10: list = []
    svc10.run("全通过", device="FAKE-DEV", timeout=5.0, repeat=3, interval=0.05,
              fail_fast=True, on_done=lambda c, j: done10.append(c.query))
    deadline = time.time() + 15
    while svc10.busy and time.time() < deadline:
        time.sleep(0.05)
    check("fail_fast 开启 + 全通过 → 灌满 3 轮（pass 不误停）",
          len(done10) == 3 and svc10.stop_reason == "", f"done={len(done10)}")

    # [7e] 手动停止优先于 fail_fast 记因（用户先点停止，不被篡改为失败即停）
    adb11 = _FakeAdb()
    svc11 = SingleInjectService(adb=adb11, judger_factory=_FakeJudger,
                                monitor_factory=_FakeMonitor)
    done11: list = []
    svc11.run("手动优先", repeat=0, interval=0.15, fail_fast=True,
              on_done=lambda c, j: done11.append(1))
    deadline = time.time() + 8
    while len(done11) < 2 and time.time() < deadline:
        time.sleep(0.05)
    svc11.stop()
    deadline = time.time() + 8
    while svc11.busy and time.time() < deadline:
        time.sleep(0.05)
    check("手动停止 → 原因记 manual（不被 fail_fast 覆盖）",
          svc11.stop_reason == "manual" and svc11.busy is False,
          svc11.stop_reason)

    # [7f] 页面：失败即停为**默认**（2026-10-19 用户裁决：不做复选框）
    ok7, err7 = True, ""
    try:
        ctx3 = _StubCtx()
        tab4 = SingleInjectTab(ctx3)
        tab4._svc = SingleInjectService(adb=_FakeAdb(), judger_factory=_SeqJudger,
                                        monitor_factory=_FakeMonitor)
        tab4.build()
        tab4._field.value = "失败即停页面"
        tab4._repeat_field.value = "3"
        tab4._interval_field.value = "0"
        tab4._run()
        deadline = time.time() + 15
        while not tab4._records and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)                       # 等 IDLE 收尾
        check("页面默认失败即停 → 只落 1 张卡片（首败即停，无开关）",
              len(tab4._records) == 1 and len(tab4._result_list.controls) == 1,
              str(len(tab4._records)))
        check("无「失败即停」复选框（默认行为）",
              not hasattr(tab4, "_failfast_cb"))
        check("语义模式偏好落盘（single_inject_mode）",
              ctx3.cfg.get("single_inject_mode") == "3", str(ctx3.cfg.d))
        check("收尾提示「失败即停」而非常规汇总",
              any("失败即停" in tx for tx, _ in ctx3.done)
              and not any("循环灌入完成" in tx for tx, _ in ctx3.done),
              str(ctx3.done))
        check("失败即停提示为失败色（ok=False）",
              any("失败即停" in tx and ok is False for tx, ok in ctx3.done),
              str(ctx3.done))
    except Exception as ex:  # noqa: BLE001
        ok7, err7 = False, f"{type(ex).__name__}: {ex}"
    check("页面失败即停整体（默认生效/落盘/卡片/提示）", ok7, err7)

    # 源码接线断言（防回归被改掉）
    tab_src7 = (ROOT / "cartest" / "app" / "tools"
                / "single_inject_tab.py").read_text(encoding="utf-8")
    check("失败即停为默认（页面无复选框、执行传 fail_fast=True）",
          "_failfast_cb" not in tab_src7 and "fail_fast=True" in tab_src7
          and "single_inject_fail_fast" not in tab_src7)
    svc_src = (ROOT / "cartest" / "app" / "tools" / "services"
               / "single_inject_service.py").read_text(encoding="utf-8")
    check("服务层 fail_fast 默认开启（run 参数 + _track_done + stop_reason）",
          "fail_fast: bool = True" in svc_src and "def _track_done" in svc_src
          and "def stop_reason" in svc_src.replace("@property\n", ""))

    # ── [8] 顺序执行（queries，2026-10-19 用户诉求）──
    # [8a] 逐条灌入：queries 3 条 → 3 轮（忽略 repeat），顺序与内容一致
    adb12 = _FakeAdb()
    svc12 = SingleInjectService(adb=adb12, judger_factory=_FakeJudger,
                                monitor_factory=_FakeMonitor)
    seen12: list[tuple] = []
    done12: list[str] = []
    ok8 = svc12.run("", queries=["打开空调", "导航去公司", "播放音乐"],
                    device="FAKE-DEV", timeout=5.0, repeat=1, interval=0.05,
                    on_round=lambda i, n: seen12.append((i, n)),
                    on_done=lambda c, j: done12.append(c.query))
    check("顺序执行受理（query 为空 + queries 非空）", ok8 is True)
    deadline = time.time() + 20
    while svc12.busy and time.time() < deadline:
        time.sleep(0.05)
    check("queries 逐条灌入（顺序与内容一致、忽略 repeat=1）",
          adb12.sent == ["打开空调", "导航去公司", "播放音乐"], str(adb12.sent))
    check("语义模式默认 3（透传到 send_voice）",
          adb12.modes == [3, 3, 3], str(adb12.modes))
    check("on_round 总轮次=用例数", seen12 == [(1, 3), (2, 3), (3, 3)], str(seen12))
    check("顺序执行 rounds_done=3", svc12.rounds_done == 3, str(svc12.rounds_done))
    check("顺序执行 on_done 逐条回调",
          done12 == ["打开空调", "导航去公司", "播放音乐"], str(done12))

    # [8b] fail_fast：第 2 条失败 → 不跑第 3 条
    adb13 = _FakeAdb()
    _seq13 = [True, False, True]
    svc13 = SingleInjectService(adb=adb13,
                                judger_factory=lambda t: _SeqJudger(_seq13),
                                monitor_factory=_FakeMonitor)
    svc13.run("", queries=["第一条", "第二条", "第三条"], timeout=5.0,
              interval=0.05, fail_fast=True)
    deadline = time.time() + 20
    while svc13.busy and time.time() < deadline:
        time.sleep(0.05)
    check("顺序执行 + 失败即停：第 2 条失败即停（不跑第 3 条）",
          adb13.sent == ["第一条", "第二条"] and svc13.stop_reason == "fail_fast",
          f"sent={adb13.sent} reason={svc13.stop_reason}")

    # [8c] 停止：第 1 条后进入长间隔，stop 立即结束（不跑后续用例）
    adb14 = _FakeAdb()
    svc14 = SingleInjectService(adb=adb14, judger_factory=_FakeJudger,
                                monitor_factory=_FakeMonitor)
    done14: list[int] = []
    svc14.run("", queries=["甲", "乙", "丙"], timeout=5.0, interval=5.0,
              on_done=lambda c, j: done14.append(1))
    deadline = time.time() + 8
    while len(done14) < 1 and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)                        # 已进入第 1→2 条的长间隔
    t0 = time.time()
    svc14.stop()
    deadline = time.time() + 8
    while svc14.busy and time.time() < deadline:
        time.sleep(0.05)
    check("顺序执行可停止（间隔内立即结束，不跑后续用例）",
          svc14.busy is False and len(done14) == 1 and (time.time() - t0) < 3.0,
          f"done={len(done14)} elapsed={time.time() - t0:.2f}s")
    check("顺序执行停止原因为 manual", svc14.stop_reason == "manual", svc14.stop_reason)

    # [8d] 空输入拒绝（queries 全空白视同空）
    svc15 = SingleInjectService(adb=_FakeAdb(), judger_factory=_FakeJudger,
                                monitor_factory=_FakeMonitor)
    check("空 query + 空 queries → 拒绝", svc15.run("") is False)
    check("空 query + 全空白 queries → 拒绝",
          svc15.run("", queries=[" ", ""]) is False)

    # [8e] 语义模式接线：voice_mode 透传到 send_voice（不再是假控件）
    adb_mode = _FakeAdb()
    svc_mode = SingleInjectService(adb=adb_mode, judger_factory=_FakeJudger,
                                   monitor_factory=_FakeMonitor)
    svc_mode.run("模式测试", timeout=5.0, voice_mode=1)
    deadline = time.time() + 10
    while svc_mode.busy and time.time() < deadline:
        time.sleep(0.05)
    check("voice_mode=1 → send_voice 收到 mode=1",
          adb_mode.modes == [1], str(adb_mode.modes))

    # [8f] 页面：用例/结果 双 tab（chips + 常驻槽）+ 语义模式下拉读取
    tab_src_tab = (ROOT / "cartest" / "app" / "tools"
                   / "single_inject_tab.py").read_text(encoding="utf-8")
    check("用例/结果 双 tab（chips + 常驻槽 visible 切换）",
          "_switch_view" in tab_src_tab and "_rebuild_chips" in tab_src_tab
          and "_cases_slot" in tab_src_tab and "_results_slot" in tab_src_tab
          and "components.chip" in tab_src_tab)
    check("语义模式接线（页面 _voice_mode → Service voice_mode）",
          "def _voice_mode" in tab_src_tab and "voice_mode=voice_mode" in tab_src_tab
          and "single_inject_mode" in tab_src_tab)

    # [8e] 页面：导入用例 → 列表 → 指定用例 → 顺序执行 → 结果卡片
    ok8p, err8p = True, ""
    try:
        ctx8 = _StubCtx()
        tab5 = SingleInjectTab(ctx8)
        tab5._svc = SingleInjectService(adb=_FakeAdb(), judger_factory=_FakeJudger,
                                        monitor_factory=_FakeMonitor)
        tab5.build()
        tmp8 = Path(mkdtemp(prefix="inject_cases_"))
        case_file = tmp8 / "cases.txt"
        case_file.write_text("打开空调\n导航去公司\n播放音乐\n", encoding="utf-8")
        tab5._case_path_field.value = str(case_file)
        tab5._import_cases()
        check("导入用例 → _cases / 列表 / 顺序执行可用",
              len(tab5._cases) == 3 and len(tab5._case_list.controls) == 3
              and tab5._btn_seq.disabled is False,
              f"cases={len(tab5._cases)} controls={len(tab5._case_list.controls)}")
        check("导入后自动切到「用例」tab（views）",
              tab5._view == "cases" and tab5._cases_slot.visible is True
              and tab5._results_slot.visible is False, tab5._view)
        check("用例文件路径落盘",
              ctx8.cfg.get("single_inject_case_file") == str(case_file),
              str(ctx8.cfg.d))

        tab5._pick_case(1)
        check("点击用例 → 指定该条填入输入框",
              tab5._field.value == "导航去公司", str(tab5._field.value))

        tab5._interval_field.value = "0"
        tab5._repeat_field.value = "1"       # 顺序执行应忽略次数
        tab5._run_sequential()
        deadline = time.time() + 20
        while len(tab5._records) < 3 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)                      # 等 IDLE 收尾
        check("顺序执行 → 3 条结果卡片（忽略「次数」）",
              len(tab5._records) == 3 and len(tab5._result_list.controls) == 3,
              str(len(tab5._records)))
        check("执行自动切到「结果」tab（views）",
              tab5._view == "results" and tab5._results_slot.visible is True
              and tab5._cases_slot.visible is False, tab5._view)
        check("tab 计数（用例 3 / 结果 3）",
              [c.content.value for c in tab5._chips_row.controls]
              == ["用例 3", "结果 3"],
              str([c.content.value for c in tab5._chips_row.controls]))
        tab5._switch_view("cases")
        check("手动切回「用例」tab",
              tab5._view == "cases" and tab5._cases_slot.visible is True)
        check("顺序执行收尾汇总（按「条」）",
              any("顺序执行完成 3 条" in tx for tx, _ in ctx8.done),
              str(ctx8.done))

        tab5._clear_cases()
        check("清空用例 → 列表空态 + 顺序执行禁用",
              not tab5._cases and tab5._btn_seq.disabled is True
              and tab5._case_holder.content is tab5._case_empty)

        ctx8.page.web = True
        asyncio.run(tab5._browse())
        check("Web 模式浏览 → 提示手输路径（不创建 picker）",
              any("Web" in tx for tx, _ in ctx8.status), str(ctx8.status[-1:]))
    except Exception as ex:  # noqa: BLE001
        ok8p, err8p = False, f"{type(ex).__name__}: {ex}"
    check("页面顺序执行整体（导入/列表/指定/执行/清空）", ok8p, err8p)

    tab_src8 = (ROOT / "cartest" / "app" / "tools"
                / "single_inject_tab.py").read_text(encoding="utf-8")
    check("浏览功能合规（async + await pick_files + 不碰 overlay）",
          "async def _browse" in tab_src8
          and "await picker.pick_files" in tab_src8
          and ".overlay.append(" not in tab_src8
          and ".overlay.remove(" not in tab_src8)
    check("「循环间隔」已改名「间隔」（循环/顺序共用）",
          'label="间隔(秒)"' in tab_src8 and "循环间隔(秒)" not in tab_src8)

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
