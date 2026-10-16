"""迁移一致性验证 —— parser/live 模块

对比新旧解析器在**相同输入**下的产出：
  - 真实日志文件（8758 行 logcat）→ parse_log_file / match_by_proximity /
    match_replies 的全部输出逐条比对
  - 构造样本 → 覆盖真实日志未包含的分支（logid 提取与匹配、sid 精确匹配优先）
  - 边界 → 无效时间戳 / 不存在的文件 / 空文件

用法：
    python tools/verify_parser.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _legacy import require as _require_legacy   # noqa: E402  同目录定位器
CVT_DIR = _require_legacy("CarVoiceTestTool")     # 缺失时打印 [SKIP] 并退出 0
CARTEST_DIR = Path(__file__).resolve().parents[1]
REAL_LOG = CVT_DIR / "logs" / "raw_logcat_20260914_162419.log"

PASS, FAIL = "[PASS]", "[FAIL]"
_results: list[tuple[bool, str]] = []


def check(name: str, old, new) -> None:
    """对比新旧产出是否一致"""
    ok = old == new
    _results.append((ok, name))
    print(f"{PASS if ok else FAIL} {name}")
    if not ok:
        print(f"      旧: {str(old)[:200]}")
        print(f"      新: {str(new)[:200]}")


def assert_true(name: str, cond: bool, extra: str = "") -> None:
    """断言（用于"样本是否真的覆盖了目标分支"这类自检）"""
    _results.append((bool(cond), name))
    print(f"{PASS if cond else FAIL} {name}" + (f"  {extra}" if extra else ""))


def q_tuple(q) -> tuple:
    return (q.timestamp.isoformat(), q.query, q.sid, q.domain, q.source)


def l_tuple(x) -> tuple:
    return (x.timestamp.isoformat(), x.logid, x.source)


def r_tuple(r) -> tuple:
    return (r.timestamp.isoformat(), r.reply, r.sid, r.source)


def m_tuple(m) -> tuple:
    return (m.query, m.query_time.isoformat() if m.query_time else None,
            m.sid, m.domain, m.logid,
            m.logid_time.isoformat() if m.logid_time else None,
            m.reply, m.reply_time.isoformat() if m.reply_time else None, m.source)


def run_parse(parser, path: str):
    res = parser.parse_log_file(path)
    return {
        "queries": [q_tuple(x) for x in res["queries"]],
        "logids": [l_tuple(x) for x in res["logids"]],
        "replies": [r_tuple(x) for x in res["replies"]],
        "intents": [(d["timestamp"].isoformat(), d["intent"]) for d in res["intents"]],
    }


def run_full(parser, path: str):
    res = parser.parse_log_file(path)
    matched = parser.match_by_proximity(res["queries"], res["logids"])
    matched = parser.match_replies(matched, res["replies"])
    return [m_tuple(m) for m in matched]


# ── 构造样本：覆盖真实日志缺失的 logid / sid 分支 ──
SAMPLE_LOGID = """\
09-14 16:24:20.100  1489  1649 I isssr_log: {"normal_text":"最近的新闻"}
09-14 16:24:20.600  1489  1649 I ASS-CbmToolPkStrategy: 云端落域 query = [最近的新闻] sid = [SID001] domain = [新闻]
09-14 16:24:21.200  1489  1649 I VoiceProcessor: "cbm_tool_logid": {"text":"12345678901234ab"}
09-14 16:24:21.500  1489  1649 I TtsSpeaker: startFlowSpeak sid=SID001, text=为您播报新闻, traceId=xyz
09-14 16:24:22.900  1489  1649 I isssr_log: {"normal_text":"播放音乐"}
09-14 16:24:23.400  1489  1649 I ASS-CbmToolPkStrategy: 云端落域 query = [播放音乐] sid = [SID002] domain = [音乐]
09-14 16:24:23.900  1489  1649 I Network: X-Tt-Logid: abcdef1234567890
09-14 16:24:25.000  1489  1649 I isssr_log: {"tts_text":"正在为您播放音乐"}
"""

# 边界：无效时间戳行、空文件
SAMPLE_EDGE = """\
这不是一行日志
09-99 99:99:99.999  这是非法时间戳
09-14 16:24:20.100  1489  1649 I isssr_log: {"normal_text":"只有ASR没有回复"}
"""


def main() -> int:
    print("=" * 70)
    print("迁移一致性验证：parser/live 模块")
    print("=" * 70)

    sys.path.insert(0, str(CVT_DIR))
    from log_parser import LogParser as OldParser                     # noqa: E402
    sys.path.insert(0, str(CARTEST_DIR))
    from cartest.core.parser.live import LogParser as NewParser       # noqa: E402

    # ── 1. 真实日志文件 ──
    print("\n[1] 真实日志文件（8758 行 logcat）")
    if not REAL_LOG.exists():
        print(f"  [SKIP] 日志不存在: {REAL_LOG}")
    else:
        op = OldParser(year=2026)
        np_ = NewParser(year=2026)
        check("  parse_log_file 全字段一致", run_parse(op, str(REAL_LOG)), run_parse(np_, str(REAL_LOG)))
        check("  完整链路（解析+匹配）一致", run_full(op, str(REAL_LOG)), run_full(np_, str(REAL_LOG)))

        r = op.parse_log_file(str(REAL_LOG))
        print(f"      样本规模: queries={len(r['queries'])} logids={len(r['logids'])} "
              f"replies={len(r['replies'])} intents={len(r['intents'])}")

    # ── 2. 构造样本：logid 与 sid 分支 ──
    print("\n[2] 构造样本：logid 提取与匹配 / sid 精确优先")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sample.log"
        p.write_text(SAMPLE_LOGID, encoding="utf-8")

        op = OldParser(year=2026)
        np_ = NewParser(year=2026)
        check("  parse_log_file 全字段一致", run_parse(op, str(p)), run_parse(np_, str(p)))
        check("  匹配结果一致", run_full(op, str(p)), run_full(np_, str(p)))

        # 断言样本确实触发了目标分支（否则这个验证是空的）
        r = np_.parse_log_file(str(p))
        print(f"      样本覆盖: queries={len(r['queries'])} logids={len(r['logids'])} "
              f"replies={len(r['replies'])}")
        assert_true("样本含 logid（cbm + bds 各一）", len(r["logids"]) == 2,
                    f"logids={len(r['logids'])}")
        assert_true("logid 来源正确", {x.source for x in r["logids"]} == {"cbm", "bds"},
                    str({x.source for x in r["logids"]}))

        # ── 3. 边界样本 ──
        print("\n[3] 边界：非法时间戳 / 无回复会话")
        pe = Path(td) / "edge.log"
        pe.write_text(SAMPLE_EDGE, encoding="utf-8")
        check("  边界样本一致", run_parse(OldParser(year=2026), str(pe)),
              run_parse(NewParser(year=2026), str(pe)))

        # ── 4. 不存在的文件 / 目录级 parse_logs ──
        print("\n[4] 不存在的文件 / 目录级 parse_logs")
        missing = str(Path(td) / "nope.log")
        check("  不存在的文件", run_parse(OldParser(year=2026), missing),
              run_parse(NewParser(year=2026), missing))

        sub = Path(td) / "logs"
        sub.mkdir()
        (sub / "main_001.log").write_text(SAMPLE_LOGID, encoding="utf-8")
        (sub / "other_001.log").write_text(SAMPLE_LOGID, encoding="utf-8")   # 非 main_ 应被跳过
        import log_parser as _op_mod
        import cartest.core.parser.live as _np_mod
        o_m, o_i = _op_mod.parse_logs(str(sub))
        n_m, n_i = _np_mod.parse_logs(str(sub))
        check("  parse_logs 匹配结果一致", [m_tuple(x) for x in o_m], [m_tuple(x) for x in n_m])
        check("  parse_logs intents 一致",
              [(d["timestamp"].isoformat(), d["intent"]) for d in o_i],
              [(d["timestamp"].isoformat(), d["intent"]) for d in n_i])
        # 单文件产出 4 条（2 条 ASR + 2 条 CBM query）；两个文件都存在，
        # 若 other_001.log 未被跳过则会是 8 条
        assert_true("非 main_ 文件被跳过（只解析 main_001.log）", len(n_m) == 4,
                    f"matched={len(n_m)}")

    # ── 汇总 ──
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
