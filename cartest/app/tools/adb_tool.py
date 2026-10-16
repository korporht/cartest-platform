# -*- coding: utf-8 -*-
"""ADB 工具类 - 纯业务逻辑，不涉及 UI"""
from __future__ import annotations

import atexit
import os
import shlex
import subprocess
import threading
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .constants import ADB_TIMEOUT

_TRACKED_PROCS: Set[subprocess.Popen] = set()
_TRACKED_LOCK = threading.Lock()


def _cleanup_tracked_procs() -> None:
    with _TRACKED_LOCK:
        for proc in _TRACKED_PROCS:
            try:
                proc.terminate()
            except Exception:
                pass
        _TRACKED_PROCS.clear()


atexit.register(_cleanup_tracked_procs)


class ADBTool:
    """ADB 命令执行器"""

    @staticmethod
    def sanitize_shell_arg(arg: str) -> str:
        """对用户输入的参数进行 shell 转义，防止命令注入。

        适用于拼接到 adb shell 命令中的用户输入（路径、包名等）。
        """
        if os.name == "nt":
            return '"' + arg.replace('"', '""') + '"'
        else:
            return shlex.quote(arg)

    @staticmethod
    def run_cmd(
        cmd: str,
        is_binary: bool = False,
        timeout: int = ADB_TIMEOUT,
    ) -> Tuple[bool, Union[str, bytes]]:
        proc_kwargs: Dict[str, Any] = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": timeout,
        }
        if not is_binary:
            proc_kwargs.update({"encoding": "utf-8", "errors": "ignore"})
        try:
            result = subprocess.run(cmd, **proc_kwargs)
            success = result.returncode == 0
            output: Union[str, bytes] = (
                result.stdout if is_binary
                else (result.stdout + result.stderr).strip()
            )
            return success, output
        except subprocess.TimeoutExpired:
            return False, b"" if is_binary else "命令执行超时"
        except Exception as e:
            return False, b"" if is_binary else f"执行异常：{str(e)}"

    @staticmethod
    def run_popen(
        cmd: str,
        encoding: str = "utf-8",
        errors: str = "ignore",
    ) -> subprocess.Popen:
        """返回 Popen 对象用于实时流读取，自动追踪以便退出时清理"""
        proc = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding=encoding, errors=errors,
        )
        with _TRACKED_LOCK:
            _TRACKED_PROCS.add(proc)
        return proc

    @staticmethod
    def open_folder(path: str) -> None:
        """跨平台打开文件夹"""
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        elif os.name == "posix":
            import subprocess as _sp
            try:
                _sp.Popen(["xdg-open", path], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except FileNotFoundError:
                _sp.Popen(["open", path], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
