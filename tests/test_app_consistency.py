# -*- coding: utf-8 -*-
"""P3a 验收 #2 — GUI 与 CLI 两条入口跑同一批用例，判定结果必须一致

为什么应当一致（架构保证）：两条入口最终都调用同一个
``pipeline.runner.run_pipeline``，只是触发方式不同：

    CLI 入口：run_pipeline(params)                    ← cli.py 的调用方式
    GUI 入口：AppState.start_pipeline(params)         ← 后台线程 + state 统一补注入

本测试用实测数据确认这一点，防止将来 GUI 侧悄悄改了参数或时序。

用法:
    python tests/test_app_consistency.py
    pytest tests/test_app_consistency.py
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.app.state import AppState                      # noqa: E402
from cartest.pipeline.runner import run_pipeline            # noqa: E402
from cartest.pipeline.stages import PipelineParams          # noqa: E402
from tools.gui_demo import CASE_FILE, DemoRuntime, make_demo_assets  # noqa: E402

#: 参与对比的字段（覆盖判定结论与判定依据）
COMPARE_KEYS = ("run_id", "query", "status", "domain", "live_status",
                "device_domain", "note", "reason", "query_time")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def base_params() -> PipelineParams:
    """两侧共用的参数（注入项由各自入口负责）"""
    return PipelineParams(case_file=str(CASE_FILE), interval=0.05,
                          timeout=2.0, settle_wait=0.1)


def run_via_cli(root: Path, rt: DemoRuntime):
    """CLI 入口的调用方式：直接 run_pipeline，注入项显式给出"""
    p = base_params()
    p.adb = rt.make_adb()
    p.injector_factory = rt.make_injector
    return run_pipeline(p, root=root)


def run_via_gui(root: Path, rt: DemoRuntime):
    """GUI 入口的调用方式：AppState 驱动，注入项由 state 统一补"""
    st = AppState()
    st.adb_factory = rt.make_adb
    st.injector_factory = rt.make_injector

    os.environ["CARTEST_RUNS_DIR"] = str(root)
    if not st.start_pipeline(base_params()):
        raise RuntimeError("GUI 入口启动失败")

    t0 = time.time()
    while st.running:
        if time.time() - t0 > 90:
            raise TimeoutError("GUI 入口流水线未在预期时间内结束")
        time.sleep(0.05)
    return st.current_run


def slice_results(ctx) -> list[dict]:
    payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
    return [{k: r.get(k) for k in COMPARE_KEYS} for r in payload["results"]]


def summary(ctx) -> tuple:
    payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
    return (payload.get("total"), payload.get("pass"),
            payload.get("fail"), payload.get("conflicts"))


def first_diff(a: list[dict], b: list[dict]) -> str:
    for i, (ra, rb) in enumerate(zip(a, b)):
        if ra != rb:
            keys = [k for k in COMPARE_KEYS if ra.get(k) != rb.get(k)]
            return f"第 {i + 1} 条差异字段 {keys}: CLI={ {k: ra[k] for k in keys} } / GUI={ {k: rb[k] for k in keys} }"
    return f"条数不同: CLI={len(a)} / GUI={len(b)}"


def main() -> int:
    base = Path(mkdtemp(prefix="cartest_consistency_"))
    cli_dir, gui_dir = base / "cli", base / "gui"
    cli_dir.mkdir()
    gui_dir.mkdir()

    # 环境隔离（2026-10-10）：断言以「内置 plt1 默认」为前提——
    # 断开真实 settings.json 与项目档案，免受本机多平台切换（plt2/plt3）影响
    from cartest.core import config_center as _cc
    from cartest.core import project as _pj
    _iso = base / "iso"
    _iso.mkdir()
    _cc.set_settings_file(_iso / "settings.json")
    _pj.set_projects_dir(_iso / "projects")

    # 输出隔离（2026-09-24 全按钮点验发现）：extract 阶段走真实 CbmLogextractor，
    # 不重定向会把夹具日志的报告写进工具真实 reports/（污染用户报告列表）
    os.environ["CARTEST_LOGEXTRACT_OUT"] = str(base / "extract_out")

    print("=" * 70)
    print("P3a 验收 #2 — GUI 与 CLI 判定结果一致性")
    print(f"用例文件: {CASE_FILE.name}")
    print("=" * 70)

    try:
        # 两侧共享同一套素材（脚本 + 设备时钟必须成对同源——query_time 等字段方可比；2026-10-10）
        shared_script, shared_ts = make_demo_assets()
        print("\n[1] CLI 入口（run_pipeline）")
        ctx_cli = run_via_cli(cli_dir, DemoRuntime(script=shared_script, dev_ts=shared_ts))
        print(f"      run_id={ctx_cli.run_id}")

        print("\n[2] GUI 入口（AppState.start_pipeline）")
        ctx_gui = run_via_gui(gui_dir, DemoRuntime(script=shared_script, dev_ts=shared_ts))
        print(f"      run_id={ctx_gui.run_id}")

        print("\n[3] 对比")
        check("CLI 侧七阶段完成", ctx_cli.is_complete(),
              str({s: ctx_cli.stage_status(s) for s in ("report",)}))
        check("GUI 侧七阶段完成", ctx_gui.is_complete(),
              str({s: ctx_gui.stage_status(s) for s in ("report",)}))

        a, b = slice_results(ctx_cli), slice_results(ctx_gui)
        check("用例条数一致（5 条）", len(a) == len(b) == 5, f"CLI={len(a)} GUI={len(b)}")
        check("逐条判定结果完全一致", a == b, "" if a == b else first_diff(a, b))
        check("汇总（总数/通过/失败/需复核）一致",
              summary(ctx_cli) == summary(ctx_gui),
              f"CLI={summary(ctx_cli)} GUI={summary(ctx_gui)}")

        # 抽样确认判定依据（判定差异那条）也被两边一致地记录
        r5a = next((r for r in a if r["run_id"] == "5"), {})
        r5b = next((r for r in b if r["run_id"] == "5"), {})
        check("翻案用例（5）两侧标注一致", r5a.get("note") == r5b.get("note") and r5a.get("note") != "",
              f"{r5a.get('note')!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        os.environ.pop("CARTEST_RUNS_DIR", None)
        os.environ.pop("CARTEST_LOGEXTRACT_OUT", None)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("GUI 与 CLI 判定结果一致性验收通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
