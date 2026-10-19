# -*- coding: utf-8 -*-
"""CarTest app — 全局「任务」抽屉（40 号设计 §七）

顶栏运行胶囊（或任务图标）打开的**右侧抽屉**：列出所有活动任务
（``state.tasks`` registry：运行中 + 排队中），每行带「停止」按钮；
流水线运行时也在此显示（与运行胶囊同源）。

实现要点（同 ``ui/activity_panel.py`` 的稳妥口径，规避 flet 0.85 坑）：
  · **启动建一次 → 挂 ``page.overlay`` → 只切 ``visible``**——动态重建可点控件
    会留「失效命中区」（35 号文档发现 #11），故行槽**固定 8 个、只改值与可见性**；
  · 打开 / tick 时按 registry 快照填充（只改 Text 值 + 切行 visible + 按钮禁用）；
  · 「停止」运行中走 ``registry.stop(key)``，排队项走 ``device_queue.cancel(key)``。

见 ``40-任务跨页存活与设备操作队列设计.md``。
"""
from __future__ import annotations

import logging

import flet as ft

log = logging.getLogger(__name__)

#: 抽屉宽度
_WIDTH = 380
#: 遮罩底色（半透明黑）
_MASK_BG = "#00000060"
#: 行槽上限（活动任务超过此数截断展示；实际场景远达不到）
_MAX_ROWS = 8


