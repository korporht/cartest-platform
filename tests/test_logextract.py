# -*- coding: utf-8 -*-
"""TREXTRACT 验收 — 时间段日志提取（core/logextract 库化 + 行为）

设计依据：36-时间段日志提取设计.md。夹具：临时目录造假日志（Android 命名 + 行时间戳）。
含「有意偏差」验证：解压读取中途损坏记单文件 error 继续（旧脚本会击穿整批）。

用法:
    python tests/test_logextract.py
"""
import gzip
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core.logextract import (  # noqa: E402
    LogExtractor, coverage_hint, extract_timestamp, parse_time_arg,
    scan_directory, stream_key,
)

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


# ── 夹具工具 ──────────────────────────────────────────

def android_line(hhmmss: str, text: str = "line") -> str:
    """生成 Android 格式行：09-10 15:23:00.000 line"""
    return f"09-10 {hhmmss}.000 {text}\n"


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def make_bad_gz_truncated(path: Path) -> None:
    """坏 gz #1：内容全为无时间戳行 + 截断尾部（读时 EOFError）"""
    text = "garbage line\n" * 50
    full = gzip.compress(text.encode("utf-8"))
    path.write_bytes(full[:-8])          # 砍掉尾部 CRC/ISIZE


def make_bad_gz_flipped(path: Path) -> None:
    """坏 gz #2：中段字节翻转（读时 zlib.error，peek 也必须不崩）"""
    text = "".join(f"garbage line {i:04d}\n" for i in range(200))
    full = bytearray(gzip.compress(text.encode("utf-8")))
    mid = len(full) // 2
    full[mid] ^= 0xFF
    path.write_bytes(bytes(full))


