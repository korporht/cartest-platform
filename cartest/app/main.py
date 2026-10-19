"""CarTest app — 双栏工作台（应用入口）

界面结构（见 14-UI重构设计.md §5；2026-09-16 用户反馈修订）：
    顶栏：品牌 · 设备胶囊 · 运行胶囊 · 主题切换
    侧栏：配置（顶部）+ 导航（「通用工具」单组；见 18-工具箱与导航重构设计.md）
    内容区：侧栏直切（无标签栏、无底部状态栏）

启动：``python -m cartest.app``
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import flet as ft

from ..core import project as project_mod
from ..pipeline.context import stage_label
from . import pages
from .state import AppState
from .tools.context import _safe_on_main
from .ui import activity_panel
from .ui import components
from .ui import task_panel
from .ui import toast as toast_ui

APP_NAME = "CarTest 车机测试平台"
APP_VERSION = "0.1.0"

#: 本模块注册的项目变更监听器（防 web 多会话重复注册累积；桌面单会话即唯一）
_SESSION_LISTENERS: list = []


def _register_project_listener(handler) -> None:
    """注册项目变更监听器（先清掉本模块旧注册——每会话重建 handler）"""
    for h in _SESSION_LISTENERS:
        project_mod.remove_active_listener(h)
    _SESSION_LISTENERS.clear()
    _SESSION_LISTENERS.append(handler)
    project_mod.add_active_listener(handler)

#: 侧栏导航（**单组「通用工具」**）
#: 2026-10-12 用户裁决：删除「VR 日志采集」（整页移除）；「离线提取 / 单条灌入」
#: 并入通用工具；取消「项目定制」分组。
#: 2026-10-19 用户裁决：删除「测试流程」页（三视图壳整页移除，见 pages/__init__.py）。
PAGE_GROUPS: list[tuple[str, list[tuple]]] = [
    ("通用工具", [
        ("tool_logcat", ft.Icons.TERMINAL, "日志终端"),
        ("tool_apptrans", ft.Icons.SWAP_HORIZ, "应用与传输"),
        ("tool_screen", ft.Icons.SCREEN_ROTATION_ALT, "屏幕与触控"),
        ("tool_monkey", ft.Icons.PEST_CONTROL, "Monkey 压测"),
        ("tool_logextract", ft.Icons.SCHEDULE, "日志切割"),
        ("tool_offline", ft.Icons.MANAGE_SEARCH, "对话还原"),
        ("tool_inject", ft.Icons.SEND, "语音灌入"),
    ]),
]

SETTINGS_KEY = "settings"
SETTINGS_ICON = ft.Icons.SETTINGS
SETTINGS_LABEL = "配置"

#: key → (图标, 标题)
PAGE_META: dict[str, tuple] = {
    k: (icon, label)
    for _, entries in PAGE_GROUPS for (k, icon, label) in entries
}
PAGE_META[SETTINGS_KEY] = (SETTINGS_ICON, SETTINGS_LABEL)


def _run_chip_value(state: AppState) -> str:
    """顶栏运行胶囊文本（无运行中内容 → 空串）

    40 号：聚合**全局任务**（registry：运行中 + 排队中）与**流水线**状态——
    工具任务跨页存活后，顶栏是唯一常驻的"还有什么在跑"指示。
    """
    parts: list[str] = []
    reg = getattr(state, "tasks", None)
    if reg is not None:
        try:
            queued: list[str] = []
            for h in reg.snapshot():
                if h.get("queued"):
                    queued.append(str(h.get("label") or h.get("key") or ""))
                elif h.get("running"):
                    text = f"{h.get('label') or h.get('key')} {h.get('detail') or ''}".strip()
                    parts.append(text)
            if queued:                          # 40 号 §4.3：⏸ 排队中 N（队首名字）
                parts.append(f"⏸ 排队中 {len(queued)}（{queued[0]}）")
        except Exception:  # noqa: BLE001 — 指示器失败不影响主循环
            pass
    if state.running:
        stage = next((s for s in state.stage_status
                      if state.stage_status[s] == "running"), "")
        detail = state.stage_detail.get(stage, "") if stage else ""
        parts.append(f"{stage_label(stage)} {detail}".strip() if stage else "启动中…")
    if not parts:
        return ""
    return "◐ " + " · ".join(parts)


def main(page: ft.Page, *, state: AppState | None = None) -> None:
    state = state or AppState()
    state.log.append("GUI 会话已连接", "info")

    # 32 号：旧版物化的 data/projects/e0v.json 让位给「默认（内置）」（幂等迁移）
    try:
        project_mod.migrate_builtin_file()
    except Exception:  # noqa: BLE001 - 迁移失败不影响启动
        pass

    page.title = f"{APP_NAME} v{APP_VERSION}"
    page.window.width = 1400
    page.window.height = 900
    page.window.min_width = 1100
    page.window.min_height = 700
    page.padding = 0
    page.spacing = 0

    content_holder = ft.Container(expand=True, padding=3)  # 主体内容 3px 内边距（不贴边）
    sidebar_holder = ft.Container(width=190)
    topbar_holder = ft.Container()

    # ── 顶栏（常驻控件：tick 只改值，壳重建只改色）──
    device_dot = ft.Text("●", size=9, color=state.theme.c("success"))
    device_label = ft.Text("设备: 未连接", size=11)
    device_chip = ft.Container(
        content=ft.Row([
            device_dot, device_label,
            ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=14),
        ], spacing=4),
        padding=ft.padding.Padding(left=10, right=8, top=1, bottom=1),
        border_radius=999,
        tooltip="点击切换设备（全局生效）",
        on_click=lambda e: _open_device_dialog(),
    )
    run_chip_text = ft.Text("", size=11)
    run_chip = ft.Container(
        content=ft.Row([run_chip_text], spacing=0),
        padding=ft.padding.Padding(left=10, right=10, top=2, bottom=2),
        border_radius=999,
        visible=False,
        tooltip="点击查看 / 停止正在运行的任务",
        on_click=lambda e: _open_tasks(),
    )

    def _open_activity() -> None:
        """打开「操作日志」抽屉（顶栏入口）

        经 ``state.open_activity`` 转发（由 :func:`activity_panel.attach` 注册）——
        顶栏会因主题切换整体重建，直接闭包引用 panel 会失效。
        """
        cb = getattr(state, "open_activity", None)
        if cb is not None:
            cb()

    def _open_tasks() -> None:
        """打开「任务」抽屉（顶栏运行胶囊入口；由 task_panel.attach 注册）"""
        cb = getattr(state, "open_tasks", None)
        if cb is not None:
            cb()

    def _build_topbar() -> ft.Container:
        t = state.theme
        # UI-5：图标随主题（深色显示 ☀ 切浅色；浅色显示 ☾ 切深色）
        theme_icon = ft.Icons.LIGHT_MODE if state.theme.is_dark else ft.Icons.DARK_MODE
        theme_tip = "切到浅色" if state.theme.is_dark else "切到深色"
        device_label.color = t.c("text_secondary")
        device_chip.border = ft.border.Border.all(1, t.c("border"))
        run_chip.bgcolor = t.c("sel_bg")
        run_chip_text.color = t.c("accent_text")
        _sync_device_ui()                       # 设备点按在线状态着色（替代恒绿）
        return ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.DIRECTIONS_CAR, size=18, color=t.c("accent")),
                ft.Text("CarTest", size=14, weight=ft.FontWeight.BOLD,
                        color=t.c("text_primary")),
                ft.Container(width=12),
                device_chip,
                run_chip,
                ft.Container(expand=True),
                ft.IconButton(ft.Icons.HISTORY, tooltip="操作日志",
                              icon_size=16, icon_color=t.c("text_secondary"),
                              on_click=lambda e: _open_activity()),
                ft.IconButton(theme_icon, tooltip=theme_tip, icon_size=16,
                              icon_color=t.c("text_secondary"),
                              on_click=lambda e: _toggle_theme()),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.Padding(left=14, right=14, top=6, bottom=6),
            height=46,
            bgcolor=t.c("bg_chrome"),
            border=ft.border.Border.only(bottom=ft.border.BorderSide(1, t.c("border"))),
        )

    # ── 侧栏 ──
    def _rebuild_sidebar() -> None:
        t = state.theme
        active = state.active_page
        # 固定区：配置置顶（不随分组滚动；≤800px 高窗口仍可见——UI-5）
        fixed: list[ft.Control] = [
            ft.Container(height=4),
            components.nav_button(
                t, SETTINGS_ICON, SETTINGS_LABEL, active == SETTINGS_KEY,
                lambda e: _open_page(SETTINGS_KEY)),
            ft.Container(height=4),
            ft.Divider(height=1, color=t.c("divider")),
            ft.Container(height=4),
        ]
        # 滚动区：分组导航
        groups: list[ft.Control] = []
        for group, entries in PAGE_GROUPS:
            groups.append(ft.Text(f"  {group}", size=10, color=t.c("text_hint")))
            groups.append(ft.Container(height=2))
            for key, icon, label in entries:
                groups.append(components.nav_button(
                    t, icon, label, key == active,
                    lambda e, k=key: _open_page(k)))
            groups.append(ft.Container(height=6))
        sidebar_holder.content = ft.Container(
            content=ft.Column([
                ft.Column(fixed, spacing=3),
                ft.Container(
                    content=ft.Column(groups, spacing=3, scroll=ft.ScrollMode.AUTO),
                    expand=True),
            ], spacing=0, expand=True),
            bgcolor=t.c("bg_sidebar"), expand=True)
        _safe_update(sidebar_holder)

    # ── 页面直切 ──
    def _detach_page() -> None:
        """离开页面前：保存输入态 + 清空页面注册的回调（防陈旧闭包驱动已卸载控件树）"""
        if state.before_page_leave:
            try:
                state.before_page_leave()
            except Exception:  # noqa: BLE001
                pass
            state.before_page_leave = None
        state.log_target = None
        state.on_running_change = None
        state.tick_hooks = []
        state.goto_page = None

    def _render_content() -> None:
        key = state.active_page
        builder = pages.BUILDERS.get(key)
        content_holder.content = (builder(page, state) if builder
                                  else ft.Text("页面未实现",
                                               color=state.theme.c("text_secondary")))
        _safe_update(content_holder)

    def _open_page(key: str) -> None:
        if key not in pages.BUILDERS or key == state.active_page:
            return
        _detach_page()
        state.active_page = key
        _rebuild_sidebar()
        _render_content()

    state.goto_page = _open_page
    state.refresh_page = _render_content      # 配置页切方案后重载当前页（32 号）

    # ── 设备（全局切换：顶栏胶囊为唯一入口）──
    def _device_label_text(dev: str) -> str:
        """顶栏设备文本：``设备: 别名 — 序列号（离线）``"""
        if not dev:
            return "设备: 未连接"
        alias = ""
        try:
            alias = project_mod.alias_for_serial(dev)
        except Exception:  # noqa: BLE001 - 别名失败不影响显示
            pass
        text = f"{alias} — {dev}" if alias else dev
        if dev not in state.devices:
            text += "（离线）"
        return f"设备: {text}"

    def _sync_device_ui() -> None:
        """顶栏设备胶囊按在线状态着色 + 文本（可在任意线程调用，内部回主线程）"""
        def _do() -> None:
            t = state.theme
            dev = state.device
            if not dev:
                device_dot.color = t.c("danger")
            elif dev not in state.devices:
                device_dot.color = t.c("warning")   # 离线（保留所选，不静默换设备）
            else:
                device_dot.color = t.c("success")
            device_label.value = _device_label_text(dev)
            device_label.color = t.c("text_secondary")
            _safe_update(device_dot)
            _safe_update(device_label)

        _safe_on_main(page, _do)

    def _snack(text: str, ok: bool = True) -> None:
        """轻提示（顶部浮层 Toast；未装配时回退 SnackBar）"""
        toast_ui.toast(page, text, ok, state.theme)

    def _auto_scheme_for_device(dev: str) -> None:
        """设备（切换/连接）→ 有绑定方案且与当前不同则自动切换配置方案

        2026-10-19 用户诉求「配置自定义绑定设备，连接/切换到对应设备时能自动切换配置」。
        防环：``set_active`` 的 listener（``_on_project_changed``）会尝试联动设备，
        但当前设备已属于该方案时 listener 直接返回。
        """
        if not dev:
            return
        try:
            bound = project_mod.scheme_for_device(dev)
        except Exception:  # noqa: BLE001 - 绑定查询失败不影响设备切换
            return
        if not bound or bound == project_mod.active_key():
            return
        if not project_mod.set_active(bound):
            return
        name = bound
        try:
            name = next((p["name"] for p in project_mod.list_schemes()
                         if p["key"] == bound), bound)
        except Exception:  # noqa: BLE001
            pass
        state.log.append(f"已自动切换到方案「{name}」（设备 {dev} 绑定）", "info")
        _snack(f"设备 {dev} 已绑定方案「{name}」，已自动切换")
        try:
            state.activity.add(source="设备",
                               action=f"设备 {dev} → 自动切换方案「{name}」",
                               ok=True)
        except Exception:  # noqa: BLE001
            pass
        # 配置页正在显示 → 值整体重载（否则仍显示旧方案的值）
        if state.active_page == SETTINGS_KEY:
            hook = getattr(state, "refresh_page", None)
            if callable(hook):
                hook()

    def _on_devices_polled(cur: list[str]) -> None:
        """设备轮询结果（主线程）：顶栏同步 + 新接入设备自动接管

        只在「当前无设备 / 当前设备离线」时自动选中——当前设备在线时不抢焦点
        （用户可能正在用另一台设备跑任务）。
        """
        now = set(cur or [])
        prev = state.polled_devices
        state.polled_devices = now
        _sync_device_ui()
        new = now - prev
        if not new:
            return
        if state.device and state.device in now:
            return
        ok, _why = state.can_switch_device()
        if not ok:
            return
        for dev in sorted(new):
            try:
                bound = project_mod.scheme_for_device(dev)
            except Exception:  # noqa: BLE001
                continue
            if not bound:
                continue
            if state.set_device(dev):
                state.log.append(
                    f"检测到设备接入：{dev}（已绑定方案），已自动选中", "info")
                _auto_scheme_for_device(dev)
            break

    def _device_poll_loop() -> None:
        """后台轮询设备接入（5s；adb 调用可能慢，不能在 UI 线程做）"""
        while True:
            time.sleep(5.0)
            try:
                if state.running:
                    continue
                cur = state.refresh_devices()
            except Exception:  # noqa: BLE001
                continue
            _safe_on_main(page, lambda c=list(cur): _on_devices_polled(c))

    def _pick_device(dev: str) -> None:
        page.pop_dialog()
        if dev == state.device:
            return
        ok, reason = state.can_switch_device()
        if ok:
            if state.set_device(dev):
                state.log.append(
                    f"已切换设备: {_device_label_text(dev).removeprefix('设备: ')}", "info")
                _auto_scheme_for_device(dev)      # 2026-10-19：设备绑定方案 → 自动切
            return
        # 40 号 §六：不再默默拒绝——弹对话框，**首选「停止任务并切换」**
        def _do_stop_switch() -> None:
            """停止全部占用设备的任务并切换（停止是异步的，必须等真正落停）"""
            state.log.append(f"正在停止任务（{reason}）…", "warn")
            if not state.stop_all_tasks(timeout=10.0):
                _snack("停止超时，请手动停止后重试", ok=False)
                state.log.append("停止任务超时，设备未切换", "error")
                return
            queue = getattr(state, "device_queue", None)
            if queue is not None:
                for h in list(queue.pending()):   # 排队项目标设备即将失效 → 一并取消
                    queue.cancel(h.key)
            if state.set_device(dev):
                label = _device_label_text(dev).removeprefix("设备: ")
                # 40 号 §6 状态记录：切换动作写全局操作日志（任务明细已在 registry 历史）
                try:
                    state.activity.add(source="设备",
                                       action=f"已停止运行中任务并切换设备 → {label}",
                                       ok=True)
                except Exception:  # noqa: BLE001
                    pass
                state.log.append("任务已停止，设备已切换", "info")
                _snack("任务已停止，设备已切换")

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("任务运行中", size=14,
                          color=state.theme.c("text_primary")),
            content=ft.Text(f"当前有任务运行中（{reason}）。\n"
                            "切换设备需要先停止它们，停止期间请勿操作设备。",
                            size=12, color=state.theme.c("text_secondary")),
            actions=[
                ft.FilledButton("停止任务并切换",
                                on_click=lambda e: (
                                    page.pop_dialog(),
                                    threading.Thread(target=_do_stop_switch,
                                                     daemon=True).start())),
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
            ]))

    def _open_device_dialog(e=None) -> None:
        """设备选择对话框（打开时实时刷新列表；运行中选设备 → 走「停止任务并切换」）"""
        ok, reason = state.can_switch_device()
        if not ok:
            _snack(f"运行中（{reason}），暂不能切换设备", ok=False)
            return
        state.refresh_devices()
        current = state.device
        items = list(state.devices)
        if current and current not in items:
            items.append(current)          # 当前离线：保留展示（标注在文本里）
        rows: list[ft.Control] = []
        if items:
            for d in items:
                chosen = d == current
                rows.append(ft.TextButton(
                    ("✓ " if chosen else "") +
                    _device_label_text(d).removeprefix("设备: "),
                    on_click=lambda e, d=d: _pick_device(d)))
        else:
            rows.append(ft.Text("未发现在线设备（请检查 USB / 网络连接）",
                                size=12, color=state.theme.c("text_secondary")))
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("选择设备（全局生效）", size=14,
                          color=state.theme.c("text_primary")),
            content=ft.Column(rows, spacing=2, tight=True, width=320),
            actions=[
                ft.TextButton("刷新", on_click=lambda e: (
                    page.pop_dialog(), _open_device_dialog())),
                ft.TextButton("关闭", on_click=lambda e: page.pop_dialog()),
            ]))

    def _restore_device() -> None:
        """启动恢复链：上次选择（在线才用）→ 当前项目档案 serial（在线才用）→ 第一台"""
        devices = state.refresh_devices()
        if not devices:
            _sync_device_ui()
            return
        from ..core import config_center as _cc
        selected = ""
        try:
            selected = str(_cc.get("device.selected", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        serial = ""
        try:
            serial = str(project_mod.get("device.serial", "") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        target = (selected if selected in devices
                  else serial if serial in devices else devices[0])
        state.set_device(target)
        _sync_device_ui()
        _auto_scheme_for_device(target)           # 2026-10-19：启动恢复后按绑定切方案

    def _apply_project_device(serial: str) -> None:
        """随项目切换设备（主线程；离线也选中并标记——诚实不静默回退）"""
        if state.device == serial:
            _sync_device_ui()
            return
        if state.set_device(serial):
            state.log.append(
                f"已随项目切换设备: {_device_label_text(serial).removeprefix('设备: ')}",
                "info")
        else:
            _sync_device_ui()              # 运行中被拒：set_device 已记日志

    def _on_project_changed(key: str) -> None:
        """项目切换监听（可能来自配置页工作线程）→ 读档案 serial → 联动设备"""
        try:
            serial = str(project_mod.get("device.serial", "") or "").strip()
        except Exception:  # noqa: BLE001
            serial = ""
        if not serial:
            return
        # 2026-10-19：方案可绑多台设备——当前设备若也属于该方案，不抢切
        # （否则 A 设备触发切方案时会被档案默认设备 B 抢走）
        try:
            if state.device and state.device != serial \
                    and state.device in project_mod.scheme_devices(key):
                return
        except Exception:  # noqa: BLE001
            pass
        _safe_on_main(page, lambda: _apply_project_device(serial))

    state.on_device_change = lambda dev: _sync_device_ui()

    def _toggle_theme() -> None:
        state.theme.toggle()
        page.theme = state.theme.flet_theme()
        page.theme_mode = state.theme.theme_mode
        topbar_holder.content = _build_topbar()
        _safe_update(topbar_holder)
        _rebuild_sidebar()
        _detach_page()
        _render_content()
        try:  # page.update() 才下发布局级 theme/theme_mode（部分 Material 控件会残留旧色）
            page.update()
        except Exception:  # noqa: BLE001
            pass

    # ── 组装 ──
    page.theme = state.theme.flet_theme()
    page.theme_mode = state.theme.theme_mode
    topbar_holder.content = _build_topbar()

    # 根 Stack：主布局 + 浮层（Toast / 抽屉）为定位兄弟——**不挂 page.overlay**
    # （flet 0.85 Web 端 overlay 子树 toggling visible 后不重绘，真机点验 2026-10-14）
    root_stack = ft.Stack([
        ft.Column([
            topbar_holder,
            ft.Container(
                content=ft.Row([
                    sidebar_holder,
                    ft.VerticalDivider(width=1),
                    content_holder,
                ], spacing=0, expand=True),
                expand=True,
            ),
        ], spacing=0, expand=True),
    ], expand=True)
    page.add(root_stack)

    # ── 浮层（Toast / 全局操作日志抽屉 / 任务抽屉）挂主树 Stack 顶层 ──
    toast_ui.attach(page, state.theme, host=root_stack.controls)
    activity_panel.attach(page, state, host=root_stack.controls)
    task_panel.attach(page, state, host=root_stack.controls)   # 40 号：胶囊点开

    # ── UI 对接回调（tick 循环使用，避免模块级全局变量）──
    state.run_chip_target = lambda txt: (
        setattr(run_chip_text, "value", txt),
        setattr(run_chip, "visible", bool(txt)),
        _safe_update(run_chip))

    _rebuild_sidebar()
    _restore_device()
    _register_project_listener(_on_project_changed)
    _render_content()

    # 2026-10-19：设备接入轮询（新设备有绑定方案 → 自动选中 + 切方案）
    threading.Thread(target=_device_poll_loop, daemon=True).start()

    # ── 后台刷新循环（日志 + 运行胶囊 + 运行状态联动）──
    async def _tick() -> None:
        ticks = 0
        while True:
            await asyncio.sleep(0.25)
            ticks += 1
            lines = state.log.drain()
            if lines and state.log_target:
                state.log_target(lines)
            chip = _run_chip_value(state)
            if chip != state.last_run_chip:
                state.last_run_chip = chip
                if state.run_chip_target:
                    state.run_chip_target(chip)
            for hook in state.tick_hooks:
                try:
                    hook(ticks)
                except Exception:  # noqa: BLE001 — 动效失败不影响主循环
                    pass
            for hook in getattr(state, "ui_hooks", []):   # 应用级常驻浮层（40 号）
                try:
                    hook(ticks)
                except Exception:  # noqa: BLE001
                    pass
            if state.running != state.last_running:
                state.last_running = state.running
                if state.on_running_change:
                    state.on_running_change(state.running)

    page.run_task(_tick)


def _safe_update(ctl) -> None:
    try:
        ctl.update()
    except Exception:  # noqa: BLE001
        pass


def run_app(*, web: bool = False, port: int = 8560,
            state_factory=AppState) -> None:
    """启动 GUI（保持与旧版一致；Web 模式常驻修复逻辑不变）"""

    def _main(page: ft.Page) -> None:
        main(page, state=state_factory())

    if web:
        os.environ.setdefault("FLET_FORCE_WEB_SERVER", "1")
        ft.run(_main, port=port, view=ft.AppView.FLET_APP_WEB)
    else:
        ft.run(_main)
