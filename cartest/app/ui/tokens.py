# -*- coding: utf-8 -*-
"""CarTest ui — 设计令牌（双主题调色板）

设计依据：``14-UI重构设计.md`` §4.1（S3 深浅双生）。

约定：
  · 全部键为语义键；``theme.AppTheme.c(key)`` 直接读取；
  · **兼容键**（bg_card/bg_sidebar/bg_topbar/indicator_color/bg_log/search_*）
    值与对应语义键一致——工具页旧消费零改动；待全部页面迁移后另行删除（债务）。
"""
from __future__ import annotations

#: 深色（默认）
PALETTE_DARK: dict[str, str] = {
    # ── 语义键 ──
    "bg_base": "#1B2130",
    "bg_chrome": "#202839",
    "bg_panel": "#232B3D",
    "bg_console": "#12161F",
    "border": "#303A52",
    "divider": "#262F44",
    "text_primary": "#E5E9F0",
    "text_secondary": "#8A94A8",
    "text_hint": "#5B6B85",
    "text_disabled": "#4A5468",
    "accent": "#3B82F6",
    "accent_hover": "#5B9BFB",
    "accent_text": "#8FB6FF",
    "sel_bg": "#2A3550",
    "text_on_accent": "#FFFFFF",
    "success": "#6EE7B7",
    "danger": "#FCA5A5",
    "warning": "#FCD34D",
    "console_text": "#C8D0E0",
    # ── 日志着色 ──
    "log_text": "#C8D0E0",
    "log_ok": "#6EE7B7",
    "log_error": "#FCA5A5",
    "log_warn": "#FCD34D",
    "log_info": "#8FB6FF",
    "log_cmd": "#8A94A8",
    "log_section": "#8FB6FF",
    # ── 操作日志面板（深色 = 恒暗控制台）──
    "bg_oplog": "#1E2027",
    "oplog_text": "#C9D1E0",
    "oplog_ts": "#6B7A94",
    "oplog_op": "#4C8DFF",
    "oplog_op_bg": "#24304A",
    "oplog_cmd": "#7FB3FF",
    "oplog_info": "#8FA3BF",
    "oplog_ok": "#4CAF50",
    "oplog_warn": "#FFC107",
    "oplog_error": "#F44336",
    "oplog_error_bg": "#3A1D1F",
    "oplog_raw": "#5B6B85",
    "oplog_tag": "#7E9CC4",
    # ── 阶段状态 ──
    "stage_pending": "#5B6B85",
    "stage_running": "#3B82F6",
    "stage_done": "#6EE7B7",
    "stage_failed": "#FCA5A5",
    "stage_skipped": "#4A5468",
    # ── 兼容键（迁移期保留；与语义键等价）──
    "bg_card": "#232B3D",
    "bg_sidebar": "#202839",
    "bg_topbar": "#202839",
    "bg_log": "#12161F",
    "indicator_color": "#2A3550",
    "text_on_sidebar": "#8A94A8",
    "search_shell": "#232B3D",
    "search_border": "#303A52",
    "search_icon": "#8A94A8",
    "search_btn": "#3B82F6",
    "search_btn_hover": "#5B9BFB",
}

#: 浅色
PALETTE_LIGHT: dict[str, str] = {
    "bg_base": "#F7F9FC",
    "bg_chrome": "#FFFFFF",
    "bg_panel": "#FFFFFF",
    "bg_console": "#10151F",
    "border": "#E3E8F0",
    "divider": "#EDF1F7",
    "text_primary": "#16203A",
    "text_secondary": "#5B6B85",
    "text_hint": "#8A94A8",
    "text_disabled": "#B8C2D4",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_text": "#2563EB",
    "sel_bg": "#EAF1FF",
    "text_on_accent": "#FFFFFF",
    "success": "#059669",
    "danger": "#DC2626",
    "warning": "#B45309",
    "console_text": "#C8D0E0",
    "log_text": "#C8D0E0",
    "log_ok": "#6EE7B7",
    "log_error": "#FCA5A5",
    "log_warn": "#FCD34D",
    "log_info": "#60A5FA",
    "log_cmd": "#8A94A8",
    "log_section": "#60A5FA",
    # ── 操作日志面板（浅色 = 浅底控制台；2026-09-18 用户反馈：
    #    明亮模式下暗底 #1E2027 与整页对比过强、白底开关/标签看不清）──
    "bg_oplog": "#F1F4FA",
    "oplog_text": "#2A3446",
    "oplog_ts": "#8A94A8",
    "oplog_op": "#1D4ED8",
    "oplog_op_bg": "#E3EBFA",
    "oplog_cmd": "#1E40AF",
    "oplog_info": "#5B6B85",
    "oplog_ok": "#047857",
    "oplog_warn": "#B45309",
    "oplog_error": "#DC2626",
    "oplog_error_bg": "#FDECEC",
    "oplog_raw": "#94A3B8",
    "oplog_tag": "#2563EB",
    "stage_pending": "#8A94A8",
    "stage_running": "#2563EB",
    "stage_done": "#059669",
    "stage_failed": "#DC2626",
    "stage_skipped": "#B8C2D4",
    "bg_card": "#FFFFFF",
    "bg_sidebar": "#FFFFFF",
    "bg_topbar": "#FFFFFF",
    "bg_log": "#10151F",
    "indicator_color": "#EAF1FF",
    "text_on_sidebar": "#5B6B85",
    "search_shell": "#FFFFFF",
    "search_border": "#E3E8F0",
    "search_icon": "#8A94A8",
    "search_btn": "#2563EB",
    "search_btn_hover": "#1D4ED8",
}

#: 必须成对存在的核心语义键（测试用）
CORE_KEYS: list[str] = [
    "bg_base", "bg_chrome", "bg_panel", "bg_console", "border", "divider",
    "text_primary", "text_secondary", "text_hint", "accent", "accent_hover",
    "accent_text", "sel_bg", "text_on_accent", "success", "danger", "warning",
    "console_text",
]
