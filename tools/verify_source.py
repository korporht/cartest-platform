"""迁移一致性验证 —— source 模块

对比新旧数据源在**相同输入**下的产出：
  - split_multi_round_query：多轮拆分
  - _map_columns：列名映射（含模糊匹配与别名优先级）
  - _find_header_row：表头定位
  - load_file：四种格式（Excel/CSV/TXT/JSON）加载结果逐条比对

其中 JSON 用真实用例文件（252 条），是与生产数据一致性的直接证据。

用法：
    python tools/verify_source.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from _legacy import require as _require_legacy   # noqa: E402  同目录定位器
CVT_DIR = _require_legacy("CarVoiceTestTool")     # 缺失时打印 [SKIP] 并退出 0
CARTEST_DIR = Path(__file__).resolve().parents[1]
CASE_FILE = CVT_DIR / "data" / "PLT1第4轮_AI新闻_语音灌入用例.json"

PASS, FAIL = "[PASS]", "[FAIL]"
_results: list[tuple[bool, str]] = []


def check(name: str, old, new) -> None:
    ok = old == new
    _results.append((ok, name))
    print(f"{PASS if ok else FAIL} {name}")
    if not ok:
        print(f"      旧: {str(old)[:160]}")
        print(f"      新: {str(new)[:160]}")


def case_tuple(c) -> tuple:
    """把 TestCase 压成可比较的元组"""
    return (c.id, c.query, tuple(c.expected_keywords), c.expected_intent,
            c.category, c.priority, c.remark)


def load(dir_path: Path, file: Path, split: bool = True):
    """用指定实现加载文件，返回 cases 元组列表"""
    import importlib
    for m in list(sys.modules):
        if m in ("data_source", "cartest.core.source"):
            del sys.modules[m]
    sys.path.insert(0, str(dir_path))
    try:
        mod = importlib.import_module("data_source")
    finally:
        sys.path.remove(str(dir_path))
    ds = mod.DataSource()
    ds.split_multi_round = split
    ok, msg, n = ds.load_file(str(file))
    return ok, n, [case_tuple(c) for c in ds.cases]


def main() -> int:
    print("=" * 70)
    print("迁移一致性验证：source 模块")
    print("=" * 70)

    # ── 1. 纯函数：split_multi_round_query ──
    print("\n[1] split_multi_round_query（多轮拆分）")
    sys.path.insert(0, str(CARTEST_DIR))
    from cartest.core.source import DataSource as NewDS     # noqa: E402
    sys.path.insert(0, str(CVT_DIR))
    from data_source import DataSource as OldDS             # noqa: E402

    samples = [
        "最近的新闻",
        "Q1:画小猫\nQ2:加一只小狗",
        "Q1:打开空调",
        "Q1:导航到南京\nQ2:走高速\nQ3:避开拥堵",
        "随便说说 Q2: 这个不算前缀",
        "",
        "Q1:第一轮\nQ2:第二轮\nQ3:第三轮\nQ4:第四轮\nQ5:第五轮",
    ]
    for s in samples:
        check(f"  拆分 {s[:26]!r}", OldDS.split_multi_round_query(s),
              NewDS.split_multi_round_query(s))

    # ── 2. 纯函数：_map_columns ──
    print("\n[2] _map_columns（列映射）")
    header_sets = [
        ["序号", "步骤描述", "期望结果", "分类"],
        ["用例ID", "语音指令", "期望关键词", "意图", "优先级", "备注"],
        ["id", "query", "expected", "intent", "category", "priority", "remark"],
        ["需求名称", "操作步骤", "预期结果", "需求ID", "前置条件"],
        ["测试点", "测试步骤", "验证标准", "模块"],
        ["第1Query", "第1期望结果", "第2Query", "第2期望结果"],   # 多轮格式
        ["a", "b", "c"],                                          # 无特征列名
        [],                                                       # 空表头
    ]
    for hs in header_sets:
        check(f"  {str(hs)[:44]}", OldDS._map_columns(list(hs)), NewDS._map_columns(list(hs)))

    # ── 3. 纯函数：_find_header_row ──
    print("\n[3] _find_header_row（表头行定位）")
    row_sets = [
        [["执行了 241 条用例, PASS: 100"], ["序号", "步骤描述", "期望结果"]],
        [["序号", "query", "expected"]],
        [["随便", "什么"], ["都没有特征"]],
        [["需求名称", "操作步骤", "预期结果"]],
    ]
    for rs in row_sets:
        check(f"  {str(rs)[:44]}", OldDS._find_header_row([list(r) for r in rs]),
              NewDS._find_header_row([list(r) for r in rs]))

    # ── 4. 真实 JSON 用例文件 ──
    print("\n[4] load_file — 真实用例文件（JSON, 252 条）")
    if not CASE_FILE.exists():
        print(f"{FAIL} 用例文件不存在: {CASE_FILE}")
    else:
        o_ok, o_n, o_cases = load(CVT_DIR, CASE_FILE)
        n_ok, n_n, n_cases = load(CARTEST_DIR, CASE_FILE)
        check("  加载状态与条数", (o_ok, o_n), (n_ok, n_n))
        check("  用例逐条一致", o_cases, n_cases)
        print(f"      共比对 {len(o_cases)} 条用例（id/query/keywords/intent/category/priority/remark）")

    # ── 5. CSV / TXT / Excel ──
    print("\n[5] load_file — 其他格式")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # CSV（首行是汇总行，验证表头定位）
        csv_file = td_path / "t.csv"
        csv_file.write_text(
            "执行了 3 条用例, PASS: 2\n"
            "序号,步骤描述,期望结果,分类\n"
            "1,最近的新闻,新闻,媒体\n"
            "2,播放音乐,music,娱乐\n"
            "3,,,\n",
            encoding="utf-8-sig")
        o = load(CVT_DIR, csv_file)
        n = load(CARTEST_DIR, csv_file)
        check("  CSV（含汇总首行）", o, n)

        # TXT
        txt_file = td_path / "t.txt"
        txt_file.write_text(
            "# 注释行\n最近的新闻|新闻,播报|媒体\n播放音乐\n",
            encoding="utf-8")
        o = load(CVT_DIR, txt_file)
        n = load(CARTEST_DIR, txt_file)
        check("  TXT", o, n)

        # Excel（标准 + 多轮）
        try:
            import openpyxl
            xlsx = td_path / "t.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["序号", "步骤描述", "期望结果", "分类"])
            ws.append(["1", "最近的新闻", "新闻", "媒体"])
            ws.append(["2", "播放音乐", "music", "娱乐"])
            wb.save(xlsx)
            o = load(CVT_DIR, xlsx)
            n = load(CARTEST_DIR, xlsx)
            check("  Excel（标准格式）", o, n)

            # 多轮格式：2 个 query 列即可触发
            xlsx2 = td_path / "t2.xlsx"
            wb2 = openpyxl.Workbook()
            ws2 = wb2.active
            ws2.append(["序号", "测试点", "前置条件", "模块1", "第1Query", "第1期望结果",
                        "模块2", "第2Query", "第2期望结果"])
            ws2.append(["1", "多轮", "无", "A", "打开空调", "空调已打开",
                        "B", "调到24度", "温度已调整"])
            wb2.save(xlsx2)
            o = load(CVT_DIR, xlsx2)
            n = load(CARTEST_DIR, xlsx2)
            check("  Excel（多轮格式）", o, n)
        except ImportError:
            print("  [SKIP] openpyxl 未安装，跳过 Excel")

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
