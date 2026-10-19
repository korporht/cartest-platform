"""CarTest core — 离线全量日志提取（CbmLogextractor 薄封装）

**设计决策：保持 CbmLogextractor 独立，不把它的代码搬进 core。**

理由：
  1. 它与主链路是**调用关系**而非**代码重复**——全项目只有一份实现
  2. 2900 行 + ``regress_baseline/`` + ``test_logextract.py`` 回归体系已稳定，
     搬迁只增加风险
  3. 它本身已是"单入口 + 子命令"设计，接口天然清晰

执行形态（2026-09-17 修订，真机验证发现 #3）：
  **子进程执行**（``python logextract.py all <目录>``），不做进程内 importlib 加载——
  工具内部用 ``multiprocessing.Pool`` 并发；Windows spawn 的子进程按**模块名**反序列化
  ``process_log_file``，进程内加载出的 ``_cartest_logextract`` 在子进程不可导入，
  会抛 ``Can't pickle …: No module named '_cartest_logextract'``。
  子进程形态与用户手工 CLI 用法完全一致。

本模块只做三件事：定位工具 → 子进程调用（stdout 逐行转发）→ 返回产物路径。

与 ``live.py`` 的分工：
  ``live``    面向"一条用例一次会话"的实时判定（窗口 10s / 30s）
  ``offline`` 面向"整目录日志的事后复盘"（复用 CbmLogextractor 的成熟实现）
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

#: 默认工具位置（可用环境变量 CARTEST_LOGEXTRACT_DIR 覆盖）
DEFAULT_TOOL_DIR = Path(r"D:\BaiduNetdiskDownload\Tools\CbmLogextractor")

#: 执行器注入签名：``fn(argv, on_log) -> returncode``（默认＝子进程执行）
Runner = Callable[..., int]

#: 工具 stdout 契约行：``全量报告已写入: <路径>``（集成指南 §5.1，精确产物定位）
_FULL_REPORT_RE = re.compile(r"^全量报告已写入[:：]\s*(.+?)\s*$")


class LogextractUnavailable(RuntimeError):
    """CbmLogextractor 不可用（未安装 / 路径错误）"""


#: 工具入口候选（按优先级）：源码入口 → 单文件 exe（2026-10-19 支持免 Python 分发）
_PY_ENTRY = "logextract.py"
_EXE_ENTRY = "CbmLogextractor.exe"


def _find_entry(directory: Path) -> tuple[str, str] | None:
    """在目录内定位工具入口 → ``(kind, filename)``；kind ∈ {'py', 'exe'}"""
    if (directory / _PY_ENTRY).is_file():
        return "py", _PY_ENTRY
    if (directory / _EXE_ENTRY).is_file():
        return "exe", _EXE_ENTRY
    return None


def tool_dir() -> Path:
    """CbmLogextractor 目录（候选解析：env → 配置 tools.cbm_logextractor_dir → 候选根）

    2026-09-21：外部工具目录被移动过（BaiduNetdiskDownload → AgentProject/Tools），
    写死路径会导致「离线提取」静默不可用 → 统一走 `core.tools_locator`。
    保留旧环境变量 `CARTEST_LOGEXTRACT_DIR` 兼容。

    2026-10-19：入口支持单文件 ``CbmLogextractor.exe``（测试同事免装 Python）；
    目录探测时两种入口任选其一，源码入口优先。
    """
    from .. import tools_locator
    for marker in (_PY_ENTRY, _EXE_ENTRY):
        found = tools_locator.resolve("cbm_logextractor", "CbmLogextractor",
                                      "CARTEST_LOGEXTRACT_DIR", marker=marker)
        if found is not None:
            return found
    # 兜底：显式 env / 默认路径（让 available() 与错误提示给出原语义）
    return Path(os.environ.get("CARTEST_LOGEXTRACT_DIR", str(DEFAULT_TOOL_DIR)))


def tool_entry() -> tuple[Path, str]:
    """工具入口 → ``(路径, 'py'|'exe')``；都没有时抛 :class:`LogextractUnavailable`"""
    d = tool_dir()
    found = _find_entry(d)
    if found is None:
        raise LogextractUnavailable(
            f"未找到 CbmLogextractor 入口（需要 {_PY_ENTRY} 或 {_EXE_ENTRY}）: {d}")
    kind, name = found
    return d / name, kind


def reports_dir() -> Path:
    """报告目录（= ``<工具目录>/reports``；工具内 ``REPORTS_DIR`` 同锚点）"""
    return tool_dir() / "reports"


def available() -> bool:
    """工具是否可用（目录存在，且含源码入口或 exe 入口）"""
    d = tool_dir()
    return d.is_dir() and _find_entry(d) is not None


def _default_runner(argv: list, on_log) -> int:
    """子进程执行：逐行转发 stdout（工具输出为中文，PYTHONIOENCODING=utf-8 保险）"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        argv, cwd=str(tool_dir()),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.stdout is not None  # noqa: S101 — PIPE 已指定
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.strip() and on_log:
            on_log(line, "info")
    return proc.wait()


