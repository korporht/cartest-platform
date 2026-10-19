# -*- coding: utf-8 -*-
"""P3a 验收 — AppState 驱动链路（启动 / 停止 / 续跑；无设备）

替身范围（与 tools/gui_demo.py 同一套装置，单一事实来源）：
  · adb / monitor 为替身，其余（DataSource / Injector / Judger / 七阶段编排）全部真实。

覆盖：
  1. LogBuffer 语义（drain / history / clear）
  2. 启动 → 七阶段跑通 → current_run 挂载
  3. 停止请求在**阶段边界**生效 → run.json 留下可续跑的断点 → 续跑跑完

用法:
    python tests/test_app_state.py
    pytest tests/test_app_state.py
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

from cartest.app.state import AppState, LogBuffer          # noqa: E402
from cartest.pipeline.context import RunContext            # noqa: E402
from cartest.pipeline.stages import PipelineParams         # noqa: E402
from tools.gui_demo import CASE_FILE, DemoRuntime          # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def make_state() -> AppState:
    """带替身注入的 AppState（每个测试独立一份）"""
    rt = DemoRuntime()
    st = AppState()
    st.adb_factory = rt.make_adb
    st.injector_factory = rt.make_injector
    return st


def new_params(interval: float = 0.05, timeout: float = 2.0) -> PipelineParams:
    return PipelineParams(case_file=str(CASE_FILE), interval=interval,
                          timeout=timeout, settle_wait=0.1)


def wait_idle(st: AppState, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while st.running:
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.05)
    return True


def isolated_runs_dir(fn):
    """让每个测试跑在独立的 runs 根目录下（避免运行记录互相干扰）

    同时隔离 extract 报告输出（2026-09-24 全按钮点验发现）：流水线 extract 阶段
    走真实 CbmLogextractor，不重定向会把夹具日志报告写进工具真实 reports/。
    """
    def wrapper(*a, **kw):
        tmp = mkdtemp(prefix=f"cartest_{fn.__name__}_")
        old = os.environ.get("CARTEST_RUNS_DIR")
        old_out = os.environ.get("CARTEST_LOGEXTRACT_OUT")
        os.environ["CARTEST_RUNS_DIR"] = tmp
        os.environ["CARTEST_LOGEXTRACT_OUT"] = os.path.join(tmp, "extract_tool_out")
        try:
            print(f"      运行根目录: {tmp}")
            return fn(*a, **kw)
        finally:
            if old is None:
                os.environ.pop("CARTEST_RUNS_DIR", None)
            else:
                os.environ["CARTEST_RUNS_DIR"] = old
            if old_out is None:
                os.environ.pop("CARTEST_LOGEXTRACT_OUT", None)
            else:
                os.environ["CARTEST_LOGEXTRACT_OUT"] = old_out
            shutil.rmtree(tmp, ignore_errors=True)
    wrapper.__name__ = fn.__name__
    return wrapper


@isolated_runs_dir
def test_log_buffer() -> None:
    print("\n[1] LogBuffer 语义")
    buf = LogBuffer(maxlen=5)
    buf.append("a")
    buf.append("b", "warn")

    got = buf.drain()
    check("drain 返回待消费项（tag, text）", got == [("info", "a"), ("warn", "b")], str(got))
    check("drain 后待消费清空", buf.drain() == [])
    check("history 保留已消费行（供切页回填）",
          buf.history() == [("info", "a"), ("warn", "b")], str(buf.history()))

    buf.append("c")
    check("history 继续累积", len(buf.history()) == 3, str(len(buf.history())))
    buf.clear()
    check("clear 同时清空 history", buf.history() == [], str(buf.history()))


@isolated_runs_dir
def test_start_and_complete() -> None:
    print("\n[2] 启动 → 七阶段跑通（替身）")
    st = make_state()

    check("start_pipeline 接受启动", st.start_pipeline(new_params()) is True)
    check("running 已置位", st.running is True)
    check("运行中重复启动被拒", st.start_pipeline(new_params()) is False)

    check("流水线在超时内结束", wait_idle(st))
    ctx = st.current_run
    check("current_run 已挂上", ctx is not None)

    if ctx:
        status = {s: ctx.stage_status(s) for s in st.stage_status}
        check("七阶段全部完成", ctx.is_complete(), str(status))
        check("results.json 已生成", ctx.results_json.is_file())
        payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        check("判定覆盖 5 条用例", payload.get("total") == 5, str(payload.get("total")))
    check("state.running 已复位", st.running is False)
    check("日志记录了阶段事件", any("[prepare]" in t for _, t in st.log.history()))


@isolated_runs_dir
def test_stop_and_resume() -> None:
    print("\n[3] 中断（阶段边界）→ 续跑")
    st = make_state()
    st.start_pipeline(new_params(timeout=2.0))
    time.sleep(0.4)                     # 让 prepare 过掉、进入 inject
    st.request_stop()

    check("stop_requested 已置位", st.stop_requested is True)
    check("中断后 running 复位", wait_idle(st))
    check("日志含停止提示", any("停止" in t for _, t in st.log.history()))

    ctx = st.current_run
    check("中断后仍能取到该次运行", ctx is not None)
    if not ctx:
        return
    check("中断时未完成", ctx.is_complete() is False)
    # 引擎契约（2026-10-16 停止语义升级）：停止在**用例粒度**生效——中断可落在
    # inject 内部（inject=running 断点）或竞速下 inject 已完成的边界（settle=running）。
    # 两种落点都必须：断点阶段不标 failed、后续阶段未执行、续跑能补完。
    inject_st = ctx.stage_status("inject")
    check("inject 已完成或记断点（不得 failed）",
          inject_st in ("done", "running"), inject_st)
    check("断点之后不再前进（settle 未完成）",
          ctx.stage_status("settle") in ("pending", "running"),
          ctx.stage_status("settle"))
    check("后续阶段未执行", ctx.stage_status("report") == "pending",
          ctx.stage_status("report"))
    check("UI 阶段条与 run.json 一致（inject）",
          st.stage_status["inject"] == inject_st, st.stage_status["inject"])

    run_id = ctx.run_id
    print(f"      → 从 {run_id} 续跑")
    check("resume_pipeline 接受续跑", st.resume_pipeline(run_id) is True)
    check("续跑前已恢复阶段状态（inject 非 failed）",
          st.stage_status["inject"] in ("done", "running"),
          st.stage_status["inject"])
    check("续跑在超时内结束", wait_idle(st))

    final = RunContext.open(run_id, root=Path(os.environ["CARTEST_RUNS_DIR"]))
    check("续跑后七阶段全部完成", final.is_complete(),
          str({s: final.stage_status(s) for s in ("settle", "extract", "judge", "report")}))
    check("续跑后 results.json 已生成", final.results_json.is_file())


def main() -> int:
    print("=" * 70)
    print("P3a 验收 — AppState 驱动链路（无设备）")
    print("=" * 70)
    test_log_buffer()
    test_start_and_complete()
    test_stop_and_resume()

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("AppState 驱动链路验收通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
