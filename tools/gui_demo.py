# -*- coding: utf-8 -*-
"""CarTest GUI 演示启动器 —— 带替身设备跑完整流水线（不连真机）

用途：无设备环境下的演示与验收。GUI 与 CLI 共用同一套替身装置。

用法::

    python tools/gui_demo.py --check              # CLI 自检：直接跑一遍七阶段
    python tools/gui_demo.py --web --port 8560    # Web GUI（配合浏览器自动化点击验收）
    python tools/gui_demo.py                      # 桌面窗口

替换范围（与 tests/test_pipeline_e2e.py 同思路，只替换设备边界）：
  · adb     —— ScriptedAdb：check_device / send_voice / device_timestamp /
               force_stop / pull_logs 全部按脚本表演
  · monitor —— SilentMonitor：不连真实 logcat

其余全部为真实实现：DataSource / Injector / Judger / 七阶段编排 / 报告产物。

日志脚本覆盖 5 条判定路径（对齐 demo_cases.json 的 5 条用例）：
  ① 正常链路（ASR+落域+回复）→ PASS
  ② ASR 有字但无回复、设备日志无落域 → FAIL
  ③ 大模型拒识 → FAIL（原因含"拒识"）
  ④ 生图兜底 → PASS
  ⑤ 实时判定无回复(FAIL) 但设备日志有落域 → judge 翻案 PASS 并标注
     （这是验收项 3「结果页显示判定依据差异」的演示素材）
"""
from __future__ import annotations

import argparse
import datetime
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.injector import Injector  # noqa: E402

CASE_FILE = Path(__file__).resolve().parent / "demo_cases.json"


