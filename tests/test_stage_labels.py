# -*- coding: utf-8 -*-
"""40 号批 7 验收 — 阶段显示中文化（只改显示层；内部 key 与 run.json 不变）

覆盖：
  [1] context：STAGE_LABELS 全量 7 阶段 + stage_label 未知 key 原样回退
  [2] 显示点接入：pipeline 阶段条/阶段详情、main 运行胶囊、state 阶段日志、
      runner 续跑提示
  [3] 不变性：STAGES 本体与 run.json 写入的 key 仍为英文（prepare…report）

用法:
    python tests/test_stage_labels.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("40 号批 7 — 阶段显示中文化")
    print("=" * 70)

    # ── [1] context 映射 ──
    from cartest.pipeline.context import STAGES, STAGE_LABELS, stage_label

    check("7 阶段全量中文化",
          set(STAGE_LABELS) == set(STAGES) and len(STAGE_LABELS) == 7,
          str(STAGE_LABELS))
    check("映射值符合约定（准备/灌入/落盘取证/提取/判定/回填/报告）",
          [STAGE_LABELS[s] for s in STAGES] ==
          ["准备", "灌入", "落盘取证", "提取", "判定", "回填", "报告"])
    check("未知 key 原样回退", stage_label("custom_x") == "custom_x")

    # ── [2] 显示点接入 ──
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
    state_src = (ROOT / "cartest" / "app" / "state.py").read_text(encoding="utf-8")
    runner_src = (ROOT / "cartest" / "pipeline" / "runner.py").read_text(encoding="utf-8")

    # 2026-10-19：「测试流程」页删除 → 页内阶段条断言移除；stage_label 仍由
    # main 运行胶囊 / state 阶段日志 / runner 续跑提示三处消费（下方继续断言）
    check("main 运行胶囊用 stage_label", "stage_label(stage)" in main_src)
    check("state 阶段日志用 stage_label", "stage_label(stage)" in state_src)
    check("runner 续跑提示用 stage_label", "stage_label(start)" in runner_src)

    # ── [3] 不变性（内部 key 仍英文）──
    check("STAGES 本体不变（英文 key）",
          STAGES == ["prepare", "inject", "settle", "extract",
                     "judge", "fillback", "report"], str(STAGES))
    begin_src = (ROOT / "cartest" / "pipeline" / "context.py").read_text(encoding="utf-8")
    begin_body = begin_src.split("def begin_stage", 1)[1].split("\n    def ", 1)[0]
    check("run.json 仍写英文 key（begin_stage 用原始 key、不查 label）",
          "label" not in begin_body)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("阶段显示中文化检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
