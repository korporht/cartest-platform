# -*- coding: utf-8 -*-
"""CarTest app — 全局操作日志（动作完成事件汇总）

工具页耗时动作完成后，除顶部**强提示**浮层外，还在此登记一条「完成 / 失败」事件，
供顶栏「操作日志」抽屉集中回看（跨页汇总，不占各工具页版面）。

与 ``tools/widgets/oplog.py`` 的区别：
  · ``OplogModel`` = **单页流式日志**（绑定 ``reports_dir``、含 op/子行/导出），
    服务于页内事件窗口；
  · ``ActivityLog`` = **跨页离散事件汇总**（时间 / 来源 / 动作 / 成败）——
    本模型只关心"做了什么、成没成"，用于一处回看所有页的动作结果。

会话级持有（挂 ``AppState``），与 ``oplog_models`` 同口径；仅内存、不落盘。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

#: 保留条数上限（deque 自动裁剪最旧）
MAX_ITEMS = 500


@dataclass(frozen=True)
class ActivityItem:
    """一条动作完成记录"""

    ts: str        # HH:mm:ss（登记时刻，本地时间）
    source: str    # 来源模块（如 "ADB" / "工具"）
    action: str    # 动作文案（只报结果，如「抓日志完成」）
    ok: bool       # True=成功 / False=失败


class ActivityLog:
    """全局操作日志（线程安全；``deque(maxlen)`` 自动裁剪最旧）"""

    def __init__(self, maxlen: int = MAX_ITEMS) -> None:
        self._items: deque[ActivityItem] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, source: str, action: str, ok: bool = True) -> None:
        """登记一条完成事件（工作线程 / 主线程均可调用）"""
        item = ActivityItem(
            ts=datetime.now().strftime("%H:%M:%S"),
            source=str(source or ""),
            action=str(action or ""),
            ok=bool(ok),
        )
        with self._lock:
            self._items.append(item)

    def items(self) -> list[ActivityItem]:
        """全部记录（**新 → 旧**）"""
        with self._lock:
            return list(reversed(self._items))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
