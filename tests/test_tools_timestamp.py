# -*- coding: utf-8 -*-
"""P3b 验收 — 时间戳工具：新实现 / 旧实现 / 标准库 三方对照

对照对象：
  · **新实现**：``cartest/app/tools/timestamp_tab.py``（搬入平台后）
  · **旧实现**：``flet_Tool/tabs/timestamp_tab.py``（搬入前，动态加载）
  · **标准库**：``time.localtime`` / ``strftime`` 独立计算

只对照**纯计算**方法（``_detect_precision`` / ``_precision_hint_text`` /
``_format_dt_from_ts``）；带 UI 副作用的 ``_convert`` / ``_from_timestamp``
由 GUI 冒烟覆盖（见 06-P3b设计.md §五 验收 3）。

用法:
    python tests/test_tools_timestamp.py
    pytest tests/test_tools_timestamp.py
"""
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.tools.timestamp_tab import TimestampTab as NewTab   # noqa: E402

#: 旧实现所在目录（flet_Tool 源）
OLD_DIR = Path(r"D:\SoftWare\WorkScript\Script\flet_Tool")

#: (时间戳, 期望位数) —— 末位 1 是"位数异常"分支（会自动截断）
SAMPLES: list[tuple[int, int]] = [
    (1757913600, 10),
    (1757913600123, 13),
    (1757913600123456, 16),
    (1757913600123456789, 19),
    (4102444800, 10),
    (0, 1),
]

SCALE = {10: 1, 13: 1000, 16: 10 ** 6, 19: 10 ** 9}

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def load_old_class():
    """动态加载旧实现；不可用时返回 None（降级为单侧对照）"""
    if not (OLD_DIR / "tabs" / "timestamp_tab.py").is_file():
        return None
    added = False
    if str(OLD_DIR) not in sys.path:
        sys.path.insert(0, str(OLD_DIR))
        added = True
    try:
        spec = importlib.util.spec_from_file_location(
            "_flet_tool_timestamp", OLD_DIR / "tabs" / "timestamp_tab.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_flet_tool_timestamp"] = mod
        spec.loader.exec_module(mod)
        return mod.TimestampTab
    except Exception as ex:  # noqa: BLE001
        print(f"  [WARN] 旧实现加载失败，降级为单侧对照: {ex}")
        return None
    finally:
        if added:
            try:
                sys.path.remove(str(OLD_DIR))
            except ValueError:
                pass


def test_precision(new, old) -> None:
    print("\n[1] 精度识别（_detect_precision）")
    for ts, expect in SAMPLES:
        got = new._detect_precision(ts)
        check(f"{ts} → {expect} 位", got == expect, f"实际 {got}")
        if old is not None:
            check("  与旧实现一致", old._detect_precision(ts) == got, str(old._detect_precision(ts)))


def test_hint(new, old) -> None:
    print("\n[2] 精度提示文案（_precision_hint_text）")
    for ts, _ in SAMPLES:
        got = new._precision_hint_text(ts)
        check(f"{ts} 提示非空", bool(got), got[:24])
        if old is not None:
            check("  与旧实现一致", old._precision_hint_text(ts) == got)


def test_format(new, old) -> None:
    print("\n[3] 时间戳 → 日期时间（_format_dt_from_ts）")
    for ts, digits in SAMPLES:
        got = new._format_dt_from_ts(ts, digits)
        ref = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(ts // SCALE.get(digits, 1)))
        check(f"{ts}({digits}位) → {ref}", got == ref, got)
        if old is not None:
            check("  与旧实现一致", old._format_dt_from_ts(ts, digits) == got, got)


def test_roundtrip(new) -> None:
    """可见正确性：格式化结果能反算回原时间戳（秒级）"""
    print("\n[4] 往返一致性（格式化 → 反算）")
    for ts, digits in SAMPLES:
        if digits not in SCALE:
            continue                      # 位数异常分支已在 [1] 覆盖
        text = new._format_dt_from_ts(ts, digits)
        if text == "—":
            continue
        back = int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")))
        check(f"{ts} 往返一致", back == ts // SCALE[digits], f"反算 {back}")


def main() -> int:
    print("=" * 70)
    print("P3b 验收 — 时间戳工具三方对照（新 / 旧 / 标准库）")
    print(f"旧实现目录: {OLD_DIR}")
    print("=" * 70)

    new = NewTab.__new__(NewTab)              # 纯计算方法不需要 __init__
    old_cls = load_old_class()
    old = old_cls.__new__(old_cls) if old_cls else None
    print(f"  旧实现: {'已加载' if old is not None else '不可用（降级）'}")

    test_precision(new, old)
    test_hint(new, old)
    test_format(new, old)
    test_roundtrip(new)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("时间戳工具对照通过（新实现与旧实现、标准库一致）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
