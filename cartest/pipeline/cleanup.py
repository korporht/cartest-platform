"""CarTest pipeline — 运行目录清理

规则优先级（见 04-P2设计.md §4.3）：

  1. ``keep=True`` 的运行 —— **永不自动删除**
  2. 最近 ``keep_recent`` 次运行 —— 保留
  3. 其余（超出数量的旧运行）—— 删除

体积上限 ``max_bytes`` 用于**报告**：若删完候选仍超限，说明最近 N 次本身就过大，
此时标记 ``over_limit`` 并提示用户，但**不因此突破规则 1/2**。

执行时机：每次新运行开始时（见 runner）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .context import RunContext, runs_dir

#: 默认保留最近 N 次运行
KEEP_RECENT = 10
#: 默认总量上限（字节）
MAX_TOTAL_BYTES = 5 * 1024 ** 3


def plan_cleanup(root: Path | None = None, keep_recent: int = KEEP_RECENT,
                 max_bytes: int = MAX_TOTAL_BYTES) -> tuple[list[dict], dict]:
    """计算清理计划（不实际删除）。

    Returns:
        ``(待删除的 run 列表（旧→新）, 统计信息)``
    """
    runs = RunContext.list_runs(root)          # 新 → 旧
    if not runs:
        return [], {"total": 0, "total_bytes": 0, "protected": 0,
                    "to_delete": 0, "after_bytes": 0, "over_limit": False}

    # 规则 1 + 2：受保护的 run_id
    protected_ids = {r["run_id"] for r in runs[:max(keep_recent, 0)]}
    protected_ids |= {r["run_id"] for r in runs if r["keep"]}

    # 规则 3：候选 = 其余（旧 → 新，优先删旧）
    candidates = [r for r in runs if r["run_id"] not in protected_ids]
    candidates.reverse()

    total_bytes = sum(r["size"] for r in runs)
    after_bytes = total_bytes - sum(r["size"] for r in candidates)

    stats = {
        "total": len(runs),
        "total_bytes": total_bytes,
        "protected": len(protected_ids),
        "to_delete": len(candidates),
        "after_bytes": after_bytes,
        "over_limit": after_bytes > max_bytes,
    }
    return candidates, stats


def cleanup_runs(root: Path | None = None, keep_recent: int = KEEP_RECENT,
                 max_bytes: int = MAX_TOTAL_BYTES, dry_run: bool = False) -> list[str]:
    """执行清理，返回被删除的 run_id 列表。

    Args:
        dry_run: 只计算不删除（用于预览与测试）
    """
    to_delete, _stats = plan_cleanup(root, keep_recent, max_bytes)
    deleted: list[str] = []
    for r in to_delete:
        if dry_run:
            deleted.append(r["run_id"])
            continue
        try:
            shutil.rmtree(r["run_dir"])
            deleted.append(r["run_id"])
        except OSError:
            pass    # 删除失败不影响主流程（下次再试）
    return deleted


def format_size(n: float) -> str:
    """字节数 → 人类可读"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}TB"


def describe(root: Path | None = None, keep_recent: int = KEEP_RECENT,
             max_bytes: int = MAX_TOTAL_BYTES) -> str:
    """清理状态的一句话描述（供 CLI 显示）"""
    _to_delete, stats = plan_cleanup(root, keep_recent, max_bytes)
    if stats["total"] == 0:
        return "暂无运行记录"
    text = (f"运行记录 {stats['total']} 次，合计 {format_size(stats['total_bytes'])}"
            f"（保护 {stats['protected']} 次）")
    if stats["to_delete"]:
        text += f" → 将清理 {stats['to_delete']} 次"
    if stats["over_limit"]:
        text += (f" ⚠️ 清理后仍超上限（{format_size(stats['after_bytes'])}）——"
                 f"最近 {keep_recent} 次本身过大，可考虑调小 keep_recent")
    return text


def runs_root() -> Path:
    return runs_dir()
