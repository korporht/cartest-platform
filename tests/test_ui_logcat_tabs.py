# -*- coding: utf-8 -*-
"""28/29 验收 — 日志终端合并页（三 tab 平级 + 常驻槽；无真机）

覆盖：
  [1] 形态：并进合并页壳、无 300px 叠层 sub_holder、三 tab 声明、基键 logtools
  [2] _normalize_subs：3/4/5 元组兼容（with_log 缺省 None＝沿用页面级）
  [3] 运行时构造 + 常驻槽语义（懒构建 / 切 visible / 无外挂槽）
  [4] 离页生命周期：保存 _active + 对已建 tab 调 on_detach（40 号：离页不停任务）
  [5] 外挂日志槽语义（#8 构造消除）——用**探针合并页**覆盖
      （2026-10-12 起所有真实合并页 with_log=False，机制本身仍需回归）

用法:
    python tests/test_ui_logcat_tabs.py
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


class _StubState:
    """ToolContext / 页面壳只需 theme + tool_state（+ before_page_leave 注册位）"""

    def __init__(self) -> None:
        from cartest.app.theme import AppTheme
        self.theme = AppTheme()
        self.tool_state: dict = {}
        self.before_page_leave = None


class _StubPage:
    """替身 page（PushTab 等会读 ``web`` / 用 ``run_thread`` / ``overlay``）"""

    web = False

    def __init__(self) -> None:
        self.overlay: list = []
        self.clipboard = self

    def run_thread(self, handler, *a, **kw) -> None:  # noqa: ANN001
        pass

    def run_task(self, handler, *a, **kw) -> None:   # noqa: ANN001
        pass

    def update(self) -> None:
        pass

    def show_dialog(self, d) -> None:  # noqa: ANN001
        pass

    def set(self, text: str) -> None:  # 兜底 clipboard.set
        pass


class _ProbeTab:
    """最小替身 tab（只满足 ``_wrap_merged`` 的契约，用于日志槽机制回归）"""

    def __init__(self, ctx) -> None:  # noqa: ANN001
        self.ctx = ctx
        self.hide_calls = 0

    def build(self):  # noqa: ANN201
        import flet as ft
        return ft.Text("probe")

    def save_state(self) -> dict:
        return {}

    def restore_state(self, state) -> None:  # noqa: ANN001
        pass

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        self.hide_calls += 1


def main() -> int:
    print("=" * 70)
    print("28/29 验收 — 日志终端合并页（三 tab 平级 + 常驻槽）")
    print("=" * 70)

    from cartest.app.tools import pages as tp

    SRC = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
    logcat_src = SRC.split("def build_logcat", 1)[1].split("def build_screen", 1)[0]

    # ── [1] 形态：并进合并页壳，叠层机制删除 ──
    check("build_logcat 已无叠层 holder", "sub_holder" not in logcat_src)
    check("build_logcat 已无 300px 限高", "height=300" not in logcat_src)
    check("build_logcat 走合并页壳", "_wrap_merged(" in logcat_src)
    check("两 tab 声明（日志终端/时间戳转换）",
          all(s in logcat_src for s in ('"日志终端"', '"时间戳转换"')))
    check("离线提取已迁出本页（独立页）",
          '"离线提取"' not in logcat_src and "def build_offline(" in SRC)
    check("旧基键 logcat_sub 已弃用", '"logcat_sub"' not in SRC)
    check("新基键 logtools", '"logtools"' in logcat_src)

    # ── [2] _normalize_subs：3/4/5 元组兼容 ──
    subs = tp._normalize_subs([
        ("a", "A", lambda c: None),
        ("b", "B", lambda c: None, False),
        ("c", "C", lambda c: None, True, True),
    ])
    check("规范化 3/4/5 元组",
          [s.key for s in subs] == ["a", "b", "c"]
          and subs[0].with_log is None and subs[0].keep_alive is False
          and subs[1].with_log is False and subs[1].keep_alive is False
          and subs[2].with_log is True and subs[2].keep_alive is True)

    # ── [3] 运行时构造 + 常驻槽语义 ──
    ok, err = True, ""
    cnt3: dict = {}
    try:
        st = _StubState()
        col = tp.build_logcat(None, st)
        check("返回 ft.Column（可构造）", col.__class__.__name__ == "Column")
        api = col._merged
        cnt3 = api["counters"]
        check("默认 active=logcat", api["active"]() == "logcat")
        check("非激活 tab 未构建（懒）", set(api["slots"]) == {"logcat"})
        check("logcat 内容槽可见", api["slots"]["logcat"].visible is True)
        check("logcat 自带面板 → 无外挂日志槽", "logcat" not in api["log_slots"])

        api["switch"]("timestamp")
        check("切 tab 后 active=timestamp", api["active"]() == "timestamp")
        check("logcat 槽仍常驻（对象未被换出）", api["slots"]["logcat"] is not None)
        check("logcat 槽已隐藏", api["slots"]["logcat"].visible is False)
        check("timestamp 槽可见", api["slots"]["timestamp"].visible is True)
        check("timestamp 无事件窗口槽", api["log_slots"].get("timestamp") is None)

        check("本页无外挂日志槽（LogTab 自带面板）", api["log_slots"] == {})

        api["switch"]("logcat")
        check("切回 logcat：内容槽可见且 timestamp 槽隐藏",
              api["slots"]["logcat"].visible is True
              and api["slots"]["timestamp"].visible is False)
        check("切 tab 派发生命周期（40 号 on_detach：只解绑 UI）",
              cnt3.get("hide", {}).get("logcat", 0) == 1, str(cnt3))
        check("on_detach 不停任务（log_tab 已无离页停抓取口径）",
              "def on_hide" not in (ROOT / "cartest" / "app" / "tools" / "log_tab.py")
              .read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001
        ok, err = False, f"{type(ex).__name__}: {ex}"
    check("三 tab 运行时构造 + 切换语义", ok, err)

    # ── [4] 离页：所有已建 tab 存态 + on_detach（40 号：离页不停任务）──
    ok4, err4 = True, ""
    try:
        st4 = _StubState()
        col4 = tp.build_logcat(None, st4)
        api4 = col4._merged
        api4["switch"]("timestamp")
        st4.before_page_leave()                      # 模拟切页/切主题
        cnt4 = api4["counters"]
        check("离页保存 active 到 tool_state",
              st4.tool_state.get("logtools", {}).get("_active") == "timestamp")
        check("离页对已建 tab 再次 on_detach（只解绑 UI；计数累计）",
              cnt4.get("hide_total") == 3
              and cnt4.get("hide", {}).get("logcat") == 2
              and cnt4.get("hide", {}).get("timestamp") == 1, str(cnt4))
    except Exception as ex:  # noqa: BLE001
        ok4, err4 = False, f"{type(ex).__name__}: {ex}"
    check("离页生命周期", ok4, err4)

    # ── [5] 外挂日志槽语义（#8 构造消除）——探针合并页 ──
    ok5, err5 = True, ""
    try:
        st5 = _StubState()
        probe = tp._wrap_merged(
            _StubPage(), st5, "probe",
            subs=[("a", "甲", lambda ctx: _ProbeTab(ctx)),
                  ("b", "乙", lambda ctx: _ProbeTab(ctx))],
            with_status=False, with_log=True)
        api5 = probe._merged
        check("探针页：懒构建（初始仅 a）", set(api5["log_slots"]) == {"a"},
              str(list(api5["log_slots"])))
        check("激活 tab 的日志槽可见",
              api5["log_slots"]["a"].visible is True)
        api5["switch"]("b")
        check("两 tab 各自带事件窗口槽",
              set(api5["log_slots"]) == {"a", "b"}, str(list(api5["log_slots"])))
        check("#8 构造消除：切换后仅激活 tab 的日志槽可见",
              api5["log_slots"]["b"].visible is True
              and api5["log_slots"]["a"].visible is False)
        check("两 tab 常驻（槽对象未被换出）",
              api5["slots"]["a"] is not None and api5["slots"]["b"] is not None)
    except Exception as ex:  # noqa: BLE001
        ok5, err5 = False, f"{type(ex).__name__}: {ex}"
    check("外挂日志槽语义（探针合并页；机制回归）", ok5, err5)

    # ── [6] 2026-10-12 用户裁决：挂输出窗口的页面 ──
    check("挂输出区：应用与传输 + Monkey（内容自然高度 + 面板铺满剩余）",
          SRC.count("with_log=True)") == 2 and "log_height" not in SRC
          and "expand=not use_log" in SRC,
          f"with_log=True 出现 {SRC.count('with_log=True)')} 次")
    check("其余页面均不挂输出窗口（with_log=False）",
          all(f"def {fn}(" in SRC for fn in
              ("build_monkey", "build_logextract", "build_single_inject",
               "build_offline", "build_screen")))
    check("日志终端页仍带面板（LogTab 自带，非外挂）",
          "LogTab" in logcat_src and "with_log=False" in logcat_src)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("日志终端合并页检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
