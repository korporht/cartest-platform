# -*- coding: utf-8 -*-
"""CarTest app — 全局任务三件套（TaskRegistry / DeviceQueue / PauseGate）

``40-任务跨页存活与设备操作队列设计.md`` 的底座。纯逻辑、无 UI 依赖、可离线测试。

三个组件：
  · :class:`TaskRegistry` —— 全局任务登记（注册 / 字段更新 / 停止 / 快照 / 历史），
    是「顶栏任务指示器 / 切设备锁 / 工具页回填」三方的**单一事实源**；
  · :class:`DeviceQueue` —— **全局单一队列**：需独占设备的任务串行执行；
    忙时入队（调用方提示"已排队"），队首完成**自动出队执行**；
    出队前校验设备未漂移（``device_provider``），漂移则取消该项；
  · :class:`PauseGate` —— 暂停门：流水线在阶段边界 ``wait()``；暂停不产生断点。

线程模型（延续 34/39 号口径）：工作线程只改 handle 字段 / 写 ``state.log``；
UI 由 ``main._tick`` 读 :meth:`TaskRegistry.snapshot` 刷新，绝不直接改控件。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

#: 任务类别：需要独占设备（参与队列与切设备锁） / 纯本地 IO
KIND_DEVICE = "device"
KIND_LOCAL = "local"

#: 历史保留上限（超出裁最旧）
HISTORY_MAX = 200


@dataclass
class TaskHandle:
    """一个全局任务的登记项（字段可由工作线程随时更新）"""

    key: str                          # 唯一键（"logcat" / "inject" / "monkey" / "push:<ts>"…）
    label: str                        # 展示名（中文，如「日志抓取」）
    kind: str = KIND_DEVICE           # device / local
    device: str = ""                  # 提交时的设备序列号（出队漂移校验用）
    running: bool = False             # 是否正在执行（排队中为 False）
    queued: bool = False              # 是否在设备队列中排队
    pausable: bool = False
    progress: float = -1.0            # 0..1；-1 = 不确定
    detail: str = ""                  # 一行状态（如「已运行 120s」）
    stop_fn: Callable[[], None] | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    ok: bool | None = None            # 结束结果（None=未结束/已取消）
    data: dict = field(default_factory=dict)   # 自由载荷（任务产物摘要，供页面回填）
    _notified: bool = False           # 完成监听器是否已触发（内部）
    _was_started: bool = False        # 是否真正启动过（排队中≠启动；完成通知的前提）


class TaskRegistry:
    """全局任务登记表（线程安全；读取一律走快照）"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: dict[str, TaskHandle] = {}
        self._history: list[TaskHandle] = []
        self._finish_listeners: list[Callable[[str, TaskHandle], None]] = []

    # ── 监听 ──
    def add_finish_listener(self, cb: Callable[[str, TaskHandle], None]) -> None:
        """任务结束回调（key, handle）——设备队列靠它自动出队；重复注册忽略"""
        with self._lock:
            if cb not in self._finish_listeners:
                self._finish_listeners.append(cb)

    def _notify_finished(self, handle: TaskHandle) -> None:
        if handle._notified:
            return
        handle._notified = True
        for cb in list(self._finish_listeners):
            try:
                cb(handle.key, handle)
            except Exception:  # noqa: BLE001 - 监听器异常不拖垮登记表
                pass

    # ── 生命周期 ──
    def register(self, handle: TaskHandle) -> TaskHandle:
        """登记任务；同 key 旧任务若仍在跑则标记结束并进历史（防御性替换）"""
        with self._lock:
            old = self._handles.pop(handle.key, None)
            if old is not None and old.running:
                old.running = False
                if old.finished_at is None:
                    old.finished_at = time.time()
                self._history.append(old)
                self._notify_finished(old)
            if handle.running:
                handle._was_started = True
            self._handles[handle.key] = handle
        return handle

    def update(self, key: str, **fields) -> None:
        """工作线程更新任务字段；``running`` **True→False 转变**视为结束（触发完成监听）

        排队中（``running=False``）的字段更新**不会**触发完成监听——
        否则设备队列会把"入队"误判为"完成"而提前出队。
        """
        with self._lock:
            h = self._handles.get(key)
            if h is None:
                return
            was = h.running
            for k, v in fields.items():
                if hasattr(h, k):
                    setattr(h, k, v)
            if h.running:
                h._was_started = True
            if was and not h.running:
                if h.finished_at is None:
                    h.finished_at = time.time()
                self._notify_finished(h)

    def unregister(self, key: str) -> TaskHandle | None:
        """任务彻底结束 → 移出活动表、进历史（曾启动过才触发完成监听）"""
        with self._lock:
            h = self._handles.pop(key, None)
            if h is None:
                return None
            was = h.running
            h.running = False
            if h.finished_at is None:
                h.finished_at = time.time()
            self._history.append(h)
            if len(self._history) > HISTORY_MAX:
                del self._history[:-HISTORY_MAX]
        if was or h._was_started:
            self._notify_finished(h)
        return h

    # ── 查询 ──
    def get(self, key: str) -> TaskHandle | None:
        with self._lock:
            return self._handles.get(key)

    def running(self) -> list[TaskHandle]:
        """正在执行的任务（不含排队中）——浅拷贝列表，handle 本体只读约定"""
        with self._lock:
            return [h for h in self._handles.values() if h.running]

    def snapshot(self) -> list[dict]:
        """全部活动任务的只读快照（供 UI / tick 使用）"""
        with self._lock:
            return [dict(vars(h)) for h in self._handles.values()]

    def history(self) -> list[TaskHandle]:
        """已结束任务（新 → 旧）"""
        with self._lock:
            return list(reversed(self._history))

    # ── 停止 ──
    def stop(self, key: str) -> bool:
        """请求停止指定任务（调它的 stop_fn；实际结束由任务线程自行落）"""
        with self._lock:
            h = self._handles.get(key)
            fn = h.stop_fn if h is not None and h.running else None
        if fn is None:
            return False
        try:
            fn()
        except Exception:  # noqa: BLE001 - 停止失败交由任务自身超时兜底
            return False
        return True

    def stop_all(self, timeout: float = 10.0) -> bool:
        """对所有运行中任务发起停止，并**等待**其真正结束（轮询）。

        返回是否在超时内全部停止。注意：调用方（如"停止并切换"）必须等这里
        返回 True 才能继续依赖设备的操作——否则任务会在漂移后的设备上继续跑。
        """
        with self._lock:
            targets = [h for h in self._handles.values() if h.running and h.stop_fn]
        for h in targets:
            try:
                h.stop_fn()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return True
            time.sleep(0.05)
        return not bool(self.running())


