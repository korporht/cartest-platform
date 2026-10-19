# -*- coding: utf-8 -*-
"""CarTest app — 测试流程页（三视图壳：执行 / 结果 / 历史）

2026-10-12 起归入侧栏「通用工具」组（原「项目定制」组已取消）。
三视图组件零改动复用（``pipeline`` / ``results`` / ``history``）；页内切换 = 容器替换，
切换前执行与 ``main._detach_page`` 等同的清理（防陈旧闭包驱动已卸载控件树）。
"""
from __future__ import annotations

import flet as ft

from ..state import AppState
from ..ui import components
from . import history, pipeline, results

#: 视图 key → (标签, build 函数)
VIEWS: list[tuple] = [
    ("pipeline", "执行", pipeline.build),
    ("results", "结果", results.build),
    ("history", "历史", history.build),
]

_VIEW_BUILDS = {k: fn for k, _, fn in VIEWS}


def _safe_update(ctl) -> None:
    try:
        ctl.update()
    except Exception:  # noqa: BLE001
        pass


def build(page: ft.Page, state: AppState) -> ft.Control:
    """三视图壳：chips 切换 + 内容区（默认视图取 ``state.active_view``）"""
    if state.active_view not in _VIEW_BUILDS:
        state.active_view = VIEWS[0][0]

    holder = ft.Container(expand=True)
    chips_row = ft.Row(spacing=8)

    def _detach() -> None:
        """视图切换前清理（等价 main._detach_page；防陈旧回调驱动已卸载控件）"""
        if state.before_page_leave:
            try:
                state.before_page_leave()
            except Exception:  # noqa: BLE001
                pass
            state.before_page_leave = None
        state.log_target = None
        state.on_running_change = None
        state.tick_hooks = []

    def _render_view() -> None:
        holder.content = _VIEW_BUILDS[state.active_view](page, state)
        _safe_update(holder)

    def _rebuild_chips() -> None:
        chips_row.controls = [
            components.chip(state.theme, label,
                            on_click=(lambda e, k=k: _switch(k)),
                            active=(k == state.active_view))
            for k, label, _ in VIEWS
        ]
        _safe_update(chips_row)

    def _switch(view: str) -> None:
        if view not in _VIEW_BUILDS or view == state.active_view:
            return
        _detach()
        state.active_view = view
        _rebuild_chips()
        _render_view()

    state.goto_view = _switch

    _rebuild_chips()
    _render_view()

    return ft.Column([chips_row, holder], spacing=8, expand=True)
