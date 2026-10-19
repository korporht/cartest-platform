# -*- coding: utf-8 -*-
"""CarTest app — 配置页（schema 驱动自动渲染）

P3c-1：遍历 ``config_center.SCHEMA`` 渲染分组卡片；``path_list`` → ``PathListEditor``；
保存（先全量校验再写盘）/ 恢复默认 / 状态行反馈。
P3d-3：保存与恢复默认改为「加载缓冲（按钮转圈禁用）→ 落盘 → 轻提示」；
校验失败不进入加载（行内明细 + 失败提示）。（见 12-P3d设计.md §4.6）
2026-10-12：轻提示由底部 ``SnackBar`` 改为顶部浮层 Toast（``ui/toast.py``）。
**2026-10-15 用户裁决：取消「保存」按钮，改动即时落盘**（原 P3d-3 的显式保存语义作废）——
「点击配置开关就实时保存」：开关/下拉改动**立即**写盘；文本框/关键词/路径表停止输入
``_TEXT_SETTLE_SEC`` 秒后写盘（防每键写盘）；校验失败**不落盘**+行内红字（保留上次有效值）；
离开页面前 ``flush()`` 强制落盘未结算的改动。「恢复默认」按钮保留（仍走加载缓冲）。
（bool/text/int 渲染器预留：用到再加，避免过度设计——见 08-P3c设计.md 攻击 #2）
"""
from __future__ import annotations

import time
from typing import Any, Callable

import flet as ft

from ...core import config_center
from ...core import project as project_mod
from ..state import AppState
from ..tools.widgets.keywords_editor import KeywordsEditor
from ..tools.widgets.path_list_editor import PathListEditor
from ..ui import toast as toast_ui
from ..ui.components import sync_text_value

#: 成功路径最短加载缓冲（毫秒）——保证「保存中…」可被感知（「恢复默认」仍在用）
_MIN_BUSY_MS = 350

#: 文本框类改动的结算时间（秒）——停手这么久才落盘，避免每个键都写盘
_TEXT_SETTLE_SEC = 0.5


class _AutoSaver:
    """配置页实时保存器（2026-10-15：取消「保存」按钮，改动即时落盘）

    设计：**轮询**编辑器当前值而非给每个编辑器挂回调（编辑器共 6 种，逐个改造
    侵入大且易漏）。每 tick 调 :meth:`poll`：

      · 值与「上次落盘值」相同 → 忽略
      · 不同且**通过校验** → 开关/下拉立即落盘；文本类等**停手** ``settle`` 秒后落盘
        （每次改动重新计时 = 防抖，连续打字只在停手时写一次）
      · 不同但**校验失败** → 不落盘（保留上次有效值），回调报错，下次 tick 继续试

    :meth:`flush` 供离开页面前调用（未结算的改动立即落盘，不丢）。
    """

    #: 「立即落盘」的编辑器类型（开关/下拉：点一下就该生效）
    INSTANT_KINDS = ("bool", "choice")

    def __init__(self, editors: dict, kinds: dict, commit: Callable, on_event: Callable,
                 *, settle: float | None = None, now: Callable = time.time) -> None:
        self._editors = editors
        self._kinds = kinds
        self._commit = commit              # (key, value) -> (ok, msg)
        self._on_event = on_event          # (key, ok, msg, err) -> None（UI 反馈）
        self._settle = _TEXT_SETTLE_SEC if settle is None else settle
        self._now = now
        self._observed: dict[str, Any] = {k: self._value(k) for k in editors}
        self._saved: dict[str, Any] = dict(self._observed)
        self._dirty_at: dict[str, float] = {}
        self._instant: set[str] = set()    # 点击类改动（开关/增删行）→ 下次 poll 立即落盘

    # ── 内部 ──
    def _value(self, key: str):
        ed = self._editors.get(key)
        if ed is None:
            return None
        try:
            return ed.get_value()
        except Exception:  # noqa: BLE001 - 编辑器取值失败视为无变化
            return None

    def _delay(self, key: str) -> float:
        return 0.0 if self._kinds.get(key) in self.INSTANT_KINDS else self._settle

    def _push(self, key: str, instant: bool = False) -> None:
        """把某个键的当前值落盘"""
        ed = self._editors.get(key)
        if ed is None:
            return
        value = self._value(key)
        err = ""
        try:
            err = ed.validate() or ""
        except Exception as exc:  # noqa: BLE001
            err = f"校验异常：{exc}"
        if err:
            self._on_event(key, False, "", err, instant)   # 留着 dirty：下次 tick 再试
            return
        ok, msg = self._commit(key, value)
        if ok:
            self._saved[key] = value
            self._dirty_at.pop(key, None)
        self._on_event(key, ok, msg, "", instant)

    # ── 对外 ──
    def poll(self) -> None:
        """tick 调用：检出改动并（按类型延迟）落盘"""
        now = self._now()
        for key in list(self._editors):
            if key == "project.active":       # 方案切换由下拉 on_select 即时处理
                continue
            cur = self._value(key)
            if cur != self._observed.get(key):
                self._observed[key] = cur
                self._dirty_at[key] = now     # 每次改动重新计时（防抖）
            if key in self._instant:          # 点击类改动：不等防抖
                self._instant.discard(key)
                self._dirty_at.pop(key, None)
                if cur != self._saved.get(key):
                    self._push(key, instant=True)
                continue
            if cur == self._saved.get(key):
                self._dirty_at.pop(key, None)  # 改回已保存值 → 无需落盘
                continue
            t0 = self._dirty_at.get(key)
            if t0 is not None and now - t0 >= self._delay(key):
                self._push(key)

    def flush(self) -> None:
        """离开页面前：未结算的改动立即落盘"""
        for key in list(self._dirty_at):
            if self._value(key) == self._saved.get(key):
                self._dirty_at.pop(key, None)
                continue
            self._push(key)

    def mark_instant(self, key: str) -> None:
        """标记「点击类」改动（开关 / 增删行 / 加删标签）→ 下一次 poll 立即落盘，不等防抖"""
        self._instant.add(key)

    def mark_saved(self, key: str, value=None) -> None:
        """外部改过值（如「恢复默认」回显）后同步基线，避免被当成用户改动再存一次"""
        cur = self._value(key) if value is None else value
        self._saved[key] = cur
        self._observed[key] = self._value(key)
        self._dirty_at.pop(key, None)


