# -*- coding: utf-8 -*-
"""CarTest core — Monkey 压测（框架）

来源工具：``D:\\BaiduNetdiskDownload\\Tools\\Monkey 自动化测试``（bat 菜单逻辑平移）。

**设计决策：crash_anr_analyzer.py 保持独立，不搬进 core**——子进程调用
（同 ``parser/offline.py`` 对 CbmLogextractor 的先例）；本模块只负责协议封装与产物定位。

设计依据：``19-Monkey压测集成设计.md``。

设备协议（自 start_com.example.carvoice.bat 提取）：
    部署 → push 三件套；启动 → nohup monkey_runner.sh；状态 → lock/done/status/pid；
    停止 → pid kill + 清锁；拉取 → dropbox/monkey_output/logcat/traces；分析 → 子进程。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: 默认工具位置（可用环境变量 CARTEST_MONKEY_DIR 覆盖）
DEFAULT_TOOL_DIR = Path(r"D:\BaiduNetdiskDownload\Tools\Monkey 自动化测试")
TOOL_DIR_ENV = "CARTEST_MONKEY_DIR"

# ── 车机端路径（bat 同款）──
DEV_JAR = "/system/framework/monkey.jar"
DEV_BIN = "/system/bin/monkey"
DEV_SCRIPT = "/sdcard/monkey_runner.sh"
DEV_LOCK = "/sdcard/monkey_running.lock"
DEV_STATUS = "/sdcard/monkey_status.txt"
DEV_PID = "/sdcard/monkey_pid.txt"
DEV_DONE = "/sdcard/monkey_done.txt"
DEV_OUTPUT_GLOB = "/sdcard/monkey_output_*.txt"
DEV_DROPBOX = "/data/system/dropbox/."
DEV_DROPBOX_CLEAN = "find /data/system/dropbox/ -maxdepth 1 -type f -delete"
DEV_TRACES = "/data/anr/traces.txt"

DEFAULT_COUNT = 50000
DEFAULT_THROTTLE = 300


class MonkeyUnavailable(RuntimeError):
    """Monkey 工具不可用（未安装 / 路径错误）"""


def tool_dir() -> Path:
    """Monkey 工具目录（候选解析：env → 配置 tools.monkey_tool_dir → 候选根）

    2026-09-21：与 CbmLogextractor 同因（外部工具目录被移动）改为候选择解析，
    沿用旧环境变量 `TOOL_DIR_ENV` 兼容。
    """
    from . import tools_locator
    found = tools_locator.resolve("monkey_tool", "Monkey 自动化测试", TOOL_DIR_ENV)
    if found is not None:
        return found
    return Path(os.environ.get(TOOL_DIR_ENV, str(DEFAULT_TOOL_DIR)))


def available() -> bool:
    """工具是否可用（分析器与部署件都在）"""
    d = tool_dir()
    return (d.is_dir() and (d / "crash_anr_analyzer.py").is_file()
            and (d / "monkey_runner.sh").is_file())


@dataclass
class MonkeyConfig:
    """压测参数（UI 配置区 → 命令构造）"""
    pkg: str
    count: int = DEFAULT_COUNT
    throttle: int = DEFAULT_THROTTLE
    clean_logs: bool = False
    clean_dropbox: bool = False


@dataclass
class StepResult:
    """单步执行结果（部署 / 启动 / 停止 / 拉取）"""
    ok: bool
    detail: str = ""


@dataclass
class MonkeyStatus:
    """车机端状态合成（lock / done / status 行 / pid）"""
    phase: str = "idle"        # idle | running | done
    pid: str = ""
    started: str = ""
    count: str = ""
    throttle: str = ""
    pkg: str = ""
    raw: str = ""

    @classmethod
    def from_files(cls, *, lock: bool, done: bool,
                   status_text: str = "", pid: str = "") -> "MonkeyStatus":
        """由车机端文件合成状态（lock 优先；status 行容错解析）"""
        st = cls()
        pid_clean = (pid or "").strip()
        st.pid = pid_clean if pid_clean.isdigit() else ""   # 过滤 adb 错误文本
        st.raw = (status_text or "").strip()
        parsed = parse_status_line(status_text)
        st.started = parsed.get("started", "")
        st.count = parsed.get("count", "")
        st.throttle = parsed.get("throttle", "")
        st.pkg = parsed.get("pkg", "")
        if lock:
            st.phase = "running"
        elif done:
            st.phase = "done"
        else:
            st.phase = "idle"
        return st


@dataclass
class MonkeyReport:
    """分析产物（同日产物 md/html 成对）"""
    path_md: str = ""
    path_html: str = ""


def parse_status_line(text: str) -> dict:
    """解析 monkey_status.txt（``状态|启动时间|事件数|间隔ms|包名``）；缺字段容错"""
    out: dict[str, str] = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            out["state"] = parts[0]
            out["started"] = parts[1]
            out["count"] = parts[2] if len(parts) > 2 else ""
            out["throttle"] = parts[3] if len(parts) > 3 else ""
            out["pkg"] = parts[4] if len(parts) > 4 else ""
        break
    return out


def build_start_cmd(pkg: str, count: int, throttle: int) -> str:
    """启动命令（照 bat：nohup + 脱离 adb shell，支持拔 USB 后台跑）"""
    return (f"nohup sh {DEV_SCRIPT} {pkg} {int(count)} {int(throttle)} "
            f"> /dev/null 2>&1 &")


def build_clean_cmds(clean_logs: bool, clean_dropbox: bool) -> list[str]:
    """启动前可选清理命令（顺序：monkey 日志 → dropbox 全清；开关为 False 不含）"""
    cmds: list[str] = []
    if clean_logs:
        cmds.append(f"rm -f {DEV_OUTPUT_GLOB} {DEV_STATUS} {DEV_DONE} {DEV_PID} {DEV_LOCK}")
    if clean_dropbox:
        cmds.append(DEV_DROPBOX_CLEAN)
    return cmds


def eta_seconds(count: int, throttle: int) -> int:
    """预计时长（秒）= 次数 × 间隔ms / 1000（bat 提示同口径）"""
    return int(count) * int(throttle) // 1000


def find_latest_report(directory: str | Path) -> MonkeyReport | None:
    """在目录里找最新一份 crash_test_report_*.html/.md（无 → None）"""
    d = Path(directory)
    if not d.is_dir():
        return None
    htmls = sorted(d.glob("crash_test_report_*.html"), key=lambda p: p.stat().st_mtime)
    mds = sorted(d.glob("crash_test_report_*.md"), key=lambda p: p.stat().st_mtime)
    if not htmls and not mds:
        return None
    return MonkeyReport(
        path_md=str(mds[-1]) if mds else "",
        path_html=str(htmls[-1]) if htmls else "")


# ════════════════════════════════════════════════
#  MonkeyController — 设备操作（adb 注入 → 离线替身可测）
# ════════════════════════════════════════════════


class MonkeyController:
    """Monkey 压测控制器（部署 / 启动 / 状态 / 停止 / 拉取 / 分析定位）

    ``adb`` 需实现 ``shell(cmd) -> (ok, out)`` / ``push(local, remote)`` /
    ``pull(remote, local)`` / ``root(device_id)`` / ``remount(device_id)``；
    默认取 ``core.adb.session()`` 单例。``tool_dir_override`` 供测试注入。
    """

    def __init__(self, adb=None, tool_dir_override: str | Path | None = None) -> None:
        self._adb = adb
        self._tool_dir_override = Path(tool_dir_override) if tool_dir_override else None

    @property
    def adb(self):
        if self._adb is None:
            from . import adb as adb_mod
            self._adb = adb_mod.session()
        return self._adb

    @property
    def tools(self) -> Path:
        """工具目录（注入覆盖优先）"""
        return self._tool_dir_override or tool_dir()

    def _require_tools(self) -> "StepResult | None":
        d = self.tools
        missing = [n for n in ("monkey.jar", "monkey", "monkey_runner.sh",
                               "crash_anr_analyzer.py") if not (d / n).is_file()]
        if missing:
            return StepResult(False, f"工具目录缺少文件：{'、'.join(missing)}（{d}）")
        return None

    # ── 部署 ──
    def deploy(self) -> StepResult:
        """root/remount → push 三件套 → chmod（首次使用一次）

        Android 10+ 动态分区：`adb remount` 禁用 verity 后可能提示**需重启设备**才生效；
        此时 /system 仍只读——部署前显式探测并给出指引（真机验证发现 #4）。
        """
        err = self._require_tools()
        if err:
            return err
        adb = self.adb
        try:
            adb.root("")
            adb.remount("")
        except Exception as ex:  # noqa: BLE001
            return StepResult(False, f"root/remount 异常：{ex}")
        ok, _ = adb.shell("test -w /system/framework && echo WRITABLE")
        if not ok:
            return StepResult(
                False,
                "系统分区只读（/system 不可写）。请执行 `adb root` + `adb remount`；"
                "若 remount 提示需重启（verity/overlayfs 生效），请重启设备后重试部署。")
        for name, remote in (("monkey.jar", DEV_JAR),
                             ("monkey", DEV_BIN),
                             ("monkey_runner.sh", DEV_SCRIPT)):
            ok, out = adb.push(self.tools / name, remote)
            if not ok:
                hint = ""
                if "Read-only" in out or "只读" in out:
                    hint = "（系统分区只读：请 root+remount，必要时禁用 verity 并重启设备后重试）"
                return StepResult(False, f"推送 {name} 失败：{out}{hint}")
        ok, out = adb.shell(f"chmod 755 {DEV_BIN}")
        if not ok:
            return StepResult(False, f"chmod 失败：{out}")
        return StepResult(True, "部署完成（jar / 可执行 / runner 已就位）")

    # ── 启动 ──
    def start(self, cfg: MonkeyConfig) -> StepResult:
        """防重入检查 →（可选）清理 → nohup 启动"""
        if not cfg.pkg:
            return StepResult(False, "包名不能为空")
        adb = self.adb
        ok, out = adb.shell(f"test -f {DEV_LOCK} && echo LOCKED")
        if ok and "LOCKED" in out:
            return StepResult(False, "已有运行中的 monkey（先停止再启动）")
        for cmd in build_clean_cmds(cfg.clean_logs, cfg.clean_dropbox):
            adb.shell(cmd)
        ok, out = adb.shell(build_start_cmd(cfg.pkg, cfg.count, cfg.throttle))
        if not ok:
            return StepResult(False, f"启动失败：{out}")
        minutes = eta_seconds(cfg.count, cfg.throttle) // 60
        return StepResult(True, f"已启动（预计约 {minutes} 分钟完成）")

    # ── 状态 ──
    def status(self) -> MonkeyStatus:
        """lock / done / status 行 / pid 合成（读取失败不抛异常）"""
        adb = self.adb
        lock = self._file_exists(DEV_LOCK)
        done = self._file_exists(DEV_DONE)
        _, status_text = adb.shell(f"cat {DEV_STATUS}")
        _, pid = adb.shell(f"cat {DEV_PID}")
        return MonkeyStatus.from_files(lock=lock, done=done,
                                       status_text=status_text, pid=pid)

    def _file_exists(self, path: str) -> bool:
        ok, out = self.adb.shell(f"test -f {path} && echo 1")
        return ok and "1" in out

    # ── 停止 ──
    def stop(self) -> StepResult:
        """pid kill（兜底 grep）→ 清 lock/pid/done（保留 crash/anr/日志数据）"""
        adb = self.adb
        _, pid = adb.shell(f"cat {DEV_PID}")
        pid = pid.strip()
        if pid.isdigit():
            adb.shell(f"kill {pid}")
            adb.shell(f"kill -9 {pid}")
            detail = f"已停止（PID {pid}）"
        else:
            adb.shell("ps -A | grep monkey.Monkey | awk '{print $2}' | xargs kill -9")
            detail = "已发送停止（grep 兜底，无 PID 文件）"
        adb.shell(f"rm -f {DEV_LOCK} {DEV_PID} {DEV_DONE}")
        return StepResult(True, detail)

    # ── 拉取 ──
    def pull(self, dest: str | Path) -> StepResult:
        """dropbox + monkey 输出/状态/完成标记/runner + logcat + ANR traces → 归档目录"""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        adb = self.adb
        adb.root("")
        ok_drop, out_drop = adb.pull(DEV_DROPBOX, dest)
        self._pull_monkey_files(dest)            # 显式列举逐个 pull（发现 #6：glob 直拉静默失败）
        ok_log, logcat = adb.shell("logcat -d -t 2000")
        if ok_log:
            (dest / "logcat.txt").write_text(logcat, encoding="utf-8", errors="replace")
        adb.pull(DEV_TRACES, dest)               # 可能不存在（忽略失败）
        if not ok_drop:
            return StepResult(False, f"dropbox 拉取失败：{out_drop}")
        return StepResult(True, f"已拉取到 {dest}")

    def _pull_monkey_files(self, dest: Path) -> list[str]:
        """列举 /sdcard 上的 monkey 运行件（output*/status/done/runner）并逐个 pull。

        发现 #6：原实现用 ``adb pull /sdcard/monkey_output_*.txt`` 单条 glob 直拉且忽略
        失败，实测从未拉回（归档缺运行证据：状态行/完成标记/output/runner 版本）。
        返回成功拉回的文件名列表。
        """
        adb = self.adb
        got: list[str] = []
        ok, out = adb.shell(
            f"ls {DEV_OUTPUT_GLOB} {DEV_STATUS} {DEV_DONE} {DEV_SCRIPT} 2>/dev/null")
        if not ok:
            return got
        for line in out.splitlines():
            p = line.strip()
            if not p.startswith("/sdcard/"):
                continue
            ok2, _ = adb.pull(p, dest)
            if ok2:
                got.append(p.rsplit("/", 1)[-1])
        return got

    # ── 分析（工具独立，子进程调用）──
    def analyze(self, log_dir: str | Path, pkg: str,
                on_log: "Callable[[str, str], None] | None" = None) -> MonkeyReport | None:
        """调用 crash_anr_analyzer.py 生成报告；返回最新报告（失败 / 无 crash 文件 → None）

        ``on_log`` 提供时逐行转发分析器输出（如「未找到任何 crash 文件」——真机验证
        发现 #5：原实现丢弃输出，界面只显示"分析未产出报告"而无原因）。
        """
        import subprocess
        import sys as _sys

        script = self.tools / "crash_anr_analyzer.py"
        if not script.is_file():
            return None
        try:
            r = subprocess.run(
                [_sys.executable, str(script), pkg, str(log_dir)],
                cwd=str(self.tools), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600)
        except Exception as ex:  # noqa: BLE001
            if on_log:
                on_log(f"[分析] 执行异常: {ex}", "error")
            return None
        if on_log:
            for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
                if line.strip():
                    on_log(f"[分析] {line}", "info")
        return find_latest_report(self.tools) or find_latest_report(log_dir)

    # ── 历史报告 ──
    def list_reports(self) -> list[MonkeyReport]:
        """工具目录下历史报告（md/html 按文件名配对；时间倒序）"""
        out: dict[str, MonkeyReport] = {}
        for p in sorted(self.tools.glob("crash_test_report_*"),
                        key=lambda p: p.stat().st_mtime):
            rep = out.setdefault(p.stem, MonkeyReport())
            if p.suffix == ".html":
                rep.path_html = str(p)
            elif p.suffix == ".md":
                rep.path_md = str(p)
        return list(reversed(list(out.values())))
