# -*- coding: utf-8 -*-
"""Tab 基类 - 消除各 Tab 间的重复样板代码"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, TypeVar

import flet as ft

from .. import tasks as tasks_mod

T = TypeVar("T")


class BaseTab:
    """所有 Tab 的公共基类，提供线程安全的日志、状态更新、控件保护"""

    def __init__(self, app: Any) -> None:
        """初始化基类。

        Args:
            app: ``MainApp`` 实例，提供 ``page``、``theme``、``cfg`` 等。
        """
        self.app = app
        self.t = app.theme
        self._tick_fns: list = []          # 本 tab 注册的 tick 刷新器（detach 时退订）

    def build(self) -> ft.Control:
        """构建 Tab 的 UI 控件树。子类必须实现。"""
        raise NotImplementedError

    def on_hide(self) -> None:
        """Tab 隐藏时调用（后台任务清理）。

        .. deprecated:: 40 号起离页**不再停任务**——页壳改调 :meth:`on_detach`；
           本方法仅作为旧宿主/测试替身的兼容回退保留。
        """

    def on_detach(self) -> None:
        """页面卸载时**只解绑 UI，不停任务**（40 号新语义）。

        任务生命周期归 ``state.tasks``（registry）：
        真正停止仅四场景——用户点停止 / 关窗 / 切设备「停止并切换」/ 进程退出。
        子类应把原 ``on_hide`` 里的"停任务"逻辑移到任务的 ``stop_fn``；
        这里只做退订 tick 刷新器等 UI 清理。页面重建时 :meth:`build` 从
        handle 回填运行态（进度/按钮态），保证"切走再切回"状态一致。
        """
        hooks = getattr(self.app.state, "tick_hooks", None)
        if hooks is not None:
            for fn in self._tick_fns:
                try:
                    hooks.remove(fn)
                except ValueError:
                    pass

    def _tick_subscribe(self, fn: Callable[[int], None]) -> None:
        """订阅主循环 tick（250ms）；本 tab 离页时由 :meth:`on_detach` 自动退订

        tick 刷新器**只读 handle/模型改控件值**（不动结构、不重建控件）——
        离页后 handle 仍在 registry，切回页面重建时由 build 恢复订阅。
        """
        if fn not in self._tick_fns:
            self._tick_fns.append(fn)
        hooks = getattr(self.app.state, "tick_hooks", None)
        if hooks is not None and fn not in hooks:
            hooks.append(fn)

    def on_show(self) -> None:
        """Tab 显示时调用（恢复状态）。"""

    def save_state(self) -> dict:
        """保存运行时状态（主题切换前调用）。"""
        return {}

    def restore_state(self, state: dict) -> None:
        """恢复运行时状态（主题切换后调用）。"""

    def _safe_update(self, ctrl: ft.Control) -> None:
        """从后台线程安全更新控件。"""
        try:
            ctrl.update()
            self.app.page.update()
        except Exception:
            pass

    def _run_in_thread(
        self, target: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> threading.Thread:
        """启动一个 daemon 线程执行 ``target``。"""
        t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
        t.start()
        return t

    def _thread_safe_set(self, ctrl: ft.Control, **attrs: Any) -> None:
        """线程安全地设置控件属性并更新。"""
        try:
            for k, v in attrs.items():
                setattr(ctrl, k, v)
            ctrl.update()
            self.app.page.update()
        except Exception:
            pass

    def _done(self, text: str, ok: bool = True) -> None:
        """耗时动作**完成反馈**（强提示浮层 + 全局操作日志）

        宿主（``ToolContext``）实现 ``notify_done``；测试替身 / 旧宿主没有该方法时
        静默跳过（与 ``snack`` 的 hasattr 保护同口径）。文案只报结果
        （如「抓日志完成」）。线程安全由 ``notify_done`` 内部保证。
        """
        fn = getattr(self.app, "notify_done", None)
        if fn is not None:
            fn(text, ok)

    # ── 全局任务（40 号）：登记 / 更新 / 注销 ──
    def _task_register(self, key: str, label: str, *, kind: str = tasks_mod.KIND_DEVICE,
                       stop_fn: Callable[[], None] | None = None,
                       pausable: bool = False) -> None:
        """把本 Tab 正在启动的长任务登记到全局 registry（跨页存活）

        handle 的 ``stop_fn`` 由任务的实际停止函数担任——顶栏任务抽屉、
        切设备「停止并切换」都经 ``registry.stop(key)`` 调到它。
        """
        reg = getattr(self.app.state, "tasks", None)
        if reg is None:
            return
        dev = str(getattr(self.app.state, "device", "") or "")
        reg.register(tasks_mod.TaskHandle(
            key=key, label=label, kind=kind, device=dev, running=True,
            pausable=pausable, stop_fn=stop_fn))

    def _task_update(self, key: str, **fields) -> None:
        """更新任务字段（progress/detail/running…；工作线程安全）"""
        reg = getattr(self.app.state, "tasks", None)
        if reg is not None:
            reg.update(key, **fields)

    def _task_unregister(self, key: str) -> None:
        """任务彻底结束 → 移出活动表、进历史"""
        reg = getattr(self.app.state, "tasks", None)
        if reg is not None:
            reg.unregister(key)

    def _submit_device_task(self, key: str, label: str,
                            starter: Callable[[], None]) -> str:
        """把一个设备任务交给全局队列（40 号 §四）

        返回 ``"started"``（队列已执行 starter，**调用方勿再调**）/ ``"queued"``
        （排队中，调用方应提示）/ ``"bypass"``（无 registry/queue 底座——测试替身，
        调用方自行直跑 starter）。
        同 key 的旧排队项先取消（重新提交语义）。
        """
        state = self.app.state
        reg = getattr(state, "tasks", None)
        queue = getattr(state, "device_queue", None)
        if reg is None or queue is None:
            return "bypass"                       # 无底座（测试替身）→ 调用方直跑
        queue.cancel(key)                         # 幂等：重复提交 = 取旧排新
        dev = str(getattr(state, "device", "") or "")
        handle = tasks_mod.TaskHandle(key=key, label=label,
                                      kind=tasks_mod.KIND_DEVICE, device=dev)
        reg.register(handle)
        return queue.submit(handle, starter)      # "started" / "queued"

    def _queued_feedback(self, key: str, label: str) -> None:
        """排队强提示（40 号 §4.3：强提示 + 写全局操作日志）"""
        queue = getattr(self.app.state, "device_queue", None)
        head = queue.head() if queue is not None else None
        blocker = (getattr(head, "label", "") or "") \
            if head is not None and getattr(head, "key", "") != key else ""
        if blocker:
            text = f"{label}已排队：待「{blocker}」完成后自动执行"
        elif getattr(self.app.state, "running", False):
            text = f"{label}已排队：流水线运行中，结束后自动执行"
        else:
            text = f"{label}已排队：待前序任务完成后自动执行"
        self._done(text)
        self.app.set_status(text, "warning")
