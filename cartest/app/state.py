"""CarTest app — 应用状态

集中管理 UI 需要读写的状态：主题、设备、当前运行、阶段状态、日志缓冲，
并承载**流水线驱动**（启动 / 续跑 / 请求停止），使多个页面都能触发同一套流程。

设计约束：
  · 流水线跑在**后台线程** → 所有状态与日志更新必须线程安全
  · 回调只写 state，**不碰任何控件**；UI 刷新由 main.py 的 tick 循环统一调度
    （见 05-P3设计.md §4.3）
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from ..core import adb as adb_mod
from ..pipeline.context import PipelineStopped, STAGES, RunContext, stage_label
from ..pipeline.runner import params_from_dict, resume_pipeline, run_pipeline
from ..pipeline.stages import PipelineParams
from . import activity as activity_mod
from . import tasks as tasks_mod
from .theme import AppTheme


class LogBuffer:
    """线程安全日志缓冲（append 可在任意线程，drain 由 UI 定时调用）

    内部维护两份：
      · 待消费队列 —— tick 每 250ms drain 一次，喂给当前页面的日志面板
      · 历史快照   —— 供页面重建时回填（切页/切主题后日志面板不至于空白）

    模式来自 ``flet_Tool/log_buffer.py``（见 05-P3设计.md §4.3 复用清单）。
    """

    def __init__(self, maxlen: int = 3000) -> None:
        self._items: deque[tuple[str, str]] = deque(maxlen=maxlen)
        self._history: deque[tuple[str, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, text: str, tag: str = "info") -> None:
        with self._lock:
            self._items.append((tag, text))
            self._history.append((tag, text))

    def drain(self) -> list[tuple[str, str]]:
        """取出全部待显示日志（清空缓冲）"""
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def history(self) -> list[tuple[str, str]]:
        """已产生的全部日志（含已被消费的），供页面重建时回填"""
        with self._lock:
            return list(self._history)

    def clear(self) -> None:
        """清空待消费与历史（新一次运行时用，避免上一轮日志被回填回来）"""
        with self._lock:
            self._items.clear()
            self._history.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class AppState:
    """应用级状态（单例语义，由 main.py 创建后传给各页面）"""

    def __init__(self) -> None:
        self.theme = AppTheme()
        self.log = LogBuffer()

        # 设备（全局设备切换：顶栏为唯一入口，见 set_device）
        self.device: str = ""
        self.devices: list[str] = []
        #: 设备占用锁（长任务 token 集合；非空＝运行中，禁止切换设备）
        self._device_holds: set[str] = set()
        #: 设备变化回调：``fn(device: str)``（main.py 注册刷新顶栏；注册方负责回主线程）
        self.on_device_change: Callable[[str], None] | None = None

        # 当前运行
        self.current_run: RunContext | None = None
        self.running: bool = False
        #: 停止请求（阶段边界生效，见 PipelineStopped）
        self.stop_requested: bool = False
        self.stage_status: dict[str, str] = {s: "pending" for s in STAGES}
        self.stage_detail: dict[str, str] = {}

        #: 当前页面 key（侧栏直切；标签工作区已按用户反馈移除，2026-09-16）
        self.active_page: str = "tool_logcat"
        #: 侧栏「最近运行」→ 结果页选中的运行（UI-3 消费）
        self.selected_run_id: str = ""
        #: 运行胶囊目标回调与去抖缓存（由 main.py 注册）
        self.run_chip_target: Callable[[str], None] | None = None
        self.last_run_chip: str = ""
        #: 页内微动效钩子（tick 驱动；离开页面时由壳清空）——fn(tick: int)
        self.tick_hooks: list = []
        #: 应用级 UI 钩子（任务抽屉实时刷新等）——**切页不清空**
        # （tick_hooks 由 _detach_page 整表替换，页级钩子随页面生命周期；
        #  跨页存活的常驻浮层须挂这里，否则打开状态下冻结在旧快照）
        self.ui_hooks: list = []

        # 依赖注入（None = 用真实设备实现；填上后流水线走替身，见 tools/gui_demo.py）
        self.adb_factory: Callable[[], object] | None = None
        self.injector_factory: Callable[[], object] | None = None

        #: 上次使用的参数（切页重建面板时预填，避免重复输入）
        self.last_params: dict = {}

        #: 工具页输入态（key → Tab.save_state() 结果；切页/切主题后回填）
        self.tool_state: dict[str, dict] = {}
        #: 页面离开前回调（工具页借此保存输入态；由 _render() 在重建前调用）
        self.before_page_leave: Callable[[], None] | None = None

        #: 操作日志（事件）模型：``tool_key → OplogModel``——**会话级持有**，
        #: 使切页/切主题（页面重建、ToolContext 重建）后事件历史不丢
        #: （原先模型挂在 ToolContext 上，重建即清空；2026-09-18 用户反馈）
        self.oplog_models: dict = {}

        #: 全局操作日志：跨页汇总所有工具页的「动作完成/失败」事件（会话级、仅内存），
        #: 供顶栏「操作日志」抽屉一处回看（2026-10-14，见 39 号设计）
        self.activity: activity_mod.ActivityLog = activity_mod.ActivityLog()

        #: 全局任务三件套（40 号）：登记表 / 暂停门 / 设备队列。
        #: 工具页长任务登记到 ``tasks``（进程级，切页不停）；``device_queue`` 把
        #: 需独占设备的任务串行化（忙则排队、闲则自动出队）；
        #: ``pause_gate`` 供流水线在阶段边界暂停（与停止同级）。
        self.tasks: tasks_mod.TaskRegistry = tasks_mod.TaskRegistry()
        self.pause_gate: tasks_mod.PauseGate = tasks_mod.PauseGate()
        self.device_queue: tasks_mod.DeviceQueue = tasks_mod.DeviceQueue(
            self.tasks, device_provider=lambda: self.device,
            busy_provider=lambda: self.running)   # 流水线运行中 → 工具任务一律排队（40 号 §4.2）
        self.device_queue.notify = lambda msg: self.log.append(msg, "warn")

        # ── UI 对接（由 main.py 与页面注册，避免模块级全局变量）──
        #: 日志消费回调：``fn(lines: list[tuple[tag, text]]) -> None``
        self.log_target: Callable[[list[tuple[str, str]]], None] | None = None
        #: 主题切换回调（页面需重建时用）
        self.on_theme_change: Callable[[], None] | None = None
        #: 运行状态变化回调：``fn(running: bool)``（由 tick 轮询触发，页面注册）
        self.on_running_change: Callable[[bool], None] | None = None
        #: 上次 tick 观察到的 running（避免重复触发）
        self.last_running: bool = False
        #: 页面跳转回调：``fn(page_key: str) -> None``（由 main.py 注册）
        self.goto_page: Callable[[str], None] | None = None
        #: 设备接入轮询的上次快照（2026-10-19：新设备接入自动选中+切方案）
        self.polled_devices: set[str] = set()
        #: 打开「操作日志」抽屉回调：``fn() -> None``（由 main.py 装配后注册；
        #: 顶栏按钮经此调用，避免顶栏重建后闭包失效）
        self.open_activity: Callable[[], None] | None = None
        #: 重渲染当前页回调：``fn() -> None``（由 main.py 注册；配置页切方案后重载值用）
        self.refresh_page: Callable[[], None] | None = None

    # ── 便捷 ──────────────────────────────
    def reset_stages(self) -> None:
        self.stage_status = {s: "pending" for s in STAGES}
        self.stage_detail = {}

    def set_stage(self, stage: str, status: str, detail: str = "") -> None:
        if stage in self.stage_status:
            self.stage_status[stage] = status
            if detail:
                self.stage_detail[stage] = detail

    # ── 流水线驱动（后台线程；回调只写 state）──
    def _stage_cb(self, stage: str, status: str, detail: str) -> None:
        """阶段事件回调 —— 运行在后台线程，**不得触碰控件**"""
        if stage.startswith("_"):
            if detail:
                self.log.append(detail, "info")
            return
        # 停止请求在阶段边界生效（阶段原子，不打断）
        if status == "running" and self.stop_requested:
            raise PipelineStopped(f"已在 {stage} 阶段前停止")
        self.set_stage(stage, status, detail)
        # 40 号 §八：日志显示中文化（内部 key 与 run.json 不变）
        label = stage_label(stage)
        if status == "done":
            self.log.append(f"[{label}] {detail or '完成'}", "info")
        elif status == "failed":
            self.log.append(f"[{label}] 失败: {detail}", "error")
        else:
            self.log.append(f"[{label}] 运行中…", "cmd")

    def start_pipeline(self, params: PipelineParams, *, keep: bool = False) -> bool:
        """启动一次新运行（后台线程）。已有运行在跑时返回 False。"""
        if self.running:
            return False
        if self.device_queue.pending():        # 40 号 §4.2：队列有持设备的任务 → 拒绝
            self.log.append("设备队列有排队任务，请先停止/等队列完成再启动流水线", "warn")
            return False
        self._apply_injections(params)
        self.running = True
        self.stop_requested = False
        self.pause_gate.resume()               # 清残留暂停态（上次异常退出可能留下）
        self.reset_stages()
        threading.Thread(target=self._run_new, args=(params, keep),
                         daemon=True).start()
        return True

    def _apply_injections(self, params: PipelineParams) -> None:
        """把本会话的注入项补进 params（真实设备模式下工厂为 None，无副作用）"""
        if params.adb is None and self.adb_factory:
            params.adb = self.adb_factory()
        if params.injector_factory is None and self.injector_factory:
            params.injector_factory = self.injector_factory
        if params.on_log is None:
            params.on_log = self.log.append
        if params.stop_check is None:
            params.stop_check = lambda: self.stop_requested

    def resume_pipeline(self, run_id: str) -> bool:
        """从断点续跑（后台线程）。参数从 run.json 快照重建，注入项重新挂上。"""
        if self.running:
            return False
        if self.device_queue.pending():        # 40 号 §4.2：队列有持设备的任务 → 拒绝
            self.log.append("设备队列有排队任务，请先停止/等队列完成再续跑", "warn")
            return False
        # 续跑使用 run.json 快照的设备（非顶栏当前所选）——不一致时显式告警
        try:
            snap_dev = str(RunContext.open(run_id).device or "")
            if snap_dev and self.device and snap_dev != self.device:
                self.log.append(
                    f"注意：本次续跑使用运行快照的设备 {snap_dev}"
                    f"（顶栏当前所选 {self.device}）", "warn")
        except Exception:  # noqa: BLE001 - 快照读取失败不阻断续跑
            pass
        self.running = True
        self.stop_requested = False
        self.pause_gate.resume()               # 清残留暂停态
        self._restore_stages(run_id)
        threading.Thread(target=self._run_resume, args=(run_id,),
                         daemon=True).start()
        return True

    # ── 流水线暂停 / 继续（40 号 §五：与停止同级）──
    def pause_pipeline(self) -> bool:
        """暂停：线程在**阶段边界**挂起（不写 run.json、不产生断点），可继续可停止"""
        if not self.running or self.pause_gate.paused:
            return False
        self.pause_gate.pause()
        self.log.append("已请求暂停（当前阶段结束后在边界挂起；不产生断点）", "warn")
        return True

    def continue_pipeline(self) -> bool:
        """继续：释放暂停门，同一线程从挂起的阶段边界接着跑"""
        if not self.pause_gate.paused:
            return False
        self.pause_gate.resume()
        self.log.append("已继续", "info")
        return True

    def request_stop(self) -> None:
        """请求停止（当前阶段结束后生效；也可在阶段间隙立刻生效）

        40 号 §5.3：暂停中也可停止——置位后**释放暂停门**，让线程醒来到
        阶段边界抛 ``PipelineStopped``（中断阶段记 running＝断点，可续跑）。
        """
        if self.running:
            self.stop_requested = True
            self.pause_gate.resume()           # 暂停中停止 → 放行到边界
            self.log.append("已请求停止（当前阶段结束后生效）", "warn")

    def _restore_stages(self, run_id: str) -> None:
        """续跑前把已有阶段状态读进 UI（否则全显示 pending，看不出从哪继续）"""
        self.reset_stages()
        try:
            ctx = RunContext.open(run_id)
        except Exception:  # noqa: BLE001
            return
        for s in STAGES:
            st = ctx.stage(s)
            if st.status in ("done", "skipped", "failed", "running"):
                # running＝中断断点（灌入中途停止），同样回填 UI 保持一致
                self.stage_status[s] = st.status
                if st.detail:
                    self.stage_detail[s] = st.detail

    def _sync_stages_from(self, ctx: RunContext | None) -> None:
        """按 run.json 的真实阶段状态刷新 UI。

        中断时停止请求在阶段边界抛出，UI 那条路径没走到 set_stage，
        若不回读会让阶段条与真实断点不一致（run.json 记 running，界面显示 pending）。
        """
        if ctx is None:
            return
        for s in STAGES:
            st = ctx.stage(s)
            self.stage_status[s] = st.status
            if st.detail:
                self.stage_detail[s] = st.detail

    def _run_new(self, params: PipelineParams, keep: bool) -> None:
        try:
            ctx = run_pipeline(params, keep=keep, on_stage=self._stage_cb,
                               gate=self.pause_gate)
            self.current_run = ctx
            self.log.append(f"完成。运行目录: {ctx.run_dir}", "info")
        except PipelineStopped as ex:
            self.log.append(f"{ex}（可到历史页「续跑」）", "warn")
            self.current_run = self._latest_run()
            self._sync_stages_from(self.current_run)
        except Exception as ex:  # noqa: BLE001
            self.log.append(f"流水线中断: {ex}", "error")
            self.current_run = self._latest_run()
            self._sync_stages_from(self.current_run)
        finally:
            self.running = False
            self.pause_gate.resume()           # 清暂停态（暂停中停止/异常路径）
            self.device_queue.drain()          # 40 号 §4.2：流水线结束 → 放行排队任务

    def _run_resume(self, run_id: str) -> None:
        try:
            snapshot = RunContext.open(run_id)
            params = params_from_dict(snapshot.params)
            self._apply_injections(params)
            self.log.append(f"从断点续跑：{run_id}", "cmd")
            ctx = resume_pipeline(run_id, params, on_stage=self._stage_cb,
                                  gate=self.pause_gate)
            self.current_run = ctx
            self.log.append(f"完成。运行目录: {ctx.run_dir}", "info")
        except PipelineStopped as ex:
            self.log.append(f"{ex}（可到历史页「续跑」）", "warn")
            self.current_run = self._latest_run()
            self._sync_stages_from(self.current_run)
        except Exception as ex:  # noqa: BLE001
            self.log.append(f"续跑中断: {ex}", "error")
            self.current_run = self._latest_run()
            self._sync_stages_from(self.current_run)
        finally:
            self.running = False
            self.pause_gate.resume()           # 清暂停态（暂停中停止/异常路径）
            self.device_queue.drain()          # 40 号 §4.2：流水线结束 → 放行排队任务

    @staticmethod
    def _latest_run() -> RunContext | None:
        """最新一次运行（中断后仍能让结果页/产物区有内容可看）"""
        runs = RunContext.list_runs()
        if not runs:
            return None
        try:
            return RunContext.open(runs[0]["run_id"])
        except Exception:  # noqa: BLE001
            return None

    # ── 全局设备（顶栏切换为唯一入口）──────────────────
    def refresh_devices(self) -> list[str]:
        """读在线设备（可能失败，失败返回空列表）。

        注入了替身 adb 时走替身（优先 ``list_devices`` 多设备模拟，无则退
        ``check_device`` 单设备），使"设备"列表在无真机时也能如实显示。

        **保留当前所选**：设备离线时不静默重选（操作时显式报错优于悄悄换设备）；
        仅在"从未选择过"时留待调用方（main.py 恢复链）决定初值。
        """
        try:
            if self.adb_factory:
                fake = self.adb_factory()
                lister = getattr(fake, "list_devices", None)
                if callable(lister):
                    self.devices = [str(d) for d in (lister() or [])]
                else:
                    ok, dev = fake.check_device(force=True)
                    self.devices = [dev] if ok else []
            else:
                self.devices = adb_mod.session().list_devices()
        except Exception:  # noqa: BLE001
            self.devices = []
        return self.devices

    def can_switch_device(self) -> tuple[bool, str]:
        """能否切换设备：``(ok, 占用说明)``——有任务占用设备时禁止（40 号 §2.4）

        占用来源（按优先级拼提示）：
          ① 流水线运行中（``running`` 或 pipeline token / 暂停态）
          ② registry 里登记的 device 任务（跨页存活的工具任务）
          ③ 旧版 token（``_device_holds``，迁移期兼容）
          ④ 设备队列排队中（切走会取消排队项，一并禁止）
        """
        if self.running or "pipeline" in self._device_holds:
            return False, "流水线运行中"
        if self.pause_gate.paused:
            return False, "流水线已暂停"
        busy: list[str] = []
        for h in self.tasks.running():
            if h.kind == tasks_mod.KIND_DEVICE and h.key != "pipeline":
                busy.append(h.label)
        token_labels = {"logcat": "日志抓取进行中", "inject": "语音灌入进行中"}
        for t in sorted(self._device_holds):
            if t in token_labels:
                busy.append(token_labels[t])
        seen: list[str] = []
        for x in busy:
            if x and x not in seen:
                seen.append(x)
        if seen:
            return False, "、".join(seen)
        if self.device_queue.pending():
            return False, "设备队列排队中"
        return True, ""

    def stop_all_tasks(self, timeout: float = 10.0) -> bool:
        """停止所有占用设备的任务（切设备「停止并切换」/ 关窗用；40 号 §2.4）

        流水线走 ``request_stop``（阶段边界生效，可能需要等待当前阶段结束）；
        工具任务走 ``tasks.stop_all``（等 handle 真正落停）。
        返回是否在超时内全部停止——**调用方必须等 True 才能切设备**。
        """
        if self.running:
            self.request_stop()
        tools_ok = self.tasks.stop_all(timeout)
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            time.sleep(0.05)
        return tools_ok and not self.running

    def device_acquire(self, token: str) -> None:
        """长任务占用设备（运行中禁止切换；token：pipeline / inject / logcat）"""
        self._device_holds.add(token)

    def device_release(self, token: str) -> None:
        self._device_holds.discard(token)

    def set_device(self, dev: str, *, persist: bool = True) -> bool:
        """**全局切换设备**（唯一入口）。

        写 ``state.device`` → 同步 ``ANDROID_SERIAL``（裸 adb 命令全收敛；
        显式 ``-s`` 的链路不受影响）→ 失效 ADB 设备探测缓存（防 30s TTL 返回
        旧设备）→ 持久化 ``device.selected`` → 触发 ``on_device_change``
        （顶栏刷新；回调可能从工作线程触发，注册方负责回主线程）。

        运行中（长任务占用）拒绝切换并记日志，返回 False。
        """
        ok, reason = self.can_switch_device()
        if not ok:
            self.log.append(f"运行中（{reason}），暂不能切换设备", "warn")
            return False
        dev = str(dev or "").strip()
        self.device = dev
        if dev:
            os.environ["ANDROID_SERIAL"] = dev
        else:
            os.environ.pop("ANDROID_SERIAL", None)   # 防残留 env 把裸 adb 路由到离线设备
        try:
            adb_mod.session().invalidate_cache()
        except Exception:  # noqa: BLE001
            pass
        if persist:
            try:
                from ..core import config_center
                config_center.set("device.selected", dev)
            except Exception:  # noqa: BLE001 - 持久化失败不影响本次切换
                pass
        if self.on_device_change:
            try:
                self.on_device_change(dev)
            except Exception:  # noqa: BLE001
                pass
        return True

    # ── 壳辅助（UI-1）──────────────────────
    def recent_runs(self, limit: int = 5) -> list[dict]:
        """最近运行摘要（供侧栏渲染）。

        status：有 results.json → ``✓n ✗n``（有失败→danger，否则 success）；
        阶段含 failed → 失败（danger）；其余（未完成）→ 中断（warning）。
        """
        out: list[dict] = []
        for item in RunContext.list_runs()[:limit]:
            run_id = item["run_id"]
            status_text, status_key = "中断", "warning"
            try:
                ctx = RunContext.open(run_id)
                if ctx.results_json.is_file():
                    payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
                    status_text = f"✓{payload.get('pass', 0)} ✗{payload.get('fail', 0)}"
                    status_key = "danger" if payload.get("fail") else "success"
                elif any(ctx.stage(s).status == "failed" for s in STAGES):
                    status_text, status_key = "失败", "danger"
            except Exception:  # noqa: BLE001 — 坏运行不拖垮侧栏
                pass
            parts = run_id.split("_", 2)
            out.append({
                "run_id": run_id,
                "name": parts[2] if len(parts) == 3 else run_id,
                "created_at": item.get("created_at", ""),
                "size": item.get("size", 0),
                "status_text": status_text,
                "status_key": status_key,
            })
        return out

    def select_run(self, run_id: str) -> None:
        """选中某次运行（侧栏点击 → 结果页使用）"""
        self.selected_run_id = run_id

    @property
    def runs_root(self) -> Path:
        from ..pipeline.context import runs_dir
        return runs_dir()
