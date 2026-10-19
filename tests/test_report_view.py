# -*- coding: utf-8 -*-
"""32 验收 — 离线提取报告解析（core/parser/report.py）

覆盖：
  [1] 解析 HTML 总报告（<pre> 内嵌详细 JSON；md 包装兼容路径）→ cbm/bds 两段
  [2] 展平记录：补 source、缺失字段空串、按 query_time 排序
  [3] KPI 汇总：total/answered/unanswered + 各段计数
  [4] 健壮性：文件不存在 / 无数据块（md·HTML）/ json 损坏（md·HTML）→ ReportError（不静默）
  [5] 真实报告冒烟（若 CbmLogextractor reports/ 有产物则解析一份）
  [6] 视图接线回归（2026-09-24 全按钮点验）：搜索实时过滤 / 解析失败回退下拉
  [7] 布局回归（2026-10-15 裁决）：无右栏 / 记录表占满 / 抽屉「原始 JSON」按需展开 / 行高亮 / 筛选对齐

用法:
    python tests/test_report_view.py
"""
import html as H
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


SAMPLE = {
    "report_time": "2026-09-10 17:38:42",
    "cbm": {"total": 2, "matched": 1, "unmatched_query": 1, "unmatched_logid": 0,
            "records": [
                {"source_file": "main_00211.gz", "query_time": "2026-09-10 16:05:14.441",
                 "query": "退出", "sid": "wgw000bcf22", "domain": "cmd",
                 "cbm_tool_pk_type": "cbm_semantic", "logid_time": "", "logid": "",
                 "traceId": "t-56", "reqMessageId": "", "reply": ""},
                {"source_file": "main_00211.gz", "query_time": "2026-09-10 16:04:40.956",
                 "query": "零跑C11和E0V比", "sid": "wgw000bc54f", "domain": "其他技能",
                 "cbm_tool_pk_type": "cbm_agent", "logid_time": "2026-09-10 16:04:43.415",
                 "logid": "202609101604412BE96BBA", "traceId": "t-51", "reqMessageId": "",
                 "reply": "我无法查到该车型。"},
            ]},
    "bds": {"total": 1, "records": [
        {"source_file": "main_00211.gz", "query_time": "2026-09-10 15:47:18.318",
         "query": "一位加班攀比烂", "sid": "b5180a69", "domain": "chat",
         "logid": "20260910154322F731E6", "reply": "",
         "reply_status": "no_logid",
         "reply_missing_reason": "该查询未关联到 BDS logid, 无法关联 websocket 回复"},
    ]},
}


