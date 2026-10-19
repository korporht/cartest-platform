# -*- coding: utf-8 -*-
"""40 号批 4 验收 — 其余四页接入全局任务（monkey/push/logextract/recorder）

覆盖：
  [1] 四页均已「离页不停任务」（on_hide 移除 → on_detach）
  [2] monkey：一键流登记/注销 + 补设备 token（device_acquire/release）
  [3] push：经设备队列提交 + 登记 + 补 token
  [4] logextract：登记本地任务（KIND_LOCAL，不入设备队列）
  [5] recorder：登记 + build 回填（修「录制中离页静默」）
  [6] 行为级：本地任务经 stop_fn 可停（registry.stop → cancel 事件置位）

用法:
    python tests/test_batch4_tasks.py
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def _src(name: str) -> str:
    return (ROOT / "cartest" / "app" / "tools" / name).read_text(encoding="utf-8")


def main() -> int:
    print("=" * 70)
    print("40 号批 4 — 其余四页接入全局任务")
    print("=" * 70)

    monkey_src = _src("monkey_tab.py")
    push_src = _src("push_tab.py")
    lex_src = _src("logextract_tab.py")
    rec_src = _src("recorder_tab.py")

    # ── [1] 离页不停任务 ──
    for name, src in (("monkey", monkey_src), ("push", push_src),
                      ("logextract", lex_src), ("recorder", rec_src)):
        check(f"{name} 已无 on_hide（离页停任务口径移除）", "def on_hide" not in src)

    # ── [2] monkey ──
    check("monkey 一键流登记全局任务", '_task_register("monkey"' in monkey_src)
    check("monkey 结果回调注销任务", '_task_unregister("monkey"' in monkey_src)
    check("monkey 补设备 token", 'device_acquire("monkey")' in monkey_src
          and 'device_release("monkey")' in monkey_src)
    check("monkey build 回填登记", "tasks.get(\"monkey\") is None" in monkey_src)

    # ── [3] push ──
    check("push 经设备队列提交", '_submit_device_task("push"' in push_src)
    check("push 登记 + token", '_task_register("push"' in push_src
          and 'device_acquire("push")' in push_src
          and 'device_release("push")' in push_src)
    check("push worker 结束注销", '_task_unregister("push"' in push_src)

    # ── [4] logextract（本地任务）──
    check("logextract 登记本地任务", '_task_register("logextract"' in lex_src
          and "tasks_mod.KIND_LOCAL" in lex_src)
    check("logextract 进度写 handle", '_task_update("logextract"' in lex_src)
    check("logextract worker 结束注销", '_task_unregister("logextract"' in lex_src)

    # ── [5] recorder ──
    check("recorder 登记本地任务", '_task_register("recorder"' in rec_src)
    check("recorder 启停两端注销", rec_src.count('_task_unregister("recorder"') >= 2)
    check("recorder build 回填运行态", "self._rec.is_running()" in rec_src
          and "self._start_timer()" in rec_src)

    # ── [6] 行为级：本地任务 stop_fn 可停 ──
    ok6, err6 = True, ""
    try:
        from cartest.app.tasks import TaskHandle, TaskRegistry

        reg = TaskRegistry()
        cancel = threading.Event()
        reg.register(TaskHandle(key="recorder", label="PC 录屏",
                                kind="local", running=True,
                                stop_fn=cancel.set))
        check("登记后 running 可见", any(h.key == "recorder" for h in reg.running()))
        reg.stop("recorder")
        check("registry.stop → stop_fn（取消事件置位）", cancel.is_set())
        reg.unregister("recorder")
        check("注销后进历史", reg.get("recorder") is None
              and any(getattr(h, "key", "") == "recorder" for h in reg.history()))
    except Exception as ex:  # noqa: BLE001
        ok6, err6 = False, f"{type(ex).__name__}: {ex}"
    check("本地任务停止链路", ok6, err6)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("其余四页接入检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
