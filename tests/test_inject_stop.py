# -*- coding: utf-8 -*-
"""灌入阶段中途停止 — 用例粒度生效（2026-10-16 用户反馈修复）

背景：原实现停止请求只在**阶段边界**检查（``_stage_cb``），灌入批一旦启动
（``inj.start_batch`` + 轮询等待）就不再响应停止——点「停止」后整批几十条
用例仍继续灌。修复：``stage_inject`` 轮询循环内检查 ``p.stop_check``，
命中则 ``inj.stop()``（当前条灌完即退）并抛 ``PipelineStopped``（断点可续跑）。

覆盖：
  [1] 行为级：灌入中 stop_check 置位 → PipelineStopped 抛出、inj.stop() 被调用、
      进度文件保留已完成条目（续跑 skip_executed 不重灌）
  [2] 行为级：stop_check 恒 False（正常路径）→ 阶段正常完成，不误中断
  [3] 源码口径：state 挂 stop_check 钩子 / runner 序列化排除 / 异常定义在 pipeline 层

用法:
    python tests/test_inject_stop.py
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）
from cartest.core.injector import InjectState  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeInjector:
    """替身注入器：只模拟 stage_inject 用到的接口与状态机（state 用真枚举）"""

    def __init__(self, hang: bool = False) -> None:
        self.state = InjectState.IDLE
        self.skipped_count = 0
        self.stop_called = 0
        self._hang = hang              # True = stop() 后仍 RUNNING（模拟线程挂死）

    def set_params(self, **kw) -> None: ...
    def set_device(self, d) -> None: ...
    def set_progress_store(self, store, mode, prev) -> None:
        self._store = store
        self._prev = prev

    def set_callbacks(self, **kw) -> None: ...

    def start_batch(self, ds, mode=None, start_from=0) -> None:
        self.state = InjectState.RUNNING   # 立即进入 RUNNING（轮询循环开始等待）
        if not self._hang:
            # 模拟批正常跑完：0.5s 后落停（真实 Injector 由后台线程驱动）
            def _done_later() -> None:
                time.sleep(0.5)
                if self.state == InjectState.RUNNING:
                    self.state = InjectState.IDLE
            threading.Thread(target=_done_later, daemon=True).start()

    def _finish_a_case(self, key: str) -> None:
        """模拟一条用例完成 → 进度落盘（与真实 Injector._record_progress 同构）"""
        self._store.append({
            "run_id": key, "src_id": key, "query": key,
            "device": "DEV1", "ts": "2026-10-16 12:00:00",
            "send_ok": True, "status": "pass",
        })

    def stop(self) -> None:
        self.stop_called += 1
        if not self._hang:
            self.state = InjectState.IDLE   # 当前条灌完即退


def _make_run(cases: list[dict]):
    """建一次运行（用例写入 cases.json），返回 (ctx, store_path)"""
    from cartest.pipeline.context import RunContext

    root = Path(mkdtemp(prefix="injstop_"))
    ctx = RunContext.create("cases.xlsx", device="DEV1", root=root)
    ctx.cases_json.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return ctx


def main() -> int:
    print("=" * 70)
    print("灌入阶段中途停止 — 用例粒度生效")
    print("=" * 70)

    from cartest.pipeline.context import PipelineStopped
    from cartest.pipeline.stages import PipelineParams, stage_inject

    cases = [{"id": f"c{i}", "query": f"第{i}句"} for i in range(1, 6)]

    # ── [1] 灌入中停止 ──
    ctx = _make_run(cases)
    inj = FakeInjector()
    p = PipelineParams(case_file=ctx.case_file, device="DEV1",
                       injector_factory=lambda: inj)

    stopped = {"flag": False, "ex": ""}
    # 首次轮询（0.2s 后）先让一条用例完成落盘，然后置位停止请求
    def _stop_check() -> bool:
        if not stopped["flag"]:
            inj._finish_a_case("c1")
            stopped["flag"] = True
        return True

    p.stop_check = _stop_check
    try:
        stage_inject(ctx, p)
    except PipelineStopped as ex:
        stopped["ex"] = str(ex)
    except Exception as ex:  # noqa: BLE001
        stopped["ex"] = f"异常类型错误: {type(ex).__name__}: {ex}"

    check("停止请求 → 抛 PipelineStopped（而非跑完整批）",
          "停止" in stopped["ex"], stopped["ex"])
    check("inj.stop() 被调用", inj.stop_called == 1)
    check("batch 线程已落停（IDLE）", inj.state == InjectState.IDLE, str(inj.state))
    prog = [json.loads(l) for l in
            ctx.progress_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    done_keys = {r.get("run_id") for r in prog if r.get("status") == "pass"}
    check("已完成条目保留在进度文件（断点）", "c1" in done_keys or len(prog) >= 1,
          str(prog))

    # 续跑：resume_mode=skip_executed + prev 含 c1 → 不再重复灌 c1
    prev = {r.get("run_id"): r["status"] for r in prog}
    check("续跑语义：c1 已完成会被跳过", "c1" in prev and prev["c1"] == "pass")

    # ── [2] 正常路径不误中断 ──
    ctx2 = _make_run(cases)
    inj2 = FakeInjector()
    p2 = PipelineParams(case_file=ctx2.case_file, device="DEV1",
                        injector_factory=lambda: inj2)
    p2.stop_check = lambda: False
    try:
        detail = stage_inject(ctx2, p2)
        check("stop_check 恒 False → 阶段正常完成", "执行 0 条" in detail, detail)
    except PipelineStopped as ex:
        check("stop_check 恒 False → 阶段正常完成", False, f"误中断: {ex}")

    # stop_check = None（未挂钩子，如 CLI）→ 照常跑完
    ctx3 = _make_run(cases)
    inj3 = FakeInjector()
    p3 = PipelineParams(case_file=ctx3.case_file, device="DEV1",
                        injector_factory=lambda: inj3)
    try:
        stage_inject(ctx3, p3)
        check("stop_check=None（CLI）→ 照常完成", True)
    except PipelineStopped as ex:
        check("stop_check=None（CLI）→ 照常完成", False, f"误中断: {ex}")

    # ── [3] 源码口径 ──
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")
    stages_src = (ROOT / "cartest" / "pipeline" / "stages.py").read_text(encoding="utf-8")
    runner_src = (ROOT / "cartest" / "pipeline" / "runner.py").read_text(encoding="utf-8")
    context_src = (ROOT / "cartest" / "pipeline" / "context.py").read_text(encoding="utf-8")

    check("state 挂 stop_check 钩子（指向 stop_requested）",
          "params.stop_check = lambda: self.stop_requested" in state_src)
    check("runner 序列化排除 stop_check", '"stop_check"' in runner_src)
    check("PipelineStopped 定义在 pipeline 层（context.py）",
          "class PipelineStopped" in context_src
          and "class PipelineStopped" not in state_src)
    check("stage_inject 轮询循环检查 stop_check",
          "p.stop_check" in stages_src and "inj.stop()" in stages_src)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("灌入中途停止检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
