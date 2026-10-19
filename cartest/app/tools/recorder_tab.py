# -*- coding: utf-8 -*-
"""PC 端录屏页 —— 录制电脑桌面 + 麦克风（独立入口，与流水线不联动）

UI 为新写（旧版是 tkinter 卡片），后端复用 ``screen_recorder.ScreenRecorder``
（原 ``GUI_Done.py`` 的类，纯 Python、无 UI 耦合）。

相对旧版修复的三处（用户裁决"真实现"，见 06-P3b设计.md §4.6）：
  · 窗口置顶真正接入（旧版函数写了从未调用）
  · 音频开关真正生效（旧版复选框未被读取）
  · 显示器选择真正生效（旧版参数被忽略）

另加：**启动前依赖自检**（旧版缺库时会静默产出空文件）。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import flet as ft

from ...core import config_center
from .base_tab import BaseTab
from .. import tasks as tasks_mod
from .screen_recorder import ScreenRecorder, check_dependencies


def record_dir() -> str:
    """录屏产物目录（随「本地输出根目录」，2026-10-12 起可配置）"""
    return config_center.output_dir("recordings")


class RecorderTab(BaseTab):
    """PC 端屏幕录制"""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._rec: ScreenRecorder | None = None
        self._timer: threading.Timer | None = None

    # ── 状态持久化（切页/切主题后回填参数）──
    def save_state(self) -> dict:
        def _v(ctl, default=""):
            return getattr(ctl, "value", default) if ctl is not None else default
        return {
            "monitor": _v(getattr(self, "_monitor_dd", None), "1"),
            "mode": _v(getattr(self, "_mode_dd", None), "全屏"),
            "ts": bool(_v(getattr(self, "_ts_sw", None), True)),
            "audio": bool(_v(getattr(self, "_audio_sw", None), True)),
        }

    def restore_state(self, state: dict) -> None:
        if not state:
            return

    def on_detach(self) -> None:
        """离页**不停录制**（40 号新语义）：计时器与任务继续，指示器可见

        修复「录制中离页静默」：原 on_hide 掐掉计时器 → 页面重建后显示空闲态、
        录制却在后台无 indication 继续。现登记全局任务（registry），停止走
        本页「停止」按钮 / 顶栏任务抽屉；切回页面由 build 回填运行态。
        """
        super().on_detach()

    # ── UI ────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        missing = check_dependencies()

        # 显示器列表（来自 mss）
        monitors = [("1", "主显示器")]
        try:
            import mss
            with mss.mss() as sct:
                for i, m in enumerate(sct.monitors[1:], start=1):
                    monitors.append((str(i), f"显示器 {i}（{m['width']}x{m['height']}）"))
        except Exception:  # noqa: BLE001
            pass

        self._monitor_dd = ft.Dropdown(
            label="显示器", dense=True, width=220, value="1",
            options=[ft.dropdown.Option(k, v) for k, v in monitors])

        self._mode_dd = ft.Dropdown(
            label="录制范围", dense=True, width=150, value="全屏",
            options=[ft.dropdown.Option("全屏"), ft.dropdown.Option("窗口")],
            on_select=self._on_mode_change)      # flet 0.85: Dropdown 用 on_select

        self._window_dd = ft.Dropdown(
            label="目标窗口（窗口模式）", dense=True, expand=True, disabled=True,
            options=[])

        self._ts_sw = ft.Switch(label="时间戳水印", value=True)
        self._audio_sw = ft.Switch(label="录制麦克风", value=True)

        self._timer_txt = ft.Text("00:00:00", size=30, font_family="Consolas",
                                  weight=ft.FontWeight.BOLD, color=t.c("accent"))
        self._path_txt = ft.Text(f"保存目录：{record_dir()}", size=11,
                                 color=t.c("text_secondary"), selectable=True)
        self._dep_txt = ft.Text(
            ("⚠ 缺少依赖：" + "、".join(missing) + "（请先安装后再录制）") if missing else "",
            size=11, color=t.c("danger"), visible=bool(missing))

        self._start_btn = ft.FilledButton(
            "开始录制", icon=ft.Icons.FIBER_MANUAL_RECORD, on_click=self._start)
        self._stop_btn = ft.FilledButton(
            "停止录制", icon=ft.Icons.STOP, disabled=True, on_click=self._stop,
            style=ft.ButtonStyle(bgcolor=t.c("danger"), color=t.c("text_on_accent")))

        card = ft.Container(
            content=ft.Column([
                ft.Row([ft.Icon(ft.Icons.VIDEOCAM, size=18, color=t.c("accent")),
                        ft.Text("屏幕录制（PC 桌面 + 麦克风）", size=13,
                                weight=ft.FontWeight.BOLD, color=t.c("text_primary")),
                        ft.Container(expand=True), self._timer_txt], spacing=8,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Divider(height=1, color=t.c("border")),
                ft.Row([self._monitor_dd, self._mode_dd, self._ts_sw, self._audio_sw],
                       spacing=14, wrap=True),
                ft.Row([self._window_dd], spacing=8),
                ft.Row([self._start_btn, self._stop_btn], spacing=10),
                self._dep_txt,
                self._path_txt,
            ], spacing=10),
            padding=16, bgcolor=t.c("bg_card"), border_radius=8,
            border=ft.border.Border.all(1, t.c("border")))

        # 先填充窗口列表（窗口模式用）
        self._reload_windows()

        # 40 号：切走再切回且仍在录制 → 回填运行态（按钮/计时）+ 补登记
        if self._rec is not None and self._rec.is_running():
            self._set_ui_running(True)
            self._start_timer()
            if getattr(self.app.state, "tasks", None) is not None \
                    and self.app.state.tasks.get("recorder") is None:
                self._task_register("recorder", "PC 录屏",
                                    kind=tasks_mod.KIND_LOCAL, stop_fn=self._stop)
                self._task_update("recorder", detail="录制中")

        return ft.Column([card, ft.Container(expand=True)], spacing=10, expand=True)

    # ── 事件 ──────────────────────────────
    def _on_mode_change(self, e=None) -> None:
        window_mode = self._mode_dd.value == "窗口"
        self._window_dd.disabled = not window_mode
        if window_mode:
            self._reload_windows()
        try:
            self._window_dd.update()
        except Exception:  # noqa: BLE001
            pass

    def _reload_windows(self) -> None:
        try:
            titles = ScreenRecorder.get_available_windows()
        except Exception:  # noqa: BLE001
            titles = []
        self._window_dd.options = [ft.dropdown.Option(x) for x in titles[:80]]
        if titles and not self._window_dd.value:
            self._window_dd.value = titles[0]

    def _start(self, e=None) -> None:
        if self._rec and self._rec.is_running():
            return
        missing = check_dependencies()
        if missing:
            self.app.set_status("缺少依赖，无法录制：" + "、".join(missing), "danger")
            return

        out_dir = record_dir()
        os.makedirs(out_dir, exist_ok=True)
        mode = "window" if self._mode_dd.value == "窗口" else "fullscreen"
        path = os.path.join(out_dir,
                            f"pc_record_{datetime.now():%Y%m%d_%H%M%S}.mp4")
        rec = ScreenRecorder(fps=15, show_timestamp=bool(self._ts_sw.value))
        self._rec = rec
        self._path_txt.value = f"本次输出：{path}"
        self._set_ui_running(True)
        self.app.set_status("录制中…", "warning")
        # 40 号：登记全局任务（修复录制中离页/切页的「静默运行」——指示器/抽屉可见可停）
        self._task_register("recorder", "PC 录屏", kind=tasks_mod.KIND_LOCAL,
                            stop_fn=self._stop)
        self._task_update("recorder", detail="录制中")

        def _work() -> None:
            ok = rec.start(
                path,
                selected_monitor=int(self._monitor_dd.value or 1),
                show_timestamp=bool(self._ts_sw.value),
                capture_mode=mode,
                target_window=self._window_dd.value if mode == "window" else None,
                record_audio=bool(self._audio_sw.value),
            )
            self.app.page.run_thread(lambda: self._after_start(ok))

        threading.Thread(target=_work, daemon=True).start()
        self._start_timer()

    def _stop(self, e=None) -> None:
        rec = self._rec
        if not rec:
            return
        self.app.set_status("正在停止并合成音视频…", "info")
        self._stop_timer()

        def _work() -> None:
            out = rec.stop()
            self.app.page.run_thread(lambda: self._after_stop(out))

        threading.Thread(target=_work, daemon=True).start()

    # ── 回调（主线程）──────────────────────
    def _after_start(self, ok: bool) -> None:
        if ok:
            self.app.set_status("录制中…", "warning")
        else:
            self._task_unregister("recorder")      # 40 号：启动失败 → 出表
            self._set_ui_running(False)
            self._stop_timer()
            self.app.set_status("启动录制失败（详见日志）", "danger")
            self._done("录制启动失败", ok=False)

    def _after_stop(self, out: str) -> None:
        self._task_unregister("recorder")          # 40 号：录制结束 → 出表（进历史）
        self._set_ui_running(False)
        if out:
            self.app.set_status(f"录制完成：{os.path.basename(out)}", "success")
            self._path_txt.value = f"已保存：{out}"
            self._done("录制完成")
        else:
            self.app.set_status("录制结束（未产出文件）", "warning")
            self._done("录制失败", ok=False)
        try:
            self._path_txt.update()
        except Exception:  # noqa: BLE001
            pass

    def _set_ui_running(self, running: bool) -> None:
        self._start_btn.disabled = running
        self._stop_btn.disabled = not running
        for ctl in (self._start_btn, self._stop_btn):
            try:
                ctl.update()
            except Exception:  # noqa: BLE001
                pass

    # ── 计时 ──────────────────────────────
    def _start_timer(self) -> None:
        self._stop_timer()
        self._tick()

    def _stop_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _tick(self) -> None:
        rec = self._rec
        if not rec or not rec.is_running():
            return
        text = time.strftime("%H:%M:%S", time.gmtime(rec.get_elapsed_time()))
        try:
            self.app.page.run_thread(lambda: self._set_timer_text(text))
        except Exception:  # noqa: BLE001
            pass
        self._timer = threading.Timer(1.0, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _set_timer_text(self, text: str) -> None:
        self._timer_txt.value = text
        try:
            self._timer_txt.update()
        except Exception:  # noqa: BLE001
            pass
