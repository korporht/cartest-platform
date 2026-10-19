# -*- coding: utf-8 -*-
"""LogParser 日志解析自测（不依赖旧工具）

覆盖：
  1) parse_timestamp（正常 / 非法）
  2) 各 extract_* 提取器（ASR 双模式 / CBM query / logid 双源 / TTS 六模式 / 车控 / intent）
  3) parse_log_file（含 .gz）
  4) match_by_proximity（10s 窗口、logid 须在 query 之后、未匹配 query 也保留）
  5) match_replies（30s 窗口、sid 精确匹配优先）

数据全部现场生成到临时目录。

用法:
    python tests/test_parser.py
    pytest tests/test_parser.py
"""
import gzip
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.parser.live import (                        # noqa: E402
    LogParser, LogidRecord, QueryRecord, ReplyRecord,
)

FAILED = []
YEAR = 2026


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def ts(h, m, s, ms=0):
    return datetime(YEAR, 9, 14, h, m, s, ms * 1000)


# ── [1] 时间戳解析 ──
def test_timestamp():
    print("[1] parse_timestamp")
    p = LogParser(year=YEAR)
    t = p.parse_timestamp("09-14 16:24:18.982  1489  1649 I SDM: xxx")
    check("正常解析", t == ts(16, 24, 18, 982), str(t))
    check("毫秒补齐（.98 → 980ms）",
          p.parse_timestamp("09-14 16:24:18.98  x") == ts(16, 24, 18, 980))
    check("非法时间戳 → None", p.parse_timestamp("不是日志") is None)
    check("空串 → None", p.parse_timestamp("") is None)


# ── [2] 提取器 ──
def test_extractors():
    print("[2] 各 extract_* 提取器")
    p = LogParser(year=YEAR)
    T = ts(16, 24, 20)

    # ASR 双模式
    a = p.extract_asr_result('x "normal_text":"最近的新闻"', T)
    check("ASR normal_text", a and a.query == "最近的新闻" and a.source == "asr")
    b = p.extract_asr_result("srResultBack: 播放音乐--true", T)
    check("ASR srResultBack", b and b.query == "播放音乐" and b.source == "asr")
    check("ASR 无匹配 → None", p.extract_asr_result("无关行", T) is None)

    # CBM query（需同时含两个特征串）
    line = 'ASS-CbmToolPkStrategy: 云端落域 query = [最近的新闻] sid = [S1] domain = [新闻]'
    c = p.extract_cbm_query(line, T)
    check("CBM query 全字段", c and c.query == "最近的新闻" and c.sid == "S1"
          and c.domain == "新闻" and c.source == "cbm", str(c))
    check("缺'云端落域' → None",
          p.extract_cbm_query('ASS-CbmToolPkStrategy: query = [x]', T) is None)

    # logid 双源
    l1 = p.extract_logid('"cbm_tool_logid": {"text":"12345678901234ab"}', T)
    check("CBM logid", l1 and l1.logid == "12345678901234ab" and l1.source == "cbm")
    l2 = p.extract_logid("X-Tt-Logid: abcdef1234567890", T)
    check("BDS logid", l2 and l2.logid == "abcdef1234567890" and l2.source == "bds")
    check("logid 无匹配 → None", p.extract_logid("无关", T) is None)

    # TTS 六模式
    r1 = p.extract_tts_reply("[TtsSpeaker]startFlowSpeak sid=S1, text=您好, traceId=x", T)
    check("TTS 模式1 startFlowSpeak", r1 and r1.reply == "您好" and r1.sid == "S1", str(r1))
    r3 = p.extract_tts_reply('"tts_text":"正在为您播放音乐"', T)
    check("TTS 模式3 tts_text", r3 and r3.reply == "正在为您播放音乐")
    check("TTS 模式3 过短被过滤", p.extract_tts_reply('"tts_text":"短"', T) is None)
    r6 = p.extract_tts_reply('event=550 "content":"BDS回复内容"', T)
    check("TTS 模式6 event=550", r6 and r6.reply == "BDS回复内容" and r6.source == "bds")

    # 车控
    cc = p.extract_car_control_reply("CCA-TtsIdQueryHelper: 已帮你打开空调", T)
    check("车控回复", cc and cc.reply == "已帮你打开空调" and cc.source == "car_control")
    check("车控模板行被跳过",
          p.extract_car_control_reply("CCA-TtsIdQueryHelper: CCA-1005 ttsResults", T) is None)

    # intent
    check("intent_type", p.extract_intent_type('"intent_type":"news"', T) == "news")
    check("intent 无匹配 → None", p.extract_intent_type("无关", T) is None)


