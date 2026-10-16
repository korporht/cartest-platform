"""CarTest pipeline — 流水线编排

模块：
  context.py  RunContext + run.json 状态机
  cleanup.py  运行目录清理
  stages.py   7 个阶段实现
  runner.py   编排与断点恢复
"""
from . import cleanup, context, runner, stages
from .context import STAGES, RunContext
from .runner import params_from_dict, params_to_dict, resume_pipeline, run_pipeline
from .stages import STAGE_FUNCS, PipelineParams

__all__ = [
    "STAGES", "RunContext", "STAGE_FUNCS", "PipelineParams",
    "cleanup", "context", "params_from_dict", "params_to_dict",
    "resume_pipeline", "run_pipeline", "runner", "stages",
]
