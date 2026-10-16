# -*- coding: utf-8 -*-
"""单条灌入（快灌）— 一句话 → 灌入 → 结果卡片

来源：``EOV_Voice_Tool.py`` 的"单条快灌"日常用法（``30-功能分类与可配置分离.md``
§六 第 2 项）。内核复用 ``core.Injector``（见 ``services/single_inject_service.py``），
本页只做输入/结果/历史的界面。

界面：
  · 输入行：一句话输入框（**回车即灌**）+ 灌入 / 停止 + 超时 + 语义模式
  · **循环灌入（2026-10-15 用户诉求）**：次数（留空/0＝不限，用「停止」结束）+
    循环间隔（秒）——每轮独立灌入（独立日志窗口，轮间不串判），逐条落结果卡片
  · 结果卡片：`#n  query  ✓/✕  落域  ⏱ 端到端  reply / 失败原因`，点卡片复制该条
  · 历史：最近 50 条落 ``data/tools/single_inject_history.json``，下拉可回填重灌
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import flet as ft

from ...core import config
from ...core.injector import InjectState
from ...core.judger import default_timeout as judge_default_timeout
from .. import tasks as tasks_mod
from ..ui.components import sync_text_value
from . import constants
from .base_tab import BaseTab
from .services.single_inject_service import SingleInjectService

#: 历史文件（工具偏好目录）
_HISTORY_FILE = Path(constants.LOCAL_SAVE_ROOT) / "single_inject_history.json"
_HISTORY_MAX = 50


class SingleInjectTab(BaseTab):
    """单条灌入页"""

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self._svc = SingleInjectService()
        self._records: list[dict] = []
        self._history: list[str] = []
        self._load_history()

        self._field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_last", "")),
            hint_text="输入一句话，回车即灌入（如「导航去公司」）",
            dense=True, expand=True, text_size=13,
            on_change=sync_text_value,           # 发现 #14
            on_submit=lambda e: self._run())
        self._timeout_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_timeout",
                                       f"{judge_default_timeout():g}")),
            label="超时(秒)", dense=True, width=110, text_size=12,
            on_change=sync_text_value)
        self._mode_dd = ft.Dropdown(
            label="语义模式", dense=True, width=150, text_size=12, value="3",
            options=[ft.dropdown.Option("3", "3 语义直入"),
                     ft.dropdown.Option("1", "1"),
                     ft.dropdown.Option("2", "2")],
            tooltip="灌入模式（VR getSemantic 的 mode 参数，默认 3）")
        # 循环灌入（2026-10-15）：次数留空/0 ＝ 不限（用「停止」结束）
        self._repeat_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_repeat", "1")),
            label="次数", dense=True, width=96, text_size=12,
            hint_text="留空/0=不限",
            tooltip="循环灌入次数：1＝单发；留空或 0＝一直灌到点「停止」",
            on_change=sync_text_value)
        self._interval_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_interval", "2")),
            label="循环间隔(秒)", dense=True, width=140, text_size=12,
            tooltip="每轮之间的等待（给车机留出播报与落日志时间；0＝不等）",
            on_change=sync_text_value)
        self._history_dd = ft.Dropdown(
            label="历史", dense=True, width=260, text_size=12,
            options=[ft.dropdown.Option(h) for h in self._history],
            on_select=lambda e: self._pick_history())
        self._btn_run = ft.FilledButton("灌入", icon=ft.Icons.SEND,
                                        on_click=lambda e: self._run())
        self._btn_stop = ft.OutlinedButton("停止", icon=ft.Icons.STOP,
                                           disabled=True,
                                           on_click=lambda e: self._stop())
        self._result_list = ft.ListView(expand=True, spacing=6, padding=0)
        self._hint = ft.Text("", size=10, color=self.t.c("text_hint"))
        # 循环灌入运行态（供按钮/提示判定）：本次总轮次(0=不限) + 本批通过/失败计数
        self._loop_total = 1
        self._run_pass = 0
        self._run_fail = 0

    # ── 构建 ────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        # 40 号：跨页回填运行态——灌入进行中（切走再切回）按钮还原为 busy
        if self._svc.busy:
            self._btn_run.disabled = True
            self._btn_stop.disabled = False
        return ft.Column([
            ft.Row([self._field, self._btn_run, self._btn_stop], spacing=8),
            ft.Row([self._timeout_field, self._mode_dd, self._repeat_field,
                    self._interval_field, self._history_dd],
                   spacing=8, wrap=True),
            ft.Text("每灌一句都会记入下方事件窗口（含命令与回显）；"
                    "结果卡片可点击复制。次数 > 1 或留空＝循环灌入："
                    "每轮独立（独立日志窗口、轮间不串判），点「停止」结束。",
                    size=10, color=t.c("text_hint")),
            self._result_list,
        ], spacing=8, expand=True)

    # ── 动作 ────────────────────────────────
    def _run(self) -> None:
        q = (self._field.value or "").strip()
        if not q:
            self._status("请输入要灌入的内容", "danger")
            return
        if self._svc.busy:
            self._status("上一次灌入仍在进行", "warning")
            return
        try:
            timeout = float(self._timeout_field.value or f"{judge_default_timeout():g}")
        except ValueError:
            self._status("超时必须是数字", "danger")
            return
        repeat = self._parse_repeat()
        if repeat is None:
            return
        try:
            interval = float(self._interval_field.value or 0)
        except ValueError:
            self._status("循环间隔必须是数字（秒）", "danger")
            return
        if interval < 0:
            self._status("循环间隔不能为负", "danger")
            return
        self._save_prefs(q, timeout, self._repeat_field.value, interval)
        self._add_history(q)
        # 40 号 §4：设备忙（其他设备任务/流水线运行中）→ 入队，完成后自动执行；
        # "started" 时队列已执行 starter，勿再直跑（防双重执行）
        verdict = self._submit_device_task("inject", "语音灌入",
                                           lambda: self._begin_inject(
                                               q, timeout, repeat, interval))
        if verdict == "queued":
            self._queued_feedback("inject", "语音灌入")
            return
        if verdict == "bypass":
            self._begin_inject(q, timeout, repeat, interval)

    def _parse_repeat(self) -> int | None:
        """解析「次数」：留空 / 0 / 负数 → 0（不限，直到点停止）；非法 → None（已提示）"""
        raw = (self._repeat_field.value or "").strip()
        if not raw:
            return 0
        try:
            n = int(raw)
        except ValueError:
            self._status("次数必须是整数（留空或 0＝不限）", "danger")
            return None
        return max(0, n)

    def _begin_inject(self, q: str, timeout: float, repeat: int = 1,
                      interval: float = 0.0) -> None:
        """真正发起灌入（可由队列出队触发——工作线程调用；UI 走 on_main）"""
        self._set_busy(True)
        self._loop_total = repeat
        self._run_pass = 0
        self._run_fail = 0
        rounds_txt = f" × {repeat}" if repeat else " × 不限"
        self._status(f"已发送：{q}{rounds_txt if repeat != 1 else ''}", "warning")
        state = self.app.state
        # 设备占用：灌入期间禁止顶栏切换设备（先占后启——IDLE 回调必然在 acquire 之后）
        state.device_acquire("inject")
        if getattr(state, "tasks", None) is not None \
                and state.tasks.get("inject") is None:
            # 直跑路径（无队列）→ 此处登记；队列路径已在提交时登记
            self._task_register("inject", "语音灌入",
                                kind=tasks_mod.KIND_DEVICE, stop_fn=self._svc.stop)
        self._task_update("inject", detail=q)
        started_ok = self._svc.run(
            q, device=str(getattr(state, "device", "") or ""),
            timeout=timeout, repeat=repeat, interval=interval,
            on_log=lambda m, tag="info": self.app.append_log_to_panel(m),
            on_done=lambda case, judgment: self._on_done(case, judgment, time.time()),
            on_state=self._on_state,
            on_round=lambda idx, total: self._on_round(q, idx, total))
        if not started_ok:
            state.device_release("inject")
            self._task_unregister("inject")
            self._set_busy(False)
            self._status("启动失败（上一次灌入仍在进行）", "warning")

    def _on_round(self, q: str, idx: int, total: int) -> None:
        """轮次回调（服务线程）→ 顶栏任务详情 + 状态行显示「第 n/N 次」"""
        lbl = f"第 {idx}/{total} 次" if total else f"第 {idx} 次（不限）"
        self._task_update("inject", detail=f"{q}（{lbl}）")
        self._status(f"循环灌入 {lbl}：{q}", "warning")

    def _on_state(self, st) -> None:  # noqa: ANN001 — 服务线程回调
        """服务状态回调：IDLE 一定到达（含停止/异常路径）→ 兜底复位按钮

        2026-09-24 全按钮点验修复：此前只依赖 ``on_done``——「停止」中止的
        灌入不会触发 on_case_done → 「灌入」按钮永久禁用（真机实测 3 分钟+
        不恢复，需切页才复位）。
        """
        if st == InjectState.IDLE:
            self._task_unregister("inject")     # 40 号：任务结束 → 移出活动表、进历史
            self._set_busy(False)
            self.app.state.device_release("inject")
            # 循环灌入收尾汇总（单发已逐次提示，此处跳过）
            n = self._run_pass + self._run_fail
            if self._loop_total != 1 and n:
                self._done(f"循环灌入完成 {n} 次（通过 {self._run_pass}）",
                           ok=self._run_fail == 0)

    def _stop(self) -> None:
        self._svc.stop()
        queue = getattr(self.app.state, "device_queue", None)
        if queue is not None:
            queue.cancel("inject")                 # 排队中的灌入一并取消（40 号 §4.3）
        self._status("已请求停止", "warning")

    def _on_done(self, case, judgment, started: float) -> None:  # noqa: ANN001
        """结果回调 → 结果卡片 + 状态行（后台线程；控件更新走 _safe_update）"""
        rec = {
            "query": case.query,
            "passed": bool(getattr(judgment, "passed", False)),
            "domain": getattr(judgment, "intent_type", "") or "",
            "reply": getattr(judgment, "reply_text", "") or "",
            "reason": getattr(judgment, "reason", "") or "",
            "status": case.status,
            "cost": round(getattr(judgment, "e2e_response_time", 0.0) or
                          (time.time() - started), 2),
            "at": time.strftime("%H:%M:%S"),
        }
        self._records.insert(0, rec)
        self._result_list.controls.insert(0, self._record_card(len(self._records), rec))
        self._safe_update(self._result_list)
        # 40 号：结果即时落 tool_state——离页重建后 restore_state 不丢在途结果
        try:
            self.app.state.tool_state["inject"] = self.save_state()
        except Exception:  # noqa: BLE001
            pass
        ok = rec["passed"]
        if ok:
            self._run_pass += 1
        else:
            self._run_fail += 1
        self._status(("✔ " if ok else "✕ ") + (rec["reply"] or rec["reason"])[:60],
                     "success" if ok else "danger")
        # 单发逐次提示；循环灌入改为收尾统一提示（见 _on_state，避免不限循环刷屏）
        if self._loop_total == 1:
            self._done("灌入完成" if ok else "灌入失败", ok=ok)

    def _record_card(self, idx: int, rec: dict) -> ft.Control:
        t = self.t
        ok = rec["passed"]
        color = t.c("success" if ok else "danger")
        reply = rec["reply"] or rec["reason"] or "（无回复）"
        card = ft.Container(
            content=ft.Row([
                ft.Text(f"#{idx}", size=10, width=30, color=t.c("text_hint")),
                ft.Text(("✓" if ok else "✕"), size=11, width=16, color=color),
                ft.Text(rec["query"], size=11, width=200, no_wrap=True,
                        color=t.c("text_primary")),
                ft.Text(rec["domain"] or "—", size=10, width=76, no_wrap=True,
                        color=t.c("text_secondary")),
                ft.Text(f"⏱ {rec['cost']}s", size=10, width=64,
                        color=t.c("text_secondary")),
                ft.Text(reply, size=10, expand=True, max_lines=2,
                        color=t.c("text_primary" if rec["reply"] else "text_secondary")),
                ft.Text(rec["at"], size=9, width=56, color=t.c("text_hint")),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(left=10, right=10, top=6, bottom=6),
            bgcolor=t.c("bg_card"), border_radius=8,
            border=ft.border.Border.all(1, color if not ok else t.c("border")),
            on_click=lambda e, r=rec: self._copy(r),
            tooltip="点击复制该条结果")
        return card

    def _copy(self, rec: dict) -> None:
        text = (f"{rec['query']}\n结果: {'通过' if rec['passed'] else '失败'}\n"
                f"落域: {rec['domain']}\n耗时: {rec['cost']}s\n"
                f"回复: {rec['reply']}\n原因: {rec['reason']}")
        try:
            self.app.page.set_clipboard(text)
            self._status("已复制该条结果")
        except Exception as ex:  # noqa: BLE001
            self._status(f"复制失败：{ex}", "danger")

    # ── 辅助 ────────────────────────────────
    def _set_busy(self, busy: bool) -> None:
        """切换灌入/停止按钮态（可能由队列出队线程触发 → 统一经 on_main）"""
        def _do() -> None:
            self._btn_run.disabled = busy
            self._btn_stop.disabled = not busy
            # 显式刷新：仅设 ``disabled`` 不会自动推给前端——此前按钮要等
            # 第一轮 ``_on_done`` 的 ``_safe_update`` 才变灰（实测 ~2s 无反馈，
            # 循环灌入下尤其明显）。对齐 log_tab 的按钮翻转写法
            self._safe_update(self._btn_run)
            self._safe_update(self._btn_stop)
        try:
            self.app.on_main(_do)
        except Exception:  # noqa: BLE001 — 测试替身无 on_main 时直接执行
            _do()

    def _status(self, text: str, color_key: str = "text_hint") -> None:
        try:
            self.app.set_status(text, color_key)
        except Exception:  # noqa: BLE001
            self._hint.value = text
            self._safe_update(self._hint)

    def _save_prefs(self, query: str, timeout: float, repeat: str = "1",
                    interval: float = 0.0) -> None:
        try:
            self.app.cfg.set("single_inject_last", query)
            self.app.cfg.set("single_inject_timeout", str(timeout))
            self.app.cfg.set("single_inject_repeat", str(repeat))
            self.app.cfg.set("single_inject_interval", f"{interval:g}")
        except Exception:  # noqa: BLE001
            pass

    def _pick_history(self) -> None:
        if self._history_dd.value:
            self._field.value = self._history_dd.value
            self._safe_update(self._field)

    def _load_history(self) -> None:
        try:
            data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._history = [str(x) for x in data][:_HISTORY_MAX]
        except Exception:  # noqa: BLE001
            self._history = []

    def _add_history(self, query: str) -> None:
        self._history = [query] + [h for h in self._history if h != query]
        self._history = self._history[:_HISTORY_MAX]
        self._history_dd.options = [ft.dropdown.Option(h) for h in self._history]
        self._safe_update(self._history_dd)
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(json.dumps(self._history, ensure_ascii=False,
                                                indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ── 生命周期 ────────────────────────────
    def save_state(self) -> dict:
        return {"records": self._records[:20]}

    def restore_state(self, state: dict) -> None:
        recs = (state or {}).get("records") or []
        if recs and not self._records:
            self._records = list(recs)
            for i, r in enumerate(reversed(self._records)):
                self._result_list.controls.append(self._record_card(i + 1, r))

    def on_detach(self) -> None:
        """离页**不停灌入**（40 号新语义）：任务归 ``state.tasks`` 持有

        结果回调照常落卡（控件已卸载时 ``_safe_update`` 静默）并即时持久化
        （见 ``_on_done``）；切回页面由 ``build()`` 回填按钮 busy 态。
        停止途径：本页「停止」按钮 / 顶栏任务抽屉 / 切设备「停止并切换」。
        """
        super().on_detach()