# ── [3] parse_log_file ──
def test_parse_file():
    print("[3] parse_log_file（.log 与 .gz）")
    content = (
        '09-14 16:24:20.100  1  2 I isssr_log: {"normal_text":"最近的新闻"}\n'
        '09-14 16:24:20.600  1  2 I ASS-CbmToolPkStrategy: 云端落域 query = [最近的新闻] sid = [S1] domain = [新闻]\n'
        '09-14 16:24:21.500  1  2 I [TtsSpeaker]startFlowSpeak sid=S1, text=为您播报, traceId=x\n'
        '09-14 16:24:22.000  1  2 I isssr_log: {"intent_type":"news"}\n'
        '不是日志行\n'
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "main_001.log"
        p.write_text(content, encoding="utf-8")
        parser = LogParser(year=YEAR)
        r = parser.parse_log_file(str(p))
        check("queries=2（ASR + CBM）", len(r["queries"]) == 2, f"n={len(r['queries'])}")
        check("replies=1", len(r["replies"]) == 1, f"n={len(r['replies'])}")
        check("intents=1", len(r["intents"]) == 1, f"n={len(r['intents'])}")
        check("非法行被跳过", all(q.query for q in r["queries"]))

        # gz
        gz = Path(td) / "main_002.log.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as f:
            f.write(content)
        r2 = parser.parse_log_file(str(gz))
        check(".gz 解析一致",
              len(r2["queries"]) == len(r["queries"]) and len(r2["replies"]) == len(r["replies"]))

        # 不存在的文件
        r3 = parser.parse_log_file(str(Path(td) / "nope.log"))
        check("不存在文件 → 空结果",
              r3 == {"queries": [], "logids": [], "replies": [], "intents": []})


# ── [4] match_by_proximity ──
def test_match_proximity():
    print("[4] match_by_proximity（10s 窗口）")
    p = LogParser(year=YEAR)
    queries = [QueryRecord(timestamp=ts(16, 24, 20), query="Q1", source="asr"),
               QueryRecord(timestamp=ts(16, 24, 30), query="Q2", source="asr")]
    logids = [LogidRecord(timestamp=ts(16, 24, 21), logid="L1"),
              LogidRecord(timestamp=ts(16, 24, 30, 500), logid="L2")]
    res = p.match_by_proximity(queries, logids)
    check("两个 query 都被匹配", len(res) == 2, f"n={len(res)}")
    check("L1 匹配到 Q1", res[0].logid == "L1" and res[0].query == "Q1", str(res[0]))
    check("L2 匹配到 Q2", res[1].logid == "L2" and res[1].query == "Q2", str(res[1]))

    # logid 在 query 之前 → 不匹配
    logids2 = [LogidRecord(timestamp=ts(16, 24, 10), logid="L0")]
    res2 = p.match_by_proximity(queries, logids2)
    check("logid 早于 query → 不匹配并保留 query", len(res2) == 2 and not res2[0].logid)

    # 超出窗口（L9 距最近的 Q2 也有 15s > 10s 窗口）
    logids3 = [LogidRecord(timestamp=ts(16, 24, 45), logid="L9")]
    res3 = p.match_by_proximity(queries, logids3)
    check("超出 10s 窗口 → 不匹配", all(not r.logid for r in res3))


# ── [5] match_replies ──
def test_match_replies():
    print("[5] match_replies（30s 窗口 / sid 优先）")
    p = LogParser(year=YEAR)
    from cartest.core.parser.live import MatchedResult
    results = [MatchedResult(query="Q1", query_time=ts(16, 24, 20), sid="S1")]
    replies = [
        ReplyRecord(timestamp=ts(16, 24, 22), reply="时间近但不是S1", sid="OTHER", source="tts"),
        ReplyRecord(timestamp=ts(16, 24, 25), reply="S1的回复", sid="S1", source="tts"),
    ]
    res = p.match_replies(results, replies)
    check("sid 精确匹配优先（忽略时间更近的）", res[0].reply == "S1的回复", str(res[0].reply))

    # 无 sid 时按时间最近
    results2 = [MatchedResult(query="Q2", query_time=ts(16, 24, 20))]
    replies2 = [ReplyRecord(timestamp=ts(16, 24, 25), reply="远的", source="tts"),
                ReplyRecord(timestamp=ts(16, 24, 22), reply="近的", source="tts")]
    res2 = p.match_replies(results2, replies2)
    check("无 sid → 取时间最近", res2[0].reply == "近的", str(res2[0].reply))

    # 超出窗口
    results3 = [MatchedResult(query="Q3", query_time=ts(16, 24, 20))]
    replies3 = [ReplyRecord(timestamp=ts(16, 25, 30), reply="太晚", source="tts")]
    res3 = p.match_replies(results3, replies3)
    check("超 30s 窗口 → 不匹配", not res3[0].reply)

    # 抢配场景（2026-10-10，T28 真机错位修复）：同 query 两条记录
    # ——ASR（无 sid）在前 + CBM（有 sid）在后；自己的回复时间比"他人的"更近，
    # 旧逻辑下 ASR 记录会先以"时间最近"抢走自己的回复、CBM 只能捡错配；
    # 修复后 CBM 经 **sid 全局配对**先锁定自己的回复。
    results4 = [
        MatchedResult(query="Q4", query_time=ts(16, 24, 20)),                  # ASR（无 sid）
        MatchedResult(query="Q4", query_time=ts(16, 24, 20, 100), sid="S4"),   # CBM（有 sid）
    ]
    replies4 = [
        ReplyRecord(timestamp=ts(16, 24, 25), reply="他人的回复", sid="S9", source="tts"),
        ReplyRecord(timestamp=ts(16, 24, 21), reply="S4的回复", sid="S4", source="tts"),
    ]
    res4 = p.match_replies(results4, replies4)
    check("sid 全局配对：CBM 记录锁定自己的回复（不被他记录抢配）",
          res4[1].reply == "S4的回复", str(res4[1].reply))
    check("ASR 记录只拿剩余回复（不再抢走自己会话的）",
          res4[0].reply == "他人的回复", str(res4[0].reply))


ALL_TESTS = (test_timestamp, test_extractors, test_parse_file, test_match_proximity, test_match_replies)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("LogParser 自测全部通过")
