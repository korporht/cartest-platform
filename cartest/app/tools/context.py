# -*- coding: utf-8 -*-
"""CarTest tools — 工具页宿主适配层（ToolContext）

迁移策略（见 06-P3b设计.md §4.2）：`flet_Tool` 的 Tab 只依赖宿主 app 的**一小撮接口**，
本模块把平台的 `page`/`state` 包装成那套接口，使 Tab 代码**近乎零改动**复用。

实测依赖面（Agent 逐行统计 log/touch/timestamp 三个 Tab）::

    page.run_thread / show_dialog / pop_dialog / update / set_clipboard
    cfg.get / cfg.set
    set_status(text, color_key)
    append_log_to_panel(text) / _log_panel_list
    theme.c(key)
    snack(text)（可选，Tab 用 hasattr 保护）

操作日志窗口（23-操作日志窗口设计.md 一期）：
  · 行渲染 = 单 ``ft.Text`` + 多 ``TextSpan``（时间戳/级别字形/正文分色，零额外控件）
  · 三级分层：``raw``（噪音/底层）行**永久隐藏**——2026-10-12 用户裁决「事件窗口只作
    展示」，原来的「详细输出」开关随工具条一并下线
  · 结构化 API：``begin_op / end_op / log``（主操作耗时）；``append_log_to_panel`` 兼容不变
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import flet as ft

from ..state import AppState
from ..theme import AppTheme
from ..ui import toast as toast_ui
from . import constants
from .widgets import oplog as oplog_mod

log = logging.getLogger("tools.ctx")

#: 兼容别名（口径同 oplog.MAX_LINES）
MAX_PANEL_LINES = oplog_mod.MAX_LINES


#: 工具键 → 事件来源模块（事件优先：事件行显示 [模块]）
_MODULE_BY_KEY: dict[str, str] = {
    "logcat": "日志终端",
    "timestamp": "时间戳转换",
    "offline": "对话还原",
    "apk": "应用与传输",
    "push": "应用与传输",
    "touch": "屏幕与触控",
    "recorder": "屏幕与触控",
    "monkey": "Monkey",
    "logextract": "日志切割",
    "testflow": "测试流程",
}


def _safe_update(ctl) -> None:
    """控件更新：控件可能尚未挂载（切页瞬间），失败不致命"""
    try:
        ctl.update()
    except Exception:  # noqa: BLE001
        pass


def _safe_on_main(page, fn) -> None:
    """控件树变更调度：非主线程调用时经 ``page.run_task`` 回主线程执行

    2026-09-23 崩溃修复（34 号文档）：工作线程直接 ``controls.append`` + ``update()``
    与 Flet 0.85 ObjectPatch 的 diff（点击事件后的自动 update）并发竞争 →
    ``_compare_lists`` ``IndexError: list index out of range`` → **整应用崩溃**。
    ``page.run_task`` = ``run_coroutine_threadsafe``（任何线程可安全投递），
    回主线程后与事件循环串行，竞争窗口消除。
    """
    if threading.current_thread() is threading.main_thread():
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            log.warning("on_main(inline) 回调异常 %s: %r",
                        getattr(fn, "__qualname__", fn), ex)
        return

    async def _go() -> None:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            log.warning("on_main(run_task) 回调异常 %s: %r",
                        getattr(fn, "__qualname__", fn), ex)

    try:
        page.run_task(_go)
    except Exception as ex:  # noqa: BLE001
        log.warning("on_main(run_task) 投递失败 %s: %r",
                    getattr(fn, "__qualname__", fn), ex)


class _OpHandle:
    """``ToolContext.op()`` 的句柄：``fail()`` 显式标记该操作失败"""

    def __init__(self, op_id: int) -> None:
        self.op_id = op_id
        self.ok = True

    def fail(self) -> None:
        self.ok = False


class ToolConfig:
    """工具偏好持久化（对等 flet_Tool 的 ``AppConfig.get/set``）"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 配置写失败不影响主流程，但不再静默——见 06-P3b设计.md 攻击 #4
            pass


