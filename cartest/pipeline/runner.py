"""CarTest pipeline — 编排与断点恢复

职责：
  · 按序执行阶段，每阶段在 ``run.json`` 边界写状态
  · 某阶段失败即**停止**（不自动重试——失败原因需要人看）
  · 从断点恢复：找最后一个 done/skipped 的下一阶段继续
  · 新运行开始时清理旧的运行目录

用法::

    ctx = run_pipeline(params)              # 从头跑
    ctx = resume_pipeline("2026-09-15_1430_xxx")   # 从中断处继续
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Callable

from .cleanup import cleanup_runs
from .context import PipelineStopped, STAGES, RunContext, stage_label
from .stages import STAGE_FUNCS, PipelineParams

#: 阶段事件回调：(stage_name, status, detail)
StageCb = Callable[[str, str, str], None] | None

#: 不参与序列化的参数（注入对象）
_NON_SERIALIZABLE = {"adb", "injector_factory", "on_log", "stop_check"}
#: 可直接写入 JSON 的类型
_SIMPLE_TYPES = (str, int, float, bool)


def params_to_dict(p: PipelineParams) -> dict:
    """提取可序列化参数（供 run.json 保存，断点续跑时重建）。

    **注意**：不能用 ``dataclasses.asdict()``——它会 deepcopy 全部字段，
    遇到注入对象（含线程锁/不可拷贝对象）会直接抛 TypeError。
    这里逐字段取值并过滤类型，不做拷贝。
    """
    out: dict = {}
    for f in fields(PipelineParams):
        if f.name in _NON_SERIALIZABLE:
            continue
        v = getattr(p, f.name)
        if v is None or isinstance(v, _SIMPLE_TYPES):
            out[f.name] = v
    return out


def params_from_dict(d: dict, **overrides) -> PipelineParams:
    """从 run.json 的参数快照重建（只取已知字段，忽略历史遗留键）"""
    known = {f.name for f in fields(PipelineParams)}
    data = {k: v for k, v in (d or {}).items() if k in known}
    data.update(overrides)
    return PipelineParams(**data)


def run_pipeline(params: PipelineParams, *, keep: bool = False,
                 root: Path | None = None, do_cleanup: bool = True,
                 on_stage: StageCb = None, gate=None) -> RunContext:
    """跑一次完整流水线。

    Args:
        params:     流水线参数
        keep:       标记该运行为"保留"（不参与自动清理）
        root:       运行根目录（默认 ``runs/``；测试可覆盖）
        do_cleanup: 开始前是否清理旧运行
        on_stage:   阶段事件回调 ``(stage, status, detail)``
        gate:       暂停门（40 号 §五）：``wait()`` 在阶段边界阻塞；
                    暂停**不写 run.json**（不产生断点）

    Raises:
        阶段失败时抛出原异常（状态已写入 run.json，可用 resume_pipeline 续跑）
    """
    if do_cleanup:
        deleted = cleanup_runs(root)
        if deleted and on_stage:
            on_stage("_cleanup", "done", f"清理 {len(deleted)} 次旧运行")

    ctx = RunContext.create(params.case_file, params.device, keep=keep,
                            root=root, params=params_to_dict(params))
    _execute_stages(ctx, params, start_idx=0, on_stage=on_stage, gate=gate)
    return ctx


def resume_pipeline(run_id: str, params: PipelineParams | None = None,
                    *, root: Path | None = None,
                    on_stage: StageCb = None, gate=None) -> RunContext:
    """从断点继续。

    Args:
        params: 为 None 时从 run.json 的参数快照重建（含注入对象需显式传入）
    """
    ctx = RunContext.open(run_id, root)

    if ctx.is_complete():
        if on_stage:
            on_stage("_resume", "done", "该运行已完成，无需续跑")
        return ctx

    if params is None:
        params = params_from_dict(ctx.params)

    start = ctx.next_stage()
    if start is None:
        return ctx

    if on_stage:
        on_stage("_resume", "running", f"从 {stage_label(start)} 阶段继续")
    _execute_stages(ctx, params, start_idx=STAGES.index(start),
                    on_stage=on_stage, gate=gate)
    return ctx


def _execute_stages(ctx: RunContext, params: PipelineParams, start_idx: int,
                    on_stage: StageCb, gate=None) -> None:
    """从 start_idx 顺序执行到结束；失败即停并保留状态

    40 号 §五：``gate``（暂停门）在每个阶段边界 ``wait()``——暂停时线程挂起，
    **不 advance、不写盘**（run.json 保持原状，与「停止＝断点」语义区分）。
    """
    for stage in STAGES[start_idx:]:
        if gate is not None:
            try:
                gate.wait()
            except Exception:  # noqa: BLE001 — 暂停门故障不阻断流水线
                pass
        ctx.begin_stage(stage)

        # 本次运行以 run.json 里的参数为准（resume 场景下 params 可能被覆盖）
        if stage == "prepare" and params.case_file:
            ctx.case_file = params.case_file
            ctx.params = params_to_dict(params)
        if params.device:
            ctx.device = params.device
        ctx.save()

        if on_stage:
            on_stage(stage, "running", "")

        try:
            detail = STAGE_FUNCS[stage](ctx, params)
        except PipelineStopped:
            # 中断＝断点：阶段保持 running（begin_stage 已写盘），续跑从本阶段重入
            raise
        except Exception as e:  # noqa: BLE001 — 失败原因需保留给人看
            ctx.fail_stage(stage, str(e))
            if on_stage:
                on_stage(stage, "failed", str(e))
            raise

        ctx.finish_stage(stage, detail)
        if on_stage:
            on_stage(stage, "done", detail)
