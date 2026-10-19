# -*- coding: utf-8 -*-
"""操作日志窗口（一期/二期/三期）检查 — 模型层 / 渲染集成 / 操作耗时接线

覆盖（23-操作日志窗口设计.md / 24-操作日志窗口实施计划.md）：
  [1] tokens：oplog 键双调色板（深=暗控制台 / 浅=浅底控制台）
  [2] 模型层：解析/噪音/操作耗时/过滤/搜索/导出/上限
  [3] 渲染集成：context 源码断言（spans 行渲染、工具条、兼容转发、结构化 API）
  [3b] 三期：步骤视图卡片聚合 / 只看某操作 / 耗时全量接入
  [4] 操作耗时接线：六个主操作源码断言（T3 单列）

用法:
    python tests/test_oplog.py
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.tools.widgets import oplog  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("操作日志窗口（一期）检查")
    print("=" * 70)

    # ── [1] tokens ──
    from cartest.app.ui import tokens
    d, l = tokens.PALETTE_DARK, tokens.PALETTE_LIGHT
    oplog_keys = [k for k in d if k.startswith("oplog_") or k == "bg_oplog"]
    check("oplog 键齐（双调色板）",
          len(oplog_keys) >= 12 and all(k in l for k in oplog_keys),
          str(sorted(set(oplog_keys) - set(l))))
    check("bg_oplog 双主题各异（深=暗控制台 / 浅=浅底控制台）",
          d["bg_oplog"] == "#1E2027" and l["bg_oplog"] == "#F1F4FA",
          f"d={d['bg_oplog']} l={l['bg_oplog']}")
    check("浅色面板：正文深字 + 操作/错误底为浅色系",
          l["oplog_text"] == "#2A3446" and l["oplog_op_bg"] == "#E3EBFA"
          and l["oplog_error_bg"] == "#FDECEC")
    check("级别色随主题（深=高亮 / 浅=深色可读）",
          d["oplog_ok"] == "#4CAF50" and l["oplog_ok"] == "#047857"
          and d["oplog_error"] == "#F44336" and l["oplog_error"] == "#DC2626")

    # ── [2] 模型层 ──
    with tempfile.TemporaryDirectory() as td:
        m = oplog.OplogModel(reports_dir=td)

        # 解析
        cases = [
            ("[CMD] adb root", "cmd", "adb root"),
            ("[INFO] 检查中...", "info", "检查中..."),
            ("[OK] 完成", "ok", "完成"),
            ("[WARN] 注意", "warn", "注意"),
            ("[FAIL] 失败", "error", "失败"),
            ("[ERR] 异常", "error", "异常"),
            ("[ERROR] 异常", "error", "异常"),
            ("=== Root+Remount 开始 ===", "op", "Root+Remount 开始"),
        ]
        ok_parse = True
        for text, lv, body in cases:
            level, got, detail = oplog.parse_legacy(text)
            if level != lv or got != body or detail:
                ok_parse = False
                check(f"解析 {text!r}", False, f"→ {level}/{got}/{detail}")
        check("旧前缀解析（CMD/INFO/OK/WARN/FAIL/ERR/段标题）", ok_parse)

        # 噪音 vs 关键无前缀行
        for noise in ("List of devices attached", "Using overlayfs for /system",
                      "daemon started successfully", "Transferring: 1.2 MB/s"):
            level, _, detail = oplog.parse_legacy(noise)
            if level != "raw" or not detail:
                check(f"噪音判定 {noise!r}", False, f"→ {level}/{detail}")
        check("噪音行 → raw + detail", True)
        level, _, detail = oplog.parse_legacy("  /la_log/vstgloballogcontrol: 23M")
        check("关键无前缀行 → info 可见（不误藏）", level == "info" and not detail)
        level, _, detail = oplog.parse_legacy("Remount succeeded")
        check("结果行（Remount succeeded）不列噪音", level == "info" and not detail)

        # 追加 + 噪音行**永久隐藏**（2026-10-12：事件窗口只作展示，「详细输出」开关已下线）
        m.append_legacy("[INFO] 开始")
        m.append_legacy("List of devices attached")
        m.append_legacy("[OK] 完成")
        check("噪音行恒不可见（已无开关可放开）",
              [x.level for x in m.visible()] == ["info", "ok"])

        # 操作耗时
        op = m.begin_op("Root+Remount", tag="adb")
        head = m.line_by_seq(m._lines[-1].seq)
        check("操作头行（level=op + op_id）",
              head is not None and head.level == "op" and head.op_id == op)
        m.append("[CMD] adb root", level="cmd")
        import time as _t
        _t.sleep(0.02)
        ln = m.end_op(op, ok=True)
        check("end_op 写回耗时/结果",
              ln is not None and (ln.dur or 0) >= 0.02 and ln.ok is True)
        check("end_op 未知 id → None", m.end_op(999) is None)

        # 模式过滤 / 搜索：2026-10-12 随工具条下线（事件窗口只作展示）
        m.append("[FAIL] 失败一", level="error")
        check("模式恒「全部」：错误行与其他行一起可见",
              [x.level for x in m.visible()][-1] == "error",
              str([x.level for x in m.visible()]))

        # 导出（所见即所得）
        txt = m.export_txt()
        content = txt.read_text(encoding="utf-8")
        check("导出 txt：含时间戳/级别/耗时",
              txt.is_file() and "Root+Remount" in content
              and "[OP]" in content and "耗时" in content
              and "[ERR]" in content)
        html = m.export_html()
        hc = html.read_text(encoding="utf-8")
        check("导出 html：自包含 + 级别色",
              html.is_file() and "#4CAF50" in hc and "#F44336" in hc
              and "oplog" not in hc.lower().replace("<!--", ""))

        # 上限
        m2 = oplog.OplogModel(reports_dir=td)
        for i in range(oplog.MAX_LINES + 100):
            m2.append(f"line-{i}", level="info")
        check("上限：超限丢最旧段", m2.count == oplog.MAX_LINES + 100 - 200,
              str(m2.count))

        # ── 二期：自动高亮 / 折叠 / 级别修正 / HTML 美化（正则检索已随工具条下线）──
        m3 = oplog.OplogModel(reports_dir=td)
        m3.append_legacy("[OK] Root+Remount done")
        m3.append_legacy("adb: no devices/emulators found")
        m3.append_legacy("Permission denied")
        levels = {x.text: x.level for x in m3.visible()}
        check("级别修正：adb 报错 → error",
              levels.get("adb: no devices/emulators found") == "error", str(levels))
        check("级别修正：Permission denied → warn",
              levels.get("Permission denied") == "warn", str(levels))

        ranges = m3.highlight_ranges("设备 5ad9194b 路径 /sdcard/Download/ 容量 512MB")
        kinds = {k for _, _, k in ranges}
        check("自动高亮：设备号/路径/容量（搜索下线后只剩 auto）",
              len(ranges) >= 3 and kinds == {"auto"}, str(ranges))

        op2 = m3.begin_op("批量操作")
        m3.append_legacy("[推送] 明细一")
        check("事件优先（2026-09-22 改）：子行**默认展开**",
              m3.expand_events is True and m3.is_expanded(op2)
              and any(x.text == "[推送] 明细一" for x in m3.visible())
              and any(x.level == "op" for x in m3.visible())
              and m3.child_count(op2) == 1)
        check("点事件行 → 单独收起（明细行消失）",
              (not m3.toggle_expand(op2)) and not m3.is_expanded(op2)
              and not any(x.text == "[推送] 明细一" for x in m3.visible()))
        check("再 toggle → 展开",
              m3.toggle_expand(op2) and m3.is_expanded(op2)
              and any(x.text == "[推送] 明细一" for x in m3.visible()))
        check("操作块取行（复制该操作块用）",
              len(m3.op_lines(op2)) == 2, str(len(m3.op_lines(op2))))

        html2 = m3.export_html()
        hc2 = html2.read_text(encoding="utf-8")
        check("HTML 美化：行类 + 级别色 + 操作行底纹",
              'class="row op"' in hc2 and "#4CAF50" in hc2
              and "background:#24304A" in hc2 and ".log{white-space:pre-wrap}" in hc2)

    # ── [3] 渲染集成（源码断言）──
    context_src = (ROOT / "cartest" / "app" / "tools" / "context.py").read_text(encoding="utf-8")
    _oplog_src = (ROOT / "cartest" / "app" / "tools" / "widgets"
                  / "oplog.py").read_text(encoding="utf-8")
    check("spans 行渲染（单控件多段）", "TextSpan" in context_src and "spans=" in context_src)
    check("字体栈 + 行高 1.5",
          "JetBrains Mono" in context_src and "height=1.5" in context_src)
    check("兼容转发 append_log_to_panel", "def append_log_to_panel" in context_src)
    check("多行输出自动拆行（逐行分级）", "splitlines" in context_src)
    check("事件窗口无滚动锁控件（📌 已随工具条下线）",
          "_toggle_follow" not in context_src and "max_scroll_extent" not in context_src)
    # 发现 #8（2026-09-21 起由构造消除）：合并页每 tab 各自常驻日志面板槽，
    # 切换只切可见性——不再"切换时替换面板内容"，也就不存在"写进不可见面板"
    pages_src = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")
    check("合并页各 tab 常驻日志面板 + 可见性切换（发现 #8 构造消除）",
          'log_slots[sub_key] = log_slot' in pages_src
          and 'e["log_slot"].visible = active' in pages_src
          and "log_holder" not in pages_src
          and 'spacing=2, expand=True)' in context_src)
    apk_src = (ROOT / "cartest" / "app" / "tools" / "apk_tab.py").read_text(encoding="utf-8")
    check("APK 页日志统一到操作日志面板（内联日志条移除）",
          "append_log_to_panel" in apk_src and "[self._log_text]" not in apk_src)
    # 2026-10-12 用户裁决：事件窗口**只作展示** → 工具条与能力集整体下线
    check("事件窗口工具条已完全移除（纯展示面板）",
          all(s not in context_src for s in (
              "_build_log_toolbar", "LogWindowSpec", "log_spec",
              "spec.regex", "spec.search", "spec.mode", "spec.detail",
              "spec.follow", "spec.clear", "_apply_query", "_on_mode_click",
              "_log_search", "_regex_sw", "_detail_sw", "_pin_txt",
              "_mode_btns", "_toggle_follow")))
    check("模型侧模式/查询/详情能力已删除（源码）",
          all(s not in _oplog_src for s in (
              "def set_mode", "def set_query", "_text_match",
              "set_show_detail", "_use_regex", "invalid_regex")))
    cc_src = (ROOT / "cartest" / "core" / "config_center.py").read_text(encoding="utf-8")
    check("配置页「事件窗口（操作日志）」整组已移除（无残留键）",
          all(k not in cc_src for k in (
              "ui.oplog.mode", "ui.oplog.detail", "ui.oplog.follow",
              "ui.oplog.search", "ui.oplog.regex", "ui.oplog.clear")))
    check("事件块展开/右键菜单 + 导出该事件块（源码）",
          "_toggle_op_view" in context_src and "on_secondary_tap" in context_src
          and "复制该事件块" in context_src and "导出该事件块" in context_src)
    check("自动高亮接入渲染（源码）", "highlight_ranges" in context_src)
    check("结构化 API（begin_op/end_op/log）",
          all(s in context_src for s in ("def begin_op", "def end_op", "def log(")))
    check("面板底改用 bg_oplog", 'c("bg_oplog")' in context_src)

    # ── [3b] 三期：步骤视图（Timeline）+ 事件优先口径 + 耗时全量接入 ──
    with tempfile.TemporaryDirectory() as td:
        m4 = oplog.OplogModel(reports_dir=td)
        m4.append_legacy("[INFO] 平台就绪")           # 未归属 → 默认显示
        op_a = m4.begin_op("Root+Remount", tag="adb",
                           module="日志终端", param="设备 5ad9194b")
        m4.append("remount ok", level="ok")
        m4.append("adb: no devices/emulators found", level="error")
        time.sleep(0.02)
        m4.end_op(op_a, ok=True)
        op_b = m4.begin_op("大文件推送", tag="工具", module="应用与传输",
                           param="st.bin · 10.0 MB")   # 进行中（不收口）
        m4.append("push chunk 1", level="cmd")

        # 事件优先默认口径
        vis_levels = [x.level for x in m4.visible()]
        check("默认展开：事件行 + 全部子行 + 未归属行（噪音 detail 行仍隐藏）",
              vis_levels == ["info", "op", "ok", "error", "op", "cmd"],
              str(vis_levels))
        check("子行计数（＋N 行提示用）",
              m4.child_count(op_a) == 2 and m4.child_count(op_b) == 1)
        check("模块继承：子行归属其事件模块",
              all(x.module == "日志终端" for x in m4.op_lines(op_a)))

        # 「详细输出」现在只放行**噪音/detail 行**（事件子行已默认展开，与之解耦）
        print("    （默认展开可见行数：", len(m4.visible()), "）")
        m4.toggle_expand(op_a)
        check("单独收起 op_a → 其子行隐藏，但**错误行常驻**",
              not m4.is_expanded(op_a) and m4.child_count(op_a) == 2
              and [x.level for x in m4.visible()]
              == ["info", "op", "error", "op", "cmd"],
              str([x.level for x in m4.visible()]))
        m4.toggle_expand(op_a)
        check("再点展开 → 子行回来", m4.is_expanded(op_a)
              and len(m4.visible()) == 6, str(len(m4.visible())))

        # 事件模型（步骤视图已删除；op_lines / 耗时仍服务终端视图与事件块导出）
        head_a = m4.op_lines(op_a)[0]
        check("事件行耗时/结果写回（end_op）",
              head_a.dur is not None and (head_a.dur or 0) >= 0.02 and head_a.ok is True,
              f"dur={head_a.dur} ok={head_a.ok}")
        check("事件块行序（头行在前，子行在后）",
              [x.level for x in m4.op_lines(op_a)] == ["op", "ok", "error"])

        head = m4.op_lines(op_a)[0]
        check("plain 导出：事件行带 [模块] 与参数摘要",
              "[OP] [日志终端] Root+Remount" in m4.plain(head)
              and "(设备 5ad9194b)" in m4.plain(head), m4.plain(head))
        blk = m4.export_block(op_a)
        _btext = blk.read_text(encoding="utf-8") if blk else ""
        check("导出单个事件块（只含该块，含头行时间戳与耗时）",
              blk is not None and blk.is_file() and "Root+Remount" in _btext
              and "remount ok" in _btext and "耗时" in _btext
              and "大文件推送" not in _btext and f"_op{op_a}." in blk.name,
              blk.name if blk else "None")
        check("导出不存在的事件 → None", m4.export_block(999) is None)
        m4.clear()
        check("clear 重置展开集",
              not m4.expanded
              and m4.visible() == [])

        m5 = oplog.OplogModel(reports_dir=td)
        m5.append_legacy("[INFO] 无操作")
        check("无操作时仍保留未归属行（终端视图可见）",
              [x.text for x in m5.visible()] == ["无操作"])
        html3 = m5.export_html()
        check("HTML 导出仍可用（空事件表）", html3.is_file())

    check("步骤视图已整体删除（模型/视图/筛选条均无残留）",
          all(s not in context_src for s in ("_op_card", "_other_card", "_focus_op",
                                             "_update_filter_row", "只看这个",
                                             "def _set_view", "def _rebuild_step_view",
                                             "_touch_step_view")))
    check("oplog 模型侧步骤视图聚合已移除",
          all(s not in _oplog_src for s in ("OpCard", "op_cards", "op_card",
                                            "FILTER_UNATTRIBUTED", "set_filter_op",
                                            "unattributed_count", "self.view")))
    check("操作计时上下文 op()（耗时全量接入样板）",
          "def op(" in context_src and "class _OpHandle" in context_src)
    # 事件优先接线（模块**筛选** 2026-10-12 已删：页级单模块 → 开关打开也恒隐藏）
    check("事件优先：模块映射 + [模块] 渲染 + ＋N 行（源码）",
          all(s in context_src for s in ("_MODULE_BY_KEY", "child_count",
                                         "label = ln.module")))
    check("模块筛选已彻底移除（context 侧无残留）",
          all(s not in context_src for s in ("_module_filter", "def modules",
                                             "_module_dd", "_module_options",
                                             "_refresh_module_dd", "_on_module_select",
                                             "spec.module")))
    check("模块筛选已彻底移除（模型侧无残留）",
          all(s not in _oplog_src for s in ("_module_filter", "def modules",
                                            "set_module_filter")))
    check("事件优先：展开语义（toggle_expand / expand；无 collapsed 残留）",
          "toggle_expand" in context_src and "collapsed" not in context_src)
    for _f, _needles in (
        ("cartest/app/tools/log_tab.py",
         ("ADB 连接", "查看大小", "截图", "Logcat 抓取", "param=")),
        ("cartest/app/tools/apk_tab.py",
         ("安装 APK", "卸载 APK", "APK 信息", "强制停止", "包列表",
          "推送文件", "拉取文件", "查询版本", "param=")),
        ("cartest/app/tools/monkey_tab.py",
         ("拉取 + 分析", '"停止"', "刷新状态", '"部署"', '"一键流"', "param=")),
        ("cartest/app/tools/widgets/offline_extract.py", ("对话还原", "param=")),
        ("cartest/app/tools/push_tab.py", ("大文件推送", "param=")),
    ):
        _src = (ROOT / _f).read_text(encoding="utf-8")
        _missing = [_n for _n in _needles if _n not in _src]
        check(f"耗时/参数接入 {_f.split('/')[-1]}", not _missing, str(_missing))

    # ── [4] 操作耗时接线（T3）──
    def _count(files: list[str], needle: str) -> int:
        total = 0
        for f in files:
            total += (ROOT / f).read_text(encoding="utf-8").count(needle)
        return total

    n_begin = _count([
        "cartest/app/tools/log_tab.py",
        "cartest/app/tools/push_tab.py",
        "cartest/app/tools/monkey_tab.py",
    ], "begin_op(")
    n_end = _count([
        "cartest/app/tools/log_tab.py",
        "cartest/app/tools/push_tab.py",
        "cartest/app/tools/monkey_tab.py",
    ], "end_op(")
    check("六个主操作接线（begin_op ×6 / end_op ≥6）", n_begin >= 6 and n_end >= 6,
          f"begin={n_begin} end={n_end}")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("操作日志窗口（一期）检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