def main() -> int:
    print("=" * 70)
    print("32 验收 — 离线提取报告解析")
    print("=" * 70)

    from cartest.core.parser import report as rp

    tmp = Path(mkdtemp(prefix="report_"))

    # ── [1] 解析 HTML 总报告（<pre> 内嵌详细 JSON；真实产物形态）──
    detail = H.escape(json.dumps(SAMPLE, ensure_ascii=False, indent=2), quote=True)
    html_f = tmp / "all_report_test.html"
    html_f.write_text(
        "<html><body>"
        "<pre>{ \"cbm\": { \"total\": 0 }, \"bds\": {} }</pre>"   # 简单报告干扰项（无 report_time）
        f"<pre>{detail}</pre>"
        "<pre># 链路追踪报告</pre>"
        "<pre># 角色对话提取报告</pre>"
        "</body></html>", encoding="utf-8")
    data, raw = rp.load_report(html_f)
    check("解析出 report_time", data.get("report_time") == SAMPLE["report_time"])
    check("解析出 cbm/bds 两段",
          isinstance(data.get("cbm"), dict) and isinstance(data.get("bds"), dict))
    check("含 report_time 的块优先（非简单报告干扰项）",
          data.get("cbm", {}).get("total") == SAMPLE["cbm"]["total"],
          str(data.get("cbm", {}).get("total")))
    check("原文＝抽取的 JSON（```json 包裹，供原始视角）",
          raw.startswith("```json") and '"report_time"' in raw)

    # md 包装兼容路径（旧产物兜底）
    md = tmp / "all_extracted_test.md"
    md.write_text("# 车载日志提取报告\n\n```json\n"
                  + json.dumps(SAMPLE, ensure_ascii=False, indent=2)
                  + "\n```\n", encoding="utf-8")
    data_md, _ = rp.load_report(md)
    check("md 兼容路径可解析（旧产物兜底）",
          data_md.get("report_time") == SAMPLE["report_time"])

    # ── [2] 展平记录 ──
    rows = rp.iter_records(data)
    check("记录数 = cbm + bds", len(rows) == 3, str(len(rows)))
    check("按 query_time 升序（最早在前）",
          [r["query"] for r in rows] == ["一位加班攀比烂", "零跑C11和E0V比", "退出"],
          str([r["query"] for r in rows]))
    check("补 source 标记", {r["source"] for r in rows} == {"cbm", "bds"})
    check("缺失字段补空串（不 KeyError）",
          all(set(rp.RECORD_FIELDS) <= set(r) for r in rows))
    check("bds 记录保留缺失原因",
          any("no_logid" == r["reply_status"] for r in rows))

    # ── [3] KPI ──
    sm = rp.summarize(data)
    check("KPI total/answered/unanswered",
          (sm["total"], sm["answered"], sm["unanswered"]) == (3, 1, 2), str(sm))
    check("KPI 含各段计数",
          sm["sections"]["cbm"]["matched"] == 1
          and sm["sections"]["cbm"]["unmatched_query"] == 1
          and "bds" in sm["sections"], str(sm["sections"]))
    check("KPI 报告时间透传", sm["report_time"] == SAMPLE["report_time"])

    # ── [4] 健壮性（显式报错，不静默）──
    for name, text, kind in (
            ("missing", None, "文件不存在"),
            ("nojson", "# 报告\n没有代码块\n", "无数据块（md）"),
            ("broken", "# 报告\n\n```json\n{ bad json\n```\n", "损坏（md）"),
            ("html_nojson", "<html><body><p>无数据块</p></body></html>", "无数据块（HTML）"),
            ("html_broken", "<html><pre>{ bad json</pre></html>", "损坏（HTML）")):
        p = tmp / f"{name}.txt"
        if text is not None:
            p.write_text(text, encoding="utf-8")
        try:
            rp.parse_report(p)
            ok = False
        except rp.ReportError:
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check(f"ReportError：{kind}", ok)

    # ── [5] 真实产物冒烟 ──
    from cartest.core.parser import offline
    try:
        latest = offline.latest_report()
    except Exception:  # noqa: BLE001
        latest = None
    if latest and Path(str(latest)).is_file():
        try:
            d2 = rp.parse_report(latest)
            rows2 = rp.iter_records(d2)
            check(f"真实报告可解析（{Path(str(latest)).name}）",
                  isinstance(d2, dict) and isinstance(rows2, list),
                  f"rows={len(rows2)}")
            check("真实报告 KPI 可算", rp.summarize(d2).get("total") == len(rows2))
        except Exception as ex:  # noqa: BLE001
            check("真实报告可解析", False, f"{type(ex).__name__}: {ex}")
    else:
        check("真实报告不存在（冒烟跳过）", True)

    # ── [6] 视图接线回归（2026-09-24 全按钮点验发现）──
    # ① 搜索框此前只有 sync_text_value 占位 → 输入后不重建 → "看似有条目却筛不动"
    #    （且行点击的 idx 会在已输入未应用时错位选中）→ 现 on_change 直接重建体
    # ② 下拉选到不可解析的产物（如 role_dialog.md）时，旧实现保留切换后的
    #    value 但内容未换 → 下拉与内容错位；现失败回退到当前已加载报告
    src = (ROOT / "cartest/app/tools/widgets/report_view.py").read_text(encoding="utf-8")
    check("搜索框 on_change 触发实时筛选（轻量重建：只换表+胶囊，不整版）",
          "sync_text_value(e),\n                                 self._rebuild_table_only()" in src)
    check("筛选/搜索走 _rebuild_table_only（不整版重建）",
          "def _rebuild_table_only(self) -> None:" in src
          and "self._mode = mode\n        self._rebuild_table_only()" in src)
    check("下拉解析失败回退当前报告（防错位）",
          "self._report_dd.value = self._path or None" in src)

    # ②运行时验证（无 GUI：控件可独立构造）
    #   2026-10-08：bad 候选改为临时坏 HTML（下拉现只列 all_report_*.html，
    #   不可解析输入 = 无数据块的 HTML，语义等价于原"下拉里选到坏产物"）
    from cartest.app.tools.widgets.report_view import ReportView

    class _Theme:
        def c(self, _k): return "#000000"

    class _Ctx:
        theme = _Theme()

        def set_status(self, *a, **k): pass

    good = offline.latest_report() if offline.available() else None
    bad = tmp / "bad_report.html"
    bad.write_text("<html><body><p>无数据块</p></body></html>", encoding="utf-8")
    if good and Path(str(good)).is_file():
        view = ReportView(_Ctx())
        check("视图可加载真实报告", view.load(good, notify=False) is True)
        view._report_dd.value = str(bad)
        view._on_report_select()                       # 解析失败 → 回退
        check("下拉失败回退到已加载报告", view._report_dd.value == str(good),
              f"{view._report_dd.value}")
    else:
        check("下拉回退运行时验证（无样例产物，跳过）", True)

    # ── [7] 布局回归（2026-10-15 用户裁决：删除右栏原始数据，JSON 收进详情抽屉）──
    # ① 记录表占满全宽（无右栏并排）；② 点行 → 详情抽屉 + 左表行高亮；
    # ③ 抽屉内「原始 JSON」按需展开（点行重置收起态）；④ 全量数据正规入口＝「打开报告」。
    check("记录表占满（无「原始数据（全量）」右栏）",
          'self._pane("记录表"' in src
          and "原始数据（全量）" not in src)
    check("右栏联动代码已移除（_highlight_raw/_toggle_raw/_raw_offset）",
          "_highlight_raw" not in src and "_toggle_raw" not in src
          and "_raw_offset" not in src)
    check("抽屉「原始 JSON」按需展开（点行重置收起态）",
          "展开原始 JSON" in src
          and "self._detail_json = False                  # 换记录时收起上一条的 JSON" in src)

    # 运行时验证用**自建**样例报告（tmp 内），不依赖 reports/ 是否留有真实产物
    v2 = ReportView(_Ctx())
    check("视图可加载自建样例报告", v2.load(html_f, notify=False) is True)
    if v2._rows:
        n = len(v2._rows)
        check("左表行引用＝筛选后行数",
              len(v2._row_refs) == len(v2._filtered()),
              f"{len(v2._row_refs)}/{len(v2._filtered())}")

        v2._set_filter("answered")
        n_ans = sum(1 for r in v2._rows if str(r.get("reply") or "").strip())
        check("「有回复」筛选确实生效（行数＝有回复记录数）",
              len(v2._row_refs) == n_ans, f"{len(v2._row_refs)}/{n_ans}(总{n})")
        v2._set_filter("unanswered")
        n_un = sum(1 for r in v2._rows if not str(r.get("reply") or "").strip())
        check("「无回复」筛选与「有回复」互补",
              len(v2._row_refs) == n_un and n_ans + n_un == n)
        v2._set_filter("answered")        # 回到「有回复」：序号对齐
        f = v2._filtered()
        check("筛选后行引用序号与 _filtered 一一对齐（原始序号）",
              list(v2._row_refs) == [i for i, _ in f],
              f"{list(v2._row_refs)[:4]} vs {[i for i, _ in f][:4]}")

        # 点行 → 抽屉 + 行高亮 + JSON 收起态重置
        v2._detail_json = True            # 先置脏，点行应重置
        if f:
            orig = f[0][0]
            v2._select(orig)
            check("点行 → 选中序号＝该行原始序号", v2._sel_idx == orig)
            check("点行 → 抽屉打开且 JSON 收起态重置",
                  v2._drawer_open is True and v2._detail_json is False)
            check("点行 → 左表行同步高亮（sel_bg 底色）",
                  bool(v2._row_refs[orig].bgcolor))
            check("点行 → 抽屉含「展开原始 JSON」按钮（未展开态）",
                  any("展开原始 JSON" in str(getattr(c, "content", ""))
                      for c in (v2._detail.controls or [])))
            v2._toggle_detail_json(v2._rows[orig], orig)
            check("toggle 后 JSON 展开态置位且抽屉重建",
                  v2._detail_json is True
                  and any("收起原始 JSON" in str(getattr(c, "content", ""))
                          for c in (v2._detail.controls or [])))
        else:
            check("联动运行时验证（筛选后无行，跳过）", True)
    else:
        check("并排联动运行时验证（无样例产物，跳过）", True)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("报告解析检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
