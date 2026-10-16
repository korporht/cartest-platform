# -*- coding: utf-8 -*-
"""TaskRegistry 单元测试（40 号批 1 — 全局任务登记表）

覆盖：
  [1] register / update / get / snapshot 字段往返
  [2] 同 key 重复注册：旧 running 任务标记结束 + 进历史 + 触发完成监听
  [3] unregister：移出活动表 → 历史 + 完成监听
  [4] update(running=False)：完成监听恰好触发一次
  [5] stop：调 stop_fn；无 stop_fn / 已结束 → False
  [6] stop_all：任务自行结束 → True
  [7] stop_all：任务不结束 → 超时 False
  [8] 并发 update 线程安全
  [9] history 上限裁剪

用法:
    python tests/test_task_registry.py
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.tasks import (  # noqa: E402
    KIND_DEVICE, KIND_LOCAL, TaskHandle, TaskRegistry,
)

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("TaskRegistry（全局任务登记表）— 40 号批 1")
    print("=" * 70)

    print("\n[1] register / update / get / snapshot")
    reg = TaskRegistry()
    h = reg.register(TaskHandle(key="logcat", label="日志抓取", kind=KIND_DEVICE,
                                device="641e23b", running=True))
    check("register 返回同一 handle", reg.get("logcat") is h)
    check("running() 含该任务", [x.key for x in reg.running()] == ["logcat"])
    reg.update("logcat", progress=0.5, detail="已运行 60s")
    check("update 字段生效", h.progress == 0.5 and h.detail == "已运行 60s")
    snap = reg.snapshot()
    check("snapshot 只读快照含字段", len(snap) == 1 and snap[0]["key"] == "logcat"
          and snap[0]["progress"] == 0.5)
    check("update 不存在的 key 不抛", reg.update("nope", progress=1) is None)

    print("\n[2] 同 key 重复注册：旧任务结束 + 历史 + 监听")
    reg = TaskRegistry()
    fired: list[str] = []
    reg.add_finish_listener(lambda k, hh: fired.append(k))
    reg.register(TaskHandle(key="x", label="旧", running=True))
    reg.register(TaskHandle(key="x", label="新", running=True))
    check("旧任务被标记结束并进历史",
          len(reg.history()) == 1 and reg.history()[0].label == "旧")
    check("完成监听触发一次", fired == ["x"], str(fired))
    check("活动表里是新任务", reg.get("x").label == "新")

    print("\n[3] unregister：历史 + 监听")
    reg = TaskRegistry()
    fired = []
    reg.add_finish_listener(lambda k, hh: fired.append(k))
    reg.register(TaskHandle(key="a", label="A", running=True))
    got = reg.unregister("a")
    check("unregister 返回 handle 且已结束",
          got is not None and got.running is False and got.finished_at is not None)
    check("进历史", len(reg.history()) == 1)
    check("完成监听触发", fired == ["a"])
    check("再 unregister 返回 None", reg.unregister("a") is None)

    print("\n[4] update(running=False)：监听恰好一次")
    reg = TaskRegistry()
    fired = []
    reg.add_finish_listener(lambda k, hh: fired.append(k))
    reg.register(TaskHandle(key="b", label="B", running=True))
    reg.update("b", running=False)
    reg.update("b", running=False)          # 重复
    reg.unregister("b")
    check("完成监听只触发一次", fired == ["b"], str(fired))

    print("\n[5] stop：调 stop_fn")
    reg = TaskRegistry()
    calls: list[str] = []
    reg.register(TaskHandle(key="c", label="C", running=True,
                            stop_fn=lambda: calls.append("c")))
    reg.register(TaskHandle(key="d", label="D", running=True))   # 无 stop_fn
    check("stop 调 stop_fn", reg.stop("c") is True and calls == ["c"])
    check("无 stop_fn → False", reg.stop("d") is False)
    check("不存在的 key → False", reg.stop("nope") is False)

    print("\n[6] stop_all：任务自行结束 → True")
    reg = TaskRegistry()
    stopped: list[str] = []

    def _worker(key: str) -> None:
        h = TaskHandle(key=key, label=key, running=True,
                       stop_fn=lambda: stopped.append(key))
        reg.register(h)
        if key == "slow":
            time.sleep(0.3)                 # stop 后稍等再结束
        reg.update(key, running=False)

    t = threading.Thread(target=_worker, args=("slow",), daemon=True)
    t.start()
    time.sleep(0.05)
    ok = reg.stop_all(timeout=3.0)
    t.join()
    check("stop_all 全部停止", ok is True and stopped == ["slow"], str(stopped))

    print("\n[7] stop_all：任务不结束 → 超时 False")
    reg = TaskRegistry()
    reg.register(TaskHandle(key="stubborn", label="不退", running=True,
                            stop_fn=lambda: None))
    t0 = time.time()
    ok = reg.stop_all(timeout=0.4)
    check("超时返回 False 且耗时≈timeout",
          ok is False and 0.3 <= time.time() - t0 < 1.5,
          f"{time.time() - t0:.2f}s")

    print("\n[8] 并发 update 线程安全")
    reg = TaskRegistry()
    reg.register(TaskHandle(key="hot", label="并发", running=True))
    errors: list[str] = []

    def _hammer(i: int) -> None:
        try:
            for j in range(200):
                reg.update("hot", progress=(i * 200 + j) / 1000.0, detail=f"{i}-{j}")
                reg.snapshot()
                reg.running()
        except Exception as ex:  # noqa: BLE001
            errors.append(repr(ex))

    threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("6 线程 × 200 次无异常", not errors, "; ".join(errors[:3]))

    print("\n[9] history 上限裁剪")
    reg = TaskRegistry()
    for i in range(260):
        reg.register(TaskHandle(key=f"t{i}", label=f"T{i}", running=True))
        reg.unregister(f"t{i}")
    check("history ≤ 200", len(reg.history()) <= 200, str(len(reg.history())))
    check("保留最新", reg.history()[0].key == "t259")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("TaskRegistry 检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
