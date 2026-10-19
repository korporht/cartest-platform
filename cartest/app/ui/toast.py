# -*- coding: utf-8 -*-
"""CarTest app — 轻提示 / 强提示（Toast）

**顶部居中浮层**：成功绿 / 失败红，圆角胶囊 + 图标 + 文字，到时淡出上移并隐藏。
不占布局、不遮挡底部动作条、不打断操作。

两档强度（2026-10-14）：
  · **轻提示**（``strong=False``，默认）：小胶囊，停留 ``_DURATION_MS``（1.8s）——
    用于「保存成功」「已复制」这类低干扰反馈。
  · **强提示**（``strong=True``）：字号/图标/内边距更大，停留 ``_STRONG_DURATION_MS``
    （4s）——用于工具页**耗时动作完成**（抓日志/截图/安装…）的明显反馈。
  两档**各持 token、互不顶替**：强提示不会被紧随其后的轻提示顶掉。两档共用同一个
  外层定位容器（``self.control``，只向 ``page.overlay`` 追加一次），在无提示时整块隐藏。

为什么不用 ``SnackBar``（2026-10-12 用户裁决）：
  底部弹条会盖住配置页底部固定的「保存」条，且必须等它自己超时或点关闭；
  「保存成功」这类低干扰反馈更适合浮在内容之上、自动退场。

实现要点：
  · 控件挂 ``page.overlay``（渲染在页面内容之上；flet 0.85 未废弃，见 ``base_page.Overlay``）
    —— **不侵入现有布局树**（主布局仍是一个 Column，无需 Stack 包裹）
  · 工作线程调用安全：控件树变更统一经 :func:`_on_main` 回主线程（34 号竞态修复）
  · 同一档连续调用取「最后一次」（token 递增，旧定时器作废），不会串台
  · 未装配 host（单测 / 无壳）时 :func:`toast` 回退 ``SnackBar``，调用点零分支
"""
from __future__ import annotations

import asyncio
import logging
import threading

import flet as ft

log = logging.getLogger(__name__)

#: 轻提示完全显示时长（毫秒）
_DURATION_MS = 1800
#: 强提示完全显示时长（毫秒）——耗时动作完成反馈，停留更久以免错过（2026-10-14）
_STRONG_DURATION_MS = 4000
#: 淡出动画时长（毫秒）
_FADE_MS = 160
#: 距页面顶部距离（顶栏 46px + 页面标题行之下，避免压住标题/副标题）
_TOP = 92
#: 隐藏态相对位移（向上，配合淡出）
_OFFSET_HIDDEN = ft.Offset(0, -0.35)
_OFFSET_SHOWN = ft.Offset(0, 0)

#: 档位样式（轻 / 强）
_LIGHT_STYLE = {"size": 12, "icon": 16, "pad_lr": 16, "pad_tb": 9}
_STRONG_STYLE = {"size": 15, "icon": 22, "pad_lr": 22, "pad_tb": 13}


def _on_main(page, fn) -> None:  # noqa: ANN001
    """控件树变更调度：非主线程调用时经 ``page.run_task`` 回主线程执行

    与 ``tools/context._safe_on_main`` 同源（34 号文档：工作线程直接改控件 +
    update 与 Flet ObjectPatch 的 diff 并发 → ``IndexError`` → 整应用崩溃）。
    ui 层不反向依赖 tools 层，故此处保留一份精简实现。
    """
    if threading.current_thread() is threading.main_thread():
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            log.warning("toast(on_main inline) 异常: %r", ex)
        return

    async def _go() -> None:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            log.warning("toast(on_main) 异常: %r", ex)

    try:
        page.run_task(_go)
    except Exception as ex:  # noqa: BLE001
        log.warning("toast 调度失败: %r", ex)


