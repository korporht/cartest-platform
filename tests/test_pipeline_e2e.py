# -*- coding: utf-8 -*-
"""P1 端到端验收：无设备跑通完整流水线

**这是 P1 的最终交付验证。**

链路：用例 → Injector 编排 → 灌入（替身）→ 日志进队 → Judger 判定 →
      结果回填 → ProgressStore 落盘 → 断点续跑

只有两处是替身：
  · ``adb``     —— 不发真实命令，按脚本把"设备日志行"推进队列
  · ``monitor`` —— 不连真实 logcat

其余全部是 core 的真实实现：``DataSource`` / ``Injector`` / ``Judger`` /
``ProgressStore``。判定走的是真实的 7 级优先级链。

用法:
    python tests/test_pipeline_e2e.py
    pytest tests/test_pipeline_e2e.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core.injector import InjectMode, Injector, InjectState   # noqa: E402
from cartest.core.progress import ProgressStore, pairs_from_cases     # noqa: E402
from cartest.core.source import DataSource, TestCase                  # noqa: E402

FAILED = []
DEV_TS = "09-15 10:00:00.000"


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


# ── 替身：按脚本"表演"设备 ────────────────────
class ScriptedAdb:
    """发送 query 后，把该 query 对应的日志行推入队列（模拟设备回复）"""

    def __init__(self, log_q, script: dict):
        self.q = log_q
        self.script = script
        self.sent: list[str] = []

    def send_voice(self, text, device_id="", mode=3, on_log=None):
        self.sent.append(text)
        for line in self.script.get(text, []):
            self.q.put(("log", line))
            time.sleep(0.005)
        return True, "OK"

    def device_timestamp(self):
        return DEV_TS


class SilentMonitor:
    """不连真机：只提供 Injector 需要的接口"""

    def __init__(self, q):
        self.q = q
        self.started = False

    def start(self, device_id=""):
        self.started = True

    def stop(self):
        self.started = False

    def restart(self, reason=""):
        return reason

    def health_check(self, idle_timeout=25.0):
        return None

    def should_health_check(self, interval=2.0):
        return False        # 替身不做健康检查（也不该因此让 pump 崩溃）

    @property
    def epoch(self):
        return 1

    @property
    def is_running(self):
        return self.started


# ── 日志脚本：4 条用例分别走 4 条判定路径 ──
# 时间戳须晚于 DEV_TS，否则被 Judger 的"旧日志过滤"丢弃
LOG_SCRIPT = {
    # ① 正常链路：ASR + 落域 + 回复 → PASS
    "最近的新闻": [
        '09-15 10:00:01.100  1  2 I isssr_log: {"normal_text":"最近的新闻"}',
        '09-15 10:00:01.300  1  2 I ASS-CbmToolPkStrategy: 云端落域 query = [最近的新闻] sid = [S1] domain = [新闻]',
        '09-15 10:00:02.500  1  2 I sparkSrResult: {"srCode":20204,"nlp":{"text":"为您播报最近的新闻"}}',
    ],
    # ② ASR 有字但无回复 → FAIL
    "播放音乐": [
        '09-15 10:00:01.100  1  2 I isssr_log: {"normal_text":"播放音乐"}',
        '09-15 10:00:01.300  1  2 I ASS-CbmToolPkStrategy: 云端落域 query = [播放音乐] sid = [S2] domain = [音乐]',
    ],
    # ③ 注入链路拒识 → FAIL（原因须含"拒识"）
    "L2 是什么意思": [
        '09-15 10:00:01.500  1  2 I ASS-CbmToolPkStrategy: handle sparkDenial 大模型拒识 query = [L2 是什么意思]',
        '09-15 10:00:01.600  1  2 I isssr_log: {"normal_text":"L2 是什么意思"}',
    ],
    # ④ 生图兜底：无字面关键词但已生图 → PASS
    "画一只猫": [
        '09-15 10:00:01.200  1  2 I VoiceProcessor: streamTextToPicture start',
        '09-15 10:00:01.400  1  2 I isssr_log: {"normal_text":"画一只猫"}',
        '09-15 10:00:03.200  1  2 I DataPointImpl: {"imageUrl":"http://cdn.example.com/a/cat.png"}',
    ],
}

CASES = [
    ("1", "最近的新闻", ["新闻"]),        # ① 关键词命中 → PASS
    ("2", "播放音乐", []),                # ② 无关键词、无回复 → FAIL
    ("3", "L2 是什么意思", ["L2"]),       # ③ 拒识优先 → FAIL
    ("4", "画一只猫", ["生成符合描述的图片"]),  # ④ 生图兜底 → PASS
]

EXPECT = {"1": "pass", "2": "fail", "3": "fail", "4": "pass"}


def build_data():
    ds = DataSource()
    ds._cases = [TestCase(id=cid, query=q, expected_keywords=kws) for cid, q, kws in CASES]
    return ds


def main() -> int:
    print("=" * 70)
    print("P1 端到端验收：无设备跑通完整流水线")
    print("=" * 70)

    tmp = Path(mkdtemp(prefix="cartest_e2e_"))
    try:
        # ── 组装（除 adb / monitor 外全为真实实现）──
        # 关键：adb 与 Injector 必须共用同一个日志队列，否则日志进不去判定器
        log_q = __import__("queue").Queue(maxsize=1000)
        adb = ScriptedAdb(log_q, LOG_SCRIPT)
        inj = Injector(adb=adb, log_q=log_q, monitor_factory=lambda q: SilentMonitor(q))
        inj.set_params(interval=0.05, timeout=6.0)
        inj.set_device("FAKE-DEV")

        data_path = tmp / "cases.json"
        data = build_data()
        pairs = pairs_from_cases(data.cases)
        store = ProgressStore.of(data_path, pairs, progress_dir=tmp / "progress")
        inj.set_progress_store(store, "skip_executed")

        logs = []
        inj.set_callbacks(on_log=lambda m, t: logs.append((t, m)))

        print("\n[1] 执行第一轮（4 条用例，覆盖 4 条判定路径）")
        t0 = time.time()
        inj.start_batch(data, mode=InjectMode.INTERVAL)
        while inj.state != InjectState.IDLE:
            if time.time() - t0 > 60:
                raise TimeoutError("流水线未在预期时间内结束")
            time.sleep(0.05)
        cost = time.time() - t0
        print(f"      耗时 {cost:.1f}s，灌入 {len(adb.sent)} 条")

        # ── 断言 ──
        check("4 条都发了", adb.sent == [c[1] for c in CASES], str(adb.sent))

        actual = {c.id: c.status for c in data.cases}
        check("判定结果与预期一致", actual == EXPECT, f"实际={actual}")

        check("① 正常链路 PASS", data.cases[0].status == "pass", data.cases[0].status)
        check("② 无回复 FAIL", data.cases[1].status == "fail", data.cases[1].status)
        check("② 失败原因指明无回复", "无回复" in data.cases[1].actual_result,
              data.cases[1].actual_result[:60])
        check("③ 拒识 FAIL 且原因含'拒识'", data.cases[2].status == "fail"
              and "拒识" in data.cases[2].actual_result, data.cases[2].actual_result[:60])
        check("④ 生图兜底 PASS", data.cases[3].status == "pass", data.cases[3].actual_result[:60])
        check("④ 兜底原因标注已生图", "端侧已生图" in data.cases[3].actual_result,
              data.cases[3].actual_result[:60])

        # ── 进度落盘 ──
        print("\n[2] 进度落盘与断点续跑")
        check("进度文件已生成", store.path.exists(), store.path.name)
        summary = store.summary()
        check("进度记录 4 条", summary["executed"] == 4, str(summary))
        check("通过/失败计数正确",
              summary["pass"] == 2 and summary["fail"] == 2, str(summary))
        latest = store.latest()
        check("每条都有 run_id 与 query",
              all(r.get("run_id") and r.get("query") for r in latest.values()),
              str(sorted(latest)))

        # ── 第二轮：全部应被跳过 ──
        adb2 = ScriptedAdb(log_q, LOG_SCRIPT)
        inj2 = Injector(adb=adb2, monitor_factory=lambda q: SilentMonitor(q))
        inj2.set_params(interval=0.05, timeout=6.0)
        data2 = build_data()
        store2 = ProgressStore.of(data_path, pairs, progress_dir=tmp / "progress")
        inj2.set_progress_store(store2, "skip_executed", {k: v.get("status") for k, v in latest.items()})
        inj2.start_batch(data2, mode=InjectMode.INTERVAL)
        t0 = time.time()
        while inj2.state != InjectState.IDLE:
            if time.time() - t0 > 30:
                raise TimeoutError("第二轮未结束")
            time.sleep(0.05)

        check("第二轮零灌入（全部跳过）", adb2.sent == [], str(adb2.sent))
        check("跳过计数=4", inj2.skipped_count == 4, str(inj2.skipped_count))

        # ── 回调 ──
        print("\n[3] 回调契约")
        check("on_log 收到日志", len(logs) > 0, f"{len(logs)} 条")
        check("日志含通过/失败标记",
              any("通过" in m for _, m in logs) and any("失败" in m for _, m in logs),
              str([m for t, m in logs if t in ("pass", "fail")][:2]))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("P1 端到端验收通过：整条流水线在无设备环境下跑通")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