def extract_report(log_dir: str | Path, *, jobs: int | None = None,
                   on_log: Callable[[str, str], None] | None = None,
                   runner: Runner | None = None,
                   out_dir: str | Path | None = None) -> Path:
    """对日志目录跑一次「一键全流程」，返回生成的 Markdown 报告路径。

    全流程 = 字段提取 → 角色对话还原 → 导出表格 → 全量 HTML 报告
    （工具的 ``all`` 子命令；对接产物 = ``all_report_<stamp>.html`` 一份）。

    Args:
        log_dir: 日志目录（含 main_*.log / main_*.gz）
        jobs:    并发数（默认由工具决定，为 CPU 核数）
        on_log:  可选日志回调 ``(msg, tag)``；提供时逐行转发子进程输出
        runner:  注入执行器（测试用）``fn(argv, on_log) -> rc``
        out_dir: 报告输出目录（传给工具 ``--out``）；缺省读环境变量
                 ``CARTEST_LOGEXTRACT_OUT``。两者都为空时走**隔离运行目录**
                 （``reports/_run_XXXX/``），成功后把总报告与 ``_raw_<stamp>/``
                 归档**提升**到 ``reports/`` 根——既满足集成指南 §6.5 的并发隔离
                 强约束，又保持「报告落 ``reports/`` 根」的既有 UX（2026-10-13）

    Returns:
        生成的 HTML 总报告路径（工具 ``all`` 产出 ``all_report_<stamp>.html`` 于
        输出目录根——单页全量报告，唯一对接产物；6 份分报告归档 ``_raw_<stamp>/``
        仅供备查，不进对接链）。路径优先取自工具 stdout 契约行
        ``全量报告已写入: <路径>``（集成指南 §5.1，精确且不受并发目录共享影响）

    Raises:
        LogextractUnavailable: 工具不可用
        RuntimeError: 执行失败（含返回码与输入路径）。**注意**：``all`` 的返回码 1
            有两义（参数错误 / 个别文件解析失败）——仅当本轮未产出新报告时才抛出；
            报告已生成时降级为 ``on_log`` 告警并正常返回（见函数内注释与集成指南 §4.2）
    """
    entry, kind = tool_entry()

    out = out_dir or os.environ.get("CARTEST_LOGEXTRACT_OUT") or None
    run_dir: Path | None = None
    if out is None:
        # 未指定输出目录 → 隔离运行目录（同盘，便于成功后原子提升到 reports/ 根）
        reports_dir().mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="_run_", dir=str(reports_dir())))
        out = run_dir

    # 源码入口需要解释器；exe 入口自身可执行（argv[0] 直接是 exe 路径）
    argv: list = [sys.executable, str(entry)] if kind == "py" else [str(entry)]
    argv.append("all")
    if jobs:
        argv += ["--jobs", str(jobs)]
    Path(out).mkdir(parents=True, exist_ok=True)
    argv += ["--out", str(out)]
    argv.append(str(log_dir))

    # stdout 契约行嗅探（在转发前截获，不影响 on_log 语义）
    contract: list[Path] = []

    def _sniff(msg: str, tag: str = "info") -> None:
        m = _FULL_REPORT_RE.match(msg or "")
        if m:
            contract.append(Path(m.group(1)))
        if on_log:
            on_log(msg, tag)

    keep_run = False        # 提升失败时置真：保留隔离目录（不清理，避免丢产物）
    try:
        # 运行前快照已有报告 → 供 rc!=0 时判定「本轮是否真的产出了新报告」
        before = _report_paths(out)

        rc = (runner or _default_runner)(argv, _sniff)
        # 定位：stdout 契约行优先（精确），退回 glob 最新（兼容未打印该行的执行器/旧版本）
        report = next((p for p in reversed(contract) if p.is_file()), None) \
            or _find_latest_html(out)

        if rc:
            # 对齐工具契约（集成指南 §4.2）：``all`` 的 rc=1 有两义——
            #   a) 参数/输入错误、或硬失败 → 本轮**无**产物；
            #   b) 主流程完成但个别文件解析失败（工具 ``failed_files`` 非空）→
            #      全量报告**已生成**（logextract.py 末尾 ``return 1 if failed_files else 0``）。
            # 只在本轮没有新报告时才判失败；陈旧报告不得当作成功（共享 ``reports/`` 下尤甚）。
            if report is None or report in before:
                raise RuntimeError(f"CbmLogextractor 执行失败（返回码 {rc}），输入: {log_dir}")
            if on_log:
                on_log(f"[WARN] 工具返回码 {rc}（部分文件解析失败），报告已生成，按告警继续",
                       "warning")

        if report is None:
            where = reports_dir() if run_dir is not None else Path(out)
            raise RuntimeError(f"执行完成但未找到报告产物，请检查 {where}")

        if run_dir is not None:
            # 隔离运行结束 → 把总报告与归档提升到 reports/ 根（同盘 os.replace，原子）
            try:
                promoted = _promote_run_artifacts(run_dir, reports_dir())
            except OSError as ex:
                # 提升失败（占用/权限）→ 保留隔离目录并返回其内路径，绝不丢产物
                keep_run = True
                if on_log:
                    on_log(f"[WARN] 报告提升到 reports/ 失败（保留隔离目录）：{ex}", "warning")
                return report
            report = promoted or report
        return report
    finally:
        if run_dir is not None and not keep_run:
            shutil.rmtree(run_dir, ignore_errors=True)


