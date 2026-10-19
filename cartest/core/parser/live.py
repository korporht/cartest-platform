"""CarTest core — 实时日志解析

参考 CbmLogextractor 逻辑，从 logcat 行中提取结构化字段
（query / logid / reply / intent），并做时间邻近匹配。

来源：``CarVoiceTestTool/log_parser.py`` 平移（2026-09-14）。

相对原实现的调整：
  1. **修正 ``parse_logs`` 的返回类型标注**——原标注为 ``-> list``，
     实际返回 ``(matched, intents)`` 元组（接口设计文档中已记录该不符）。
  2. 裸 ``except:`` 改为 ``except Exception:``——原写法会吞掉
     ``KeyboardInterrupt``／``SystemExit``，属明确缺陷。正常路径行为不变。
  3. ``Optional[X]`` 保留（与 core 其余模块风格一致即可，不影响行为）。
"""
from __future__ import annotations

import fnmatch
import gzip
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class QueryRecord:
    """Query记录"""
    timestamp: datetime
    query: str
    sid: str = ""
    domain: str = ""
    source: str = ""  # asr/cbm/bds


@dataclass
class LogidRecord:
    """Logid记录"""
    timestamp: datetime
    logid: str
    source: str = ""  # cbm/bds


@dataclass
class ReplyRecord:
    """回复记录"""
    timestamp: datetime
    reply: str
    sid: str = ""
    source: str = ""  # tts/car_control


@dataclass
class MatchedResult:
    """匹配结果"""
    query: str
    query_time: datetime
    sid: str = ""
    domain: str = ""
    logid: str = ""
    logid_time: Optional[datetime] = None
    reply: str = ""
    reply_time: Optional[datetime] = None
    source: str = ""


#: 默认日志文件匹配（= 改造前 main_ 前缀 + .log/.gz 后缀，逐字等价）
DEFAULT_FILE_GLOBS: tuple[str, ...] = ("main_*.log", "main_*.gz")

#: 流式分片来源标记（extract_tts_reply 产出；由 _ReplyStreamAggregator 聚合）
FRAGMENT_SOURCES: tuple[str, ...] = ("tts_frag", "nlp_frag")


class _ReplyStreamAggregator:
    """流式回复分片聚合器（2026-10-09，T28/T1MA 平台适配）

    T28/T1MA 的回复为**双通道**流式分片（每片一行日志）：
      · ``20204 nlp`` 正文（带 sid）——完整显示文本
      · ``ASS-TTSEngine appendText`` 播报（无 sid）——与正文同文本的播报切分
    "每片一条记录"会让回复匹配只命中一片；两通道混拼又会重叠错乱。因此：

      · nlp 流**按 sid 分槽**直连聚合（多会话交错天然隔离）——**优先输出**
      · appendText 流按 ``idle_gap``（1.5s，与 judger 实时收敛口径一致）静默分段，
        仅在 **nlp 无片**时兜底输出（避免双通道重复）
      · 拼接用重叠去重（整片重复 / 重叠增量，与 judger._overlap_append 同款）
    """

    def __init__(self, idle_gap: float = 1.5) -> None:
        self._gap = idle_gap
        self._nlp: dict[str, list] = {}          # sid(or "") → [text, first_ts]
        self._tts_done: list = []                # appendText 已完成段
        self._tts_cur: Optional[ReplyRecord] = None
        self._tts_last_ts: Optional[datetime] = None

    @staticmethod
    def _append(buf: str, seg: str) -> str:
        seg = seg.strip()
        if not seg:
            return buf
        if not buf:
            return seg
        mlen = min(len(buf), len(seg))
        for k in range(mlen, 0, -1):
            if buf.endswith(seg[:k]):
                return buf + seg[k:]
        return buf + seg

    def feed(self, rec: ReplyRecord, out: list) -> None:
        if rec.source == "nlp_frag":
            seg = rec.reply
            if not seg.strip():
                # 空片无贡献（真机实测：状态型 20204 无正文）——不建槽，
                # 避免产出"空流"记录浪费回复配对轮次（2026-10-10）
                return
            key = rec.sid or ""
            slot = self._nlp.get(key)
            if slot is None:
                self._nlp[key] = [seg.strip(), rec.timestamp]
            else:
                slot[0] = self._append(slot[0], seg)
            return
        # appendText 播报流：静默分段（无 sid，仅时间界）
        seg = rec.reply.strip()
        cur = self._tts_cur
        if cur is None:
            self._tts_cur = ReplyRecord(timestamp=rec.timestamp, reply=seg, source="tts")
        elif self._tts_last_ts is not None and \
                (rec.timestamp - self._tts_last_ts).total_seconds() > self._gap:
            self._tts_done.append(cur)
            self._tts_cur = ReplyRecord(timestamp=rec.timestamp, reply=seg, source="tts")
        else:
            cur.reply = self._append(cur.reply, rec.reply)
        self._tts_last_ts = rec.timestamp

    def finish(self, out: list) -> None:
        if self._tts_cur is not None:
            self._tts_done.append(self._tts_cur)
            self._tts_cur = None
        if self._nlp:
            # nlp 正文优先（带 sid、完整；appendText 与其为双通道冗余，不重复输出）
            for sid, (text, first_ts) in self._nlp.items():
                out.append(ReplyRecord(timestamp=first_ts, reply=text,
                                       sid=sid, source="tts"))
        else:
            out.extend(self._tts_done)


