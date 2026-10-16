# -*- coding: utf-8 -*-
"""CarTest app — 单例守卫（P3d-2）

保证同一时间只有一个 CarTest 应用实例（桌面 / Web 统一约束）：

- **判定**：Windows 命名互斥体（``ctypes``，零依赖）——进程退出即自动释放，
  不存在"陈旧锁"问题（对比端口占用检测：旧实例静默死亡后端口会空出）
- **信息**：锁文件 ``logs/app.lock``（pid / mode / port / started），用于友好提示
  与 ``--restart`` 的定点清理
- **清理**：``--restart`` 时按「锁文件 pid + 端口 LISTENING owner」定向
  ``taskkill /F /T``，最多等待 5 秒旧实例释放互斥
- **兼容**：旧版实例（无守卫）不持有互斥 → 通过"端口占用"分支兜底提示

设计依据：2026-09-16 用户裁决（应用内守卫，Web 承接"杀旧起新"的开发语义）。
"""
from __future__ import annotations

import atexit
import collections.abc
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..core.config import LOGS_DIR

__all__ = ["ensure_single", "is_running", "probe_instances", "cleanup_instances",
           "LOCK_FILE", "MUTEX_NAME"]

#: 命名互斥体（Local\ 前缀：本会话可见，不跨用户）
MUTEX_NAME = "Local\\CarTest_Single_Instance"
LOCK_FILE = LOGS_DIR / "app.lock"

_MUTEX_HANDLE = None      # 持有到进程退出（故意不关闭句柄：关闭即释放互斥）
_ERROR_ALREADY_EXISTS = 183

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _k32.GetExitCodeProcess.restype = ctypes.c_bool
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
else:  # pragma: no cover - 非 Windows 直接放行
    _k32 = None

#: OpenProcess 的最小查询权限（只问"进程还在不在"，不读内存）
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: GetExitCodeProcess 的"仍在运行"哨兵值
_STILL_ACTIVE = 259


def _alive(pid: int) -> bool:
    """进程是否**仍在运行**（零依赖）。

    注意：Windows 上进程已终止但仍有未关闭的句柄时，``OpenProcess`` 依然成功
    （进程对象未被销毁）——因此必须再查 ``GetExitCodeProcess``，否则会把手柄
    未释放的已死进程误判为存活。
    """
    if _k32 is None or pid <= 0:
        return False
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False                                  # 已销毁 / 无权限
    try:
        code = ctypes.c_ulong()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True                               # 查不到退出码：保守当活着
        return code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def is_running(name: str = MUTEX_NAME) -> bool:
    """当前是否有实例持有守卫互斥体（探测后立即归还，不占锁）。"""
    if _k32 is None:
        return False
    handle = _k32.CreateMutexW(None, False, name)
    existed = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    if handle:
        _k32.CloseHandle(handle)          # 只探测：无论新建与否都放手
    return existed


def probe_instances(ports: collections.abc.Sequence[int] = (), name: str = MUTEX_NAME,
                    lock_file: Path | None = None) -> list[dict]:
    """探测可能占用单例的进程（不杀）：锁文件存活 pid + 端口 LISTENING owner。

    返回 ``[{"pid": int, "source": str}]``；另附一条 ``pid=0`` 的互斥体哨兵
    （仅提示"平台在跑"，无 pid 可杀时用）。
    """
    found: dict[int, str] = {}

    def _add(pid: int, source: str) -> None:
        if pid and pid != os.getpid():
            found.setdefault(int(pid), source)

    info = _read_lock(lock_file or LOCK_FILE)
    pid = info.get("pid")
    if pid and _alive(int(pid)):
        desc = f"锁文件 mode={info.get('mode', '?')}"
        if info.get("port"):
            desc += f"，端口={info['port']}"
        _add(int(pid), desc)
    for port in ports:
        for owner in _port_owner(port):
            _add(owner, f"端口 {port} LISTENING")

    items = [{"pid": p, "source": s} for p, s in found.items()]
    if is_running(name) and not items:
        items.append({"pid": 0, "source": "互斥体被占用（pid 未知，可能是旧版实例）"})
    return items


