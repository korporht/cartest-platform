# -*- coding: utf-8 -*-
"""CarTest core — 离线提取报告解析（CbmLogextractor 产物）

**产物格式**（2026-10-08 起对接 HTML 总报告 ``all_report_<stamp>.html``）：

单页 HTML 内嵌 4 个 ``<pre>`` 文本块（对应 tab：简单报告 / 详细报告 / 链路报告 / 角色对话）::

    <pre>{ "cbm": {…}, "bds": {…} }                          简单报告 JSON（无 report_time）</pre>
    <pre>{ "report_time": …, "cbm": {…records…}, "bds": {…} } 详细报告 JSON  ← 本模块抽取此块</pre>
    <pre># 链路追踪报告 …（Markdown 原文）</pre>
    <pre># 角色对话提取报告 …（Markdown 原文）</pre>

抽取规则：块内容 ``html.unescape`` 后逐个尝试 JSON 解析——**含 ``report_time``
的顶层对象优先**（详细报告），回退含 ``cbm`` 者；找不到**显式抛错**（页面提示，
不静默降级）。`` ```json `` 块（旧 md 包装）作为兼容路径保留。

本模块把两段记录**展平**为统一行（``source`` = cbm / bds），供页面表格 / KPI /
详情抽屉使用；``load_report`` 附带的"原文"＝抽取出的详细报告 JSON
（`` ```json `` 包裹，供「原始」视角渲染数据本体）。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

__all__ = ["ReportError", "parse_report", "summarize", "iter_records", "load_report"]

#: ```json … ``` 代码块（md 包装，兼容路径；容忍 \r\n 与前后空白）
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)

#: <pre> … </pre> 块（HTML 总报告包装，2026-10-08 起主路径）
_PRE_BLOCK = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S)

#: 记录行展示所需字段（缺失即空串；详情抽屉展示全部）
RECORD_FIELDS = ("source", "query_time", "query", "domain", "sid", "logid",
                 "logid_time", "traceId", "reqMessageId", "cbm_tool_pk_type",
                 "reply", "reply_status", "reply_missing_reason", "source_file")


class ReportError(RuntimeError):
    """报告不可解析（文件不存在 / 无 JSON 块 / json 损坏）"""


def _iter_json_candidates(text: str):
    """按容器顺序产出文本内的 JSON 候选块（md `` ```json `` 块 + HTML ``<pre>`` 块）"""
    for m in _JSON_BLOCK.finditer(text):
        yield m.group(1)
    for m in _PRE_BLOCK.finditer(text):
        yield html.unescape(m.group(1)).strip()


def _extract_report_json(text: str) -> tuple[dict[str, Any], str]:
    """从报告文本中抽取详细报告 JSON → ``(data, JSON原文)``

    选择规则：含 ``report_time`` 的顶层对象优先（详细报告）；回退含 ``cbm`` 者。
    全无 → ``ReportError``（不静默）。
    """
    fallback: tuple[dict[str, Any], str] | None = None
    for block in _iter_json_candidates(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("report_time"):
            return data, block
        if fallback is None and "cbm" in data:
            fallback = (data, block)
    if fallback is not None:
        return fallback
    raise ReportError("报告内未找到 JSON 数据块（格式可能已变）")


def parse_report(path: str | Path) -> dict[str, Any]:
    """读取报告文件（HTML 总报告 / 旧 md）→ ``{report_time, cbm: {...}, bds: {...}}``"""
    p = Path(path)
    if not p.is_file():
        raise ReportError(f"报告文件不存在：{p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    data, _ = _extract_report_json(text)
    return data


def load_report(path: str | Path) -> tuple[dict[str, Any], str]:
    """解析 + 附带 JSON 原文（原文供「原始」视角与取证用；```json 包裹供渲染）"""
    p = Path(path)
    if not p.is_file():
        raise ReportError(f"报告文件不存在：{p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    data, raw_json = _extract_report_json(text)
    return data, "```json\n" + raw_json + "\n```"


def iter_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    """展平 ``cbm`` / ``bds`` 两段记录（补 ``source`` 与缺失字段），按 query_time 排序"""
    rows: list[dict[str, Any]] = []
    for source in ("cbm", "bds"):
        section = report.get(source)
        if not isinstance(section, dict):
            continue
        for rec in section.get("records") or []:
            if not isinstance(rec, dict):
                continue
            row = {k: rec.get(k, "") for k in RECORD_FIELDS}
            row["source"] = source
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("query_time") or ""))
    return rows


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """KPI：报告时间 + 各段计数 + 展平后的有/无回复计数"""
    rows = iter_records(report)
    answered = sum(1 for r in rows if str(r.get("reply") or "").strip())
    out: dict[str, Any] = {
        "report_time": str(report.get("report_time") or ""),
        "total": len(rows),
        "answered": answered,
        "unanswered": len(rows) - answered,
        "sections": {},
    }
    for source in ("cbm", "bds"):
        section = report.get(source)
        if isinstance(section, dict):
            out["sections"][source] = {
                "total": section.get("total", len(section.get("records") or [])),
                "matched": section.get("matched", ""),
                "unmatched_query": section.get("unmatched_query", ""),
                "unmatched_logid": section.get("unmatched_logid", ""),
            }
    return out
