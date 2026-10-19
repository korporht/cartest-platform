# -*- coding: utf-8 -*-
"""UI-1 验收 — 设计令牌（双主题）

设计依据：14-UI重构设计.md §4.1。

用法:
    python tests/test_ui_tokens.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.ui import tokens  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("UI-1 验收 — 设计令牌（双主题）")
    print("=" * 70)

    d, l = tokens.PALETTE_DARK, tokens.PALETTE_LIGHT

    check("深浅两套键集完全一致", set(d) == set(l),
          f"仅深色有={sorted(set(d) - set(l))} 仅浅色有={sorted(set(l) - set(d))}")
    check("核心语义键齐全（两套都有）",
          all(k in d and k in l for k in tokens.CORE_KEYS),
          str([k for k in tokens.CORE_KEYS if k not in d or k not in l]))
    bad = [k for k, v in {**l, **d}.items() if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v)]
    check("全部值为 6 位十六进制色", not bad, str(bad))

    from cartest.app.theme import AppTheme
    t = AppTheme()
    check("默认明亮：c(accent)==浅色表", t.c("accent") == l["accent"], t.c("accent"))
    check("stage_color 映射（明亮）", t.stage_color("running") == l["stage_running"])
    t.toggle()
    check("toggle 后读深色表", t.c("accent") == d["accent"], t.c("accent"))

    for legacy, modern in [("bg_card", "bg_panel"), ("bg_sidebar", "bg_chrome"),
                           ("bg_topbar", "bg_chrome"), ("bg_log", "bg_console"),
                           ("indicator_color", "sel_bg")]:
        check(f"兼容键 {legacy} == {modern}", t.c(legacy) == t.c(modern))

    t.toggle()
    check("切回默认（明亮）", t.c("accent") == l["accent"])

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("设计令牌检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
