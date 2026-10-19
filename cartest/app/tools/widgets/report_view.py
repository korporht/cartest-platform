# -*- coding: utf-8 -*-
"""离线提取报告视图 — 在窗口内展示 CbmLogextractor 报告

设计（2026-09-21 用户裁决：*"是否有方案可以在窗口内显示报告；生成报告需要根据
文件格式及内容进行规划方案展示"*）：

产物 = **HTML 总报告**（``all_report_<stamp>.html``，单页内嵌详细报告 JSON——
两段 ``cbm`` / ``bds``，见 ``core/parser/report.py``；2026-10-08 起唯一对接产物）。
故**不直接平铺 HTML**（标签不可读），布局两段式：

  ① **KPI 行**：报告时间 · 总数 / 有回复 / 无回复 · CBM(命中/未匹配) · BDS
  ② **记录表**（占满全宽）：`时间 | query | 来源 | 落域 | 有回复`，点行开右侧
     **详情抽屉**（sid / logid / traceId / reqMessageId / 完整 reply / 缺失原因，
     底部可**按需展开原始 JSON**）

**右栏原始数据的去留（2026-10-15 用户裁决）**："原始数据表可以单独打开，或者
不做……目前这样可读性太差了" → **删除常驻右栏**（2026-10-12 的「表格 ⇄ 原始」
并排联动、``scroll_to(offset)`` 定位、块高固定那套随之移除）；需要看原始字段时
在详情抽屉点「原始 JSON」展开。看全量数据的正规入口＝「打开报告」（浏览器开
HTML）。

性能注记（2026-10-15）：右栏 Column 全量挂载 186 个 JSON 块曾致进页/刷新在浏览器
产生 950+700ms 长帧（~1.7s 冻结）；删右栏 + 搜索/筛选走 ``_rebuild_table_only``
（只重建左表）后长帧归零。

「报告」下拉＝历次总报告。形态与结果页（``pages/results.py``）一致：
工具条 + ListView 表 + 右抽屉 + 空态。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import flet as ft

from ....core.parser import offline as offline_mod
from ....core.parser import report as report_mod
from ...ui import components

#: 表格列宽（保持与结果页相近的紧凑风格）
_COLS = (("时间", 128), ("query", 250), ("来源", 48), ("落域", 90), ("回复", 0))

#: 详情抽屉宽度（右栏删除后放开一些；JSON/reply 可读性优先）
_DRAWER_W = 300

#: 等宽字体栈（原始 JSON 块）
_MONO = "JetBrains Mono, Cascadia Mono, Consolas, monospace"


class ReportView:
    """报告视图（解析 → KPI → 记录表 → 详情抽屉＋原始 JSON 按需展开）"""

    def __init__(self, ctx, on_status=None) -> None:  # noqa: ANN001
        self.ctx = ctx
        self.t = ctx.theme
        self._on_status = on_status
        self._path: str = ""
        self._rows: list[dict] = []
        self._summary: dict = {}
        self._mode: str = "all"          # all | answered | unanswered
        self._kw: str = ""
        self._sel_idx: int | None = None          # 选中记录的**原始序号**（全量 _rows 下标）
        self._sel_prev: int | None = None         # 上一次高亮的行（只重绘两行）
        self._row_refs: dict[int, ft.Container] = {}   # 原始序号 → 表格行容器
        self._detail_json = False                      # 抽屉「原始 JSON」展开态（点行重置）
        self._drawer_open: bool = False                # 详情抽屉开合（重建时恢复）

        # 注意：记录表 / 详情抽屉都**在 ``_rebuild_body`` 里新建**，不在此处
        # 复用——见该方法的说明（Flet 0.85 的 frozen 机制会整应用报错）。
        self._body = ft.Container(expand=True)

        self._kpi = ft.Text("", size=10, color=self.t.c("text_secondary"),
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._search = ft.TextField(
            on_change=lambda e: [components.sync_text_value(e),
                                 self._rebuild_table_only()],
            hint_text="🔍 搜 query / 落域 / sid…",
            dense=True, width=220, text_size=12)
        self._report_dd = ft.Dropdown(label="报告", dense=True, width=300, text_size=12,
                                     on_select=lambda e: self._on_report_select())
        self._view_row = ft.Row(spacing=6)

    # ── 构建 ────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        self.refresh_reports()
        self._rebuild_body()

        def _snack(text: str, ok: bool = True) -> None:
            try:
                self.ctx.set_status(text, "success" if ok else "danger")
            except Exception:  # noqa: BLE001
                pass

        return ft.Column([
            ft.Row([self._report_dd, self._search,
                    ft.TextButton("刷新", icon=ft.Icons.REFRESH,
                                  on_click=lambda e: self.load_latest()),
                    ft.TextButton("打开报告", icon=ft.Icons.DESCRIPTION,
                                  on_click=lambda e: self._open_report()),
                    ft.TextButton("打开目录", icon=ft.Icons.FOLDER,
                                  on_click=lambda e: self._open_dir())],
                   spacing=8, wrap=True),
            ft.Row([self._view_row, ft.Container(width=8), self._kpi],
                   spacing=6, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self._body,
        ], spacing=6, expand=True)

    def _pane(self, title: str, content: ft.Control, *, expand: int) -> ft.Container:
        """分栏外壳：小标题 + 内容"""
        t = self.t
        return ft.Container(
            content=ft.Column([
                ft.Text(title, size=10, color=t.c("text_hint")),
                content,
            ], spacing=4, expand=True),
            expand=expand, padding=6, border_radius=8,
            border=ft.border.Border.all(1, t.c("border")))

    def refresh_reports(self) -> None:
        """填充报告下拉（历次 HTML 总报告，按 mtime 倒序）

        2026-10-08 用户裁决：**只认 HTML 这一份产物**（"只需要提取后的产物，
        保持在一份"）——只列根目录 ``all_report_*.html``；``_raw_<stamp>/``
        归档的 md 分报告不再入下拉（需要时经「打开目录」查阅）。
        """
        items: list[tuple[str, str]] = []
        try:
            d = offline_mod.reports_dir()
            if d.is_dir():
                files = [f for f in d.glob("all_report_*.html") if f.is_file()]
                files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                for f in files[:30]:
                    items.append((str(f), f.name))
        except Exception:  # noqa: BLE001
            items = []
        self._report_dd.options = [ft.dropdown.Option(key=k, text=v) for k, v in items]
        if items and self._report_dd.value not in [k for k, _ in items]:
            self._report_dd.value = items[0][0]
        try:
            self._report_dd.update()
        except Exception:  # noqa: BLE001
            pass

    def load_latest(self, notify: bool = True) -> bool:
        """加载最新报告"""
        try:
            p = offline_mod.latest_report()
        except Exception as ex:  # noqa: BLE001
            p = None
            if notify:
                self._status(f"读取报告目录失败：{ex}", ok=False)
        if not p:
            if notify:
                self._status("暂无报告——先在下方选择日志目录并点「提取」", ok=False)
            return False
        return self.load(p, notify=notify)

    def load(self, path: str | Path, notify: bool = True) -> bool:
        """解析并展示指定报告（失败显式提示，不静默）"""
        try:
            data, _raw = report_mod.load_report(path)
        except Exception as ex:  # noqa: BLE001
            if notify:
                self._status(f"报告解析失败：{ex}", ok=False)
            return False
        self._path = str(path)
        self._rows = report_mod.iter_records(data)
        self._summary = report_mod.summarize(data)
        self._sel_idx = None
        self._sel_prev = None
        self._detail_json = False
        self.refresh_reports()
        self._report_dd.value = str(path)
        self._rebuild_body()
        if notify:
            self._status(f"已加载报告：{os.path.basename(str(path))}（{len(self._rows)} 条）")
        return True

    # ── 内部 ────────────────────────────────
    def _status(self, text: str, ok: bool = True) -> None:
        if self._on_status:
            try:
                self._on_status(text, ok)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            self.ctx.set_status(text, "success" if ok else "danger")
        except Exception:  # noqa: BLE001
            pass

    def _on_report_select(self) -> None:
        """下拉切换：解析失败时回退到当前已加载报告（防「下拉已切、内容未换」错位）"""
        if self._report_dd.value:
            if not self.load(self._report_dd.value):
                self._report_dd.value = self._path or None
                try:
                    self._report_dd.update()
                except Exception:  # noqa: BLE001
                    pass

    def _filtered(self) -> list[tuple[int, dict]]:
        """筛选后的 ``(原始序号, 记录)`` —— 原始序号＝全量 ``_rows`` 下标（选中态跨筛选稳定）"""
        rows = list(enumerate(self._rows))
        if self._mode == "answered":
            rows = [(i, r) for i, r in rows if str(r.get("reply") or "").strip()]
        elif self._mode == "unanswered":
            rows = [(i, r) for i, r in rows if not str(r.get("reply") or "").strip()]
        kw = (self._search.value or "").strip().lower()
        if kw:
            rows = [(i, r) for i, r in rows
                    if kw in " ".join(str(r.get(k, "")) for k in
                                      ("query", "domain", "sid", "logid", "reply")).lower()]
        return rows

    def _rebuild_body(self) -> None:
        """重建 KPI / 筛选胶囊 / 记录表 + 详情抽屉（2026-10-15 起无右栏）

        表与抽屉控件**每次新建**：它们是 ``self._body`` 的子控件，而旧 body 被替换
        时旧子树会被 Flet 0.85 标记 frozen，复用实例再 ``update()`` 会抛
        "Frozen controls cannot be updated" 并整应用报错（2026-10-12 实测：
        切筛选后再点行必崩）。新建则永远挂在当前树上。
        """
        t = self.t
        self._table = ft.ListView(expand=True, spacing=0)
        self._table.controls = self._table_controls()
        self._detail = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)
        if self._sel_idx is not None and 0 <= self._sel_idx < len(self._rows):
            self._detail.controls = self._detail_controls(self._rows[self._sel_idx],
                                                          self._sel_idx)
        self._drawer = ft.Container(
            content=ft.Container(
                content=self._detail, width=_DRAWER_W - 20, padding=10,
                bgcolor=t.c("bg_chrome"), border_radius=8,
                border=ft.border.Border.all(1, t.c("border"))),
            width=_DRAWER_W if self._drawer_open else 0,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT))

        sm = self._summary or {}
        src = f"{os.path.basename(self._path)} · " if self._path else ""
        if sm:
            sec = sm.get("sections", {})
            cbm = sec.get("cbm", {})
            bds = sec.get("bds", {})
            self._kpi.value = (
                f"{src}报告时间 {sm.get('report_time') or '—'} · "
                f"记录 {sm.get('total', 0)} · 有回复 {sm.get('answered', 0)} · "
                f"无回复 {sm.get('unanswered', 0)} · "
                f"CBM {cbm.get('total', '—')}（命中 {cbm.get('matched', '—')}，"
                f"未匹配 query {cbm.get('unmatched_query', '—')}，"
                f"未匹配 logid {cbm.get('unmatched_logid', '—')}） · "
                f"BDS {bds.get('total', '—')}")
        else:
            self._kpi.value = ""

        self._view_row.controls = self._chips()

        self._body.content = ft.Row([
            self._pane("记录表", self._table, expand=1),
            self._drawer,
        ], spacing=8, expand=True)
        for c in (self._kpi, self._view_row, self._body, self._report_dd):
            try:
                c.update()
            except Exception:  # noqa: BLE001
                pass

    # ── 行高亮（右栏删除后仅左表；只重绘新旧两行）────────
    def _row_bg(self, selected: bool) -> str:
        """表格行底色（选中＝``sel_bg``；未选中与面板同色，看不出边界）"""
        return self.t.c("sel_bg") if selected else self.t.c("bg_panel")

    def _highlight_row(self, orig: int) -> None:
        """点表格行 → 左表新旧两行重绘底色（原右栏联动高亮的左表部分）"""
        for i in (self._sel_prev, orig):
            if i is None:
                continue
            row = self._row_refs.get(i)
            if row is not None:
                row.bgcolor = self._row_bg(i == orig)
                try:
                    row.update()
                except Exception:  # noqa: BLE001
                    pass
        self._sel_prev = orig

    def _chips(self) -> list[ft.Control]:
        """筛选胶囊（全部/有回复/无回复，含计数与激活态）"""
        t = self.t
        return [
            components.chip(t, f"全部 {len(self._rows)}",
                            on_click=lambda e: self._set_filter("all"),
                            active=(self._mode == "all")),
            components.chip(t, f"有回复 {sum(1 for r in self._rows if str(r.get('reply') or '').strip())}",
                            on_click=lambda e: self._set_filter("answered"),
                            active=(self._mode == "answered")),
            components.chip(t, f"无回复 {sum(1 for r in self._rows if not str(r.get('reply') or '').strip())}",
                            on_click=lambda e: self._set_filter("unanswered"),
                            active=(self._mode == "unanswered")),
        ]

    def _rebuild_table_only(self) -> None:
        """搜索/筛选只作用于本表 → 只重建表 + 筛选胶囊（不动抽屉/KPI）

        整版 ``_rebuild_body`` 全量重建曾明显卡顿（2026-10-15），轻量路径只换表与胶囊。
        """
        self._table.controls = self._table_controls()
        self._view_row.controls = self._chips()
        for c in (self._table, self._view_row):
            try:
                c.update()
            except Exception:  # noqa: BLE001
                pass

    def _set_filter(self, mode: str) -> None:
        self._mode = mode
        self._rebuild_table_only()

    def _table_controls(self) -> list[ft.Control]:
        t = self.t
        self._row_refs = {}
        rows = self._filtered()
        if not self._rows:
            return [ft.Container(
                content=ft.Text("暂无报告——在上方选择日志目录并点「提取」；"
                                "或从「报告」下拉选历次产物", size=11,
                                color=t.c("text_hint")),
                padding=ft.padding.Padding(left=8, top=12))]
        if not rows:
            return [ft.Container(
                content=ft.Text("当前筛选无记录", size=11, color=t.c("text_hint")),
                padding=ft.padding.Padding(left=8, top=12))]
        head = ft.Container(
            content=ft.Row([
                ft.Text(label, size=9, width=w if w else None,
                        expand=(w == 0), color=t.c("text_hint"))
                for label, w in _COLS], spacing=6),
            padding=ft.padding.Padding(left=8, right=8, top=6, bottom=4),
            border=ft.border.Border.only(bottom=ft.BorderSide(1, t.c("border"))))
        out: list[ft.Control] = [head]
        for orig, r in rows[:400]:
            out.append(self._make_row(orig, r))
        return out

    def _make_row(self, orig: int, r: dict) -> ft.Control:
        """一行记录（``orig`` = 全量 ``_rows`` 下标，选中态跨筛选稳定）"""
        t = self.t
        has_reply = bool(str(r.get("reply") or "").strip())
        cells = [
            ft.Text(str(r.get("query_time", ""))[-12:], size=10, width=_COLS[0][1],
                    color=t.c("text_secondary")),
            ft.Text(str(r.get("query", ""))[:60], size=10, width=_COLS[1][1],
                    color=t.c("text_primary"), no_wrap=True),
            ft.Text(str(r.get("source", "")).upper(), size=9, width=_COLS[2][1],
                    color=t.c("accent")),
            ft.Text(str(r.get("domain", ""))[:10], size=10, width=_COLS[3][1],
                    color=t.c("text_secondary"), no_wrap=True),
            ft.Text(("✓ " + str(r.get("reply", ""))[:40]) if has_reply
                    else (str(r.get("reply_status") or "无回复")),
                    size=10, expand=True, no_wrap=True,
                    color=t.c("text_secondary" if has_reply else "warning")),
        ]
        row = ft.Container(
            content=ft.GestureDetector(
                content=ft.Row(cells, spacing=6),
                on_tap=lambda e, i=orig: self._select(i)),
            padding=ft.padding.Padding(left=8, right=8, top=4, bottom=4),
            border_radius=4, bgcolor=self._row_bg(orig == self._sel_idx))
        self._row_refs[orig] = row
        return row

    def _detail_controls(self, r: dict, orig: int) -> list[ft.Control]:
        """详情抽屉内容（选中记录的字段逐条列出）"""
        t = self.t

        def _kv(k: str, v, color: str | None = None) -> ft.Row:
            return ft.Row([
                ft.Text(k, size=10, width=76, color=t.c("text_secondary")),
                ft.Text(str(v) if v not in (None, "") else "—", size=10, expand=True,
                        color=color or t.c("text_primary"), selectable=True),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.START)

        out = [
            ft.Row([ft.Text(f"记录 #{orig + 1}", size=11, weight=ft.FontWeight.BOLD,
                            color=t.c("text_primary")),
                    ft.Container(expand=True),
                    ft.IconButton(ft.Icons.CLOSE, icon_size=14,
                                  on_click=lambda e: self._close_drawer())]),
            _kv("来源", str(r.get("source", "")).upper()),
            _kv("query 时间", r.get("query_time")),
            _kv("query", r.get("query")),
            _kv("落域", r.get("domain")),
            _kv("sid", r.get("sid")),
            _kv("logid", r.get("logid")),
            _kv("logid 时间", r.get("logid_time")),
            _kv("traceId", r.get("traceId")),
            _kv("reqMessageId", r.get("reqMessageId")),
            _kv("pk 类型", r.get("cbm_tool_pk_type")),
            _kv("回复状态", r.get("reply_status"),
                color=t.c("warning") if r.get("reply_status") else None),
            _kv("缺失原因", r.get("reply_missing_reason"),
                color=t.c("warning") if r.get("reply_missing_reason") else None),
            _kv("来源文件", r.get("source_file")),
            ft.Text("回复", size=10, color=t.c("text_secondary")),
            ft.Text(str(r.get("reply") or "—"), size=10, selectable=True,
                    color=t.c("text_primary")),
            ft.TextButton("复制该记录", icon=ft.Icons.COPY,
                          on_click=lambda e, rr=r: self._copy(rr)),
            # 原始 JSON 按需展开（2026-10-15 用户裁决：右栏删除，字段明细＋JSON 收进抽屉）
            ft.TextButton("展开原始 JSON" if not self._detail_json
                          else "收起原始 JSON",
                          icon=ft.Icons.UNFOLD_MORE if not self._detail_json
                          else ft.Icons.UNFOLD_LESS,
                          on_click=lambda e, rr=r, oo=orig: self._toggle_detail_json(rr, oo)),
        ]
        if self._detail_json:
            out.append(ft.Text(json.dumps(r, ensure_ascii=False, indent=2),
                               size=9, font_family=_MONO, selectable=True,
                               color=t.c("text_secondary")))
        return out

    def _toggle_detail_json(self, r: dict, orig: int) -> None:
        """抽屉内展开/收起原始 JSON（重建抽屉内容——控件量小，无性能顾虑）"""
        self._detail_json = not self._detail_json
        self._detail.controls = self._detail_controls(r, orig)
        try:
            self._detail.update()
        except Exception:  # noqa: BLE001
            pass

    def _select(self, orig: int) -> None:
        """点表格行 → 详情抽屉 + 左表行高亮（原始 JSON 在抽屉内按需展开）"""
        if not (0 <= orig < len(self._rows)):
            return
        self._sel_idx = orig
        self._detail_json = False                  # 换记录时收起上一条的 JSON
        self._detail.controls = self._detail_controls(self._rows[orig], orig)
        self._open_drawer()
        self._highlight_row(orig)

    def _open_drawer(self) -> None:
        self._drawer_open = True
        self._drawer.width = 252
        try:
            self._drawer.update()
        except Exception:  # noqa: BLE001
            pass

    def _close_drawer(self) -> None:
        self._drawer_open = False
        self._drawer.width = 0
        try:
            self._drawer.update()
        except Exception:  # noqa: BLE001
            pass

    def _copy(self, r: dict) -> None:
        text = "\n".join(f"{k}: {r.get(k, '')}" for k in report_mod.RECORD_FIELDS)
        try:
            self.ctx._page.set_clipboard(text)   # 内部走 run_task（Clipboard.set 是 async）
            self._status("已复制该记录")
        except Exception as ex:  # noqa: BLE001
            self._status(f"复制失败：{ex}", ok=False)

    def _open_report(self) -> None:
        if not self._path:
            self.load_latest()
        try:
            if self._path and not self.ctx._page.web:  # noqa: SLF001
                os.startfile(self._path)               # noqa: S606
        except Exception:  # noqa: BLE001
            pass

    def _open_dir(self) -> None:
        try:
            if not self.ctx._page.web:                 # noqa: SLF001
                os.startfile(str(offline_mod.reports_dir()))   # noqa: S606
        except Exception:  # noqa: BLE001
            pass
