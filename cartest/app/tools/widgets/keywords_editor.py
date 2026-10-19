# -*- coding: utf-8 -*-
"""关键词标签编辑器 — 一行一词 → 可点删的标签块（配置页用）

2026-09-21 用户反馈：*"这里关键词是什么意思？可以采用其他方案显示吗？易改易读"*。
原方案 = 多行文本框（44 个词连成一坨，看不出边界、删一个要小心退格）。改为：

  · **标签块**：每个词一枚胶囊，右侧 `×` 一点即删（误删可「恢复推荐词」）
  · **添加**：输入框回车 / 点「添加」（自动去重、去空白）
  · **推荐词**：`恢复推荐词` 一键回到项目档案里的值
  · **计数**：标题右侧显示「N 个词」，便于确认批量改动的结果

Editor 协议与配置页其它编辑器一致：``build / validate / get_value / set_value``。
"""
from __future__ import annotations

import flet as ft

from ...ui.components import sync_text_value


class KeywordsEditor:
    """关键词标签编辑器（``TYPE_TAGS``）：值 = ``list[str]``，空列表 → ``None``（跟随档案）"""

    def __init__(self, value, theme, *, hint: str = "输入关键词后回车添加",
                 recommended=None, on_change=None) -> None:
        self.t = theme
        self._hint = hint
        self._on_change = on_change        # 点击类改动（加/删/恢复推荐）即时落盘回调
        self._recommended = [str(v) for v in (recommended or [])]
        self._tags: list[str] = [str(v) for v in (value or []) if str(v).strip()]
        self._input = ft.TextField(
            hint_text=hint, dense=True, text_size=11, expand=True,
            border_radius=8, content_padding=ft.padding.Padding(
                left=10, top=6, right=10, bottom=6),
            color=theme.c("text_primary"), bgcolor=theme.c("bg_panel"),
            border_color=theme.c("border"),
            hint_style=ft.TextStyle(size=11, color=theme.c("text_hint")),
            on_change=sync_text_value,           # 发现 #14：必须挂 on_change
            on_submit=self._add)
        self._wrap = ft.Row(spacing=6, wrap=True, run_spacing=6)
        self._count = ft.Text("", size=10, color=theme.c("text_hint"))
        self._rebuild()

    # ── Editor 协议 ─────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        # 输入行 = **非 wrap 的普通 Row + expand 输入框**（与 PathListEditor 同模式；
        # 2026-09-21 真机：wrap 行里放可伸展控件会让该卡片变灰块）
        return ft.Column([
            self._wrap,
            ft.Row([self._input,
                    ft.TextButton("添加", icon=ft.Icons.ADD, on_click=self._add),
                    ft.TextButton("恢复推荐词", icon=ft.Icons.UNDO,
                                  on_click=self._restore),
                    self._count], spacing=6,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=6)

    def validate(self) -> str | None:
        pending = (self._input.value or "").strip()
        if pending and pending in self._tags:          # 未回车就保存 → 明确提示
            return f"待添加的关键词已存在：{pending}（点「添加」或清空输入框）"
        return None

    def get_value(self) -> list[str] | None:
        return list(self._tags) or None

    def set_value(self, value) -> None:  # noqa: ANN001
        self._tags = [str(v) for v in (value or []) if str(v).strip()]
        self._rebuild()

    # ── 内部 ────────────────────────────────
    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - 回调失败不影响编辑
            pass

    def _add(self, e=None) -> None:  # noqa: ANN001
        raw = (self._input.value or "").strip()
        if not raw:
            return
        if raw not in self._tags:                       # 去重
            self._tags.append(raw)
        self._input.value = ""
        self._rebuild()
        self._notify()

    def _remove(self, tag: str) -> None:
        self._tags = [x for x in self._tags if x != tag]
        self._rebuild()
        self._notify()

    def _restore(self, e=None) -> None:  # noqa: ANN001
        self._tags = list(self._recommended)
        self._rebuild()
        self._notify()

    def _rebuild(self) -> None:
        t = self.t
        # 标签 = 单个 Text（**不要**在 Wrap 里套 Row：Wrap 子项宽度无界 → Flutter 报错 → 整页灰块）
        self._wrap.controls = [
            ft.Container(
                content=ft.Text(f"{tag}  ✕", size=10, color=t.c("text_primary")),
                on_click=lambda e, g=tag: self._remove(g),
                tooltip="点击移除该关键词",
                padding=ft.padding.Padding(left=10, right=10, top=3, bottom=3),
                bgcolor=t.c("bg_panel"), border_radius=999,
                border=ft.border.Border.all(1, t.c("border")))
            for tag in self._tags]
        if not self._tags:
            self._wrap.controls = [ft.Text("（暂无关键词）", size=10,
                                          color=t.c("text_hint"))]
        self._count.value = f"{len(self._tags)} 个词"
        for ctl in (self._wrap, self._count):
            try:
                ctl.update()
            except Exception:  # noqa: BLE001
                pass
