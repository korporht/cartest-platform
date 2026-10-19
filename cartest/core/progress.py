"""CarTest core — 用例执行进度存储（GUI / CLI 断点续跑共用）

设计要点：
  - 会话指纹 = 数据文件绝对路径 + 用例条数 + 前 20 条 id/query 的 sha1 前 8 位；
    指纹不符（比如同名文件换了内容）**不会自动续跑**，避免把 A 文件的进度套到 B 文件上。
  - 进度文件：`logs/progress/<数据文件stem>__<指纹8位>.jsonl`，每条灌入后 fsync 追加，
    崩溃/断电最多丢最后一条，重跑即自动续灌（幂等：同 run_id 取最后一条）。
  - 记录字段：run_id / src_id / query / device / ts / status / asr / domain / reason / send_ok

来源：CarVoiceTestTool/progress_store.py 平移（2026-09-14）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .config import LOGS_DIR

PROGRESS_DIR = LOGS_DIR / "progress"
#: 参与指纹计算的用例条数（只看头部，避免大文件每次全量 hash）
HEAD_SAMPLE = 20


def fingerprint(data_path: str | Path, pairs: list[tuple[str, str]]) -> str:
    """会话指纹：数据文件路径 + 条数 + 前 N 条 id/query。"""
    h = hashlib.sha1()
    h.update(str(Path(data_path).resolve()).lower().encode("utf-8"))
    h.update(f"|{len(pairs)}".encode("utf-8"))
    for cid, query in pairs[:HEAD_SAMPLE]:
        h.update(f"|{cid}:{query}".encode("utf-8"))
    return h.hexdigest()[:8]


def pairs_from_cases(cases) -> list[tuple[str, str]]:
    """从 TestCase 对象或 dict 列表提取 (id, query) 对"""
    out = []
    for c in cases:
        if isinstance(c, dict):
            out.append((str(c.get("id", "")), str(c.get("query", ""))))
        else:
            out.append((str(getattr(c, "id", "")), str(getattr(c, "query", ""))))
    return out


# ── 旧进度文件导通（迁移期用）────────────────────────────
#: 历史遗留进度文件的候选目录。旧工具（CarVoiceTestTool/excel_batch）的进度文件
#: 可通过 ``add_legacy_dir()`` 注册进来，由 ``import_legacy()`` 一次性导入。
#: P3 切换完成后可清空。
LEGACY_DIRS: list[Path] = []


def add_legacy_dir(path: str | Path) -> None:
    """注册一个旧进度文件目录（去重）"""
    p = Path(path)
    if p not in LEGACY_DIRS:
        LEGACY_DIRS.append(p)


class ProgressStore:
    """单个用例文件的进度文件读写"""

    def __init__(self, path: Path, fp: str = "") -> None:
        self.path = Path(path)
        self.fingerprint = fp
        self.imported = 0        # 本次从旧进度文件导入的条数

    # ── 构造 ────────────────────────────────
    @classmethod
    def of(cls, data_path: str | Path, pairs: list[tuple[str, str]],
           progress_dir: str | Path | None = None) -> "ProgressStore":
        """按用例文件定位（打开或创建）进度文件。

        Args:
            progress_dir: 覆盖进度文件目录（测试隔离用；默认用模块级 ``PROGRESS_DIR``）
        """
        base = Path(progress_dir) if progress_dir else PROGRESS_DIR
        base.mkdir(parents=True, exist_ok=True)
        fp = fingerprint(data_path, pairs)
        stem = Path(data_path).stem
        return cls(base / f"{stem}__{fp}.jsonl", fp)

    @classmethod
    def open(cls, data_path: str | Path, pairs: list[tuple[str, str]],
             import_legacy: bool = True,
             progress_dir: str | Path | None = None) -> "ProgressStore":
        """统一入口：定位进度文件 + 一次性导入历史遗留进度（CLI 旧文件导通到同一份）。

        GUI 与命令行都走这里，保证「同一用例文件只有一份进度」。
        """
        store = cls.of(data_path, pairs, progress_dir)
        if import_legacy:
            try:
                n = store.import_legacy(data_path, pairs)
                if n:
                    store.imported = n
            except Exception:
                pass
        return store

    @classmethod
    def find_existing(cls, data_path: str | Path, pairs: list[tuple[str, str]],
                      progress_dir: str | Path | None = None):
        """找到有执行记录的进度（含新导入的旧记录）；没有则返回 None"""
        store = cls.open(data_path, pairs, progress_dir=progress_dir)
        if store.path.exists() and store.path.stat().st_size > 0 and store.summary()["executed"] > 0:
            return store
        return None

    @classmethod
    def legacy_candidates(cls, data_path: str | Path) -> list[Path]:
        """与本用例文件可能同会话的旧进度文件"""
        stem = Path(data_path).stem
        out: list[Path] = []
        for d in LEGACY_DIRS:
            out.append(d / f"batch_progress_{stem}.jsonl")
            out.append(d / "news_progress.jsonl")
        return out

    def import_legacy(self, data_path: str | Path, pairs: list[tuple[str, str]]) -> int:
        """把旧进度文件里属于本用例文件的记录导入（按 run_id 去重，可重复调用）。

        安全约束：旧文件里的 run_id 必须存在于当前用例集合，否则跳过整份文件——
        避免把 A 文件的进度误嫁接到 B 文件上（如 news_progress.jsonl 只对应新闻用例）。
        """
        valid_ids = {cid for cid, _ in pairs}
        existing = set(self.latest())
        imported = 0
        for legacy in self.legacy_candidates(data_path):
            if not legacy.exists() or legacy.resolve() == self.path.resolve():
                continue
            try:
                recs = [json.loads(ln) for ln in
                        legacy.read_text(encoding="utf-8").splitlines() if ln.strip()]
            except Exception:
                continue
            recs = [r for r in recs if r.get("run_id")]
            if not recs:
                continue
            hit = sum(1 for r in recs if r["run_id"] in valid_ids)
            if hit < max(1, len(recs) * 0.5):      # 过半不匹配 → 判定为别的用例文件
                continue
            # 同一 run_id 可能因重灌/重试有多条：只取最后一条（与运行期语义一致）
            latest_legacy: dict[str, dict] = {}
            for r in recs:
                latest_legacy[r["run_id"]] = r
            batch = []
            for rid, r in latest_legacy.items():
                if rid in existing or rid not in valid_ids:
                    continue
                existing.add(rid)
                rec = {
                    "run_id": rid,
                    "src_id": str(r.get("src_id", rid.split("-Q")[0])),
                    "query": r.get("query", ""),
                    "device": r.get("device", ""),
                    "ts": r.get("ts", ""),
                    "send_ok": bool(r.get("send_ok")),
                    "status": r.get("status", ""),
                    "asr": (r.get("asr") or "")[:60],
                    "domain": (r.get("domain") or "")[:60],
                    "reason": (r.get("reason") or "")[:120],
                    "legacy": legacy.name,
                }
                batch.append(rec)
            if batch:
                self.append_many(batch)
                imported += len(batch)
        return imported

    # ── 写入 ────────────────────────────────
    def append(self, rec: dict) -> None:
        self.append_many([rec])

    def append_many(self, recs: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ── 读取 ────────────────────────────────
    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def latest(self) -> dict[str, dict]:
        """run_id → 最后一次记录（含失败/发送失败）"""
        latest: dict[str, dict] = {}
        for r in self._records():
            latest[r.get("run_id", "")] = r
        return latest

    def latest_ok(self) -> dict[str, dict]:
        """run_id → 最后一次**成功灌入**的记录"""
        return {k: v for k, v in self.latest().items() if v.get("send_ok")}

    def summary(self) -> dict:
        """{executed, last_ts, devices, pass, fail}"""
        latest = self.latest()
        ok = {k: v for k, v in latest.items() if v.get("send_ok")}
        ts = [v.get("ts", "") for v in ok.values() if v.get("ts")]
        devs = sorted({v.get("device", "") for v in ok.values() if v.get("device")})
        return {
            "executed": len(ok),
            "total_records": len(latest),
            "last_ts": max(ts) if ts else "",
            "devices": devs,
            "pass": sum(1 for v in ok.values() if v.get("status") == "pass"),
            "fail": sum(1 for v in ok.values() if v.get("status") == "fail"),
        }

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False
