# -*- coding: utf-8 -*-
"""UI-3 验收 — 结果页（R1 表格+抽屉）

覆盖：纯函数助手（加载/筛选/计数/解析运行）；源码断言（T2 追加）。

设计依据：14-UI重构设计.md §6.2。
用法:
    python tests/test_ui_results.py
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
    print("UI-3 验收 — 结果页（R1 表格+抽屉）")
    print("=" * 70)

    # ── [1] 纯函数助手 ──
    from cartest.app.pages.results import (
        filter_rows, status_counts, load_payload, resolve_run_id,
    )

    rows = [
        {"run_id": "1", "query": "最近的新闻", "status": "pass", "domain": "News", "note": ""},
        {"run_id": "2", "query": "播放音乐", "status": "fail", "domain": "", "note": ""},
        {"run_id": "3", "query": "导航去公司", "status": "pass", "domain": "导航",
         "note": "实时判定为失败但设备日志有落域(导航)，以设备日志为准"},
    ]
    got = [r["run_id"] for _, r in filter_rows(rows, "全部", False, "")]
    check("filter_rows：全部", got == ["1", "2", "3"], str(got))
    got = [r["run_id"] for _, r in filter_rows(rows, "fail", False, "")]
    check("filter_rows：按状态", got == ["2"], str(got))
    got = [r["run_id"] for _, r in filter_rows(rows, "全部", True, "")]
    check("filter_rows：仅需复核", got == ["3"], str(got))
    got = [r["run_id"] for _, r in filter_rows(rows, "全部", False, "导航")]
    check("filter_rows：关键字命中 query/落域", got == ["3"], str(got))
    got = [r["run_id"] for _, r in filter_rows(rows, "全部", False, "NEWS")]
    check("filter_rows：关键字大小写不敏感", got == ["1"], str(got))

    c = status_counts(rows)
    check("status_counts：通过/失败/需复核",
          c == {"total": 3, "pass": 2, "fail": 1, "conflict": 1}, str(c))

    # load_payload / resolve_run_id（临时 runs 目录）
    from cartest.pipeline.context import RunContext
    tmp = mkdtemp(prefix="ui3_results_")
    os.environ["CARTEST_RUNS_DIR"] = tmp
    try:
        ctx1 = RunContext.create("cases_demo.json")
        ctx1.results_json.write_text(json.dumps({
            "run_id": ctx1.run_id, "generated_at": "2026-09-16 10:00:00",
            "total": 3, "pass": 2, "fail": 1, "conflicts": 1,
            "results": rows}, ensure_ascii=False), encoding="utf-8")
        ctx2 = RunContext.create("cases_other.json")   # 无 results

        p = load_payload(ctx1.run_id)
        check("load_payload：读取成功", bool(p) and p["total"] == 3)
        check("load_payload：无 results/坏 id → None",
              load_payload(ctx2.run_id) is None and load_payload("no-such-run") is None)

        class _Stub:
            def __init__(self, sel="", cur=None):
                self.selected_run_id = sel
                self.current_run = cur

        check("resolve_run_id：selected 优先",
              resolve_run_id(_Stub(sel=ctx1.run_id)) == ctx1.run_id)
        check("resolve_run_id：current_run 次之",
              resolve_run_id(_Stub(cur=ctx1)) == ctx1.run_id)
        check("resolve_run_id：回退最近含 results 的运行",
              resolve_run_id(_Stub()) == ctx1.run_id)

        empty = mkdtemp(prefix="ui3_empty_")
        os.environ["CARTEST_RUNS_DIR"] = empty
        check("resolve_run_id：无任何运行 → 空串", resolve_run_id(_Stub()) == "")
    finally:
        os.environ.pop("CARTEST_RUNS_DIR", None)

    # ── [2] 源码断言（R1 结构）──
    SRC = (ROOT / "cartest" / "app" / "pages" / "results.py").read_text(encoding="utf-8")
    check("含搜索框", "搜 query" in SRC)
    check("含运行选择器", "run_dd" in SRC)
    check("含状态 chips（组件化）", "components.chip" in SRC)
    check("含导出", "导出" in SRC)
    check("抽屉含实时/设备对照", "实时判定" in SRC and "设备日志" in SRC)
    check("空态引导（去流水线）", "去流水线" in SRC)
    check("抽屉开关（关闭按钮）", "_close_drawer" in SRC)
    check("复制详情走 page.set_clipboard（2026-09-24：直呼 async clipboard.set = 静默空操作）",
          "set_clipboard(text)" in SRC and "clipboard.set(text)" not in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("结果页检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
