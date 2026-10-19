# -*- coding: utf-8 -*-
"""单条灌入（快灌）— 一句话 → 灌入 → 结果卡片

来源：``EOV_Voice_Tool.py`` 的"单条快灌"日常用法（``30-功能分类与可配置分离.md``
§六 第 2 项）。内核复用 ``core.Injector``（见 ``services/single_inject_service.py``），
本页只做输入/结果/历史的界面。

界面：
  · 输入行：一句话输入框（**回车即灌**）+ 灌入 / 停止 + 超时
  · **循环灌入（2026-10-15 用户诉求）**：次数（留空/0＝不限，用「停止」结束）+
    间隔（秒）——每轮独立灌入（独立日志窗口，轮间不串判），逐条落结果卡片
  · **失败即停（2026-10-19 用户诉求）**：默认行为、界面无开关——任一轮失败
    （含发送失败/异常）即结束循环，收尾提示区分「失败即停」与「手动停止」
  · **用例加载 + 顺序执行（2026-10-19 用户诉求）**：点「浏览…」选中即自动加载
    （xlsx / csv / txt / json，复用 ``core.source.DataSource``；Web 模式手输路径回车）
    → 列出用例（点击某条＝填入输入框，指定用例执行）→「顺序执行」按列表逐条灌入，
    与循环灌入共用「间隔」「失败即停」「停止」通路；非用例文件（不存在/文件夹/
    后缀不符/解析失败）清空列表并提示原因
  · 结果卡片：`#n  query  ✓/✕  落域  ⏱ 端到端  reply / 失败原因`，点卡片复制该条
  · 历史：最近 50 条落 ``data/tools/single_inject_history.json``，下拉可回填重灌、旁可一键清空
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import flet as ft

from ...core import config
from ...core.injector import InjectState
from ...core.judger import default_timeout as judge_default_timeout
from ...core.source import DataSource, TestCase
from .. import tasks as tasks_mod
from ..ui import components
from ..ui.components import sync_text_value
from . import constants
from .base_tab import BaseTab
from .services.single_inject_service import SingleInjectService

#: 历史文件（工具偏好目录）
_HISTORY_FILE = Path(constants.LOCAL_SAVE_ROOT) / "single_inject_history.json"
_HISTORY_MAX = 50

#: 可接受的用例文件后缀（与 ``core.source.DataSource.load_file`` 支持面一致）
_CASE_EXTS = {".xlsx", ".xls", ".csv", ".txt", ".text", ".json"}


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
        # 循环灌入（2026-10-15）：次数留空/0 ＝ 不限（用「停止」结束）
        self._repeat_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_repeat", "1")),
            label="次数", dense=True, width=96, text_size=12,
            hint_text="留空/0=不限",
            tooltip="循环灌入次数：1＝单发；留空或 0＝一直灌到点「停止」",
            on_change=sync_text_value)
        self._interval_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_interval", "2")),
            label="间隔(秒)", dense=True, width=120, text_size=12,
            tooltip="每轮/每条之间的等待（给车机留出播报与落日志时间；0＝不等）",
            on_change=sync_text_value)
        # 失败即停（2026-10-19 用户裁决：循环/顺序执行**默认行为**，不做开关）
        # —— 任一轮/任一用例失败（含发送失败/异常）即结束执行，见服务层 fail_fast
        # ── 用例导入 + 顺序执行（2026-10-19）──
        self._cases: list = []                    # list[TestCase]（DataSource 加载）
        self._case_path_field = ft.TextField(
            value=str(self.app.cfg.get("single_inject_case_file", "")),
            hint_text="用例文件路径（xlsx / csv / txt / json）",
            dense=True, expand=True, text_size=12,
            on_change=sync_text_value,
            on_submit=lambda e: self._import_cases())
        self._btn_browse = ft.OutlinedButton(
            "浏览…", icon=ft.Icons.FOLDER_OPEN,
            tooltip="选择用例文件（选中即自动加载；Web 模式粘贴路径后回车）",
            on_click=self._browse)
        self._btn_clear_cases = ft.TextButton(
            "清空", tooltip="清空已加载的用例列表（同时忘掉历史路径）",
            on_click=lambda e: self._clear_cases())
        self._btn_seq = ft.FilledButton(
            "顺序执行", icon=ft.Icons.PLAYLIST_PLAY,
            disabled=True,
            tooltip="按用例列表逐条灌入（间隔/失败即停与循环灌入共用）",
            on_click=lambda e: self._run_sequential())
        self._case_list = ft.ListView(spacing=2, padding=0, expand=True)
        self._case_empty = ft.Text(
            "尚未加载用例（支持 xlsx / csv / txt / json；"
            "点「浏览…」选中即自动加载，Web 模式粘贴路径后回车）",
            size=10, color=self.t.c("text_hint"))
        self._case_holder = ft.Container(
            expand=True, border_radius=8, padding=6,
            border=ft.border.Border.all(1, self.t.c("border")),
            content=self._case_empty)
        # 用例 / 结果 两视图（小 tab 切换；常驻槽只切 visible —— 对齐合并页做法，
        # 避免动态替换可点控件留下的失效命中区）
        self._view = "cases"                      # cases | results
        self._chips_row = ft.Row(spacing=6)
        self._cases_slot = ft.Container(expand=True, content=self._case_holder)
        self._results_slot = ft.Container(expand=True, visible=False)
        self._loop_kind = "loop"                  # loop | sequential（收尾提示用）
        self._history_dd = ft.Dropdown(
            label="历史", dense=True, width=260, text_size=12,
            options=[ft.dropdown.Option(h) for h in self._history],
            on_select=lambda e: self._pick_history())
        # 清空历史（内存 + 落盘；无历史时置灰）
        self._btn_clear_history = ft.IconButton(
            icon=ft.Icons.DELETE_SWEEP, icon_size=16,
            disabled=not self._history,
            tooltip="清空历史记录（连同本地文件）",
            on_click=lambda e: self._clear_history())
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
        # 上次用例文件回填（无内存用例时自动加载——路径失效则静默，不刷红）
        if not self._cases and (self._case_path_field.value or "").strip():
            self._import_cases(quiet=True)
        self._rebuild_case_list()
        # 执行中/有结果时默认看结果，否则看用例
        if self._svc.busy or self._records:
            self._view = "results"
        self._results_slot.content = self._result_list
        self._cases_slot.visible = (self._view == "cases")
        self._results_slot.visible = (self._view == "results")
        self._rebuild_chips()
        return ft.Column([
            ft.Row([self._field, self._btn_run, self._btn_stop], spacing=8),
            ft.Row([self._timeout_field, self._repeat_field,
                    self._interval_field, self._history_dd,
                    self._btn_clear_history],
                   spacing=8, wrap=True),
            ft.Row([self._btn_browse, self._case_path_field,
                    self._btn_clear_cases, self._btn_seq], spacing=8),
            ft.Text("每灌一句都会记入下方事件窗口（含命令与回显）；"
                    "结果卡片可点击复制。次数 > 1 或留空＝循环灌入："
                    "每轮独立（独立日志窗口、轮间不串判），点「停止」结束。"
                    "循环/顺序执行任一次失败即自动结束；"
                    "加载用例后可点某条指定执行，或「顺序执行」逐条灌入。",
                    size=10, color=t.c("text_hint")),
            self._chips_row,
            self._cases_slot,
            self._results_slot,
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
            self._status("间隔必须是数字（秒）", "danger")
            return
        if interval < 0:
            self._status("间隔不能为负", "danger")
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

    # ── 用例 / 结果 视图切换（2026-10-19 用户诉求：两个小 tab）──
    def _rebuild_chips(self) -> None:
        self._chips_row.controls = [
            components.chip(self.t, f"用例 {len(self._cases)}",
                            on_click=lambda e: self._switch_view("cases"),
                            active=self._view == "cases"),
            components.chip(self.t, f"结果 {len(self._records)}",
                            on_click=lambda e: self._switch_view("results"),
                            active=self._view == "results"),
        ]
        self._safe_update(self._chips_row)

    def _switch_view(self, view: str) -> None:
        if view not in ("cases", "results"):
            return
        self._view = view
        self._cases_slot.visible = (view == "cases")
        self._results_slot.visible = (view == "results")
        self._rebuild_chips()
        self._safe_update(self._cases_slot)
        self._safe_update(self._results_slot)

    # ── 用例加载 + 顺序执行（2026-10-19）────────
    def _run_sequential(self) -> None:
        """顺序执行：按已加载的用例列表逐条灌入（间隔/失败即停/停止与循环共用）"""
        if not self._cases:
            self._status("请先加载用例文件（点「浏览…」选择）", "warning")
            return
        if self._svc.busy:
            self._status("上一次灌入仍在进行", "warning")
            return
        try:
            timeout = float(self._timeout_field.value or f"{judge_default_timeout():g}")
            interval = float(self._interval_field.value or 0)
        except ValueError:
            self._status("超时/间隔必须是数字", "danger")
            return
        if interval < 0:
            self._status("间隔不能为负", "danger")
            return
        queries = [c.query for c in self._cases]
        self._save_prefs("", timeout, self._repeat_field.value, interval)
        verdict = self._submit_device_task(
            "inject", "顺序灌入",
            lambda: self._begin_inject("", timeout, repeat=len(queries),
                                       interval=interval, queries=queries))
        if verdict == "queued":
            self._queued_feedback("inject", "顺序灌入")
            return
        if verdict == "bypass":
            self._begin_inject("", timeout, repeat=len(queries),
                               interval=interval, queries=queries)

    async def _browse(self, e=None) -> None:  # noqa: ANN001
        """浏览用例文件（0.85：FilePicker 是 Service，构造即注册、**勿挂 overlay**）"""
        if getattr(self.app.page, "web", False):
            self._status("文件选择仅支持桌面模式（Web 模式请直接粘贴路径）", "warning")
            return
        try:
            picker = ft.FilePicker()
            files = await picker.pick_files(
                dialog_title="选择用例文件（xlsx / csv / txt / json）",
                allowed_extensions=["xlsx", "xls", "csv", "txt", "text", "json"])
            if files:
                self._case_path_field.value = files[0].path
                self._safe_update(self._case_path_field)
                self._import_cases()
        except Exception as ex:  # noqa: BLE001
            self._status(f"选择文件失败：{ex}", "danger")

    def _reject_cases(self, msg: str, *, quiet: bool = False) -> None:
        """用例文件不可用 → 清空列表并报错（不留半成品，不落盘）

        ``quiet=True``（启动时自动回填）只清空不刷红，避免历史路径失效每次报错。
        """
        self._cases = []
        self._rebuild_case_list()
        self._rebuild_chips()
        if not quiet:
            self._status(msg, "danger")

    def _import_cases(self, *, quiet: bool = False) -> None:
        """加载用例文件（浏览选中即自动调用；Web 模式手输路径回车）

        复用 ``core.source.DataSource``（xlsx/xls/csv/txt/json）。非用例文件
        （不存在 / 是目录 / 后缀不符 / 解析失败）统一走 :meth:`_reject_cases`：
        清空旧用例并给出明确原因，绝不让上一次的列表冒充这一次的结果。
        """
        path = (self._case_path_field.value or "").strip()
        if not path:
            if not quiet:
                self._status("请先选择或输入用例文件路径", "warning")
            return
        p = Path(path)
        if not p.exists():
            self._reject_cases(f"不是有效用例文件：路径不存在（{p}）", quiet=quiet)
            return
        if p.is_dir():
            self._reject_cases(f"不是用例文件：这是文件夹（{p.name}）", quiet=quiet)
            return
        if p.suffix.lower() not in _CASE_EXTS:
            self._reject_cases(
                f"不是用例文件：{p.name}（仅支持 xlsx / csv / txt / json）",
                quiet=quiet)
            return
        try:
            ds = DataSource()
            ok, msg, _count = ds.load_file(p)
        except Exception as ex:  # noqa: BLE001 —— 坏文件/损坏压缩包等
            self._reject_cases(f"用例文件读取失败：{ex}", quiet=quiet)
            return
        if not ok or not ds.cases:
            self._reject_cases(f"用例文件解析失败：{msg or '未读到任何用例'}",
                               quiet=quiet)
            return
        self._cases = list(ds.cases)
        self._rebuild_case_list()
        self._switch_view("cases")          # 加载即看用例列表
        self._status(f"已加载 {len(self._cases)} 条用例（{p.name}）", "success")
        try:
            self.app.cfg.set("single_inject_case_file", str(p))
        except Exception:  # noqa: BLE001
            pass

    def _clear_cases(self) -> None:
        self._cases = []
        self._case_path_field.value = ""
        self._rebuild_case_list()
        self._rebuild_chips()
        self._safe_update(self._case_path_field)
        try:                                 # 清空即忘掉历史路径，避免重启又自动加载
            self.app.cfg.set("single_inject_case_file", "")
        except Exception:  # noqa: BLE001
            pass
        self._status("已清空用例列表")

    def _pick_case(self, idx: int) -> None:
        """点击用例 → 指定用例执行（填入输入框，点「灌入」即跑该条）"""
        if not (0 <= idx < len(self._cases)):
            return
        q = self._cases[idx].query
        self._field.value = q
        self._safe_update(self._field)
        self._status(f"已选第 {idx + 1} 条：{q}（点「灌入」执行该条）")

    def _rebuild_case_list(self) -> None:
        """按 ``self._cases`` 重建用例列表；无用例显示空态提示"""
        self._case_list.controls = [self._case_row(i, c)
                                    for i, c in enumerate(self._cases, 1)]
        self._btn_seq.disabled = not self._cases
        self._case_holder.content = (self._case_list if self._cases
                                     else self._case_empty)
        self._safe_update(self._case_list)
        self._safe_update(self._btn_seq)
        self._safe_update(self._case_holder)

    def _case_row(self, idx: int, case) -> ft.Control:  # noqa: ANN001
        """用例行：``#n  query  分类``——点击＝指定该条执行"""
        t = self.t
        cat = getattr(case, "category", "") or ""
        return ft.Container(
            content=ft.Row([
                ft.Text(f"#{idx}", size=10, width=34, color=t.c("text_hint")),
                ft.Text(case.query, size=11, expand=True, no_wrap=True,
                        color=t.c("text_primary")),
                ft.Text(cat or "—", size=9, width=90, no_wrap=True,
                        color=t.c("text_secondary")),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(left=8, right=8, top=3, bottom=3),
            border_radius=6,
            on_click=lambda e, i=idx - 1: self._pick_case(i),
            tooltip="点击＝指定该条执行（填入输入框）")

    def _begin_inject(self, q: str, timeout: float, repeat: int = 1,
                      interval: float = 0.0,
                      queries: list[str] | None = None) -> None:
        """真正发起灌入（可由队列出队触发——工作线程调用；UI 走 on_main）

        ``queries`` 非空＝顺序执行：按列表逐条灌入（轮数＝用例数，忽略 ``repeat``）。
        **失败即停为默认行为**（2026-10-19 用户裁决）——不再由界面开关控制。
        """
        self._set_busy(True)
        self._switch_view("results")        # 开始执行 → 自动切到结果 tab
        self._loop_kind = "sequential" if queries else "loop"
        self._loop_total = len(queries) if queries else repeat
        self._run_pass = 0
        self._run_fail = 0
        if queries:
            self._status(f"顺序执行：共 {len(queries)} 条用例", "warning")
        else:
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
        self._task_update("inject", detail=q or f"{len(queries or [])} 条用例")
        started_ok = self._svc.run(
            q, device=str(getattr(state, "device", "") or ""),
            timeout=timeout, repeat=repeat, interval=interval,
            fail_fast=True, queries=queries,
            on_log=lambda m, tag="info": self.app.append_log_to_panel(m),
            on_done=lambda case, judgment: self._on_done(case, judgment, time.time()),
            on_state=self._on_state,
            on_round=lambda idx, total: self._on_round(q, idx, total, queries))
        if not started_ok:
            state.device_release("inject")
            self._task_unregister("inject")
            self._set_busy(False)
            self._status("启动失败（上一次灌入仍在进行）", "warning")

    def _on_round(self, q: str, idx: int, total: int,
                  queries: list[str] | None = None) -> None:
        """轮次回调（服务线程）→ 顶栏任务详情 + 状态行显示「第 n/N 次/条」"""
        cur = queries[idx - 1] if (queries and 0 < idx <= len(queries)) else q
        lbl = f"第 {idx}/{total} 次" if total else f"第 {idx} 次（不限）"
        detail = f"{cur}（{lbl}）" if cur else lbl
        self._task_update("inject", detail=detail)
        kind = "顺序执行" if queries else "循环灌入"
        self._status(f"{kind} {lbl}：{cur}" if cur else f"{kind} {lbl}", "warning")

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
            # 循环/顺序执行收尾汇总（单发已逐次提示，此处跳过）
            n = self._run_pass + self._run_fail
            if self._loop_total != 1 and n:
                if self._svc.stop_reason == "fail_fast":
                    # 失败即停：失败轮 = 通过的下一轮（遇首败即停）
                    unit = "条" if self._loop_kind == "sequential" else "次"
                    self._done(f"第 {self._run_pass + 1} {unit}灌入失败，"
                               f"「失败即停」已提前结束", ok=False)
                elif self._loop_kind == "sequential":
                    self._done(f"顺序执行完成 {n} 条（通过 {self._run_pass}）",
                               ok=self._run_fail == 0)
                else:
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
        self._rebuild_chips()               # 「结果 N」计数即时更新
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
            if (query or "").strip():           # 顺序执行不覆盖上次手输内容
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

    def _sync_history_btn(self) -> None:
        """历史清空按钮随列表状态启停（空列表点它没有意义）"""
        self._btn_clear_history.disabled = not self._history
        self._safe_update(self._btn_clear_history)

    def _clear_history(self) -> None:
        """清空历史：内存 + 下拉 + 本地文件（一次性，不做二次确认）"""
        self._history = []
        self._history_dd.options = []
        self._history_dd.value = None
        self._safe_update(self._history_dd)
        self._sync_history_btn()
        try:
            _HISTORY_FILE.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 —— 文件被占用/权限不足不阻断清空
            pass
        self._status("已清空历史记录")

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
        self._sync_history_btn()
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(json.dumps(self._history, ensure_ascii=False,
                                                indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ── 生命周期 ────────────────────────────
    def save_state(self) -> dict:
        return {
            "records": self._records[:20],
            # 用例列表随页存（切页/切主题重建后不丢，免重新导入）
            "cases": [{"id": c.id, "query": c.query,
                       "category": getattr(c, "category", "")}
                      for c in self._cases],
            "case_path": (self._case_path_field.value or ""),
        }

    def restore_state(self, state: dict) -> None:
        recs = (state or {}).get("records") or []
        if recs and not self._records:
            self._records = list(recs)
            for i, r in enumerate(reversed(self._records)):
                self._result_list.controls.append(self._record_card(i + 1, r))
        cases = (state or {}).get("cases") or []
        if cases and not self._cases:
            self._cases = [
                TestCase(id=str(c.get("id") or f"TC{i:04d}"),
                         query=str(c.get("query") or ""),
                         category=str(c.get("category") or ""))
                for i, c in enumerate(cases, 1) if c.get("query")]
            self._rebuild_case_list()
        path = str((state or {}).get("case_path") or "")
        if path and not (self._case_path_field.value or "").strip():
            self._case_path_field.value = path

    def on_detach(self) -> None:
        """离页**不停灌入**（40 号新语义）：任务归 ``state.tasks`` 持有

        结果回调照常落卡（控件已卸载时 ``_safe_update`` 静默）并即时持久化
        （见 ``_on_done``）；切回页面由 ``build()`` 回填按钮 busy 态。
        停止途径：本页「停止」按钮 / 顶栏任务抽屉 / 切设备「停止并切换」。
        """
        super().on_detach()
