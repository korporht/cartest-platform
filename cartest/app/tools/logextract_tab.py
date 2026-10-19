# -*- coding: utf-8 -*-
"""时间段提取 Tab — 按时间范围从日志目录提取/切割车载日志

平台化：核心逻辑在 ``cartest.core.logextract``（库化自 ``extract_logs.py``），
本页只做输入/进度/日志/取消。Web 模式不支持目录选择 → 需手动粘贴路径。

设计依据：36-时间段日志提取设计.md。
"""
from __future__ import annotations

import os
import threading

import flet as ft

from .base_tab import BaseTab
from .. import tasks as tasks_mod
from ..ui.components import sync_text_value
from ...core.logextract import LogExtractor


class LogExtractTab(BaseTab):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._cancel = threading.Event()
        self._running = False
        self._last_output = ""

    # ── 状态持久化 ──────────────────────────
    def save_state(self) -> dict:
        return {
            "source": getattr(self, "_source_field", None) and self._source_field.value or "",
            "date": getattr(self, "_date_field", None) and self._date_field.value or "",
            "time": getattr(self, "_time_field", None) and self._time_field.value or "",
            "range": getattr(self, "_range_field", None) and self._range_field.value or "5",
            "output": getattr(self, "_output_field", None) and self._output_field.value or "",
        }

    def restore_state(self, state: dict) -> None:
        for attr, key, dflt in (
            ("_source_field", "source", ""),
            ("_date_field", "date", ""),
            ("_time_field", "time", ""),
            ("_range_field", "range", "5"),
            ("_output_field", "output", ""),
        ):
            ctl = getattr(self, attr, None)
            if ctl:
                ctl.value = state.get(key, dflt)

    def on_detach(self) -> None:
        """离页**不停提取**（40 号新语义）：任务归 ``state.tasks`` 持有

        取消途径：本页「取消」按钮 / 顶栏任务抽屉（本地任务经 stop_fn 置取消事件）。
        """
        super().on_detach()

    # ── UI ──────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t

        def _field(label: str, hint: str = "", **kw) -> ft.TextField:
            return ft.TextField(
                on_change=sync_text_value, label=label, hint_text=hint,
                text_size=12, filled=True, bgcolor=t.c("bg_panel"),
                border_radius=8, **kw)

        self._source_field = _field("源目录（日志根目录，递归扫描）",
                                    "D:\\path\\to\\logs_20260910_152823", expand=True,
                                    text_style=ft.TextStyle(font_family="Consolas"))
        self._browse_btn = ft.ElevatedButton(
            "浏览…", icon=ft.Icons.FOLDER_OPEN, on_click=self._browse)
        if self.app.page.web:
            self._browse_btn.disabled = True
            self._browse_btn.tooltip = "Web 模式不支持目录选择，请直接粘贴路径"

        self._date_field = _field("日期 YYYYMMDD", "留空自动从路径推断",
                                  expand=2, text_style=ft.TextStyle(font_family="Consolas"))
        self._time_field = _field("时间", "15:23-15:26 / 1542 / 1540-",
                                  expand=2, text_style=ft.TextStyle(font_family="Consolas"))
        self._range_field = _field("前后分钟", "5", width=96,
                                   text_style=ft.TextStyle(font_family="Consolas"))
        self._output_field = _field("输出目录", "留空 → 平台数据目录自动命名",
                                    expand=True, text_style=ft.TextStyle(font_family="Consolas"))

        self._preview_switch = ft.Switch(label="仅预览（不写盘）", value=False, label_text_style=ft.TextStyle(size=11))
        self._slice_switch = ft.Switch(label="头尾切割（关=整拷）", value=True, label_text_style=ft.TextStyle(size=11))

        self._btn_preview = ft.ElevatedButton(
            "预览", icon=ft.Icons.SEARCH, on_click=self._on_preview)
        self._btn_start = ft.ElevatedButton(
            "开始提取", icon=ft.Icons.CONTENT_CUT, on_click=self._on_start,
            style=ft.ButtonStyle(bgcolor=t.c("accent"), color=t.c("text_on_accent"),
                                 shape=ft.RoundedRectangleBorder(radius=8)))
        self._btn_cancel = ft.ElevatedButton(
            "取消", icon=ft.Icons.STOP, on_click=self._cancel_run, disabled=True)
        self._btn_open = ft.ElevatedButton(
            "打开输出目录", icon=ft.Icons.FOLDER, on_click=self._open_output, disabled=True)

        self._progress = ft.ProgressBar(value=0, color=t.c("accent"))
        self._progress_text = ft.Text("", size=11, color=t.c("text_hint"))
        if self._running:                        # 40 号：切走再切回 → 按钮态回填
            self._set_running(True)

        return ft.Column([
            ft.Text("日志切割", size=14, weight=ft.FontWeight.BOLD,
                    color=t.c("text_primary")),
            ft.Text("按时间范围提取/切割车载日志：同流重叠选取 → 头尾按行时间切割 → "
                    "统一输出 .gz（二进制 DLT 整拷）", size=10, color=t.c("text_hint")),
            ft.Row([self._source_field, self._browse_btn], spacing=8),
            ft.Row([self._date_field, self._time_field, self._range_field], spacing=8),
            ft.Row([self._output_field], spacing=8),
            ft.Row([self._preview_switch, self._slice_switch], spacing=16),
            ft.Row([self._btn_preview, self._btn_start, self._btn_cancel,
                    self._btn_open], spacing=8),
            self._progress,
            self._progress_text,
        ], spacing=10, scroll=ft.ScrollMode.AUTO)

    # ── 事件 ────────────────────────────────
    async def _browse(self, e=None) -> None:  # noqa: ANN001
        if self.app.page.web:
            self.app.set_status("目录选择仅支持桌面模式（Web 模式请直接粘贴路径）", "warning")
            return
        # 0.85：FilePicker 是 Service（构造即注册），不要挂 overlay
        try:
            picker = ft.FilePicker()
            path = await picker.get_directory_path(dialog_title="选择日志源目录")
            if path:
                self._source_field.value = path
                try:
                    self._source_field.update()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as ex:  # noqa: BLE001
            self.app.set_status(f"选择目录失败：{ex}", "danger")

    def _on_preview(self, e=None) -> None:  # noqa: ANN001
        self._run(list_only=True)

    def _on_start(self, e=None) -> None:  # noqa: ANN001
        self._run(list_only=False)

    def _cancel_run(self, e=None) -> None:  # noqa: ANN001
        if self._running:
            self._cancel.set()
            self.app.set_status("取消中（等待当前文件结束，已写出的保留）…", "warning")

    def _open_output(self, e=None) -> None:  # noqa: ANN001
        if not self._last_output:
            return
        if self.app.page.web:
            self.app.append_log_to_panel(f"[提取] Web 模式输出目录: {self._last_output}")
            return
        try:
            os.startfile(self._last_output)  # type: ignore[attr-defined]  # noqa: S606
        except Exception as ex:  # noqa: BLE001
            self.app.set_status(f"打开失败：{ex}", "danger")

    def _ui(self, fn) -> None:
        """UI 变更回主线程（见 context._safe_on_main；2026-09-23 竞态崩溃修复）"""
        on_main = getattr(self.app, "on_main", None)
        if on_main is not None:
            on_main(fn)
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    def _set_running(self, running: bool) -> None:
        self._running = running

        def _ui_apply() -> None:
            self._btn_start.disabled = running
            self._btn_preview.disabled = running
            self._btn_cancel.disabled = not running
            for ctl in (self._btn_start, self._btn_preview, self._btn_cancel):
                try:
                    ctl.update()
                except Exception:  # noqa: BLE001
                    pass

        self._ui(_ui_apply)

    def _run(self, list_only: bool) -> None:
        if self._running:
            return
        source = (self._source_field.value or "").strip().strip('"')
        time_str = (self._time_field.value or "").strip()
        date_str = (self._date_field.value or "").strip()
        output_dir = (self._output_field.value or "").strip().strip('"')
        range_raw = (self._range_field.value or "5").strip()
        if not source:
            self.app.set_status("请先选择或输入源目录", "danger")
            return
        if not os.path.isdir(source):
            self.app.set_status(f"源目录不存在: {source}", "danger")
            return
        if not time_str:
            self.app.set_status("请填写时间（如 15:23-15:26 或 1542）", "danger")
            return
        try:
            range_min = int(range_raw or "5")
        except ValueError:
            self.app.set_status(f"前后分钟必须是整数: {range_raw}", "danger")
            return

        enable_slice = bool(self._slice_switch.value)
        self._cancel.clear()
        self._set_running(True)
        # 40 号：登记全局任务（本地 IO 类，不入设备队列；跨页存活 + 指示器 + 抽屉可停）
        self._task_register("logextract", "日志切割", kind=tasks_mod.KIND_LOCAL,
                            stop_fn=self._cancel.set)
        self._progress.value = 0
        self._progress_text.value = "准备中…"
        for ctl in (self._progress, self._progress_text):
            try:
                ctl.update()
            except Exception:  # noqa: BLE001
                pass
        self.app.clear_log_panel()
        mode = "预览" if list_only else "提取"
        self.app.set_status(f"{mode}中: {time_str} · 切割{'开' if enable_slice else '关'}",
                            "accent")
        self._task_update("logextract",
                          detail=f"{mode} {time_str} · 切割{'开' if enable_slice else '关'}")
        # 普通后台线程（与其它工具页一致）：
        # page.run_thread 启动的 worker 内控件 update 不送达 Web 客户端（发现 #8）
        self._run_in_thread(self._worker, source, time_str, range_min, date_str,
                            output_dir, list_only, enable_slice)

    def _worker(self, source: str, time_str: str, range_min: int, date_str: str,
                output_dir: str, list_only: bool, enable_slice: bool) -> None:
        def _on_log(level: str, msg: str) -> None:
            prefix = {"warn": "⚠ ", "error": "✖ "}.get(level, "")
            self.app.append_log_to_panel(f"[提取] {prefix}{msg}")

        def _on_progress(phase: str, done: int, total: int) -> None:
            if phase == "scan":
                text, frac = f"扫描中… 已发现 {done} 个候选文件", 0.0
            elif phase == "select":
                text, frac = f"选取中… 已处理 {done}", 0.0
            elif phase == "write":
                frac = (done / total) if total else 0.0
                text = f"写入 {done}/{total}"
            else:  # slice
                text, frac = f"切割中… 已读 {done} 行", None
            self._task_update("logextract", progress=(-1.0 if frac is None else frac),
                              detail=text)

            def _apply() -> None:
                if frac is not None:
                    self._progress.value = max(0.0, min(frac, 1.0))
                self._progress_text.value = text
                for ctl in (self._progress, self._progress_text):
                    try:
                        ctl.update()
                    except Exception:  # noqa: BLE001
                        pass

            self._ui(_apply)

        name = os.path.basename(source.rstrip("\\/")) or source
        op = self.app.begin_op("日志切割", tag="工具",
                               param=f"{name} · {time_str}{'（预览）' if list_only else ''}")
        ok_final = False
        completed = False
        try:
            extractor = LogExtractor(on_log=_on_log, on_progress=_on_progress,
                                     cancel=self._cancel)
            result = extractor.extract(
                source, time_str=time_str, range_min=range_min,
                date=date_str or None, output_dir=output_dir or None,
                list_only=list_only, enable_slice=enable_slice)
            ok_final = bool(result.ok)
            if result.cancelled:
                self.app.set_status("已取消（已写出的文件保留）", "warning")
                self._done("提取已取消", ok=False)
            elif not result.ok:
                self.app.set_status(f"失败：{result.message}", "danger")
                self._done("提取失败", ok=False)
            elif result.output_dir and not list_only:
                completed = True
                self._last_output = result.output_dir
                self.app.set_status(f"✓ {result.message}", "success")
                self._done("提取完成")
                self._ui(lambda: self._enable_open(True))
            else:
                self.app.set_status(result.message, "warning")
                self._done("预览完成")
                self._ui(lambda: self._enable_open(False))
        except Exception as exc:  # noqa: BLE001
            self.app.set_status(f"提取异常: {exc}", "danger")
            self._done("提取失败", ok=False)
        finally:
            self.app.end_op(op, ok=ok_final)

            def _done_ui() -> None:
                if completed:
                    self._progress.value = 1.0
                self._progress_text.value = "完成" if completed else ""
                for ctl in (self._progress, self._progress_text):
                    try:
                        ctl.update()
                    except Exception:  # noqa: BLE001
                        pass

            self._ui(_done_ui)
            self._task_unregister("logextract")    # 40 号：完成即出表（进历史）
            self._set_running(False)

    def _enable_open(self, enabled: bool) -> None:
        self._btn_open.disabled = not enabled
        try:
            self._btn_open.update()
        except Exception:  # noqa: BLE001
            pass
