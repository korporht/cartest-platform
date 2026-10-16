# -*- coding: utf-8 -*-
"""全局设备切换测试（2026-10-10）— 38 号设计

覆盖：
  [1] set_device 中枢：state / env / 持久化 / 回调
  [2] 运行中锁定：流水线 running、长任务占用（inject / logcat）→ 拒绝切换
  [3] refresh_devices：保留所选（离线不静默重选）；替身 list_devices / 旧替身回退
  [4] 项目联动：alias_for_serial（命中 / name 回退 / 未命中）；set_active 监听器
  [5] stages prepare：指定设备离线 → 显式报错（不假过）；在线 → 通过
  [6] 续跑设备不一致警告（state.resume_pipeline）

用法:
    python tests/test_device_switch.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.app.state import AppState  # noqa: E402
from cartest.core import config_center, project  # noqa: E402
from cartest.pipeline.context import RunContext  # noqa: E402
from cartest.pipeline.stages import PipelineParams, stage_prepare  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeAdbMulti:
    """替身：多设备在线"""

    def check_device(self, force: bool = False) -> tuple[bool, str]:
        return True, "DEV-A"

    def list_devices(self) -> list[str]:
        return ["DEV-A", "DEV-B"]


class FakeAdbOld:
    """旧替身：只有 check_device（无 list_devices 能力）"""

    def check_device(self, force: bool = False) -> tuple[bool, str]:
        return True, "DEV-1"


def main() -> int:
    tmp = Path(mkdtemp(prefix="device_switch_"))
    config_center.set_settings_file(tmp / "settings.json")   # 隔离：不碰真实 settings
    project.set_projects_dir(tmp / "projects")
    os.environ["CARTEST_RUNS_DIR"] = str(tmp / "runs")
    env_backup = os.environ.get("ANDROID_SERIAL")

    print("=" * 70)
    print("全局设备切换测试 — set_device / 锁定 / 联动 / prepare 校验")
    print("=" * 70)
    try:
        # ── [1] set_device 中枢 ──
        print("\n[1] set_device 中枢")
        state = AppState()
        state.devices = ["DEV-A", "DEV-B"]
        changed: list[str] = []
        state.on_device_change = changed.append
        ok = state.set_device("DEV-B")
        check("切换成功", ok is True)
        check("state.device 更新", state.device == "DEV-B")
        check("环境变量同步", os.environ.get("ANDROID_SERIAL") == "DEV-B")
        check("持久化 device.selected",
              config_center.get("device.selected", None) == "DEV-B")
        check("回调触发", changed == ["DEV-B"], str(changed))

        ok2 = state.set_device("")
        check("清空切换成功", ok2 is True and state.device == "")
        check("清空时 env 移除", "ANDROID_SERIAL" not in os.environ)

        # ── [2] 运行中锁定 ──
        print("\n[2] 运行中锁定")
        state.set_device("DEV-A")
        state.running = True
        ok3 = state.set_device("DEV-B")
        check("流水线运行中拒绝切换", ok3 is False and state.device == "DEV-A")
        state.running = False

        state.device_acquire("inject")
        ok4, reason = state.can_switch_device()
        check("长任务占用时拒绝", ok4 is False and "灌入" in reason, reason)
        state.device_acquire("logcat")
        ok5, reason5 = state.can_switch_device()
        check("多占用合并说明", ok5 is False and "灌入" in reason5 and "日志" in reason5,
              reason5)
        state.device_release("inject")
        state.device_release("logcat")
        ok6, _ = state.can_switch_device()
        check("释放后允许", ok6 is True)
        ok7 = state.set_device("DEV-B")
        check("释放后切换成功", ok7 is True and state.device == "DEV-B")

        # ── [3] refresh_devices 保留所选 ──
        print("\n[3] refresh_devices 保留所选")
        s2 = AppState()
        s2.adb_factory = lambda: FakeAdbMulti()
        s2.device = "DEV-C"                       # 当前离线（不在列表）
        devices = s2.refresh_devices()
        check("列表来自替身 list_devices", devices == ["DEV-A", "DEV-B"], str(devices))
        check("所选保留（不静默重选）", s2.device == "DEV-C")

        s3 = AppState()
        s3.adb_factory = lambda: FakeAdbOld()
        check("旧替身回退单设备", s3.refresh_devices() == ["DEV-1"])

        # ── [4] 项目联动（alias / 监听器）──
        print("\n[4] 项目联动（alias / 监听器）")
        proj = tmp / "projects"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "plt2.json").write_text(json.dumps({
            "key": "plt2", "name": "PLT2（测试台架）",
            "device": {"serial": "DEV-PLT2", "alias": "PLT2X"}}, ensure_ascii=False),
            encoding="utf-8")
        (proj / "t2.json").write_text(json.dumps({
            "key": "t2", "name": "T2（无别名）",
            "device": {"serial": "DEV-T2"}}, ensure_ascii=False), encoding="utf-8")
        check("alias 命中（device.alias 优先）",
              project.alias_for_serial("DEV-PLT2") == "PLT2X")
        check("alias 回退 name 前缀", project.alias_for_serial("DEV-T2") == "T2")
        check("未命中返回空串", project.alias_for_serial("DEV-NONE") == "")

        events: list[str] = []
        project.add_active_listener(events.append)
        try:
            project.set_active("plt2")
            check("set_active 触发监听", events == ["plt2"], str(events))
            project.set_active("plt2")
            check("重复 set 不触发", events == ["plt2"], str(events))
            project.set_active("t2")
            check("再切换再触发", events == ["plt2", "t2"], str(events))
        finally:
            project.remove_active_listener(events.append)

        # ── [5] stages prepare 指定设备校验 ──
        print("\n[5] stages prepare 指定设备校验")
        cases = tmp / "cases.json"
        cases.write_text(json.dumps([{"id": "1", "query": "你好", "category": "t"}],
                                    ensure_ascii=False), encoding="utf-8")

        def run_prepare(device: str) -> str:
            ctx = RunContext.create(str(cases), device=device, root=tmp / "runs")
            p = PipelineParams(case_file=str(cases), device=device, adb=FakeAdbMulti())
            return stage_prepare(ctx, p)

        try:
            run_prepare("DEV-OFF")
            check("指定离线设备应报错", False)
        except RuntimeError as ex:
            check("指定离线设备显式报错",
                  "不在线" in str(ex) and "DEV-OFF" in str(ex), str(ex))
        detail = run_prepare("DEV-A")
        check("在线设备通过", "用例" in detail, detail)

        # ── [6] 续跑设备不一致警告 ──
        print("\n[6] 续跑设备不一致警告")
        s4 = AppState()
        s4.device = "DEV-NOW"
        ctx2 = RunContext.create(str(cases), device="DEV-OLD", root=tmp / "runs")
        ok8 = s4.resume_pipeline(ctx2.run_id)
        check("续跑已启动", ok8 is True)
        texts = [t for _tag, t in s4.log.history()]
        check("含不一致警告",
              any("DEV-OLD" in t and "DEV-NOW" in t for t in texts), str(texts[-3:]))
        deadline = time.time() + 5              # 等后台线程收尾（用例快照空 → 快速结束）
        while s4.running and time.time() < deadline:
            time.sleep(0.1)
    finally:
        if env_backup is None:
            os.environ.pop("ANDROID_SERIAL", None)
        else:
            os.environ["ANDROID_SERIAL"] = env_backup
        os.environ.pop("CARTEST_RUNS_DIR", None)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全局设备切换测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
