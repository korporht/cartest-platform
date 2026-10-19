# -*- coding: utf-8 -*-
"""CarTest tools — 操作日志模型层（``23-操作日志窗口设计.md`` 一期）

职责：
  · ``LogLine`` 数据模型（时间戳 / 级别 / 标签 / 正文 / 所属操作 / 详情标记 / 耗时）
  · 旧前缀解析（``[CMD]/[INFO]/[OK]/[WARN]/[FAIL]/[ERR]/=== 段标题``）
  · 噪音行识别（默认隐藏的第三级：``List of devices attached`` / ``Using overlayfs`` …）
  · 过滤（全部/仅错误/仅操作）、搜索（忽略大小写子串）、详情开关
  · 导出（``.txt`` 所见即所得 / 自包含 ``.html``）

渲染由 ``tools/context.py`` 负责；本模块**零 UI 依赖**，可离线测试。
"""
from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass
from pathlib import Path

#: 面板行数上限（超限丢弃最旧段，与 06-P3b 口径一致）
MAX_LINES = 3000

LEVELS = ("op", "cmd", "info", "ok", "warn", "error", "raw")

LEVEL_LABEL = {"op": "OP", "cmd": "CMD", "info": "INFO", "ok": "OK",
               "warn": "WARN", "error": "ERR", "raw": "RAW"}

#: 一级：段标题 ``=== xxx ===``
_SECTION_RE = re.compile(r"^=+\s*(.+?)\s*=+$")
#: 旧前缀 → 级别
_PREFIX_RE = re.compile(r"^\[(CMD|INFO|OK|WARN|FAIL|ERR|ERROR|操作)\]\s*(.*)$")

#: 三级噪音行清单（保守——结果类行不得列入，如 ``Remount succeeded``）
NOISE_PREFIXES: tuple[str, ...] = (
    "List of devices attached",
    "adb server",
    "daemon not running",
    "daemon started successfully",
    "Using overlayfs",
    "Now remounting",
    "Sending:",
    "Sending file",
    "Transferring:",
    "Transferring file",
)

#: 级别修正提示（无前缀行；保守——只收确定性的报错特征，二期）
LEVEL_HINTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"no devices/emulators found|^adb:\s", re.IGNORECASE), "error"),
    (re.compile(r"permission denied|device offline|not found$", re.IGNORECASE), "warn"),
)

#: 自动高亮规则（设备号 / 绝对路径 / 容量，二期）
AUTO_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b[0-9a-fA-F]{8}\b"),
    re.compile(r"/(?:sdcard|la_log|data|mnt|system|dev)/[\w./-]*"),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:[KMGT]B)\b"),
)


@dataclass
class LogLine:
    seq: int
    ts: str                     # HH:mm:ss.SSS
    level: str                  # op/cmd/info/ok/warn/error/raw
    text: str
    tag: str = "工具"           # adb / 设备 / 工具 / 系统
    op_id: int | None = None
    detail: bool = False        # True=三级（受「显示详细输出」控制）
    dur: float | None = None    # op 行耗时（秒）
    ok: bool | None = None      # op 行结果
    module: str = ""            # 事件来源模块（日志终端 / 应用与传输 / Monkey …；子行继承）
    param: str = ""             # 事件参数摘要（包名 / 路径 / 次数 …）


def now_ts() -> str:
    """本地时间 ``HH:mm:ss.SSS``"""
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{ms:03d}"


def parse_legacy(text: str) -> tuple[str, str, bool]:
    """旧格式行 → ``(level, 正文, detail)``

    无前缀行：命中噪音清单 → ``raw`` + ``detail=True``；否则 ``info``（可见，
    不误藏——「查看大小 / 连接状态」等无前缀结果行必须保留）。
    """
    stripped = text.strip()
    m = _SECTION_RE.match(stripped)
    if m:
        return "op", m.group(1), False
    m = _PREFIX_RE.match(text)
    if m:
        label, body = m.group(1), m.group(2)
        level = {"CMD": "cmd", "INFO": "info", "OK": "ok", "WARN": "warn",
                 "FAIL": "error", "ERR": "error", "ERROR": "error",
                 "操作": "op"}[label]
        return level, body, False
    if any(stripped.startswith(p) for p in NOISE_PREFIXES):
        return "raw", text, True
    for pat, level in LEVEL_HINTS:
        if pat.search(stripped):
            return level, text, False
    return "info", text, False


