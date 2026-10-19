# -*- coding: utf-8 -*-
"""40 号批 5 验收 — 流水线暂停/继续（与停止同级；暂停不写 running）

覆盖：
  [1] 源码口径：runner 主循环阶段边界插 gate.wait()；state 有 pause/continue
      且 request_stop 释放暂停门；pipeline 页三平级按钮（暂停/继续互斥）
  [2] runner 级：暂停中 run.json **不写 running**（无断点残留）；继续后跑完
  [3] runner 级：暂停中停止 → 醒来到边界抛 PipelineStopped，中断阶段记
      running＝断点，next_stage() 视为未完成（可续跑）
  [4] state 级：pause/continue 状态机 + request_stop 放行 + 暂停态禁止切设备

用法:
    python tests/test_pipeline_pause.py
"""
import json
import sys
import threading
import time
from pathlib import Path

from _tmpdir import mkdtemp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def _read_run(root: Path) -> dict:
    runs = sorted(root.glob("*/run.json"))
    assert runs, "run.json 未生成"
    return json.loads(runs[-1].read_text(encoding="utf-8"))


def main() -> int:
    print("=" * 70)
    print("40 号批 5 — 流水线暂停/继续")
    print("=" * 70)

    # ── [1] 源码口径 ──
    runner_src = (ROOT / "cartest" / "pipeline" / "runner.py").read_text(encoding="utf-8")
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")

    check("runner 阶段边界插暂停门", "gate.wait()" in runner_src
          and "def _execute_stages" in runner_src and "gate=None" in runner_src)
    check("state 有暂停/继续方法", "def pause_pipeline" in state_src
          and "def continue_pipeline" in state_src)
    check("request_stop 释放暂停门（暂停中可停止）",
          "self.pause_gate.resume()" in
          state_src.split("def request_stop", 1)[1].split("\n    def ", 1)[0])
    check("流水线结束放行排队任务", state_src.count("device_queue.drain()") >= 2)
    check("启动/续跑拒绝非空队列", state_src.count("device_queue.pending()") >= 3)
    # 2026-10-19：「测试流程」页删除 → 页内暂停/继续按钮断言移除；
    # 暂停/继续能力仍在 core（runner gate）+ state（pause_pipeline/continue_pipeline），
    # 由下方行为用例继续守护。

    # ── [2]/[3] runner 级行为 ──
    from cartest.pipeline import runner as runner_mod
    from cartest.pipeline.context import STAGES
    from cartest.pipeline.stages import PipelineParams
    from cartest.app.tasks import PauseGate

    tmp = Path(mkdtemp(prefix="catest_pause_"))   # GUARD：走 _tmpdir（进程退出自动回收）

    params = PipelineParams(case_file="cases.json", device="DEV1", settle_wait=0.0)

    def _fake_stages() -> None:
        for s in STAGES:
            runner_mod.STAGE_FUNCS[s] = (lambda ctx, p, _s=s: "ok")

    saved = dict(runner_mod.STAGE_FUNCS)
    _fake_stages()
    try:
        # [2] 暂停中不写 running → 继续 → 跑完
        gate = PauseGate()
        gate.pause()
        box: dict = {}
        th = threading.Thread(
            target=lambda: box.setdefault("ctx", runner_mod.run_pipeline(
                params, root=tmp, do_cleanup=False, gate=gate)),
            daemon=True)
        th.start()
        time.sleep(0.3)
        data = _read_run(tmp)
        check("暂停中：run.json 无 running 断点",
              not any(v.get("status") == "running" for v in data.get("stages", {}).values()),
              str(data.get("stages")))
        gate.resume()
        th.join(timeout=10)
        ctx = box.get("ctx")
        check("继续后跑完（全部 done）",
              ctx is not None and ctx.is_complete())

        # [3] 暂停中停止 → 边界抛 PipelineStopped → 断点可续跑
        tmp2 = Path(mkdtemp(prefix="catest_pause2_"))
        gate2 = PauseGate()
        gate2.pause()
        stop = {"on": False}
        events: list[tuple] = []

        def _cb(stage: str, status: str, detail: str) -> None:
            events.append((stage, status))
            if status == "running" and stop["on"]:
                raise RuntimeError("PIPELINE_STOPPED")   # 模拟 state._stage_cb 的抛出

        box2: dict = {}
        th2 = threading.Thread(
            target=lambda: box2.setdefault("err", _run_catch(
                lambda: runner_mod.run_pipeline(params, root=tmp2, do_cleanup=False,
                                                gate=gate2, on_stage=_cb))),
            daemon=True)
        th2.start()
        time.sleep(0.2)
        stop["on"] = True
        gate2.resume()                        # request_stop 的放行等价物
        th2.join(timeout=10)
        check("暂停中停止：边界抛出", isinstance(box2.get("err"), RuntimeError))
        data2 = _read_run(tmp2)
        run_stages = [s for s, v in data2.get("stages", {}).items()
                      if v.get("status") == "running"]
        check("停止：中断阶段记 running（断点）", len(run_stages) == 1, str(run_stages))
        from cartest.pipeline.context import RunContext
        ctx2 = RunContext.open(data2["run_id"], root=tmp2)
        nxt = ctx2.next_stage()
        check("断点被视为未完成（可续跑）", nxt == run_stages[0], str(nxt))
    finally:
        runner_mod.STAGE_FUNCS.clear()
        runner_mod.STAGE_FUNCS.update(saved)

    # ── [4] state 级状态机 ──
    ok4, err4 = True, ""
    try:
        from cartest.app.state import AppState

        st = AppState()
        check("未运行时暂停失败", st.pause_pipeline() is False)
        st.running = True
        check("暂停生效", st.pause_pipeline() is True and st.pause_gate.paused)
        ok_sw, reason_sw = st.can_switch_device()
        check("暂停态禁止切设备", ok_sw is False, reason_sw)
        st.stop_requested = False
        st.request_stop()
        check("暂停中停止 → 放行 + 置位",
              st.stop_requested is True and st.pause_gate.paused is False)
        check("继续（未暂停）失败", st.continue_pipeline() is False)
        st.pause_gate.pause()
        check("继续生效", st.continue_pipeline() is True
              and st.pause_gate.paused is False)
    except Exception as ex:  # noqa: BLE001
        ok4, err4 = False, f"{type(ex).__name__}: {ex}"
    check("state 暂停/继续状态机", ok4, err4)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("流水线暂停/继续检查通过")
    print("=" * 70)
    return 0


def _run_catch(fn):
    try:
        fn()
        return None
    except Exception as ex:  # noqa: BLE001
        return ex


if __name__ == "__main__":
    sys.exit(main())