class LogParser:
    """日志解析器"""

    def __init__(self, year: int | None = None, fragments: bool = False):
        self.year = year or datetime.now().year
        # 流式分片提取开关（2026-10-09）：默认关＝E0V 行为零漂移；
        # T28/T1MA 由项目档案 log.reply_fragments=true 开启（模式7/8 + 聚合器）
        self.fragments = bool(fragments)
        # CBM匹配窗口(秒)
        self.cbm_match_window = 10
        # 回复匹配窗口(秒)
        self.reply_match_window = 30

    def parse_timestamp(self, line: str) -> Optional[datetime]:
        """解析日志时间戳 MM-DD HH:MM:SS.mmm"""
        m = re.match(r'(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)', line)
        if m:
            month, day, hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            ms = int(m.group(6)[:3].ljust(3, '0'))
            try:
                return datetime(self.year, month, day, hour, minute, second, ms * 1000)
            except Exception:
                return None
        return None

    def extract_asr_result(self, line: str, ts: datetime) -> Optional[QueryRecord]:
        """提取ASR识别结果"""
        # normal_text
        m = re.search(r'"normal_text"\s*:\s*"([^"]+)"', line)
        if m:
            return QueryRecord(timestamp=ts, query=m.group(1), source="asr")

        # ASS-SrResultBack
        m = re.search(r'srResultBack:\s*(.+?)--true', line)
        if m:
            return QueryRecord(timestamp=ts, query=m.group(1).strip(), source="asr")

        return None

    def extract_cbm_query(self, line: str, ts: datetime) -> Optional[QueryRecord]:
        """提取CBM query"""
        if 'ASS-CbmToolPkStrategy' not in line or '云端落域' not in line:
            return None

        query_m = re.search(r'query\s*=\s*\[([^\]]*)\]', line)
        sid_m = re.search(r'sid\s*=\s*\[([^\]]*)\]', line)
        domain_m = re.search(r'domain\s*=\s*\[([^\]]*)\]', line)

        if query_m:
            return QueryRecord(
                timestamp=ts,
                query=query_m.group(1),
                sid=sid_m.group(1) if sid_m else "",
                domain=domain_m.group(1) if domain_m else "",
                source="cbm"
            )
        return None

    def extract_logid(self, line: str, ts: datetime) -> Optional[LogidRecord]:
        """提取logid"""
        # CBM logid (新格式)
        m = re.search(r'"cbm_tool_logid"\s*:\s*\{[^}]*"text"\s*:\s*"(\d{14}[0-9A-Fa-f]+)"', line)
        if m:
            return LogidRecord(timestamp=ts, logid=m.group(1), source="cbm")

        # BDS logid
        m = re.search(r'X-Tt-Logid:\s*(\S+)', line)
        if m:
            return LogidRecord(timestamp=ts, logid=m.group(1), source="bds")

        return None

    def extract_tts_reply(self, line: str, ts: datetime) -> Optional[ReplyRecord]:
        """提取TTS回复 - 多种模式"""
        # 模式1: [TtsSpeaker]startFlowSpeak
        if '[TtsSpeaker]startFlowSpeak' in line:
            sid_m = re.search(r'sid=(\S+?)(?:,|\s)', line)
            text_m = re.search(r'text=(.+?),\s*traceId=', line)
            if not text_m:
                text_m = re.search(r'text=(.+?)$', line)
            if text_m:
                return ReplyRecord(
                    timestamp=ts,
                    reply=text_m.group(1).strip(),
                    sid=sid_m.group(1) if sid_m else "",
                    source="tts"
                )

        # 模式2: TtsServiceAgent
        if 'TtsServiceAgent' in line:
            m = re.search(r'text[=:]\s*"([^"]+)"', line)
            if m:
                return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="tts")

        # 模式3: tts_text字段
        m = re.search(r'"tts_text"\s*:\s*"([^"]+)"', line)
        if m and len(m.group(1)) > 5:  # 过滤太短的
            return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="tts")

        # 模式4: reply字段
        m = re.search(r'"reply"\s*:\s*"([^"]+)"', line)
        if m and len(m.group(1)) > 5:
            return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="tts")

        # 模式5: response字段
        m = re.search(r'"response"\s*:\s*"([^"]+)"', line)
        if m and len(m.group(1)) > 5:
            return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="tts")

        # 模式6: content字段（BDS回复）
        if 'event=550' in line:
            m = re.search(r'"content"\s*:\s*"([^"]*)"', line)
            if m:
                return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="bds")

        # 模式7（2026-10-09，T28/T1MA，fragments 开关控制）: ASS-TTSEngine 流式播报分片
        #   `ASS-TTSEngine: appendText() called with: text = [你好呀，], textState = [0]`
        #   textState: 0=首片 1=续片 2=流结束；产出 source="tts_frag" 交聚合器
        if self.fragments and 'ASS-TTSEngine: appendText()' in line:
            m = re.search(r'text = \[(.*?)\], textState = \[(\d)\]', line)
            if m:
                return ReplyRecord(timestamp=ts, reply=m.group(1),
                                   sid="", source="tts_frag")
            return None

        # 模式8（2026-10-09，T28/T1MA，fragments 开关控制）: 20204 nlp 流式正文分片（含 sid）
        #   `sparkSrResult srCode: [20204] ... "nlp":{"text":"片段"...},"header":{..."sid":"wgw..."}`
        #   锚定 `"nlp":{` 避免匹配 cbm_meta 转义串（\"nlp\" 前缀带反斜杠，不受影响）
        if self.fragments and 'sparkSrResult srCode: [20204]' in line:
            m = re.search(r'"nlp":\{"text":"((?:[^"\\]|\\.)*?)"', line)
            if m is None:
                # 截断容错（2026-10-10 T1MA 真机）：超长文本会撞 logcat 单行长度上限
                # 被物理截断（行尾戛然而止、无闭合引号）——降级提取行尾前的合法前缀，
                # 避免整条长文本（如 cbm_tool_news 新闻列表）丢失
                m = re.search(r'"nlp":\{"text":"((?:[^"\\]|\\.)*)', line)
            if m:
                seg = m.group(1).replace('\\n', '\n').replace('\\"', '"')
                sid_m = re.search(r'"sid":"(wgw[^"]+)"', line)
                return ReplyRecord(timestamp=ts, reply=seg,
                                   sid=sid_m.group(1) if sid_m else "",
                                   source="nlp_frag")
            return None

        return None

    def extract_car_control_reply(self, line: str, ts: datetime) -> Optional[ReplyRecord]:
        """提取车控回复"""
        if 'CCA-TtsIdQueryHelper' not in line:
            return None

        # 跳过模板行
        if 'CCA-1005' in line or 'ttsResults' in line:
            return None

        m = re.search(r'CCA-TtsIdQueryHelper:\s*(.+)', line)
        if m:
            return ReplyRecord(timestamp=ts, reply=m.group(1).strip(), source="car_control")
        return None

    def extract_intent_type(self, line: str, ts: datetime) -> Optional[str]:
        """提取意图类型"""
        m = re.search(r'"intent_type"\s*:\s*"([^"]+)"', line)
        if m:
            return m.group(1)
        return None

    def parse_log_file(self, file_path: str) -> dict:
        """解析单个日志文件"""
        queries: list[QueryRecord] = []
        logids: list[LogidRecord] = []
        replies: list[ReplyRecord] = []
        intents: list[dict] = []

        path = Path(file_path)

        # 打开文件
        if path.suffix == '.gz':
            try:
                f = gzip.open(path, 'rt', encoding='utf-8', errors='ignore')
            except Exception:
                return {'queries': [], 'logids': [], 'replies': [], 'intents': []}
        else:
            try:
                f = open(path, 'r', encoding='utf-8', errors='ignore')
            except Exception:
                return {'queries': [], 'logids': [], 'replies': [], 'intents': []}

        with f:
            frag_agg = _ReplyStreamAggregator() if self.fragments else None
            for line in f:
                line = line.rstrip()
                ts = self.parse_timestamp(line)
                if not ts:
                    continue

                # 提取ASR结果
                asr = self.extract_asr_result(line, ts)
                if asr:
                    queries.append(asr)

                # 提取CBM query
                cbm = self.extract_cbm_query(line, ts)
                if cbm:
                    queries.append(cbm)

                # 提取logid
                logid = self.extract_logid(line, ts)
                if logid:
                    logids.append(logid)

                # 提取TTS回复（流式分片经聚合器；其余直接入列）
                tts = self.extract_tts_reply(line, ts)
                if tts:
                    if frag_agg is not None and tts.source in FRAGMENT_SOURCES:
                        frag_agg.feed(tts, replies)
                    else:
                        replies.append(tts)

                # 提取车控回复
                car = self.extract_car_control_reply(line, ts)
                if car:
                    replies.append(car)

                # 提取意图
                intent = self.extract_intent_type(line, ts)
                if intent:
                    intents.append({'timestamp': ts, 'intent': intent})

            if frag_agg is not None:
                frag_agg.finish(replies)

        return {
            'queries': queries,
            'logids': logids,
            'replies': replies,
            'intents': intents
        }

    def match_by_proximity(self, queries: list, logids: list) -> list:
        """时间邻近匹配 query↔logid"""
        results = []
        used_queries = set()

        # 按时间排序
        sorted_logids = sorted(logids, key=lambda x: x.timestamp)
        sorted_queries = sorted(queries, key=lambda x: x.timestamp)

        # 外层 for 遍历每个 logid 恰好一次，无需 used_logids 去重；
        # 旧的 sorted_logids.index(logid) 按值查找为 O(n²) 且对相同记录会误定位。
        for logid in sorted_logids:
            best_match = None
            best_delta = float('inf')

            for i, query in enumerate(sorted_queries):
                if i in used_queries:
                    continue

                delta = (logid.timestamp - query.timestamp).total_seconds()

                # logid应该在query之后
                if 0 <= delta <= self.cbm_match_window:
                    if delta < best_delta:
                        best_match = i
                        best_delta = delta

            if best_match is not None:
                used_queries.add(best_match)
                query = sorted_queries[best_match]
                results.append(MatchedResult(
                    query=query.query,
                    query_time=query.timestamp,
                    sid=query.sid,
                    domain=query.domain,
                    logid=logid.logid,
                    logid_time=logid.timestamp,
                    source=query.source
                ))

        # 添加未匹配的query
        for i, query in enumerate(sorted_queries):
            if i not in used_queries:
                results.append(MatchedResult(
                    query=query.query,
                    query_time=query.timestamp,
                    sid=query.sid,
                    domain=query.domain,
                    source=query.source
                ))

        return results

    def match_replies(self, results: list, replies: list) -> list:
        """匹配回复内容

        两轮匹配（2026-10-10 增强）：
          **第一轮 sid 精确配对**——sid 是会话唯一标识，同 sid 的 query↔回复
          即确定关联（不受窗口/顺序影响）。真机实测（T28 连续注入排队）发现：
          同 query 的多条记录（ASR 记录无 sid、且时间更早）会以"时间最近"抢走
          窗口内属于其他会话的回复，造成回复错位——先全局 sid 配对可根治。
          **第二轮 时间邻近**（未配对者）：维持原 30s 窗口 + 取最近逻辑不变。
        """
        used_replies = set()

        # ── 第一轮：sid 精确配对（全局，与记录顺序无关）──
        reply_by_sid: dict[str, list[int]] = {}
        for i, rp in enumerate(replies):
            if rp.source == "tts" and rp.sid:
                reply_by_sid.setdefault(rp.sid, []).append(i)
        for result in results:
            if not result.sid or not result.query_time:
                continue
            cands = [i for i in reply_by_sid.get(result.sid, []) if i not in used_replies]
            if not cands:
                continue
            # 同 sid 多候选：取时间在 query 之后且最近者；均在其前则取最早者（时钟异常兜底）
            best, best_delta = None, float('inf')
            for i in cands:
                delta = (replies[i].timestamp - result.query_time).total_seconds()
                if delta >= 0 and delta < best_delta:
                    best, best_delta = i, delta
            if best is None:
                best = min(cands, key=lambda i: replies[i].timestamp)
            used_replies.add(best)
            result.reply = replies[best].reply
            result.reply_time = replies[best].timestamp

        # ── 第二轮：时间邻近（未配对者；原逻辑不变）──
        for result in results:
            if not result.query_time or result.reply:
                continue

            best_reply = None
            best_delta = float('inf')

            for i, reply in enumerate(replies):
                if i in used_replies:
                    continue

                delta = (reply.timestamp - result.query_time).total_seconds()

                # 回复应该在query之后
                if 0 <= delta <= self.reply_match_window:
                    # 优先sid精确匹配
                    if reply.source == "tts" and reply.sid and result.sid and reply.sid == result.sid:
                        best_reply = i
                        best_delta = delta
                        break

                    # 时间邻近匹配
                    if delta < best_delta:
                        best_reply = i
                        best_delta = delta

            if best_reply is not None:
                used_replies.add(best_reply)
                reply = replies[best_reply]
                result.reply = reply.reply
                result.reply_time = reply.timestamp

        return results


