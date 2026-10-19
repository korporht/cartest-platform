# -*- coding: utf-8 -*-
"""Monkey 压测页 — 配置 → 部署 / 一键流 / 停止 / 拉取分析 / 报告

框架版：链路完整、离线可测；**真机全流程验证留后续任务**
（见 19-Monkey压测集成设计.md §四）。

2026-10-12 用户裁决：**报告并入日志窗口**——原「报告（近 10 份）」卡片删除，
报告路径改为写进底部**事件窗口**（点「报告列表」列一次，拉取+分析完成后自动列）。
页面自此挂标准事件窗口（``with_log=True``，与「应用与传输」同形：内容自然高度、
窗口铺满剩余）。
"""
from __future__ import annotations

import logging
import os
import time

import flet as ft

from ...core import monkey as monkey_mod
from .base_tab import BaseTab
from .. import tasks as tasks_mod
from ..ui.components import sync_text_value
from .services.monkey_service import MonkeyService

log = logging.getLogger("monkey_tab")

DEFAULT_PKG = "com.lioncarmind.car"


def default_package() -> str:
    """Monkey 默认包名（项目档案 → ``DEFAULT_PKG`` 兜底；30/31 号可配置分离）"""
    from ...core import project
    return str(project.get("device.main_package", DEFAULT_PKG))


