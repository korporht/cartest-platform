# -*- coding: utf-8 -*-
"""RunContext 状态机 与 运行目录清理 自测

覆盖：
  1) make_run_id 格式与非法字符清理
  2) RunContext.create / open / list_runs
  3) 状态机：begin / finish / fail / skip；running 视为未完成
  4) last_done_stage / next_stage（断点恢复依据）
  5) 清理规则：keep 永不删、最近 N 次保留、其余删除；体积超限只报告不越权

全部在临时目录内进行，不触碰真实 runs/。

用法:
    python tests/test_pipeline_context.py
    pytest tests/test_pipeline_context.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.pipeline.cleanup import cleanup_runs, describe, plan_cleanup   # noqa: E402
from cartest.pipeline.context import (                                     # noqa: E402
    DONE, FAILED, PENDING, RUNNING, SKIPPED, STAGES, RunContext, make_run_id,
)

FAILED_LIST = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED_LIST.append(name)


# ── [1] run_id 生成 ──
def test_make_run_id():
    print("[1] make_run_id")
    t = time.mktime((2026, 9, 15, 14, 30, 0, 0, 0, -1))
    rid = make_run_id("PLT1第4轮_AI新闻_语音灌入用例.json", now=t)
    check("含日期时间前缀（精确到秒）", rid.startswith("2026-09-15_143000_"), rid)
    check("含用例名简述", "AI新闻" in rid, rid)

    rid2 = make_run_id("a/b:c*d?e.json", now=t)
    check("非法字符被清理", not any(c in rid2 for c in '\\/:*?"<>|'), rid2)

    long_name = "x" * 100 + ".json"
    rid3 = make_run_id(long_name, now=t)
    check("超长名被截断（≤30 字符）", len(rid3.split("_", 2)[2]) <= 30, rid3)


# ── [2] 创建 / 打开 / 列出 ──
def test_create_open_list():
    print("[2] create / open / list_runs")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ctx = RunContext.create("cases.json", device="DEV1", root=root)
        check("目录已创建", ctx.run_dir.is_dir(), str(ctx.run_dir))
        check("run.json 已写", ctx.meta_file.is_file())
        check("初始阶段全为 pending",
              all(ctx.stage_status(s) == PENDING for s in STAGES), str(ctx.summary()))

        ctx2 = RunContext.open(ctx.run_id, root=root)
        check("open 回读设备号", ctx2.device == "DEV1", ctx2.device)
        check("open 回读阶段数", len(ctx2.stages) == len(STAGES))

        runs = RunContext.list_runs(root)
        check("list_runs 找到 1 个", len(runs) == 1, str(len(runs)))
        check("list_runs 含 size 字段", "size" in runs[0])


# ── [3] 状态机 ──
def test_state_machine():
    print("[3] 状态机")
    with tempfile.TemporaryDirectory() as td:
        ctx = RunContext.create("cases.json", root=Path(td))

        ctx.begin_stage("prepare")
        check("begin → running", ctx.stage_status("prepare") == RUNNING, ctx.stage_status("prepare"))
        ctx.finish_stage("prepare", "252 条")
        check("finish → done", ctx.stage_status("prepare") == DONE)
        check("detail 被记录", ctx.stage("prepare").detail == "252 条")

        ctx.begin_stage("inject")
        ctx.fail_stage("inject", "设备断开")
        check("fail → failed", ctx.stage_status("inject") == FAILED)
        check("error 被记录", "设备断开" in ctx.stage("inject").error)

        ctx.skip_stage("settle", "无日志")
        check("skip → skipped", ctx.stage_status("settle") == SKIPPED)

        # 持久化：重开
        ctx2 = RunContext.open(ctx.run_id, root=Path(td))
        check("状态跨进程保持", ctx2.stage_status("inject") == FAILED)


# ── [4] 断点恢复依据 ──
def test_resume_logic():
    print("[4] last_done_stage / next_stage")
    with tempfile.TemporaryDirectory() as td:
        ctx = RunContext.create("cases.json", root=Path(td))
        check("初始 next_stage = prepare", ctx.next_stage() == "prepare", str(ctx.next_stage()))

        ctx.finish_stage("prepare")
        check("完成 prepare 后 → inject", ctx.next_stage() == "inject")
        check("last_done = prepare", ctx.last_done_stage() == "prepare")

        # running 视为未完成（中断残留）→ 重跑该阶段
        ctx.begin_stage("inject")
        check("running 不被当作已完成", ctx.last_done_stage() == "prepare",
              str(ctx.last_done_stage()))
        check("next_stage 仍指向 inject（重跑）", ctx.next_stage() == "inject")

        # 全部完成
        for s in STAGES:
            ctx.finish_stage(s)
        check("全部完成 → is_complete", ctx.is_complete())
        check("next_stage 为 None", ctx.next_stage() is None)


# ── [5] 清理规则 ──
def test_cleanup_rules():
    print("[5] 清理规则")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 造 12 个 run（时间递增，保证排序稳定）
        base = time.mktime((2026, 9, 1, 0, 0, 0, 0, 0, -1))
        ids = []
        for i in range(12):
            rid = make_run_id(f"case{i}.json", now=base + i * 3600)
            ctx = RunContext.create(f"case{i}.json", root=root)
            # create 用当前时间，这里手工改 created_at 以便排序确定
            ctx.created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i * 3600))
            if i == 0:
                ctx.keep = True          # 最旧的标记为 keep
            ctx.save()
            ids.append(ctx.run_id)

        runs = RunContext.list_runs(root)
        check("造出 12 个 run", len(runs) == 12, str(len(runs)))

        to_delete, stats = plan_cleanup(root, keep_recent=10)
        # 12 个 run：最近 10 个受数量保护 + case0 受 keep 保护（两者叠加）→ 只剩 1 个可删
        check("保护集 = 最近10 + keep(1)", stats["protected"] == 11, str(stats))
        check("计划删除 1 个", stats["to_delete"] == 1, str(stats))
        check("keep 的不在删除列表", ids[0] not in [r["run_id"] for r in to_delete],
              str([r["run_id"] for r in to_delete]))

        deleted = cleanup_runs(root, keep_recent=10)
        check("实际删除 1 个", len(deleted) == 1, str(deleted))
        remaining = RunContext.list_runs(root)
        check("剩余 11 个（10 最近 + 1 keep）", len(remaining) == 11, str(len(remaining)))
        check("keep 的仍在", ids[0] in [r["run_id"] for r in remaining])

        # dry_run 不真删：11 个 run，保留最近 5 + keep 1 = 6 → 计划删 5
        n = cleanup_runs(root, keep_recent=5, dry_run=True)
        check("dry_run 返回计划但不删",
              len(n) == 5 and len(RunContext.list_runs(root)) == 11,
              f"planned={len(n)} remaining={len(RunContext.list_runs(root))}")

        # keep_recent=0 时全部可删（但 keep 标记的除外）
        deleted2 = cleanup_runs(root, keep_recent=0)
        rest = RunContext.list_runs(root)
        check("keep_recent=0 → 只剩 keep 标记的", len(rest) == 1 and rest[0]["run_id"] == ids[0],
              str([r["run_id"] for r in rest]))


# ── [6] 空目录与描述 ──
def test_empty_and_describe():
    print("[6] 空目录 / describe")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("空目录 list_runs 为空", RunContext.list_runs(root) == [])
        check("空目录 cleanup 返回空", cleanup_runs(root) == [])
        check("空目录 plan 不崩", plan_cleanup(root)[1]["total"] == 0)
        check("describe 处理空目录", "暂无" in describe(root), describe(root))

        RunContext.create("c.json", root=root)
        check("describe 含统计", "运行记录" in describe(root), describe(root))


ALL_TESTS = (test_make_run_id, test_create_open_list, test_state_machine,
             test_resume_logic, test_cleanup_rules, test_empty_and_describe)


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
    print()
    if FAILED_LIST:
        print(f"失败 {len(FAILED_LIST)} 项: {FAILED_LIST}")
        sys.exit(1)
    print("RunContext / cleanup 自测全部通过")
