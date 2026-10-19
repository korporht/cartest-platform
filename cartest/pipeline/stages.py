"""CarTest pipeline — 七个阶段

每阶段签名统一：``f(ctx, params) -> str``（返回 detail 描述；失败抛异常）。

阶段与产物的对应关系（见 04-P2设计.md §2.3）：

  1. prepare   加载用例 + 前置检查        → cases.json
  2. inject    跑用例（含断点续跑）        → progress.jsonl
  3. settle    触发落盘 + 拉设备日志       → device_logs/
  4. extract   结构化提取                  → extract/
  5. judge     判定                        → results.json
  6. fillback  回填 xlsx 副本              → report/*.xlsx
  7. report    出报告                      → report/*.md

**幂等性约定**：重复执行同一阶段不产生副作用（覆盖自己的产物，不动他人产物）。
``device_logs/`` 是原始证据，除 settle 外任何阶段不得写入。

**依赖注入**：``PipelineParams`` 携带 adb / injector 工厂等，便于测试时替换。
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..core import adb as adb_mod
from ..core import config
from ..core import project as project_mod
from ..core.injector import InjectMode, InjectState, Injector
from ..core.judger import Judger, default_timeout as judge_default_timeout
from ..core.parser import live as parser_live
from ..core.parser import offline as parser_offline
from ..core.progress import ProgressStore, fingerprint, pairs_from_cases
from ..core.source import DataSource, TestCase
from .context import PipelineStopped, RunContext


@dataclass
class PipelineParams:
    """流水线参数（含依赖注入点）"""

    case_file: str = ""
    device: str = ""
    interval: float = config.DEFAULT_INTERVAL
    # 判定超时：默认走项目档案 judge.timeout（T28/T1MA 排队场景 30s），兜底 20s
    timeout: float = field(default_factory=judge_default_timeout)
    resume_mode: str = "skip_executed"
    #: 起始序号（0-based，= 注入器 start_from）——第 N 条**之前**的用例本次不灌
    #: （2026-10-15 用户裁决新增：GUI「灌入范围 → 从指定序号起重灌」/ CLI --start-index）
    start_index: int = 0
    extract_jobs: int | None = None
    settle_wait: float = 3.0          # 触发 flush 后等待落盘的秒数
    voice_package: str = "com.lioncarmind.car"

    # 依赖注入（None = 用真实实现）
    adb: object | None = None
    injector_factory: Callable[[], Injector] | None = None
    on_log: Callable[[str, str], None] | None = None
    #: 停止检查钩子（None = 不支持中途停止；app/state 挂 ``lambda: state.stop_requested``）。
    #: 灌入轮询循环内**用例粒度**生效——点停止后当前条灌完即中断（2026-10-16 用户反馈修复）
    stop_check: Callable[[], bool] | None = None

    def log(self, msg: str, tag: str = "info") -> None:
        if self.on_log:
            self.on_log(msg, tag)

    def adb_session(self):
        if self.adb is None:
            self.adb = adb_mod.session()
        return self.adb


# ── 1. prepare ────────────────────────────────
def stage_prepare(ctx: RunContext, p: PipelineParams) -> str:
    """加载用例 + 前置检查 → cases.json

    幂等：重复执行会重新加载并覆盖 cases.json（用例文件可能已更新）。
    """
    if not p.case_file:
        raise ValueError("缺少用例文件（--cases）")

    ds = DataSource()
    ds.split_multi_round = True
    ok, msg, n = ds.load_file(p.case_file)
    if not ok:
        raise RuntimeError(f"加载用例失败: {msg}")

    # 用例快照（本次实际执行的用例，后续阶段一律从此恢复）
    ctx.cases_json.write_text(
        json.dumps([_case_to_dict(c) for c in ds.cases], ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 前置检查：设备连接（失败即中止，避免后面白跑）
    #   注：不做"是否替身"的分支判断——注入的替身应完整替代真实实现，
    #   否则测试无法模拟"设备未连接"这类失败路径。
    adb = p.adb_session()
    connected, dev = adb.check_device(force=True)
    if not connected:
        raise RuntimeError(f"设备检查失败: {dev}")
    if not p.device:
        p.device = dev
    else:
        # 指定设备必须真实在线——否则 check_device 会返回别的在线设备使检查
        # 假过，流水线全程操作错设备（2026-10-10 全局设备切换改造新增；
        # 替身无 list_devices 能力时跳过校验，保持离线可测）
        lister = getattr(adb, "list_devices", None)
        online = list(lister() or []) if callable(lister) else []
        if online and p.device not in online:
            raise RuntimeError(
                f"指定设备 {p.device} 不在线（当前在线: {', '.join(online)}）——"
                f"请在顶栏切换设备或检查连接")
    p.log(f"[prepare] 设备就绪: {p.device}", "info")

    return f"{n} 条用例"


def _case_to_dict(c: TestCase) -> dict:
    return {
        "id": c.id, "query": c.query,
        "expected_keywords": list(c.expected_keywords),
        "expected_intent": c.expected_intent,
        "category": c.category, "priority": c.priority, "remark": c.remark,
    }


def _load_cases(ctx: RunContext) -> DataSource:
    """从 cases.json 恢复用例（快照语义）"""
    data = json.loads(ctx.cases_json.read_text(encoding="utf-8"))
    ds = DataSource()
    ds._cases = [TestCase(**d) for d in data]
    return ds


# ── 2. inject ─────────────────────────────────
def stage_inject(ctx: RunContext, p: PipelineParams) -> str:
    """跑用例（含断点续跑）→ progress.jsonl

    幂等：进度文件按 run_id 幂等（同 id 取最后一条），重跑不会重复计算；
    若已全部执行且模式为 skip_executed，则本次零灌入。
    """
    ds = _load_cases(ctx)
    pairs = pairs_from_cases(ds.cases)

    # 进度文件直接用 run 目录下的 progress.jsonl（不走 of() 的指纹命名）
    fp = fingerprint(p.case_file or ctx.case_file, pairs)
    store = ProgressStore(ctx.progress_jsonl, fp)

    # 断点续跑：已有的记录作为"上次状态"
    prev = {k: v.get("status", "") for k, v in store.latest().items()}

    inj = p.injector_factory() if p.injector_factory else Injector(adb=p.adb)
    inj.set_params(interval=p.interval, timeout=p.timeout)
    inj.set_device(p.device)
    inj.set_progress_store(store, p.resume_mode, prev)
    inj.set_callbacks(on_log=lambda m, t="info": p.log(m, t))

    inj.start_batch(ds, mode=InjectMode.INTERVAL, start_from=max(0, p.start_index))

    deadline = time.time() + 3600 * 6      # 兜底上限 6 小时
    while inj.state != InjectState.IDLE:
        # 停止请求：用例粒度生效——通知 batch 线程当前条灌完即退出，然后抛停止
        if p.stop_check is not None and p.stop_check():
            inj.stop()
            stop_deadline = time.time() + 300   # 限时等待收尾（防 batch 线程挂死）
            while inj.state != InjectState.IDLE and time.time() < stop_deadline:
                time.sleep(0.2)
            raise PipelineStopped("已在灌入阶段停止（已完成条目已记进度，可续跑）")
        if time.time() > deadline:
            inj.stop()
            raise TimeoutError("灌入超时（超过 6 小时）")
        time.sleep(0.2)

    summary = store.summary()
    skipped = inj.skipped_count
    prefix = (f"从第 {p.start_index + 1} 条开始 · " if p.start_index > 0 else "")
    return (f"{prefix}执行 {summary['executed'] - skipped} 条 / 跳过 {skipped} 条"
            f"（通过 {summary['pass']} 失败 {summary['fail']}）")


# ── 3. settle ─────────────────────────────────
def stage_settle(ctx: RunContext, p: PipelineParams) -> str:
    """触发日志落盘 + 拉取设备日志 → device_logs/

    幂等：device_logs/ 每次重新拉取会覆盖同名文件——但**只覆盖自己**，
    不动其他产物。真实设备上同名文件即同一份日志，覆盖无副作用。

    **flush 策略随平台档案**（2026-10-10 真机事故）：
      · E0V（默认）：force-stop 主应用触发 vstgloballogcontrol 落盘
      · T28/T1MA（``log.settle_flush=false``）：日志实时落盘，**不得 force-stop**——
        该平台主应用被 force-stop 后不自动复活，车机回落 Launcher、语音服务进入
        "输入法期间"拦截态，后续注入全部被拒（实测事故：整轮测试废弃 + 设备坏态）
    """
    adb = p.adb_session()
    ctx.device_logs_dir.mkdir(parents=True, exist_ok=True)   # 契约：目录必存在
    if project_mod.get("log.settle_flush", True):
        p.log("[settle] 触发日志 flush（force-stop 语音应用）…", "info")
        adb.force_stop(p.voice_package, p.device)
        time.sleep(p.settle_wait)
    else:
        p.log("[settle] 跳过 force-stop flush（该平台日志实时落盘；档案 log.settle_flush=false）",
              "info")

    p.log("[settle] 拉取设备日志…", "info")
    ok, msg, count = adb.pull_logs(ctx.device_logs_dir, p.device)
    if not ok:
        # 拉取失败不算致命：extract 阶段将基于空目录产出空结果
        p.log(f"[settle] 日志拉取未成功: {msg}", "warn")
        return f"拉取未成功: {msg}"
    return f"{msg}"


# ── 4. extract ────────────────────────────────
def stage_extract(ctx: RunContext, p: PipelineParams) -> str:
    """结构化提取（CbmLogextractor）→ extract/

    幂等：覆盖 extract/ 目录。
    """
    ctx.extract_dir.mkdir(parents=True, exist_ok=True)

    if not ctx.device_logs_dir.is_dir() or not any(ctx.device_logs_dir.iterdir()):
        return "无设备日志可提取（settle 未产出）"

    # 平台开关（项目档案 extract.enabled，默认 true 保持旧行为）：
    # T28/T1MA 日志格式 CbmLogextractor 尚未适配（2026-10-09 实测：回复链 0/N），档案设 false
    if not bool(project_mod.get("extract.enabled", True)):
        return "该平台未启用对话还原（项目档案 extract.enabled=false；CbmLogextractor 待适配）"

    if not parser_offline.available():
        return "CbmLogextractor 不可用，跳过提取"

    try:
        report = parser_offline.extract_report(
            ctx.device_logs_dir, jobs=p.extract_jobs,
            on_log=lambda m, t="info": p.log(f"[extract] {m}", t))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"提取失败: {e}") from e

    # 报告复制进 run 目录（原始报告留在工具目录，此处为本次运行的副本）
    dst = ctx.extract_dir / report.name
    shutil.copy2(report, dst)
    return f"报告 {dst.name}"


# ── 5. judge ──────────────────────────────────
def stage_judge(ctx: RunContext, p: PipelineParams) -> str:
    """判定 → results.json

    数据来源两条腿：
      · 实时判定（progress.jsonl）—— 灌入时由 Judger 产出
      · 设备日志结论（device_logs/ 经 LogParser 解析）—— 权威来源

    两者冲突时**以设备日志为准**并标注，供人工复核。
    幂等：覆盖 results.json。**可单独重跑**（只读 device_logs/ 与 progress.jsonl）。
    """
    ds = _load_cases(ctx)
    live_map = _load_progress(ctx)              # run_id → 进度记录
    device_map = _parse_device_logs(ctx, log_fn=p.log)   # query → 设备日志结论

    results = []
    for case in ds.cases:
        rec = live_map.get(case.id, {})
        device = device_map.get(case.query, {})

        live_status = rec.get("status", "pending")
        live_domain = rec.get("domain", "")
        live_reply = ""

        dev_domain = device.get("domain", "")
        dev_reply = device.get("reply", "")

        merged_status, note = _merge_verdict(live_status, live_domain, dev_domain)

        results.append({
            "run_id": case.id,
            "query": case.query,
            "category": case.category,
            "live_status": live_status,
            "live_domain": live_domain,
            "device_domain": dev_domain,
            "domain": dev_domain or live_domain,     # 设备日志优先
            "reply": dev_reply or live_reply,
            "query_time": device.get("query_time", ""),
            "status": merged_status,
            "note": note,
            "reason": rec.get("reason", ""),
        })

    payload = {
        "run_id": ctx.run_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "conflicts": sum(1 for r in results if r["note"]),
        "results": results,
    }
    ctx.results_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return (f"共 {payload['total']} 条：通过 {payload['pass']} / 失败 {payload['fail']}"
            + (f"，需复核 {payload['conflicts']} 条" if payload["conflicts"] else ""))


def _load_progress(ctx: RunContext) -> dict[str, dict]:
    """读 progress.jsonl → {run_id: 最后一条}"""
    if not ctx.progress_jsonl.is_file():
        return {}
    latest: dict[str, dict] = {}
    for line in ctx.progress_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        latest[rec.get("run_id", "")] = rec
    return latest


def _parse_created_at(text: str) -> datetime | None:
    """解析 run 创建时刻（``"%Y-%m-%d %H:%M:%S"``）；失败返回 None（不做过滤）"""
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S") - timedelta(seconds=60)
    except (ValueError, TypeError):
        return None


def _parse_device_logs(ctx: RunContext, log_fn=None) -> dict[str, dict]:
    """解析 device_logs/ → {query: {domain, reply, query_time}}

    用 core 的 live 解析器（与实时链路同一套规则）。
    同一 query 可能对应多条记录（ASR normal_text 记录无 domain、CBM 落域记录有；
    真机上 ASR 记录还会重复多条且时间更晚）——按**非空字段优先**合并，
    避免后写记录把 CBM 的 domain/reply 覆盖为空。

    **时间窗过滤（2026-10-10 真机实测新增）**：device_logs 可能含历史归档
    （T28 的 /log/android/logcat/ 拉取会带出多份 gz）——同 query 的历史记录
    经合并会污染本次结论（甚至用历史证据"翻案"）。只保留 run 创建之后
    （留 60s 余量防主机/设备时钟偏差）的记录。

    平台档案（2026-10-09 T28/T1MA）：
      · ``log.file_glob``        日志文件名匹配（默认 main_*.log/gz；T28: logcat.txt + *.txt.gz）
      · ``log.reply_fragments``  流式分片回复提取（默认 false；T28: true）
    """
    if not ctx.device_logs_dir.is_dir():
        return {}
    globs = project_mod.get("log.file_glob", None)
    fragments = bool(project_mod.get("log.reply_fragments", False))
    # getattr 防御：测试替身（SimpleNamespace）可能不带 created_at——无则不过滤
    since = _parse_created_at(getattr(ctx, "created_at", ""))

    # 防静默失效：目录有文件、但按 glob 无一命中（多半是档案 file_glob 配错）→ 显式告警
    try:
        files = parser_live.iter_log_files(str(ctx.device_logs_dir), globs)
        if not files and any(f.is_file() for f in ctx.device_logs_dir.rglob('*')):
            if log_fn:
                log_fn(f"[judge] device_logs 有文件但无一匹配 file_glob="
                       f"{globs or '默认(main_*.log/gz)'}——请检查项目档案 log.file_glob", "warn")
    except Exception:  # noqa: BLE001 - 统计失败不影响后续解析
        pass

    try:
        matched, _intents = parser_live.parse_logs(
            str(ctx.device_logs_dir), file_globs=globs, fragments=fragments)
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for m in matched:
        if not m.query:
            continue
        # 时间窗：跳过早于本次运行的记录（历史归档污染防护，2026-10-10）
        if since and m.query_time and m.query_time < since:
            continue
        rec = {
            "domain": m.domain or "",
            "reply": m.reply or "",
            "query_time": m.query_time.strftime("%H:%M:%S") if m.query_time else "",
        }
        # 权威度（2026-10-10）：有 sid = CBM 落域记录（domain 权威、回复经 sid 配对）；
        # 无 sid = ASR 记录（domain 必空、回复只能时间邻近易错配）。同 query 多条记录时
        # **高权威覆盖低权威**——否则 ASR 记录先入为主会把错配回复带进结论。
        rank = 2 if m.sid else 1
        prev = out.get(m.query)
        if prev is None:
            rec["_rank"] = rank
            out[m.query] = rec
            continue
        if rank > prev.get("_rank", 1):
            rec["_rank"] = rank
            out[m.query] = rec
            continue
        for k, v in rec.items():
            if k == "_rank":
                continue
            if v and not prev.get(k):
                prev[k] = v
    for rec in out.values():
        rec.pop("_rank", None)
    return out


def _merge_verdict(live_status: str, live_domain: str, dev_domain: str) -> tuple[str, str]:
    """合并实时判定与设备日志结论。

    规则（对齐 09-14 的教训：设备日志才是权威）：
      · 实时说失败、设备日志有落域与回复 → 判 pass，但标注"实时判定可能失真"
      · 实时说通过、设备日志无任何记录   → 保持 pass，标注"设备日志未覆盖"
      · 两者一致                          → 无标注
    """
    if live_status in ("pass", "fail") and dev_domain:
        if live_status == "fail":
            return "pass", f"实时判定为失败但设备日志有落域({dev_domain})，以设备日志为准"
        return "pass", ""
    if live_status == "pass" and not dev_domain:
        return "pass", "设备日志未覆盖该 query，结论来自实时判定"
    if live_status == "fail" and not dev_domain:
        return "fail", ""
    return live_status, ""


# ── 6. fillback ───────────────────────────────
def stage_fillback(ctx: RunContext, p: PipelineParams) -> str:
    """回填 xlsx 副本 → report/*.xlsx

    **不修改源文件**：结果写入 run 目录下的副本，供人工确认后替换。
    幂等：覆盖自己的输出。

    ⚠️ 基础版：追加「灌入执行 / domain / reply」三列，不做原表格式保留
       （完整版需复用旧工具的 fill_log_columns 逻辑，列入后续迭代）。
    """
    if not ctx.results_json.is_file():
        return "无判定结果，跳过回填"

    payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
    ctx.report_dir.mkdir(parents=True, exist_ok=True)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return "openpyxl 不可用，跳过回填"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "灌入结果"

    headers = ["用例ID", "query", "状态", "落域", "回复", "query时间", "备注"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4F6BF6", fill_type="solid")

    fills = {"pass": "D1FAE5", "fail": "FEE2E2"}
    for r in payload["results"]:
        ws.append([r["run_id"], r["query"], r["status"], r["domain"],
                   r["reply"], r["query_time"], r["note"]])
        color = fills.get(r["status"])
        if color:
            ws.cell(row=ws.max_row, column=3).fill = PatternFill(
                start_color=color, fill_type="solid")

    for col, width in zip("ABCDEFG", (12, 32, 8, 14, 40, 12, 28)):
        ws.column_dimensions[col].width = width

    out = ctx.report_dir / f"{ctx.run_id}_灌入结果.xlsx"
    wb.save(str(out))
    return f"已写入 {out.name}"


# ── 7. report ─────────────────────────────────
def stage_report(ctx: RunContext, p: PipelineParams) -> str:
    """出报告 → report/*.md

    幂等：覆盖自己的输出。
    """
    if not ctx.results_json.is_file():
        return "无判定结果，跳过报告"

    payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
    ctx.report_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# 灌入测试报告 — {ctx.run_id}",
        "",
        f"- 用例文件：`{ctx.case_file}`",
        f"- 设备：`{ctx.device or '(未记录)'}`",
        f"- 生成时间：{payload['generated_at']}",
        "",
        "## 汇总",
        "",
        "| 指标 | 数量 |",
        "|---|---|",
        f"| 总用例 | {payload['total']} |",
        f"| 通过 | {payload['pass']} |",
        f"| 失败 | {payload['fail']} |",
        f"| 需人工复核 | {payload['conflicts']} |",
        "",
        "## 需复核条目",
        "",
    ]

    noted = [r for r in payload["results"] if r["note"]]
    if noted:
        lines += ["| 用例ID | query | 说明 |", "|---|---|---|"]
        lines += [f"| {r['run_id']} | {r['query']} | {r['note']} |" for r in noted]
    else:
        lines.append("（无）")

    lines += ["", "## 失败条目", ""]
    failed = [r for r in payload["results"] if r["status"] == "fail"]
    if failed:
        lines += ["| 用例ID | query | 落域 | 原因 |", "|---|---|---|---|"]
        lines += [f"| {r['run_id']} | {r['query']} | {r['domain']} | {r['reason'][:60]} |"
                  for r in failed]
    else:
        lines.append("（无）")

    out = ctx.report_dir / f"{ctx.run_id}_报告.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return f"已写入 {out.name}"


# ── 注册表 ────────────────────────────────────
STAGE_FUNCS: dict[str, Callable[[RunContext, PipelineParams], str]] = {
    "prepare": stage_prepare,
    "inject": stage_inject,
    "settle": stage_settle,
    "extract": stage_extract,
    "judge": stage_judge,
    "fillback": stage_fillback,
    "report": stage_report,
}