def read_gz_lines(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read().splitlines()


def main() -> int:
    print("=" * 70)
    print("TREXTRACT 验收 — 时间段日志提取")
    print("=" * 70)
    tmp = Path(mkdtemp(prefix="logextract_"))

    # ── 夹具 A：重叠选取 + 头尾切割（端到端） ──
    src_a = tmp / "src_a" / "logs_20260910_152823"
    write_lines(src_a / "main_00101_2026-09-10_15-20-00.log",
                [android_line(f"15:{m:02d}:00", "early") for m in (18, 19, 20)])
    write_lines(src_a / "main_00101_2026-09-10_15-25-00.log",
                [android_line(f"15:{m:02d}:00", f"win{m}") for m in (20, 21, 22, 23, 24, 25)])

    print("\n[1] 纯函数（时间解析 / 文件名时间戳 / 流标识）")
    check("parse_time_arg 区间", parse_time_arg("15:40-15:45") == (15, 40, 15, 45, True))
    check("parse_time_arg 紧凑点 ±r", parse_time_arg("1542") == (15, 37, 15, 47, True))
    check("parse_time_arg 开放下界", parse_time_arg("1540-") == (15, 40, 24, 0, True))
    try:
        parse_time_arg("abc")
        check("parse_time_arg 非法格式抛 ValueError", False)
    except ValueError:
        check("parse_time_arg 非法格式抛 ValueError", True)
    check("extract_timestamp Android 命名",
          extract_timestamp("main_00101_2026-09-10_15-20-00.log") == ("20260910", 15, 20, 0))
    check("extract_timestamp AIOS 命名",
          extract_timestamp("aios_0001_20260910-151821.dlt.gz") == ("20260910", 15, 18, 21))
    check("extract_timestamp 无时间戳 → None", extract_timestamp("random.log") is None)
    check("stream_key 同流归并",
          stream_key("main_00101_2026-09-10_15-20-00.log") == "main_00101"
          and stream_key("main_00101_2026-09-10_15-25-00.log") == "main_00101")

    print("\n[2] 端到端提取（窗口 15:23-15:26）")
    out_a = tmp / "out_a"
    r = LogExtractor().extract(str(src_a), time_str="15:23-15:26", output_dir=str(out_a))
    check("ok=True", r.ok, r.message)
    check("仅命中切头文件（早于窗口的文件不选）",
          r.stats is not None and r.stats.sliced == 1 and r.stats.copied == 0,
          str(r.stats))
    gz = out_a / "main_00101_2026-09-10_15-25-00.log.gz"
    check("产出 .gz 存在", gz.exists(), str(sorted(p.name for p in out_a.iterdir())))
    lines = read_gz_lines(gz)
    check("切割保留窗口内 3 行（15:23/24/25）", len(lines) == 3, f"{len(lines)}: {lines}")
    check("窗口外行已剔除", all("win20" not in ln and "win21" not in ln and "win22" not in ln
                                for ln in lines))
    check("早于窗口的文件无产出",
          not (out_a / "main_00101_2026-09-10_15-20-00.log").exists()
          and not (out_a / "main_00101_2026-09-10_15-20-00.log.gz").exists())

    print("\n[3] 仅预览（list_only 零落盘）")
    out_never = tmp / "out_never"
    r2 = LogExtractor().extract(str(src_a), time_str="15:23-15:26",
                                output_dir=str(out_never), list_only=True)
    check("ok=True 且 plan 1 项", r2.ok and len(r2.plan) == 1, str(r2.plan))
    check("action=slice_head",
          bool(r2.plan) and r2.plan[0].action == "slice_head",
          str(r2.plan[0] if r2.plan else None))
    check("输出目录未创建（零落盘）", not out_never.exists())

    print("\n[4] overwrite 语义")
    r3 = LogExtractor(overwrite=False).extract(str(src_a), time_str="15:23-15:26",
                                               output_dir=str(out_a))
    check("已存在 + overwrite=False → 拒绝", (not r3.ok) and "已存在" in r3.message, r3.message)
    r4 = LogExtractor().extract(str(src_a), time_str="15:23-15:26", output_dir=str(out_a))
    check("默认 overwrite=True → 清理重建成功", r4.ok, r4.message)

    print("\n[5] --no-slice 路径（按文件名时间过滤 + 整拷）")
    out_ns = tmp / "out_ns"
    r5 = LogExtractor().extract(str(src_a), time_str="15:23-15:26",
                                output_dir=str(out_ns), enable_slice=False)
    check("ok=True 且整拷 1 个", r5.ok and r5.stats is not None and r5.stats.copied == 1,
          str(r5.stats))
    plain = out_ns / "main_00101_2026-09-10_15-25-00.log"
    check("原文件名落盘（未转 .gz）", plain.exists() and not (out_ns / "main_00101_2026-09-10_15-25-00.log.gz").exists())
    check("整拷内容与原文件一致",
          plain.read_text(encoding="utf-8") ==
          (src_a / "main_00101_2026-09-10_15-25-00.log").read_text(encoding="utf-8"))

    print("\n[6] 坏文件容错（有意偏差：单文件 error，后续继续）")
    src_b = tmp / "src_b" / "logs_20260910_160000"
    write_lines(src_b / "main_00201_2026-09-10_15-24-00.log",
                [android_line(f"15:{m:02d}:00", "good") for m in (22, 23, 24)])
    make_bad_gz_truncated(src_b / "main_00202_2026-09-10_15-24-30.gz")
    make_bad_gz_flipped(src_b / "main_00203_2026-09-10_15-24-45.gz")
    out_b = tmp / "out_b"
    r7 = LogExtractor().extract(str(src_b), time_str="15:23-15:26", output_dir=str(out_b))
    check("坏文件不击穿整批（ok=True）", r7.ok, r7.message)
    check("errors=2（截断 + 字节翻转）", r7.stats is not None and r7.stats.errors == 2,
          str(r7.stats))
    check("好文件仍产出", r7.stats is not None and r7.stats.sliced == 1, str(r7.stats))
    good_gz = out_b / "main_00201_2026-09-10_15-24-00.log.gz"
    check("好文件内容正确（15:23/24）",
          good_gz.exists() and len(read_gz_lines(good_gz)) == 2)
    check("坏文件无产出残留",
          not any("main_00202" in p.name or "main_00203" in p.name for p in out_b.rglob("*")))

    print("\n[7] 取消（写阶段中断；已写出文件保留）")
    src_c = tmp / "src_c" / "logs_20260910_161500"
    for i in range(6):
        write_lines(src_c / f"final_{i:03d}.log",
                    [android_line(t, f"c{i}") for t in
                     ("15:23:40", "15:24:10", "15:24:40", "15:25:10", "15:25:40")])
    cancel = threading.Event()

    def on_prog(phase: str, done: int, total: int) -> None:
        if phase == "write" and done >= 2:
            cancel.set()

    out_c = tmp / "out_c"
    r8 = LogExtractor(on_progress=on_prog, cancel=cancel).extract(
        str(src_c), time_str="15:23-15:26", output_dir=str(out_c))
    check("cancelled=True 且 ok=False", r8.cancelled and not r8.ok, r8.message)
    check("提示含「已取消」", "已取消" in r8.message, r8.message)
    check("部分产出保留（0 < written < 6）",
          r8.stats is not None and 0 < len(r8.stats.written) < 6,
          str(r8.stats.written if r8.stats else None))

    print("\n[8] 参数错误快速失败（不抛异常）")
    r9 = LogExtractor().extract(str(tmp / "no_such_dir"), time_str="15:23")
    check("源目录不存在 → ok=False", (not r9.ok) and "源目录" in r9.message, r9.message)
    r10 = LogExtractor().extract(str(src_a), time_str="abc")
    check("时间格式错 → ok=False", (not r10.ok) and "时间格式" in r10.message, r10.message)
    r11 = LogExtractor().extract(str(src_a), time_str="15:23-15:26", date="notadate")
    check("日期格式错 → ok=False", (not r11.ok) and "日期" in r11.message, r11.message)

    print("\n[9] 无匹配 + 时间覆盖提示")
    r12 = LogExtractor().extract(str(src_a), time_str="20:00-20:05",
                                 output_dir=str(tmp / "out_none"))
    check("无匹配 → ok=True（非失败）", r12.ok and "无匹配" in r12.message, r12.message)
    check("无匹配 → output_dir 为空串", r12.output_dir == "")
    check("无匹配 → 不创建输出目录", not (tmp / "out_none").exists())
    hint = coverage_hint(scan_directory(str(src_a)))
    check("coverage_hint 含时间覆盖信息",
          ("文件名时间" in hint) and ("15:18" in hint or "15:20" in hint), hint.splitlines()[0])

    print("\n[10] 页面构建与注册（工具页接线）")
    from cartest.app.tools.logextract_tab import LogExtractTab

    class _Page:
        web = False

        def update(self) -> None:
            pass

        def run_task(self, h, *a, **kw) -> None:  # noqa: ANN001
            pass

    class _StubCtx:
        """ToolContext 替身（页面用到 theme/page/set_status/日志面板/操作留痕）"""

        def __init__(self) -> None:
            from cartest.app.theme import AppTheme
            from cartest.app.state import AppState

            class _Cfg:
                def __init__(self) -> None:
                    self.d: dict = {}

                def get(self, k, default=None):  # noqa: ANN001
                    return self.d.get(k, default)

                def set(self, k, v) -> None:  # noqa: ANN001
                    self.d[k] = v

            self.theme = AppTheme()
            self.cfg = _Cfg()
            self.state = AppState()
            self.page = _Page()
            self.status: list[tuple] = []
            self.logs: list[str] = []
            self._op_id = 0

        def set_status(self, text: str, color_key: str = "text_secondary") -> None:
            self.status.append((text, color_key))

        def append_log_to_panel(self, text: str) -> None:
            self.logs.append(str(text))

        def clear_log_panel(self) -> None:
            self.logs.clear()

        def begin_op(self, title: str, *, tag: str = "工具", param: str = "") -> int:
            self._op_id += 1
            return self._op_id

        def end_op(self, op_id: int, ok: bool = True) -> None:
            pass

    ctx = _StubCtx()
    ok_ui, err_ui = True, ""
    try:
        tab = LogExtractTab(ctx)
        ctl = tab.build()
        check("页面构建（flet kwargs 有效）", ctl is not None)
        tab._source_field.value = str(src_a)
        tab._time_field.value = "15:23-15:26"
        tab._output_field.value = str(tmp / "out_ui")
        tab._run(list_only=True)
        deadline = time.time() + 10
        while tab._running and time.time() < deadline:
            time.sleep(0.05)
        check("预览全链路（状态行 + 按钮复位）",
              not tab._running and tab._btn_cancel.disabled is True, str(ctx.status[-1:]))
        check("状态行含预览结果", any("预览" in t for t, _ in ctx.status), str(ctx.status[-1:]))
        check("日志面板有输出", any("[提取]" in s for s in ctx.logs), str(len(ctx.logs)))
        tab._btn_open.disabled = True  # 预览后打开按钮保持禁用
        check("save/restore_state 不丢输入",
              tab.save_state()["time"] == "15:23-15:26"
              and (tab.restore_state(tab.save_state()) or True))
    except Exception as ex:  # noqa: BLE001
        ok_ui, err_ui = False, f"{type(ex).__name__}: {ex}"
    check("页面整体（构建/预览/状态/日志）", ok_ui, err_ui)

    import importlib
    from cartest.app.tools import pages as tp
    main_mod = importlib.import_module("cartest.app.main")
    from cartest.app import pages as app_pages
    groups = {g: [k for k, _, _ in e] for g, e in main_mod.PAGE_GROUPS}
    check("BUILDERS 含 tool_logextract", "tool_logextract" in tp.BUILDERS)
    check("侧栏「通用工具」含 日志切割（2026-10-15 改名）",
          "tool_logextract" in groups.get("通用工具", []), str(groups))
    check("注册表与 BUILDERS 一致",
          set(main_mod.PAGE_META) == set(app_pages.BUILDERS))
    check("PAGE_META 标签=日志切割",
          main_mod.PAGE_META.get("tool_logextract", ("", ""))[1] == "日志切割")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("时间段提取检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
