# -*- coding: utf-8 -*-
"""CarTest core — 按时间范围提取车载日志（库化）

来源工具：``D:\\SoftWare\\WorkScript\\Script\\car_logs\\extract_logs.py``（1301 行 CLI，2026-09-11）。
本模块是它的库化版：**只搬家不改逻辑**——算法函数、时间戳模式注册表、切割策略原样平移；
CLI 壳（argparse / 交互模式 / print）丢弃，进度经回调、取消经 ``threading.Event``。

行为对齐旧脚本，**唯一有意偏差**（见 36-时间段日志提取设计.md）：
  解压读取中途损坏（gzip CRC / 截断 / zip 成员损坏）按单文件记 ``error`` 继续处理
  其余文件——旧脚本此处无兜底，异常会击穿整批中断（日志被截断在实际使用中常见）。

用法::

    from cartest.core.logextract import extract_logs

    result = extract_logs(r"runs/logs_20260910_152823", time_str="15:23-15:26")
    if result.ok:
        print(result.message, result.output_dir)

带进度/取消::

    ex = LogExtractor(on_log=..., on_progress=..., cancel=ev)
    result = ex.extract(source, time_str="15:23-15:26", list_only=True)   # 仅预览
"""
from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import config as _config

# ============================================================
# 时间戳提取模式注册表
# 格式: (后缀匹配, 编译正则, 日期 strptime 格式)
# 正则必须捕获 4 个组: (日期字符串, 时, 分, 秒)
# 注: 多段后缀（如 .dlt.gz）排在单段后缀（如 .gz）之前，避免误匹配
# ============================================================
TIMESTAMP_PATTERNS = [
    # SYS1: aios_{seq}_YYYYMMDD-HHMMSS.dlt.gz（排在 .gz 之前）
    ('.dlt.gz', re.compile(
        r'^aios_\d{4}_(\d{8})-(\d{2})(\d{2})(\d{2})\.dlt\.gz$'
    ), '%Y%m%d'),

    # SYS1: aios_{seq}_YYYYMMDD-HHMMSS.dlt（排在 .dlt 前）
    ('.dlt', re.compile(
        r'^aios_\d{4}_(\d{8})-(\d{2})(\d{2})(\d{2})\.dlt$'
    ), '%Y%m%d'),

    # Android/voicecontrol: {type}_{pid}_YYYY-MM-DD_HH-MM-SS(_latest)?.gz
    ('.gz', re.compile(
        r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})(?:_latest)?\.gz$'
    ), '%Y-%m-%d'),

    # SYS2/DIM_Slog_Boot: {pid}_{seq}_last?_YYYY-MM-DD_HH-MM-SS.zip
    ('.zip', re.compile(
        r'(?:_last_)?(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.zip$'
    ), '%Y-%m-%d'),

    # SYS2/DIM: DIM_YYYY-MM-DD_HH-MM-SS_{seq}.log
    ('.log', re.compile(
        r'^DIM_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_\d{5}\.log$'
    ), '%Y-%m-%d'),

    # Android/通用.log变体: {type}_{pid}_YYYY-MM-DD_HH-MM-SS(_latest)?.log
    ('.log', re.compile(
        r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})(?:_latest)?\.log$'
    ), '%Y-%m-%d'),
]

# 支持的文件后缀（用于扫描过滤和 mtime 回退匹配）
SUPPORTED_SUFFIXES = {'.gz', '.zip', '.log', '.dlt', '.dlt.gz'}

# 行级时间戳（用于边界文件切割）
# 与 log_preprocessor 保持一致，供本模块独立使用
_LINE_TS_PATTERNS = [
    # SYS2/DIM: 2026/07/29 12:24:42.440022
    (re.compile(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})[.,](\d+)'),
     'slash'),
    # SYS1 / 通用: 2026-07-29 12:24:42[.123]
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?'),
     'dash_full'),
    # Android: 07-29 12:24:24.310（需外部补年份）
    (re.compile(r'(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})[.,](\d{3})'),
     'android'),
]

#: 回调签名
OnLog = Callable[[str, str], None]            # (level, msg)；level: info | warn | error
OnProgress = Callable[[str, int, int], None]  # (phase, done, total)；total=0 表示未知

#: 切割行循环的节流粒度（进度/取消）
_CANCEL_EVERY_LINES = 2000
_PROGRESS_EVERY_LINES = 5000
_PROGRESS_EVERY_FILES = 50


def default_output_base() -> str:
    """平台默认输出根目录（``<DATA_DIR>/tools/logextract``，与工具页常量同址）"""
    return os.path.join(str(_config.DATA_DIR), "tools", "logextract")


# ── 结果类型 ──────────────────────────────────────────

@dataclass
class PlanItem:
    """一个待处理文件（预览与摘要共用）"""
    full_path: str
    rel_dir: str
    name: str
    action: str                      # copy | slice_head | slice_tail | slice_both


@dataclass
class ExtractStats:
    """输出统计（字段与旧脚本 stats dict 一致）"""
    copied: int = 0
    sliced: int = 0
    slice_lines: int = 0
    binary_copied: int = 0
    unfilterable: int = 0
    empty_slice: int = 0
    errors: int = 0
    written: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class ExtractResult:
    """提取结果。``ok=True`` 表示流程正常走完（含"无匹配"）；失败/取消时 ``ok=False``。"""
    ok: bool
    message: str
    output_dir: str = ""
    stats: ExtractStats | None = None
    plan: list[PlanItem] = field(default_factory=list)
    cancelled: bool = False