class _PageProxy:
    """``ft.Page`` 薄代理：只补接口差异，其余属性透传。

    补的点：``set_clipboard``（flet_Tool 的写法 → 0.85 的 ``page.clipboard.set()``）。
    """

    def __init__(self, page: ft.Page) -> None:
        self._page = page

    def __getattr__(self, name: str):
        # 仅在常规查找失败时进入 → 不会递归
        return getattr(self._page, name)

    def set_clipboard(self, text: str) -> None:
        """flet_Tool 语义 → 0.85：``Clipboard.set`` 是 **async**，必须 ``run_task`` 调度

        发现 #9：同步直调只会产生未 await 的协程 → 复制静默失效（真机自测）。
        """
        try:
            self._page.run_task(self._page.clipboard.set, text)
        except Exception:  # noqa: BLE001
            try:
                self._page.clipboard.set(text)      # 兜底（老版本同步实现）
            except Exception:  # noqa: BLE001
                pass


class ToolContext:
    """工具页宿主适配器（对等 flet_Tool 的 ``MainApp``）

    由页面壳为每个工具创建一个：``ctx = ToolContext(page, state, "logcat")``，
    然后 ``LogTab(ctx)`` 即可原样运行。
    """

    #: 控制台等宽字体栈（23 设计：系统字体，零资产）
    _MONO = "JetBrains Mono, Cascadia Mono, Consolas, monospace"
    _GLYPH = {"op": "▸", "cmd": "$", "info": "ℹ", "ok": "✓",
              "warn": "⚠", "error": "✕", "raw": "⚙"}
    _LEVEL_TOKEN = {"op": "oplog_op", "cmd": "oplog_cmd", "info": "oplog_info",
                    "ok": "oplog_ok", "warn": "oplog_warn",
                    "error": "oplog_error", "raw": "oplog_raw"}

    def __init__(self, page: ft.Page, state: AppState, tool_key: str) -> None:
        self.state = state
        self.tool_key = tool_key
        self.theme: AppTheme = state.theme        # Tab 里取 ``self.t = app.theme``
        self._module = _MODULE_BY_KEY.get(tool_key, "工具")   # 事件来源模块
        self._page = _PageProxy(page)
        self._cfg = ToolConfig(
            Path(constants.LOCAL_SAVE_ROOT) / f"{tool_key}_config.json")
        # 事件模型**会话级**持有（state.oplog_models）：切页/切主题重建后历史仍在
        models = getattr(state, "oplog_models", None)
        if models is None:                      # 老 state 兜底（无该字段时挂一份）
            models = {}
            try:
                state.oplog_models = models
            except Exception:  # noqa: BLE001
                pass
        model = models.get(tool_key)
        if model is None:
            model = oplog_mod.OplogModel(constants.LOCAL_RUNTIME_LOG_DIR)
            models[tool_key] = model
        self._log_model: oplog_mod.OplogModel = model
        self._log_list: ft.ListView | None = None
        self._status_text: ft.Text | None = None
        self._op_controls: dict[int, ft.Text] = {}

    # ── 与 flet_Tool 的 app 接口对齐 ──────────
    @property
    def page(self):
        """页面代理（``update`` / ``run_thread`` / 对话框 / ``set_clipboard``）"""
        return self._page

    @property
    def cfg(self) -> ToolConfig:
        """工具偏好读写"""
        return self._cfg

    @property
    def _log_panel_list(self) -> ft.ListView:
        """工具页日志面板的 ListView（Tab 会把它当 ``content`` 或续写）

        2026-10-12 用户裁决：事件窗口**只作展示**——📌「跟随」开关与工具条一并下线，
        故恒为 ``auto_scroll=True``（始终跟随最新行）。实测（发现 #7）：flet 0.85 Web 端
        ``ScrollEvent`` 无有效载荷（scroll_delta=0、data/local_position=None），
        "滚轮上翻自动锁定、回底自动恢复"本就无法实现，显式开关是当时唯一的确定性方案；
        现按"纯展示"口径去掉该开关，回归最简单的跟随行为。
        """
        if self._log_list is None:
            self._log_list = ft.ListView(expand=True, spacing=2, auto_scroll=True)
        return self._log_list

    # ── 日志渲染（单 Text + TextSpan）──────────
    def _style(self, color_key: str, *, size: int = 11, bold: bool = False,
               bg: str | None = None) -> ft.TextStyle:
        return ft.TextStyle(
            size=size, font_family=self._MONO, height=1.5,
            color=self.theme.c(color_key),
            weight=ft.FontWeight.BOLD if bold else None,
            bgcolor=bg)

    def _line_text(self, ln: oplog_mod.LogLine) -> ft.Text:
        """一行 = 单 ft.Text（时间戳/字形/模块或标签/正文分色；错误/事件行带行底）

        事件优先口径：**事件行 = app 内一次功能触发**，展示
        `▸/▾ [模块] 名称 参数摘要 ⏱ 耗时 ✓`；子行**默认展开**（2026-09-22 用户裁决），
        点事件行可单独收起（收起时尾标 `＋N 行`）；错误行常驻。
        """
        t = self.theme
        row_bg = (t.c("oplog_op_bg") if ln.level == "op"
                  else t.c("oplog_error_bg") if ln.level == "error" else None)
        glyph = self._GLYPH.get(ln.level, "·")
        expanded = True
        if ln.level == "op" and ln.op_id is not None:
            expanded = self._log_model.is_expanded(ln.op_id)
            glyph = "▾" if expanded else "▸"
        spans = [
            ft.TextSpan(ln.ts + " ", style=self._style("oplog_ts", size=10, bg=row_bg)),
            ft.TextSpan(glyph + " ",
                        style=self._style(self._LEVEL_TOKEN.get(ln.level, "oplog_text"),
                                          bold=(ln.level == "op"), bg=row_bg)),
        ]
        label = ""
        if ln.level == "op":
            label = ln.module                      # 事件行显示来源模块
        elif ln.tag and ln.tag != "工具":
            label = ln.tag
        if label:
            spans.append(ft.TextSpan(f"[{label}] ", style=self._style("oplog_tag", bg=row_bg)))
        indent = "  " if ln.level in ("cmd", "raw") else ""
        spans.extend(self._body_spans(
            ln.text,
            self._style(self._LEVEL_TOKEN.get(ln.level, "oplog_text"),
                        bold=(ln.level == "op"), bg=row_bg),
            indent))
        if ln.level == "op" and ln.param:
            spans.append(ft.TextSpan("   " + ln.param,
                                     style=self._style("oplog_ts", bg=row_bg)))
        if ln.level == "op" and ln.dur is not None:
            spans.append(ft.TextSpan(
                f"   ⏱ {ln.dur:.1f}s " + ("✓" if ln.ok else "✕"),
                style=self._style("oplog_ok" if ln.ok else "oplog_error", bg=row_bg)))
        if ln.level == "op" and ln.op_id is not None and not expanded:
            n = self._log_model.child_count(ln.op_id)
            if n:
                spans.append(ft.TextSpan(
                    f"   ＋{n} 行", style=self._style("oplog_ts", bg=row_bg)))
        # 事件行/错误行要响应手势（展开、右键菜单）——selectable 文本会吞指针事件，
        # 这两类行关闭选择（复制走右键菜单）
        selectable = ln.level not in ("op", "error")
        return ft.Text(spans=spans, selectable=selectable)

    def _wrap_interactive(self, ln: oplog_mod.LogLine, inner: ft.Text) -> ft.Control:
        """操作头行（可折叠 + 右键）与错误行（右键）包一层手势"""
        if ln.level == "op" and ln.op_id is not None:
            return ft.GestureDetector(
                content=inner,
                on_tap=lambda e, o=ln.op_id: self._toggle_op_view(o),
                on_secondary_tap=lambda e, l=ln: self._show_line_menu(l))
        if ln.level == "error":
            return ft.GestureDetector(
                content=inner,
                on_secondary_tap=lambda e, l=ln: self._show_line_menu(l))
        return inner

    def _line_control(self, ln: oplog_mod.LogLine) -> ft.Control:
        return self._wrap_interactive(ln, self._line_text(ln))

    def _body_spans(self, text: str, base_style: ft.TextStyle,
                    indent: str = "") -> list[ft.TextSpan]:
        """正文 spans；高亮 = query 命中（accent 加粗）+ 自动规则（设备号/路径/容量）"""
        out: list[ft.TextSpan] = []
        if indent:
            out.append(ft.TextSpan(indent, style=base_style))
        ranges = self._log_model.highlight_ranges(text)
        if not ranges:
            out.append(ft.TextSpan(text, style=base_style))
            return out
        hl_query = ft.TextStyle(size=11, font_family=self._MONO, height=1.5,
                                color=self.theme.c("accent_text"),
                                weight=ft.FontWeight.BOLD, bgcolor=base_style.bgcolor)
        hl_auto = ft.TextStyle(size=11, font_family=self._MONO, height=1.5,
                               color=self.theme.c("oplog_warn"),
                               bgcolor=base_style.bgcolor)
        pos = 0
        for start, end, kind in ranges:
            if start > pos:
                out.append(ft.TextSpan(text[pos:start], style=base_style))
            out.append(ft.TextSpan(text[start:end],
                                   style=hl_query if kind == "query" else hl_auto))
            pos = end
        if pos < len(text):
            out.append(ft.TextSpan(text[pos:], style=base_style))
        return out

    def _toggle_op_view(self, op_id: int) -> None:
        """点事件行：展开/收起该事件的子行（事件优先：默认收起）"""
        self._log_model.toggle_expand(op_id)
        self._rebuild_log_view()

    def _show_line_menu(self, ln: oplog_mod.LogLine) -> None:
        """右键菜单（事件行/错误行）：复制该行 / 复制该事件块 / 导出该事件块"""
        def _copy(text: str, tip: str) -> None:
            try:
                self._page.set_clipboard(text)
                self.set_status(tip, "success")
            except Exception as ex:  # noqa: BLE001
                self.set_status(f"复制失败: {ex}", "danger")
            self._close_menu()

        def _copy_line(e=None) -> None:
            _copy(self._log_model.plain(ln), "已复制该行")

        def _copy_block(e=None) -> None:
            if ln.op_id is not None:
                text = "\n".join(self._log_model.plain(x)
                                 for x in self._log_model.op_lines(ln.op_id))
            else:
                text = self._log_model.plain(ln)
            _copy(text, "已复制该事件块")

        def _export_block(e=None) -> None:
            path = (self._log_model.export_block(ln.op_id)
                    if ln.op_id is not None else None)
            if path is None:
                self.set_status("该行不属于任何事件，无法单独导出", "warning")
            else:
                self.set_status(f"已导出事件块: {path.name}", "success")
            self._close_menu()

        dlg = ft.AlertDialog(
            title=ft.Text(f"日志行 {ln.ts} · {ln.level.upper()}", size=13),
            content=ft.Text(ln.text[:120], size=11, selectable=True),
            actions=[
                ft.TextButton("复制该行", on_click=_copy_line),
                ft.TextButton("复制该事件块", on_click=_copy_block,
                              disabled=(ln.op_id is None)),
                ft.TextButton("导出该事件块", on_click=_export_block,
                              disabled=(ln.op_id is None)),
                ft.TextButton("关闭", on_click=lambda e: self._close_menu()),
            ])
        try:
            self._page.show_dialog(dlg)
        except Exception:  # noqa: BLE001
            pass

    def _close_menu(self) -> None:
        try:
            self._page.pop_dialog()
        except Exception:  # noqa: BLE001
            pass

    def _append_control(self, ln: oplog_mod.LogLine) -> None:
        inner = self._line_text(ln)
        if ln.level == "op" and ln.op_id is not None:
            self._op_controls[ln.op_id] = inner
        widget = self._wrap_interactive(ln, inner)
        self.on_main(lambda: self._append_one(widget))

    def _append_one(self, widget) -> None:
        """单行追加（仅主线程；见 ``on_main``）"""
        lst = self._log_panel_list
        lst.controls.append(widget)
        _safe_update(lst)

    def _rebuild_log_view(self) -> None:
        """过滤/搜索/开关/折叠变化 → 全量重渲（主线程；见 ``on_main``）"""
        self.on_main(self._rebuild_log_view_ui)

    def _rebuild_log_view_ui(self) -> None:
        """全量重渲实现（≤MAX_LINES；仅主线程直接调用）"""
        lst = self._log_panel_list
        lst.controls.clear()
        self._op_controls.clear()
        for ln in self._log_model.visible():
            inner = self._line_text(ln)
            if ln.level == "op" and ln.op_id is not None:
                self._op_controls[ln.op_id] = inner
            lst.controls.append(self._wrap_interactive(ln, inner))
        _safe_update(lst)

    # ── 结构化 API（事件优先：begin_op = app 内一次功能触发）────────
    def begin_op(self, title: str, *, tag: str = "工具", param: str = "",
                 module: str | None = None) -> int:
        """开一个事件行，返回 op_id（配合 ``end_op`` 写回耗时/结果）

        ``param`` 参数摘要（包名 / 路径 / 次数…）；``module`` 缺省用本页模块。
        """
        op_id = self._log_model.begin_op(
            title, tag=tag, module=(module or self._module), param=param)
        ln = self._log_model.last_line
        if ln is not None and self._log_model.passes(ln):
            self._append_control(ln)
        return op_id

    def end_op(self, op_id: int, ok: bool = True) -> None:
        """结束操作：原地刷新该行（⏱ 耗时 + ✓/✕）"""
        ln = self._log_model.end_op(op_id, ok=ok)
        if ln is None:
            return
        ctrl = self._op_controls.get(op_id)

        def _ui() -> None:
            if ctrl is not None:
                ctrl.spans = self._line_text(ln).spans
                _safe_update(ctrl)

        self.on_main(_ui)

    def log(self, text: str, level: str = "info", *,
            tag: str = "工具", detail: bool = False) -> None:
        """结构化日志（level: op/cmd/info/ok/warn/error/raw；detail=三级系统输出）

        所有行都带本页模块——「模块」筛选才不会被事件外的行漏掉。
        """
        ln = self._log_model.append(text, level=level, tag=tag, detail=detail,
                                    module=self._module)
        if self._log_model.passes(ln):
            self._append_control(ln)

    @contextlib.contextmanager
    def op(self, title: str, *, tag: str = "工具", param: str = "",
           module: str | None = None):
        """操作计时上下文（三期：耗时全量接入，替代手写 begin/end 样板）

        ``with self.app.op("查看大小", tag="设备", param="路径 3 条") as o:`` ——
        正常结束 ok=True；``o.fail()`` 置失败；抛异常自动置失败并上抛。**不吞异常**。
        """
        handle = _OpHandle(self.begin_op(title, tag=tag, param=param, module=module))
        try:
            yield handle
        except BaseException:
            handle.fail()
            raise
        finally:
            self.end_op(handle.op_id, ok=handle.ok)

    def append_log_to_panel(self, text: str) -> None:
        """追加日志（兼容旧调用；多行输出自动拆行 → 逐行分级/装饰/噪音隐藏）"""
        lines = str(text).splitlines() or [""]
        added: list[ft.Control] = []
        for raw in lines:
            if not raw.strip():
                continue
            ln = self._log_model.append_legacy(raw, module=self._module)
            if not self._log_model.passes(ln):
                continue
            inner = self._line_text(ln)
            if ln.level == "op" and ln.op_id is not None:
                self._op_controls[ln.op_id] = inner
            added.append(self._wrap_interactive(ln, inner))
        if added:
            self.on_main(lambda: self._append_many(added))

    def _append_many(self, added: list) -> None:
        """批量追加已渲染行（仅主线程；见 ``on_main``）

        修复（2026-09-23，35 号文档 §六）：面板行数**必须有上限**——洪峰下
        （~1700 行/秒、累积 2 万+ 行）无界增长会把 flet 引擎拖死：输入/帧
        全部停滞，按钮点击延迟 2 分钟以上才被处理。超限裁掉最旧行，与
        会话模型（``UI_LOG_MAX_LINES``）保留策略一致。
        """
        lst = self._log_panel_list
        lst.controls.extend(added)
        overflow = len(lst.controls) - constants.UI_LOG_MAX_LINES
        if overflow > 0:
            del lst.controls[:overflow]
        _safe_update(lst)

    def clear_log_panel(self) -> None:
        """清空工具页日志面板（模型 + 视图）"""
        self._log_model.clear()
        self._op_controls.clear()

        def _ui() -> None:
            lst = self._log_panel_list
            lst.controls.clear()
            _safe_update(lst)

        self.on_main(_ui)

    def set_status(self, text: str, color_key: str = "text_secondary") -> None:
        """工具页自己的状态行（不占用平台状态栏）"""
        if self._status_text is None:
            return

        def _ui() -> None:
            self._status_text.value = text
            self._status_text.color = self.theme.c(color_key)
            _safe_update(self._status_text)

        self.on_main(_ui)

    def snack(self, text: str, ok: bool = True) -> None:
        """轻提示（顶部浮层 Toast；未装配时回退 SnackBar。Tab 侧用 hasattr 保护）

        线程安全由 ``ui.toast.toast`` 内部保证（工作线程会自行调度回主线程）。
        """
        toast_ui.toast(self._page, text, ok, self.theme)

    def notify_done(self, text: str, ok: bool = True) -> None:
        """工具页**耗时动作完成**的统一反馈（2026-10-14，见 39 号设计）

        一次触发两件事：
          ① 顶部**强提示**浮层（更大、停留 4s）——明显的完成反馈；
          ② 记入**全局操作日志**（``state.activity``）——顶栏「操作日志」抽屉可集中回看。

        文案**只报结果**（如「抓日志完成」），不带数量/路径详情。
        线程安全：浮层由 ``ui.toast`` 内部调度回主线程；``activity.add`` 自带锁。
        """
        toast_ui.toast(self._page, text, ok, self.theme, strong=True)
        activity = getattr(self.state, "activity", None)
        if activity is None:
            return
        try:
            activity.add(source=self._module, action=text, ok=ok)
        except Exception as ex:  # noqa: BLE001
            log.warning("notify_done 写全局操作日志失败: %r", ex)

    def on_main(self, fn) -> None:
        """把控件树变更调度回主线程执行（工作线程切勿直接改控件/update）

        见模块函数 :func:`_safe_on_main`（2026-09-23 竞态崩溃修复，34 号文档）。
        """
        _safe_on_main(self._page, fn)

    # ── 供页面壳使用 ─────────────────────────
    def attach_status(self, text_ctl: ft.Text) -> None:
        """绑定工具页的状态文本控件（``set_status`` 的目标）"""
        self._status_text = text_ctl

    def build_log_panel(self) -> ft.Container:
        """工具页日志面板容器（暗色控制台行视图）

        2026-10-12 用户裁决：事件窗口**只作展示**——工具条（模式/详细输出/跟随/搜索/
        正则/清空）与配置页整组「事件窗口（操作日志）」一并下线，面板只由日志列表构成。

        注意：外层 Container 必须 ``expand=True``——``_wrap``/``_wrap_merged`` 把它
        作为 Flex 子项获取高度；缺失时 Column 在无界约束下列表高度塌缩为 0。
        """
        t = self.theme
        self._rebuild_log_view()      # 历史回填（会话级模型 → 控件；切页/切主题后不空白）
        items: list[ft.Control] = [self._log_panel_list]
        panel = ft.Container(
            content=ft.Column(items, spacing=2, expand=True),
            bgcolor=t.c("bg_oplog"), border_radius=8, padding=8,
            border=ft.border.Border.all(1, t.c("border")),
            expand=True)
        return panel
