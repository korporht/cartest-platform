"""CarTest CLI — 最小命令集

设计（用户裁决，见 04-P2设计.md）：**只做两个命令**

    cartest run    --cases <file> [--device <sn>] [--resume <模式>] [--keep]
    cartest resume [<run_id>]        # 省略 run_id 时自动找最近一个未完成的运行

不做子命令体系——GUI（P3）会接同一套 core，命令行只服务自动化与无人值守。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .core import config
from .core.judger import default_timeout as judge_default_timeout
from .pipeline.cleanup import KEEP_RECENT, cleanup_runs, describe
from .pipeline.context import RunContext
from .pipeline.runner import resume_pipeline, run_pipeline
from .pipeline.stages import PipelineParams

RESUME_MODES = ["skip_executed", "retry_failed", "redo_all"]


def _bind_device_env(device: str) -> None:
    """把 CLI 选定设备写入进程级 ``ANDROID_SERIAL``（与 GUI ``state.set_device`` 同构）。

    多设备在线时：裸 ``adb`` 命令按该变量路由；设备时钟查询
    （``_query_device_timestamp`` 无环境变量时回落到探测缓存=devices[0]）
    也会落到该设备——否则 CLI 跑 T1MA 时时钟可能查的是 T28（静默错设备）。
    空值不动（尊重用户在 shell 里显式设置的值）。
    """
    dev = (device or "").strip()
    if dev:
        os.environ["ANDROID_SERIAL"] = dev


def _fix_console_encoding() -> None:
    """Windows 控制台默认可能不是 UTF-8，中文会乱码"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _on_stage(stage: str, status: str, detail: str) -> None:
    if stage.startswith("_"):
        print(f"  {detail}")
        return
    if status == "running":
        print(f"[{stage}] 运行中…", end="", flush=True)
    elif status == "done":
        print(f"\r[{stage}] {detail or '完成'}")
    elif status == "failed":
        print(f"\r[{stage}] 失败: {detail}")


def _find_latest_unfinished() -> str | None:
    """最近的未完成运行（供 resume 省略 run_id 时使用）"""
    for r in RunContext.list_runs():          # 新 → 旧
        try:
            ctx = RunContext.open(r["run_id"])
        except Exception:
            continue
        if not ctx.is_complete():
            return r["run_id"]
    return None


def cmd_run(args) -> int:
    case_file = Path(args.cases)
    if not case_file.is_file():
        print(f"错误：用例文件不存在: {case_file}", file=sys.stderr)
        return 2

    _bind_device_env(args.device)   # 设备时钟/裸 adb 全收敛到指定设备

    print(describe())
    params = PipelineParams(
        case_file=str(case_file),
        device=args.device,
        interval=args.interval,
        # None 时显式取档案默认（dataclass 传参不触发 default_factory，须在此处解析）
        timeout=args.timeout if args.timeout is not None else judge_default_timeout(),
        resume_mode=args.resume_mode,
        # --start-index 是 1-based（1=第一条）；校验下限，避免负数进 range()
        start_index=max(0, int(args.start_index) - 1),
    )

    try:
        ctx = run_pipeline(params, keep=args.keep, on_stage=_on_stage)
    except Exception as e:  # noqa: BLE001
        print(f"\n流水线中断: {e}", file=sys.stderr)
        runs = RunContext.list_runs()
        if runs:
            print(f"可用 `cartest resume {runs[0]['run_id']}` 从中断处继续",
                  file=sys.stderr)
        return 1

    print(f"\n完成。运行目录: {ctx.run_dir}")
    print(f"  结果: {ctx.results_json}")
    print(f"  报告: {ctx.report_dir}")
    return 0


def cmd_resume(args) -> int:
    run_id = args.run_id
    if not run_id:
        run_id = _find_latest_unfinished()
        if not run_id:
            print("没有未完成的运行。")
            return 0
        print(f"继续最近的未完成运行: {run_id}")

    # 设备绑定与 run 快照一致（多设备时设备时钟查询不得落到另一台）
    try:
        _bind_device_env(RunContext.open(run_id).device)
    except Exception:  # noqa: BLE001 - 打不开时由 resume_pipeline 给出正式错误
        pass

    try:
        ctx = resume_pipeline(run_id, on_stage=_on_stage)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"\n流水线中断: {e}", file=sys.stderr)
        return 1

    print(f"\n完成。运行目录: {ctx.run_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cartest", description="CarTest 车机测试平台")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑一次完整流水线")
    p_run.add_argument("--cases", required=True, help="用例文件（xlsx/csv/txt/json）")
    p_run.add_argument("--device", default="", help="设备序列号（默认自动检测）")
    p_run.add_argument("--resume", dest="resume_mode", default="skip_executed",
                       choices=RESUME_MODES, help="灌入范围（默认跳过已执行）")
    p_run.add_argument("--start-index", dest="start_index", type=int, default=1,
                       help="起始序号（1=第一条；前段本次不灌，"
                            "常与 --resume redo_all 搭配＝从该条起重灌）")
    p_run.add_argument("--interval", type=float, default=config.DEFAULT_INTERVAL,
                       help=f"灌入间隔秒数（默认 {config.DEFAULT_INTERVAL}）")
    p_run.add_argument("--timeout", type=float, default=None,
                       help="单条判定超时秒数（默认跟随项目档案 judge.timeout，兜底 20s）")
    p_run.add_argument("--keep", action="store_true",
                       help="标记为保留（不参与自动清理）")
    p_run.set_defaults(func=cmd_run)

    p_res = sub.add_parser("resume", help="从中断处继续")
    p_res.add_argument("run_id", nargs="?", default="",
                       help="运行 ID（省略则取最近一个未完成的运行）")
    p_res.set_defaults(func=cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = ["KEEP_RECENT", "cleanup_runs", "main"]