def cleanup_instances(ports: collections.abc.Sequence[int] = (), name: str = MUTEX_NAME,
                      lock_file: Path | None = None,
                      wait: float = 5.0) -> list[dict]:
    """定向清理残留实例（锁文件 pid + 端口 owner），最多等 ``wait`` 秒释放互斥。

    返回探测列表，逐项补 ``killed``（清理后仍存活为 False）。无目标时为 ``[]``。
    """
    if sys.platform != "win32":
        return []
    targets = probe_instances(ports, name=name, lock_file=lock_file)
    for item in targets:
        if item["pid"]:
            _kill(item["pid"])

    for item in targets:
        if not item["pid"]:                           # 哨兵：无 pid 可杀
            item["killed"] = False
            continue
        deadline = time.monotonic() + wait
        while _alive(item["pid"]) and time.monotonic() < deadline:
            time.sleep(0.2)
        item["killed"] = not _alive(item["pid"])
    return targets


def _create_mutex(name: str) -> bool:
    """尝试持有命名互斥体；True=本进程首个持有者。"""
    global _MUTEX_HANDLE
    if _k32 is None:
        return True
    handle = _k32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(handle)      # 已有实例：不留多余句柄
        return False
    _MUTEX_HANDLE = handle
    return True


def _read_lock(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_lock(path: Path, mode: str, port: int | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": os.getpid(), "mode": mode, "port": port,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _port_owner(port: int) -> list[int]:
    """端口 LISTENING 的 pid 列表（与 serve_main.bat 同逻辑的 Python 版）。"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
            shell=True, capture_output=True, text=True, timeout=10)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = int(parts[-1])
                if pid not in pids:
                    pids.append(pid)
    except Exception:  # noqa: BLE001
        pass
    return pids


def _kill(pid: int) -> None:
    try:
        subprocess.run(f"taskkill /F /T /PID {pid}", shell=True,
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_lock(path: Path) -> None:
    try:
        if _read_lock(path).get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def ensure_single(mode: str, port: int | None = None, restart: bool = False,
                  lock_file: Path | None = None, name: str = MUTEX_NAME) -> str | None:
    """单例守卫：``None`` = 可启动；返回字符串 = 已有实例（调用方应打印并退出）。

    Args:
        mode: ``"web"`` / ``"desktop"``
        port: Web 模式端口（用于端口占用兜底与 ``--restart`` 清理）
        restart: True 时先清理旧实例（锁文件 pid + 端口 owner），最多等 5s
        lock_file / name: 供测试注入（默认生产值）
    """
    if sys.platform != "win32":
        return None
    lf = lock_file or LOCK_FILE

    if not _create_mutex(name):
        info = _read_lock(lf)
        desc = f"mode={info.get('mode', '?')}"
        if info.get("port"):
            desc += f"，端口={info['port']}"
        if info.get("pid"):
            desc += f"，PID={info['pid']}"
        if not restart:
            return (f"CarTest 已在运行（{desc}）。如需重启：加 --restart，"
                    f"或使用 serve_main.bat")

        # --restart：定点清理（锁文件 pid + 端口 owner）
        targets: list[int] = []
        if info.get("pid"):
            targets.append(int(info["pid"]))
        if mode == "web" and port:
            targets.extend(_port_owner(port))
        for pid in dict.fromkeys(targets):
            _kill(pid)

        acquired = False
        for _ in range(20):                      # 最多等 5 秒释放互斥
            time.sleep(0.25)
            if _create_mutex(name):
                acquired = True
                break
        if not acquired:
            return "重启失败：旧实例未在 5 秒内退出"

    # 持锁成功：Web 模式再做端口兜底（旧版实例无守卫 / 他程序占用）
    if mode == "web" and port:
        owners = _port_owner(port)
        if owners and not restart:
            return (f"端口 {port} 已被占用（PID {', '.join(map(str, owners))}）——"
                    f"可能是旧版实例或其它程序；请加 --restart 或先关闭后重试")
        if owners and restart:
            for pid in owners:
                _kill(pid)
            for _ in range(12):                  # 最多等 3 秒释放端口
                time.sleep(0.25)
                if not _port_owner(port):
                    break

    _write_lock(lf, mode, port)
    atexit.register(_cleanup_lock, lf)
    return None
