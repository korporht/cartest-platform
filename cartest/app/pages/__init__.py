"""CarTest app — 页面注册表

含「测试流程」页（UI-4 T5 三视图壳：执行/结果/历史）、
工具页（``app.tools``，合并页）与「配置」页（P3c）。
"""
from ..tools import pages as tool_pages
from . import history, pipeline, results, settings, testflow

#: 页面 key → build(page, state) -> Control
BUILDERS = {
    "testflow": testflow.build,
    "settings": settings.build,
    **tool_pages.BUILDERS,
}

__all__ = ["BUILDERS", "history", "pipeline", "results", "settings", "testflow", "tool_pages"]
