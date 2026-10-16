# -*- coding: utf-8 -*-
"""UI-3 验收 — 历史页（H1 运行管理表）

覆盖：运行行汇总 load_run_rows / 筛选 filter_runs；源码断言（T4 追加）。

设计依据：14-UI重构设计.md §6.3。
用法:
    python tests/test_ui_history.py
"""
import json
import os
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


def main() -> int:
    print("=" * 70)
    print("UI-3 验收 — 历史页（H1 运行管理表）")
    print("=" * 70)

    # ── [1] 助手 ──
    from cartest.app.pages.history import load_run_rows, filter_runs
    from cartest.pipeline.context import RunContext

    tmp = mkdtemp(prefix="ui3_hist_")
    os.environ["CARTEST_RUNS_DIR"] = tmp
    try:
        ctx1 = RunContext.create("cases_alpha.json", keep=True)
        ctx1.cases_json.write_text(
            json.dumps([{"id": str(i)} for i in range(7)]), encoding="utf-8")
        ctx1.results_json.write_text(json.dumps({
            "run_id": ctx1.run_id, "total": 7, "pass": 6, "fail": 1,
            "conflicts": 1, "results": []}), encoding="utf-8")

        ctx2 = RunContext.create("cases_beta.json")   # 中断（无 results）

        ctx3 = RunContext.create("cases_gamma.json")
        ctx3.results_json.write_text(json.dumps({
            "run_id": ctx3.run_id, "total": 3, "pass": 3, "fail": 0,
            "conflicts": 0, "results": []}), encoding="utf-8")

        from cartest.pipeline.context import STAGES
        for s in STAGES:          # 标记 ctx1/ctx3 为完成（阶段全 done）
            ctx1.finish_stage(s, "ok")
            ctx3.finish_stage(s, "ok")

        rows = load_run_rows()
        check("load_run_rows 返回 3 行", len(rows) == 3, str(len(rows)))
        by_id = {r["run_id"]: r for r in rows}
        r1 = by_id[ctx1.run_id]
        check("完成行：keep/计数/用例数",
              r1["keep"] is True and r1["pass"] == 6 and r1["fail"] == 1
              and r1["cases"] == 7,
              str({k: r1[k] for k in ("keep", "pass", "fail", "cases")}))
        check("完成行：complete=True 且 stages 7 项",
              r1["complete"] is True and len(r1["stages"]) == 7, str(r1["stages"]))
        r2 = by_id[ctx2.run_id]
        check("中断行：complete=False 且计数 None",
              r2["complete"] is False and r2["pass"] is None and r2["fail"] is None)
        check("名称已去日期前缀", r1["name"] == "cases_alpha", r1["name"])

        f = filter_runs(rows, "全部", "")
        check("filter_runs：全部", len(f) == 3, str(len(f)))
        f = filter_runs(rows, "keep", "")
        check("filter_runs：keep", len(f) == 1 and f[0]["run_id"] == ctx1.run_id)
        f = filter_runs(rows, "中断", "")
        check("filter_runs：中断", len(f) == 1 and f[0]["run_id"] == ctx2.run_id)
        f = filter_runs(rows, "全部", "BETA")
        check("filter_runs：关键字大小写不敏感",
              len(f) == 1 and f[0]["run_id"] == ctx2.run_id)
    finally:
        os.environ.pop("CARTEST_RUNS_DIR", None)

    # ── [2] 源码断言（H1 结构）──
    SRC = (ROOT / "cartest" / "app" / "pages" / "history.py").read_text(encoding="utf-8")
    check("含阶段微条", "阶段微条" in SRC and "_segments" in SRC)
    check("含行操作（结果/续跑/保留/删除）",
          all(k in SRC for k in ("结果", "续跑", "保留", "删除")))
    check("删除/清理走确认弹窗", "AlertDialog" in SRC and "确认删除" in SRC)
    check("清理含预览（plan_cleanup）", "plan_cleanup" in SRC)
    check("空态引导", "暂无运行" in SRC)
    check("结果动作联动（select_run + nav_view）",
          "select_run" in SRC and "nav_view" in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("历史页检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
