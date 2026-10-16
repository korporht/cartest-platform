# -*- coding: utf-8 -*-
"""测试临时目录 —— ``mkdtemp`` + 进程退出自动回收

**背景**：仓库里二十来个测试用裸 ``tempfile.mkdtemp()`` 建临时目录却从不删除，
每跑一次 ``tools/check_all.py`` 就往 ``%TEMP%`` 里累积一批目录（实测已堆积 128 个，
前缀五花八门：``p3d2_single_*`` / ``ext_tools_*`` / ``judge_rules_*`` …）。

本模块把「建目录」与「回收」绑在一起，用法与标准库完全一致::

    from _tmpdir import mkdtemp

    tmp = Path(mkdtemp(prefix="p3d2_single_"))

回收挂在 ``atexit``：``check_all.py`` 用 ``subprocess`` 逐个跑测试，每个测试是
**独立进程**，因此正常退出、``sys.exit(1)`` 的失败路径、未捕获异常都会触发回收。

**为什么不用 ``with tempfile.TemporaryDirectory()``**（仓库既有写法）：
那个写法要求调用方有可包裹的作用域，而本仓库有几种结构不适用 ——
``test_app_state.py`` 是在被反复调用的辅助函数里建目录、``test_ui_results.py``
一次建两个、``test_app_consistency.py`` 建在模块级辅助里。用本模块则**调用点一字不改**，
二十二个文件改法完全一致。

清理失败静默忽略（``ignore_errors``）：Windows 上刚被杀掉的子进程可能短暂占着目录
句柄，回收失败不该盖住测试本身的结论。
"""
from __future__ import annotations

import atexit
import shutil
import tempfile as _tempfile

__all__ = ["mkdtemp"]


def mkdtemp(prefix: str = "cartest_", *args, **kwargs) -> str:
    """建临时目录并登记进程退出时回收；返回路径（同 ``tempfile.mkdtemp``）。"""
    path = _tempfile.mkdtemp(prefix=prefix, *args, **kwargs)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path
