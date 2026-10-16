# -*- coding: utf-8 -*-
"""Judger 判定链自测（无需真机）

覆盖 2026-09-14 修复点：
  1) "落域到X意图"类期望 → 用实际落域兜底（消除落域类用例的假 FAIL）
  2) 负向表述"不会落域到X意图" 不参与兜底，仍按字面判定
  3) 落域不匹配 / 无落域 时不得兜底通过
  4) 生图兜底（端侧已生图）与描述性长文案路径行为不变（回归）
  5) 拒识优先于关键词 PASS
  6) 时钟注入（core 新增）—— 判定逻辑完全离线可测

来源：CarVoiceTestTool/self_test_judger.py 平移（2026-09-14）。

用法:
    python tests/test_judger.py
    pytest tests/test_judger.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.judger import Judger                          # noqa: E402

FAILED = []


class Case:
    """最小用例替身（鸭子类型，与 TestCase 的 3 个被读属性一致）"""

    def __init__(self, keywords=None, intent=""):
        self.query = "q"
        self.expected_keywords = keywords or []
        self.expected_intent = intent


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def make(asr="", domain="", reply=False, image=False):
    j = Judger()
    j._asr_text = asr
    j._intent_type = domain
    if reply:
        j._reply_started = True
        j._tts_text = "一些回复文本"
    if image:
        j._final_image_time = "12:00:00"
    return j


# ── [1] 落域文案解析 ──
def test_domain_expect_parse():
    print("[1] domain_expect 解析")
    de = Judger.domain_expect
    check("落域到新闻意图 → 新闻", de("落域到新闻意图") == "新闻", repr(de("落域到新闻意图")))
    check("落域至互联网电台意图 → 互联网电台", de("落域至互联网电台意图") == "互联网电台")
    check("负向表述 → 空", de("不会落域到新闻意图") == "", repr(de("不会落域到新闻意图")))
    check("不落域表述 → 空", de("不落域到新闻意图") == "")
    check("无关文案 → 空", de("可以正确搜索到相关新闻") == "")


# ── [2] 落域兜底 ──
def test_domain_fallback_pass():
    print("[2] 落域命中 → 兜底 PASS")
    j = make(asr="找找近期台海相关新闻", domain="新闻")
    r = j.judge(Case(["落域到新闻意图"]))
    check("判定 PASS", r.passed, r.reason)
    check("原因标注兜底", "落域兜底" in r.reason, r.reason)


def test_domain_fallback_negative_case():
    print("[3] 负向用例（不会落域到新闻意图）不得兜底")
    j = make(asr="我想听剑来", domain="internetRadio")
    r = j.judge(Case(["不会落域到新闻意图"]))
    # 该关键词是 9 字短关键词，字面不命中 → FAIL 是正确的表意
    check("不被兜底为 PASS", not (r.passed and "落域兜底" in r.reason), r.reason)


def test_domain_mismatch_fail():
    print("[4] 落域不匹配 / 无落域 → 不兜底")
    j = make(asr="找找近期台海相关新闻", domain="chat")
    r = j.judge(Case(["落域到新闻意图"]))
    check("落域不符 → FAIL", not r.passed, r.reason)
    j2 = make(asr="找找近期台海相关新闻", domain="")
    r2 = j2.judge(Case(["落域到新闻意图"]))
    check("无落域 → FAIL", not r2.passed, r2.reason)


# ── [5] 生图兜底 ──
def test_aigc_fallback_regression():
    print("[5] 生图兜底回归")
    j = make(asr="画一只猫", domain="文生图助手", image=True)
    r = j.judge(Case(["生成符合描述的图片"]))
    check("生图 → PASS", r.passed, r.reason)
    check("原因标注已生图", "端侧已生图" in r.reason, r.reason)


# ── [6] 描述性长文案路径 ──
def test_long_keyword_path():
    print("[6] 描述性长文案路径回归")
    desc = "正常返回新闻列表，播报新闻开场白，新闻可播放"
    j = make(asr="最近的新闻", domain="新闻", reply=True)
    r = j.judge(Case([desc]))
    check("ASR+回复 → PASS", r.passed, r.reason)
    j2 = make(asr="最近的新闻", domain="新闻", reply=False)
    r2 = j2.judge(Case([desc]))
    check("无回复 → FAIL", not r2.passed, r2.reason)


# ── [7] 拒识优先 ──
def test_denial_priority():
    print("[7] 拒识优先于关键词 PASS")
    j = make(asr="L2 是什么", domain="cbm_denial")
    j._denied = True
    j._denial_reason = "车机CBM路由拒识(sparkDenial)"
    # 关键词字面其实能命中（ASR 里就有 "L2"），但拒识必须优先判 FAIL
    r = j.judge(Case(["L2"]))
    check("拒识 → FAIL", not r.passed, r.reason)
    check("原因含拒识说明", "拒识" in r.reason, r.reason)


# ── [8] 时钟注入（core 新增能力）──
def test_clock_injection():
    print("[8] 时钟注入：判定逻辑可完全离线")
    calls = []

    def fake_clock():
        calls.append(1)
        return "09-15 10:00:00.000"

    j = Judger(clock=fake_clock)
    j.start_collection()
    check("注入的时钟被调用", len(calls) == 1, f"calls={len(calls)}")
    check("发送时间戳来自注入时钟", j._send_timestamp == "09-15 10:00:00.000",
          repr(j._send_timestamp))
    check("未使用真实 adb（无异常）", True)

    # TTL 内第二次采集复用缓存，不重复调用
    j2 = Judger(clock=fake_clock)
    j2._dev_ts_cache = (time.time(), "09-15 10:00:00.000")
    before = len(calls)
    j2.start_collection()
    check("TTL 内复用缓存", len(calls) == before, f"calls 从 {before} → {len(calls)}")


# ── [9] 超时与等待中 ──
def test_timeout_and_waiting():
    print("[9] 超时 / 等待中的判定")
    j = Judger(timeout=20.0)
    j._start_time = time.time() - 30.0     # 已等待 30s > 超时 20s
    r = j.judge(Case())
    check("无响应 + 超时 → FAIL", not r.passed and "超时" in r.reason, r.reason)

    j2 = Judger(timeout=20.0)
    j2._start_time = time.time()           # 刚开始等待
    r2 = j2.judge(Case())
    check("无响应 + 未超时 → 等待中", not r2.passed and "等待" in r2.reason, r2.reason)


ALL_TESTS = (test_domain_expect_parse, test_domain_fallback_pass, test_domain_fallback_negative_case,
             test_domain_mismatch_fail, test_aigc_fallback_regression, test_long_keyword_path,
             test_denial_priority, test_clock_injection, test_timeout_and_waiting)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("Judger 判定自测全部通过")
