# -*- coding: utf-8 -*-
"""大文件推送 Tab（P3e）— 分块推送；<2GB 直推校验，≥2GB 分块→合并→终验

平台化：执行逻辑在 ``cartest.core.filetransfer``（库），本页只做输入/进度/日志/取消。
Web 模式不支持文件选择（FilePicker 会使会话崩溃）→ 需手动粘贴路径。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import flet as ft

from .base_tab import BaseTab
from .. import tasks as tasks_mod
from ..ui.components import sync_text_value
from ...core.filetransfer import FilePusher


class PushTab(BaseTab):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._cancel = threading.Event()
        self._running = False

    # ── 状态持久化 ──────────────────────────
    def save_state(self) -> dict:
        return {
            "local": getattr(self, "_local_field", None) and self._local_field.value or "",
            "remote": getattr(self, "_remote_field", None) and self._remote_field.value or "",
        }

    def restore_state(self, state: dict) -> None:
        if hasattr(self, "_local_field") and self._local_field:
            self._local_field.value = state.get("local", "")
        if hasattr(self, "_remote_field") and self._remote_field:
            self._remote_field.value = state.get("remote", "")

    def on_detach(self) -> None:
        """离页**不停推送**（40 号新语义）：任务归 ``state.tasks`` 持有

        取消途径：本页「取消」按钮 / 顶栏任务抽屉（stop_fn 置取消事件）。
        """
        super().on_detach()

    # ── UI ──────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t

        self._local_field = ft.TextField(
            on_change=sync_text_value,
            label="本地文件路径", hint_text="D:\\path\\to\\large.bin",
            text_size=12, expand=True,
            text_style=ft.TextStyle(font_family="Consolas"),
            filled=True, bgcolor=t.c("bg_panel"), border_radius=8)
        self._browse_btn = ft.ElevatedButton(
            "浏览…", icon=ft.Icons.FOLDER_OPEN, on_click=self._browse)
        if self.app.page.web:
            self._browse_btn.disabled = True
            self._browse_btn.tooltip = "Web 模式不支持文件选择，请直接粘贴路径"

        self._remote_field = ft.TextField(
            on_change=sync_text_value,
            label="设备目标路径（以 / 结尾则自动拼接文件名）",
            value="/sdcard/Download/",
            text_size=12, expand=True,
            text_style=ft.TextStyle(font_family="Consolas"),
            filled=True, bgcolor=t.c("bg_panel"), border_radius=8)

        self._start_btn = ft.ElevatedButton(
            "开始推送", icon=ft.Icons.UPLOAD, on_click=self._start,
            style=ft.ButtonStyle(bgcolor=t.c("accent"), color=t.c("text_on_accent"),
                                 shape=ft.RoundedRectangleBorder(radius=8)))
        self._cancel_btn = ft.ElevatedButton(
            "取消", icon=ft.Icons.STOP, on_click=self._cancel_push, disabled=True)

        self._progress = ft.ProgressBar(value=0, color=t.c("accent"))
        self._progress_text = ft.Text("", size=11, color=t.c("text_hint"))
        if self._running:                        # 40 号：切走再切回 → 按钮态回填
            self._set_running(True)

        return ft.Column([
            ft.Text("大文件推送", size=14, weight=ft.FontWeight.BOLD,
                    color=t.c("text_primary")),
            ft.Text("分块推送突破 adb push 限制：分块 → 逐块 MD5 → 设备端合并 → 终验；"
                    "取消/失败自动清理残留分块", size=10, color=t.c("text_hint")),
            ft.Row([self._local_field, self._browse_btn], spacing=8),
            ft.Row([self._remote_field], spacing=8),
            ft.Row([self._start_btn, self._cancel_btn], spacing=8),
            self._progress,
            self._progress_text,
        ], spacing=10)

    # ── 事件 ────────────────────────────────
    async def _browse(self, e=None) -> None:  # noqa: ANN001
        if self.app.page.web:
            self.app.set_status("文件选择仅支持桌面模式（Web 模式请直接粘贴路径）", "warning")
            return
        # 0.85：FilePicker 是 Service（构造即注册），**不要挂 overlay**
        try:
            picker = ft.FilePicker()
            files = await picker.pick_files(dialog_title="选择要推送的文件")
            if files:
                self._local_field.value = files[0].path
                try:
                    self._local_field.update()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as ex:  # noqa: BLE001
            self.app.set_status(f"选择文件失败：{ex}", "danger")

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
            self._start_btn.disabled = running
            self._cancel_btn.disabled = not running
            for ctl in (self._start_btn, self._cancel_btn):
                try:
                    ctl.update()
                except Exception:  # noqa: BLE001
                    pass

        self._ui(_ui_apply)

    def _cancel_push(self, e=None) -> None:  # noqa: ANN001
        if self._running:
            self._cancel.set()
            self.app.set_status("取消中（等待当前分块结束并清理）…", "warning")

    def _start(self, e=None) -> None:  # noqa: ANN001
        if self._running:
            return
        local = (self._local_field.value or "").strip()
        remote = (self._remote_field.value or "").strip()
        if not local:
            self.app.set_status("请先选择或输入本地文件路径", "danger")
            return
        if not Path(local).is_file():
            self.app.set_status(f"本地文件不存在: {local}", "danger")
            return
        if not remote:
            self.app.set_status("请填写设备目标路径", "danger")
            return
        if remote.endswith("/"):
            remote = f"{remote}{Path(local).name}"

        # 40 号 §4：设备忙（其他设备任务/流水线运行中）→ 入队，完成后自动推送；
        # "started" 时队列已执行 starter，勿再直跑（防双重执行）
        verdict = self._submit_device_task("push", "大文件推送",
                                           lambda: self._begin_push(local, remote))
        if verdict == "queued":
            self._queued_feedback("push", "大文件推送")
            return
        if verdict == "bypass":
            self._begin_push(local, remote)

    def _begin_push(self, local: str, remote: str) -> None:
        """真正发起推送（可由队列出队触发；worker 放后台线程）"""
        self._cancel.clear()
        self._set_running(True)
        # 40 号：登记全局任务（stop_fn = 置取消事件 → 推送器自动清理残留分块）
        self._task_register("push", "大文件推送",
                            kind=tasks_mod.KIND_DEVICE, stop_fn=self._cancel.set)
        try:
            self.app.state.device_acquire("push")  # 补 token：推送期间禁止切设备
        except Exception:  # noqa: BLE001 — 测试替身无该方法时跳过
            pass
        self._progress.value = 0
        self._progress_text.value = "准备中…"
        for ctl in (self._progress, self._progress_text):
            try:
                ctl.update()
            except Exception:  # noqa: BLE001
                pass
        self.app.clear_log_panel()
        self.app.set_status(f"推送中: {local} → {remote}", "accent")
        # 普通后台线程（与其它工具页一致）：
        # page.run_thread 启动的 worker 内控件 update 不送达 Web 客户端（发现 #8）
        self._run_in_thread(self._worker, local, remote)

    def _worker(self, local: str, remote: str) -> None:
        def _on_log(level: str, msg: str) -> None:
            prefix = {"warn": "⚠ ", "error": "✖ "}.get(level, "")
            self.app.append_log_to_panel(f"[推送] {prefix}{msg}")

        def _on_progress(i: int, total: int, pct: float, mbps: float, eta: float) -> None:
            text = (f"{i}/{total} 块 · {pct:.1f}% · "
                    f"{mbps:.1f} MB/s · 剩余约 {eta:.0f}s")
            self._task_update("push", progress=max(0.0, min(pct / 100.0, 1.0)),
                              detail=text)

            def _apply() -> None:
                self._progress.value = max(0.0, min(pct / 100.0, 1.0))
                self._progress_text.value = text
                for ctl in (self._progress, self._progress_text):
                    try:
                        ctl.update()
                    except Exception:  # noqa: BLE001
                        pass

            self._ui(_apply)

        ok_final = False
        _name = os.path.basename(local)
        try:
            _size = os.path.getsize(local) / (1024 * 1024)
            _param = f"{_name} · {_size:.1f} MB → {remote}"
        except OSError:
            _param = f"{_name} → {remote}"
        op = self.app.begin_op("大文件推送", tag="工具", param=_param)
        try:
            pusher = FilePusher(on_log=_on_log, on_progress=_on_progress,
                                cancel=self._cancel)
            result = pusher.push(local, remote)
            ok_final = bool(result.ok)
            if result.ok:
                self.app.set_status(f"✓ {result.message}", "success")
                self._progress.value = 1.0
                self._done("推送完成")
            else:
                self.app.set_status(f"推送失败: {result.message}",
                                    "warning" if self._cancel.is_set() else "danger")
                self._progress.value = 0
                self._done("推送已取消" if self._cancel.is_set() else "推送失败",
                           ok=False)
        except Exception as exc:  # noqa: BLE001
            self.app.set_status(f"推送异常: {exc}", "danger")
            self._progress.value = 0
            self._done("推送失败", ok=False)
        finally:
            self.app.end_op(op, ok=ok_final)

            def _done_ui() -> None:
                try:
                    self._progress.update()
                except Exception:  # noqa: BLE001
                    pass

            self._ui(_done_ui)
            self._task_unregister("push")          # 40 号：完成即出表（触发出队）
            try:
                self.app.state.device_release("push")
            except Exception:  # noqa: BLE001
                pass
            self._set_running(False)
