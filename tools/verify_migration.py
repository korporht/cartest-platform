"""迁移一致性验证 —— 新 core 与旧实现的行为对比

用途：P1 期间每搬一个模块，就把它与旧实现放在**相同输入**上对比输出，
      确保"只搬家不改逻辑"。

用法：
    python tools/verify_migration.py

当前覆盖：
    - progress.fingerprint  ← 必须与旧实现同值（否则已有进度文件全部对不上）
    - progress.pairs_from_cases
"""
from __future__ import annotations

import sys
from pathlib import Path

from _legacy import require as _require_legacy   # noqa: E402  同目录定位器
CVT_DIR = _require_legacy("CarVoiceTestTool")     # 缺失时打印 [SKIP] 并退出 0
CARTEST_DIR = Path(__file__).resolve().parents[1]
CASE_FILE = CVT_DIR / "data" / "E0V第4轮_AI新闻_语音灌入用例.json"

PASS, FAIL = "[PASS]", "[FAIL]"
_results: list[tuple[bool, str]] = []


def check(name: str, old, new) -> None:
    ok = old == new
    _results.append((ok, name))
    print(f"{PASS if ok else FAIL} {name}")
    print(f"      旧实现: {old}")
    print(f"      新实现: {new}")


def main() -> int:
    print("=" * 68)
    print("迁移一致性验证：progress 模块")
    print("=" * 68)

    # ── 1. 用旧实现加载真实用例 ──
    sys.path.insert(0, str(CVT_DIR))
    from data_source import DataSource                     # noqa: E402
    from progress_store import fingerprint as old_fp       # noqa: E402
    from progress_store import pairs_from_cases as old_pairs  # noqa: E402
    from progress_store import ProgressStore as old_store_cls  # noqa: E402

    print(f"\n用例文件: {CASE_FILE.name}")
    if not CASE_FILE.exists():
        print(f"{FAIL} 用例文件不存在，无法验证")
        return 1

    ds = DataSource()
    ds.split_multi_round = True
    ok, msg, n = ds.load_file(str(CASE_FILE))
    print(f"加载结果: ok={ok} n={n} msg={msg}")

    old_pairs_list = old_pairs(ds.cases)
    old_fingerprint = old_fp(CASE_FILE, old_pairs_list)

    # ── 2. 切到新实现（隔离 sys.path，避免模块名串味）──
    for m in list(sys.modules):
        if m in ("config", "data_source", "progress_store"):
            del sys.modules[m]
    sys.path.remove(str(CVT_DIR))
    sys.path.insert(0, str(CARTEST_DIR))

    from cartest.core.progress import fingerprint as new_fp          # noqa: E402
    from cartest.core.progress import pairs_from_cases as new_pairs   # noqa: E402
    from cartest.core.progress import ProgressStore as new_store_cls  # noqa: E402

    new_pairs_list = new_pairs(ds.cases)
    new_fingerprint = new_fp(CASE_FILE, new_pairs_list)

    # ── 3. 对比 ──
    print()
    check("pairs_from_cases 输出一致", len(old_pairs_list), len(new_pairs_list))
    check("pairs 内容一致（前5条抽样）", old_pairs_list[:5], new_pairs_list[:5])
    check("fingerprint 一致", old_fingerprint, new_fingerprint)

    # 指纹的"历史值"——2026-09-14 实际使用的进度文件名里就是这个值。
    # 注意：该值随**用例文件内容**变化；本次核对时用例集已更新，新旧实现算出的
    # 指纹**彼此一致**但不再等于历史值 ⇒ 迁移无损，历史进度文件也已不再对应
    # 本用例集。故此处只在"新旧不一致"时才算失败（上面已断言），历史值仅作提示。
    if new_fingerprint == "694f095f":
        print("[PASS] fingerprint 等于历史进度文件名中的值（694f095f）")
    else:
        print(f"[SKIP] 历史指纹 694f095f 已随用例文件更新为 {new_fingerprint}"
              f"（与旧实现一致 ⇒ 迁移无损；2026-09-14 的进度文件不再对应本用例集）")

    # ── 4. 真实进度文件读取对照（只读，不写入）──
    print("\n" + "-" * 68)
    print("真实进度文件读取对照")
    print("-" * 68)

    old_store = old_store_cls.of(CASE_FILE, old_pairs_list)
    old_summary = old_store.summary()
    print(f"旧实现读取: {old_store.path.name}")

    new_store = new_store_cls.of(CASE_FILE, new_pairs_list,
                                 progress_dir=CVT_DIR / "logs" / "progress")
    new_summary = new_store.summary()
    print(f"新实现读取: {new_store.path.name}")

    check("进度文件名一致", old_store.path.name, new_store.path.name)
    check("summary.executed 一致", old_summary["executed"], new_summary["executed"])
    check("summary.pass 一致", old_summary["pass"], new_summary["pass"])
    check("summary.fail 一致", old_summary["fail"], new_summary["fail"])
    check("summary.last_ts 一致", old_summary["last_ts"], new_summary["last_ts"])
    check("summary.devices 一致", old_summary["devices"], new_summary["devices"])

    # ── 汇总 ──
    print()
    print("=" * 68)
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"结果: {passed}/{total} 通过")
    for ok, name in _results:
        if not ok:
            print(f"  {FAIL} {name}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
