# -*- coding: utf-8 -*-
"""UI-5 验收 — 动效打磨 + 配置页收尾（无 UI）

覆盖（源码断言）：
  · 主题图标随主题（深→☀ / 浅→☾；UI-1 遗留项①）
  · 侧栏「配置固定 + 分组滚动」分区（≤800px 高窗口优化；UI-1 遗留项②）
  · 抽屉 150ms ease-out 滑入（§7）
  · 配置页动作区（保存按钮在顶部标题行 + 底部状态行；§6.5；行为测试见 test_settings_feedback）

设计依据：14-UI重构设计.md §6.5 / §7 / §10（#7 #10）。
用法:
    python tests/test_ui_polish.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_SRC = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
SETTINGS_SRC = (ROOT / "cartest" / "app" / "pages" / "settings.py").read_text(encoding="utf-8")
RESULTS_SRC = (ROOT / "cartest" / "app" / "pages" / "results.py").read_text(encoding="utf-8")
CONTEXT_SRC = (ROOT / "cartest" / "app" / "tools" / "context.py").read_text(encoding="utf-8")
LOGTAB_SRC = (ROOT / "cartest" / "app" / "tools" / "log_tab.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("UI-5 验收 — 动效打磨 + 配置页收尾（无 UI）")
    print("=" * 70)

    # ── [1] 主题图标随主题 ──
    check("图标双态（LIGHT_MODE / DARK_MODE）",
          "LIGHT_MODE if" in MAIN_SRC and "DARK_MODE" in MAIN_SRC)
    check("依据 is_dark 选择", "state.theme.is_dark" in MAIN_SRC)
    check("tooltip 随主题（切到浅色/切到深色）",
          "切到浅色" in MAIN_SRC and "切到深色" in MAIN_SRC)

    # ── [2] 侧栏固定/滚动分区 ──
    check("固定区与滚动区分离（源码结构）",
          "固定区" in MAIN_SRC and "滚动区" in MAIN_SRC
          and "content=ft.Column(groups" in MAIN_SRC)
    check("配置固定在滚动区之前", MAIN_SRC.index("SETTINGS_LABEL") < MAIN_SRC.index("滚动区"))
    check("分组区占满余下高度（expand）",
          "content=ft.Column(groups, spacing=3, scroll=ft.ScrollMode.AUTO)" in MAIN_SRC)

    # ── [3] 抽屉动画（§7：150ms ease-out）──
    check("抽屉 150ms 动画", "animate=ft.Animation(150" in RESULTS_SRC)
    check("缓动 EASE_OUT", "EASE_OUT" in RESULTS_SRC)

    # ── [4] 配置页动作区（2026-10-15：取消「保存」按钮 → 改动即时落盘）──
    check("已无「保存」按钮（header 不含 save_btn）",
          "save_btn" not in SETTINGS_SRC)
    check("标题行说明「改动即时保存」",
          "改动即时保存" in SETTINGS_SRC.split("header = ft.Row(", 1)[1].split("])", 1)[0])
    check("底部只留状态行",
          "ft.Row([status]" in SETTINGS_SRC)
    check("卡片区独立滚动",
          "cards_col = ft.Column(cards, spacing=10, scroll=ft.ScrollMode.AUTO" in SETTINGS_SRC)

    # ── [5] 行为回归入口（check_all 另跑 test_settings_feedback）──
    check("保存反馈行为测试存在", (ROOT / "tests" / "test_settings_feedback.py").is_file())

    # ── [6] 日志终端布局整理（2026-09-17 用户反馈 + 真机验证发现 #1）──
    # 2026-10-12：📌「跟随」开关随工具条下线，事件窗口恒 auto_scroll（纯展示口径）
    check("事件窗口恒自动跟随（📌 开关已删除）",
          "_toggle_follow" not in CONTEXT_SRC and "auto_scroll=True" in CONTEXT_SRC)
    check("三按钮已删除（标记日志/查看标记/运行日志）",
          "标记日志" not in LOGTAB_SRC and "查看标记" not in LOGTAB_SRC
          and "运行日志" not in LOGTAB_SRC)
    check("标记服务与常量已移除",
          "LogMarkService" not in LOGTAB_SRC and "LOG_MARKS" not in LOGTAB_SRC)
    check("日志终端页挂载新版面板（工具条 + bg_oplog；旧 11/7 容器已被 23 设计取代）",
          "build_log_panel" in LOGTAB_SRC and "left=11, top=7" not in LOGTAB_SRC)
    check("日志终端页不再双挂载（无第二个日志面板）",
          "ctx.build_log_panel()" not in
          (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
          .split("def build_logcat", 1)[1].split("def build_screen", 1)[0])

    # ── [7] 离线提取入口（CbmLogextractor 独立使用，2026-09-17 用户确认）──
    PAGES_SRC = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
    PANEL_PATH = ROOT / "cartest" / "app" / "tools" / "widgets" / "offline_extract.py"
    check("折叠区「离线提取」已挂载",
          "离线提取" in PAGES_SRC and "OfflineExtractPanel" in PAGES_SRC)
    if PANEL_PATH.is_file():
        panel_src = PANEL_PATH.read_text(encoding="utf-8")
        check("走 offline.extract_report（独立于流水线）", "extract_report" in panel_src)
        check("不可用守卫（available/LogextractUnavailable）",
              "available()" in panel_src and "LogextractUnavailable" in panel_src)
        check("后台线程 + 日志转发",
              "run_thread" in panel_src and "append_log_to_panel" in panel_src)
        check("报告/目录打开 + 目录选择",
              "打开报告" in panel_src and "打开目录" in panel_src
              and "get_directory_path" in panel_src)
    else:
        check("离线提取面板存在", False, "missing offline_extract.py")

    # ── [8] 默认主题 / 主题切换下发 / 截图预览（2026-09-17 用户反馈）──
    import importlib
    theme_mod = importlib.import_module("cartest.app.theme")
    th = theme_mod.AppTheme()
    check("默认主题为明亮", th.is_dark is False)
    th.toggle()
    check("一键切换后为深色", th.is_dark is True)
    toggle_body = MAIN_SRC.split("def _toggle_theme", 1)[1].split("async def _tick", 1)[0]
    check("切换主题后 page.update() 下发（防 Material 控件残留旧色）",
          "page.update()" in toggle_body)
    TOUCH_SRC = (ROOT / "cartest" / "app" / "tools" / "touch_tab.py").read_text(encoding="utf-8")
    check("截图预览 data URI 内联（Web 端可显示；flet 0.85 无 src_base64）",
          "data:image/png;base64," in TOUCH_SRC and "_data_uri" in TOUCH_SRC)
    check("预览图缩放 + mtime 缓存（切页不卡：JPEG ≤1600px）",
          "_preview_data_uri" in TOUCH_SRC and "image/jpeg" in TOUCH_SRC
          and "_PREVIEW_CACHE" in TOUCH_SRC)
    # 行为（有样本时）：缩放 JPEG 体积应远小于原图
    import importlib as _il
    import os as _os
    _tt = _il.import_module("cartest.app.tools.touch_tab")
    if _os.path.exists(_tt.TOUCH_SHOT):
        _raw = _os.path.getsize(_tt.TOUCH_SHOT)
        _uri = _tt._preview_data_uri(_tt.TOUCH_SHOT)
        check("预览 URI 为缩放 JPEG 且体积 < 原图",
              _uri.startswith("data:image/jpeg;base64,") and len(_uri) < _raw,
              f"png={_raw}B uri={len(_uri)}B")
        check("预览编码缓存命中（二次调用一致）",
              _tt._preview_data_uri(_tt.TOUCH_SHOT) == _uri)
    else:
        check("预览样本不存在（体积断言跳过）", True)
    check("截图分辨率同步真实尺寸（PNG IHDR）",
          "_png_size" in TOUCH_SRC and "self._device_w, self._device_h = size" in TOUCH_SRC)
    STATE_SRC = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")
    check("默认页为日志终端（tool_logcat）", 'active_page: str = "tool_logcat"' in STATE_SRC)

    # ── [9] 输入控件浅色可读（2026-09-17 用户反馈：bg_log 黑底黑字）──
    PUSH_SRC = (ROOT / "cartest" / "app" / "tools" / "push_tab.py").read_text(encoding="utf-8")
    APK_SRC = (ROOT / "cartest" / "app" / "tools" / "apk_tab.py").read_text(encoding="utf-8")
    TS_SRC = (ROOT / "cartest" / "app" / "tools" / "timestamp_tab.py").read_text(encoding="utf-8")
    check("push 输入框填充随主题（bg_panel，无 bg_log）",
          'bgcolor=t.c("bg_panel")' in PUSH_SRC and "bg_log" not in PUSH_SRC)
    check("apk 路径/包名填充随主题（bg_panel ×2）",
          APK_SRC.count('bgcolor=t.c("bg_panel")') >= 2)
    check("时间戳输入框填充随主题（bg_panel，无 bg_log）",
          'bgcolor=t.c("bg_panel")' in TS_SRC and "bg_log" not in TS_SRC)
    check("日志终端挂载新版面板；预览区底层仍深色（bg_log）",
          "build_log_panel" in LOGTAB_SRC
          and 'bgcolor=t.c("bg_log")' in
          (ROOT / "cartest" / "app" / "tools" / "touch_tab.py").read_text(encoding="utf-8"))

    # ── [10] 明亮模式操作日志可读 + Monkey 日志窗布局（2026-09-18 用户反馈）──
    from cartest.app.ui import tokens as _tk
    _d, _l = _tk.PALETTE_DARK, _tk.PALETTE_LIGHT
    check("操作日志面板底双主题各异（浅色不再恒暗 #1E2027）",
          _d["bg_oplog"] == "#1E2027" and _l["bg_oplog"] != "#1E2027",
          f"light={_l['bg_oplog']}")
    # 2026-10-12：工具条（含主题着色的开关/按钮）整体下线 → 只校验面板与行的着色
    check("面板底随主题着色（bg_oplog + border；禁固定色）",
          'bgcolor=t.c("bg_oplog")' in CONTEXT_SRC
          and 'border=ft.border.Border.all(1, t.c("border"))' in CONTEXT_SRC)
    check("日志行/状态色随主题（不再用固定 oplog_info）",
          "self.theme.c(" in CONTEXT_SRC
          and 'oplog_info"))' not in CONTEXT_SRC)
    check("带日志窗口时主体只占自身高度（窗口起点不再被空白压低）",
          "expand=not with_log" in
          (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8"))

    # 运行时构造（源码断言抓不到 flet 控件 kwargs 拼写错误——2026-09-18 曾因
    # Switch(label_style=...) 不存在导致整页 ErrorWidget；check_all 源码断言全绿）
    from cartest.app.theme import AppTheme as _AppTheme
    from cartest.app.tools.context import ToolContext as _ToolContext

    class _StubState:                      # ToolContext 只用 theme / tool_state
        def __init__(self) -> None:
            self.theme = _AppTheme()
            self.tool_state: dict = {}

    _ok, _err = True, ""
    for _dark in (False, True):
        try:
            _st = _StubState()
            if _dark:
                _st.theme.toggle()
            _c = _ToolContext(None, _st, "logcat")
            _c.build_log_panel()
        except Exception as _ex:  # noqa: BLE001
            _ok, _err = False, f"{'dark' if _dark else 'light'}: {type(_ex).__name__}: {_ex}"
    check("日志面板可运行时构造（双主题；flet kwargs 有效）", _ok, _err)

    # 事件优先：终端视图运行时构造（展开/收起 + 右键菜单 + 导出块——flet kwargs 同样要真跑）
    _ok3, _err3 = True, ""
    try:
        _st3 = _StubState()
        _c3 = _ToolContext(None, _st3, "logcat")
        _c3.append_log_to_panel("未归属的一行")
        _oid = _c3.begin_op("Root+Remount", tag="adb")
        _c3.log("remount ok", level="ok")
        _c3.end_op(_oid, ok=True)
        _c3._oid2 = _c3.begin_op("进行中的操作")      # 未收口事件
        _oid3 = _c3.begin_op("失败的操作", param="包名=com.x")
        _c3.log("boom", level="error")
        _c3.end_op(_oid3, ok=False)
        _c3.build_log_panel()
        _c3._toggle_op_view(_oid3)                    # 点事件行收起
        _c3._toggle_op_view(_oid3)                    # 再点展开
        _c3._show_line_menu(_c3._log_model.op_lines(_oid3)[0])   # 右键菜单（构造对话框）
        _c3._export_block_ok = _c3._log_model.export_block(_oid3) is not None
        _c3.clear_log_panel()
    except Exception as _ex:  # noqa: BLE001
        _ok3, _err3 = False, f"{type(_ex).__name__}: {_ex}"
    check("终端视图可运行时构造（展开/收起/右键菜单/导出块/清空）",
          _ok3 and _c3._export_block_ok, _err3)

    # 事件模型会话级持有：同一 state 重建 ToolContext（切页/切主题）后历史不丢
    _ok4, _err4 = True, ""
    try:
        _st4 = _StubState()
        _c1 = _ToolContext(None, _st4, "logcat")
        _c1.build_log_panel()
        _o1 = _c1.begin_op("查看大小", tag="设备", param="路径 3 条")
        _c1.log("/la_log/x: 264M", level="info")
        _c1.end_op(_o1, ok=True)
        _c1.append_log_to_panel("[OK] 收尾行")     # 事件外散行：仍应带本页模块
        _n1 = len(_c1._log_panel_list.controls)
        # 模拟「切主题 → 页面重建」：同一 state 新建 ctx
        _c2 = _ToolContext(None, _st4, "logcat")
        _c2.build_log_panel()
        _n2 = len(_c2._log_panel_list.controls)
        _same = _c2._log_model is _c1._log_model
        _mod_ok = all(ln.module == "日志终端" for ln in _c2._log_model._lines)
        _ok4 = _same and _mod_ok and _n2 > 0 and _n2 == _n1
        if not _ok4:
            _err4 = f"same_model={_same} mod={_mod_ok} n1={_n1} n2={_n2}"
    except Exception as _ex:  # noqa: BLE001
        _ok4, _err4 = False, f"{type(_ex).__name__}: {_ex}"
    check("事件模型会话级（重建后历史回填）", _ok4, _err4)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("UI-5 动效打磨检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