class TaskPanel:
    """全局任务抽屉（一个页面一个实例）"""

    def __init__(self, page, state) -> None:  # noqa: ANN001
        self._page = page
        self._state = state
        self._title = ft.Text("任务", size=14, weight=ft.FontWeight.BOLD)
        self._count = ft.Text("", size=11)
        self._icon = ft.Icon(ft.Icons.ALT_ROUTE, size=18)
        self._close_btn = ft.IconButton(ft.Icons.CLOSE, icon_size=18,
                                        on_click=lambda e: self.close())
        self._empty = ft.Text("暂无活动任务", size=12)
        self._rows: list[dict] = []
        for idx in range(_MAX_ROWS):
            icon = ft.Text("◐", size=12)
            name = ft.Text("", size=12, weight=ft.FontWeight.BOLD, expand=True)
            detail = ft.Text("", size=11)
            # 两个按钮互斥 visible（flet 0.85：TextButton 构造后改 .text 不重绘）
            stop_btn = ft.TextButton("停止", on_click=lambda e, i=idx: self._on_stop(i))
            cancel_btn = ft.TextButton("取消", on_click=lambda e, i=idx: self._on_stop(i),
                                       visible=False)
            row = ft.Container(
                content=ft.Row([
                    icon,
                    ft.Column([name, detail], spacing=1, expand=True),
                    stop_btn, cancel_btn,
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding(left=4, right=4, top=4, bottom=4),
                border_radius=8,
                visible=False,
            )
            self._rows.append({"row": row, "icon": icon, "name": name,
                               "detail": detail, "btn": stop_btn,
                               "btn_cancel": cancel_btn, "key": ""})
        self._panel = ft.Container(
            content=ft.Column([
                ft.Row([self._icon, self._title, self._count,
                        ft.Container(expand=True), self._close_btn],
                       spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Divider(height=1),
                self._empty,
                *[r["row"] for r in self._rows],
            ], spacing=6, expand=True),
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
        """挂载浮层：优先挂**主树 Stack**（host 传 main.py 根 Stack.controls）——
        flet 0.85 Web 端 ``page.overlay`` 子树 toggling ``visible`` 后**不重绘**
        （真机点验 2026-10-14：语义/命中区在、像素不出）；主树控件绘制可靠。
        host=None 时回退 page.overlay（旧行为）。"""
        (host if host is not None else page.overlay).append(self.control)
        page.update()

    # ── 开关 ──
    def open(self) -> None:  # noqa: ANN201
        """打开抽屉（按 registry 快照填充；主线程调用）"""
        try:
            self._apply_theme()
            self._fill()
            self.control.visible = True
            self._update()
        except Exception as ex:  # noqa: BLE001
            log.warning("任务抽屉打开失败: %r", ex)

    def close(self) -> None:
        self.control.visible = False
        self._update()

    def toggle(self) -> None:
        if self.control.visible:
            self.close()
        else:
            self.open()

    def on_tick(self, ticks: int = 0) -> None:
        """tick 钩子：抽屉打开时跟随刷新（关着时零开销）"""
        if not self.control.visible:
            return
        try:
            self._fill()
            self._update()
        except Exception as ex:  # noqa: BLE001
            log.warning("任务抽屉刷新失败: %r", ex)

    # ── 停止 / 取消 ──
    def _on_stop(self, idx: int) -> None:
        key = self._rows[idx]["key"]
        if not key:
            return
        reg = getattr(self._state, "tasks", None)
        stopped = reg.stop(key) if reg is not None else False
        if not stopped:
            # 运行中停止失败 / 排队项 → 走队列取消
            queue = getattr(self._state, "device_queue", None)
            if queue is not None:
                try:
                    queue.cancel(key)
                except Exception:  # noqa: BLE001
                    pass
        self._fill()
        self._update()

    # ── 渲染 ──
    def _apply_theme(self) -> None:
        t = self._state.theme
        self._title.color = t.c("text_primary")
        self._count.color = t.c("text_hint")
        self._empty.color = t.c("text_hint")
        self._icon.color = t.c("accent")
        self._panel.bgcolor = t.c("bg_panel")
        self._panel.border = ft.border.Border.only(
            left=ft.border.BorderSide(1, t.c("border")))

    def _fill(self) -> None:
        """按 registry 快照填充行槽（只改值 / visible / disabled）"""
        t = self._state.theme
        reg = getattr(self._state, "tasks", None)
        items: list = []
        if reg is not None:
            try:
                items = reg.snapshot()
            except Exception:  # noqa: BLE001
                items = []
        items = items[:_MAX_ROWS]
        self._empty.visible = not items
        self._empty.color = t.c("text_hint")
        self._count.value = f"{len(items)} 项" if items else ""
        for i, slot in enumerate(self._rows):
            if i >= len(items):
                slot["row"].visible = False
                slot["key"] = ""
                continue
            h = items[i]
            queued = bool(h.get("queued"))
            running = bool(h.get("running"))
            slot["key"] = str(h.get("key") or "")
            slot["name"].value = str(h.get("label") or h.get("key") or "")
            if queued:
                slot["icon"].value = "⋯"
                slot["icon"].color = t.c("text_hint")
                slot["detail"].value = "排队中（前序设备任务完成后自动执行）"
                slot["btn"].visible = False
                slot["btn_cancel"].visible = True
            else:
                slot["icon"].value = "◐"
                slot["icon"].color = t.c("accent")
                detail = str(h.get("detail") or "")
                if h.get("kind") == "device" and h.get("device"):
                    detail = f"[{h['device']}] {detail}".strip()
                slot["detail"].value = detail or "运行中"
                slot["btn"].visible = True
                slot["btn_cancel"].visible = False
                slot["btn"].disabled = not running
            slot["row"].visible = True
            slot["row"].bgcolor = t.c("bg_card")

    def _update(self) -> None:
        """刷新抽屉（overlay 子树；整页 update 保证 visible/值变更必达前端）"""
        try:
            self._page.update()
        except Exception:  # noqa: BLE001
            try:
                self.control.update()
            except Exception:  # noqa: BLE001
                pass


def attach(page, state, host=None) -> TaskPanel:  # noqa: ANN001
    """创建并装配任务抽屉（返回 panel；main.py 组装时调用一次）

    ``host``：主树根 Stack 的 controls 列表（推荐，见 :meth:`attach_to`）。
    同时注册 ``state.open_tasks`` 供顶栏胶囊调用，并把 ``panel.on_tick``
    挂进 ``state.tick_hooks``（打开期间跟随刷新）。
    """
    panel = TaskPanel(page, state)
    try:
        panel.attach_to(page, host)
    except Exception as ex:  # noqa: BLE001
        log.warning("任务抽屉装配失败: %r", ex)
    try:
        state.open_tasks = panel.open
        # 挂应用级钩子（state.ui_hooks 切页不清空；tick_hooks 会被 _detach_page 整表替换）
        hooks = getattr(state, "ui_hooks", None)
        if hooks is None:
            hooks = getattr(state, "tick_hooks", None)
        if hooks is not None and panel.on_tick not in hooks:
            hooks.append(panel.on_tick)
    except Exception as ex:  # noqa: BLE001
        log.warning("任务抽屉回调注册失败: %r", ex)
    return panel
