# -*- coding: utf-8 -*-
"""CarTest app — 全局「操作日志」抽屉

顶栏「操作日志」图标打开的**右侧抽屉**：集中展示所有工具页的动作完成 / 失败事件
（``state.activity``，见 ``cartest/app/activity.py``）。

实现要点（同 ``ui/toast.py`` 的稳妥口径）：
  · **启动建一次 → 挂 ``page.overlay`` → 只切 ``visible``**——规避 flet 0.85
    「动态重建可点控件 → 失效命中区」（35 号文档发现 #11）；
  · 打开时按当前记录**快照**填充只读行（单 ``ft.Text`` + spans，零交互控件）；
  · 遮罩点击 / 关闭按钮 / 清空按钮均为固定控件，一次建好后不改结构。

见 ``39-操作完成反馈与全局操作日志设计.md``。
"""
from __future__ import annotations

import logging

import flet as ft

log = logging.getLogger(__name__)

#: 抽屉宽度
_WIDTH = 420
#: 遮罩底色（半透明黑）
_MASK_BG = "#00000060"


class ActivityPanel:
    """全局操作日志抽屉（一个页面一个实例）"""

    def __init__(self, page, state) -> None:  # noqa: ANN001
        self._page = page
        self._state = state
        self._title = ft.Text("操作日志", size=14, weight=ft.FontWeight.BOLD)
        self._count = ft.Text("", size=11)
        self._list = ft.ListView(expand=True, spacing=2, auto_scroll=False,
                                 padding=ft.Padding(left=2, right=2, top=4, bottom=4))
        self._clear_btn = ft.TextButton("清空", on_click=lambda e: self._on_clear())
        self._close_btn = ft.IconButton(ft.Icons.CLOSE, icon_size=18,
                                        on_click=lambda e: self.close())
        self._icon = ft.Icon(ft.Icons.HISTORY, size=18)
        self._panel = ft.Container(
            content=ft.Column([
                ft.Row([self._icon, self._title, self._count,
                        ft.Container(expand=True), self._clear_btn, self._close_btn],
                       spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Divider(height=1),
                self._list,
            ], spacing=8, expand=True),
            width=_WIDTH,
            padding=ft.Padding(left=14, right=14, top=12, bottom=12),
        )
        # 遮罩（占满抽屉左侧剩余区域，点击关闭）；面板自身吞掉点击避免误关
        self._mask = ft.Container(expand=True, bgcolor=_MASK_BG,
                                  on_click=lambda e: self.close())
        self._panel.on_click = lambda e: None
        self.control = ft.Container(
            content=ft.Row([self._mask, self._panel], spacing=0,
                           vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            top=0, left=0, right=0, bottom=0,
            visible=False,
        )

    # ── 装配 ──
    def attach_to(self, page, host=None) -> None:  # noqa: ANN001
        """挂载浮层：优先挂**主树 Stack**（flet 0.85 Web 端 page.overlay 子树
        toggling visible 后不重绘——真机点验 2026-10-14）；host=None 回退 overlay。"""
        (host if host is not None else page.overlay).append(self.control)
        page.update()

    # ── 开关 ──
    def open(self) -> None:  # noqa: ANN201
        """打开抽屉（按当前记录快照填充；主线程调用）"""
        try:
            self._apply_theme()
            self._fill()
            self.control.visible = True
            self._update()
        except Exception as ex:  # noqa: BLE001
            log.warning("操作日志抽屉打开失败: %r", ex)

    def close(self) -> None:
        self.control.visible = False
        self._update()

    def toggle(self) -> None:
        if self.control.visible:
            self.close()
        else:
            self.open()

    def _on_clear(self) -> None:
        """清空全局操作日志并即时刷新（面板已打开）"""
        try:
            self._state.activity.clear()
            self._fill()
            self._update()
        except Exception as ex:  # noqa: BLE001
            log.warning("操作日志清空失败: %r", ex)

    # ── 渲染 ──
    def _apply_theme(self) -> None:
        t = self._state.theme
        self._title.color = t.c("text_primary")
        self._count.color = t.c("text_hint")
        self._icon.color = t.c("accent")
        self._panel.bgcolor = t.c("bg_panel")
        self._panel.border = ft.border.Border.only(
            left=ft.border.BorderSide(1, t.c("border")))

    def _fill(self) -> None:
        t = self._state.theme
        items = self._state.activity.items()
        self._list.controls.clear()
        if not items:
            self._list.controls.append(ft.Container(
                content=ft.Text("暂无记录", size=12, color=t.c("text_hint")),
                padding=ft.Padding(left=4, top=12, right=4, bottom=4)))
            self._count.value = ""
        else:
            for it in items:
                self._list.controls.append(self._line(it, t))
            self._count.value = f"{len(items)} 条"

    @staticmethod
    def _line(item, t) -> ft.Text:  # noqa: ANN001
        """单行（只读 Text + spans；无交互控件）"""
        ok = getattr(item, "ok", True)
        spans = [
            ft.TextSpan(f"{item.ts}  ", style=ft.TextStyle(size=11, color=t.c("text_hint"))),
            ft.TextSpan("✓ " if ok else "✕ ",
                        style=ft.TextStyle(size=12,
                                           color=t.c("success" if ok else "danger"),
                                           weight=ft.FontWeight.BOLD)),
        ]
        if getattr(item, "source", ""):
            spans.append(ft.TextSpan(f"[{item.source}] ",
                                     style=ft.TextStyle(size=11, color=t.c("accent_text"))))
        spans.append(ft.TextSpan(item.action,
                                 style=ft.TextStyle(size=12, color=t.c("text_primary"))))
        return ft.Text(spans=spans)

    def _update(self) -> None:
        """刷新抽屉（overlay 子树；整页 update 保证 visible/值变更必达前端）"""
        try:
            self._page.update()
        except Exception:  # noqa: BLE001
            try:
                self.control.update()
            except Exception:  # noqa: BLE001
                pass


def attach(page, state, host=None) -> ActivityPanel:  # noqa: ANN001
    """创建并装配抽屉（返回 panel；main.py 组装时调用一次）

    ``host``：主树根 Stack 的 controls 列表（推荐，见 :meth:`attach_to`）。
    同时注册 ``state.open_activity`` 供顶栏按钮调用（顶栏会因主题切换重建，
    故经 state 回调而非直接闭包引用）。
    """
    panel = ActivityPanel(page, state)
    try:
        panel.attach_to(page, host)
    except Exception as ex:  # noqa: BLE001
        log.warning("操作日志抽屉装配失败: %r", ex)
    try:
        state.open_activity = panel.open
    except Exception as ex:  # noqa: BLE001
        log.warning("操作日志回调注册失败: %r", ex)
    return panel