class _Lane:
    """一档提示（独立胶囊 + 独立 token）：轻提示 / 强提示各一"""

    __slots__ = ("icon", "text", "pill", "seq")

    def __init__(self, theme, *, size: int, icon_size: int,  # noqa: ANN001
                 pad_lr: int, pad_tb: int) -> None:
        self.seq = 0
        self.icon = ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=icon_size,
                            color=theme.c("text_on_accent"))
        self.text = ft.Text("", size=size, color=theme.c("text_on_accent"),
                            weight=ft.FontWeight.W_500)
        self.pill = ft.Container(
            content=ft.Row([self.icon, self.text], spacing=8, tight=True,
                           alignment=ft.MainAxisAlignment.CENTER,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(left=pad_lr, right=pad_lr, top=pad_tb, bottom=pad_tb),
            border_radius=999,
            bgcolor=theme.c("success"),
            shadow=ft.BoxShadow(blur_radius=14, spread_radius=0,
                                color="#40000000", offset=ft.Offset(0, 3)),
            opacity=0.0,
            offset=_OFFSET_HIDDEN,
            visible=False,
            animate_opacity=ft.Animation(_FADE_MS, ft.AnimationCurve.EASE_OUT),
            animate_offset=ft.Animation(_FADE_MS, ft.AnimationCurve.EASE_OUT),
        )


class ToastHost:
    """顶部居中浮层宿主（一个页面一个实例；内含轻 / 强两档）"""

    def __init__(self, theme) -> None:  # noqa: ANN001
        self._theme = theme
        self._light = _Lane(theme, size=_LIGHT_STYLE["size"], icon_size=_LIGHT_STYLE["icon"],
                            pad_lr=_LIGHT_STYLE["pad_lr"], pad_tb=_LIGHT_STYLE["pad_tb"])
        self._strong = _Lane(theme, size=_STRONG_STYLE["size"], icon_size=_STRONG_STYLE["icon"],
                             pad_lr=_STRONG_STYLE["pad_lr"], pad_tb=_STRONG_STYLE["pad_tb"])
        # 兼容属性（对外沿用旧的轻提示「胶囊」语义）
        self._pill = self._light.pill
        self._icon = self._light.icon
        self._text = self._light.text
        # 外层：定位（overlay 是 positioned host，用 top/left/right 贴顶居中）+ 可见性；
        # 两档在内部纵向排列（flet 中 visible=False 的控件不占位，故单档时原位显示）
        self.control = ft.Container(
            content=ft.Column([self._light.pill, self._strong.pill], spacing=8, tight=True,
                              horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            top=_TOP, left=0, right=0,
            alignment=ft.Alignment.TOP_CENTER,
            visible=False,
        )

    # ── 装配 ──
    def attach_to(self, page, host=None) -> None:  # noqa: ANN001
        """把浮层挂到渲染宿主：优先**主树 Stack**（flet 0.85 Web 端 page.overlay
        子树 toggling visible 后不重绘——真机点验 2026-10-14）；host=None 回退 overlay。"""
        (host if host is not None else page.overlay).append(self.control)
        page.update()

    # ── 显示 ──
    def show(self, page, text: str, ok: bool = True, strong: bool = False) -> None:  # noqa: ANN001
        """显示一条提示（线程安全；同档连续调用只保留最后一条）

        ``strong=True`` → 强提示档（更大、停留 4s），与轻提示**互不顶替**。
        """
        _on_main(page, lambda: self._show_now(page, text, ok, strong))

    def _show_now(self, page, text: str, ok: bool, strong: bool) -> None:  # noqa: ANN001
        lane = self._strong if strong else self._light
        duration = _STRONG_DURATION_MS if strong else _DURATION_MS
        lane.seq += 1
        token = lane.seq
        t = self._theme
        lane.icon.name = (ft.Icons.CHECK_CIRCLE_OUTLINE if ok
                          else ft.Icons.ERROR_OUTLINE)
        lane.icon.color = t.c("text_on_accent")
        lane.text.value = text
        lane.text.color = t.c("text_on_accent")
        lane.pill.bgcolor = t.c("success" if ok else "danger")
        lane.pill.visible = True
        lane.pill.opacity = 1.0
        lane.pill.offset = _OFFSET_SHOWN
        self._refresh(page)

        async def _hide() -> None:
            await asyncio.sleep(duration / 1000.0)
            if token != lane.seq:           # 已被同档后续提示顶替 → 让位，不误关新提示
                return
            lane.pill.opacity = 0.0
            lane.pill.offset = _OFFSET_HIDDEN
            self._refresh(page)
            await asyncio.sleep(_FADE_MS / 1000.0)
            if token != lane.seq:
                return
            lane.pill.visible = False
            self._refresh(page)

        try:
            page.run_task(_hide)
        except Exception as ex:  # noqa: BLE001
            log.warning("toast 定时隐藏调度失败: %r", ex)

    def _refresh(self, page) -> None:  # noqa: ANN001
        """同步外层可见性（任一模档可见即显示）并刷新浮层"""
        self.control.visible = self._light.pill.visible or self._strong.pill.visible
        self._update(page)

    def _update(self, page) -> None:  # noqa: ANN001
        """刷新浮层（overlay 子树；整页 update 保证 visible/值变更必达前端）"""
        try:
            page.update()
        except Exception:  # noqa: BLE001
            try:
                self.control.update()
            except Exception:  # noqa: BLE001
                pass


def attach(page, theme, host=None) -> ToastHost:  # noqa: ANN001
    """创建并装配 Toast（返回 host；main.py 组装时调用一次）

    ``host``：主树根 Stack 的 controls 列表（推荐，见 :meth:`attach_to`）。
    装配失败不抛（回退 SnackBar），但**必须留痕**——否则界面"看着正常其实没提示"
    正是本项目最忌讳的静默失败。
    """
    host_t = ToastHost(theme)
    try:
        host_t.attach_to(page, host)
    except Exception as ex:  # noqa: BLE001
        log.warning("toast 装配失败（将回退 SnackBar）: %r", ex)
    try:
        page._cartest_toast = host_t
        page._cartest_toast_theme = theme
    except Exception as ex:  # noqa: BLE001
        log.warning("toast 挂载到 page 失败（将回退 SnackBar）: %r", ex)
    return host_t


def toast(page, text: str, ok: bool = True, theme=None, strong: bool = False) -> None:  # noqa: ANN001
    """提示统一入口（各页面调用）

    ``strong=True`` → 强提示档（耗时动作完成）；默认轻提示档。
    已装配 host → 顶部浮层 Toast；未装配（单测 / 无壳）→ 回退 ``SnackBar``
    （``theme`` 用于回退时着色），保证调用点零分支。
    """
    host = getattr(page, "_cartest_toast", None)
    if host is not None:
        host.show(page, text, ok, strong)
        return

    t = theme if theme is not None else getattr(page, "_cartest_toast_theme", None)
    bgcolor = None
    if t is not None:
        try:
            bgcolor = t.c("success" if ok else "danger")
        except Exception:  # noqa: BLE001
            bgcolor = None
    try:
        kw = {"duration": _STRONG_DURATION_MS} if strong else {}
        page.show_dialog(ft.SnackBar(ft.Text(text), bgcolor=bgcolor, **kw))
    except Exception:  # noqa: BLE001
        pass
