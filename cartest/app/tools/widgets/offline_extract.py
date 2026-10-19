# -*- coding: utf-8 -*-
"""离线提取面板 — CbmLogextractor 独立入口（不依赖流水线）

对任意日志目录做「字段提取 → 角色对话还原 → 导出表格」，交付物 = **HTML 总报告
一份**（``all_report_<stamp>.html``；2026-10-08 起唯一对接产物，见
``core.parser.offline`` / ``report_view``）。面板挂载于侧栏「离线提取」
独立页（``pages.build_offline``）。工具本体保持独立（不搬代码——见 offline.py 设计决策）。
"""
from __future__ import annotations

import os

import flet as ft
from ...ui.components import sync_text_value

from ....core.parser import offline


class OfflineExtractPanel:
    """离线提取（目录输入/选择 → 提取 → 报告路径与打开）

    ``on_report``（可选）：提取成功回调，参数为报告路径——独立页用它把报告
    **自动载入窗口内的报告视图**（2026-09-21 用户裁决）。
    """

    def __init__(self, ctx, on_report=None) -> None:  # ctx: ToolContext
        self.ctx = ctx
        self.on_report = on_report
        t = ctx.theme
        self._report: str = ""
        self._dir_field = ft.TextField(
            on_change=sync_text_value,
            label="日志目录",
            value=str(ctx.cfg.get("offline_extract_dir", "")),
            hint_text=r"例：D:\...\logs_20260917_Android（含 *.log）",
            dense=True, expand=True, text_size=12)
        self._status = ft.Text("", size=11, color=t.c("text_hint"), selectable=True)
        self._btn_pick = ft.OutlinedButton(
            "选择目录", icon=ft.Icons.FOLDER_OPEN, on_click=self._pick)
        self._btn_run = ft.FilledButton(
            "提取", icon=ft.Icons.PLAY_ARROW, on_click=self._extract)
        self._btn_report = ft.OutlinedButton(
            "打开报告", icon=ft.Icons.DESCRIPTION, disabled=True,
            on_click=self._open_report)
        self._btn_dir = ft.OutlinedButton(
            "打开目录", icon=ft.Icons.FOLDER, disabled=True, on_click=self._open_dir)

    def build(self) -> ft.Control:
        t = self.ctx.theme
        latest = ""
        try:
            p = offline.latest_report()
            if p:
                latest = f"（最近报告：{os.path.basename(str(p))}）"
        except Exception:  # noqa: BLE001
            pass
        tip = ft.Text(
            "对任意日志目录提取 → 对话还原 → 一份 HTML 全量报告（CbmLogextractor）" + latest,
            size=10, color=t.c("text_hint"))
        # 注意：目录字段 expand=True → **不能**与按钮同放 Row(wrap=True)
        # （Wrap 不支持 Expanded，会整页渲染失败）——目录独占一行
        return ft.Column([
            ft.Row([self._dir_field], spacing=8),
            ft.Row([self._btn_pick, self._btn_run, self._btn_report, self._btn_dir],
                   spacing=8, wrap=True),
            tip,
            self._status,
        ], spacing=6)

    # ── 动作 ──
    async def _pick(self, e=None) -> None:
        """选择目录（仅桌面模式；Web 不支持 FilePicker）

        注意：0.85 的 ``FilePicker`` 是 **Service**（``flet.controls.services.file_picker``），
        构造时经 ``Service.init()`` 自动注册进 ``page._services``；**不要再挂
        ``page.overlay``**（挂上去会被当控件下发 → 前端报 ``Unknown control: FilePicker``）。
        """
        if getattr(self.ctx.page, "web", False):
            self._set_status("Web 模式请手动输入目录（桌面模式可选目录）", "warning")
            return
        try:
            picker = ft.FilePicker()          # 服务：构造即注册，无需 overlay
            path = await picker.get_directory_path(dialog_title="选择日志目录")
            if path:
                self._dir_field.value = path
                self._safe_update(self._dir_field)
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"选择目录失败：{ex}", "danger")

    def _extract(self, e=None) -> None:
        d = (self._dir_field.value or "").strip()
        if not d or not os.path.isdir(d):
            self._set_status("请填写存在的日志目录", "danger")
            return
        if not offline.available():
            self._set_status(
                f"CbmLogextractor 不可用（检查 {offline.tool_dir()} 或设 CARTEST_LOGEXTRACT_DIR）",
                "danger")
            return
        try:
            self.ctx.cfg.set("offline_extract_dir", d)
        except Exception:  # noqa: BLE001
            pass
        self._set_status("提取中…（大目录可能需数十秒，进度见事件窗口）", "warning")
        self._btn_run.disabled = True
        self._safe_update(self._btn_run)

        def _work() -> None:
            try:
                with self.ctx.op("对话还原", tag="工具",
                                 param=os.path.basename(d.rstrip("\\/"))):
                    report = offline.extract_report(
                        d, on_log=lambda msg, tag="info": self._log(msg))
                    self._report = str(report)
                    self.ctx.append_log_to_panel(f"[OK] 报告：{self._report}")
            except offline.LogextractUnavailable as ex:
                self._finish(False, f"CbmLogextractor 不可用：{ex}")
                return
            except Exception as ex:  # noqa: BLE001
                self._finish(False, f"提取失败：{ex}")
                return
            self._finish(True, f"✔ 报告：{self._report}")

        self.ctx.page.run_thread(_work)

    def _open_report(self, e=None) -> None:
        self._open_path(self._report)

    def _open_dir(self, e=None) -> None:
        if self._report:
            self._open_path(os.path.dirname(self._report))

    def _open_path(self, path: str) -> None:
        if not path:
            return
        if getattr(self.ctx.page, "web", False):
            self._log(f"[界面] Web 模式：路径 {path}")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"打开失败：{ex}", "danger")

    # ── 辅助 ──
    def _log(self, msg: str) -> None:
        try:
            self.ctx.append_log_to_panel(msg)
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, text: str, color_key: str = "text_hint") -> None:
        self._status.value = text
        self._status.color = self.ctx.theme.c(color_key)
        self._safe_update(self._status)

    def _finish(self, ok: bool, msg: str) -> None:
        self._set_status(msg, "success" if ok else "danger")
        try:
            self.ctx.notify_done("提取完成" if ok else "提取失败", ok=ok)
        except Exception:  # noqa: BLE001
            pass
        self._btn_run.disabled = False
        self._btn_report.disabled = not ok
        self._btn_dir.disabled = not ok
        self._safe_update(self._btn_run)
        self._safe_update(self._btn_report)
        self._safe_update(self._btn_dir)
        if ok and self._report and self.on_report is not None:
            try:
                self.on_report(self._report)      # 自动载入报告视图
            except Exception as ex:  # noqa: BLE001
                self._log(f"[界面] 载入报告视图失败：{ex}")

    @staticmethod
    def _safe_update(ctl) -> None:
        try:
            ctl.update()
        except Exception:  # noqa: BLE001
            pass
