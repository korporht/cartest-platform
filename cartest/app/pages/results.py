"""CarTest app — 结果页（R1 表格 + 右侧详情抽屉）

设计依据：14-UI重构设计.md §6.2（2026-09-16 用户裁决 R1）。

布局：
    筛选条：搜索（query/落域）· 状态 chips（带计数）· 运行选择器 · 导出 CSV
    表格：ID | query | 状态 | 落域 | 实时判定 | 备注（点行高亮 → 抽屉）
    抽屉：宽 260，150ms 滑入；实时判定 vs 设备日志对照 / 回复 / 判定理由 /
          复核告警 / 查看原始日志 · 复制详情

数据源：指定运行的 results.json（默认：selected_run_id → current_run → 最近含结果运行）。
"""
from __future__ import annotations

import csv
import json
import os

import flet as ft

from ...pipeline.context import RunContext
from ..state import AppState
from ..ui import components
from ..ui import toast as toast_ui


def build(page: ft.Page, state: AppState) -> ft.Control:
    t = state.theme
    sel = {"run_id": resolve_run_id(state), "idx": None}
    payload = load_payload(sel["run_id"])
    filt = {"chip": "全部", "status": "全部", "only": False, "kw": ""}

    # ── 基础控件 ──
    table = ft.ListView(expand=True, spacing=0)
    detail = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)
    drawer_inner = ft.Container(
        content=detail, width=252, padding=10,
        bgcolor=t.c("bg_chrome"), border_radius=8,
        border=ft.border.Border.all(1, t.c("border")))
    drawer = ft.Container(
        content=drawer_inner, width=0, clip_behavior=ft.ClipBehavior.HARD_EDGE,
        animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT))

    def _snack(text: str, ok: bool = True) -> None:
        """轻提示（顶部浮层 Toast；未装配时回退 SnackBar）"""
        toast_ui.toast(page, text, ok, t)

    def _rows() -> list[dict]:
        return (payload or {}).get("results", []) if payload else []

    # ── 工具条 ──
    search = ft.TextField(on_change=components.sync_text_value, hint_text="🔍 搜 query / 落域…",
                          dense=True, width=220, text_size=12)
    chips_row = ft.Row(spacing=6)
    meta_text = ft.Text("", size=10, color=t.c("text_hint"))
    runs = state.recent_runs(limit=10)
    run_dd = ft.Dropdown(
        label="运行", dense=True, width=340, text_size=12,
        options=[ft.dropdown.Option(
            key=r["run_id"],
            text=f'{r["name"]} · {r["created_at"][11:16]} · {r["status_text"]}')
            for r in runs],
        value=sel["run_id"] or None)
    export_btn = ft.TextButton("导出 CSV", icon=ft.Icons.DOWNLOAD,
                               on_click=lambda e: _export())

    def _set_filter(key: str) -> None:
        filt["chip"] = key
        if key == "需复核":
            filt["status"], filt["only"] = "全部", True
        else:
            filt["status"], filt["only"] = key, False
        _refresh_chips()
        _refresh_table()

    def _refresh_chips() -> None:
        c = status_counts(_rows())
        items = [
            ("全部", "全部", c["total"], "text_secondary"),
            ("通过", "pass", c["pass"], "success"),
            ("失败", "fail", c["fail"], "danger"),
            ("需复核", "需复核", c["conflict"], "warning"),
        ]
        chips_row.controls = [
            components.chip(t, f"{label} {n}", color_key=ck,
                            active=filt["chip"] == key,
                            on_click=lambda e, k=key: _set_filter(k))
            for label, key, n, ck in items]
        _safe_update(chips_row)

    # ── 表格 ──
    def _status_label(status: str) -> tuple[str, str]:
        if status == "pass":
            return "✓ 通过", t.c("success")
        if status == "fail":
            return "✗ 失败", t.c("danger")
        return str(status or "—"), t.c("text_secondary")

    def _make_row(idx: int, r: dict) -> ft.Container:
        is_sel = sel["idx"] == idx
        status_label, status_color = _status_label(str(r.get("status", "")))
        live = str(r.get("live_status", ""))
        note = str(r.get("note", ""))
        live_color = (t.c("success") if live == "pass"
                      else t.c("danger") if live == "fail" else t.c("text_hint"))
        return ft.Container(
            content=ft.Row([
                ft.Text(str(r.get("run_id", "")), size=10, width=34,
                        color=t.c("text_hint")),
                ft.Text(str(r.get("query", ""))[:60], size=11, expand=True,
                        color=t.c("text_primary"), no_wrap=True),
                ft.Text(status_label, size=10, width=64, color=status_color),
                ft.Text(str(r.get("domain", ""))[:8], size=10, width=66,
                        color=t.c("text_secondary"), no_wrap=True),
                ft.Text({"pass": "✓", "fail": "✗"}.get(live, "—"), size=10,
                        width=60, color=live_color),
                ft.Text(("⚠ " + note) if note else "", size=10, expand=True,
                        color=t.c("warning"), no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=8),
            padding=ft.padding.Padding(left=8, right=8, top=5, bottom=5),
            bgcolor=t.c("sel_bg") if is_sel else None,
            border=ft.border.Border(left=ft.border.BorderSide(
                2, t.c("accent") if is_sel else ft.Colors.TRANSPARENT)),
            on_click=lambda e, i=idx: _select(i),
            ink=True)

    def _empty_state() -> ft.Control:
        return ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.INBOX, size=42, color=t.c("text_hint")),
                ft.Text("还没有可查看的结果", size=13, color=t.c("text_primary")),
                ft.Text("在「流水线」页跑一次，或到「历史」页选择运行",
                        size=11, color=t.c("text_hint")),
                ft.FilledButton("去流水线跑一次", icon=ft.Icons.ROCKET_LAUNCH,
                                on_click=lambda e: state.nav_view("pipeline")),
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(top=60, left=20, right=20, bottom=20))

    def _refresh_table() -> None:
        if not payload:
            table.controls = [_empty_state()]
            _safe_update(table)
            return
        pairs = filter_rows(_rows(), filt["status"], filt["only"], filt["kw"])
        head = ft.Container(
            content=ft.Row([
                ft.Text("ID", size=9, width=34, color=t.c("text_hint")),
                ft.Text("query", size=9, expand=True, color=t.c("text_hint")),
                ft.Text("状态", size=9, width=64, color=t.c("text_hint")),
                ft.Text("落域", size=9, width=66, color=t.c("text_hint")),
                ft.Text("实时", size=9, width=60, color=t.c("text_hint")),
                ft.Text("备注", size=9, expand=True, color=t.c("text_hint")),
            ], spacing=8),
            padding=ft.padding.Padding(left=8, right=8, top=2, bottom=4))
        controls: list[ft.Control] = [head, ft.Divider(height=1, color=t.c("divider"))]
        if pairs:
            controls += [_make_row(i, r) for i, r in pairs]
        else:
            controls.append(ft.Text("（筛选后无数据）", size=11, color=t.c("text_hint")))
        table.controls = controls
        _safe_update(table)

    # ── 抽屉 ──
    def _kv(k: str, v, color: str | None = None, mono: bool = False) -> ft.Row:
        return ft.Row([
            ft.Text(k, size=10, width=56, color=t.c("text_secondary")),
            ft.Text(str(v or "（空）"), size=10, expand=True, selectable=True,
                    color=color or t.c("text_primary"),
                    font_family="Consolas" if mono else None),
        ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START)

    def _open_drawer() -> None:
        drawer.width = 260
        _safe_update(drawer)

    def _close_drawer() -> None:
        sel["idx"] = None
        drawer.width = 0
        _safe_update(drawer)
        _refresh_table()

    def _open_logs() -> None:
        try:
            ctx = RunContext.open(sel["run_id"])
            if page.web:
                _snack(f"Web 模式：日志目录 {ctx.device_logs_dir}")
                return
            os.startfile(str(ctx.device_logs_dir))  # noqa: S606 — 本机桌面用途
        except Exception as ex:  # noqa: BLE001
            _snack(f"打开失败：{ex}", ok=False)

    def _copy_detail(r: dict) -> None:
        text = (f"{r.get('run_id')} {r.get('query')}\n"
                f"状态: {r.get('status')}\n落域: {r.get('domain')}\n"
                f"回复: {r.get('reply')}\n"
                f"实时: {r.get('live_status')} / 设备: {r.get('device_domain')}\n"
                f"备注: {r.get('note', '')}")
        try:
            page.set_clipboard(text)   # 2026-09-24：勿直呼 page.clipboard.set（async，不调度=静默空操作）
            _snack("已复制详情")
        except Exception as ex:  # noqa: BLE001
            _snack(f"复制失败：{ex}", ok=False)

    def _refresh_detail() -> None:
        idx = sel["idx"]
        rows_all = _rows()
        if idx is None or not rows_all or idx >= len(rows_all):
            drawer.width = 0
            _safe_update(drawer)
            return
        r = rows_all[idx]
        status = str(r.get("status", ""))
        live = str(r.get("live_status", ""))
        note = str(r.get("note", ""))
        pill_text, pill_key = (("✓ 最终通过", "success") if status == "pass"
                               else ("✗ 未通过", "danger"))
        blocks: list[ft.Control] = [
            ft.Row([
                ft.Text(f"{r.get('run_id', '')} · {str(r.get('query', ''))[:22]}",
                        size=12, weight=ft.FontWeight.BOLD,
                        color=t.c("text_primary"), expand=True, no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ft.IconButton(ft.Icons.CLOSE, icon_size=14, tooltip="关闭",
                              icon_color=t.c("text_hint"),
                              on_click=lambda e: _close_drawer()),
            ], spacing=2),
            components.chip(t, pill_text, color_key=pill_key),
            ft.Text("实时判定", size=10, color=t.c("text_hint")),
            ft.Container(
                content=ft.Column([
                    _kv("结果", "失败" if live == "fail" else (live or "（无）"),
                        color=t.c("danger") if live == "fail" else None),
                    _kv("落域", r.get("live_domain", "")),
                ], spacing=2),
                padding=8, border_radius=6, bgcolor=t.c("bg_panel"),
                border=ft.border.Border.all(1, t.c("border"))),
            ft.Text("设备日志（权威）", size=10, color=t.c("text_hint")),
            ft.Container(
                content=ft.Column([
                    _kv("落域", r.get("device_domain", "")),
                    _kv("回复", r.get("reply", "")),
                ], spacing=2),
                padding=8, border_radius=6, bgcolor=t.c("bg_panel"),
                border=ft.border.Border.all(
                    1, t.c("success") if r.get("device_domain") else t.c("border"))),
            _kv("query时间", r.get("query_time", "")),
        ]
        if r.get("reason"):
            blocks += [
                ft.Text("判定理由", size=10, color=t.c("text_hint")),
                ft.Text(str(r["reason"])[:400], size=10, selectable=True,
                        font_family="Consolas", color=t.c("text_secondary")),
            ]
        if note:
            blocks.append(ft.Container(
                content=ft.Text(f"⚠ {note}", size=10, color=t.c("warning")),
                padding=8, border_radius=6, bgcolor=t.c("bg_panel")))
        blocks.append(ft.Row([
            ft.TextButton("查看原始日志", icon=ft.Icons.FOLDER_OPEN,
                          on_click=lambda e: _open_logs()),
            ft.TextButton("复制详情", icon=ft.Icons.COPY,
                          on_click=lambda e: _copy_detail(r)),
        ], spacing=2))
        detail.controls = blocks
        _safe_update(detail)
        drawer.width = 260
        _safe_update(drawer)

    def _select(idx: int) -> None:
        sel["idx"] = idx
        _refresh_table()
        _refresh_detail()

    # ── 导出 ──
    def _export() -> None:
        if not payload or not sel["run_id"]:
            _snack("无结果可导出", ok=False)
            return
        try:
            ctx = RunContext.open(sel["run_id"])
            ctx.report_dir.mkdir(parents=True, exist_ok=True)
            out = ctx.report_dir / f"{ctx.run_id}_results.csv"
            with open(out, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["用例ID", "query", "状态", "落域", "回复", "query时间", "备注"])
                for r in _rows():
                    w.writerow([r.get("run_id"), r.get("query"), r.get("status"),
                                r.get("domain"), r.get("reply"), r.get("query_time"),
                                r.get("note", "")])
            _snack(f"已导出：{out}")
        except Exception as ex:  # noqa: BLE001
            _snack(f"导出失败：{ex}", ok=False)

    # ── 运行切换 / 全量刷新 ──
    def _on_run_change(e) -> None:  # noqa: ANN001
        rid = run_dd.value or ""
        if rid:
            state.select_run(rid)
            _reload(rid)

    def _on_search(e) -> None:  # noqa: ANN001
        filt["kw"] = search.value or ""
        _refresh_table()

    run_dd.on_select = _on_run_change
    search.on_change = _on_search

    def _refresh_all() -> None:
        meta_text.value = (f"{sel['run_id']} · 生成于 {payload.get('generated_at', '—')}"
                           if payload else "（无运行）")
        _safe_update(meta_text)
        _refresh_chips()
        _refresh_table()
        _refresh_detail()

    def _reload(run_id: str) -> None:
        nonlocal payload
        sel["run_id"] = run_id
        sel["idx"] = None
        payload = load_payload(run_id)
        drawer.width = 0
        _refresh_all()

    # ── 装配 ──
    row1 = ft.Row([search, chips_row], spacing=10,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
    row2 = ft.Row([meta_text, ft.Container(expand=True), run_dd, export_btn],
                  spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    table_card = ft.Container(content=table, expand=True, bgcolor=t.c("bg_chrome"),
                              border_radius=8, padding=8,
                              border=ft.border.Border.all(1, t.c("border")))
    body = ft.Row([table_card, drawer], spacing=8, expand=True,
                  vertical_alignment=ft.CrossAxisAlignment.STRETCH)

    _refresh_all()
    return ft.Column([row1, row2, body], spacing=8, expand=True)


# ── 数据 ──────────────────────────────────────
def _glyph(status: str) -> str:
    return {"pass": "●", "fail": "✕", "pending": "○", "skip": "—"}.get(status, "?")


def _safe_update(ctl) -> None:
    try:
        ctl.update()
    except Exception:
        pass


# ── UI-3 助手（纯函数，可单测）────────────────────
def load_payload(run_id: str) -> dict | None:
    """读指定运行的 results.json；不可用 → None"""
    if not run_id:
        return None
    try:
        ctx = RunContext.open(run_id)
        if ctx.results_json.is_file():
            return json.loads(ctx.results_json.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 坏运行不拖垮页面
        pass
    return None


def resolve_run_id(state) -> str:
    """默认运行：selected_run_id → current_run（有结果）→ 最近含 results 的运行 → ''"""
    if getattr(state, "selected_run_id", ""):
        return state.selected_run_id
    ctx = getattr(state, "current_run", None)
    if ctx is not None and ctx.results_json.is_file():
        return ctx.run_id
    for r in RunContext.list_runs():
        try:
            c = RunContext.open(r["run_id"])
            if c.results_json.is_file():
                return c.run_id
        except Exception:  # noqa: BLE001
            continue
    return ""


def filter_rows(rows: list[dict], status: str, only_conflict: bool,
                keyword: str = "") -> list[tuple[int, dict]]:
    """筛选（返回 (原索引, 行)）：状态 / 仅需复核（note 非空）/ 关键字（query+落域）"""
    kw = keyword.strip().lower()
    out: list[tuple[int, dict]] = []
    for i, r in enumerate(rows):
        if status != "全部" and r.get("status") != status:
            continue
        if only_conflict and not r.get("note"):
            continue
        if kw and kw not in str(r.get("query", "")).lower() \
                and kw not in str(r.get("domain", "")).lower():
            continue
        out.append((i, r))
    return out


def status_counts(rows: list[dict]) -> dict[str, int]:
    """状态计数（chips 用）：总数/通过/失败/需复核"""
    return {
        "total": len(rows),
        "pass": sum(1 for r in rows if r.get("status") == "pass"),
        "fail": sum(1 for r in rows if r.get("status") == "fail"),
        "conflict": sum(1 for r in rows if r.get("note")),
    }
