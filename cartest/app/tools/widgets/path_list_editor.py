# -*- coding: utf-8 -*-
"""可复用控件 — 路径列表编辑器（配置页专用，P3c）

值形态（与 ``config_center`` 的 ``path_list`` 类型一致）::

    [{"path": "/sdcard/DCIM/", "alias": "相机", "enabled": True}, ...]

界面：每行 = 路径输入 | 别名输入 | 启用开关 | 删除；底部「＋ 添加路径」。

说明：
  · ``get_value()`` 从**当前控件值**收集（编辑即生效，无需同步回调）；
  · ``validate()`` 只校验"启用项路径非空"（空行允许存在，便于先添加后填写）；
  · 别名留空时由消费方展示层回退（取路径末段），编辑器不做强制。
"""
from __future__ import annotations

import flet as ft
from ...ui.components import sync_text_value


class PathListEditor:
    """路径列表编辑器（组合式：``build()`` 产出控件树，``get_value()`` 收集值）

    ``on_change``（可选，2026-10-15 实时保存）：**点击类**改动（启用开关 / 添加 / 移除行）
    即回调——这类动作应当立刻落盘，而不是等配置页的文本防抖窗口。
    """

    def __init__(self, value: list[dict] | None = None, *, on_change=None) -> None:  # noqa: ANN001
        self._on_change = on_change
        self._rows: list[dict] = []
        self._holder = ft.Column(spacing=4)
        self.set_value(value)

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - 回调失败不影响编辑
            pass

    # ── 构建 ──────────────────────────────────────
    def build(self) -> ft.Control:
        add_btn = ft.TextButton("＋ 添加路径", icon=ft.Icons.ADD,
                                on_click=lambda e: self._on_add())
        return ft.Column([self._holder, ft.Row([add_btn])], spacing=4)

    # ── 值 ────────────────────────────────────────
    def set_value(self, value: list[dict] | None) -> None:
        """整体替换（含界面控件重建；用于「恢复默认」后同步）"""
        self._rows.clear()
        self._holder.controls.clear()
        for item in (value or []):
            self._append_row(str(item.get("path", "")), str(item.get("alias", "")),
                             bool(item.get("enabled", True)))
        self._refresh()

    def get_value(self) -> list[dict]:
        return [
            {
                "path": (r["path"].value or "").strip(),
                "alias": (r["alias"].value or "").strip(),
                "enabled": bool(r["enabled"].value),
            }
            for r in self._rows
        ]

    def validate(self) -> str | None:
        """启用项路径不得为空；返回错误文案或 None。"""
        for i, item in enumerate(self.get_value(), 1):
            if item["enabled"] and not item["path"]:
                return f"第 {i} 行：启用状态下路径不能为空"
        return None

    # ── 行管理 ────────────────────────────────────
    def _on_add(self) -> None:
        self._append_row("", "", True)
        self._refresh()
        self._notify()

    def _on_remove(self, row: dict) -> None:
        if row in self._rows:
            self._rows.remove(row)
        self._refresh()
        self._notify()

    def _append_row(self, path: str, alias: str, enabled: bool) -> None:
        row: dict = {
            "path": ft.TextField(on_change=sync_text_value, value=path, hint_text="远端路径，如 /la_log/…", expand=True,
                                 dense=True, text_size=12),
            "alias": ft.TextField(on_change=sync_text_value, value=alias, hint_text="别名", width=110, dense=True,
                                  text_size=12),
            "enabled": ft.Switch(value=enabled,
                                 on_change=lambda e: self._notify()),   # 开关＝点击类：即时落盘
        }
        row["ctl"] = ft.Row([
            row["path"], row["alias"], row["enabled"],
            ft.IconButton(ft.Icons.CLOSE, icon_size=16, tooltip="移除",
                          on_click=lambda e, r=row: self._on_remove(r)),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self._rows.append(row)
        self._holder.controls.append(row["ctl"])

    def _refresh(self) -> None:
        self._holder.controls = [r["ctl"] for r in self._rows]
        try:
            self._holder.update()
        except Exception:  # noqa: BLE001
            pass    # 未挂载时（构建/测试阶段）无 UI 可刷新，属预期
