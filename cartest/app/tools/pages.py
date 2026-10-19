# -*- coding: utf-8 -*-
"""CarTest tools — 平台页面壳

把搬入的 Tab 包成平台的页面（签名 ``build(page, state) -> ft.Control``）：

  · 创建 ``ToolContext``（宿主适配）
  · 用 ``state.tool_state`` 回填上次输入
  · 注册 ``state.before_page_leave`` 在切页/切主题前保存输入态

Tab 本体保持与 ``flet_Tool`` 一致（见 06-P3b设计.md §4.3）。
合并页（``_wrap_merged``）：多子工具 chips 切换（见 18-工具箱与导航重构设计.md §四）。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable

import flet as ft

from ..state import AppState
from ..ui import components
from .context import ToolContext
from .apk_tab import ApkTab
from .log_tab import LogTab
from .logextract_tab import LogExtractTab
from .monkey_tab import MonkeyTab
from .push_tab import PushTab
from .recorder_tab import RecorderTab
from .single_inject_tab import SingleInjectTab
from .timestamp_tab import TimestampTab
from .touch_tab import TouchTab
from .widgets.offline_extract import OfflineExtractPanel
from .widgets.report_view import ReportView


def _wrap(tab, state: AppState, key: str, ctx: ToolContext,
          *, with_status: bool = True, with_log: bool = False) -> ft.Control:
    """公共页面壳：构建控件 → 回填输入 → 注册离开时保存 → 组装

    **顺序**：必须先 ``build()``——Tab 的 ``restore_state()`` 写自己的输入控件
    （``_input_field.value``），控件不存在时它有 ``hasattr`` 保护，会**静默不生效**。

    **已知限制（Flet Web 端）**：控件挂载后再修改 ``TextField.value`` 不会被前端渲染
    （前端的 TextEditingController 以本地态优先），所以切页回来**输入框显示为空**。
    值本身已写入内存——点「转换」等按钮即按该值执行，**功能无损**，只差显示。
    若要连显示一起恢复，需让 Tab 的 ``build()`` 读取预置值（那会改动搬入的代码，暂不做）。

    **布局（2026-10-12 用户裁决）**：带日志窗口时（``with_log=True``）内容只占**自身
    高度**、日志面板铺满剩余——与 :func:`_wrap_merged` 同口径（用户反馈「窗口起点太低」：
    原先主体与面板各占一半，内容不满时空白全压在面板上方）。不带日志窗口时内容铺满整页。
    2026-09-18 的旧口径（主体 ``expand=True`` 各占一半）由此取代。
    """
    main_control = tab.build()

    saved = state.tool_state.get(key)
    if saved:
        try:
            tab.restore_state(saved)
        except Exception as ex:  # noqa: BLE001
            ctx.append_log_to_panel(f"[界面] 恢复输入态失败: {ex}")

    # 生命周期：进入页面（对等 flet_Tool 的 on_show 调用）
    try:
        tab.on_show()
    except Exception as ex:  # noqa: BLE001
        ctx.append_log_to_panel(f"[界面] on_show 异常: {ex}")

    def _save() -> None:
        try:
            state.tool_state[key] = tab.save_state()
        except Exception:  # noqa: BLE001
            pass
        # 生命周期：离开页面。**40 号新语义：离页不停任务**——只解绑 UI
        # （on_detach）；任务归 state.tasks（registry）持有，顶栏指示器可见、
        # 抽屉可停（原"离页即 on_hide 停任务"口径作废，见 40 号 §三）。
        try:
            detach = getattr(tab, "on_detach", None)
            if callable(detach):
                detach()
            elif hasattr(tab, "on_hide"):
                tab.on_hide()          # 旧宿主/测试替身兼容回退
        except Exception as ex:  # noqa: BLE001
            ctx.append_log_to_panel(f"[界面] on_detach 异常: {ex}")

    state.before_page_leave = _save

    rows: list[ft.Control] = [
        ft.Container(content=main_control, expand=not with_log)]
    if with_log:
        rows.append(ctx.build_log_panel())
    if with_status:
        status = ft.Text("就绪", size=11, color=ctx.theme.c("text_secondary"))
        ctx.attach_status(status)
        rows.append(ft.Container(
            content=ft.Row([status], spacing=8),
            padding=ft.padding.Padding(left=8, top=4, bottom=4, right=8)))

    return ft.Column(rows, spacing=8, expand=True)


def _safe_update(ctl) -> None:
    """控件更新：控件可能尚未挂载（切页瞬间），失败不致命"""
    try:
        ctl.update()
    except Exception:  # noqa: BLE001
        pass


@dataclasses.dataclass
class Sub:
    """合并页子工具声明（``_wrap_merged`` 的 subs 项）"""

    key: str
    label: str
    make: Callable                      # make(ctx) -> tab/panel（需 build()）
    with_log: bool | None = None        # None = 沿用页面级 with_log
    keep_alive: bool = False            # True = 切走不停后台任务（离页仍停）


def _normalize_subs(subs) -> list[Sub]:
    """``(key, label, make[, with_log][, keep_alive])`` → :class:`Sub` 列表

    兼容 3/4/5 元组：既有页面（应用与传输 / 屏幕与触控）不改调用点。
    """
    out: list[Sub] = []
    for item in subs:
        if isinstance(item, Sub):
            out.append(item)
            continue
        key, label, make = item[0], item[1], item[2]
        with_log = item[3] if len(item) > 3 else None
        keep_alive = bool(item[4]) if len(item) > 4 else False
        out.append(Sub(key, label, make, with_log, keep_alive))
    return out


def resolve_active(saved: dict | None, keys: list[str]) -> str:
    """合并页当前子工具的选取：记住上次（仅合法值）→ 否则首项"""
    if saved:
        act = saved.get("_active")
        if act in keys:
            return act
    return keys[0] if keys else ""


def _wrap_merged(page: ft.Page, state: AppState, base_key: str,
                 subs: list[tuple], *,
                 with_status: bool = True, with_log: bool = False) -> ft.Control:
    """合并页壳：子工具 chips 切换 + 常驻内容槽 + 状态行 +（可选）日志面板

    **纵向分配（2026-10-12 用户裁决）**：带日志面板的子工具——内容只占**自身高度**，
    日志面板铺满剩余（``expand=True``）；不带面板的子工具——内容铺满整页。
    此前是「内容与面板各占一半」，内容不满时空白全压在面板上方，用户感知为
    「窗口起点位置太低」（应用与传输实测：内容到 y≈296 就结束，面板却从 y≈460 才起）。

    ``subs`` = ``[(sub_key, label, make_tab[, with_log][, keep_alive]), ...]``，
    ``make_tab(ctx) -> BaseTab``（需 ``build()``）。3/4/5 元组均兼容，见
    :func:`_normalize_subs`。

    **常驻 + 可见性切换（2026-09-21，28-日志终端合并页改造设计.md §3.3）**：
    每个 tab 首次进入时构建一次（懒），此后**常驻控件树**，切换只改 ``visible``
    ——旧的「切换时替换内容/面板」写法已整体删除。三个收益：
      ① 切 tab 不打断后台任务（控件不是"卸载后重建"）——Logcat 抓取可跨 tab 继续；
      ② 规避已知 Web 端坑：动态重建/替换可点控件会留下**失效命中区**（发现 #11）；
      ③ 每 tab 各自持有日志面板 + 只切可见性 → 「切换后客户端仍显示上一个面板」的
         问题（发现 #8）**由构造消除**。

    生命周期与 :func:`_wrap` 对等（build → restore → on_show；离开 save + on_hide）：
      · 仅激活的子工具被构建（惰性）；切换时旧子工具 ``on_hide``（``keep_alive``
        为真则跳过——如日志终端 tab 的 Logcat 抓取不因切 tab 中断；**离页一律 on_hide**）
      · 输入态沿用 per-key ``tool_state``（``apk`` / ``push`` …）
      · 当前子工具记录在 ``tool_state[base_key]["_active"]``
    """
    subs_n = _normalize_subs(subs)
    keys = [s.key for s in subs_n]
    by_key = {s.key: s for s in subs_n}
    active_key = resolve_active(state.tool_state.get(base_key), keys)

    built: dict[str, dict] = {}
    content_slots: dict[str, ft.Container] = {}      # 常驻内容槽（按 tab key）
    log_slots: dict[str, ft.Container] = {}          # 常驻日志面板槽（仅带面板的 tab）
    slots_col = ft.Column(spacing=8, expand=True)
    status_text = ft.Text("就绪", size=11, color=state.theme.c("text_secondary"))
    chips_row = ft.Row(spacing=8)
    counters: dict = {"hide_total": 0, "hide": {}}      # 测试钩子用（生命周期计数）

    def _ensure(sub_key: str) -> dict:
        if sub_key in built:
            return built[sub_key]
        sub = by_key[sub_key]
        ctx = ToolContext(page, state, sub_key)
        if with_status:
            ctx.attach_status(status_text)
        tab = sub.make(ctx)
        control = tab.build()
        saved = state.tool_state.get(sub_key)
        if saved:
            try:
                tab.restore_state(saved)
            except Exception as ex:  # noqa: BLE001
                ctx.append_log_to_panel(f"[界面] 恢复输入态失败: {ex}")

        use_log = sub.with_log if sub.with_log is not None else with_log
        # 有日志面板 → 内容只占自身高度、面板铺满剩余；无面板 → 内容铺满整页
        content_slot = ft.Container(content=control, expand=not use_log, visible=False)
        log_slot = None
        if use_log:
            panel = ctx.build_log_panel()
            log_slot = ft.Container(content=panel, expand=True, visible=False)
        stack = ft.Column([c for c in (content_slot, log_slot) if c is not None],
                          spacing=8, expand=True, visible=False)
        slots_col.controls.append(stack)
        content_slots[sub_key] = content_slot
        if log_slot is not None:
            log_slots[sub_key] = log_slot
        built[sub_key] = {"ctx": ctx, "tab": tab, "sub": sub, "stack": stack,
                          "content_slot": content_slot, "log_slot": log_slot}
        _safe_update(slots_col)
        return built[sub_key]

    def _deactivate(sub_key: str, *, leaving: bool = False) -> None:
        entry = built.get(sub_key)
        if not entry:
            return
        tab = entry["tab"]
        if hasattr(tab, "save_state"):
            try:
                state.tool_state[sub_key] = tab.save_state()
            except Exception:  # noqa: BLE001
                pass
        # 40 号新语义：切 tab / 离页**一律只解绑 UI**（on_detach），任务由
        # registry 持有（原"keep_alive 才跨 tab、离页必停"口径作废；
        # Sub.keep_alive 字段保留兼容但不再分支）。
        if hasattr(tab, "on_detach"):
            try:
                tab.on_detach()
            except Exception as ex:  # noqa: BLE001
                entry["ctx"].append_log_to_panel(f"[界面] on_detach 异常: {ex}")
        elif hasattr(tab, "on_hide"):
            try:
                tab.on_hide()
            except Exception as ex:  # noqa: BLE001
                entry["ctx"].append_log_to_panel(f"[界面] on_hide 异常: {ex}")
        counters["hide_total"] += 1
        counters["hide"][sub_key] = counters["hide"].get(sub_key, 0) + 1

    def _activate(sub_key: str) -> None:
        entry = _ensure(sub_key)
        for k, e in built.items():
            active = (k == sub_key)
            e["stack"].visible = active
            e["content_slot"].visible = active
            if e["log_slot"] is not None:
                e["log_slot"].visible = active          # 非激活 tab 的面板不可见（#8）
        if hasattr(entry["tab"], "on_show"):
            try:
                entry["tab"].on_show()
            except Exception as ex:  # noqa: BLE001
                entry["ctx"].append_log_to_panel(f"[界面] on_show 异常: {ex}")
        _safe_update(slots_col)

    def _switch(sub_key: str) -> None:
        nonlocal active_key
        if sub_key == active_key or sub_key not in by_key:
            return
        _deactivate(active_key)
        active_key = sub_key
        _rebuild_chips()
        _activate(sub_key)

    def _rebuild_chips() -> None:
        chips_row.controls = [
            components.chip(state.theme, s.label,
                            on_click=(lambda e, k=s.key: _switch(k)),
                            active=(s.key == active_key))
            for s in subs_n
        ]
        _safe_update(chips_row)

    def _save_all() -> None:
        for k in list(built):                            # 所有已建 tab 存态 + 停后台
            _deactivate(k, leaving=True)
        state.tool_state[base_key] = {"_active": active_key}

    state.before_page_leave = _save_all

    _rebuild_chips()
    _activate(active_key)

    rows: list[ft.Control] = [chips_row, slots_col]
    if with_status:
        rows.append(ft.Container(
            content=ft.Row([status_text], spacing=8),
            padding=ft.padding.Padding(left=8, top=4, bottom=4, right=8)))
    control = ft.Column(rows, spacing=8, expand=True)
    # 测试钩子（29 号实施计划约定）：无 UI 也能断言切换/可见性/生命周期。
    # slots / log_slots 传**同一字典对象**（后建的 tab 也会出现在里面），counters 同理。
    control._merged = {
        "switch": _switch,
        "active": lambda: active_key,
        "slots": content_slots,
        "log_slots": log_slots,
        "counters": counters,
    }
    return control


def build_logcat(page: ft.Page, state: AppState) -> ft.Control:
    """日志终端（合并页：日志终端 | 时间戳转换）

    2026-09-21 用户反馈：原「带子工具的页」把子工具叠在主体**上方**（固定 300px
    限高 + 内部滚动），开子工具即挤压主体与事件窗口（截图实证：日志窗只剩一线）。
    改为与「应用与传输 / 屏幕与触控」同形的合并页——两胶囊平级、内容替换、默认
    日志终端；tab 懒构建、建成常驻、切换只切可见性（见 ``28-日志终端合并页改造设计.md``）。

    子键保持 ``logcat`` / ``timestamp``：输入态与 ``timestamp_config.json`` 无缝延续
    （旧键 ``logcat_sub`` 失效，自动回落日志终端 tab）。

    日志面板：LogTab **自带**（故 ``with_log=False``——外挂会双挂载，发现 #1）。
    切 tab **不停** Logcat 抓取（``keep_alive=True``）；离开本页统一 ``on_hide``。

    变更（2026-09-21）：**离线提取迁出本页** → 侧栏「离线提取」独立页
    （依赖 CbmLogextractor 与本项目日志格式；见 :func:`build_offline`）。
    """
    return _wrap_merged(
        page, state, "logtools",
        subs=[
            ("logcat", "日志终端", lambda ctx: LogTab(ctx), False, True),
            ("timestamp", "时间戳转换", lambda ctx: TimestampTab(ctx), False),
        ],
        with_status=True, with_log=False)


class _OfflinePage:
    """离线提取独立页适配（提取面板 + 窗口内报告视图；供 :func:`_wrap` 使用）

    2026-09-21 用户裁决：① 报告要「在窗口内显示」（不再只是写文件 + 打开报告）；
    ② 离线提取依赖 CbmLogextractor 与本项目 CBM/BDS 日志格式，从日志终端
    胶囊页迁到侧栏独立页。
    """

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.view = ReportView(ctx)
        self.panel = OfflineExtractPanel(ctx, on_report=self._on_report)

    def _on_report(self, path: str) -> None:
        """提取完成 → 报告自动载入窗口视图"""
        self.view.load(path)

    def build(self) -> ft.Control:
        return ft.Column([
            self.panel.build(),
            ft.Divider(height=1),
            self.view.build(),
        ], spacing=8, expand=True)

    def on_show(self) -> None:
        """进页即尝试载入最新报告（无报告时静默，视图自带空态提示）"""
        if not self.view._path:                       # noqa: SLF001
            self.view.load_latest(notify=False)

    def on_hide(self) -> None:
        pass

    def save_state(self) -> dict:
        return {}

    def restore_state(self, state: dict) -> None:
        pass


def build_offline(page: ft.Page, state: AppState) -> ft.Control:
    """离线提取（单页）：提取区 + 报告视图

    2026-10-12 用户裁决：**去掉底部终端/步骤窗口**（``with_log=False``）——本页要展示
    报告表格与原始提取数据，底部事件窗口把纵向空间吃掉一半。事件仍进内存日志模型
    （``append_log_to_panel`` 有 ``UI_LOG_MAX_LINES`` 上限），只是不再占版面。
    """
    ctx = ToolContext(page, state, "offline")
    return _wrap(_OfflinePage(ctx), state, "offline", ctx,
                 with_status=True, with_log=False)



def build_screen(page: ft.Page, state: AppState) -> ft.Control:
    """屏幕与触控（合并页：触控映射 + PC 录屏）"""
    return _wrap_merged(
        page, state, "screen",
        subs=[
            ("touch", "触控映射", lambda ctx: TouchTab(ctx)),
            ("recorder", "PC 录屏", lambda ctx: RecorderTab(ctx)),
        ],
        with_status=True, with_log=False)


def build_apptrans(page: ft.Page, state: AppState) -> ft.Control:
    """应用与传输（合并页：APK 管理 + 大文件推送）

    本页是设备写操作（装/卸 APK、分块推送），必须有操作输出窗口。
    2026-10-12 用户裁决：输出窗口**上移并变大**——内容只占自身高度，输出面板从
    内容下方铺到底（此前固定 170px 贴底，内容与面板之间留一大块空白）。
    """
    return _wrap_merged(
        page, state, "apptrans",
        subs=[
            ("apk", "APK 管理", lambda ctx: ApkTab(ctx)),
            ("push", "大文件推送", lambda ctx: PushTab(ctx)),
        ],
        with_status=True, with_log=True)


def build_monkey(page: ft.Page, state: AppState) -> ft.Control:
    """Monkey 压测（部署 / 一键流 / 状态 / 停止 / 拉取分析 / 报告）

    2026-10-12 用户裁决：**报告并入日志窗口**——原「报告（近 10 份）」卡片删除，
    报告路径写进底部事件窗口（点「报告列表」列一次，拉取+分析后自动列）。
    本页自此挂标准事件窗口（内容自然高度 + 窗口铺满剩余，与「应用与传输」同形）。
    """
    ctx = ToolContext(page, state, "monkey")
    return _wrap(MonkeyTab(ctx), state, "monkey", ctx,
                 with_status=True, with_log=True)


def build_logextract(page: ft.Page, state: AppState) -> ft.Control:
    """时间段提取（按时间范围提取/切割车载日志；``36`` 号文档）

    核心库化自 ``extract_logs.py``（Script\\car_logs，原脚本保留）。
    """
    ctx = ToolContext(page, state, "logextract")
    return _wrap(LogExtractTab(ctx), state, "logextract", ctx,
                 with_status=True, with_log=False)   # 2026-10-12：底部窗口只留日志终端页


def build_single_inject(page: ft.Page, state: AppState) -> ft.Control:
    """单条灌入（快灌）：一句话 → 灌入 → 结果卡片（``30`` 号文档 §六 第 2 项）

    来源 ``EOV_Voice_Tool`` 的日常用法；内核复用 ``core.Injector``。
    """
    ctx = ToolContext(page, state, "inject")
    return _wrap(SingleInjectTab(ctx), state, "inject", ctx,
                 with_status=True, with_log=False)   # 2026-10-12：底部窗口只留日志终端页


#: 侧栏 key → 页面构建函数
BUILDERS: dict = {
    "tool_logcat": build_logcat,
    "tool_apptrans": build_apptrans,
    "tool_screen": build_screen,
    "tool_monkey": build_monkey,
    "tool_logextract": build_logextract,
    "tool_inject": build_single_inject,
    "tool_offline": build_offline,
}
