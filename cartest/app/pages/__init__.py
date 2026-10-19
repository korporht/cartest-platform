"""CarTest app — 页面注册表

含「配置」页（P3c）与工具页（``app.tools``，合并页）。

2026-10-19 用户裁决：删除「测试流程」页（原 UI-4 T5 三视图壳：执行/结果/历史，
复用 ``pipeline`` / ``results`` / ``history`` 三模块）。语音灌入的循环/顺序执行
取代了整表流水线 UI 入口；core ``cartest.pipeline``（CLI ``cartest run``）不受影响。
"""
from ..tools import pages as tool_pages
from . import settings

#: 页面 key → build(page, state) -> Control
BUILDERS = {
    "settings": settings.build,
    **tool_pages.BUILDERS,
}

__all__ = ["BUILDERS", "settings", "tool_pages"]
