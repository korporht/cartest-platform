"""CarTest core — 日志解析

- ``live.py``    实时日志解析（logcat 行 → 结构化字段 + 时间邻近匹配）
- ``offline.py`` 离线全量提取（调用 CbmLogextractor，保持其独立不搬代码）

两者职责区分：
  ``live`` 面向"一条用例一次会话"的实时判定（窗口 10s/30s）；
  ``offline`` 面向"整目录日志的事后复盘"（复用 CbmLogextractor 的成熟实现）。
"""
from . import live, offline

__all__ = ["live", "offline"]
