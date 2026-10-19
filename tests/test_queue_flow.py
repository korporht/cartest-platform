# -*- coding: utf-8 -*-
"""40 号批 3 验收 — 设备操作队列落地（logcat / pull / 灌入入队 + 自动出队）

覆盖：
  [1] 源码口径：log_tab（logcat/pull）与 single_inject 经 `_submit_device_task`
      提交；state 的 DeviceQueue 挂了 busy_provider（流水线运行中 → 排队）
  [2] DeviceQueue.busy_provider：流水线忙 → submit 返回 queued；闲 → started
  [3] DeviceQueue.drain()：公开出队入口（流水线结束时 kick，防队列滞留）
  [4] base_tab._submit_device_task / _queued_feedback：重复提交取消旧排队项
  [5] 行为级：SimTab 走 _submit_device_task → 忙时入队 → 队首完成自动出队执行

用法:
    python tests/test_queue_flow.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("40 号批 3 — 设备操作队列落地")
    print("=" * 70)

    # ── [1] 源码口径 ──
    log_src = (ROOT / "cartest" / "app" / "tools" / "log_tab.py").read_text(encoding="utf-8")
    inj_src = (ROOT / "cartest" / "app" / "tools" / "single_inject_tab.py").read_text(encoding="utf-8")
    base_src = (ROOT / "cartest" / "app" / "tools" / "base_tab.py").read_text(encoding="utf-8")
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")

    check("logcat 经队列提交", '_submit_device_task("logcat"' in log_src)
    check("pull 抓日志经队列提交", '_submit_device_task(f"pull:{ts}"' in log_src)
    check("单条灌入经队列提交", '_submit_device_task("inject"' in inj_src)
    check("排队有强提示反馈", "_queued_feedback(" in log_src and "_queued_feedback(" in inj_src)
    check("base_tab 有 _submit_device_task", "def _submit_device_task" in base_src)
    check("空闲路径不再直跑（防双重执行回归）",
          all('verdict == "bypass"' in s for s in (log_src, inj_src))
          and 'if self._submit_device_task' not in log_src
          and 'if self._submit_device_task' not in inj_src)
    check("state 队列挂 busy_provider（流水线互斥）", "busy_provider=lambda: self.running" in state_src)

    # ── [2] busy_provider ──
    from cartest.app.tasks import DeviceQueue, TaskHandle, TaskRegistry

    reg = TaskRegistry()
    pipeline_running = {"v": False}
    q = DeviceQueue(reg, device_provider=lambda: "DEV1",
                    busy_provider=lambda: pipeline_running["v"])
    ran: list[str] = []
    h1 = TaskHandle(key="inject", label="单条灌入")
    reg.register(h1)
    pipeline_running["v"] = True
    verdict = q.submit(h1, lambda: ran.append("inject"))
    check("流水线运行中 → 排队", verdict == "queued" and ran == [], verdict)
    h1.queued = True

    pipeline_running["v"] = False
    q.cancel("inject")                            # 排队项取消（而非 unregister——pending 元组仍在）
    h2 = TaskHandle(key="inject2", label="单条灌入")
    reg.register(h2)
    verdict2 = q.submit(h2, lambda: ran.append("inject2"))
    check("流水线结束 → 立即执行", verdict2 == "started" and ran == ["inject2"], str(ran))

    # ── [3] drain 公开入口 ──
    check("DeviceQueue 有公开 drain()", hasattr(q, "drain") and callable(q.drain))
    reg.update("inject2", running=False)          # 结束 → 触发完成监听（不出错即可）
    q.drain()                                     # 队列空 → no-op 不抛

    # ── [4]/[5] 行为级：走 _submit_device_task 的完整流程 ──
    ok5, err5 = True, ""
    try:
        from cartest.app.theme import AppTheme
        from cartest.app.tools.base_tab import BaseTab

        class _StubState:
            def __init__(self) -> None:
                self.theme = AppTheme()
                self.tasks = TaskRegistry()
                self.device = "DEV1"
                self.running = False
                self.device_queue = DeviceQueue(
                    self.tasks, device_provider=lambda: self.device,
                    busy_provider=lambda: self.running)

        class _StubPage:
            def run_thread(self, *a, **kw) -> None:  # noqa: ANN001
                pass
            def run_task(self, *a, **kw) -> None:  # noqa: ANN001
                pass
            def update(self) -> None:
                pass

        class _StubApp:
            def __init__(self) -> None:
                self.state = _StubState()
                self.page = _StubPage()
                self.theme = self.state.theme
                self.statuses: list[str] = []
            def set_status(self, text, _k=None) -> None:  # noqa: ANN001
                self.statuses.append(text)
            def notify_done(self, text, ok=True) -> None:  # noqa: ANN001
                self.statuses.append(text)

        class SimTab(BaseTab):
            """最小 tab：模拟一个同步设备任务（starter 里完成并注销）"""
            def __init__(self, app) -> None:  # noqa: ANN001
                super().__init__(app)
                self.ran: list[str] = []
            def build(self):  # noqa: ANN201
                import flet as ft
                return ft.Text("sim")
            def start(self, key: str, label: str) -> None:
                def starter():
                    self.ran.append(key)
                    self._task_unregister(key)     # 同步任务完成即出表
                verdict = self._submit_device_task(key, label, starter)
                if verdict == "queued":
                    self._queued_feedback(key, label)
                elif verdict == "bypass":
                    starter()

        app = _StubApp()
        tab = SimTab(app)
        tab.build()

        # 空闲 → 立即执行
        tab.start("pull:1", "抓日志")
        check("空闲提交 → 立即执行", tab.ran == ["pull:1"]
              and app.state.tasks.get("pull:1") is None)

        # 占一个运行中任务 → 第二个排队 → 第一个完成自动出队
        tab2 = SimTab(app)
        tab2.build()
        tab2.start("logcat", "日志抓取")
        tab2.ran.clear()
        # 手动把 logcat 变成长任务（模拟抓流中）：重新登记 running=True
        app.state.tasks.register(TaskHandle(
            key="logcat", label="日志抓取", kind="device", running=True,
            stop_fn=lambda: None))
        tab2.start("inject", "单条灌入")
        check("设备忙 → 排队", tab2.ran == [] and app.state.device_queue.pending()
              and app.state.device_queue.pending()[0].key == "inject")
        check("排队提示文案（待「日志抓取」）",
              any("已排队" in s and "日志抓取" in s for s in app.statuses),
              str(app.statuses[-1]))
        app.state.tasks.unregister("logcat")       # 抓取完成 → 完成监听 → 自动出队
        check("队首完成 → 自动出队执行", tab2.ran == ["inject"]
              and app.state.device_queue.pending() == [])

        # 重复提交同 key → 旧排队项被取消、换新
        tab3 = SimTab(app)
        tab3.build()
        app.state.tasks.register(TaskHandle(key="busy", label="占位", kind="device",
                                            running=True, stop_fn=lambda: None))
        tab3.start("inject", "单条灌入")
        tab3.start("inject", "单条灌入")
        pend = app.state.device_queue.pending()
        check("重复提交取消旧排队项", len(pend) == 1, str([p.key for p in pend]))
    except Exception as ex:  # noqa: BLE001
        ok5, err5 = False, f"{type(ex).__name__}: {ex}"
    check("队列落地行为（提交/排队/自动出队/重复提交）", ok5, err5)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("设备操作队列落地检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
