# -*- coding: utf-8 -*-
"""时间戳 Tab - 左右两列布局，时间戳↔日期互转，含复制按钮和精度badge
"""
from __future__ import annotations

import time
import threading
from typing import Any, Callable, Dict, Optional
import flet as ft
from functools import partial
from .base_tab import BaseTab
from .constants import TIMESTAMP_BADGE_WIDTH

# 精度说明
PRECISION_INFO = {
    10: ("秒", "s"),
    13: ("毫秒", "ms"),
    16: ("微秒", "us"),
    19: ("纳秒", "ns"),
}


class TimestampTab(BaseTab):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._lock = threading.Lock()
        # 存储最近一次转换结果，供复制使用
        self._last_results = {}  # {label: value_str}
        self._debounce_timer = None  # 防抖计时器，避免每次按键都触发转换

    def save_state(self) -> dict:
        """保存运行时状态"""
        input_val = ""
        if hasattr(self, "_input_field") and self._input_field:
            input_val = self._input_field.value or ""
        return {"input_value": input_val}

    def restore_state(self, state: dict) -> None:
        """恢复运行时状态"""
        if "input_value" in state and hasattr(self, "_input_field") and self._input_field:
            self._input_field.value = state["input_value"]

    def build(self) -> ft.Control:
        t = self.t

        # ── 实时时钟 ──
        self._clock = ft.Text("", size=28, font_family="Consolas",
                               weight=ft.FontWeight.BOLD, color=t.c("accent"))

        # ── 输入框 ──
        self._input_field = ft.TextField(
            hint_text="输入时间戳（10/13/16/19位）或日期时间…",
            text_size=15,
            text_style=ft.TextStyle(font_family="Consolas"),
            filled=True,
            bgcolor=t.c("bg_panel"),
            border_radius=12,
            content_padding=ft.padding.Padding(left=16, top=12, right=16, bottom=12),
            on_submit=self._convert,
            on_change=lambda e: self._auto_convert(),
        )

        # ── 精度识别提示文字 ──
        self._precision_hint = ft.Text("", size=12, italic=True, color=t.c("text_hint"))

        # ── 结果区：左列（时间戳→日期）、右列（日期→时间戳）──
        # 左列结果行（时间戳 → 日期时间）
        self._left_rows = []   # List[ft.Row]
        self._build_left_rows(t)

        # 右列结果行（日期时间 → 时间戳）
        self._right_rows = []
        self._build_right_rows(t)

        # 左右并排的 Row
        self._result_row = ft.Row(
            controls=[
                ft.Container(
                    content=ft.Column(controls=self._left_rows, spacing=8),
                    expand=True,
                ),
                ft.VerticalDivider(width=1, color=t.c("border")),
                ft.Container(
                    content=ft.Column(controls=self._right_rows, spacing=8),
                    expand=True,
                ),
            ],
            spacing=12,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        # ── 快捷按钮 ──
        fill_btns = []
        for digits, (label, _) in PRECISION_INFO.items():
            btn = ft.ElevatedButton(
                f"填入当前({label})",
                on_click=partial(lambda e, d=digits: self._fill_now(d)),
                style=ft.ButtonStyle(padding=8),
            )
            fill_btns.append(btn)

        copy_now_btn = ft.ElevatedButton(
            "📋 复制当前时间戳",
            on_click=self._copy_current_ts,
            style=ft.ButtonStyle(padding=8,
                                 bgcolor=t.c("accent"),
                                 color=ft.Colors.WHITE),
        )

        return ft.Container(
            content=ft.Column(
                controls=[
                    # 实时时钟
                    ft.Container(
                        content=self._clock,
                        alignment=ft.alignment.Alignment(0, 0),
                        padding=16,
                    ),
                    # 输入框
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                self._input_field,
                                self._precision_hint,
                            ],
                            spacing=4,
                        ),
                        padding=ft.Padding(24, 0, 24, 0),
                    ),
                    ft.Container(height=12),
                    # 转换按钮行
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.ElevatedButton("🔄 转换", on_click=self._convert,
                                                   style=ft.ButtonStyle(
                                                       bgcolor=t.c("accent"),
                                                       color=ft.Colors.WHITE,
                                                       padding=12)),
                                ft.OutlinedButton("🗑 清空", on_click=self._clear_all),
                            ],
                            spacing=12,
                        ),
                        padding=ft.Padding(24, 0, 24, 0),
                    ),
                    ft.Container(height=8),
                    # 结果区标题
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Text("📤 时间戳 → 日期时间", size=13,
                                         weight=ft.FontWeight.BOLD, color=t.c("accent")),
                                ft.VerticalDivider(width=1),
                                ft.Text("📥 日期时间 → 时间戳", size=13,
                                         weight=ft.FontWeight.BOLD, color=t.c("accent")),
                            ],
                            spacing=12,
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        padding=ft.Padding(24, 4, 24, 4),
                    ),
                    # 结果区
                    ft.Container(
                        content=self._result_row,
                        padding=ft.Padding(24, 0, 24, 0),
                        expand=True,
                    ),
                    ft.Divider(height=1, color=t.c("border")),
                    # 快捷按钮
                    ft.Container(
                        content=ft.Row(
                            controls=fill_btns + [copy_now_btn],
                            spacing=8,
                            wrap=True,
                        ),
                        padding=ft.Padding(24, 10, 24, 10),
                    ),
                ],
                spacing=4,
                expand=True,
            ),
            expand=True,
            padding=ft.Padding(0, 20, 0, 0),
        )

    def _build_left_rows(self, t) -> None:
        """构建左列：时间戳 → 日期时间 的各精度结果行"""
        items = [
            (10, "秒级 (10位)", self._format_dt_from_ts),
            (13, "毫秒级 (13位)", self._format_dt_from_ts),
            (16, "微秒级 (16位)", self._format_dt_from_ts),
            (19, "纳秒级 (19位)", self._format_dt_from_ts),
        ]
        for digits, label, formatter in items:
            row = self._make_result_row(t, label, digits, formatter, side="left")
            self._left_rows.append(row)

    def _build_right_rows(self, t) -> None:
        """构建右列：日期时间 → 时间戳 的各精度结果行"""
        items = [
            (10, "秒级 (10位)", self._format_ts_from_dt),
            (13, "毫秒级 (13位)", self._format_ts_from_dt),
            (16, "微秒级 (16位)", self._format_ts_from_dt),
            (19, "纳秒级 (19位)", self._format_ts_from_dt),
        ]
        for digits, label, formatter in items:
            row = self._make_result_row(t, label, digits, formatter, side="right")
            self._right_rows.append(row)

    def _make_result_row(self, t, label, digits, formatter, side="") -> ft.Row:
        """生成一个结果行：badge + 值 + 复制按钮"""
        badge = ft.Container(
            content=ft.Text(f"{digits}位", size=11, weight=ft.FontWeight.BOLD,
                             color=ft.Colors.WHITE),
            bgcolor=t.c("accent") if side == "left" else t.c("success"),
            border_radius=6,
            padding=ft.Padding(8, 3, 8, 3),
            width=TIMESTAMP_BADGE_WIDTH,
            alignment=ft.alignment.Alignment(0, 0),
        )

        value_text = ft.Text("", size=13, font_family="Consolas",
                              color=t.c("text_primary"),
                              overflow=ft.TextOverflow.ELLIPSIS,
                              expand=True)

        copy_btn = ft.IconButton(
            icon=ft.Icons.COPY,
            tooltip="复制",
            icon_size=16,
            on_click=partial(lambda e, v=value_text, l=label: self._copy_value(e, v, l)),
        )

        # 保存引用，供更新时使用
        setattr(self, f"_left_val_{digits}" if side == "left" else f"_right_val_{digits}", value_text)
        setattr(self, f"_left_badge_{digits}" if side == "left" else f"_right_badge_{digits}", badge)

        row = ft.Row(
            controls=[badge, value_text, copy_btn],
            spacing=8,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        return row

    # ── 格式化函数 ──

    def _format_dt_from_ts(self, ts_val, digits) -> str:
        """将时间戳转为日期时间字符串"""
        try:
            # 统一到秒级
            if digits == 13:
                ts_s = ts_val // 1000
            elif digits == 16:
                ts_s = ts_val // 1_000_000
            elif digits == 19:
                ts_s = ts_val // 1_000_000_000
            else:
                ts_s = ts_val

            dt = time.localtime(ts_s)
            return time.strftime("%Y-%m-%d %H:%M:%S", dt)
        except (OSError, OverflowError, ValueError):
            return "—"

    def _format_ts_from_dt(self, dt_str, digits) -> str:
        """将日期时间字符串转为时间戳"""
        # 这个函数实际上不直接使用，转换逻辑在 _convert 中统一处理
        return "—"

    # ── 核心转换逻辑 ──

    def _convert(self, e=None) -> None:
        raw = self._input_field.value.strip() if self._input_field.value else ""
        if not raw:
            self._clear_results()
            return

        # 判断输入类型
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            self._from_timestamp(int(raw))
        else:
            self._from_datetime(raw)

    def _auto_convert(self) -> None:
        raw = self._input_field.value.strip() if self._input_field.value else ""
        if raw and (raw.isdigit() or len(raw) >= 8):
            # 取消之前的防抖计时器
            if self._debounce_timer is not None:
                try:
                    self._debounce_timer.cancel()
                except Exception:
                    pass
            # 启动新的防抖计时器（300ms 后执行转换）
            self._debounce_timer = threading.Timer(0.3, self._convert)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _detect_precision(self, ts: int) -> int:
        """检测时间戳精度，返回位数"""
        s = len(str(abs(ts)))
        return s

    def _precision_hint_text(self, ts: int) -> str:
        """生成精度识别提示"""
        s = len(str(abs(ts)))
        if s == 10:
            return "🔍 识别为：秒级时间戳（10位）"
        elif s == 13:
            return "🔍 识别为：毫秒级时间戳（13位）"
        elif s == 16:
            return "🔍 识别为：微秒级时间戳（16位）"
        elif s == 19:
            return "🔍 识别为：纳秒级时间戳（19位）"
        else:
            return f"⚠ 位数异常（{s}位），将自动截断处理"

    def _from_timestamp(self, ts: int) -> None:
        """时间戳 → 日期时间（更新左列）"""
        s = len(str(abs(ts)))
        # 自动截断超长时间戳
        if s > 19:
            ts = ts // (10 ** (s - 19))
            s = 19

        # 统一到各精度值
        ts_10 = ts // (10 ** (s - 10)) if s > 10 else ts
        ts_13 = ts // (10 ** (s - 13)) if s > 13 else ts * (10 ** (13 - s)) if s < 13 else ts
        ts_16 = ts // (10 ** (s - 16)) if s > 16 else ts * (10 ** (16 - s)) if s < 16 else ts
        ts_19 = ts // (10 ** (s - 19)) if s > 19 else ts * (10 ** (19 - s)) if s < 19 else ts

        # 计算各精度对应的秒级时间戳
        ts_per_digits = {
            10: ts_10,
            13: ts_13 // 1000,
            16: ts_16 // 1_000_000,
            19: ts_19 // 1_000_000_000,
        }

        # 收集所有需要更新的 UI 数据
        updates = {}
        for digits, ts_s in ts_per_digits.items():
            try:
                dt = time.localtime(ts_s)
                dt_str = time.strftime("%Y-%m-%d %H:%M:%S", dt)
                updates[f"left_{digits}"] = (dt_str, self.t.c("text_primary"))
                self._last_results[f"left_{digits}"] = dt_str
            except (OSError, OverflowError, ValueError):
                updates[f"left_{digits}"] = ("— 无效时间戳 —", ft.Colors.RED)

        # 右列（同一时间戳对应的各精度值）
        for digits, val in zip([10, 13, 16, 19], [ts_10, ts_13, ts_16, ts_19]):
            updates[f"right_{digits}"] = (str(val), self.t.c("text_primary"))
            self._last_results[f"right_{digits}"] = str(val)

        # 线程安全地更新所有 UI 控件
        hint_text = self._precision_hint_text(ts)
        hint_color = self.t.c("accent") if s in (10, 13, 16, 19) else ft.Colors.ORANGE

        def _apply():
            try:
                self._precision_hint.value = hint_text
                self._precision_hint.color = hint_color
                self._precision_hint.update()
                for key, (val, color) in updates.items():
                    prefix = "left" if key.startswith("left") else "right"
                    digits = int(key.split("_")[1])
                    widget = getattr(self, f"_{prefix}_val_{digits}")
                    widget.value = val
                    widget.color = color
                    widget.update()
            except Exception:
                pass

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            try:
                self.app.page.run_thread(_apply)
            except Exception:
                pass

    def _from_datetime(self, dt_str: str) -> None:
        """日期时间 → 时间戳（更新右列）"""
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%Y%m%d %H%M%S",
        ]
        ts_s = None
        for fmt in formats:
            try:
                ts_s = int(time.mktime(time.strptime(dt_str.strip(), fmt)))
                break
            except ValueError:
                continue

        if ts_s is None:
            def _show_error():
                try:
                    self._clear_results_unsafe()
                    self._precision_hint.value = "⚠ 无法识别日期格式，请检查输入"
                    self._precision_hint.color = ft.Colors.RED
                    self._precision_hint.update()
                except Exception:
                    pass

            if threading.current_thread() is threading.main_thread():
                _show_error()
            else:
                try:
                    self.app.page.run_thread(_show_error)
                except Exception:
                    pass
            return

        # 收集所有需要更新的 UI 数据
        updates = {}
        for digits in [10, 13, 16, 19]:
            multiplier = {10: 1, 13: 1000, 16: 1_000_000, 19: 1_000_000_000}[digits]
            val = ts_s * multiplier
            updates[f"right_{digits}"] = str(val)
            self._last_results[f"right_{digits}"] = str(val)

        # 同时更新左列
        try:
            dt = time.localtime(ts_s)
            dt_str_out = time.strftime("%Y-%m-%d %H:%M:%S", dt)
            for digits in [10, 13, 16, 19]:
                updates[f"left_{digits}"] = dt_str_out
                self._last_results[f"left_{digits}"] = dt_str_out
        except (OSError, OverflowError, ValueError):
            pass

        # 线程安全地更新所有 UI 控件
        hint_text = f"🔍 识别为：日期时间 → 秒级时间戳 {ts_s}"
        hint_color = self.t.c("accent")

        def _apply():
            try:
                self._precision_hint.value = hint_text
                self._precision_hint.color = hint_color
                self._precision_hint.update()
                for key, val in updates.items():
                    prefix = "left" if key.startswith("left") else "right"
                    digits = int(key.split("_")[1])
                    widget = getattr(self, f"_{prefix}_val_{digits}")
                    widget.value = val
                    widget.color = self.t.c("text_primary")
                    widget.update()
            except Exception:
                pass

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            try:
                self.app.page.run_thread(_apply)
            except Exception:
                pass

    def _clear_results(self) -> None:
        """清空所有结果（线程安全）"""
        self._last_results.clear()

        def _apply():
            self._clear_results_unsafe()

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            try:
                self.app.page.run_thread(_apply)
            except Exception:
                pass

    def _clear_results_unsafe(self) -> None:
        """清空所有结果（内部使用，不检查线程）"""
        self._precision_hint.value = ""
        self._precision_hint.update()
        for digits in [10, 13, 16, 19]:
            for prefix in ["_left_val_", "_right_val_"]:
                try:
                    widget = getattr(self, f"{prefix}{digits}")
                    widget.value = ""
                    widget.update()
                except AttributeError:
                    pass

    def _clear_all(self, e=None) -> None:
        self._input_field.value = ""
        self._input_field.update()
        self._clear_results()

    # ── 复制功能 ──

    def _copy_value(self, e, widget, label) -> None:
        """复制指定行的值到剪贴板"""
        val = widget.value
        if not val or val.startswith("—"):
            return
        self.app.page.set_clipboard(val)
        # 显示提示
        if hasattr(self.app, "snack"):
            self.app.snack(f"✅ 已复制：{val[:30]}{'...' if len(val) > 30 else ''}")

    def _copy_current_ts(self, e=None) -> None:
        """复制当前时间戳（秒级）到剪贴板"""
        now_ts = int(time.time())
        self.app.page.set_clipboard(str(now_ts))
        if hasattr(self.app, "snack"):
            self.app.snack(f"✅ 已复制当前时间戳：{now_ts}")

    def _fill_now(self, digits: int) -> None:
        now = int(time.time())
        if digits == 10:
            val = str(now)
        elif digits == 13:
            val = str(now * 1000)
        elif digits == 16:
            val = str(now * 1_000_000)
        elif digits == 19:
            val = str(now * 1_000_000_000)
        else:
            val = str(now)
        self._input_field.value = val
        self._input_field.update()
        self._convert()

    def update_clock(self) -> None:
        """外部定时调用"""
        try:
            if hasattr(self, "_clock") and self._clock:
                self._clock.value = time.strftime("%H:%M:%S")
                self._clock.update()
        except Exception:
            pass
