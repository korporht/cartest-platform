"""CarTest pipeline — 运行上下文与状态机

一次测试运行 = 一个 ``runs/<run_id>/`` 目录 + 一份 ``run.json`` 状态。

设计约束（见 04-P2设计.md §4.1 / §4.2）：
  - ``run.json`` **只在阶段边界写**（进入阶段写 running，出阶段写 done/failed）
  - 阶段本身幂等；阶段内部状态由 ``core.progress.ProgressStore`` 兜底
  - ``device_logs/`` 是原始证据，任何阶段都不得修改

与 ProgressStore 的分工：
  - ``ProgressStore``  → 哪些**用例**跑过（逐条 fsync，抗崩溃）
  - ``run.json``       → 流水线走到**哪一步**（阶段边界，抗中断）
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core import config

#: 流水线阶段（顺序即执行顺序）
STAGES: list[str] = [
    "prepare",    # 加载用例 + 前置检查
    "inject",     # 跑用例（含断点续跑）
    "settle",     # 触发落盘 + 拉取设备日志
    "extract",    # 结构化提取（CbmLogextractor）
    "judge",      # 判定（设备日志为准）
    "fillback",   # 回填 xlsx
    "report",     # 出报告
]

#: 阶段状态取值
PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"

#: 阶段显示中文化（40 号 §八）：**只改显示层**——STAGES 本体、run.json 的 key、
#: runs/<id>/ 产物目录一律不变（否则破坏历史运行与断点续跑）
STAGE_LABELS: dict[str, str] = {
    "prepare": "准备",
    "inject": "灌入",
    "settle": "落盘取证",
    "extract": "提取",
    "judge": "判定",
    "fillback": "回填",
    "report": "报告",
}


def stage_label(s: str) -> str:
    """阶段 key → 中文显示名（未知 key 原样返回，兼容历史/自定义阶段）"""
    return STAGE_LABELS.get(s, s)


class PipelineStopped(Exception):
    """用户请求停止（编排层控制流，非错误）。

    两处抛出：阶段边界（runner）与**灌入轮询循环内**（stages.stage_inject，
    用例粒度生效——不必等整批跑完）。抛出后 ``run.json`` 保留中断阶段为
    running，可随时从历史页续跑。
    """

_SAFE_RE = re.compile(r'[\\/:*?"<>|\s]+')


def runs_dir() -> Path:
    """所有运行的根目录（可用环境变量 CARTEST_RUNS_DIR 覆盖，便于测试隔离）"""
    import os
    override = os.environ.get("CARTEST_RUNS_DIR")
    return Path(override) if override else config.RUNS_DIR


def make_run_id(case_file: str | Path, now: float | None = None) -> str:
    """生成 run_id：``YYYY-MM-DD_HHMMSS_<用例名简述>``

    精确到秒——分钟级精度会让"同一分钟内连跑两次"撞 ID。
    """
    ts = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(now))
    stem = _SAFE_RE.sub("_", Path(case_file).stem).strip("_")
    if len(stem) > 30:
        stem = stem[:30]
    return f"{ts}_{stem}" if stem else ts


@dataclass
class StageState:
    """单个阶段的状态"""
    status: str = PENDING
    started: str = ""
    finished: str = ""
    detail: str = ""
    error: str = ""


@dataclass
class RunContext:
    """一次运行的上下文：目录、元信息、阶段状态"""

    run_id: str
    run_dir: Path
    case_file: str = ""
    device: str = ""
    keep: bool = False
    created_at: str = ""
    stages: dict[str, StageState] = field(default_factory=dict)
    #: 本次运行的参数快照（断点续跑时用于重建 PipelineParams）
    params: dict = field(default_factory=dict)

    # ── 构造 ──────────────────────────────
    @classmethod
    def create(cls, case_file: str | Path, device: str = "", keep: bool = False,
               root: Path | None = None, params: dict | None = None) -> "RunContext":
        """新建一次运行。

        总是创建**新的**运行目录——若同 ID 已存在（同一秒内重复启动），
        自动追加序号（``_2`` / ``_3``…）。断点续跑请用 :meth:`open` + runner.resume。
        """
        root = root or runs_dir()
        base_id = make_run_id(case_file)
        run_id, run_dir, suffix = base_id, root / base_id, 1
        while (run_dir / "run.json").is_file():
            suffix += 1
            run_id = f"{base_id}_{suffix}"
            run_dir = root / run_id

        ctx = cls(
            run_id=run_id,
            run_dir=run_dir,
            case_file=str(case_file),
            device=device,
            keep=bool(keep),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            stages={s: StageState() for s in STAGES},
            params=dict(params or {}),
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx.save()
        return ctx

    @classmethod
    def open(cls, run_id: str, root: Path | None = None) -> "RunContext":
        """打开已有运行（断点续跑用）"""
        root = root or runs_dir()
        run_dir = root / run_id
        meta_file = run_dir / "run.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"运行不存在或缺少 run.json: {run_dir}")

        data = json.loads(meta_file.read_text(encoding="utf-8"))
        ctx = cls(
            run_id=data["run_id"],
            run_dir=run_dir,
            case_file=data.get("case_file", ""),
            device=data.get("device", ""),
            keep=bool(data.get("keep", False)),
            created_at=data.get("created_at", ""),
            stages={},
            params=dict(data.get("params", {})),
        )
        raw_stages = data.get("stages", {})
        for s in STAGES:
            st = raw_stages.get(s, {})
            ctx.stages[s] = StageState(
                status=st.get("status", PENDING),
                started=st.get("started", ""),
                finished=st.get("finished", ""),
                detail=st.get("detail", ""),
                error=st.get("error", ""),
            )
        return ctx

    @classmethod
    def list_runs(cls, root: Path | None = None) -> list[dict]:
        """列出所有运行（按创建时间倒序），供清理与查看用"""
        root = root or runs_dir()
        if not root.is_dir():
            return []
        out = []
        for d in root.iterdir():
            meta = d / "run.json"
            if not (d.is_dir() and meta.is_file()):
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "run_id": data.get("run_id", d.name),
                "run_dir": d,
                "created_at": data.get("created_at", ""),
                "keep": bool(data.get("keep", False)),
                "size": _dir_size(d),
            })
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return out

    # ── 产物路径 ──────────────────────────
    @property
    def meta_file(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def cases_json(self) -> Path:
        return self.run_dir / "cases.json"

    @property
    def progress_jsonl(self) -> Path:
        return self.run_dir / "progress.jsonl"

    @property
    def device_logs_dir(self) -> Path:
        return self.run_dir / "device_logs"

    @property
    def extract_dir(self) -> Path:
        return self.run_dir / "extract"

    @property
    def results_json(self) -> Path:
        return self.run_dir / "results.json"

    @property
    def report_dir(self) -> Path:
        return self.run_dir / "report"

    # ── 状态机 ────────────────────────────
    def stage(self, name: str) -> StageState:
        if name not in self.stages:
            self.stages[name] = StageState()
        return self.stages[name]

    def begin_stage(self, name: str, detail: str = "") -> None:
        st = self.stage(name)
        st.status = RUNNING
        st.started = time.strftime("%Y-%m-%d %H:%M:%S")
        st.finished = ""
        st.error = ""
        if detail:
            st.detail = detail
        self.save()

    def finish_stage(self, name: str, detail: str = "") -> None:
        st = self.stage(name)
        st.status = DONE
        st.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        if detail:
            st.detail = detail
        self.save()

    def fail_stage(self, name: str, error: str) -> None:
        st = self.stage(name)
        st.status = FAILED
        st.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        st.error = str(error)[:500]
        self.save()

    def skip_stage(self, name: str, reason: str = "") -> None:
        st = self.stage(name)
        st.status = SKIPPED
        st.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        st.detail = reason
        self.save()

    def stage_status(self, name: str) -> str:
        return self.stage(name).status

    def last_done_stage(self) -> str | None:
        """最后一个已完成的阶段（按流水线顺序）"""
        last = None
        for s in STAGES:
            if self.stage(s).status in (DONE, SKIPPED):
                last = s
            else:
                break
        return last

    def next_stage(self) -> str | None:
        """下一个待执行的阶段：最后一个 done/skipped 的下一阶段。

        注：``running`` 视为**未完成**（可能是中断残留），需重跑该阶段——
        阶段幂等，重跑无副作用。
        """
        last = self.last_done_stage()
        if last is None:
            return STAGES[0]
        idx = STAGES.index(last)
        return STAGES[idx + 1] if idx + 1 < len(STAGES) else None

    def is_complete(self) -> bool:
        return all(self.stage(s).status in (DONE, SKIPPED) for s in STAGES)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "完整": self.is_complete(),
            "阶段": {s: self.stage(s).status for s in STAGES},
        }

    # ── 持久化 ────────────────────────────
    def save(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "case_file": self.case_file,
            "device": self.device,
            "keep": self.keep,
            "params": self.params,
            "stages": {s: asdict(self.stages[s]) for s in STAGES if s in self.stages},
        }
        self.meta_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _dir_size(path: Path) -> int:
    """目录总字节数（统计失败按 0 计）"""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
