# -*- coding: utf-8 -*-
"""CarTest ui — 壳组件工厂（克制的 Flet 控件构建器）

只做"外观 + 回调"，不含业务；颜色一律取 ``AppTheme.c()``。
设计依据：``14-UI重构设计.md`` §5；
导航按钮突出显示＝2026-09-16 用户反馈；内边距加大＝同日追加反馈。
"""
from __future__ import annotations

from typing import Any, Callable

import flet as ft


def _update_safe(ctl) -> None:
    try:
        ctl.update()
    except Exception:  # 未挂载时（测试/构建阶段）无 UI 可刷，属预期
        pass


def sync_text_value(e) -> None:
    """``ft.TextField`` 的 ``on_change`` 占位回调——**每个输入框都必须挂**

    发现 #14（2026-09-18 用户反馈）：flet 0.85 Web 端只有在控件**有 on_change**
    时才把用户输入回传服务端；没有回调的 TextField，服务端 ``control.value``
    永远是构建时的旧值 → 「APK 路径框输入后服务端读不到值，点安装/推送提示请先填写」。

    因此：凡是要被后续逻辑读取的 TextField，一律 ``on_change=sync_text_value``
    （值本身由 flet 写入 ``control.value``，这里无需额外处理）；
    ``tests/test_textfield_sync_guard.py`` 会静态拦住漏挂的输入框。
    """


def nav_button(t, icon: Any, label: str, selected: bool,
               on_click: Callable) -> ft.Container:
    """侧栏导航按钮：选中＝实心主色底 + 白字加粗；舒适内边距。"""
    fg = t.c("text_on_accent") if selected else t.c("text_secondary")
    btn = ft.Container(
        content=ft.Row([
            ft.Icon(icon, size=16, color=fg),
            ft.Text(label, size=12, color=fg,
                    weight=ft.FontWeight.BOLD if selected else ft.FontWeight.NORMAL),
        ], spacing=10),
        height=38,
        border_radius=8,
        padding=ft.padding.Padding(left=12, right=10),
        bgcolor=t.c("accent") if selected else None,
        on_click=on_click,
        ink=True,
    )

    def _hover(e) -> None:
        if selected:
            return
        btn.bgcolor = t.c("sel_bg") if e.data == "true" else None
        _update_safe(btn)

    btn.on_hover = _hover
    return btn


def chip(t, text: str, color_key: str = "text_secondary",
         bg_key: str | None = None, on_click: Callable | None = None,
         active: bool = False) -> ft.Container:
    """胶囊标签（状态 / 计数）。

    active=选中态（sel_bg 底 + accent 文本加粗 + accent 描边）；on_click 时启用点击。
    """
    if active:
        color_key, bg_key = "accent_text", "sel_bg"
    return ft.Container(
        content=ft.Text(text, size=11, color=t.c(color_key),
                        weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL),
        padding=ft.padding.Padding(left=10, right=10, top=3, bottom=3),
        border=ft.border.Border.all(1, t.c("accent") if active else t.c("border")),
        border_radius=999,
        bgcolor=t.c(bg_key) if bg_key else None,
        on_click=on_click,
        ink=on_click is not None,
    )