# ============================================================
# 纯逻辑（自旧脚本原样平移）
# ============================================================

def detect_date_from_path(path):
    """从路径中自动提取 YYYYMMDD 格式的日期"""
    m = re.search(r'(\d{8})', os.path.basename(os.path.abspath(path)))
    return m.group(1) if m else None


def parse_time_arg(time_str, range_min=5):
    """
    解析时间参数，返回 (after_h, after_m, before_h, before_m, has_start)

    支持格式:
      "15:40-15:45" / "1540-1545"     → 直接区间
      "15:40-" / "1540-"              → 从此时间往后 (至 24:00)
      "15:42" / "1542"                → 中心时间，± range_min 分钟展开
    返回的值中 before 为不含 (exclusive)，内部自行处理。
    """
    # 紧凑区间: HHMM-HHMM 或 HHMM~HHMM (如 1540-1545)
    m = re.match(r'^(\d{4})\s*[-~]\s*(\d{4})$', time_str)
    if m:
        ah, am = int(m.group(1)[:2]), int(m.group(1)[2:])
        bh, bm = int(m.group(2)[:2]), int(m.group(2)[2:])
        return (ah, am, bh, bm, True)

    # 紧凑无上界: HHMM- 或 HHMM~ (如 1540-)
    m = re.match(r'^(\d{4})\s*[-~]$', time_str)
    if m:
        ah, am = int(m.group(1)[:2]), int(m.group(1)[2:])
        return (ah, am, 24, 0, True)

    # 紧凑单点: HHMM (如 1542)
    m = re.match(r'^(\d{4})$', time_str)
    if m:
        ch, cm = int(m.group(1)[:2]), int(m.group(1)[2:])
        ct = ch * 60 + cm
        st = max(0, ct - range_min)
        et = min(24 * 60, ct + range_min)
        return (st // 60, st % 60, et // 60, et % 60, True)

    # 冒号区间: HH:MM-HH:MM 或 HH:MM~HH:MM
    m = re.match(r'^(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})$', time_str)
    if m:
        ah, am = int(m.group(1)), int(m.group(2))
        bh, bm = int(m.group(3)), int(m.group(4))
        return (ah, am, bh, bm, True)

    # 冒号无上界: HH:MM- 或 HH:MM~ (如 15:40-)
    m = re.match(r'^(\d{1,2}):(\d{2})\s*[-~]$', time_str)
    if m:
        ah, am = int(m.group(1)), int(m.group(2))
        return (ah, am, 24, 0, True)

    # 冒号单点: HH:MM
    m = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if m:
        ch, cm = int(m.group(1)), int(m.group(2))
        ct = ch * 60 + cm
        st = max(0, ct - range_min)
        et = min(24 * 60, ct + range_min)
        return (st // 60, st % 60, et // 60, et % 60, True)

    raise ValueError(f"Invalid time format: {time_str}. Use HH:MM-HH:MM or HH:MM")


def normalize_date(date_str, fmt):
    """将捕获的日期字符串统一转为 YYYYMMDD 格式"""
    try:
        return datetime.strptime(date_str, fmt).strftime('%Y%m%d')
    except ValueError:
        return None


def extract_timestamp(filename):
    """
    从文件名提取时间戳。

    返回 (date_yyyymmdd, hour, minute, second) 或 None。
    None 表示文件名不含时间戳，需回退到文件修改时间。
    """
    for suffix, regex, date_fmt in TIMESTAMP_PATTERNS:
        if not filename.endswith(suffix):
            continue
        m = regex.search(filename)
        if not m:
            continue
        groups = m.groups()
        if len(groups) < 3:
            continue
        date_str = groups[0]
        h = int(groups[1])
        minute = int(groups[2])
        sec = int(groups[3]) if len(groups) > 3 else 0
        normalized = normalize_date(date_str, date_fmt)
        if normalized:
            return (normalized, h, minute, sec)
        # 日期解析失败，放弃此模式
        return None
    return None


def match_time_range(file_date, file_h, file_m, file_s,
                     target_date, after_total, before_total, has_start):
    """判断文件时间是否在目标范围内"""
    if target_date and file_date != target_date:
        return False
    file_total = file_h * 3600 + file_m * 60 + file_s
    if has_start:
        return after_total <= file_total < before_total
    else:
        return file_total < before_total


def infer_year_from_path(path):
    """从路径中推断年份（logs_/batch_/carlog_ 目录名）"""
    pattern = re.compile(r'(?:logs_|batch_|carlog_)(\d{4})(\d{2})(\d{2})[_\-]')
    current = os.path.abspath(path)
    while current and current not in ('/', '\\', '.'):
        m = pattern.search(os.path.basename(current))
        if m:
            return int(m.group(1))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def parse_line_timestamp(line, default_year=None):
    """解析日志行时间戳，返回 (date_yyyymmdd, seconds_of_day) 或 None"""
    for regex, kind in _LINE_TS_PATTERNS:
        m = regex.search(line)
        if not m:
            continue
        try:
            if kind == 'slash':
                # Y/M/D H:M:S.frac
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                h, mi, s = int(m.group(4)), int(m.group(5)), int(m.group(6))
            elif kind == 'dash_full':
                # Y-M-D H:M:S[.frac]
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                h, mi, s = int(m.group(4)), int(m.group(5)), int(m.group(6))
            else:  # android: M-D H:M:S.mmm
                y = default_year or datetime.now().year
                mo, d = int(m.group(1)), int(m.group(2))
                h, mi, s = int(m.group(3)), int(m.group(4)), int(m.group(5))
            date_str = f'{y:04d}{mo:02d}{d:02d}'
            sod = h * 3600 + mi * 60 + s
            return date_str, sod
        except (ValueError, TypeError):
            continue
    return None


def line_in_range(line, target_date, after_total, before_total, has_start,
                  default_year=None):
    """判断单行是否落在目标时间范围内。无法解析时间戳的行返回 None（由调用方决定去留）。"""
    ts = parse_line_timestamp(line, default_year)
    if ts is None:
        return None
    line_date, sod = ts
    if target_date and line_date != target_date:
        return False
    if has_start:
        return after_total <= sod < before_total
    else:
        return sod < before_total


def stream_key(filename):
    """从文件名提取日志流标识（去掉时间戳/序号），用于按流分组切割头尾。

    aios_0001_20260910-151821.dlt.gz     → aios
    main_00211_2026-09-10_15-22-33.gz    → main_00211
    DIM_2026-09-10_15-23-09_00060.log    → DIM
    system_00211.log                     → system_00211
    """
    name = filename

    # SYS1: aios_{seq}_YYYYMMDD-HHMMSS.dlt[.gz]
    m = re.match(r'^(aios)_\d{4}_\d{8}-\d{6}\.dlt(\.gz)?$', name, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # DIM: DIM_YYYY-MM-DD_HH-MM-SS_{seq}.log
    m = re.match(r'^(DIM)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d+\.log$', name)
    if m:
        return m.group(1)

    # Android/通用: {type}_{pid}_YYYY-MM-DD_HH-MM-SS(_latest)?.gz
    m = re.match(
        r'^([A-Za-z][A-Za-z0-9]*_\d+)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_latest)?\.gz$',
        name
    )
    if m:
        return m.group(1)

    # Android/通用 .log 变体
    m = re.match(
        r'^([A-Za-z][A-Za-z0-9]*_\d+)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_latest)?\.log$',
        name
    )
    if m:
        return m.group(1)

    # 回退：去掉常见时间戳片段
    name = re.sub(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}', '', name)
    name = re.sub(r'\d{8}-\d{6}', '', name)
    name = re.sub(r'(_latest)?\.(gz|zip|log|dlt|dlt\.gz)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[_\-\s]+', '_', name).strip('_')
    return name or filename


def open_log_text_stream(filepath):
    """按后缀打开日志为文本行迭代器。返回 (iterator, close_fn)。"""
    lower = filepath.lower()

    if lower.endswith('.gz'):
        fh = gzip.open(filepath, 'rt', encoding='utf-8', errors='replace')
        return fh, fh.close

    if lower.endswith('.zip'):
        zf = zipfile.ZipFile(filepath, 'r')
        members = [n for n in zf.namelist()
                   if not n.endswith('/') and not n.startswith('__MACOSX')]
        if not members:
            zf.close()
            return iter(()), (lambda: None)

        def _zip_lines():
            try:
                for name in members:
                    with zf.open(name) as raw:
                        wrapper = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
                        for line in wrapper:
                            yield line
                        wrapper.detach()
            finally:
                zf.close()

        return _zip_lines(), (lambda: None)

    fh = open(filepath, 'r', encoding='utf-8', errors='replace')
    return fh, fh.close


def is_probably_binary_dlt(filepath):
    """粗略判断是否为二进制 SYS2（不可按行文本切割）"""
    lower = filepath.lower()
    if lower.endswith('.dlt') or lower.endswith('.dlt.gz'):
        return True
    return False


def ensure_gz_name(path):
    """确保输出文件名为 .gz"""
    lower = path.lower()
    if lower.endswith('.gz'):
        return path
    if lower.endswith('.dlt'):
        return path + '.gz'
    if lower.endswith('.zip'):
        return path[:-4] + '.gz'
    if lower.endswith('.log'):
        return path + '.gz'
    return path + '.gz'


def _file_seconds(ts):
    """ts = (date, h, m, s) → seconds of day，或 None"""
    if not ts:
        return None
    return ts[1] * 3600 + ts[2] * 60 + ts[3]


def overlaps_range(first_sod, last_sod, after_total, before_total, has_start):
    """判断探测到的行时间范围是否与 [after, before) 可能重叠"""
    if first_sod is None or last_sod is None:
        return True
    if last_sod < after_total:
        return False
    if first_sod >= before_total:
        return False
    return True


def build_output_path(base_dir, date_str, has_start,
                      after_h, after_m, before_h, before_m):
    """根据参数自动生成输出路径（位于 ``base_dir`` 下）"""
    end_time = f"{before_h:02d}{before_m:02d}"
    date_part = date_str if date_str else "all"
    if has_start:
        start_time = f"{after_h:02d}{after_m:02d}"
        return os.path.join(base_dir, f"logs_{date_part}_{start_time}_to_{end_time}")
    else:
        return os.path.join(base_dir, f"logs_{date_part}_to_{end_time}")


def build_filter_desc(date_str, after_h, after_m, before_h, before_m, has_start):
    """构建时间范围描述字符串"""
    if has_start:
        time_part = f"{after_h:02d}:{after_m:02d} ~ {before_h:02d}:{before_m:02d}"
    else:
        time_part = f"before {before_h:02d}:{before_m:02d}"
    if date_str:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {time_part}"
    else:
        return f"all dates {time_part}"


def _fmt_sod(sod):
    """seconds_of_day → HH:MM:SS"""
    if sod is None:
        return '?'
    h = sod // 3600
    m = (sod % 3600) // 60
    s = sod % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


# ============================================================
# IO 核心（自旧脚本平移 + 进度/取消接线）
# ============================================================

def scan_directory(source_root, *, on_progress: OnProgress | None = None,
                   cancel: threading.Event | None = None):
    """
    递归扫描源目录，返回日志文件列表。

    返回: [(full_path, relative_dir, basename, timestamp_or_none), ...]
    """
    matches = []
    source_root = os.path.abspath(source_root)

    for dirpath, _dirnames, filenames in os.walk(source_root):
        for fname in filenames:
            if cancel is not None and cancel.is_set():
                return matches
            supported = any(fname.endswith(s) for s in SUPPORTED_SUFFIXES)
            if not supported:
                continue

            full_path = os.path.join(dirpath, fname)
            rel_dir = os.path.relpath(dirpath, source_root)
            ts = extract_timestamp(fname)
            matches.append((full_path, rel_dir, fname, ts))
            if on_progress is not None and len(matches) % _PROGRESS_EVERY_FILES == 0:
                on_progress('scan', len(matches), 0)

    return matches


def peek_line_time_range(filepath, default_year=None, sample_tail_bytes=256 * 1024):
    """
    探测文本日志的行时间范围（首/末可解析时间戳的 seconds_of_day）。

    返回 (first_sod, last_sod) 或 None。
    .log：读文件头 + seek 尾部；.gz：只读开头若干行（last 可能等于 first）。
    """
    if is_probably_binary_dlt(filepath):
        return None

    lower = filepath.lower()
    try:
        if lower.endswith('.gz'):
            first = None
            with gzip.open(filepath, 'rt', encoding='utf-8', errors='replace') as f:
                for i, line in enumerate(f):
                    ts = parse_line_timestamp(line, default_year)
                    if ts:
                        first = ts[1]
                        break
                    if i >= 200:
                        break
            if first is None:
                return None
            # gz 难以廉价 seek 到末尾；last 暂用 first，重叠判断偏保守
            return (first, first)

        size = os.path.getsize(filepath)
        first = None
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for _ in range(80):
                line = f.readline()
                if not line:
                    break
                ts = parse_line_timestamp(line, default_year)
                if ts:
                    first = ts[1]
                    break

        last = None
        with open(filepath, 'rb') as f:
            f.seek(max(0, size - sample_tail_bytes))
            chunk = f.read().decode('utf-8', errors='replace')
        for line in reversed(chunk.splitlines()):
            ts = parse_line_timestamp(line, default_year)
            if ts:
                last = ts[1]
                break

        if first is None and last is None:
            return None
        if first is None:
            first = last
        if last is None:
            last = first
        return (first, last)

    except (OSError, gzip.BadGzipFile, EOFError, zlib.error):
        # 有意偏差#2（36 号文档）：损坏 gz 的探测同样不得向调用方抛异常
        # （原版漏捕 zlib.error——中段损坏属该类，会击穿选取阶段）
        return None


def select_overlapping_files(all_matches, target_date, after_total, before_total,
                             has_start, default_year=None, *,
                             on_progress: OnProgress | None = None,
                             cancel: threading.Event | None = None):
    """
    选出与时间范围有重叠的日志文件，并规划头尾切割。

    实测约定（Android main_*.gz / DIM_*.log）：文件名时间戳是该文件内容的
    结束时刻；同流内上一文件的结束时刻即本文件起点。与 [after, before) 有
    重叠则选中；头尾边界文件标记为 slice_*，中间整文件为 copy。

    流内首文件（无 prev）会 peek 行时间：明显不重叠则跳过；无行时间戳时
    仅当文件名结束时刻贴近时间段才保留。

    返回: [(full_path, rel_dir, fname, action), ...]
      action: copy | slice_head | slice_tail | slice_both
    """
    groups = {}
    for full_path, rel_dir, fname, ts in all_matches:
        if ts is None:
            continue
        file_date = ts[0]
        if target_date and file_date != target_date:
            continue
        key = (rel_dir, stream_key(fname))
        groups.setdefault(key, []).append((full_path, rel_dir, fname, ts))

    selected = []
    processed = 0
    for items in groups.values():
        # 按文件名时间升序（该时间 = 内容结束时刻）
        items.sort(key=lambda x: (_file_seconds(x[3]), x[2]))
        n = len(items)
        for i, (full_path, rel_dir, fname, ts) in enumerate(items):
            if cancel is not None and cancel.is_set():
                return selected
            processed += 1
            if on_progress is not None and processed % 20 == 0:
                on_progress('select', processed, 0)

            end_s = _file_seconds(ts)
            prev_end_s = _file_seconds(items[i - 1][3]) if i > 0 else None

            # 内容区间约 (prev_end_s, end_s]
            # 与 [after, before) 重叠 ⟺ end_s > after 且 (无 prev 或 prev < before)
            if has_start and end_s <= after_total:
                continue
            if prev_end_s is not None and prev_end_s >= before_total:
                continue

            # 流内首文件：用行时间/文件名贴近度再确认，避免把很远的当前日志拖进来
            if prev_end_s is None:
                pr = peek_line_time_range(full_path, default_year=default_year)
                if pr is not None:
                    first_sod, last_sod = pr
                    # .gz peek 只能读到首行（last≈first）；文件名时间=内容结束时刻，
                    # 用它修正 last，避免把「早于窗口开始但延伸进窗口」的文件误丢
                    if end_s > last_sod:
                        last_sod = end_s
                    if last_sod < after_total or first_sod >= before_total:
                        continue
                else:
                    # 无行时间戳：仅当结束时刻在 [after, before+30min] 才保留
                    if not (after_total <= end_s <= before_total + 30 * 60):
                        continue

            # 首文件无 prev：内容可能远早于范围起点 → 切头
            # 有 prev：prev < after 时文件含范围前内容 → 切头
            if has_start:
                need_head = (prev_end_s is None) or (prev_end_s < after_total)
            else:
                need_head = False

            need_tail = end_s > before_total

            if need_head and need_tail:
                action = 'slice_both'
            elif need_head:
                action = 'slice_head'
            elif need_tail:
                action = 'slice_tail'
            else:
                action = 'copy'

            selected.append((full_path, rel_dir, fname, action))

    selected.sort(key=lambda x: (_file_seconds(extract_timestamp(x[2])) or 0, x[1], x[2]))
    return selected


def select_untimed_files(all_matches, target_date, after_total, before_total,
                         has_start, default_year=None, *,
                         on_progress: OnProgress | None = None,
                         cancel: threading.Event | None = None):
    """
    文件名无时间戳的日志：按行时间全量过滤（不用 mtime）。

    先 peek 首/末行时间，明显不重叠则跳过；否则标记 slice_both。
    返回 [(full_path, rel_dir, fname, 'slice_both'), ...]
    """
    selected = []
    processed = 0
    for full_path, rel_dir, fname, ts in all_matches:
        if cancel is not None and cancel.is_set():
            return selected
        if ts is not None:
            continue
        if is_probably_binary_dlt(fname):
            continue

        processed += 1
        if on_progress is not None and processed % 20 == 0:
            on_progress('select', processed, 0)

        pr = peek_line_time_range(full_path, default_year=default_year)
        if pr is None:
            continue
        first_sod, last_sod = pr
        if not overlaps_range(first_sod, last_sod, after_total, before_total, has_start):
            continue
        selected.append((full_path, rel_dir, fname, 'slice_both'))

    selected.sort(key=lambda x: (x[1], x[2]))
    return selected


def filter_by_time(matches, target_date, after_h, after_m,
                   before_h, before_m, has_start):
    """
    根据时间范围过滤匹配列表（``--no-slice`` 路径）。

    返回: [(full_path, relative_dir, basename), ...]
    """
    after_total = after_h * 3600 + after_m * 60
    before_total = before_h * 3600 + before_m * 60
    results = []

    for full_path, rel_dir, fname, ts in matches:
        if ts is not None:
            file_date, file_h, file_m, file_s = ts
            if match_time_range(file_date, file_h, file_m, file_s,
                                target_date, after_total, before_total, has_start):
                results.append((full_path, rel_dir, fname))
        else:
            # 文件名无时间戳，使用文件修改时间
            mtime = os.path.getmtime(full_path)
            dt = datetime.fromtimestamp(mtime)
            mdate = dt.strftime('%Y%m%d')
            if match_time_range(mdate, dt.hour, dt.minute, dt.second,
                                target_date, after_total, before_total, has_start):
                results.append((full_path, rel_dir, fname))

    return results


def sort_by_timestamp(file_list):
    """按日志时间戳排序（文件名时间优先，mtime 回退）"""
    def _sort_key(item):
        full_path, _rel_dir, fname = item
        ts = extract_timestamp(fname)
        if ts:
            date_str, h, m, s = ts
            return (date_str, h, m, s, fname)
        else:
            mtime = os.path.getmtime(full_path)
            dt = datetime.fromtimestamp(mtime)
            return (dt.strftime('%Y%m%d'), dt.hour, dt.minute, dt.second, fname)

    return sorted(file_list, key=_sort_key)


def slice_log_file(src_path, dest_path, target_date,
                   after_total, before_total, has_start, default_year=None, *,
                   on_log: OnLog | None = None,
                   on_progress: OnProgress | None = None,
                   cancel: threading.Event | None = None):
    """
    解压/读取日志，按行时间戳过滤后写入 .gz。

    返回 (kept_lines, total_lines, status)
      status: 'sliced' | 'copied_binary' | 'empty' | 'unfilterable' | 'error' | 'cancelled'
    无法解析时间戳的行：若文件内多数行可解析则丢弃，否则保留（避免误删无时间戳日志）。

    **有意偏差**：读取中途损坏（gzip CRC/截断等）记 'error' 返回，不再击穿整批。
    """
    dest_path = ensure_gz_name(dest_path)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or '.', exist_ok=True)

    # 二进制 SYS2 不按行切割，整文件重压缩
    if is_probably_binary_dlt(src_path):
        try:
            if src_path.lower().endswith('.gz'):
                shutil.copy2(src_path, dest_path)
            else:
                with open(src_path, 'rb') as fin, gzip.open(dest_path, 'wb') as fout:
                    shutil.copyfileobj(fin, fout)
            return 0, 0, 'copied_binary'
        except OSError as e:
            if on_log is not None:
                on_log('error', f"binary copy {src_path}: {e}")
            return 0, 0, 'error'

    try:
        stream, close_fn = open_log_text_stream(src_path)
    except (OSError, zipfile.BadZipFile, gzip.BadGzipFile) as e:
        if on_log is not None:
            on_log('error', f"open {src_path}: {e}")
        return 0, 0, 'error'

    kept = []
    total = 0
    parsed = 0
    unparsed_kept = 0
    read_error = None

    try:
        buffer = []
        try:
            for line in stream:
                total += 1
                if cancel is not None and total % _CANCEL_EVERY_LINES == 0 \
                        and cancel.is_set():
                    return 0, total, 'cancelled'
                if on_progress is not None and total % _PROGRESS_EVERY_LINES == 0:
                    on_progress('slice', total, 0)
                decision = line_in_range(
                    line, target_date, after_total, before_total, has_start,
                    default_year=default_year
                )
                if decision is None:
                    buffer.append(line)
                    continue
                parsed += 1
                if decision:
                    # 无时间戳前缀行（堆栈等）附着到上一条命中行之后
                    kept.extend(buffer)
                    buffer.clear()
                    kept.append(line)
                    unparsed_kept = 0
                else:
                    buffer.clear()
            # 文件尾部无时间戳行：多数可解析则丢弃；几乎不可解析则整文件视为无法切割
            if buffer:
                if total > 0 and parsed / max(total, 1) >= 0.5:
                    pass
                else:
                    kept.extend(buffer)
                    unparsed_kept = len(buffer)
        except Exception as e:  # noqa: BLE001
            # 有意偏差#1（36 号文档）：读取中途损坏按单文件记 error，继续其余文件
            read_error = e
    finally:
        try:
            close_fn()
        except Exception:
            pass

    if read_error is not None:
        if on_log is not None:
            on_log('error', f"读取失败 {src_path}: {read_error}")
        return 0, total, 'error'

    # 几乎没有可解析行时间 → 无法按行切割（如 /proc 快照）
    if total > 0 and parsed / max(total, 1) < 0.05:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        return 0, total, 'unfilterable'

    if not kept:
        return 0, total, 'empty'

    try:
        with gzip.open(dest_path, 'wt', encoding='utf-8', newline='') as fout:
            fout.writelines(kept)
    except OSError as e:
        if on_log is not None:
            on_log('error', f"write {dest_path}: {e}")
        return 0, total, 'error'

    _ = unparsed_kept
    return len(kept), total, 'sliced'


def apply_extraction(file_list, output_root, target_date,
                     after_total, before_total, has_start,
                     default_year=None, actions_map=None, *,
                     on_log: OnLog | None = None,
                     on_progress: OnProgress | None = None,
                     cancel: threading.Event | None = None) -> ExtractStats:
    """
    执行输出：中间文件整拷，头尾边界文件解压切割后写 .gz。

    ``file_list``: [(full_path, rel_dir, fname), ...]
    ``actions_map``: {full_path: (action, rel_dir, fname)} —— 由选取阶段规划。

    返回 :class:`ExtractStats`。
    """
    stats = ExtractStats()

    actions = actions_map or {
        fp: ('copy', rel, fname)
        for fp, rel, fname in file_list
    }

    action_labels = {
        'copy': '[整拷]',
        'slice_head': '[切头]',
        'slice_tail': '[切尾]',
        'slice_both': '[两端切]',
    }

    total_files = len(file_list)
    for idx, (full_path, rel_dir, fname) in enumerate(file_list, start=1):
        if cancel is not None and cancel.is_set():
            break
        if on_progress is not None:
            on_progress('write', idx - 1, total_files)

        action, rel, name = actions[full_path]
        dest_dir = os.path.join(output_root, rel)
        os.makedirs(dest_dir, exist_ok=True)

        if action == 'copy':
            dest_path = os.path.join(dest_dir, name)
            try:
                shutil.copy2(full_path, dest_path)
                stats.copied += 1
                stats.written.append((dest_path, rel, name))
            except OSError as e:
                if on_log is not None:
                    on_log('error', f"{name}: {e}")
                stats.errors += 1
            continue

        dest_path = ensure_gz_name(os.path.join(dest_dir, name))
        if on_log is not None:
            on_log('info', f"{action_labels.get(action, '[Slice]')} "
                            f"{os.path.join(rel, name)} -> {os.path.basename(dest_path)}")

        kept, total, status = slice_log_file(
            full_path, dest_path, target_date,
            after_total, before_total, has_start,
            default_year=default_year,
            on_log=on_log, on_progress=on_progress, cancel=cancel,
        )
        if status == 'sliced':
            stats.sliced += 1
            stats.slice_lines += kept
            stats.written.append((dest_path, rel, os.path.basename(dest_path)))
            pct = (kept / total * 100) if total else 0
            if on_log is not None:
                on_log('info', f"           保留 {kept}/{total} 行 ({pct:.1f}%)")
        elif status == 'copied_binary':
            stats.binary_copied += 1
            stats.written.append((dest_path, rel, os.path.basename(dest_path)))
            if on_log is not None:
                on_log('info', "           二进制 SYS2，已整拷")
        elif status == 'unfilterable':
            # 无行时间戳：无法按时段切割，默认跳过（避免拖入无关大文件）
            stats.unfilterable += 1
            if on_log is not None:
                on_log('warn', "           无行时间戳，已跳过")
        elif status == 'empty':
            stats.empty_slice += 1
            if on_log is not None:
                on_log('warn', "           时间范围内无日志行，已跳过")
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
        elif status == 'cancelled':
            break
        else:
            stats.errors += 1

    return stats


def coverage_hint(all_matches, default_year=None, max_untimed_peeks=12):
    """
    汇总源目录日志大致时间覆盖，用于"无匹配/无输出"时提示用户（旧版打印版去 print）。
    """
    dated = {}   # yyyymmdd -> [min_sod, max_sod]
    undated = 0
    samples = []

    for full_path, rel_dir, fname, ts in all_matches:
        if ts is not None:
            date_str, h, m, s = ts
            sod = h * 3600 + m * 60 + s
            slot = dated.setdefault(date_str, [sod, sod])
            slot[0] = min(slot[0], sod)
            slot[1] = max(slot[1], sod)
        else:
            undated += 1
            if len(samples) < max_untimed_peeks:
                pr = peek_line_time_range(full_path, default_year=default_year)
                if pr:
                    samples.append((fname, pr[0], pr[1]))

    lines = []
    lines.append("[提示] 源目录日志时间覆盖（请核对输入时间段）:")

    if dated:
        for date_str in sorted(dated.keys()):
            lo, hi = dated[date_str]
            pretty = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'
            lines.append(f"  · 文件名时间: {pretty}  {_fmt_sod(lo)} ~ {_fmt_sod(hi)}")
    else:
        lines.append("  · 文件名时间: 无")

    if samples:
        lo = min(s[1] for s in samples)
        hi = max(s[2] for s in samples)
        lines.append(
            f"  · 无文件名时间日志(抽样{len(samples)}个): 行时间约 {_fmt_sod(lo)} ~ {_fmt_sod(hi)}"
        )
        for fname, a, b in samples[:5]:
            lines.append(f"      - {fname}: {_fmt_sod(a)} ~ {_fmt_sod(b)}")
    elif undated:
        lines.append(f"  · 无文件名时间日志: {undated} 个（未能解析出行时间）")

    lines.append("  说明: 目录名中的 HHMMSS 往往是打包时刻，不代表日志写到了该时刻。")
    lines.append("  建议: 按上方覆盖范围重新输入时间；可先点「预览」查看计划。")
    return '\n'.join(lines)


# ============================================================
# 入口类（编排；对照旧 run_extraction）
# ============================================================

class LogExtractor:
    """按时间范围提取车载日志（可注入回调 / 取消 / overwrite 控制）

    使用::

        ex = LogExtractor(on_log=lambda lv, m: print(lv, m))
        ex.extract(source, time_str="15:23-15:26")          # 输出到平台默认目录
        ex.extract(source, time_str="1542", list_only=True) # 仅预览
    """

    def __init__(self, *, on_log: OnLog | None = None,
                 on_progress: OnProgress | None = None,
                 cancel: threading.Event | None = None,
                 overwrite: bool = True) -> None:
        self._on_log = on_log
        self._on_progress = on_progress
        self._cancel = cancel or threading.Event()
        self._overwrite = overwrite

    # ── 内部小工具 ────────────────────────────────────
    def _log(self, level: str, msg: str) -> None:
        if self._on_log is not None:
            self._on_log(level, msg)

    def _progress(self, phase: str, done: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(phase, done, total)

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── 主流程 ────────────────────────────────────────
    def extract(self, source, *, time_str=None, range_min=5, date=None,
                after_h=-1, after_m=0, before_h=24, before_m=0,
                output_dir=None, list_only=False,
                enable_slice=True) -> ExtractResult:
        """执行提取流程（旧 ``run_extraction`` 的库化）

        ``time_str`` 优先（"15:40-15:45" / "1542"）；否则用显式
        ``after_h/after_m/before_h/before_m``（``after_h<0`` 表示无下界）。
        """
        # ── 校验（旧 main 的校验语义）──
        if not source or not os.path.isdir(source):
            return ExtractResult(False, f"源目录不存在: {source}")

        if time_str:
            try:
                after_h, after_m, before_h, before_m, has_start = \
                    parse_time_arg(time_str, range_min)
            except ValueError as e:
                return ExtractResult(False, f"时间格式错误: {e}")
        else:
            has_start = (after_h >= 0)

        date_str = date or detect_date_from_path(source)
        if date_str and not re.match(r'^\d{8}$', date_str):
            return ExtractResult(False, f"日期格式错误: {date_str}")

        after_total = after_h * 3600 + after_m * 60
        before_total = before_h * 3600 + before_m * 60
        filter_desc = build_filter_desc(
            date_str, after_h, after_m, before_h, before_m, has_start)

        if not output_dir:
            output_dir = build_output_path(
                default_output_base(), date_str, has_start,
                after_h, after_m, before_h, before_m)

        self._log('info', f"时间范围: {filter_desc}")
        self._log('info', f"源目录: {os.path.abspath(source)}")
        self._log('info', f"头尾切割: {'开' if enable_slice else '关'}"
                          f"{'（仅预览）' if list_only else ''}")
        if not list_only:
            self._log('info', f"输出目录: {output_dir}")

        # ── 扫描 ──
        self._log('info', "[扫描] 递归查找日志文件 ...")
        all_matches = scan_directory(
            source, on_progress=self._on_progress, cancel=self._cancel)
        if self._cancelled():
            return ExtractResult(False, "已取消（扫描阶段）", cancelled=True)
        self._log('info', f"  发现 {len(all_matches)} 个候选文件")

        default_year = infer_year_from_path(source)

        # ── 选取 ──
        if enable_slice:
            self._log('info', "[选取] 带时间戳文件 (重叠 + 头尾切割) ...")
            overlap = select_overlapping_files(
                all_matches, date_str, after_total, before_total, has_start,
                default_year=default_year,
                on_progress=self._on_progress, cancel=self._cancel)
            if self._cancelled():
                return ExtractResult(False, "已取消（选取阶段）", cancelled=True)
            self._log('info', "[选取] 无时间戳文件 (按行时间全量过滤) ...")
            untimed = select_untimed_files(
                all_matches, date_str, after_total, before_total, has_start,
                default_year=default_year,
                on_progress=self._on_progress, cancel=self._cancel)
            if self._cancelled():
                return ExtractResult(False, "已取消（选取阶段）", cancelled=True)
            filtered = [(fp, rel, fn) for fp, rel, fn, _a in overlap]
            actions_map = {fp: (action, rel, fn) for fp, rel, fn, action in overlap}
            for fp, rel, fn, action in untimed:
                filtered.append((fp, rel, fn))
                actions_map[fp] = (action, rel, fn)
            self._log('info', f"  命中: {len(overlap)} 带时间戳 + {len(untimed)} 无时间戳 "
                              f"= {len(filtered)} 个文件")
        else:
            filtered = filter_by_time(
                all_matches, date_str,
                after_h, after_m, before_h, before_m, has_start)
            actions_map = {fp: ('copy', rel, fn) for fp, rel, fn in filtered}
            self._log('info', f"  命中: {len(filtered)} 个文件")

        if filtered:
            filtered = sort_by_timestamp(filtered)

        plan = [PlanItem(fp, rel, fn, actions_map[fp][0])
                for fp, rel, fn in filtered]

        # ── 无匹配 ──
        if not filtered:
            self._log('warn', "未生成结果（无匹配文件）")
            self._log('info', f"  你请求的时间: {filter_desc}")
            self._log('info', f"  候选日志文件: {len(all_matches)} 个")
            for line in coverage_hint(all_matches, default_year=default_year).splitlines():
                self._log('info', line)
            return ExtractResult(True, "无匹配文件（未生成输出）", plan=plan)

        # ── 仅预览 ──
        if list_only:
            self._log('info', f"[预览] {len(plan)} 个文件（未写盘）")
            return ExtractResult(True, f"预览: 命中 {len(plan)} 个文件", plan=plan)

        # ── 写输出 ──
        if os.path.exists(output_dir):
            if self._overwrite:
                self._log('info', f"[清理] 删除旧输出: {output_dir}")
                try:
                    shutil.rmtree(output_dir)
                except OSError as e:
                    return ExtractResult(False, f"旧输出清理失败: {e}")
            else:
                return ExtractResult(False, f"输出目录已存在（未覆盖）: {output_dir}",
                                     output_dir=output_dir)

        self._log('info', f"[提取] 处理 {len(filtered)} 个文件 ...")
        stats = apply_extraction(
            filtered, output_dir, date_str,
            after_total, before_total, has_start,
            default_year=default_year,
            actions_map=actions_map,
            on_log=self._on_log, on_progress=self._on_progress, cancel=self._cancel,
        )

        if self._cancelled():
            self._log('warn', "已取消（已写出的文件保留）")
            return ExtractResult(False, "已取消（已写出的文件保留）",
                                 output_dir=output_dir, stats=stats,
                                 plan=plan, cancelled=True)

        # ── 汇总（旧 print 版统计）──
        self._log('info', f"  整拷:     {stats.copied}")
        self._log('info', f"  已切割:   {stats.sliced} (保留 {stats.slice_lines} 行)")
        if stats.binary_copied:
            self._log('info', f"  二进制SYS2: {stats.binary_copied} (整拷)")
        if stats.unfilterable:
            self._log('info', f"  无行时间: {stats.unfilterable} (已跳过)")
        if stats.empty_slice:
            self._log('info', f"  空结果:   {stats.empty_slice} (已跳过)")
        if stats.errors:
            self._log('warn', f"  错误:     {stats.errors}")

        if not stats.written:
            self._log('warn', "未生成输出文件（无命中行或无法切割）")
            for line in coverage_hint(all_matches, default_year=default_year).splitlines():
                self._log('info', line)
            return ExtractResult(True, "未生成输出文件（无命中行或无法切割）",
                                 output_dir=output_dir, stats=stats, plan=plan)

        self._log('info', f"[完成] {len(stats.written)} 个文件 -> {output_dir}")
        return ExtractResult(True, f"完成: {len(stats.written)} 个文件",
                             output_dir=output_dir, stats=stats, plan=plan)


def extract_logs(source, **kwargs) -> ExtractResult:
    """便捷入口（一次性提取；无回调）"""
    return LogExtractor().extract(source, **kwargs)
