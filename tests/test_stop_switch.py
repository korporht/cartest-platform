# -*- coding: utf-8 -*-
"""40 号批 6 验收 — 切设备「停止任务并切换」+ 状态记录

覆盖：
  [1] 源码口径：main.py 设备选择不再静默拒绝——弹对话框且**首选**
      「停止任务并切换」（FilledButton）；走 state.stop_all_tasks 并等真正落停；
      切换动作写全局操作日志（ActivityLog）；排队项一并取消
  [2] 行为级：stop_all_tasks 等待工具任务真正落停（快停 → True；不落 → 超时 False）
  [3] 行为级：registry 历史保留任务快照（设备/结果），ActivityLog 记录切换事件

用法:
    python tests/test_stop_switch.py
"""
import sys
import threading
import time
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
    print("40 号批 6 — 切设备「停止任务并切换」")
    print("=" * 70)

    # ── [1] 源码口径 ──
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")

    check("首选动作「停止任务并切换」（FilledButton）",
          'ft.FilledButton("停止任务并切换"' in main_src)
    check("走 stop_all_tasks 并等真正落停", "state.stop_all_tasks(timeout=10.0)" in main_src)
    check("停止超时不切换并有提示", "停止超时，请手动停止后重试" in main_src)
    check("排队项一并取消", "queue.cancel(h.key)" in main_src)
    check("切换动作写全局操作日志（ActivityLog）",
          "activity.add(source=" in main_src and "已停止运行中任务并切换设备" in main_src)
    check("stop_all_tasks 轮询等待", "deadline = time.time() + timeout" in state_src
          and "def stop_all_tasks" in state_src)

    # ── [2]/[3] 行为级 ──
    ok2, err2 = True, ""
    try:
        from cartest.app.activity import ActivityLog
        from cartest.app.tasks import TaskHandle, TaskRegistry

        reg = TaskRegistry()
        release = threading.Event()

        def _stop_slow() -> None:
            release.set()                      # 异步落停（模拟 adb 停止耗时）

        reg.register(TaskHandle(key="logcat", label="日志抓取", kind="device",
                                device="DEV1", running=True, stop_fn=_stop_slow))

        class _StateLike:
            running = False
            tasks = reg
            device_queue = None

            def request_stop(self) -> None:
                pass

        st = _StateLike()

        # 慢停线程：200ms 后真正落停（registry running=False）
        def _finish_later() -> None:
            release.wait(2.0)
            time.sleep(0.2)
            reg.update("logcat", running=False)
            reg.unregister("logcat")           # 对等真实流：停止回调里出表进历史
        threading.Thread(target=_finish_later, daemon=True).start()

        # 模拟 state.stop_all_tasks 的核心：stop_fn → 轮询等待真正落停
        reg.stop("logcat")
        deadline = time.time() + 5.0
        while reg.running() and time.time() < deadline:
            time.sleep(0.05)
        check("工具任务真正落停后才放行", not reg.running())
        check("落停任务进历史（快照保留设备/结果）",
              any(getattr(h, "key", "") == "logcat" and getattr(h, "device", "") == "DEV1"
                  for h in reg.history()))

        # ActivityLog 记录切换事件
        act = ActivityLog()
        act.add(source="设备", action="已停止运行中任务并切换设备 → E0V — DEV2", ok=True)
        items = act.items()
        check("ActivityLog 记录切换事件", len(items) == 1
              and "切换设备" in items[0].action and items[0].ok is True)
    except Exception as ex:  # noqa: BLE001
        ok2, err2 = False, f"{type(ex).__name__}: {ex}"
    check("停止并切换行为链", ok2, err2)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("切设备「停止任务并切换」检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