class MonkeyTab(BaseTab):
    """Monkey 压测（工具页：配置 + 动作 + 状态 + 报告）"""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._ctl = monkey_mod.MonkeyController()
        self._svc = MonkeyService(self._ctl, on_log=self._log,
                                  on_result=self._on_result, poll_interval=60.0)
        self._pkg = str(self.app.cfg.get("monkey_pkg", default_package()))
        self._count = str(self.app.cfg.get("monkey_count", monkey_mod.DEFAULT_COUNT))
        self._throttle = str(self.app.cfg.get("monkey_throttle", monkey_mod.DEFAULT_THROTTLE))
        self._clean_logs = bool(self.app.cfg.get("monkey_clean_logs", False))
        self._clean_dropbox = bool(self.app.cfg.get("monkey_clean_dropbox", False))
        self._pkg_field: ft.TextField | None = None
        self._count_field: ft.TextField | None = None
        self._throttle_field: ft.TextField | None = None
        self._clean_logs_cb: ft.Checkbox | None = None
        self._clean_dropbox_cb: ft.Checkbox | None = None
        self._flow_op: int | None = None    # 一键流操作行（begin_op/end_op 耗时）

    # ── 生命周期 ──
    def on_detach(self) -> None:
        """离页**不停一键流**（40 号新语义）：任务归 ``state.tasks`` 持有

        停止途径：本页「停止」按钮 / 顶栏任务抽屉（stop_fn = svc.cancel）。
        """
        super().on_detach()

    def save_state(self) -> dict:
        return {
            "pkg": self._pkg_field.value if self._pkg_field else self._pkg,
            "count": self._count_field.value if self._count_field else self._count,
            "throttle": self._throttle_field.value if self._throttle_field else self._throttle,
            "clean_logs": bool(self._clean_logs_cb.value) if self._clean_logs_cb else self._clean_logs,
            "clean_dropbox": bool(self._clean_dropbox_cb.value) if self._clean_dropbox_cb else self._clean_dropbox,
        }

    def restore_state(self, state: dict) -> None:
        self._pkg = str(state.get("pkg", self._pkg))
        self._count = str(state.get("count", self._count))
        self._throttle = str(state.get("throttle", self._throttle))
        self._clean_logs = bool(state.get("clean_logs", self._clean_logs))
        self._clean_dropbox = bool(state.get("clean_dropbox", self._clean_dropbox))

    # ── UI ──
    def build(self) -> ft.Control:
        t = self.t
        self._pkg_field = ft.TextField(on_change=sync_text_value, label="包名",
                                       value=self._pkg, dense=True, expand=True)
        self._count_field = ft.TextField(on_change=sync_text_value, label="事件次数",
                                         value=self._count, dense=True, width=130)
        self._throttle_field = ft.TextField(on_change=sync_text_value, label="间隔(ms)",
                                            value=self._throttle, dense=True, width=110)
        self._clean_logs_cb = ft.Checkbox(label="清理 monkey 日志", value=self._clean_logs,
                                          on_change=self._on_clean_change)
        self._clean_dropbox_cb = ft.Checkbox(label="清空 dropbox（破坏性）", value=self._clean_dropbox,
                                             on_change=self._on_clean_change)

        tips = ft.Text(
            "一键流：启动 → 每 60s 轮询 → 完成后自动拉取 + 分析（crash/ANR）；"
            "部署为首次使用一次（root/remount + push 三件套）",
            size=10, color=t.c("text_hint"))

        cfg_card = self._card("配置", ft.Column([
            ft.Row([self._pkg_field], spacing=8),
            ft.Row([self._count_field, self._throttle_field], spacing=8),
            ft.Row([self._clean_logs_cb, self._clean_dropbox_cb], spacing=12),
            tips,
        ], spacing=6))

        act_card = self._card("操作", ft.Row([
            ft.FilledButton("部署", icon=ft.Icons.UPLOAD, on_click=self._deploy),
            ft.FilledButton("启动（一键）", icon=ft.Icons.PLAY_ARROW, on_click=self._start_flow),
            ft.OutlinedButton("停止", icon=ft.Icons.STOP, on_click=self._stop),
            ft.OutlinedButton("拉取 + 分析", icon=ft.Icons.DOWNLOAD, on_click=self._pull_analyze),
            ft.OutlinedButton("刷新状态", icon=ft.Icons.REFRESH, on_click=self._refresh_status),
            ft.OutlinedButton("报告列表", icon=ft.Icons.DESCRIPTION,
                              on_click=self._log_reports),
        ], spacing=8, wrap=True))

        self._refresh_status()
        # 40 号：切走再切回且一键流仍在跑 → 回填登记（指示器/抽屉可见）
        if self._svc.running and getattr(self.app.state, "tasks", None) is not None \
                and self.app.state.tasks.get("monkey") is None:
            self._task_register("monkey", "Monkey 一键流",
                                kind=tasks_mod.KIND_DEVICE, stop_fn=self._svc.cancel)

        # 内容只占自身高度（不 expand）——底部事件窗口铺满剩余（2026-10-12）
        return ft.Column([cfg_card, act_card], spacing=8)

    def _card(self, title: str, content: ft.Control) -> ft.Container:
        t = self.t
        return ft.Container(
            content=ft.Column([
                ft.Text(title, size=12, weight=ft.FontWeight.BOLD, color=t.c("text_primary")),
                content,
            ], spacing=6),
            padding=ft.padding.Padding(left=14, top=10, right=14, bottom=10),
            bgcolor=t.c("bg_card"), border_radius=10,
            border=ft.border.Border.all(1, t.c("border")))

    # ── 动作 ──
    def _read_cfg(self) -> monkey_mod.MonkeyConfig:
        def _int(v: str, dflt: int) -> int:
            try:
                return int(str(v).strip() or dflt)
            except ValueError:
                return dflt
        cfg = monkey_mod.MonkeyConfig(
            pkg=(self._pkg_field.value or "").strip() if self._pkg_field else self._pkg,
            count=_int(self._count_field.value if self._count_field else self._count,
                       monkey_mod.DEFAULT_COUNT),
            throttle=_int(self._throttle_field.value if self._throttle_field else self._throttle,
                          monkey_mod.DEFAULT_THROTTLE),
            clean_logs=bool(self._clean_logs_cb.value) if self._clean_logs_cb else self._clean_logs,
            clean_dropbox=bool(self._clean_dropbox_cb.value) if self._clean_dropbox_cb else self._clean_dropbox)
        return cfg

    def _on_clean_change(self, e=None) -> None:
        """勾选即写配置（防「已武装」的清理开关跨会话隐式残留；默认不勾为安全基线）"""
        if self._clean_logs_cb:
            self._clean_logs = bool(self._clean_logs_cb.value)
        if self._clean_dropbox_cb:
            self._clean_dropbox = bool(self._clean_dropbox_cb.value)
        try:
            self.app.cfg.set("monkey_clean_logs", self._clean_logs)
            self.app.cfg.set("monkey_clean_dropbox", self._clean_dropbox)
        except Exception:  # noqa: BLE001
            pass

    def _persist(self, cfg: monkey_mod.MonkeyConfig) -> None:
        for k, v in (("monkey_pkg", cfg.pkg), ("monkey_count", cfg.count),
                     ("monkey_throttle", cfg.throttle),
                     ("monkey_clean_logs", cfg.clean_logs),
                     ("monkey_clean_dropbox", cfg.clean_dropbox)):
            try:
                self.app.cfg.set(k, v)
            except Exception:  # noqa: BLE001
                pass

    def _archive_dir(self) -> str:
        """归档目录（沿用工具目录惯例：dropbox_时间戳）"""
        return os.path.join(str(monkey_mod.tool_dir()),
                            time.strftime("dropbox_%Y%m%d_%H%M%S"))

    def _deploy(self, e=None) -> None:
        self.app.set_status("部署中（root/remount + push）…", "warning")

        def _work() -> None:
            op = self.app.begin_op("部署", tag="工具",
                                   param="root/remount + 三件套")
            try:
                r = self._ctl.deploy()
            except Exception:
                self.app.end_op(op, ok=False)
                raise
            self._log(f"[部署] {r.detail}")     # 结果归入事件块
            self.app.end_op(op, ok=r.ok)
            self.app.set_status(("✔ " if r.ok else "✘ ") + r.detail,
                                "success" if r.ok else "danger")
            self._done("部署完成" if r.ok else "部署失败", ok=r.ok)
        self._run_in_thread(_work)

    def _start_flow(self, e=None) -> None:
        cfg = self._read_cfg()
        if not cfg.pkg:
            self.app.set_status("包名不能为空", "danger")
            return
        self._persist(cfg)
        if cfg.clean_logs or cfg.clean_dropbox:
            which = "、".join(n for n, on in (("清理 monkey 日志", cfg.clean_logs),
                                              ("清空 dropbox", cfg.clean_dropbox)) if on)
            self._confirm("确认清理并启动",
                          f"启动前将执行：{which}（不可恢复）。继续？",
                          lambda: self._launch_flow(cfg))
        else:
            self._launch_flow(cfg)
        self.app.set_status("一键流已在后台启动", "success")

    def _launch_flow(self, cfg: monkey_mod.MonkeyConfig) -> None:
        """启动一键流 + 一级操作行（耗时在 ``_on_result`` 收口）"""
        param = f"包名={cfg.pkg} 次数={cfg.count} 间隔={cfg.throttle}ms"
        self._flow_op = self.app.begin_op("一键流", tag="工具", param=param)
        # 40 号：补设备占用 token（一键流期间禁止切设备）+ 登记全局任务
        self.app.state.device_acquire("monkey")
        self._task_register("monkey", "Monkey 一键流",
                            kind=tasks_mod.KIND_DEVICE, stop_fn=self._svc.cancel)
        self._task_update("monkey", detail=param)
        self._svc.run_auto(cfg, self._archive_dir())
        if not self._svc.running:   # 已有任务在执行（service 拒绝启动）
            self.app.end_op(self._flow_op, ok=False)
            self._flow_op = None
            self._task_unregister("monkey")
            self.app.state.device_release("monkey")

    def _stop(self, e=None) -> None:
        self._svc.cancel()

        def _work() -> None:
            with self.app.op("停止", tag="工具"):
                r = self._ctl.stop()
            self._log(f"[停止] {r.detail}")
            self.app.set_status(("✔ " if r.ok else "✘ ") + r.detail,
                                "success" if r.ok else "danger")
            self._done("已停止" if r.ok else "停止失败", ok=r.ok)
        self._run_in_thread(_work)

    def _pull_analyze(self, e=None) -> None:
        cfg = self._read_cfg()
        self._persist(cfg)
        dest = self._archive_dir()

        def _work() -> None:
            with self.app.op("拉取 + 分析", tag="工具",
                             param=os.path.basename(dest)) as o:
                self.app.set_status("拉取中…", "warning")
                r = self._ctl.pull(dest)
                self._log(f"[拉取] {r.detail}")
                if not r.ok:
                    o.fail()
                    self.app.set_status("✘ " + r.detail, "danger")
                    self._done("拉取失败", ok=False)
                    return
                self.app.set_status("分析中（crash/ANR）…", "warning")
                rep = self._ctl.analyze(dest, cfg.pkg,
                                        on_log=lambda m, t="info": self._log(m))
                if rep:
                    p = rep.path_html or rep.path_md
                    self._log(f"报告：{p}")            # 路径进事件块（2026-10-12 报告并入日志）
                    self.app.set_status(f"✔ 报告：{os.path.basename(p)}", "success")
                    self._done("分析完成")
                else:
                    self.app.set_status("已拉取（未产出报告，查看归档目录）", "warning")
                    self._done("拉取完成")
        self._run_in_thread(_work)

    @staticmethod
    def _fmt_status(st) -> str:
        """MonkeyStatus → 一行人类可读状态（替代原「状态」卡；2026-09-23）

        状态卡原样渲染 phase=/pid=/started=epoch 生涩字段，且**文本行数变化会
        推挤下方按钮**（活层下移、点击命中错位）——删卡后信息并入「刷新状态」
        事件块：点一下即得一行摘要。
        """
        phase_map = {"run": "运行中", "running": "运行中",
                     "done": "已完成", "idle": "空闲", "stopped": "已停止"}
        parts = [f"状态：{phase_map.get(str(st.phase), st.phase)}"]
        if st.pid:
            parts.append(f"pid={st.pid}")
        if st.count:
            parts.append(f"{st.count} 次")
        if st.throttle:
            parts.append(f"间隔 {st.throttle}ms")
        if st.started:
            try:
                parts.append("启动于 " + time.strftime(
                    "%m-%d %H:%M", time.localtime(int(st.started))))
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        if st.pkg:
            parts.append(str(st.pkg))
        return " · ".join(parts)

    def _refresh_status(self, e=None) -> None:
        def _work() -> None:
            with self.app.op("刷新状态", tag="工具"):
                st = self._ctl.status()
                self._log(self._fmt_status(st))     # 结果归入事件块
        self._run_in_thread(_work)

    def _log_reports(self, e=None) -> None:
        """把近 10 份报告路径**写进事件窗口**（2026-10-12：报告卡删除，并入日志）

        路径给全（可直接复制去打开）；无报告时给一行提示。线程安全由
        ``append_log_to_panel`` 内部的 ``on_main`` 调度保证。
        """
        def _work() -> None:
            with self.app.op("报告列表", tag="工具") as o:
                reports = self._ctl.list_reports()[:10]
                if not reports:
                    self._log("暂无报告（跑一次「拉取 + 分析」后在此列出）")
                    return
                for r in reports:
                    self._log(f"{os.path.basename(r.path_html or r.path_md)}"
                              f"  ←  {r.path_html or r.path_md}")
        self._run_in_thread(_work)

    # ── 辅助 ──
    def _log(self, msg: str) -> None:
        try:
            self.app.append_log_to_panel(msg)
        except Exception:  # noqa: BLE001
            pass

    def _on_result(self, ok: bool, detail: str) -> None:
        if self._flow_op is not None:
            self.app.end_op(self._flow_op, ok=ok)
            self._flow_op = None
        self._task_unregister("monkey")            # 40 号：一键流结束 → 出表（进历史）
        try:
            self.app.state.device_release("monkey")
        except Exception:  # noqa: BLE001
            pass
        self._log(("完成：" if ok else "未完成：") + detail)
        self.app.set_status(("✔ " if ok else "✘ ") + detail,
                            "success" if ok else "danger")
        self._done("压测完成" if ok else "压测失败", ok=ok)

    def _confirm(self, title: str, msg: str, on_ok) -> None:
        dlg = ft.AlertDialog(
            title=ft.Text(title, size=14),
            content=ft.Text(msg, size=12),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.app.page.pop_dialog()),
                ft.FilledButton("确认", on_click=lambda e: (self.app.page.pop_dialog(), on_ok())),
            ])
        try:
            self.app.page.show_dialog(dlg)
        except Exception:  # noqa: BLE001
            on_ok()