class OplogModel:
    """日志行存储 + 导出（追加为 O(1)；过滤为 O(n) 仅交互时触发）

    2026-10-12 用户裁决：事件窗口**只作展示**——模式过滤 / 搜索 / 正则 / 详细输出
    四类开关连同工具条一并下线（配置页整组「事件窗口（操作日志）」移除）。模型侧仍保留
    「事件子行展开/收起」（窗口自身的点行交互）与「噪音行默认隐藏」两条可见性口径。
    """

    def __init__(self, reports_dir: str | Path) -> None:
        self.reports_dir = Path(reports_dir)
        self._lines: list[LogLine] = []
        self._seq = 0
        self._op_seq = 0
        self._ops: dict[int, dict] = {}
        #: 事件子行默认**展开**（2026-09-22 用户裁决）；False=历史口径「默认收起」
        self._expand_events = True
        self.expanded: set[int] = set()
        self._open_op: int | None = None      # 当前进行中的操作（新行自动归属）
        self._op_meta: dict[int, dict] = {}   # op_id → {title, tag, module, param}（不清）

    # ── 只读属性 ──
    @property
    def count(self) -> int:
        return len(self._lines)

    @property
    def last_line(self) -> "LogLine | None":
        return self._lines[-1] if self._lines else None

    # ── 追加 ──
    def _new(self, level: str, text: str, *, tag: str = "工具",
             detail: bool = False, op_id: int | None = None,
             module: str = "", param: str = "") -> LogLine:
        self._seq += 1
        if op_id is None and level != "op":
            op_id = self._open_op          # 操作进行中：新行自动归属该操作块
        if not module and op_id is not None:
            # 子行继承事件模块（事件结束后追加的行也能归到同一模块）
            module = self._op_meta.get(op_id, {}).get("module", "")
        ln = LogLine(self._seq, now_ts(), level, text, tag=tag,
                     op_id=op_id, detail=detail, module=module, param=param)
        self._lines.append(ln)
        if len(self._lines) > MAX_LINES:
            del self._lines[:200]
        return ln

    def append_legacy(self, text: str, *, module: str = "") -> LogLine:
        """兼容入口：解析旧前缀/噪音后入库（``module``＝来源模块，供事件行显示）"""
        level, body, detail = parse_legacy(text)
        return self._new(level, body, detail=detail, module=module)

    def append(self, text: str, level: str = "info", *,
               tag: str = "工具", detail: bool = False,
               module: str = "") -> LogLine:
        """结构化入口（关键服务逐步接入）"""
        return self._new(level, text, tag=tag, detail=detail, module=module)

    # ── 事件（一级：「操作」= app 内一次功能触发）──
    def begin_op(self, title: str, *, tag: str = "工具", module: str = "",
                 param: str = "") -> int:
        """开一个事件行（app 内动作触发），返回 op_id

        ``module`` 事件来源模块（日志终端 / 应用与传输 / Monkey …），
        ``param`` 参数摘要（包名 / 路径 / 次数 …）——事件优先视图的展示要素。
        """
        self._op_seq += 1
        op_id = self._op_seq
        ln = self._new("op", title, tag=tag, op_id=op_id,
                       module=module, param=param)
        self._ops[op_id] = {"line_seq": ln.seq, "t0": time.time()}
        self._op_meta[op_id] = {"title": title, "tag": tag,
                                "module": module, "param": param}
        self._open_op = op_id
        return op_id

    def end_op(self, op_id: int, ok: bool = True) -> LogLine | None:
        """写回耗时/结果；渲染层据此原地刷新该行"""
        info = self._ops.pop(op_id, None)
        if not info:
            return None
        if self._open_op == op_id:
            self._open_op = None
        ln = next((x for x in self._lines if x.seq == info["line_seq"]), None)
        if ln is None:
            return None
        ln.dur = time.time() - info["t0"]
        ln.ok = bool(ok)
        return ln

    def line_by_seq(self, seq: int) -> LogLine | None:
        return next((x for x in self._lines if x.seq == seq), None)

    def op_title(self, op_id: int) -> str:
        return str(self._op_meta.get(op_id, {}).get("title", f"操作 #{op_id}"))

    # ── 过滤（2026-10-12 起只剩"噪音行隐藏"，模式/搜索/详情开关已随工具条下线）──
    def highlight_ranges(self, text: str) -> list[tuple[int, int, str]]:
        """自动高亮区间 ``(start, end, kind)``（``kind`` 恒为 ``auto``）

        搜索下线后不再有 ``query`` 命中高亮，只剩 ``AUTO_PATTERNS``（错误/关键字自动
        标红）。签名与调用方保持不变，便于将来需要时再挂检索。
        """
        spans: list[tuple[int, int, str]] = []
        for pat in AUTO_PATTERNS:
            for m in pat.finditer(text):
                spans.append((m.start(), m.end(), "auto"))
        spans.sort(key=lambda x: (x[0], x[1]))
        return spans

    def toggle_expand(self, op_id: int) -> bool:
        """切换事件子行展开/收起；返回切换后是否**展开**

        2026-09-22 用户裁决「终端显示的日志改成默认展开」：默认从**收起**改为**展开**，
        因此 ``self.expanded`` 的语义变为「**用户切换过的 op 集合**」：
          · ``expand_events=True``（默认）→ 集合里的是"被收起"的
          · ``expand_events=False``（历史口径，可配置）→ 集合里的是"被展开"的
        """
        if op_id in self.expanded:
            self.expanded.discard(op_id)
        else:
            self.expanded.add(op_id)
        return self.is_expanded(op_id)

    def is_expanded(self, op_id: int) -> bool:
        """该事件当前是否展开（默认值 ``expand_events`` 与用户切换共同决定）"""
        if self._expand_events:
            return op_id not in self.expanded
        return op_id in self.expanded

    def child_count(self, op_id: int) -> int:
        """事件的子行数（供「＋N 行」提示）"""
        return sum(1 for ln in self._lines
                   if ln.op_id == op_id and ln.level != "op")

    def op_lines(self, op_id: int) -> list[LogLine]:
        """某操作块的全部行（含头行；供「复制该操作块」/折叠使用）"""
        return [ln for ln in self._lines if ln.op_id == op_id]

    # ── 事件优先：子行展开态 ──
    @property
    def running_op(self) -> int | None:
        """进行中的操作 id（无则 None）"""
        return self._open_op

    @property
    def expand_events(self) -> bool:
        """事件子行是否默认展开（终端视图）"""
        return self._expand_events

    def set_expand_events(self, flag: bool) -> None:
        """切换事件子行默认展开/收起（切换后不保留个别事件的用户态）"""
        flag = bool(flag)
        if flag != self._expand_events:
            self._expand_events = flag
            self.expanded.clear()

    def passes(self, ln: LogLine) -> bool:
        """单行是否通过当前视图条件（事件优先口径；增量追加与全量重渲共用）"""
        if (ln.level != "op" and ln.op_id is not None
                and not self.is_expanded(ln.op_id) and ln.level != "error"):
            # 事件子行：**默认展开**（2026-09-22）；**错误行常驻**（一眼看失败原因）；
            # 用户可点事件行单独收起（toggle_expand）
            return False
        if ln.detail:
            # 噪音/底层行（如 "List of devices attached"）**不显示**——2026-10-12 用户
            # 裁决：「详细输出」开关随工具条下线，事件窗口只作展示，故此类行永久隐藏
            return False
        return True

    def visible(self) -> list[LogLine]:
        """当前视图行（顺序渲染）"""
        return [ln for ln in self._lines if self.passes(ln)]

    def clear(self) -> None:
        """清空模型（配合视图清空；seq 连续由调用方无感）"""
        self._lines.clear()
        self._ops.clear()
        self._op_meta.clear()
        self.expanded.clear()
        self._open_op = None

    # ── 导出 ──
    def plain(self, ln: LogLine) -> str:
        """单行纯文本（事件行带 [模块] 与参数摘要）"""
        lbl = LEVEL_LABEL.get(ln.level, ln.level.upper())
        mod = f" [{ln.module}]" if ln.level == "op" and ln.module else ""
        par = f"  ({ln.param})" if ln.level == "op" and ln.param else ""
        dur = f"  (耗时 {ln.dur:.1f}s)" if ln.dur is not None else ""
        return f"{ln.ts} [{lbl}]{mod} {ln.text}{par}{dur}"

    def _export_path(self, suffix: str) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return self.reports_dir / time.strftime(f"oplog_%Y%m%d_%H%M%S.{suffix}")

    def export_txt(self, path: str | Path | None = None) -> Path:
        """导出**当前可见**行（所见即所得）为纯文本"""
        out = Path(path) if path else self._export_path("txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(self.plain(ln) for ln in self.visible()),
                       encoding="utf-8")
        return out

    def export_block(self, op_id: int, suffix: str = "txt") -> Path | None:
        """导出**某个事件块**（头行 + 全部子行，忽略筛选/收起）——取证用

        文件名带 ``op<N>``，便于与全量导出区分。
        """
        lines = self.op_lines(op_id)
        if not lines:
            return None
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        out = self.reports_dir / time.strftime(
            f"oplog_%Y%m%d_%H%M%S_op{op_id}.{suffix}")
        out.write_text("\n".join(self.plain(ln) for ln in lines), encoding="utf-8")
        return out

    def export_html(self, path: str | Path | None = None) -> Path:
        """导出**当前可见**行为自包含 HTML（级别色 + 操作/错误行底纹 + 等宽容器）"""
        colors = {"op": "#4C8DFF", "cmd": "#7FB3FF", "info": "#8FA3BF",
                  "ok": "#4CAF50", "warn": "#FFC107", "error": "#F44336",
                  "raw": "#5B6B85"}
        level_css = "\n".join(
            f".row.{k} .lv,.row.{k} .tx{{color:{v}}}"
            + (";background:#24304A" if k == "op" else "")
            + (";background:#3A1D1F" if k == "error" else "")
            for k, v in colors.items())
        rows = []
        for ln in self.visible():
            text = _html.escape(ln.text + (f"  (耗时 {ln.dur:.1f}s)"
                                           if ln.dur is not None else ""))
            mod = (f'<span class="mod">[{_html.escape(ln.module)}]</span>'
                   if ln.level == "op" and ln.module else "")
            par = (f'<span class="par">{_html.escape(ln.param)}</span>'
                   if ln.level == "op" and ln.param else "")
            rows.append(
                f'<div class="row {ln.level}">'
                f'<span class="ts">{ln.ts}</span>'
                f'<span class="lv">'
                f'{_html.escape(LEVEL_LABEL.get(ln.level, ln.level))}</span>'
                f'{mod}'
                f'<span class="tx">{text}</span>{par}</div>')
        out = Path(path) if path else self._export_path("html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>CarTest 操作日志</title><style>"
            "body{background:#1E2027;color:#C9D1E0;margin:0;padding:16px;"
            "font:12px/1.5 'JetBrains Mono','Cascadia Mono',Consolas,monospace}"
            ".log{white-space:pre-wrap}"
            ".row{padding:2px 6px;border-bottom:1px solid #242833}"
            ".ts{color:#6B7A94;margin-right:8px}"
            ".lv{margin-right:8px}"
            ".mod{color:#7E9CC4;margin-right:6px}"
            ".par{color:#6B7A94;margin-left:8px}"
            ".row.raw{padding-left:22px}"
            f"{level_css}"
            "</style><div class=\"log\">" + "\n".join(rows) + "</div>",
            encoding="utf-8")
        return out
