# -*- coding: utf-8 -*-
"""日志终端 Tab - UI 层，业务逻辑委托给 services/ 下的模块

功能模块：
  - ADB 连接 / Root+Remount / 重启设备 —— AdbService
  - Logcat 实时抓取（含计时器、进度条）—— LogcatService
  - 截图（Pillow 时间戳水印）—— AdbService
  - 日志拉取/清除/大小检查 —— AdbService
"""
from __future__ import annotations

import logging
import os
import threading
import time

import flet as ft

from ...core import config_center
from .. import tasks as tasks_mod
from .base_tab import BaseTab
from .constants import (
    REMOTE_SCREENSHOT_TMP,
    LOGCAT_BUFFERS, LOGCAT_MAX_DURATION,
    UI_LOG_MAX_LINES,
)
from .adb_tool import ADBTool
from .services.adb_service import AdbService
from .services.line_batcher import LineBatcher
from .services.logcat_service import LogcatService

_log = logging.getLogger("log_tab")

# 可选依赖：Pillow 用于截图时间戳水印
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


class LogTab(BaseTab):
    """日志终端 Tab — UI 组合层"""

    def __init__(self, app):
        super().__init__(app)
        _log.info("[__init__] LogTab created, app=%s", type(app).__name__)

        # ── 创建 Service 实例 ──
        self._adb = AdbService(
            on_log=self._append_log,
            on_status=lambda t, k: self.app.set_status(t, k),
        )
        self._logcat = LogcatService(
            on_log=self._logcat_feed,
            max_duration=LOGCAT_MAX_DURATION,
        )
        # Logcat 高频行 → 100ms 批量合并上屏（2026-09-23，35 号文档 §六）
        self._logcat_batcher = LineBatcher(on_flush=self._append_log_batch)
        # ── 开关状态持久引用 ──
        self.screenshot_ts_enabled = self.app.cfg.get("screenshot_timestamp_enabled", True)
        self._logcat_clear_enabled = self.app.cfg.get("logcat_clear_enabled", False)
        self._adb_running = False

        # ── 日志数据（仅保留行列表，兼容 save_state）──
        self._log_lines = ["就绪 - 等待操作…"]
        self._log_lock = threading.Lock()

        # UI 控件引用（build 后赋值）
        self._btn_logcat_start: ft.ElevatedButton | None = None
        self._btn_logcat_stop: ft.ElevatedButton | None = None
        self._logcat_progress: ft.ProgressBar | None = None
        self._logcat_timer_label: ft.Text | None = None
        self._logcat_dur_label: ft.Text | None = None
        self._logcat_clear_cb: ft.Checkbox | None = None
        self._logcat_buffer_dd: ft.Dropdown | None = None
        self._logcat_op: int | None = None      # Logcat 抓取操作行（停止时收口耗时）
        self._logcat_ui_running = False         # UI 侧运行态镜像（tick 对账用）

    # ──────────────────────────────────────────
    #  Build
    # ──────────────────────────────────────────
    def build(self):
        t = self.t
        _log.info("[build] ENTER, _log_lines=%d", len(self._log_lines))

        # 上半部分：操作卡片（**只占自身高度**——2026-10-12 用户反馈「窗口起点太低」：
        # 原来与日志面板各占一半（expand=1），内容不满时空白全压在日志窗口上方）
        top_content = ft.Column(
            controls=[
                self._build_connection_bar(),
                self._build_logcat_card(),
                self._build_toolbar(),
            ],
            spacing=6,
        )
        top = ft.Container(
            content=top_content,
            padding=ft.padding.Padding(top=12, left=12, right=12, bottom=6),
        )

        # 下半部分：日志面板（新版：工具条 + 暗色控制台行视图，见 23-设计）
        log_panel = ft.Container(
            content=self.app.build_log_panel(),
            border=ft.border.Border(
                top=ft.border.BorderSide(1, t.c("border")),
            ),
        )

        result = ft.Container(
            content=ft.Column(
                controls=[
                    top,
                    ft.Container(content=log_panel, expand=1),
                ],
                spacing=0,
                expand=True,
            ),
            padding=0,
        )
        # 40 号：订阅 tick 刷新（进度条/计时文案由 handle 驱动）；若抓取已在跑
        # （切走再切回），立即还原按钮运行态
        self._tick_subscribe(self._tick_refresh)
        if self._logcat.is_running:
            self._set_logcat_ui_running(True)
        _log.info("[build] DONE")
        return result

    # ── 生命周期管理 ──
    def on_detach(self):
        """离页**不停抓取**（40 号新语义）：任务归 ``state.tasks`` 持有

        进度/计时经 handle + tick 继续驱动；本页只退订 tick（super）。
        停止途径：本页「停止抓取」按钮 / 顶栏任务抽屉 / 切设备「停止并切换」。
        """
        super().on_detach()

    def on_show(self):
        pass

    def save_state(self) -> dict:
        return {"log_lines": list(self._log_lines)}

    def restore_state(self, state: dict):
        # 日志显示已迁移到右侧面板，restore_state 仅维护 _log_lines
        if "log_lines" in state:
            with self._log_lock:
                self._log_lines = state["log_lines"]
                self._log_lines = self._log_lines[-UI_LOG_MAX_LINES:]
        # 不再做 UI 控件更新（已迁移）

    def _append_log(self, line: str):
        """单行入口（AdbService 等低频调用方）"""
        self._append_log_batch([line])

    def _logcat_feed(self, line: str):
        """Logcat 流入口：先入批量缓冲，由 ``LineBatcher`` 定时合并上屏

        修复（2026-09-23，35 号文档 §六）：逐行调度 UI 线程会把消费速度压到
        ~16 行/秒，日志洪峰/缓冲积压时面板严重滞后；合并后开销降为 O(批)。
        """
        self._logcat_batcher.feed(line)

    def _append_log_batch(self, lines: list[str]):
        """批量维护 _log_lines（兼容 save_state）并一次性转发右侧日志面板"""
        if not lines:
            return
        with self._log_lock:
            self._log_lines.extend(lines)
            if len(self._log_lines) > UI_LOG_MAX_LINES:
                self._log_lines = self._log_lines[-UI_LOG_MAX_LINES:]
        _log.info("[_append_log] lines=%d, total=%d", len(lines), len(self._log_lines))

        # 转发到 main.py 右侧日志面板（线程安全）
        try:
            self.app.append_log_to_panel("\n".join(lines))
        except Exception as ex:
            _log.error("[_append_log] append_log_to_panel failed: %s", ex)

    # ── 连接栏 ──
    def _build_connection_bar(self):
        t = self.t
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.ElevatedButton("初始化连接", icon=ft.icons.Icons.LINK,
                                      tooltip="adb devices → adb root → adb remount（一键）",
                                      on_click=self._initialize),
                    ft.ElevatedButton(
                        "重启设备",
                        icon=ft.icons.Icons.RESTART_ALT,
                        style=ft.ButtonStyle(
                            bgcolor=t.c("warning"),
                            color=t.c("text_on_accent"),
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=self._reboot,
                    ),
                    ft.Container(expand=True),
                    ft.Switch(
                        label="截图时间戳",
                        value=self.screenshot_ts_enabled,
                        on_change=self._on_screenshot_ts_toggle,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, right=12),
        )

    # ── Logcat 控制卡片 ──
    def _build_logcat_card(self):
        t = self.t
        self._logcat_progress = ft.ProgressBar(
            width=200, value=0, bar_height=8,
            bgcolor=t.c("bg_card"), color=t.c("accent"),
        )
        self._logcat_timer_label = ft.Text(
            "", size=12, font_family="Consolas",
            weight=ft.FontWeight.BOLD, color=t.c("text_secondary"),
            selectable=False,
        )
        self._logcat_dur_label = ft.Text(
            f"{LOGCAT_MAX_DURATION}s", size=11, color=t.c("text_secondary"),
        )
        self._logcat_clear_cb = ft.Checkbox(
            label="抓取前清空", value=self._logcat_clear_enabled,
            label_style=ft.TextStyle(size=11, color=t.c("text_primary")),
            on_change=lambda e: [
                setattr(self, "_logcat_clear_enabled", e.control.value),
                self.app.cfg.set("logcat_clear_enabled", e.control.value)
            ],
            check_color=t.c("text_on_accent"), active_color=t.c("accent"),
        )
        self._logcat_buffer_dd = ft.Dropdown(
            label="缓冲区",
            value=LOGCAT_BUFFERS[0][1],
            options=[ft.dropdown.Option(b[1], text=b[0]) for b in LOGCAT_BUFFERS],
            width=170, text_size=12, label_style=ft.TextStyle(size=11),
            dense=True, border_radius=8,
        )
        self._btn_logcat_start = ft.ElevatedButton(
            "开始抓取", icon=ft.icons.Icons.PLAY_ARROW,
            style=ft.ButtonStyle(
                bgcolor=t.c("accent"), color=t.c("text_on_accent"),
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            on_click=self._start_logcat,
        )
        self._btn_logcat_stop = ft.ElevatedButton(
            "停止抓取", icon=ft.icons.Icons.STOP,
            style=ft.ButtonStyle(
                bgcolor=t.c("text_disabled"), color=t.c("text_on_accent"),
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            on_click=self._stop_logcat, disabled=True,
        )

        card = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("📊 Logcat 实时抓取",
                                    size=12, weight=ft.FontWeight.BOLD,
                                    color=t.c("text_primary")),
                            ft.Container(expand=True),
                            self._logcat_timer_label,
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(height=4),
                    ft.Row(
                        controls=[
                            self._btn_logcat_start, self._btn_logcat_stop,
                            ft.Container(width=8),
                            self._logcat_progress, ft.Container(width=8),
                            self._logcat_dur_label, ft.Container(width=16),
                            self._logcat_clear_cb, ft.Container(expand=True),
                            self._logcat_buffer_dd,
                        ],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=2,
            ),
            padding=ft.padding.Padding(left=16, top=10, right=16, bottom=10),
            bgcolor=t.c("bg_card"), border_radius=10,
            border=ft.border.Border.all(1, t.c("border")),
        )
        return ft.Container(
            content=card,
            padding=ft.padding.Padding(left=12, right=12, top=4, bottom=4),
        )

    # ── 功能工具栏 ──
    def _build_toolbar(self):
        t = self.t
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.ElevatedButton("抓日志", icon=ft.icons.Icons.DOWNLOAD,
                                      on_click=self._pull_logs),
                    ft.ElevatedButton("清日志", icon=ft.icons.Icons.DELETE_OUTLINE,
                                      style=ft.ButtonStyle(
                                          bgcolor=t.c("danger"),
                                          color=t.c("text_on_accent"),
                                      ),
                                      on_click=self._clear_logs),
                    ft.ElevatedButton("查看大小", icon=ft.icons.Icons.STORAGE,
                                      on_click=self._check_log_size),
                    ft.ElevatedButton("截图", icon=ft.icons.Icons.PHOTO_CAMERA,
                                      style=ft.ButtonStyle(
                                          bgcolor=t.c("success"),
                                          color=t.c("text_on_accent"),
                                      ),
                                      on_click=self._screenshot),
                    ft.VerticalDivider(width=1, color=t.c("border")),
                    ft.ElevatedButton("打开目录", icon=ft.icons.Icons.FOLDER_OPEN,
                                      on_click=self._open_dir),
                    ft.ElevatedButton("清空日志文本", icon=ft.icons.Icons.CLEAR_ALL,
                                      on_click=self._clear_log_text),
                ],
                spacing=6, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, top=6, right=12, bottom=6),
        )

    # ══════════════════════════════════════
    #  事件处理（委托给 Service）
    # ══════════════════════════════════════

    # ── 截图时间戳开关 ──
    def _on_screenshot_ts_toggle(self, e):
        self.screenshot_ts_enabled = e.control.value
        self.app.cfg.set("screenshot_timestamp_enabled", e.control.value)
        self._append_log(f"截图时间戳: {'开启' if e.control.value else '关闭'}")

    # ── ADB 基础操作 ──
    def _initialize(self, e=None):
        """一键初始化连接：`adb devices` → `adb root` → `adb remount` + **状态探测**

        2026-09-22：原「ADB 连接」+「Root+Remount」合并为一颗按钮；点击后**顺带探测**
        并列出设备状态（是否 root / 分区只读还是可写 / 有无 overlay 可写层 / verity），
        需要重启才生效时明确提示——避免"push 失败才发现"。
        """
        _log.info("[_initialize] button clicked")

        def _run():
            with self.app.op("初始化连接", tag="adb",
                             param="devices → root → remount + 状态探测") as o:
                try:
                    ok, why = self._adb.initialize()
                except Exception as ex:  # noqa: BLE001
                    o.fail()
                    self.app.set_status(f"初始化连接异常：{ex}", "danger")
                    self._done("连接失败", ok=False)
                    return
                if not ok:
                    o.fail()
                # 状态探测（与是否连上无关，尽力而为；失败不致命）
                state: dict = {}
                try:
                    state = self._adb.probe_state()
                    for line in state.get("lines", []):
                        self.app.append_log_to_panel(line)
                    if state.get("hint"):
                        self.app.append_log_to_panel(f"[建议] {state['hint']}")
                except Exception as ex:  # noqa: BLE001
                    self.app.append_log_to_panel(f"[状态] 探测失败：{ex}")

                summary = state.get("summary") or ""
                if not ok:
                    self.app.set_status(f"初始化连接失败：{why}", "danger")
                    self._done("连接失败", ok=False)
                elif state.get("need_reboot"):
                    self.app.set_status(
                        f"初始化连接完成（{summary}）—— 系统分区写入需先「重启设备」"
                        f"使 remount 生效", "warning")
                    self._done("设备已连接")
                    self._notify_reboot_needed()
                else:
                    self.app.set_status(f"初始化连接完成（{summary}）", "success")
                    self._done("设备已连接")
        self._run_in_thread(_run)

    def _notify_reboot_needed(self) -> None:
        """需要重启才生效时的醒目提示（轻提示；无该能力则只留在事件块/状态行）"""
        try:
            self.app.snack("⚠ 系统分区 remount 需重启生效："
                           "点「重启设备」后再执行系统写入（如 Monkey 三件套）")
        except Exception:  # noqa: BLE001
            pass

    def _check_adb(self, e=None):
        _log.info("[_check_adb] button clicked")
        def _run():
            _log.info("[_check_adb] _run thread started")
            with self.app.op("ADB 连接", tag="adb") as o:
                out = self._adb.check_adb()
                if "device" not in out:
                    o.fail()
                _log.info("[_check_adb] adb output len=%d, first=%.80s", len(out), out)
                self._append_log(out)          # 输出归入事件块
            self.app.set_status(
                "ADB 已连接" if "device" in out else "ADB 未连接",
                "success" if "device" in out else "danger",
            )
        self._run_in_thread(_run)

    def _root_remount(self, e=None):
        _log.info("[_root_remount] button clicked")
        def _run():
            _log.info("[_root_remount] _run thread started")
            op = self.app.begin_op("Root+Remount", tag="adb")
            try:
                self._adb.root_remount()
            except Exception:
                self.app.end_op(op, ok=False)
                raise
            self.app.end_op(op, ok=True)
            self.app.set_status("Root+Remount 完成", "success")
        self._run_in_thread(_run)

    def _reboot(self, e=None):
        _log.info("[_reboot] button clicked")
        def _run():
            _log.info("[_reboot] _run thread started")
            op = self.app.begin_op("重启设备", tag="设备")
            try:
                self._adb.reboot()
            except Exception:
                self.app.end_op(op, ok=False)
                raise
            self.app.end_op(op, ok=True)
            self.app.set_status("设备重启中", "warning")
        self._run_in_thread(_run)

    # ── 日志操作 ──
    def _pull_logs(self, e=None):
        _log.info("[_pull_logs] button clicked")
        ts = time.strftime("%Y%m%d_%H%M%S")
        # 40 号 §4：设备忙（其他设备任务/流水线运行中）→ 入队，完成后自动执行；
        # "started" 时队列已执行 starter，勿再直跑（防双重执行）
        verdict = self._submit_device_task(f"pull:{ts}", "抓日志",
                                           lambda: self._begin_pull(ts))
        if verdict == "queued":
            self._queued_feedback(f"pull:{ts}", "抓日志")
            return
        if verdict == "bypass":
            self._begin_pull(ts)

    def _begin_pull(self, ts: str):
        """真正拉取日志（可由队列出队触发；放后台线程跑，不堵 UI/队列线程）"""
        def _work():
            base = config_center.output_root()     # 可配置；留空＝工具所在目录 logs/
            _log.info("[_pull_logs] base=%s, ts=%s", base, ts)
            op = self.app.begin_op("抓日志", tag="设备", param=f"批次 {ts}")
            try:
                self._adb.pull_logs(base, ts)
            except Exception:
                self.app.end_op(op, ok=False)
                self._done("抓日志失败", ok=False)
                raise
            self.app.end_op(op, ok=True)
            self._task_unregister(f"pull:{ts}")    # 40 号：完成即出表（触发出队）
            self.app.set_status("日志已拉取", "success")
            self._done("抓日志完成")
        self._run_in_thread(_work)

    def _clear_logs(self, e=None):
        _log.info("[_clear_logs] button clicked")
        def _run():
            _log.info("[_clear_logs] _run thread started")
            op = self.app.begin_op("清日志", tag="设备")
            try:
                self._adb.clear_logs()
            except Exception:
                self.app.end_op(op, ok=False)
                self._done("清日志失败", ok=False)
                raise
            self.app.end_op(op, ok=True)
            self._done("日志已清空")
        self._run_in_thread(_run)

    def _check_log_size(self, e=None):
        _log.info("[_check_log_size] button clicked")
        def _run():
            _log.info("[_check_log_size] _run thread started")
            with self.app.op("查看大小", tag="设备"):
                self._adb.check_log_size()
        self._run_in_thread(_run)

    def _clear_log_text(self, e=None):
        _log.info("[_clear_log_text] clearing log text, current lines=%d", len(self._log_lines))
        with self._log_lock:
            self._log_lines = []
        # 清空日志面板（模型 + 视图；23 设计：统一走工具页 API）
        try:
            self.app.clear_log_panel()
        except Exception:
            pass

    # ── 截图 ──
    def _screenshot(self, e=None):
        _log.info("[_screenshot] button clicked")
        ts = time.strftime("%Y%m%d_%H%M%S")
        shot_dir = config_center.output_dir("screenshots")   # 随本地输出根目录
        os.makedirs(shot_dir, exist_ok=True)
        ts_enabled = self.screenshot_ts_enabled              # 快照：命名与实际执行保持一致
        # 只产出**一张**图：需水印时中间原图落 _raw、水印后即删；否则直接落成品名
        if ts_enabled and HAS_PIL:
            local_tmp = os.path.join(shot_dir, f"car_{ts}_raw.png")   # 中间文件，水印后即删
            final_dst = os.path.join(shot_dir, f"car_{ts}_ts.png")
        else:
            local_tmp = final_dst = os.path.join(shot_dir, f"car_{ts}.png")

        def _run():
            _log.info("[_screenshot] _run thread started")
            with self.app.op("截图", tag="设备",
                             param=("含时间戳水印" if ts_enabled else "无水印")) as o:
                result = self._adb.screenshot(
                    timestamp_enabled=ts_enabled,
                    has_pil=HAS_PIL,
                    remote_tmp=REMOTE_SCREENSHOT_TMP,
                    local_tmp=local_tmp,
                    final_dst=final_dst,
                    add_watermark=self._add_timestamp_watermark,
                )
                if not result:
                    o.fail()
                if result:                     # 输出归入事件块
                    self._append_log(f"[OK] 截图保存: {result}")
            if result:
                self.app.set_status("截图完成", "success")
                self._done("截图完成")
            else:
                self._done("截图失败", ok=False)
        self._run_in_thread(_run)

    @staticmethod
    def _add_timestamp_watermark(src_path: str, dst_path: str):
        """用 Pillow 在截图左上角添加时间戳水印"""
        img = Image.open(src_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        time_str = time.strftime("%m%d  %H:%M:%S")
        try:
            font = ImageFont.truetype("msyh.ttc", 28)
        except IOError:
            font = ImageFont.load_default()
        x, y = 20, 36
        for dx in [-2, -1, 0, 1, 2]:
            for dy in [-2, -1, 0, 1, 2]:
                draw.text((x + dx, y + dy), time_str, fill=(0, 0, 0), font=font)
        draw.text((x, y), time_str, fill=(255, 255, 255), font=font)
        img.save(dst_path)

    # ── Logcat ──
    def _start_logcat(self, e=None):
        _log.info("[_start_logcat] button clicked")
        if self._logcat.is_running:
            self._append_log("[WARN] Logcat 已在运行")
            return
        # 40 号 §4：设备忙（其他设备任务/流水线运行中）→ 入队，完成后自动开始；
        # "started" 时队列已执行 starter，勿再直跑（防双重执行）
        verdict = self._submit_device_task("logcat", "日志抓取",
                                           lambda: self._begin_logcat(register=False))
        if verdict == "queued":
            self._queued_feedback("logcat", "日志抓取")
            return
        if verdict == "bypass":
            self._begin_logcat()

    def _begin_logcat(self, register: bool = True):
        """真正启动抓取（可由队列出队触发——工作线程调用；UI 调用均经 on_main）"""
        self._set_logcat_ui_running(True)
        # 设备占用：抓流期间禁止顶栏切换设备（停止回调 on_stopped 必然到达 → 释放）
        self.app.state.device_acquire("logcat")
        if register:
            # 40 号：登记全局任务（跨页存活 + 顶栏指示器 + 任务抽屉可停）
            self._task_register("logcat", "日志抓取", kind=tasks_mod.KIND_DEVICE,
                                stop_fn=self._logcat.stop)
        buffer_label = self._logcat_buffer_dd.value or "-b all"
        self._logcat_op = self.app.begin_op(
            "Logcat 抓取", tag="设备",
            param=f"缓冲 {buffer_label} · 实时 · 最长 {LOGCAT_MAX_DURATION}s")   # 停止时收口耗时

        def _reader():
            _log.info("[_start_logcat] _reader thread started")
            self._logcat_batcher.start()
            try:
                self._logcat.start(
                    save_dir=config_center.output_dir("logcat"),
                    buffer_label=buffer_label,
                    clear_enabled=self._logcat_clear_enabled,
                    on_progress=self._on_logcat_progress,
                    on_stopped=self._on_logcat_stopped,
                    device_id=self._current_device_id(),
                )
            except Exception:
                if self._logcat_op is not None:
                    self.app.end_op(self._logcat_op, ok=False)
                    self._logcat_op = None
                raise
            finally:
                self._logcat_batcher.stop()

        self._run_in_thread(_reader)
        self.app.set_status("Logcat 抓取中…", "success")

    def _on_logcat_progress(self, progress_value: float, elapsed: int, remaining: int):
        """Logcat 进度回调（从后台线程调用）

        40 号：后台线程**只写 handle**——UI（进度条/计时文案）由主循环 tick 读
        handle 刷新（``_tick_refresh``）。离页后控件树已卸载，这里不再碰控件。
        """
        self._task_update(
            "logcat",
            progress=progress_value,
            detail=f"已运行 {elapsed}s  /  剩余 {remaining}s",
            data={"remaining": remaining, "elapsed": elapsed},
        )

    def _on_logcat_stopped(self, _save_path: str):
        """Logcat 停止回调（含异常/抽屉停止/切设备路径 → 设备占用在此释放）"""
        self._task_unregister("logcat")            # 40 号：移出活动表、进历史
        try:
            self.app.state.device_release("logcat")
        except Exception:
            pass
        if self._logcat_op is not None:            # 收口 Logcat 抓取耗时
            self.app.end_op(self._logcat_op, ok=True)
            self._logcat_op = None
        def _reset():
            self._btn_logcat_start.disabled = False
            self._btn_logcat_stop.disabled = True
            self._logcat_progress.value = 0
            self._logcat_timer_label.value = ""
            try:
                self._btn_logcat_start.update()
                self._btn_logcat_stop.update()
                self._logcat_progress.update()
                self._logcat_timer_label.update()
                self.app.page.update()
            except Exception:
                pass

        try:
            _on_main = getattr(self.app, "on_main", None)
            (_on_main or self.app.page.run_thread)(_reset)
        except Exception:
            pass
        self.app.set_status("Logcat 已停止", "warning")
        self._done("抓取已停止")

    def _stop_logcat(self, e=None):
        _log.info("[_stop_logcat] button clicked")
        self._logcat.stop()
        queue = getattr(self.app.state, "device_queue", None)
        if queue is not None:
            queue.cancel("logcat")                 # 排队中的抓取一并取消
        self._set_logcat_ui_running(False)

    def _set_logcat_ui_running(self, running: bool):
        """切换 Logcat 按钮状态（同步 UI 镜像，供 tick 对账）"""
        self._logcat_ui_running = running

        def _do():
            self._btn_logcat_start.disabled = running
            self._btn_logcat_stop.disabled = not running
            if not running:
                self._logcat_progress.value = 0
                self._logcat_timer_label.value = ""
            try:
                self._btn_logcat_start.update()
                self._btn_logcat_stop.update()
                self._logcat_progress.update()
                self._logcat_timer_label.update()
                self.app.page.update()
            except Exception:
                pass

        try:
            _on_main = getattr(self.app, "on_main", None)
            (_on_main or self.app.page.run_thread)(_do)
        except Exception:
            pass

    def _tick_refresh(self, ticks: int = 0):
        """主循环 tick（250ms）：按 handle / 服务实况**只改值**刷新运行态 UI

        40 号：进度与计时的唯一驱动——后台线程写 handle，这里读 handle 改控件值。
        同时对账按钮态（服务停止回调与 UI 镜像漂移时纠正），保证离页切回后一致。
        """
        running = self._logcat.is_running
        if running != self._logcat_ui_running:
            self._set_logcat_ui_running(running)
        if not running:
            return
        reg = getattr(self.app.state, "tasks", None)
        h = reg.get("logcat") if reg is not None else None
        if h is None:
            return
        if self._logcat_progress is not None and h.progress >= 0:
            self._logcat_progress.value = h.progress
        if self._logcat_timer_label is not None:
            self._logcat_timer_label.value = h.detail
            remaining = (h.data or {}).get("remaining")
            self._logcat_timer_label.color = (
                "#F59E0B" if isinstance(remaining, int) and remaining < 30 else None)
        try:
            if self._logcat_progress is not None:
                self._logcat_progress.update()
            if self._logcat_timer_label is not None:
                self._logcat_timer_label.update()
        except Exception:
            pass

    # ── 辅助 ──

    def _current_device_id(self) -> str:
        """当前设备序列号（顶栏选择；空串回退 adb 默认设备）"""
        state = getattr(self.app, "state", None)
        return str(getattr(state, "device", "") or "")

    def _open_dir(self, e=None):
        ADBTool.open_folder(config_center.output_root())