class DeviceQueue:
    """全局**单一**设备队列：需独占设备的任务串行执行（40 号 §四）

    - ``submit``：设备空闲 → 立即 ``starter()``（返回 ``"started"``）；
      忙 → 入队并把 ``handle.queued`` 置 True（返回 ``"queued"``，调用方提示"已排队"）。
    - 队首任务结束（registry 完成监听触发）→ **自动出队**执行下一个。
    - 出队前校验 ``device_provider()`` 与 ``handle.device`` 一致；不一致 → **取消该项**
      （经 ``notify`` 提示）并继续看下一个。
    """

    def __init__(self, registry: TaskRegistry,
                 device_provider: Callable[[], str],
                 busy_provider: Callable[[], bool] | None = None) -> None:
        self._registry = registry
        self._device_provider = device_provider
        #: 额外忙源（40 号 §4.2）：流水线运行中 → 工具任务一律排队
        self._busy_provider = busy_provider
        self._lock = threading.RLock()
        self._pending: list[tuple[TaskHandle, Callable[[], None]]] = []
        self._draining = False                 # 防重入（starter 同步完成会递归触发）
        #: 队列事件文案回调（"已取消：设备已切换"等）——由 state 接到 state.log
        self.notify: Callable[[str], None] | None = None
        registry.add_finish_listener(self._on_finished)

    # ── 提交 ──
    def submit(self, handle: TaskHandle, starter: Callable[[], None]) -> str:
        """提交一个**已 register** 的任务；返回 ``"started"`` / ``"queued"``"""
        with self._lock:
            busy = [h for h in self._registry.running()
                    if h.kind == KIND_DEVICE] or bool(self._pending)
            if not busy and self._busy_provider is not None:
                try:
                    busy = bool(self._busy_provider())
                except Exception:  # noqa: BLE001 - 忙源失败按空闲处理
                    busy = False
            if not busy:
                self._start(handle, starter)
                return "started"
            handle.queued = True
            # 只标 queued、**不碰 running**——排队 ≠ 结束（update running=False 会误触完成监听）
            self._registry.update(handle.key, queued=True)
            self._pending.append((handle, starter))
            return "queued"

    def _start(self, handle: TaskHandle, starter: Callable[[], None]) -> None:
        handle.queued = False
        self._registry.update(handle.key, running=True, queued=False)
        try:
            starter()
        except Exception:  # noqa: BLE001 - 启动失败按结束处理，别堵死队列
            self._registry.update(handle.key, running=False, ok=False)
            raise

    # ── 自动出队 ──
    def _on_finished(self, key: str, handle: TaskHandle) -> None:
        if handle.kind != KIND_DEVICE:
            return
        self.drain()

    def drain(self) -> None:
        """尝试出队（公开：流水线结束时也调它，避免队列滞留；40 号 §4.2）"""
        self._drain()

    def _drain(self) -> None:
        if self._draining:                     # starter 同步完成会重入 → 交给外层循环
            return
        self._draining = True
        try:
            while True:
                with self._lock:
                    if not self._pending:
                        return
                    handle, starter = self._pending.pop(0)
                cur = str(self._device_provider() or "")
                if handle.device and cur and handle.device != cur:
                    # 设备已漂移：取消该项（不执行），继续看下一个
                    handle.queued = False
                    handle.ok = None
                    self._registry.unregister(handle.key)
                    if self.notify is not None:
                        try:
                            self.notify(f"排队任务「{handle.label}」已取消：设备已切换为 {cur}")
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                self._start(handle, starter)
                if handle.running:
                    return                     # 正常：队首在跑，等它完成再出队
                # starter 同步完成 → 继续出队下一个（防队列停摆）
        finally:
            self._draining = False

    # ── 查询 / 取消 ──
    def head(self) -> TaskHandle | None:
        """队首（若在跑则返回运行中的 device 任务，否则排队首项）"""
        with self._lock:
            run = [h for h in self._registry.running() if h.kind == KIND_DEVICE]
            if run:
                return run[0]
            return self._pending[0][0] if self._pending else None

    def pending(self) -> list[TaskHandle]:
        with self._lock:
            return [h for h, _ in self._pending]

    def cancel(self, key: str) -> bool:
        """取消排队项（运行中的请走 registry.stop）"""
        with self._lock:
            for i, (h, _starter) in enumerate(self._pending):
                if h.key == key:
                    self._pending.pop(i)
                    h.queued = False
                    self._registry.unregister(h.key)
                    return True
        return False


class PauseGate:
    """暂停门：``wait()`` 在阶段边界阻塞；暂停不产生断点（40 号 §五）"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._event.set()                     # 未暂停

    def pause(self) -> None:
        self._event.clear()

    def resume(self) -> None:
        self._event.set()

    @property
    def paused(self) -> bool:
        return not self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """未暂停立即返回 True；暂停中阻塞直到 resume/超时"""
        return self._event.wait(timeout)