class _BoolEditor:
    """开关编辑器（``TYPE_BOOL``）"""

    def __init__(self, value: object, theme) -> None:
        self.sw = ft.Switch(value=bool(value), scale=0.9,
                            label="开启" if value else "关闭",
                            label_text_style=ft.TextStyle(
                                size=11, color=theme.c("text_secondary")),
                            active_color=theme.c("accent"),
                            inactive_track_color=theme.c("border"),
                            thumb_color=theme.c("text_on_accent"),
                            on_change=self._on_change)

    def _on_change(self, e) -> None:
        self.sw.label = "开启" if self.sw.value else "关闭"

    def build(self) -> ft.Control:
        return self.sw

    def validate(self) -> str | None:
        return None

    def get_value(self) -> bool:
        return bool(self.sw.value)

    def set_value(self, value: object) -> None:
        self.sw.value = bool(value)
        self.sw.label = "开启" if value else "关闭"


class _TextEditor:
    """单行文本编辑器（``TYPE_TEXT``；留空＝跟随配置方案 / 内置默认）"""

    multiline = False
    is_int = False

    def __init__(self, value: object, theme, *, hint: str = "") -> None:
        self.tf = ft.TextField(
            value="" if value is None else str(value),
            hint_text=hint, dense=True, text_size=11,
            multiline=self.multiline, min_lines=4 if self.multiline else 1,
            border_radius=8, color=theme.c("text_primary"),
            bgcolor=theme.c("bg_panel"), border_color=theme.c("border"),
            hint_style=ft.TextStyle(size=11, color=theme.c("text_hint")),
            on_change=sync_text_value)          # 发现 #14：必须挂 on_change
        if self.multiline:
            self.tf.max_lines = 8

    def build(self) -> ft.Control:
        return self.tf

    def validate(self) -> str | None:
        if self.is_int:
            raw = (self.tf.value or "").strip()
            if raw and not raw.lstrip("-").isdigit():
                return f"「{self.tf.hint_text or '该项'}」需为整数：{raw}"
        return None

    def get_value(self):
        raw = (self.tf.value or "").strip()
        if self.is_int:
            return int(raw) if raw else None
        return raw or None                      # 空 → None（跟随配置方案）

    def set_value(self, value: object) -> None:
        self.tf.value = "" if value is None else str(value)


