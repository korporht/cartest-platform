# -*- coding: utf-8 -*-
"""CarTest — 统一日志初始化（debug 调试日志）

背景：本项目多次被「静默失败」咬伤（后台线程异常只留在没人看的地方，
见 03-开发方法论.md 的红队角色③）。本模块把 stdlib ``logging`` 接到固定落点，
让模块级 logger 的输出（开发期全部 DEBUG）随时可查：

- 文件：``logs/debug.log``（DEBUG 全量，1MB × 3 轮转，UTF-8）
- 控制台：INFO+（stdout；GUI 的 bat 启动器会重定向到 ``logs\\gui_dev.log``）
- 幂等：重复调用只配置一次；不改动任何既有 ``print`` 输出

用法（各入口调一次）::

    from cartest.core import logsetup
    logsetup.setup()
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import LOGS_DIR

_CONFIGURED = False

#: 统一格式：时间 级别 [线程] 模块: 消息（线程名用于定位后台线程静默失败）
_FMT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup(level_file: int = logging.DEBUG, level_console: int = logging.INFO) -> Path:
    """初始化根 logger；返回 debug 日志文件路径（重复调用幂等）。"""
    global _CONFIGURED
    log_file = LOGS_DIR / "debug.log"
    if _CONFIGURED:
        return log_file

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(level_file)
    fh.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(level_console)
    ch.setFormatter(logging.Formatter(_FMT))
    root.addHandler(ch)

    # flet 内部（controls/transport）DEBUG 极吵（实测占 debug.log 96%+），
    # 压制到 WARNING，让 debug.log 聚焦业务日志；flet 的告警/错误仍可见
    for name in ("flet", "flet_controls", "flet_transport",
                 "flet_fastapi", "flet_desktop", "flet_web"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger("logsetup").info("debug 日志已启用: %s", log_file)
    return log_file