#: 替身设备时钟基点（2026-10-10 动态化）：原为固定 "09-15"——会被流水线的
#: 设备日志**时间窗过滤**（_parse_device_logs 跳过"早于本次运行"的记录，防历史归档
#: 污染）误杀；改为"运行时 -5s"生成（设备时钟须早于日志行 ≥1.1s，日志行须晚于
#: 时间窗下界——两者叠加后对"构造 → 起跑"的间隔有 ~55s 宽容），既通过 Judger
#: 旧日志过滤，也通过运行时间窗。脚本须在 **DemoRuntime 构造时**生成
#: （勿用模块级快照——同一进程多轮运行会过期；多处复用时用 :func:`make_demo_assets`
#: 成对共享，脚本与时钟必须同源）。
def _demo_ts(offset: float = 0.0) -> str:
    """演示时间戳：运行时 -5s + offset 秒（``MM-DD HH:MM:SS.mmm``）"""
    t = datetime.datetime.now() - datetime.timedelta(seconds=5) + datetime.timedelta(seconds=offset)
    return t.strftime("%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def make_demo_assets() -> tuple[dict[str, list[str]], str]:
    """一次生成**配套**的（日志脚本, 设备时钟）——多处复用时成对共享（2026-10-10

    两侧（如 CLI 与 GUI 一致性对比）共享同一份素材时，时间戳方可比；
    单独共享其一会导致"日志行早于设备时钟"被 Judger 丢弃。
    """
    return make_log_script(), _demo_ts()


def _log_line(offset: float, tag: str, msg: str) -> str:
    """演示日志行（设备时钟与日志行同源：运行时 -30s 起）"""
    return f"{_demo_ts(offset)}  1  2 I {tag}: {msg}"


def make_log_script() -> dict[str, list[str]]:
    """运行时生成日志脚本（**每次 DemoRuntime 构造时调用**，时间戳当刻生成）"""
    return {
        "最近的新闻": [
            _log_line(1.1, "isssr_log", '{"normal_text":"最近的新闻"}'),
            _log_line(1.3, "ASS-CbmToolPkStrategy", "云端落域 query = [最近的新闻] sid = [S1] domain = [新闻]"),
            _log_line(2.5, "sparkSrResult", '{"srCode":20204,"nlp":{"text":"为您播报最近的新闻"}}'),
        ],
        # ② ASR 有字但无回复、设备日志亦无落域 → FAIL（合并逻辑保持 fail）
        "播放音乐": [
            _log_line(1.1, "isssr_log", '{"normal_text":"播放音乐"}'),
        ],
        "L2 是什么意思": [
            _log_line(1.5, "ASS-CbmToolPkStrategy", "handle sparkDenial 大模型拒识 query = [L2 是什么意思]"),
            _log_line(1.6, "isssr_log", '{"normal_text":"L2 是什么意思"}'),
        ],
        "画一只猫": [
            _log_line(1.2, "VoiceProcessor", "streamTextToPicture start"),
            _log_line(1.4, "isssr_log", '{"normal_text":"画一只猫"}'),
            _log_line(3.2, "DataPointImpl", '{"imageUrl":"http://cdn.example.com/a/cat.png"}'),
        ],
        # ⑤ 实时判定 fail（无回复），但设备日志有落域 → judge 以设备日志为准翻案 pass
        "导航去公司": [
            _log_line(1.1, "isssr_log", '{"normal_text":"导航去公司"}'),
            _log_line(1.3, "ASS-CbmToolPkStrategy", "云端落域 query = [导航去公司] sid = [S5] domain = [导航]"),
        ],
    }


# ── 替身：设备侧 ──────────────────────────────
class ScriptedAdb:
    """按脚本"表演"设备：发送 query 后把对应日志行推进队列（模拟设备回复）"""

    def __init__(self, log_q: queue.Queue, script: dict[str, list[str]],
                 dev_ts: str = "") -> None:
        self.q = log_q
        self.script = script
        self._dev_ts = dev_ts or _demo_ts()   # 构造时冻结（须早于日志行、晚于时间窗下界）
        self.sent: list[str] = []

    # stages / Injector 调用的接口面
    def check_device(self, force: bool = False) -> tuple[bool, str]:
        return True, "FAKE-DEV-001"

    #: 模拟在线设备（多设备演示全局设备切换；首台与 check_device 返回值一致）
    DEMO_DEVICES = ("FAKE-DEV-001", "FAKE-DEV-002")

    def list_devices(self) -> list[str]:
        return list(self.DEMO_DEVICES)

    def send_voice(self, text, device_id="", mode=3, on_log=None):
        self.sent.append(text)
        for line in self.script.get(text, []):
            self.q.put(("log", line))
            time.sleep(0.005)
        return True, "OK"

    def device_timestamp(self) -> str:
        return self._dev_ts

    def force_stop(self, package: str, device_id: str = "") -> bool:
        return True

    def pull_logs(self, out_dir, device_id: str = "") -> tuple[bool, str, int]:
        """写出一份模拟的 logcat 落盘文件（文件名对齐解析器契约：main_*.log）"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = sorted(l for group in self.script.values() for l in group)
        f = out_dir / "main_demo_0915.log"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True, "已保存 1/1", 1


class SilentMonitor:
    """不连真实 logcat：只提供 Injector 需要的接口"""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self.started = False

    def start(self, device_id: str = "") -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def restart(self, reason: str = "") -> str:
        return reason

    def health_check(self, idle_timeout: float = 25.0):
        return None

    def should_health_check(self, interval: float = 2.0) -> bool:
        return False

    @property
    def epoch(self) -> int:
        return 1

    @property
    def is_running(self) -> bool:
        return self.started


# ── 替身运行时：adb 与 injector 共享同一日志队列 ──
class DemoRuntime:
    """替身运行时。adb 是唯一实例：stages 从 params.adb 拿它，
    injector_factory 也必须用同一个（否则 Injector 会落到真实 adb）。"""

    def __init__(self, script: dict[str, list[str]] | None = None,
                 dev_ts: str = "") -> None:
        self.q: queue.Queue = queue.Queue(maxsize=1000)
        # 脚本与设备时钟在构造时生成（同一时间基准；勿用模块级快照——多轮运行会过期）；
        # 多处复用（如 CLI/GUI 对比）用 make_demo_assets() 成对共享
        self.script = script if script is not None else make_log_script()
        self.adb = ScriptedAdb(self.q, self.script, dev_ts or _demo_ts())

    def make_adb(self) -> ScriptedAdb:
        return self.adb

    def make_injector(self) -> Injector:
        return Injector(adb=self.adb, log_q=self.q,
                        monitor_factory=lambda q: SilentMonitor(q))


def build_state():
    """构造带替身注入的 AppState（每次调用 = 一个新会话的独立状态与替身运行时）"""
    from cartest.app.state import AppState

    rt = DemoRuntime()
    state = AppState()
    state.adb_factory = rt.make_adb
    state.injector_factory = rt.make_injector
    return state


# ── CLI 自检：不带 GUI 直接跑一遍七阶段 ──
def run_check() -> int:
    from cartest.pipeline.context import STAGES
    from cartest.pipeline.runner import run_pipeline
    from cartest.pipeline.stages import PipelineParams

    # 环境隔离（2026-10-10）：自检断言以「内置 e0v 默认」为前提——
    # 断开真实 settings.json 与项目档案，免受本机多平台切换（t28/t1ma）影响
    from cartest.core import config_center as _cc
    from cartest.core import project as _pj
    _iso = Path(tempfile.mkdtemp(prefix="cartest_gui_demo_iso_"))
    _cc.set_settings_file(_iso / "settings.json")
    _pj.set_projects_dir(_iso / "projects")

    print("=" * 70)
    print("CarTest GUI 演示装置 — CLI 自检（替身设备，七阶段全链路）")
    print("=" * 70)

    rt = DemoRuntime()
    params = PipelineParams(
        case_file=str(CASE_FILE), interval=0.05, timeout=6.0, settle_wait=0.2,
        adb=rt.make_adb(), injector_factory=rt.make_injector,
        on_log=lambda m, t="info": print(f"      [{t}] {m}"),
    )

    seen: dict[str, str] = {}

    def on_stage(stage: str, status: str, detail: str) -> None:
        if stage.startswith("_"):
            print(f"  {stage}: {status} {detail}")
            return
        if status == "done":
            seen[stage] = "done"
        print(f"  [{stage}] {status}: {detail}")

    with tempfile.TemporaryDirectory(prefix="cartest_gui_demo_") as tmp:
        # 用临时根目录跑自检，不污染真实 runs/
        # （2026-09-24）同理隔离 extract 报告输出：替身装置走真实 CbmLogextractor，
        # 不设 --out 会把演示报告写进工具的真实 reports/ —— 见 core/parser/offline
        os.environ["CARTEST_LOGEXTRACT_OUT"] = str(Path(tmp) / "extract_tool_out")
        try:
            ctx = run_pipeline(params, root=Path(tmp), on_stage=on_stage)
        finally:
            os.environ.pop("CARTEST_LOGEXTRACT_OUT", None)

        print()
        print("─" * 70)
        ok = True
        if len(seen) != len(STAGES):
            ok = False
            print(f"[FAIL] 阶段未全部完成: {sorted(seen)}")
        else:
            print(f"[PASS] 七阶段全部完成: {' → '.join(STAGES)}")

        import json
        payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        expect = {"1": "pass", "2": "fail", "3": "fail", "4": "pass", "5": "pass"}
        actual = {r["run_id"]: r["status"] for r in payload["results"]}
        if actual == expect:
            print(f"[PASS] 判定结果与预期一致: {actual}")
        else:
            ok = False
            print(f"[FAIL] 判定不符: 实际={actual} 预期={expect}")

        # 验收项 3 的演示素材：用例 5 必须触发"实时 fail → 设备日志为准翻案 pass"
        r5 = next((r for r in payload["results"] if r["run_id"] == "5"), {})
        if r5.get("live_status") == "fail" and r5.get("status") == "pass" and r5.get("note"):
            print("[PASS] 翻案场景（判定依据差异）: "
                  f"实时={r5['live_status']} → 设备日志落域={r5.get('device_domain')} "
                  f"→ 最终 {r5['status']}")
            print(f"       标注: {r5['note']}")
        else:
            ok = False
            print(f"[FAIL] 翻案场景未触发: {r5}")

        reports = sorted(ctx.report_dir.glob("*"))
        if reports:
            print(f"[PASS] 报告产物 {len(reports)} 个: {[f.name for f in reports]}")
        else:
            ok = False
            print("[FAIL] 无报告产物")

        runs = list(Path(tmp).glob("*"))
        print(f"      运行目录: {runs[0].name if runs else '(无)'}")

    print("─" * 70)
    print("自检通过" if ok else "自检失败")
    print("=" * 70)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/gui_demo.py",
                                     description="CarTest GUI 演示启动器（替身设备）")
    parser.add_argument("--check", action="store_true", help="只跑 CLI 自检，不启动 GUI")
    parser.add_argument("--web", action="store_true", help="GUI 以 Web 模式启动")
    parser.add_argument("--port", type=int, default=8560, help="Web 模式端口（默认 8560）")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()

    # 替身 GUI：会话级隔离 extract 报告输出（演示流水线不得写进真实 reports/）
    os.environ.setdefault(
        "CARTEST_LOGEXTRACT_OUT",
        tempfile.mkdtemp(prefix="cartest_gui_demo_out_"))

    from cartest.app.main import run_app

    print(f"替身模式启动：用例文件 {CASE_FILE.name}，设备 FAKE-DEV-001")
    run_app(web=args.web, port=args.port, state_factory=build_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
