# -*- coding: utf-8 -*-
"""UI-2 验收 — 流水线页（P1 底栏 Dock）

覆盖：纯函数助手（计数/进度/耗时/ETA）/ 源码断言（T3 追加）。

设计依据：14-UI重构设计.md §6.1 / §7。
用法:
    python tests/test_ui_pipeline.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("UI-2 验收 — 流水线页（P1 底栏 Dock）")
    print("=" * 70)

    # ── [1] 纯函数助手 ──
    from cartest.app.pages.pipeline import (
        count_tags, count_progress_lines, read_case_total, eta_text, stage_durations,
    )

    lines = [("pass", "a"), ("fail", "b"), ("info", "c"), ("pass", "d"), ("error", "e")]
    got = count_tags(lines)
    check("count_tags：pass/fail 分离（error 不计入）",
          got == {"pass": 2, "fail": 1, "skip": 0}, str(got))

    tmp = Path(mkdtemp(prefix="ui2_pipe_"))
    f = tmp / "progress.jsonl"
    f.write_text('{"run_id":"1"}\n\n{"run_id":"2"}\r\n{"run_id":"3"}\n', encoding="utf-8")
    check("count_progress_lines：只数非空行", count_progress_lines(f) == 3, str(count_progress_lines(f)))
    check("count_progress_lines：文件不存在 → 0", count_progress_lines(tmp / "nope.jsonl") == 0)

    cf = tmp / "cases.json"
    cf.write_text(json.dumps([{"id": str(i)} for i in range(5)]), encoding="utf-8")
    check("read_case_total：JSON 用例数", read_case_total(cf) == 5)
    check("read_case_total：坏目标 → 0", read_case_total(tmp) == 0)

    check("eta_text：正常估算", eta_text(10, 30, 3.0).startswith("剩余 ~1"), eta_text(10, 30, 3.0))
    check("eta_text：完成/未知 → —", eta_text(40, 40, 3.0) == "—" and eta_text(0, 0, 3.0) == "—")
    check("eta_text：短时显示秒", eta_text(38, 40, 3.0).startswith("剩余 ~6 秒"), eta_text(38, 40, 3.0))

    class _FakeStage:
        def __init__(self, started, finished):
            self.started, self.finished = started, finished

    class _FakeCtx:
        def stage(self, s):
            return {"prepare": _FakeStage("2026-09-16 10:00:00", "2026-09-16 10:00:02"),
                    "inject": _FakeStage("2026-09-16 10:00:02", "2026-09-16 10:01:02"),
                    }.get(s, _FakeStage("", ""))

    d = stage_durations(_FakeCtx())
    check("stage_durations：耗时计算", d.get("prepare") == "2s" and d.get("inject") == "60s", str(d))

    # ── [2] 源码断言（结构 + Dock 交互）──
    SRC = (ROOT / "cartest" / "app" / "pages" / "pipeline.py").read_text(encoding="utf-8")
    check("含控制台/阶段详情双标签", "控制台" in SRC and "阶段详情" in SRC)
    check("Dock 双视图挂载（条件列）", "ft.Column([console_view, details_view]" in SRC)
    check("含清空/导出/折叠操作", all(k in SRC for k in ("清空", "导出", "折叠")))
    check("概览含进度条（ProgressBar）", "ft.ProgressBar" in SRC)
    check("阶段条含呼吸钩子注册", "tick_hooks.append" in SRC)
    check("进度数据源=progress.jsonl 行数", "count_progress_lines" in SRC and "progress_jsonl" in SRC)
    check("参数预填沿用 last_params", "state.last_params" in SRC)
    check("启动/停止沿用 AppState 驱动", "state.start_pipeline" in SRC and "state.request_stop" in SRC)

    # ── [3] 灌入范围（2026-10-15：改名 + 新增「从指定序号起重灌」）──
    from cartest.app.pages.pipeline import RESUME_MODES
    check("下拉改名「灌入范围」（原续跑范围）",
          'label="灌入范围"' in SRC and "续跑范围" not in SRC)
    check("四档范围（跳过已执行/仅重灌失败与无回复/全部重灌/从指定序号起重灌）",
          list(RESUME_MODES) == ["跳过已执行", "仅重灌失败与无回复", "全部重灌",
                                 "从指定序号起重灌"], str(list(RESUME_MODES)))
    check("from_index → redo_all + start_index（前段不灌，该条起重灌）",
          'if resume_mode == "from_index":' in SRC
          and 'resume_mode = "redo_all"' in SRC
          and "start_index = n - 1" in SRC)
    check("「起始序号」输入框常显置灰、仅该档可用",
          'label="起始序号"' in SRC and "_sync_start_idx" in SRC
          and "start_idx_field.disabled = resume_dd.value != _FROM_INDEX_LABEL" in SRC)
    check("起始序号校验（数字 / ≥1 / 不超用例总数）",
          "起始序号必须是数字（1=第一条）" in SRC
          and "起始序号必须 ≥ 1（1=第一条）" in SRC
          and "超出用例总数" in SRC)
    check("参数贯通 start_index（→ PipelineParams）",
          "start_index=start_index" in SRC)
    check("起始序号随 last_params 预填",
          '"start_index": start_idx_field.value' in SRC)

    # 流水线侧贯通（stages：start_index → injector.start_batch(start_from)）
    ST = (ROOT / "cartest" / "pipeline" / "stages.py").read_text(encoding="utf-8")
    check("stages 透传 start_from（含负值护栏）",
          "start_from=max(0, p.start_index)" in ST
          and "start_index: int = 0" in ST)
    check("灌入摘要标注起始序号",
          'f"从第 {p.start_index + 1} 条开始 · "' in ST)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("流水线页检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
