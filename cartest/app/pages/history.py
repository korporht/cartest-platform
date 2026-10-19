"""CarTest app — 历史页（H1 运行管理表）

设计依据：14-UI重构设计.md §6.3（2026-09-16 用户裁决 H1）。

布局：
    工具条：搜索（名称）· 筛选 chips（全部/keep/中断）· 刷新 · 清理
    汇总行：清理策略一句话（describe）
    表格：时间 | 运行 | 设备 | 用例 | 结果 | 阶段微条 | 大小 | 操作
    行操作：结果（联动结果页）· 续跑（中断态）· 保留/取消保留 · 删除（确认弹窗）
"""
from __future__ import annotations

import json
import shutil

import flet as ft

from ...pipeline.cleanup import (
    KEEP_RECENT, cleanup_runs, describe, format_size, plan_cleanup,
)
from ...pipeline.context import STAGES, RunContext
from ..state import AppState
from ..ui import components
from ..ui import toast as toast_ui


def build(page: ft.Page, state: AppState) -> ft.Control:
    t = state.theme
    rows_all: list[dict] = []
    filt = {"chip": "全部", "mode": "全部", "kw": ""}

    table = ft.ListView(expand=True, spacing=0)
    summary_text = ft.Text("", size=10, color=t.c("text_hint"))
    search = ft.TextField(on_change=components.sync_text_value, hint_text="🔍 搜运行名…",
                              dense=True, width=200, text_size=12)
    chips_row = ft.Row(spacing=6)

    def _snack(text: str, ok: bool = True) -> None:
        """轻提示（顶部浮层 Toast；未装配时回退 SnackBar）"""
        toast_ui.toast(page, text, ok, t)

    # ── 组件 ──
    def _segments(stages: list[str]) -> ft.Row:
        """阶段微条：7 段色条（done/running/failed/pending）"""
        cells = [
            ft.Container(expand=1, height=6, border_radius=2,
                         bgcolor=t.stage_color(st if st else "pending"))
            for st in stages]
        return ft.Row(cells, spacing=2, width=104)

    def _result_text(r: dict) -> tuple[str, str]:
        if r["pass"] is not None:
            return (f"✓{r['pass']} ✗{r['fail']}",
                    t.c("danger") if r["fail"] else t.c("success"))
        return "未完成", t.c("text_hint")

    def _empty_state() -> ft.Control:
        return ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.HISTORY, size=42, color=t.c("text_hint")),
                ft.Text("暂无运行记录", size=13, color=t.c("text_primary")),
                ft.Text("在「流水线」页跑一次后，这里会列出全部运行",
                        size=11, color=t.c("text_hint")),
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(top=60, left=20, right=20, bottom=20))

    def _confirm(title: str, msg: str, on_ok) -> None:  # noqa: ANN001
        def _ok(e) -> None:  # noqa: ANN001
            try:
                page.pop_dialog()
            except Exception:  # noqa: BLE001
                pass
            on_ok()

        def _cancel(e) -> None:  # noqa: ANN001
            try:
                page.pop_dialog()
            except Exception:  # noqa: BLE001
                pass

        try:
            page.show_dialog(ft.AlertDialog(
                title=ft.Text(title, size=14),
                content=ft.Text(msg, size=12),
                actions=[ft.TextButton("取消", on_click=_cancel),
                         ft.FilledButton("确认", on_click=_ok)]))
        except Exception as ex:  # noqa: BLE001
            _snack(f"无法弹出确认框：{ex}", ok=False)

    # ── 行操作 ──
    def _act_results(rid: str) -> None:
        state.select_run(rid)
        state.nav_view("results")

    def _act_resume(rid: str) -> None:
        if not state.resume_pipeline(rid):
            _snack("已有运行在进行中，无法续跑", ok=False)
            return
        state.log.append(f"续跑已启动：{rid}", "info")
        state.nav_view("pipeline")

    def _act_toggle_keep(rid: str) -> None:
        try:
            ctx = RunContext.open(rid)
            ctx.keep = not ctx.keep
            ctx.save()
            _snack(f"{'已标记保留' if ctx.keep else '已取消保留'}：{rid}")
        except Exception as ex:  # noqa: BLE001
            _snack(f"操作失败：{ex}", ok=False)
        _refresh()

    def _do_delete(ctx) -> None:  # noqa: ANN001
        try:
            shutil.rmtree(ctx.run_dir)
            _snack(f"已删除：{ctx.run_id}")
        except Exception as ex:  # noqa: BLE001
            _snack(f"删除失败：{ex}", ok=False)
        _refresh()

    def _act_delete(rid: str) -> None:
        try:
            ctx = RunContext.open(rid)
            if ctx.keep:
                _snack(f"{rid} 已标记保留，请先取消保留再删除", ok=False)
                return
            _confirm("确认删除", f"将删除运行 {rid} 及其全部产物，不可恢复。",
                     lambda: _do_delete(ctx))
        except Exception as ex:  # noqa: BLE001
            _snack(f"删除失败：{ex}", ok=False)

    def _do_cleanup() -> None:
        deleted = cleanup_runs()
        _snack(f"已清理 {len(deleted)} 次旧运行" if deleted else "无需清理")
        _refresh()

    def _act_cleanup(e=None) -> None:  # noqa: ANN001
        candidates, stats = plan_cleanup()
        if not candidates:
            _snack("无需清理（未超过保留策略）")
            return
        _confirm("确认清理",
                 f"将清理 {len(candidates)} 次旧运行"
                 f"（保留最近 {KEEP_RECENT} 次与 keep 运行；"
                 f"清理后合计 {format_size(stats['after_bytes'])}）。",
                 _do_cleanup)

    # ── 刷新 ──
    def _refresh_chips() -> None:
        c_keep = sum(1 for r in rows_all if r["keep"])
        c_int = sum(1 for r in rows_all if not r["complete"])
        items = [("全部", "全部", len(rows_all)),
                 ("keep", "keep", c_keep),
                 ("中断", "中断", c_int)]
        chips_row.controls = [
            components.chip(t, f"{label} {n}", active=filt["chip"] == mode,
                            on_click=lambda e, m=mode: _set_filter(m))
            for label, mode, n in items]
        _safe_update(chips_row)

    def _set_filter(mode: str) -> None:
        filt["chip"], filt["mode"] = mode, mode
        _refresh_chips()
        _refresh_table()

    def _on_search(e) -> None:  # noqa: ANN001
        filt["kw"] = search.value or ""
        _refresh_table()

    def _run_row(r: dict) -> ft.Container:
        rid = r["run_id"]
        result_txt, result_color = _result_text(r)
        actions: list[ft.Control] = [
            ft.TextButton("结果", on_click=lambda e, x=rid: _act_results(x))]
        if not r["complete"]:
            actions.append(ft.TextButton("续跑", icon=ft.Icons.PLAY_ARROW,
                                         on_click=lambda e, x=rid: _act_resume(x)))
        actions += [
            ft.TextButton("取消保留" if r["keep"] else "保留",
                          on_click=lambda e, x=rid: _act_toggle_keep(x)),
            ft.TextButton("删除", on_click=lambda e, x=rid: _act_delete(x)),
        ]
        return ft.Container(
            content=ft.Row([
                ft.Text(r["created_at"][5:16] if r["created_at"] else "", size=10,
                        width=64, color=t.c("text_secondary")),
                ft.Text(r["name"][:30], size=11, expand=True,
                        color=t.c("text_primary"), no_wrap=True),
                ft.Text(r["device"][:10], size=10, width=84,
                        color=t.c("text_secondary"), no_wrap=True),
                ft.Text(str(r["cases"] or "—"), size=10, width=42,
                        color=t.c("text_secondary")),
                ft.Text(result_txt, size=10, width=96, color=result_color),
                _segments(r["stages"]),
                ft.Text(format_size(r["size"]), size=10, width=56,
                        color=t.c("text_secondary")),
                ft.Row(actions, spacing=0, expand=True),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(left=8, right=8, top=4, bottom=4))

    def _refresh_table() -> None:
        rows = filter_runs(rows_all, filt["mode"], filt["kw"])
        if not rows_all:
            table.controls = [_empty_state()]
            _safe_update(table)
            return
        head = ft.Container(
            content=ft.Row([
                ft.Text("时间", size=9, width=64, color=t.c("text_hint")),
                ft.Text("运行", size=9, expand=True, color=t.c("text_hint")),
                ft.Text("设备", size=9, width=84, color=t.c("text_hint")),
                ft.Text("用例", size=9, width=42, color=t.c("text_hint")),
                ft.Text("结果", size=9, width=96, color=t.c("text_hint")),
                ft.Text("阶段", size=9, width=104, color=t.c("text_hint")),
                ft.Text("大小", size=9, width=56, color=t.c("text_hint")),
                ft.Text("操作", size=9, expand=True, color=t.c("text_hint")),
            ], spacing=8),
            padding=ft.padding.Padding(left=8, right=8, top=2, bottom=4))
        controls: list[ft.Control] = [head, ft.Divider(height=1, color=t.c("divider"))]
        if rows:
            controls += [_run_row(r) for r in rows]
        else:
            controls.append(ft.Text("（筛选后无数据）", size=11, color=t.c("text_hint")))
        table.controls = controls
        _safe_update(table)

    def _refresh() -> None:
        nonlocal rows_all
        rows_all = load_run_rows()
        summary_text.value = describe()
        _safe_update(summary_text)
        _refresh_chips()
        _refresh_table()

    search.on_change = _on_search

    # ── 装配 ──
    refresh_btn = ft.IconButton(ft.Icons.REFRESH, tooltip="刷新",
                                icon_size=16, icon_color=t.c("text_secondary"),
                                on_click=lambda e: _refresh())
    cleanup_btn = ft.FilledButton("清理", icon=ft.Icons.CLEANING_SERVICES,
                                  on_click=_act_cleanup)
    toolbar = ft.Row([search, chips_row, ft.Container(expand=True),
                      refresh_btn, cleanup_btn],
                     spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    _refresh()
    return ft.Column([
        toolbar,
        summary_text,
        ft.Container(content=table, expand=True, bgcolor=t.c("bg_chrome"),
                     border_radius=8, padding=8,
                     border=ft.border.Border.all(1, t.c("border"))),
    ], spacing=8, expand=True)


def _safe_update(ctl) -> None:
    try:
        ctl.update()
    except Exception:
        pass


# ── UI-3 助手（纯函数，可单测）────────────────────
def _short_name(run_id: str) -> str:
    parts = run_id.split("_", 2)
    return parts[2] if len(parts) == 3 else run_id


def load_run_rows() -> list[dict]:
    """运行管理表的行数据（新→旧）：名称/时间/keep/大小/设备/用例数/阶段序列/计数"""
    rows: list[dict] = []
    for item in RunContext.list_runs():
        rid = item["run_id"]
        row = {
            "run_id": rid, "name": _short_name(rid),
            "created_at": item.get("created_at", ""), "keep": bool(item.get("keep")),
            "size": item.get("size", 0), "device": "", "cases": 0,
            "stages": [], "complete": False,
            "pass": None, "fail": None, "conflicts": None,
        }
        try:
            ctx = RunContext.open(rid)
            row["device"] = ctx.device
            row["complete"] = ctx.is_complete()
            row["stages"] = [ctx.stage(s).status for s in STAGES]
            if ctx.cases_json.is_file():
                data = json.loads(ctx.cases_json.read_text(encoding="utf-8"))
                row["cases"] = len(data) if isinstance(data, list) else 0
            if ctx.results_json.is_file():
                p = json.loads(ctx.results_json.read_text(encoding="utf-8"))
                row["pass"] = p.get("pass")
                row["fail"] = p.get("fail")
                row["conflicts"] = p.get("conflicts")
        except Exception:  # noqa: BLE001 — 坏运行不拖垮表格
            pass
        rows.append(row)
    return rows


def filter_runs(rows: list[dict], mode: str, keyword: str = "") -> list[dict]:
    """mode ∈ 全部/keep/中断；keyword 匹配 name/run_id（大小写不敏感）"""
    kw = keyword.strip().lower()
    out: list[dict] = []
    for r in rows:
        if mode == "keep" and not r["keep"]:
            continue
        if mode == "中断" and r["complete"]:
            continue
        if kw and kw not in r["name"].lower() and kw not in r["run_id"].lower():
            continue
        out.append(r)
    return out
