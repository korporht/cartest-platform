"""CarTest core — 唯一的 ADB 会话

职责：命令执行 / 设备管理 / 语音链路 / 日志拉取 / 文件传输

来源：``CarVoiceTestTool/adb_manager.py`` 平移（2026-09-14），并新增：
  - ``device_timestamp()``  从 result_judger 抽出的设备时钟查询（带 TTL 缓存）
  - ``remount()`` / ``force_stop()`` / ``push()`` / ``pull()`` / ``shell()`` 便捷方法

设计约束（见 02-core接口设计.md）：
  - 命令一律以 list 形式传递（不经 shell，避免注入与转义问题）
  - 回调签名为 ``(msg, tag)``，不依赖任何 UI
  - ``_base_args`` / ``_startupinfo`` 为静态方法，供 logcat 等直接复用
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from . import config_center
from .config import (
    ADB_CACHE_TTL,
    ADB_DAEMON_FAIL_KWS,
    ADB_SEND_TIMEOUT,
    ADB_TIMEOUT,
    VOICE_PACKAGES,
    VR_URI_BASE,
)
from . import project as _project

#: 日志回调签名
OnLog = Callable[[str, str], None] | None


def _env_serial() -> str:
    """全局选定设备（``ANDROID_SERIAL`` 环境变量；GUI 顶栏切换 / CLI ``--device`` 自动写入）

    裸 ``adb`` 命令会被 adb 官方按该变量路由（显式 ``-s`` 优先于它）；
    本模块的"自动检测"路径（check_device / 设备时钟）也以它为首选设备。
    """
    return os.environ.get("ANDROID_SERIAL", "").strip()


def voice_uri_base() -> str:
    """语义灌入 URI（项目档案 → ``config.VR_URI_BASE`` 兜底）"""
    return str(_project.get("voice.inject_uri", VR_URI_BASE))


def voice_packages() -> list[str]:
    """语音栈包名（项目档案 → ``config.VOICE_PACKAGES`` 兜底）"""
    return list(_project.get("device.voice_packages", VOICE_PACKAGES) or [])


def service_starts() -> list[tuple[str, str]]:
    """语音服务组件 ``[(名称, component)]``（项目档案 → 内置三段兜底）"""
    items = _project.get("device.services", _project.BUILTIN_SERVICES) or []
    return [(str(s.get("label", "")), str(s.get("component", "")))
            for s in items if isinstance(s, dict)]


class AdbSession:
    """ADB 会话（设备探测带 30s TTL 缓存 + daemon 自愈）"""

    #: 设备时间戳缓存有效期（秒）——批量灌入时每条都查 adb 开销约百毫秒级
    DEV_TS_TTL = 2.0

    def __init__(self) -> None:
        self._cached_device_id: str = ""
        self._cached_device_time: float = 0.0
        self._dev_ts_cache: tuple[float, str] = (0.0, "")

    # ── 基础执行 ──────────────────────────────
    @staticmethod
    def _base_args(device_id: str = "") -> list[str]:
        return ["adb", "-s", device_id] if device_id else ["adb"]

    @staticmethod
    def _startupinfo() -> "subprocess.STARTUPINFO | None":
        if sys.platform != "win32":
            return None
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return si

    def run(self, cmd: list[str], timeout: float = ADB_TIMEOUT) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, startupinfo=self._startupinfo(),
        )

    def shell(self, cmd: str, device_id: str = "", timeout: float | None = None,
              on_log: OnLog = None) -> tuple[bool, str]:
        """执行一条 adb shell 命令（返回 ``(ok, 输出)``）。

        注意：``cmd`` 会作为**单个参数**传给设备 shell，等价于
        ``adb shell "<cmd>"``——需要多参数语义时请直接用 :meth:`run`。
        """
        try:
            r = self.run(self._base_args(device_id) + ["shell", cmd],
                         timeout=timeout or ADB_TIMEOUT)
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0:
                return True, out
            if on_log:
                on_log(f"[shell] rc={r.returncode}: {out}", "warn")
            return False, out
        except subprocess.TimeoutExpired:
            return False, f"[shell] 超时({timeout or ADB_TIMEOUT}s)"
        except Exception as e:  # noqa: BLE001
            return False, f"[shell] {e}"

    # ── 设备检测 ──────────────────────────────
    def check_device(self, force: bool = False) -> tuple[bool, str]:
        desired = _env_serial()
        now = time.time()
        if not force and self._cached_device_id and (now - self._cached_device_time) < ADB_CACHE_TTL:
            # 期望设备（环境变量）与缓存不一致时不走缓存——否则切换设备后 30s 内
            # 仍返回旧设备（静默操作错设备）
            if not desired or desired == self._cached_device_id:
                return True, self._cached_device_id

        try:
            ok, msg, daemon_fault = self._probe_devices()
            if ok:
                return True, msg
            if daemon_fault:
                self._repair_daemon()
                ok, msg, _ = self._probe_devices()
                if ok:
                    return True, msg
            return False, msg
        except FileNotFoundError:
            return False, "[错误] 未找到 adb，请检查 PATH"
        except subprocess.TimeoutExpired:
            return False, "[错误] ADB 命令超时"
        except Exception as e:  # noqa: BLE001
            return False, f"[错误] {e}"

    def _probe_devices(self) -> tuple[bool, str, bool]:
        result = self.run(self._base_args() + ["devices"], timeout=10)
        output = result.stdout + result.stderr
        devices: list[str] = []
        skip_kw = ("list of devices", "daemon", "attached")
        for line in output.splitlines():
            line = line.strip()
            if not line or any(k in line.lower() for k in skip_kw):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        if devices:
            # 期望设备（环境变量）在线则优先；否则回退第一台（原有行为）
            desired = _env_serial()
            chosen = desired if desired in devices else devices[0]
            self._cached_device_id = chosen
            self._cached_device_time = time.time()
            return True, chosen, False

        if self._is_daemon_failure(result.returncode, output):
            err_snippet = (result.stderr.strip() or result.stdout.strip())
            err_snippet = err_snippet.replace("\n", " | ")[:160] or "ADB server 无响应"
            return False, f"[检测] ADB 服务异常: {err_snippet}", True

        return False, "[检测] 未发现可用设备", False

    @staticmethod
    def _is_daemon_failure(rc: int, output: str) -> bool:
        if rc == 0:
            return False
        low = output.lower()
        return any(k in low for k in ADB_DAEMON_FAIL_KWS)

    def _repair_daemon(self) -> None:
        for sub_cmd in ("kill-server", "start-server"):
            try:
                self.run(self._base_args() + [sub_cmd], timeout=15)
            except Exception:
                pass
        time.sleep(1)

    def invalidate_cache(self) -> None:
        self._cached_device_time = 0.0

    def list_devices(self) -> list[str]:
        """返回所有在线设备 ID（供 UI 下拉选择）"""
        try:
            result = self.run(self._base_args() + ["devices"], timeout=10)
            output = result.stdout + result.stderr
            devices: list[str] = []
            skip_kw = ("list of devices", "daemon", "attached")
            for line in output.splitlines():
                line = line.strip()
                if not line or any(k in line.lower() for k in skip_kw):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            return devices
        except Exception:  # noqa: BLE001
            return []

    # ── 重连 / Root / Remount ─────────────────
    def reconnect(self, device_id: str = "") -> tuple[bool, str]:
        base = self._base_args(device_id)
        try:
            self.run(base + ["reconnect"], timeout=15)
            time.sleep(2)
            r = self.run(base + ["shell", "echo", "ok"], timeout=10)
            if r.returncode == 0 and "ok" in r.stdout:
                return True, "重连成功"
            return False, f"重连验证失败: {r.stderr.strip()}"
        except Exception as e:  # noqa: BLE001
            return False, f"重连异常: {e}"

    def root(self, device_id: str, max_retries: int = 3) -> bool:
        base = self._base_args(device_id)
        for _ in range(1, max_retries + 1):
            try:
                r = self.run(base + ["root"], timeout=15)
            except subprocess.TimeoutExpired:
                time.sleep(2)
                continue
            if r.returncode == 0:
                time.sleep(2)
                try:
                    r2 = self.run(base + ["shell", "whoami"], timeout=10)
                except subprocess.TimeoutExpired:
                    time.sleep(2)
                    continue
                if "root" in r2.stdout:
                    return True
            time.sleep(2)
        return False

    def remount(self, device_id: str = "") -> bool:
        """remount /system 等分区为可写"""
        try:
            r = self.run(self._base_args(device_id) + ["remount"], timeout=20)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # ── 设备时钟 ──────────────────────────────
    def device_timestamp(self) -> str:
        """设备当前时间戳字符串 ``MM-DD HH:MM:SS.mmm``（带短 TTL 缓存）。

        用于判定时过滤"发送前的旧日志"——设备时钟与 PC 时钟可能不同步，
        故一律以设备时钟为基准。查询失败返回空串。
        """
        now = time.time()
        cached_at, cached_ts = self._dev_ts_cache
        if cached_at and (now - cached_at) < self.DEV_TS_TTL:
            return cached_ts
        ts = self._query_device_timestamp()
        self._dev_ts_cache = (now, ts)
        return ts

    def _query_device_timestamp(self) -> str:
        # 显式指定设备：环境变量（全局选定）→ 探测缓存 → 空（adb 自动）
        # —— 多设备在线时裸 adb 会报 more than one device 并静默返回空串
        # （判定"过滤旧日志"能力整体降级），故这里必须带上 -s
        dev = _env_serial() or self._cached_device_id
        try:
            result = subprocess.run(
                self._base_args(dev) + ["shell", "date", "+'%m-%d %H:%M:%S.%3N'"],
                capture_output=True, text=True, timeout=2,
                startupinfo=self._startupinfo(),
            )
            if result.returncode == 0:
                return result.stdout.strip().strip("'")
        except Exception:
            pass
        return ""

    # ── 语音灌入 ──────────────────────────────
    def send_voice(self, text: str, device_id: str = "", mode: int = 3,
                   on_log: OnLog = None) -> tuple[bool, str]:
        """语义灌入（getSemantic）；exit 255 自动重连并重试一次。"""
        base = self._base_args(device_id)
        encoded_text = urllib.parse.quote(text, safe="")
        voice_url = f"{voice_uri_base()}?text={encoded_text}&mode={mode}"

        cmd = base + [
            "shell", "am", "startservice",
            "-a", "android.intent.action.VIEW",
            "-d", f'"{voice_url}"',
        ]

        def _log(msg: str, tag: str = "info") -> None:
            if on_log:
                on_log(msg, tag)

        try:
            _log(f"[灌入] → {text}", "info")
            r = self.run(cmd, timeout=ADB_SEND_TIMEOUT)

            if r.returncode == 255:
                _log("[灌入] ADB 断连 (exit 255)，正在重连...", "warn")
                rok, rmsg = self.reconnect(device_id)
                if rok:
                    _log("[灌入] 重连成功，重试中...", "warn")
                    time.sleep(1)
                    r2 = self.run(cmd, timeout=ADB_SEND_TIMEOUT)
                    if r2.returncode == 0:
                        return True, "OK (重连后重试成功)"
                    return False, f"rc={r2.returncode}: {r2.stderr.strip()}"
                return False, f"ADB 重连失败: {rmsg}"

            if r.returncode == 0:
                return True, r.stdout.strip() or "OK"
            return False, f"rc={r.returncode}: {r.stderr.strip() or r.stdout.strip()}"

        except subprocess.TimeoutExpired:
            return False, f"[灌入] 超时({ADB_SEND_TIMEOUT:.0f}s)"
        except Exception as e:  # noqa: BLE001
            return False, f"[灌入] {e}"

    def force_stop(self, package: str, device_id: str = "") -> bool:
        """强制停止应用（用于触发日志 flush 等）"""
        try:
            r = self.run(self._base_args(device_id) + ["shell", "am", "force-stop", package],
                         timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def restart_voice_service(self, device_id: str = "", on_log: OnLog = None) -> bool:
        """重启语音链路（force-stop 4 包 + kill + 拉起 3 服务）"""
        base = self._base_args(device_id)

        def _log(msg: str, tag: str = "info") -> None:
            if on_log:
                on_log(msg, tag)

        try:
            _log("[语音] 开始重启语音服务...", "warn")
            for pkg in voice_packages():
                _log(f"[语音] 停止 {pkg}...", "info")
                self.run(base + ["shell", "am", "force-stop", pkg], timeout=15)
                time.sleep(0.5)

            self.run(
                base + ["shell", "am", "kill", "--user", "0",
                        "com.iflytek.autofly.platformservice"],
                timeout=10)

            svc_starts = service_starts()
            if not svc_starts:
                # PLT2/PLT3 等平台无 iflytek.autofly.vr 等组件——档案 device.services 为空时
                # 跳过启动段（组件清单待真机验证后在项目档案补齐），不静默、不误报失败
                _log("[语音] 该平台未配置服务组件（device.services 为空）——"
                     "跳过启动段；请真机验证后在项目档案补齐", "warn")
            else:
                for name, comp in svc_starts:
                    _log(f"[语音] 启动{name}...", "info")
                    r = self.run(base + ["shell", "am", "startservice", "-n", comp], timeout=15)
                    tag = "scene" if r.returncode == 0 else "warn"
                    _log(f"[语音] {name}{'已启动' if r.returncode == 0 else '启动可能失败'}\n", tag)
                    time.sleep(1)

            _log("[语音] 语音服务重启完成", "scene")
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"[语音] 重启异常: {e}", "error")
            return False

    # ── 日志清理 / 拉取 ───────────────────────
    def clean_logs(self, device_id: str = "") -> tuple[bool, str]:
        base = self._base_args(device_id)
        remote = config_center.primary_pull_path()
        try:
            r = self.run(base + ["shell", "rm", "-rf", remote + "*"], timeout=15)
            if r.returncode == 0:
                return True, "日志已清除"
            return False, r.stderr.strip()
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def pull_logs(self, out_dir: str | Path, device_id: str = "") -> tuple[bool, str, int]:
        """拉取设备落盘日志目录到本地（返回 ``(ok, 描述, 成功文件数)``）"""
        base = self._base_args(device_id)
        remote = config_center.primary_pull_path()
        r = self.run(base + ["shell", "ls", remote], timeout=10)
        if "No such file or directory" in (r.stdout + r.stderr):
            return False, "设备日志目录不存在", 0

        files = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]
        if not files:
            return False, "设备上没有日志文件", 0

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ok_count = 0
        for f in files:
            r2 = self.run(
                base + ["pull", remote + f, str(out_dir / f)],
                timeout=60)
            if r2.returncode == 0:
                ok_count += 1

        return True, f"已保存 {ok_count}/{len(files)}", ok_count

    # ── 文件传输 ──────────────────────────────
    def push(self, local: str | Path, remote: str, device_id: str = "") -> tuple[bool, str]:
        try:
            r = self.run(self._base_args(device_id) + ["push", str(local), remote], timeout=300)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def pull(self, remote: str, local: str | Path, device_id: str = "") -> tuple[bool, str]:
        try:
            local = Path(local)
            local.parent.mkdir(parents=True, exist_ok=True)
            r = self.run(self._base_args(device_id) + ["pull", remote, str(local)], timeout=300)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return False, str(e)


# ── 模块级单例 ────────────────────────────────────────
_session: AdbSession | None = None


def session() -> AdbSession:
    """获取全局 ADB 会话单例"""
    global _session
    if _session is None:
        _session = AdbSession()
    return _session
