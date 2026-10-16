"""CarTest app — 流水线页（P1 底栏 Dock）

布局（14-UI重构设计.md §6.1）：
    工具条（用例/设备/参数/启停）→ 阶段条（耗时 + 运行呼吸）
    → 概览（进度/计数/剩余）→ 控制台 Dock（控制台 / 阶段详情，可折叠）

数据来源：
    阶段状态/耗时 —— AppState.stage_status + run.json 时间戳；
    实时进度     —— 本轮运行 progress.jsonl 的行数（Injector 无 n/m 日志行）。

**线程规则**：流水线跑在后台线程，``on_stage`` 回调**只写 AppState**，
不碰 UI；UI 由 main.py 的 tick 循环（每 250ms）统一刷新。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import flet as ft

from ...core import config
from ...core.judger import default_timeout as judge_default_timeout
from ...pipeline.context import STAGES, stage_label
from ...pipeline.stages import PipelineParams
from ..state import AppState
from ..ui import toast as toast_ui
from ..ui.components import sync_text_value

RESUME_MODES = {
    "跳过已执行": "skip_executed",
    "仅重灌失败与无回复": "retry_failed",
    "全部重灌": "redo_all",
    #: 2026-10-15 用户裁决：能"从指定序号用例开始"——前段不灌，第 N 条起**重灌**
    #: （实现＝redo_all + start_index=N-1；见 _start 里的解析）
    "从指定序号起重灌": "from_index",
}
#: 需配合「起始序号」输入框的档位
_FROM_INDEX_LABEL = "从指定序号起重灌"

MAX_LOG_LINES = 600
PULSE_TICKS = 5            # 呼吸半周期（tick≈250ms → 约 1.25s 一轮）
LIVE_REFRESH_SECS = 2.0    # 进度/详情节流


# ── 纯函数助手（无 UI，可单测）────────────────────────
def count_tags(lines: list[tuple[str, str]]) -> dict[str, int]:
    """统计日志批次中的通过/失败（按 tag；error 不并入 fail）"""
    out = {"pass": 0, "fail": 0, "skip": 0}
    for tag, _text in lines:
        if tag in out:
            out[tag] += 1
    return out


def count_progress_lines(path: Path) -> int:
    """progress.jsonl 非空行数（= 已执行用例数；写入中途的部分行不计）"""
    try:
        n = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n
    except OSError:
        return 0


def read_case_total(case_file: str | Path) -> int:
    """用例总数（仅 JSON 可静态读取；其它格式 → 0 表示未知）"""
    try:
        if Path(case_file).suffix.lower() == ".json":
            data = json.loads(Path(case_file).read_text(encoding="utf-8"))
            return len(data) if isinstance(data, list) else 0
    except (OSError, ValueError):
        pass
    return 0


def eta_text(done: int, total: int, interval: float) -> str:
    """预计剩余（按灌入间隔粗估）；未知/已完成 → —"""
    if done <= 0 or total <= 0 or interval <= 0 or done >= total:
        return "—"
    secs = (total - done) * interval
    if secs >= 60:
        return f"剩余 ~{int(round(secs / 60))} 分钟"
    return f"剩余 ~{int(secs)} 秒"


def stage_durations(ctx) -> dict[str, str]:
    """各阶段耗时（run.json 时间戳；仅 done/failed 有值）"""
    out: dict[str, str] = {}
    if ctx is None:
        return out
    for s in STAGES:
        st = ctx.stage(s)
        if st.started and st.finished:
            try:
                t0 = time.mktime(time.strptime(st.started, "%Y-%m-%d %H:%M:%S"))
                t1 = time.mktime(time.strptime(st.finished, "%Y-%m-%d %H:%M:%S"))
                out[s] = f"{max(0, int(t1 - t0))}s"
            except ValueError:
                pass
    return out


# ── 页面 ──────────────────────────────────────────────
def build(page: ft.Page, state: AppState) -> ft.Control:
    t = state.theme
    lp = state.last_params

    # 运行态缓存（本页生命周期内）
    stats = {"pass": 0, "fail": 0, "done": 0, "total": 0, "conflicts": "—"}
    dock = {"collapsed": False}
    live = {"ts": 0.0}
    run_cfg = {"interval": float(lp.get("interval", config.DEFAULT_INTERVAL) or 3.0)}

    # ── 工具条 ──
    cases_field = ft.TextField(
        on_change=sync_text_value,
        label="用例文件", value=str(lp.get("case_file", "")), dense=True, expand=True,
        hint_text=r"例如 D:\...\用例.json（xlsx/csv/txt/json）")
    device_text = ft.Text("", size=11, color=t.c("text_secondary"))
    device_chip = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.PHONE_ANDROID, size=14, color=t.c("text_hint")),
            device_text,
        ], spacing=4),
        padding=ft.padding.Padding(left=8, right=8, top=7, bottom=7),
        border_radius=6,
        border=ft.border.Border.all(1, t.c("border")),
        tooltip="设备由顶栏全局选择（点击顶栏设备胶囊切换）",
    )

    def _sync_device_chip() -> None:
        """只读设备提示（实际设备 = 全局所选，启动时读取）"""
        device_text.value = f"设备: {state.device or '(未选择)'}（顶栏切换）"
        _safe_update(device_text)

    _sync_device_chip()
    dev_seen = {"v": state.device}

    def _device_tick(tick: int) -> None:
        if state.device != dev_seen["v"]:
            dev_seen["v"] = state.device
            _sync_device_chip()

    interval_field = ft.TextField(
        on_change=sync_text_value,
        label="间隔(秒)", value=str(lp.get("interval", config.DEFAULT_INTERVAL)),
        dense=True, width=96)
    timeout_field = ft.TextField(
        on_change=sync_text_value,
        label="超时(秒)", value=str(lp.get("timeout", judge_default_timeout())),
        dense=True, width=96)
    resume_dd = ft.Dropdown(
        label="灌入范围", dense=True, width=190,
        options=[ft.dropdown.Option(k) for k in RESUME_MODES],
        value=str(lp.get("resume_label", "跳过已执行")),
        tooltip="哪些用例需要灌：续跑跳过 / 只补失败 / 全重灌 / 从指定序号起重灌")
    # 起始序号（1-based，仅「从指定序号起重灌」档可用）——常显置灰，避免布局跳动
    start_idx_field = ft.TextField(
        on_change=sync_text_value, label="起始序号",
        value=str(lp.get("start_index", "1")),
        hint_text="1=第一条", dense=True, width=96,
        disabled=(resume_dd.value != _FROM_INDEX_LABEL))

    def _sync_start_idx() -> None:
        """「起始序号」仅「从指定序号起重灌」档可用"""
        start_idx_field.disabled = resume_dd.value != _FROM_INDEX_LABEL
        _safe_update(start_idx_field)

    resume_dd.on_select = lambda e: _sync_start_idx()   # noqa: ARG005
    keep_sw = ft.Switch(label="保留", value=bool(lp.get("keep", False)))
    start_btn = ft.FilledButton("启动", icon=ft.Icons.PLAY_ARROW)
    # 40 号 §5.3：暂停 / 继续 / 停止 **三平级**（暂停常显、不可用置灰——与停止一致；
    # 继续仅暂停中显示，其余状态无含义）
    pause_btn = ft.OutlinedButton("暂停", icon=ft.Icons.PAUSE, disabled=True,
                                  tooltip="当前阶段结束后在阶段边界挂起（不产生断点，可继续）")
    continue_btn = ft.FilledButton("继续", icon=ft.Icons.PLAY_CIRCLE, visible=False,
                                   tooltip="从暂停的阶段边界接着跑（同一次运行）")
    stop_btn = ft.FilledButton("停止", icon=ft.Icons.STOP, disabled=True,
                               style=ft.ButtonStyle(bgcolor=t.c("danger")))

    toolbar = ft.Container(
        content=ft.Column([
            ft.Row([cases_field], spacing=8),
            ft.Row([device_chip, interval_field, timeout_field, resume_dd,
                    start_idx_field, keep_sw,
                    start_btn, pause_btn, continue_btn, stop_btn],
                   spacing=8, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=8),
        padding=12, border_radius=8,
        bgcolor=t.c("bg_chrome"), border=ft.border.Border.all(1, t.c("border")))

    # ── 阶段条 ──
    step_ui: dict[str, dict] = {}
    steps_row = ft.Row(spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _build_steps() -> None:
        ctrls: list[ft.Control] = []
        for i, s in enumerate(STAGES):
            if i:
                ctrls.append(ft.Container(width=22, height=2, bgcolor=t.c("border")))
            name = ft.Text(stage_label(s), size=10, color=t.c("stage_pending"))
            info = ft.Text("—", size=9, color=t.c("text_hint"))
            chip = ft.Container(
                content=ft.Column([name, info], spacing=0,
                                  horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.padding.Padding(left=10, right=10, top=5, bottom=5),
                border_radius=6, bgcolor=t.c("bg_panel"),
                border=ft.border.Border.all(1, t.c("stage_pending")),
                animate_opacity=300)
            step_ui[s] = {"chip": chip, "name": name, "info": info}
            ctrls.append(chip)
        steps_row.controls = ctrls

    def _refresh_steps() -> None:
        durations = stage_durations(state.current_run)
        for s in STAGES:
            ui = step_ui[s]
            st = state.stage_status.get(s, "pending")
            color = t.stage_color(st)
            ui["name"].color = color
            ui["chip"].border = ft.border.Border.all(1, color)
            if st == "running":
                ui["info"].value = (f"{stats['done']}/{stats['total']}"
                                    if s == "inject" and stats["total"] else "…")
                ui["info"].color = t.c("accent_text")
            elif st == "done":
                ui["info"].value = durations.get(s, "✓")
                ui["info"].color = t.c("stage_done")
            elif st == "failed":
                ui["info"].value = "✕"
                ui["info"].color = t.c("stage_failed")
            else:
                ui["info"].value = "—"
                ui["info"].color = t.c("text_hint")
            _safe_update(ui["chip"])
        _safe_update(steps_row)

    stepper_card = ft.Container(
        content=steps_row,
        padding=ft.padding.Padding(left=12, right=12, top=10, bottom=10),
        border_radius=8, bgcolor=t.c("bg_chrome"),
        border=ft.border.Border.all(1, t.c("border")))

    # ── 概览 ──
    progress_text = ft.Text("进度 —/—", size=10, color=t.c("text_secondary"))
    progress_bar = ft.ProgressBar(value=0, height=6,
                                  bgcolor=t.c("bg_panel"), color=t.c("accent"))
    count_pass = ft.Text("✓ 0", size=10, color=t.c("success"))
    count_fail = ft.Text("✗ 0", size=10, color=t.c("danger"))
    count_conf = ft.Text("⚠ —", size=10, color=t.c("warning"))
    eta_label = ft.Text("剩余 —", size=10, color=t.c("text_hint"))

    overview = ft.Container(
        content=ft.Row([
            progress_text,
            ft.Container(content=progress_bar, expand=True),
            count_pass, count_fail, count_conf, eta_label,
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.padding.Padding(left=12, right=12, top=8, bottom=8),
        border_radius=8, bgcolor=t.c("bg_chrome"),
        border=ft.border.Border.all(1, t.c("border")))

    def _refresh_overview() -> None:
        total, done = stats["total"], stats["done"]
        progress_bar.value = (done / total) if total else 0
        progress_text.value = f"进度 {done}/{total}" if total else "进度 —/—"
        count_pass.value = f"✓ {stats['pass']}"
        count_fail.value = f"✗ {stats['fail']}"
        count_conf.value = f"⚠ {stats['conflicts']}"
        eta_label.value = (eta_text(done, total, run_cfg["interval"])
                           if state.running else "—")
        for c in (progress_bar, progress_text, count_pass, count_fail, count_conf, eta_label):
            _safe_update(c)

    # ── 控制台 Dock ──
    console_list = ft.ListView(expand=True, spacing=0, auto_scroll=True)
    console_view = ft.Container(content=console_list, expand=True,
                                bgcolor=t.c("bg_console"), border_radius=6, padding=10)

    details_col = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
    details_view = ft.Container(content=details_col, expand=True, padding=8, visible=False)

    _log_colors = {
        "info": t.c("log_info"), "warn": t.c("log_warn"), "error": t.c("log_error"),
        "pass": t.c("log_ok"), "fail": t.c("log_error"), "cmd": t.c("log_cmd"),
    }

    def _append_log(text: str, tag: str = "info") -> None:
        console_list.controls.append(ft.Text(
            text, size=10.5, color=_log_colors.get(tag, t.c("log_text")),
            font_family="Consolas", selectable=True))
        if len(console_list.controls) > MAX_LOG_LINES:
            del console_list.controls[:100]
        _safe_update(console_list)

    def _dock_tab(label: str, key: str) -> tuple[ft.Container, ft.Text]:
        txt = ft.Text(label, size=11, color=t.c("text_secondary"))
        box = ft.Container(
            content=txt,
            padding=ft.padding.Padding(left=8, right=8, top=3, bottom=3),
            border=ft.border.Border.only(
                bottom=ft.border.BorderSide(2, ft.Colors.TRANSPARENT)),
            on_click=lambda e: _switch_dock(key), ink=True)
        return box, txt

    tab_c_box, tab_c_txt = _dock_tab("控制台", "console")
    tab_d_box, tab_d_txt = _dock_tab("阶段详情", "details")
    dock_tab_ui = {"console": (tab_c_box, tab_c_txt), "details": (tab_d_box, tab_d_txt)}

    def _switch_dock(key: str) -> None:
        for k, (box, txt) in dock_tab_ui.items():
            active = k == key
            txt.color = t.c("accent_text") if active else t.c("text_secondary")
            txt.weight = ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL
            box.border = ft.border.Border.only(bottom=ft.border.BorderSide(
                2, t.c("accent") if active else ft.Colors.TRANSPARENT))
            _safe_update(box)
        console_view.visible = key == "console"
        details_view.visible = key == "details"
        _safe_update(console_view)
        _safe_update(details_view)
        if key == "details":
            _refresh_details()

    clear_btn = ft.TextButton("清空", on_click=lambda e: _clear_console())
    export_btn = ft.TextButton("导出", on_click=lambda e: _export_console())
    collapse_btn = ft.TextButton("折叠 ⌄", on_click=lambda e: _toggle_collapse())

    dock_head = ft.Row([tab_c_box, tab_d_box, ft.Container(expand=True),
                        clear_btn, export_btn, collapse_btn],
                       spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    dock_body = ft.Container(
        content=ft.Column([console_view, details_view], spacing=0, expand=True),
        expand=True)
    dock_card = ft.Container(
        content=ft.Column([dock_head, dock_body], spacing=6, expand=True),
        padding=10, border_radius=8, expand=True,
        bgcolor=t.c("bg_chrome"), border=ft.border.Border.all(1, t.c("border")))

    def _clear_console() -> None:
        console_list.controls.clear()
        state.log.clear()
        _safe_update(console_list)

    def _toggle_collapse() -> None:
        dock["collapsed"] = not dock["collapsed"]
        dock_body.visible = not dock["collapsed"]
        collapse_btn.content = "展开 ⌃" if dock["collapsed"] else "折叠 ⌄"
        _safe_update(dock_body)
        _safe_update(collapse_btn)

    def _export_console() -> None:
        text = "\n".join(c.value for c in console_list.controls if isinstance(c, ft.Text))
        ctx = state.current_run
        out_dir = ctx.run_dir if ctx else config.LOGS_DIR
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"console_export_{time.strftime('%H%M%S')}.log"
            out.write_text(text, encoding="utf-8")
            toast_ui.toast(page, f"已导出：{out}", True, t)
        except Exception as ex:  # noqa: BLE001
            toast_ui.toast(page, f"导出失败：{ex}", False, t)

    # ── 阶段详情 ──
    def _refresh_details() -> None:
        ctx = state.current_run
        durations = stage_durations(ctx)
        glyph = {"pending": "○", "running": "◐", "done": "●",
                 "failed": "✕", "skipped": "—"}
        rows: list[ft.Control] = []
        for s in STAGES:
            st = state.stage_status.get(s, "pending")
            color = t.stage_color(st)
            text = state.stage_detail.get(s, "")
            if ctx:
                ss = ctx.stage(s)
                if ss.error:
                    text = f"⚠ {ss.error[:100]}"
            rows.append(ft.Row([
                ft.Text(f"{glyph.get(st, '?')} {stage_label(s)}", size=11,
                        color=color, width=110),
                ft.Text(durations.get(s, ""), size=10, color=t.c("text_hint"), width=42),
                ft.Text(text[:120], size=10, color=t.c("text_secondary"), expand=True),
            ], spacing=8))
        rows.append(ft.Divider(height=1, color=t.c("divider")))
        rows.append(ft.Text("产物", size=10, color=t.c("text_hint")))
        if ctx:
            rows.append(ft.Text(str(ctx.run_dir), size=10, selectable=True,
                                color=t.c("text_secondary")))
            if ctx.results_json.is_file():
                rows.append(ft.Text(f"· {ctx.results_json.name}", size=10,
                                    color=t.c("text_secondary"), selectable=True))
            if ctx.report_dir.is_dir():
                for f in sorted(ctx.report_dir.iterdir()):
                    rows.append(ft.Text(f"· {f.name}", size=10, selectable=True,
                                        color=t.c("accent_text")))
        else:
            rows.append(ft.Text("（尚无运行）", size=10, color=t.c("text_hint")))
        details_col.controls = rows
        _safe_update(details_col)

    # ── 实时刷新（节流）──
    def _refresh_live(force: bool = False) -> None:
        now = time.time()
        if not force and now - live["ts"] < LIVE_REFRESH_SECS:
            return
        live["ts"] = now
        ctx = state.current_run
        if ctx and state.running:
            done = count_progress_lines(ctx.progress_jsonl)
            if done:
                stats["done"] = done
        _refresh_overview()
        _refresh_steps()
        _refresh_details()

    def _sync_final_stats() -> None:
        ctx = state.current_run
        if not ctx or not ctx.results_json.is_file():
            return
        try:
            payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
            stats["pass"] = int(payload.get("pass", stats["pass"]))
            stats["fail"] = int(payload.get("fail", stats["fail"]))
            stats["conflicts"] = str(payload.get("conflicts", "—"))
            stats["total"] = int(payload.get("total", stats["total"])) or stats["total"]
            stats["done"] = stats["total"] or stats["done"]
        except (OSError, ValueError):
            pass

    # ── 日志消费（tick 循环调用）──
    def _consume(lines: list[tuple[str, str]]) -> None:
        for tag, text in lines:
            _append_log(text, tag)
        counts = count_tags(lines)
        if counts["pass"] or counts["fail"]:
            stats["pass"] += counts["pass"]
            stats["fail"] += counts["fail"]
            _refresh_overview()
        _refresh_live()

    state.log_target = _consume

    # ── 运行状态联动 ──
    def _on_run_state(running: bool) -> None:
        start_btn.disabled = running
        stop_btn.disabled = not running
        _sync_gate_buttons()
        _safe_update(start_btn)
        _safe_update(stop_btn)
        if not running:
            _sync_final_stats()
        _refresh_live(force=True)

    def _sync_gate_buttons() -> None:
        """暂停常显置灰 / 继续互斥显示（40 号 §5.3）；状态变化才更新（tick 内高频调用）"""
        running = bool(state.running)
        paused = running and bool(state.pause_gate.paused)
        key = (running, paused)
        if _sync_gate_buttons._last == key:        # noqa: SLF001 — 函数属性做 memo
            return
        _sync_gate_buttons._last = key
        pause_btn.disabled = not running or paused
        continue_btn.visible = paused
        _safe_update(pause_btn)
        _safe_update(continue_btn)

    _sync_gate_buttons._last = None  # type: ignore[attr-defined]

    state.on_running_change = _on_run_state

    # ── 呼吸动效（tick 钩子；离开页面由壳清空）──
    pulse = {"opacity": 1.0}

    def _pulse(tick: int) -> None:
        run_stage = (next((s for s in STAGES if state.stage_status.get(s) == "running"), None)
                     if state.running else None)
        if not run_stage:
            if pulse["opacity"] != 1.0:
                pulse["opacity"] = 1.0
                for s in STAGES:
                    step_ui[s]["chip"].opacity = 1.0
                    _safe_update(step_ui[s]["chip"])
            return
        val = 0.55 if (tick // PULSE_TICKS) % 2 == 0 else 1.0
        if val != pulse["opacity"]:
            pulse["opacity"] = val
            chip = step_ui[run_stage]["chip"]
            chip.opacity = val
            _safe_update(chip)

    state.tick_hooks.append(_pulse)
    state.tick_hooks.append(_device_tick)
    state.tick_hooks.append(lambda _t: _sync_gate_buttons())

    # ── 启动 / 停止 ──
    def _start(e) -> None:  # noqa: ANN001
        if state.running:
            return
        case_file = (cases_field.value or "").strip().strip('"')
        if not case_file or not Path(case_file).is_file():
            _append_log(f"用例文件不存在: {case_file or '(空)'}", "error")
            return
        try:
            interval = float(interval_field.value or config.DEFAULT_INTERVAL)
            timeout = float(timeout_field.value or judge_default_timeout())
        except ValueError:
            _append_log("间隔/超时必须是数字", "error")
            return

        # 灌入范围：从指定序号起重灌 = 前段不灌 + 第 N 条起重灌（redo_all + start_index）
        resume_mode = RESUME_MODES.get(resume_dd.value or "", "skip_executed")
        start_index = 0
        if resume_mode == "from_index":
            resume_mode = "redo_all"
            try:
                n = int((start_idx_field.value or "").strip())
            except ValueError:
                _append_log("起始序号必须是数字（1=第一条）", "error")
                return
            if n < 1:
                _append_log("起始序号必须 ≥ 1（1=第一条）", "error")
                return
            total = read_case_total(case_file)
            if total and n > total:
                _append_log(f"起始序号 {n} 超出用例总数（共 {total} 条）", "error")
                return
            start_index = n - 1

        params = PipelineParams(
            case_file=case_file,
            device=state.device or "",       # 设备由顶栏全局选择（本页只读）
            interval=interval,
            timeout=timeout,
            resume_mode=resume_mode,
            start_index=start_index)
        if not state.device:
            _append_log("未选择设备（顶栏）——将自动检测在线设备", "warn")
        if start_index:
            _append_log(f"灌入范围：从第 {start_index + 1} 条起重灌"
                        f"（前 {start_index} 条本次不灌）", "cmd")

        stats.update({"pass": 0, "fail": 0, "done": 0, "conflicts": "—"})
        stats["total"] = read_case_total(case_file)
        run_cfg["interval"] = interval
        live["ts"] = 0.0

        state.log.clear()
        console_list.controls.clear()
        state.log.append(f"启动流水线：{Path(case_file).name}", "cmd")

        state.last_params = {
            "case_file": case_file,
            "interval": interval,
            "timeout": timeout,
            "resume_label": resume_dd.value,
            "start_index": start_idx_field.value,
            "keep": bool(keep_sw.value),
        }
        if not state.start_pipeline(params, keep=bool(keep_sw.value)):
            _append_log("已有运行在进行中", "warn")
            return
        start_btn.disabled = True
        stop_btn.disabled = False
        _refresh_steps()
        _refresh_overview()
        _safe_update(start_btn)
        _safe_update(stop_btn)

    def _stop(e) -> None:  # noqa: ANN001
        state.request_stop()

    def _pause(e) -> None:  # noqa: ANN001
        if not state.pause_pipeline():
            _append_log("无法暂停（未在运行或已暂停）", "warn")

    def _continue(e) -> None:  # noqa: ANN001
        if not state.continue_pipeline():
            _append_log("当前没有暂停中的运行", "warn")

    start_btn.on_click = _start
    stop_btn.on_click = _stop
    pause_btn.on_click = _pause
    continue_btn.on_click = _continue

    # ── 首屏装配 ──
    _build_steps()
    state.log.drain()
    history = state.log.history()
    for _tag, _text in history:
        _append_log(_text, _tag)
    hcounts = count_tags(history)
    stats["pass"], stats["fail"] = hcounts["pass"], hcounts["fail"]
    ctx0 = state.current_run
    if ctx0:
        stats["total"] = read_case_total(ctx0.case_file)
        stats["done"] = count_progress_lines(ctx0.progress_jsonl)
    if not console_list.controls:
        _append_log("就绪。选择用例文件后点「启动」。", "info")
    _switch_dock("console")
    _refresh_steps()
    _refresh_overview()
    _refresh_details()
    _on_run_state(state.running)

    return ft.Column([toolbar, stepper_card, overview, dock_card],
                     spacing=8, expand=True)


def _safe_update(ctl) -> None:
    try:
        ctl.update()
    except Exception:  # noqa: BLE001
        pass
