# -*- coding: utf-8 -*-
"""DeviceQueue 单元测试（40 号批 1 — 全局单一设备队列）

覆盖：
  [1] 设备空闲 → submit 立即启动（"started"）
  [2] 设备忙 → 入队（"queued"）；队首完成 → 自动出队执行
  [3] 取消排队项 → 永不执行
  [4] 设备漂移 → 出队时取消（starter 不调 + notify 提示）
  [5] 多任务串行：逐个出队，同一时刻只有一个在跑
  [6] local 任务在跑不阻塞 submit（不占设备队列）

用法:
    python tests/test_device_queue.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.tasks import (  # noqa: E402
    KIND_DEVICE, KIND_LOCAL, DeviceQueue, TaskHandle, TaskRegistry,
)

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class _Env:
    """测试环境：registry + queue + 可变当前设备"""

    def __init__(self, device: str = "A") -> None:
        self.registry = TaskRegistry()
        self.device = device
        self.queue = DeviceQueue(self.registry, device_provider=lambda: self.device)
        self.notes: list[str] = []
        self.queue.notify = self.notes.append

    def started(self) -> list[str]:
        return [h.key for h in self.registry.running() if h.kind == KIND_DEVICE]


def main() -> int:
    print("=" * 70)
    print("DeviceQueue（全局单一设备队列）— 40 号批 1")
    print("=" * 70)

    print("\n[1] 设备空闲 → 立即启动")
    env = _Env()
    calls: list[str] = []
    h1 = env.registry.register(TaskHandle(key="pull1", label="抓日志",
                                          kind=KIND_DEVICE, device="A"))
    r = env.queue.submit(h1, lambda: calls.append("pull1"))
    check("返回 started", r == "started", r)
    check("starter 已调", calls == ["pull1"])
    check("handle 进入 running", h1.running is True and h1.queued is False)

    print("\n[2] 设备忙 → 入队；队首完成自动出队")
    env = _Env()
    calls = []
    h1 = env.registry.register(TaskHandle(key="logcat", label="日志抓取",
                                          kind=KIND_DEVICE, device="A"))
    check("队首 submit → started",
          env.queue.submit(h1, lambda: calls.append("logcat")) == "started")
    h2 = env.registry.register(TaskHandle(key="inject", label="单条灌入",
                                          kind=KIND_DEVICE, device="A"))
    r = env.queue.submit(h2, lambda: calls.append("inject"))
    check("返回 queued", r == "queued", r)
    check("排队标志", h2.queued is True and h2.running is False)
    check("starter 未调", calls == ["logcat"], str(calls))
    check("head=运行中任务", env.queue.head().key == "logcat")
    env.registry.unregister("logcat")            # 队首完成
    check("队首完成 → 自动出队执行灌入",
          calls == ["logcat", "inject"], str(calls))
    check("出队后 running=True、queued=False",
          h2.running is True and h2.queued is False)
    check("head 变为灌入", env.queue.head().key == "inject")

    print("\n[3] 取消排队项 → 永不执行")
    env = _Env()
    calls = []
    h1 = env.registry.register(TaskHandle(key="t1", label="T1",
                                          kind=KIND_DEVICE, device="A"))
    env.queue.submit(h1, lambda: calls.append("t1"))
    h2 = env.registry.register(TaskHandle(key="t2", label="T2",
                                          kind=KIND_DEVICE, device="A"))
    env.queue.submit(h2, lambda: calls.append("t2"))
    check("取消成功", env.queue.cancel("t2") is True)
    env.registry.unregister("t1")
    check("取消项不执行", calls == ["t1"], str(calls))
    check("取消项移出 registry", env.registry.get("t2") is None)
    check("重复取消 → False", env.queue.cancel("t2") is False)

    print("\n[4] 设备漂移 → 出队取消")
    env = _Env(device="A")
    calls = []
    h1 = env.registry.register(TaskHandle(key="d1", label="D1",
                                          kind=KIND_DEVICE, device="A"))
    env.queue.submit(h1, lambda: calls.append("d1"))
    h2 = env.registry.register(TaskHandle(key="d2", label="D2",
                                          kind=KIND_DEVICE, device="A"))
    env.queue.submit(h2, lambda: calls.append("d2"))
    env.device = "B"                             # 队首完成前设备被切走
    env.registry.unregister("d1")
    check("漂移项不执行", calls == ["d1"], str(calls))
    check("notify 提示", any("已取消" in n for n in env.notes), str(env.notes))
    check("漂移项移出 registry", env.registry.get("d2") is None)

    print("\n[5] 多任务串行：逐个出队")
    env = _Env()
    calls = []
    h1 = env.registry.register(TaskHandle(key="s1", label="S1",
                                          kind=KIND_DEVICE, device="A"))
    env.queue.submit(h1, lambda: calls.append("s1"))
    for i in (2, 3, 4):
        hi = env.registry.register(TaskHandle(key=f"s{i}", label=f"S{i}",
                                              kind=KIND_DEVICE, device="A"))
        env.queue.submit(hi, lambda i=i: calls.append(f"s{i}"))
    check("排队 3 项", len(env.queue.pending()) == 3)
    env.registry.unregister("s1")
    check("s2 出队", calls == ["s1", "s2"] and len(env.queue.pending()) == 2)
    env.registry.unregister("s2")
    env.registry.unregister("s3")
    check("全部串行执行完", calls == ["s1", "s2", "s3", "s4"], str(calls))
    env.registry.unregister("s4")                # 队首（s4）也结束 → 队列真正清空
    check("队列清空", env.queue.pending() == [] and env.queue.head() is None)

    print("\n[6] local 任务在跑不阻塞 submit")
    env = _Env()
    calls = []
    env.registry.register(TaskHandle(key="local", label="本地提取",
                                     kind=KIND_LOCAL, running=True))
    h = env.registry.register(TaskHandle(key="dev", label="抓日志",
                                         kind=KIND_DEVICE, device="A"))
    r = env.queue.submit(h, lambda: calls.append("dev"))
    check("local 不占队列 → 直接启动", r == "started" and calls == ["dev"], r)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("DeviceQueue 检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