def _promote_run_artifacts(run_dir: Path, dest: Path) -> Path | None:
    """把隔离运行目录里的总报告 + ``_raw_<stamp>/`` 归档提升到 ``dest``（同盘原子移动）

    返回提升后的总报告路径；无产物则 ``None``。总报告移动失败会抛 ``OSError``
    （调用方保留隔离目录）；``_raw_<stamp>/`` 归档移动失败只忽略（归档是次要产物）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    reports = [p for p in run_dir.glob("all_report_*.html") if p.is_file()]
    if not reports:
        return None
    latest = max(reports, key=lambda p: p.stat().st_mtime)
    final = dest / latest.name
    os.replace(latest, final)
    for d in run_dir.glob("_raw_*"):
        if d.is_dir():
            target = dest / d.name
            try:
                if not target.exists():
                    os.replace(d, target)
            except OSError:
                pass          # 归档非对接产物，失败不影响本次提取
    return final


def _report_paths(directory: str | Path | None = None) -> set[Path]:
    """报告目录内现有 ``all_report_*.html`` 的集合（判「本轮是否新增产物」用）"""
    d = Path(directory) if directory else reports_dir()
    if not d.is_dir():
        return set()
    return {p for p in d.glob("all_report_*.html") if p.is_file()}


def _find_latest_html(directory: str | Path | None = None) -> Path | None:
    """在报告目录中找最新的「all_report_*.html」（工具 ``all`` 的总报告）

    2026-09-29：工具 ``all`` 改为「reports/ 根目录只留总报告 ``all_report_*.html``，
    其余分报告归档到 ``_raw_<stamp>/``」。2026-10-08 用户裁决：**只认 HTML 这一份**
    （"只需要提取后的产物，保持在一份"）——故只检索根目录、不进 ``_raw_*``。
    ``directory`` 缺省为工具默认 ``reports/``（可用 ``--out`` 重定向的调用传该目录）
    """
    d = Path(directory) if directory else reports_dir()
    if not d.is_dir():
        return None
    candidates = [p for p in d.glob("all_report_*.html") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def latest_report() -> Path | None:
    """不执行提取，只返回已有报告中最新的一份（供"查看历史报告"用）"""
    return _find_latest_html()