def iter_log_files(log_dir: str, file_globs: list[str] | None = None) -> list[Path]:
    """列出目录下匹配 ``file_globs`` 的日志文件（递归、稳定排序）

    ``file_globs=None`` → 默认 :data:`DEFAULT_FILE_GLOBS`（``main_*.log`` / ``main_*.gz``，
    与改造前"main_ 前缀 + .log/.gz 后缀"逐字等价）。
    T28/T1MA 由项目档案 ``log.file_glob`` 提供（``["logcat.txt", "*.txt.gz"]``）。
    大小写敏感（``fnmatchcase``）——与旧后缀判断行为一致。
    """
    globs = list(file_globs) if file_globs else list(DEFAULT_FILE_GLOBS)
    out: list[Path] = []
    for fp in sorted(Path(log_dir).rglob('*')):
        if fp.is_file() and any(fnmatch.fnmatchcase(fp.name, g) for g in globs):
            out.append(fp)
    return out


def parse_logs(log_dir: str, file_globs: list[str] | None = None,
               fragments: bool = False) -> tuple[list, list]:
    """解析目录下的日志，返回 ``(matched, intents)`` 元组。

    Args:
        log_dir:    日志目录（递归扫描）
        file_globs: 日志文件名匹配（默认 ``main_*.log`` / ``main_*.gz``，＝旧行为；
                    其他平台由项目档案 ``log.file_glob`` 提供）
        fragments:  是否启用流式分片提取（模式7/8 + 聚合器；T28/T1MA 由档案
                    ``log.reply_fragments`` 开启；默认关＝E0V 行为零漂移）
    """
    parser = LogParser(fragments=fragments)

    all_queries: list = []
    all_logids: list = []
    all_replies: list = []
    all_intents: list = []

    # 遍历日志文件（file_globs=None 时为 main_*.log / main_*.gz，与旧行为逐字等价）
    for file_path in iter_log_files(log_dir, file_globs):
        result = parser.parse_log_file(str(file_path))
        all_queries.extend(result['queries'])
        all_logids.extend(result['logids'])
        all_replies.extend(result['replies'])
        all_intents.extend(result['intents'])

    # 匹配query↔logid
    matched = parser.match_by_proximity(all_queries, all_logids)

    # 匹配回复
    matched = parser.match_replies(matched, all_replies)

    return matched, all_intents