class _TextAreaEditor(_TextEditor):
    """多行文本编辑器（``TYPE_TEXTAREA``）→ ``list[str]``（一行一词）"""

    multiline = True

    def __init__(self, value: object, theme, *, hint: str = "") -> None:
        # 注意：初值可能是 list（方案里的关键词表）→ 必须走 set_value 转成多行，
        # 否则 str(list) 会把 Python repr 显示到界面上（2026-09-21 真机截图发现）
        super().__init__("", theme, hint=hint)
        self.set_value(value)

    def get_value(self):
        lines = [ln.strip() for ln in (self.tf.value or "").splitlines()]
        return [ln for ln in lines if ln] or None

    def set_value(self, value: object) -> None:
        if isinstance(value, (list, tuple)):
            self.tf.value = "\n".join(str(v) for v in value)
        else:
            self.tf.value = "" if value is None else str(value)


class _IntEditor(_TextEditor):
    """整数编辑器（``TYPE_INT``）"""

    is_int = True


class _ChoiceEditor:
    """单选编辑器（项目选择等；选项来自 resolver）

    ``on_change``（可选）：选中即回调 ``fn(value)``——配置方案切换要**立刻生效并重载页面**。
    注意 flet 0.85 的 ``Dropdown`` 没有 ``on_change``，事件名是 ``on_select``（踩坑 #12）。
    """

    def __init__(self, value: object, options: list[tuple[str, str]], theme,
                 on_change=None) -> None:  # noqa: ANN001
        self._valid = [k for k, _ in options]
        opts = [ft.dropdown.Option(key=k, text=label) for k, label in options]
        current = str(value) if value in self._valid else (self._valid[0] if self._valid else "")
        self.dd = ft.Dropdown(
            value=current, options=opts, dense=True, text_size=11, width=260,
            bgcolor=theme.c("bg_panel"), border_color=theme.c("border"),
            color=theme.c("text_primary"),
            on_select=(lambda e: on_change(self.dd.value)) if on_change else None)

    def build(self) -> ft.Control:
        return self.dd

    def validate(self) -> str | None:
        if self.dd.value and self.dd.value not in self._valid:
            return f"未知名目：{self.dd.value}"
        return None

    def get_value(self):
        return self.dd.value or None

    def set_value(self, value: object) -> None:
        if value in self._valid:
            self.dd.value = value
        elif self._valid:
            self.dd.value = self._valid[0]


def _grouped_schema() -> list[tuple[str, list[dict]]]:
    """schema 按 ``group`` 归并（**同组多键 = 一张卡**，避免"每键一小块"）

    2026-09-21 用户反馈：*"配置页的事件窗口太多小块，需要合并"*——原先每个 schema
    条目各占一张卡，事件窗口的三个开关就是三张同样的卡。现按组归并，一组一卡，
    卡内每项一行（标题 + 恢复默认 / 说明 / 控件）。
    """
    out: list[tuple[str, list[dict]]] = []
    index: dict[str, int] = {}
    for item in config_center.schema():
        g = str(item.get("group", ""))
        if g not in index:
            index[g] = len(out)
            out.append((g, []))
        out[index[g]][1].append(item)
    return out


def _render_editor(item: dict, theme, *, on_instant=None) -> object | None:  # noqa: ANN001
    """schema 条目 → 编辑器（``build/validate/get_value/set_value`` 四件套）

    ``on_instant``：**点击类**改动（开关 / 增删行 / 加删标签）的即时落盘回调——
    这类动作不该等文本防抖窗口（2026-10-15 实时保存）。
    """
    value = config_center.get(item["key"])
    kind = item["type"]
    if kind == config_center.TYPE_PATH_LIST:
        return PathListEditor(value, on_change=on_instant)
    if kind == config_center.TYPE_BOOL:
        return _BoolEditor(value, theme)
    if kind == config_center.TYPE_TEXT:
        return _TextEditor(value, theme, hint=str(item.get("placeholder", "留空＝跟随方案 / 内置默认")))
    if kind == config_center.TYPE_INT:
        return _IntEditor(value, theme, hint=str(item.get("placeholder", "")))
    if kind == config_center.TYPE_TAGS:
        from ...core import project as _project
        rec = item.get("recommended_path")
        recommended = _project.profile_value(rec) if rec else None
        return KeywordsEditor(value, theme, hint=str(item.get("placeholder", "输入词后回车添加")),
                              recommended=recommended, on_change=on_instant)
    if kind == config_center.TYPE_TEXTAREA:
        return _TextAreaEditor(value, theme,
                              hint=str(item.get("placeholder", "一行一词")))
    return None



