# -*- coding: utf-8 -*-
"""CarTest app — 主题配置（亮/暗双套）

调色板来源：``cartest/app/ui/tokens.py``（UI-1，S3 深浅双生——见 14-UI重构设计.md §4.1）。
API 与历史版本保持一致（``c(key)`` / ``stage_color`` / ``flet_theme`` / ``toggle``），
flet_Tool 迁入的工具页零改动。
"""
from __future__ import annotations

from typing import Dict

import flet as ft

from .ui.tokens import PALETTE_DARK, PALETTE_LIGHT


class AppTheme:
    """主题颜色管理，提供 Flet Theme 与颜色访问"""

    _LIGHT: Dict[str, str] = PALETTE_LIGHT
    _DARK: Dict[str, str] = PALETTE_DARK

    def __init__(self) -> None:
        # 双主题：默认明亮（2026-09-17 用户决策——启动即浅色，一键切深色）
        self._dark: bool = False
        self._cache: Dict[str, str] = dict(self._LIGHT)

    @property
    def is_dark(self) -> bool:
        return self._dark

    def toggle(self) -> None:
        self._dark = not self._dark
        self._cache = dict(self._DARK if self._dark else self._LIGHT)

    def c(self, key: str, default: str = "#999999") -> str:
        return self._cache.get(key, default)

    def stage_color(self, status: str) -> str:
        """阶段状态 → 颜色"""
        return self.c(f"stage_{status}", self.c("stage_pending"))

    def flet_theme(self) -> ft.Theme:
        cs = ft.ColorScheme(
            primary=self.c("accent"),
            on_primary=self.c("text_on_accent"),
            surface=self.c("bg_card"),
            on_surface=self.c("text_primary"),
            outline=self.c("border"),
            error=self.c("danger"),
        )
        return ft.Theme(
            color_scheme_seed=self.c("accent"),
            color_scheme=cs,
            font_family="微软雅黑",
            visual_density=ft.VisualDensity.COMPACT,
        )

    @property
    def theme_mode(self) -> ft.ThemeMode:
        return ft.ThemeMode.DARK if self._dark else ft.ThemeMode.LIGHT
