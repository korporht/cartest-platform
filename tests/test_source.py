# -*- coding: utf-8 -*-
"""DataSource 数据源自测（不依赖旧工具、不触碰真实数据）

覆盖：
  1) split_multi_round_query 多轮拆分（含边界：无标记/单轮带前缀/位置不对）
  2) _map_columns 列映射（精确 > 模糊，别名优先级）
  3) _find_header_row 表头定位（跳过汇总行）
  4) 四种格式加载：Excel（标准/多轮）、CSV、TXT、JSON
  5) 多轮展开开关（split_multi_round）
  6) TestCase.reset / get_stats

所有用例数据均由本测试现场生成到临时目录，绝不读写生产文件。

用法:
    python tests/test_source.py
    pytest tests/test_source.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.source import DataSource, TestCase          # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def load(path: Path, split: bool = True):
    ds = DataSource()
    ds.split_multi_round = split
    ok, msg, n = ds.load_file(str(path))
    return ds, ok, n


# ── [1] 多轮拆分 ──
def test_split_multi_round():
    print("[1] split_multi_round_query")
    f = DataSource.split_multi_round_query
    check("无标记 → 单条原样", f("最近的新闻") == [("", "最近的新闻")])
    check("空串 → 单条空", f("") == [("", "")])
    r = f("Q1:画小猫\nQ2:加一只小狗")
    check("两轮 → 拆两条", r == [("1", "画小猫"), ("2", "加一只小狗")], str(r))
    r2 = f("Q1:打开空调")
    check("单轮带前缀 → 拆一条", r2 == [("1", "打开空调")], str(r2))
    r3 = f("随便说说 Q2: 这个不算前缀")
    check("Q 前缀不在开头 → 不拆", r3 == [("", "随便说说 Q2: 这个不算前缀")], str(r3))
    r4 = f("Q1:导航\nQ2:走高速\nQ3:避开拥堵")
    check("三轮 → 拆三条", len(r4) == 3 and r4[2] == ("3", "避开拥堵"), str(r4))


# ── [2] 列映射 ──
def test_map_columns():
    print("[2] _map_columns")
    m = DataSource._map_columns(["序号", "步骤描述", "期望结果", "分类"])
    check("中文表头映射", m.get("id") == 0 and m.get("query") == 1
          and m.get("expected") == 2 and m.get("category") == 3, str(m))
    m2 = DataSource._map_columns(["用例ID", "语音指令", "期望关键词", "意图", "优先级", "备注"])
    check("别名映射", m2.get("query") == 1 and m2.get("expected") == 2
          and m2.get("intent") == 3 and m2.get("priority") == 4, str(m2))
    m3 = DataSource._map_columns(["需求名称", "操作步骤", "预期结果"])
    check("无 id 列时不误映射", "id" not in m3 or m3["id"] != 0, str(m3))
    check("空表头不崩", DataSource._map_columns([]) == {})


# ── [3] 表头定位 ──
def test_find_header_row():
    print("[3] _find_header_row")
    rows = [["执行了 241 条用例, PASS: 100"], ["序号", "步骤描述", "期望结果"]]
    check("跳过汇总首行 → 1", DataSource._find_header_row(rows) == 1)
    check("首个即表头 → 0", DataSource._find_header_row([["序号", "query", "expected"]]) == 0)
    check("无特征 → 0", DataSource._find_header_row([["随便", "什么"]]) == 0)
    check("弱特征命中 → 0", DataSource._find_header_row([["用例", "语音"]]) == 0)


# ── [4] 四种格式加载 ──
def test_load_formats():
    print("[4] 四种格式加载")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # JSON
        jf = td / "a.json"
        jf.write_text(json.dumps([
            {"id": "A1", "query": "最近的新闻", "expected_keywords": ["新闻"], "category": "媒体"},
            {"id": "A2", "query": "播放音乐", "expected_keywords": "music,歌曲", "priority": "P0"},
            "纯字符串条目",
        ], ensure_ascii=False), encoding="utf-8")
        ds, ok, n = load(jf)
        check("JSON 加载 3 条", ok and n == 3, f"n={n}")
        check("JSON keywords 数组", ds.cases[0].expected_keywords == ["新闻"])
        check("JSON keywords 逗号串 → 数组", ds.cases[1].expected_keywords == ["music", "歌曲"],
              str(ds.cases[1].expected_keywords))
        check("JSON 纯字符串条目", ds.cases[2].query == "纯字符串条目")

        # CSV（含汇总首行）
        cf = td / "a.csv"
        cf.write_text("执行了 2 条用例, PASS: 2\n序号,步骤描述,期望结果,分类\n1,最近的新闻,新闻,媒体\n2,播放音乐,music,娱乐\n",
                      encoding="utf-8-sig")
        dsc, okc, nc = load(cf)
        check("CSV 加载 2 条（跳过汇总行）", okc and nc == 2, f"n={nc}")
        check("CSV 字段解析", dsc.cases[1].query == "播放音乐" and dsc.cases[1].expected_keywords == ["music"])

        # TXT
        tf = td / "a.txt"
        tf.write_text("# 注释\n最近的新闻|新闻,播报|媒体\n播放音乐\n", encoding="utf-8")
        dst, okt, nt = load(tf)
        check("TXT 加载 2 条（跳过注释）", okt and nt == 2, f"n={nt}")
        check("TXT 竖线分隔", dst.cases[0].expected_keywords == ["新闻", "播报"])

        # Excel 标准
        try:
            import openpyxl
            xf = td / "a.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["序号", "步骤描述", "期望结果", "分类"])
            ws.append(["1", "最近的新闻", "新闻", "媒体"])
            ws.append(["2", "播放音乐", "music", "娱乐"])
            wb.save(xf)
            dsx, okx, nx = load(xf)
            check("Excel 标准加载 2 条", okx and nx == 2, f"n={nx}")

            # Excel 多轮（2 个 query 列触发）
            mf = td / "m.xlsx"
            wb2 = openpyxl.Workbook()
            ws2 = wb2.active
            ws2.append(["序号", "测试点", "前置条件", "模块1", "第1Query", "第1期望结果",
                        "模块2", "第2Query", "第2期望结果"])
            ws2.append(["1", "多轮", "无", "A", "打开空调", "空调已打开",
                        "B", "调到24度", "温度已调整"])
            wb2.save(mf)
            dsm, okm, nm = load(mf)
            check("Excel 多轮展开为 2 条", okm and nm == 2, f"n={nm}")
            check("多轮 id 后缀", {c.id for c in dsm.cases} == {"1-1", "1-2"},
                  str([c.id for c in dsm.cases]))
        except ImportError:
            print("  [SKIP] openpyxl 未安装")


# ── [5] 多轮展开开关 ──
def test_expand_switch():
    print("[5] split_multi_round 开关")
    with tempfile.TemporaryDirectory() as td:
        jf = Path(td) / "m.json"
        jf.write_text(json.dumps([{"id": "M1", "query": "Q1:画小猫\nQ2:加小狗"}]),
                      encoding="utf-8")

        ds_on, _, n_on = load(jf, split=True)
        check("开关开 → 拆成 2 条", n_on == 2, f"n={n_on}")
        check("子用例 id 带轮次", [c.id for c in ds_on.cases] == ["M1-Q1", "M1-Q2"],
              str([c.id for c in ds_on.cases]))

        ds_off, _, n_off = load(jf, split=False)
        check("开关关 → 保持 1 条", n_off == 1, f"n={n_off}")
        check("原文保留", ds_off.cases[0].query == "Q1:画小猫\nQ2:加小狗")


# ── [6] TestCase.reset / get_stats ──
def test_case_reset_and_stats():
    print("[6] TestCase.reset / get_stats")
    c = TestCase(id="T1", query="q", status="pass", asr_text="asr",
                 inject_time="09-15 10:00:00", error_msg="e", prev_session=True)
    c.reset()
    check("reset 清空运行时字段", c.status == "pending" and not c.asr_text
          and not c.inject_time and not c.error_msg and not c.prev_session)
    check("reset 不动用例定义", c.id == "T1" and c.query == "q")

    ds = DataSource()
    ds._cases = [TestCase(id="1", query="a", status="pass"),
                 TestCase(id="2", query="b", status="fail"),
                 TestCase(id="3", query="c", status="pending")]
    st = ds.get_stats()
    check("统计正确", st["total"] == 3 and st["pass"] == 1 and st["fail"] == 1 and st["pending"] == 1,
          str(st))


ALL_TESTS = (test_split_multi_round, test_map_columns, test_find_header_row,
             test_load_formats, test_expand_switch, test_case_reset_and_stats)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("DataSource 自测全部通过")
