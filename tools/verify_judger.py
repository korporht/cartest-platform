"""迁移一致性验证 —— judger 模块

把**同一批日志**分别喂给新旧判定器，对比判定结果是否完全一致。
不连接真实设备：两边都把设备时钟替换为固定值。

这是"只搬家不改逻辑"最直接的证据——判定链是整套工具最核心的资产，
任何一行正则或优先级的改动都会在这里暴露。

用法：
    python tools/verify_judger.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from _legacy import require as _require_legacy   # noqa: E402  同目录定位器
CVT_DIR = _require_legacy("CarVoiceTestTool")     # 缺失时打印 [SKIP] 并退出 0
CARTEST_DIR = Path(__file__).resolve().parents[1]

PASS, FAIL = "[PASS]", "[FAIL]"
_results: list[tuple[bool, str]] = []

FIXED_TS = "09-15 10:00:00.000"

# ── 日志样本（真实格式：MM-DD HH:MM:SS.mmm 前缀）──
L_ASR = '09-15 10:00:01.100  1234  5678 I isssr_log: {"normal_text":"最近的新闻"}'
L_DOMAIN = '09-15 10:00:01.300  1234  5678 I ASS-CbmToolPkStrategy: 云端落域 domain = [新闻]'
L_REPLY = '09-15 10:00:02.500  1234  5678 I sparkSrResult: {"srCode":20204,"nlp":{"text":"为您播报最近的新闻"}}'
L_REPLY_2 = '09-15 10:00:02.800  1234  5678 I sparkSrResult: {"srCode":20204,"nlp":{"text":"，头条是..."}}'
L_TTS = '09-15 10:00:02.100  1234  5678 I isssr_log: {"tts_text":"好的"}'
L_DENIAL = '09-15 10:00:01.500  1234  5678 I ASS-CbmToolPkStrategy: handle sparkDenial 大模型拒识 query = [L2 是什么意思]'
L_IMAGE = '09-15 10:00:03.200  1234  5678 I DataPointImpl: {"imageUrl":"http://cdn.example.com/a/b/c.png"}'
L_EXPEND = '09-15 10:00:03.100  1234  5678 I DataPointImpl: {"expendImageUrl":"http://cdn.example.com/placeholder.png"}'
L_AIGC_FLAG = '09-15 10:00:01.200  1234  5678 I VoiceProcessor: streamTextToPicture start'
L_ERR = '09-15 10:00:01.700  1234  5678 E VoiceProcessor: java.lang.NullPointerException: boom'
L_NOISE = '09-15 10:00:01.050  1234  5678 I OtherApp: nothing relevant here'
L_OLD = '09-15 09:59:50.000  1234  5678 I isssr_log: {"normal_text":"旧会话串扰"}'


def check(name: str, old, new) -> None:
    ok = old == new
    _results.append((ok, name))
    print(f"{PASS if ok else FAIL} {name}")
    if not ok:
        print(f"      旧实现: {old!r}")
        print(f"      新实现: {new!r}")


class Case:
    def __init__(self, keywords=None, intent=""):
        self.query = "q"
        self.expected_keywords = keywords or []
        self.expected_intent = intent


def run_old(lines: list[str], case: Case, timeout: float = 20.0, elapsed: float = 0.0):
    from result_judger import ResultJudger
    j = ResultJudger()
    j._query_device_timestamp = lambda: FIXED_TS      # 绕开 adb
    j._send_timestamp = FIXED_TS
    j._timeout = timeout
    j._start_time = time.time() - elapsed
    for ln in lines:
        j.feed_log(ln)
    r = j.judge(case)
    return (r.passed, r.asr_text, r.intent_type, r.reply_text, r.reason)


def run_new(lines: list[str], case: Case, timeout: float = 20.0, elapsed: float = 0.0):
    from cartest.core.judger import Judger
    j = Judger(clock=lambda: FIXED_TS, timeout=timeout)
    j._send_timestamp = FIXED_TS
    j._start_time = time.time() - elapsed
    for ln in lines:
        j.feed_log(ln)
    r = j.judge(case)
    return (r.passed, r.asr_text, r.intent_type, r.reply_text, r.reason)


# ── 场景定义：(名称, 日志, 用例, 超时, 已等待秒数) ──
SCENARIOS = [
    ("正常链路：ASR + 落域 + 回复 → PASS",
     [L_NOISE, L_ASR, L_DOMAIN, L_REPLY, L_REPLY_2], Case(), 20.0, 0.0),

    ("ASR + 落域但无回复 → FAIL",
     [L_ASR, L_DOMAIN], Case(), 20.0, 0.0),

    ("拒识优先（即便 ASR 有文本）→ FAIL",
     [L_DENIAL, L_ASR], Case(["L2"]), 20.0, 0.0),

    ("生图兜底（关键词未字面命中）→ PASS",
     [L_AIGC_FLAG, L_ASR, L_IMAGE], Case(["生成符合描述的图片"]), 20.0, 0.0),

    ("占位图兜底 → PASS",
     [L_AIGC_FLAG, L_ASR, L_EXPEND], Case(["生成符合描述的图片"]), 20.0, 0.0),

    ("落域兜底（期望文案无法字面命中）→ PASS",
     [L_ASR, L_DOMAIN], Case(["落域到新闻意图"]), 20.0, 0.0),

    ("短关键词未命中且无兜底 → FAIL",
     [L_ASR, L_DOMAIN], Case(["音乐播放"]), 20.0, 0.0),

    ("关键词字面命中 → PASS",
     [L_ASR, L_DOMAIN, L_REPLY], Case(["新闻"]), 20.0, 0.0),

    ("TTS 文本路径（车控短确认）→ PASS",
     [L_ASR, L_DOMAIN, L_TTS], Case(), 20.0, 0.0),

    ("错误行（无响应）→ FAIL",
     [L_ERR], Case(), 20.0, 0.0),

    ("空日志 + 超时 → FAIL",
     [], Case(), 20.0, 30.0),

    ("空日志 + 未超时 → 等待中",
     [], Case(), 20.0, 0.0),

    ("旧日志串扰被时间戳过滤",
     [L_OLD, L_ASR], Case(["旧会话"]), 20.0, 0.0),

    ("意图匹配路径",
     [L_ASR], Case(intent="新闻"), 20.0, 0.0),
]


def main() -> int:
    print("=" * 70)
    print("迁移一致性验证：judger 模块（同一批日志 → 对比判定结果）")
    print("=" * 70)

    sys.path.insert(0, str(CVT_DIR))
    sys.path.insert(0, str(CARTEST_DIR))
    # 环境隔离（2026-10-10）：对照以「内置 e0v 默认」为前提——
    # 断开真实 settings.json 与项目档案，免受本机多平台切换（t28/t1ma）影响
    import tempfile as _tf
    from cartest.core import config_center as _cc
    from cartest.core import project as _pj
    _iso = Path(_tf.mkdtemp(prefix="cartest_verify_judger_iso_"))
    _cc.set_settings_file(_iso / "settings.json")
    _pj.set_projects_dir(_iso / "projects")

    print()
    for name, lines, case, timeout, elapsed in SCENARIOS:
        old = run_old(list(lines), case, timeout, elapsed)
        new = run_new(list(lines), case, timeout, elapsed)
        check(name, old, new)

    print()
    print("=" * 70)
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"结果: {passed}/{total} 通过")
    for ok, name in _results:
        if not ok:
            print(f"  {FAIL} {name}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