def build(page: ft.Page, state: AppState) -> ft.Control:
    t = state.theme
    editors: dict[str, object] = {}
    kinds: dict[str, str] = {}                 # key → schema type（决定即时/防抖 + Toast）
    err_ui: dict[str, ft.Text] = {}            # key → 行内校验错误（红字）
    saver: dict[str, object] = {"obj": None}   # 实时保存器（装配在编辑器建好之后）
    #: 底部状态行（持久反馈；key 供测试定位——轻提示只闪 1.8s，此处可回看）
    status = ft.Text("", size=11, color=t.c("text_hint"), key="settings_status")
    busy = {"flag": False}

    def _set_status(text: str, color_key: str = "text_hint") -> None:
        status.value = text
        status.color = t.c(color_key)
        try:
            status.update()
        except Exception:  # noqa: BLE001
            pass

    def _snack(text: str, ok: bool = True) -> None:
        """轻提示（顶部浮层 Toast：成功绿 / 失败红；未装配时回退 SnackBar）"""
        toast_ui.toast(page, text, ok, t)

    def _sync_topbar_device() -> None:
        """刷新顶栏设备胶囊（别名来自当前方案档案 ``device.alias``）

        配置页改了「设备别名」（或设备序列号）并保存后，顶栏要**立刻**反映——
        复用壳注册的 ``state.on_device_change``（main.py → ``_sync_device_ui``，
        只重算文本/配色，不重建顶栏）。无壳（测试）时静默跳过。
        """
        cb = getattr(state, "on_device_change", None)
        if callable(cb):
            try:
                cb(getattr(state, "device", ""))
            except Exception:  # noqa: BLE001
                pass

    def _run_busy(btn: ft.Control, label: str, work) -> None:
        """加载缓冲：按钮转圈禁用 → 线程执行 work() → 复原 → 状态行 + 弹窗"""
        if busy["flag"]:
            return
        busy["flag"] = True
        orig_content, orig_icon = btn.content, btn.icon
        btn.disabled = True
        btn.icon = None
        btn.content = ft.Row(
            [ft.ProgressRing(width=14, height=14, stroke_width=2,
                             color=t.c("text_on_accent")),
             ft.Text(label, size=12)],
            spacing=8, tight=True, alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        try:
            page.update()
        except Exception:  # noqa: BLE001
            pass

        def worker() -> None:
            try:
                ok, msg = work()
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"内部错误：{exc}"
            if ok:
                time.sleep(_MIN_BUSY_MS / 1000.0)
                _sync_topbar_device()      # 设备别名 / 序列号改动即时反映到顶栏
            btn.content = orig_content
            btn.icon = orig_icon
            btn.disabled = False
            _set_status(msg, "success" if ok else "danger")
            _snack(msg, ok)
            try:
                page.update()
            except Exception:  # noqa: BLE001
                pass
            busy["flag"] = False

        page.run_thread(worker)

    def _card_border() -> ft.border.Border:
        return ft.border.Border.all(1, t.c("border"))

    def _reset_group(key: str, group: str, btn: ft.TextButton) -> None:
        if busy["flag"]:
            return

        def work() -> tuple[bool, str]:
            if key.startswith("project.") and key != "project.active":
                # 32 号：project.* 的值属于方案文件 → "恢复默认"＝从方案里移除该 path
                # （`save_values` 对空值即删除该 path，等于回到"跟随内置默认"）
                done, why = project_mod.save_values({key: None})
                if not done:
                    return False, f"恢复失败：{why}"
            elif not config_center.reset(key):
                return False, f"恢复失败：写入 {config_center.SETTINGS_FILE} 出错"
            editor = editors.get(key)
            if editor is not None:
                editor.set_value(config_center.get(key))
            saver["obj"] and saver["obj"].mark_saved(key)   # 回显后同步基线（防再存一次）
            return True, f"已恢复默认：{group}"

        _run_busy(btn, "恢复中…", work)

    def _value_to_store(key: str, value):
        """非 ``project.*`` 键的落盘值（留空 → ``None``）

        历史（2026-09-21 真机验证）：点「保存」会把**未编辑**的 project.* 也写实成
        settings 覆盖，导致切方案不生效。32 号起 ``project.*`` 的值**不再走 settings**，
        而是直接写进当前方案文件（见 :func:`cartest.core.project.save_values`），
        本函数只服务其余键。
        """
        if key.startswith("project.") and key != "project.active":
            try:
                if value == project_mod.profile_value(key[len("project."):]):
                    return None
            except Exception:  # noqa: BLE001
                pass
        return value

    def _commit_one(key: str, value) -> tuple[bool, str]:
        """单键落盘（实时保存用）：``project.*`` → 方案文件；其余 → settings.json

        与旧「保存」按钮的差异：**不再全量写**，只写改动的那个键（其他键未动即无需写，
        也避免把未编辑的旧值重新固化）。语义与 32 号一致（project.* 只属于方案文件）。
        """
        if key.startswith("project.") and key != "project.active":
            done, used = project_mod.save_values({key: value})
            if not done:
                return False, f"保存失败：{used}"
            name = _scheme_name(used) if isinstance(used, str) else ""
            return True, (f"已保存到方案「{name}」" if name else "已保存")
        if not config_center.set(key, _value_to_store(key, value)):
            return False, f"保存失败：写入 {config_center.SETTINGS_FILE} 出错"
        return True, "已保存"

    def _mark_instant(key: str) -> None:
        """编辑器的「点击类」改动回调（开关/增删行/加删标签）→ 标记为即时落盘"""
        obj = saver["obj"]
        if obj is not None:
            obj.mark_instant(key)      # type: ignore[attr-defined]

    cards: list[ft.Control] = []

    # ── 配置方案卡（32 号：方案＝data/projects/<key>.json，可自定义增/删/改，上限 5 组；
    #    保存把 project.* 值写进当前方案文件，只存与内置默认不同的差异）──
    scheme_holder = ft.Column([], spacing=6)

    def _scheme_options() -> list[tuple[str, str]]:
        opts: list[tuple[str, str]] = [(project_mod.BUILTIN_KEY, project_mod.DEFAULT_LABEL)]
        opts += [(p["key"], p["name"]) for p in project_mod.list_schemes()]
        return opts

    def _scheme_name(key: str) -> str:
        if key == project_mod.BUILTIN_KEY:
            return project_mod.DEFAULT_LABEL
        for p in project_mod.list_schemes():
            if p["key"] == key:
                return p["name"]
        return project_mod.DEFAULT_LABEL

    # ── 设备绑定（2026-10-19 用户诉求）：方案 ↔ 设备序列号 ──
    # 连接/切换到已绑定设备时，app/main 自动切到对应方案（见 scheme_for_device）。
    def _binding_row() -> ft.Control:
        key = project_mod.active_key()
        devices = project_mod.scheme_devices(key)
        text = "、".join(devices) if devices else "未绑定"
        is_builtin = key == project_mod.BUILTIN_KEY
        bind_btn = ft.OutlinedButton(
            "绑定设备…", icon=ft.Icons.LINK,
            disabled=is_builtin,
            tooltip=("内置方案不可绑定——请先新建方案" if is_builtin
                     else "选择在线设备或手动输入序列号"),
            on_click=lambda e: _open_bind_dialog())
        unbind_btn = ft.TextButton(
            "全部解绑", visible=bool(devices),
            on_click=lambda e: _unbind_all(key))
        return ft.Row([
            ft.Text("绑定设备：", size=11, color=t.c("text_secondary")),
            ft.Text(text, size=11, color=t.c("text_primary")),
            bind_btn, unbind_btn,
        ], spacing=8, wrap=True)

    def _unbind_all(key: str) -> None:
        for dev in list(project_mod.scheme_devices(key)):
            project_mod.unbind_device(key, dev)
        _refresh_schemes()
        _snack("已解绑该方案的全部设备", ok=True)

    def _toggle_bind(key: str, serial: str, is_bound: bool) -> None:
        if is_bound:
            done, msg = project_mod.unbind_device(key, serial)
            act = "解绑"
        else:
            done, msg = project_mod.bind_device(key, serial)
            act = "绑定"
        if not done:
            _snack(f"{act}失败：{msg}", ok=False)
            return
        _refresh_schemes()
        # 绑定的正是当前设备 → 立即切到该方案（所见即所得）
        if not is_bound and state.device == serial \
                and project_mod.active_key() != key:
            project_mod.set_active(key)
        _snack(f"已{act}设备 {serial}", ok=True)

    def _open_bind_dialog() -> None:
        key = project_mod.active_key()
        if key == project_mod.BUILTIN_KEY:
            _snack("内置方案不可绑定设备，请先新建方案", ok=False)
            return
        try:
            state.refresh_devices()
        except Exception:  # noqa: BLE001
            pass
        bound = set(project_mod.scheme_devices(key))
        rows: list[ft.Control] = []
        for dev in state.devices:
            is_bound = dev in bound
            alias = ""
            try:
                alias = project_mod.alias_for_serial(dev)
            except Exception:  # noqa: BLE001
                pass
            label = f"{alias} — {dev}" if alias else dev
            rows.append(ft.TextButton(
                ("✓ 已绑定  " if is_bound else "＋ 绑定  ") + label,
                on_click=lambda e, d=dev, b=is_bound: (
                    page.pop_dialog(), _toggle_bind(key, d, b))))
        if not rows:
            rows.append(ft.Text("未发现在线设备（可在下方手动输入序列号）",
                                size=11, color=t.c("text_secondary")))
        manual = ft.TextField(hint_text="手动输入设备序列号", dense=True,
                              text_size=12, on_change=sync_text_value)
        err = ft.Text("", size=10, color=t.c("danger"))

        def _bind_manual(e=None) -> None:  # noqa: ANN001
            serial = (manual.value or "").strip()
            if not serial:
                err.value = "序列号不能为空"
                try:
                    err.update()
                except Exception:  # noqa: BLE001
                    pass
                return
            page.pop_dialog()
            _toggle_bind(key, serial, serial in bound)

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(f"绑定设备 — 方案「{_scheme_name(key)}」", size=14,
                          color=t.c("text_primary")),
            content=ft.Column(rows + [ft.Divider(height=1), manual, err],
                              spacing=4, tight=True, width=420),
            actions=[
                ft.TextButton("关闭", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("绑定手动输入", on_click=_bind_manual),
            ]))

    def _on_scheme_picked(key: str) -> None:
        """下拉选方案 → 立即切换 + 重载本页（值整体换成新方案的那套）"""
        if not key or key == project_mod.active_key():
            return
        project_mod.set_active(key)
        _snack(f"已切到方案「{_scheme_name(key)}」，值已重载", ok=True)
        hook = getattr(state, "refresh_page", None)
        if callable(hook):
            hook()
        else:                                    # 无壳（测试）时退化为只刷新下拉
            _refresh_schemes()

    def _refresh_schemes() -> None:
        """重建下拉 + 按钮（增/删/改名后立即反映；`editors` 同步换新实例）"""
        editors["project.active"] = _ChoiceEditor(project_mod.active_key(),
                                                  _scheme_options(), t,
                                                  on_change=_on_scheme_picked)
        full = project_mod.scheme_count() >= project_mod.MAX_SCHEMES
        add_btn.disabled = full
        add_btn.tooltip = (f"已达上限（最多 {project_mod.MAX_SCHEMES} 组）——删除一个方案后可再新增"
                           if full else "新建一个空白方案（值＝内置默认）并切换过去")
        active_is_scheme = project_mod.active_key() != project_mod.BUILTIN_KEY
        ren_btn.disabled = del_btn.disabled = not active_is_scheme
        ren_btn.tooltip = "" if active_is_scheme else "先在上方选中一个方案，再重命名"
        del_btn.tooltip = "" if active_is_scheme else "先在上方选中一个方案，再删除"
        scheme_holder.controls = [
            ft.Row([editors["project.active"].build()], spacing=8),
            ft.Row([add_btn, ren_btn, del_btn], spacing=8, wrap=True),
            _binding_row(),
        ]
        try:
            scheme_holder.update()
        except Exception:  # noqa: BLE001
            pass

    def _ask_name(title: str, initial: str, on_ok) -> None:  # noqa: ANN001
        """名称输入对话框（#14 教训：TextField 必须挂 on_change，否则服务端读不到）"""
        field = ft.TextField(value=initial, dense=True, text_size=12, autofocus=True,
                             on_change=sync_text_value,
                             hint_text="给这套配置起个名字（例：台架A / 客户车机）")
        err = ft.Text("", size=10, color=t.c("danger"))

        def _ok(e=None) -> None:  # noqa: ANN001
            name = (field.value or "").strip()
            if not name:
                err.value = "方案名不能为空"
                try:
                    field.update()
                except Exception:  # noqa: BLE001
                    pass
                return
            page.pop_dialog()
            on_ok(name)

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(title, size=14, color=t.c("text_primary")),
            content=ft.Column([field, err], spacing=4, tight=True, width=300),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("确定", on_click=_ok),
            ]))

    def _confirm(text: str, on_ok) -> None:  # noqa: ANN001
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("确认", size=14, color=t.c("text_primary")),
            content=ft.Text(text, size=12, color=t.c("text_secondary")),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("删除", on_click=lambda e: (page.pop_dialog(), on_ok())),
            ]))

    def _add_scheme(e=None) -> None:  # noqa: ANN001
        if project_mod.scheme_count() >= project_mod.MAX_SCHEMES:
            _snack(f"最多 {project_mod.MAX_SCHEMES} 组方案（请先删除一个）", ok=False)
            return

        def ok(name: str) -> None:
            done, res = project_mod.create_scheme(name)
            if not done:
                _snack(f"新增失败：{res}", ok=False)
                return
            project_mod.set_active(res)
            _refresh_schemes()
            _snack(f"已新建并切换到「{name}」（空白方案＝内置默认值，改动即时写入）", ok=True)

        _ask_name("新增配置方案", "", ok)

    def _rename_scheme(e=None) -> None:  # noqa: ANN001
        key = project_mod.active_key()
        if key == project_mod.BUILTIN_KEY:
            _snack("当前是「默认（内置）」，无需重命名", ok=False)
            return

        def ok(name: str) -> None:
            done, res = project_mod.rename_scheme(key, name)
            if not done:
                _snack(f"重命名失败：{res}", ok=False)
                return
            _refresh_schemes()
            _snack(f"已重命名为「{res}」", ok=True)

        _ask_name("重命名方案", _scheme_name(key), ok)

    def _remove_scheme(e=None) -> None:  # noqa: ANN001
        key = project_mod.active_key()
        if key == project_mod.BUILTIN_KEY:
            _snack("当前是「默认（内置）」，无可删除", ok=False)
            return
        label = _scheme_name(key)

        def ok() -> None:
            done, nxt = project_mod.remove_scheme(key)
            if not done:
                _snack(f"删除失败：{nxt}", ok=False)
                return
            _refresh_schemes()
            _snack(f"已删除「{label}」；当前方案：{_scheme_name(nxt)}", ok=True)

        _confirm(f"删除方案「{label}」？删除后不可恢复（方案文件将被移除）。", ok)

    add_btn = ft.OutlinedButton("新增方案", icon=ft.Icons.ADD, on_click=_add_scheme)
    ren_btn = ft.OutlinedButton("重命名", icon=ft.Icons.DRIVE_FILE_RENAME_OUTLINE,
                                on_click=_rename_scheme)
    del_btn = ft.OutlinedButton("删除方案", icon=ft.Icons.DELETE_OUTLINE,
                                on_click=_remove_scheme)
    proj_reset = ft.TextButton("恢复默认")
    proj_reset.on_click = lambda e, b=proj_reset: _reset_group("project.active", "配置方案", b)
    cards.append(ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("配置方案", size=13, weight=ft.FontWeight.BOLD,
                        color=t.c("text_primary")),
                ft.Container(expand=True),
                proj_reset,
            ]),
            ft.Text("一个方案＝一套车机相关配置（包名/服务/灌入 URI/关键词/判定规则/VR）；"
                    f"最多 {project_mod.MAX_SCHEMES} 组，可增删改。改动**即时写入当前方案**"
                    "（只存与内置默认不同的值）；切换方案会重载本页并清掉项目相关的工具偏好。",
                    size=10, color=t.c("text_hint")),
            scheme_holder,
        ], spacing=6),
        padding=12, bgcolor=t.c("bg_card"), border_radius=8, border=_card_border()))
    _refresh_schemes()

    for group, items in _grouped_schema():
        rows: list[ft.Control] = []
        for item in items:
            key = item["key"]
            editor = _render_editor(
                item, t,
                on_instant=lambda k=key: _mark_instant(k))   # 开关/增删行 → 即时落盘
            label = ft.Text(item.get("label") or item["group"], size=11,
                            color=t.c("text_secondary"))
            reset_btn = ft.TextButton("恢复默认")
            reset_btn.on_click = (lambda e, k=key, g=group, b=reset_btn:
                                  _reset_group(k, g, b))
            if editor is None:
                body: ft.Control = ft.Text(f"（类型 {item['type']} 暂未实现渲染）",
                                           size=11, color=t.c("text_hint"))
            else:
                editors[key] = editor
                kinds[key] = str(item["type"])
                body = editor.build()
            err_text = ft.Text("", size=10, color=t.c("danger"))
            err_ui[key] = err_text
            rows += [
                ft.Row([label, ft.Container(expand=True), reset_btn],
                       spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(item.get("desc", ""), size=10, color=t.c("text_hint")),
                body,
                err_text,
            ]
        cards.append(ft.Container(
            content=ft.Column([
                ft.Text(group, size=13, weight=ft.FontWeight.BOLD,
                        color=t.c("text_primary")),
                *rows,
            ], spacing=6),
            padding=12, bgcolor=t.c("bg_card"), border_radius=8, border=_card_border(),
        ))

    # ── 实时保存装配（2026-10-15：取消「保存」按钮）──
    def _set_inline_error(key: str, text: str) -> None:
        ctl = err_ui.get(key)
        if ctl is None:
            return
        if ctl.value == text:
            return
        ctl.value = text
        try:
            ctl.update()
        except Exception:  # noqa: BLE001
            pass

    def _on_autosave(key: str, ok: bool, msg: str, err: str,
                     instant: bool = False) -> None:
        """自动保存结果 → 行内红字 + 状态行（+ 点击类成功时轻提示）

        校验失败**不弹 Toast**：文本输入过程中会反复进出失败态，弹窗会连续骚扰。
        """
        if err:
            _set_inline_error(key, f"未保存：{err}")
            _set_status(f"未保存：{err}", "danger")
            return
        _set_inline_error(key, "")
        if not ok:
            _set_status(msg, "danger")
            return
        _sync_topbar_device()                 # 设备别名/序列号改动即时反映到顶栏
        _set_status(f"{msg}（{time.strftime('%H:%M:%S')}）", "success")
        if instant or kinds.get(key) in _AutoSaver.INSTANT_KINDS:
            _snack(msg, ok=True)              # 点一下即存的动作：给一次轻提示

    _saver = _AutoSaver(editors, kinds, _commit_one, _on_autosave)
    saver["obj"] = _saver
    hooks = getattr(state, "tick_hooks", None)
    if hooks is not None:                     # 无壳（测试）时跳过
        hooks.append(lambda _tick: _saver.poll())
    try:
        state.before_page_leave = _saver.flush   # 离开页面：未结算的改动立即落盘
    except Exception:  # noqa: BLE001
        pass

    cards_col = ft.Column(cards, spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)

    # 顶部标题行：标题 + 说明（2026-10-15 起无「保存」按钮——改动即时落盘）
    header = ft.Row([
        ft.Text("配置", size=14, weight=ft.FontWeight.BOLD, color=t.c("text_primary")),
        ft.Text("改动即时保存（开关点一下即生效；文本停手后自动写入）；"
                "底部状态行显示最近一次写入结果",
                size=10, color=t.c("text_hint")),
    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    # 底部状态行：保存结果的持久反馈（轻提示只闪一下，这里可回看路径）
    footer = ft.Container(
        content=ft.Row([status], spacing=12,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.padding.Padding(top=8))

    return ft.Container(
        content=ft.Column([
            header,
            cards_col,
            footer,
        ], spacing=10, expand=True),
        expand=True,
        padding=ft.padding.Padding(left=16, top=12, right=16, bottom=12),
    )
