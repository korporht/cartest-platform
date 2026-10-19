"""CarTest core — 结果判定

基于日志分析判定测试用例通过/失败。

来源：``CarVoiceTestTool/result_judger.py`` 平移（2026-09-14）。

**这是整套工具最核心的资产**——判定优先级链 + 落域兜底 + 生图兜底，
每一条都对应一次真实误判的修复。改动前务必先跑 ``tests/test_judger.py``。

相对原实现的唯一变更：
  设备时钟由**构造注入**（``clock``），默认实现走 ``AdbSession.device_timestamp``。
  原实现把 ``adb shell date`` 内联在判定器里，导致判定逻辑无法离线单测；
  注入后传入一个返回固定字符串的函数即可完全离线验证判定链。
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .config import DEFAULT_RESULT_TIMEOUT
from . import project as _project

# ── 判定规则与文案的**代码兜底**（项目档案可覆盖 ``judge.*``；见 30 号 §5.4）──
#: 拒识特征行（CBM 路由 sparkDenial / cbm_denial；注入链路被拦截，未发送云端）
DEFAULT_DENY_PATTERN = (
    r'sparkDenial|大模型拒识|'
    r'pk_type["\']?\s*[:=]\s*["\']?cbm_denial|domain\s*=\s*\[cbm_denial\]'
)
#: 拒识原因前缀（实际输出后接 ``", query=…"``）
DEFAULT_DENIAL_REASON = "车机CBM路由拒识(sparkDenial)"
#: ASR 成功但无回复的判定文案
DEFAULT_MSG_NO_REPLY = "ASR识别成功但无回复文本（拒识/多轮追问未返回/长回复超时）"


def deny_pattern() -> str:
    """拒识特征正则（项目档案 ``judge.deny_pattern`` → 代码兜底）"""
    return str(_project.get("judge.deny_pattern", DEFAULT_DENY_PATTERN))


def denial_reason() -> str:
    """拒识原因前缀（项目档案 ``judge.denial_reason`` → 代码兜底）"""
    return str(_project.get("judge.denial_reason", DEFAULT_DENIAL_REASON))


def msg_no_reply() -> str:
    """无回复判定文案（项目档案 ``judge.messages.no_reply`` → 代码兜底）"""
    return str(_project.get("judge.messages.no_reply", DEFAULT_MSG_NO_REPLY))


def default_timeout() -> float:
    """判定超时**默认值**（项目档案 ``judge.timeout`` → ``DEFAULT_RESULT_TIMEOUT``）

    2026-10-09 平台适配：T28/T1MA 连续注入排队延迟实测 20~28s+，档案设 30；
    CLI/GUI 未显式传超时时统一从这里取（单一来源，勿在各消费点内联兜底）。
    """
    try:
        return float(_project.get("judge.timeout", DEFAULT_RESULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_RESULT_TIMEOUT

if TYPE_CHECKING:
    from .source import TestCase


@dataclass
class Judgment:
    """判定结果"""
    passed: bool = False
    asr_text: str = ""
    intent_type: str = ""
    plugin_name: str = ""
    response_time: float = 0.0            # 兼容旧代码
    asr_response_time: float = 0.0        # ASR 识别时间
    sys_response_time: float = 0.0        # 系统响应时间
    intent_response_time: float = 0.0     # 意图识别时间
    tts_response_time: float = 0.0        # TTS 播放时间
    e2e_response_time: float = 0.0        # 端到端响应时间
    log_snippet: str = ""
    reason: str = ""
    reply_text: str = ""                  # 回复文本（20204 nlp 分片 / tts_text 累积）


class Judger:
    """结果判定器

    构造参数:
        clock: 返回设备时间戳字符串 ``MM-DD HH:MM:SS.mmm`` 的函数。
               默认走 ``AdbSession.device_timestamp``（带 2s TTL 缓存）。
               测试时传入任意返回固定串的函数即可离线运行。
        timeout: 判定超时（秒）。
    """

    #: 设备时间戳缓存有效期（秒），见 _get_device_timestamp
    DEV_TS_TTL = 2.0

    def __init__(self, clock: Callable[[], str] | None = None,
                 timeout: float = DEFAULT_RESULT_TIMEOUT) -> None:
        self._clock = clock
        self._timeout = timeout
        # 规则/文案取一次（项目档案可覆盖；``re`` 预编译避免逐行重编译）
        self._deny_re = re.compile(deny_pattern())
        self._denial_reason_base = denial_reason()
        self._msg_no_reply = msg_no_reply()
        self._collected_logs: list[str] = []
        self._start_time: float = 0.0
        self._send_time: float = 0.0  # 发送 voice 命令的时间
        self._dev_ts_cache: tuple[float, str] = (0.0, "")  # (获取时刻, 设备时间戳)
        self._matched: bool = False
        self._asr_text: str = ""
        self._intent_type: str = ""
        self._plugin_name: str = ""
        self._tts_text: str = ""
        self._scene: str = ""
        self._error_found: bool = False
        self._error_msg: str = ""
        self._asr_time: str = ""              # ASR 识别时间戳
        self._intent_time: str = ""           # 意图识别时间戳
        self._tts_time: str = ""              # TTS 播放时间戳
        self._sys_response_time: str = ""     # 系统响应时间戳
        self._sys_response_detected: bool = False
        self._send_timestamp: str = ""        # 发送时的设备时间戳，用于过滤旧日志
        self._final_image_time: str = ""      # 端侧生图完成图（imageUrl）时间戳
        self._placeholder_time: str = ""      # 端侧生图占位图（expendImageUrl）时间戳
        self._is_aigc: bool = False           # 是否生图链路（streamTextToPicture/文生图）
        self._denied: bool = False            # 是否被 CBM 路由拒识（sparkDenial/cbm_denial）
        self._denial_reason: str = ""         # 拒识原因描述
        self._reply_text: str = ""            # 回复文本（20204 nlp 分片 / tts_text 累积）
        self._reply_started: bool = False     # 是否已收到回复内容帧
        self._reply_time: str = ""            # 回复首帧时间戳
        self._reply_last_wall: float = 0.0    # 最近一次回复帧的墙钟时间
        self._last_reply_seg: str = ""        # 最近一帧干净回复文本（展示用）

    def set_timeout(self, timeout: float) -> None:
        self._timeout = timeout

    def reset(self) -> None:
        self._collected_logs.clear()
        self._start_time = 0.0
        self._send_time = 0.0
        self._matched = False
        self._asr_text = ""
        self._intent_type = ""
        self._plugin_name = ""
        self._tts_text = ""
        self._scene = ""
        self._error_found = False
        self._error_msg = ""
        self._asr_time = ""
        self._intent_time = ""
        self._tts_time = ""
        self._sys_response_time = ""
        self._sys_response_detected = False
        self._send_timestamp = ""
        self._final_image_time = ""
        self._placeholder_time = ""
        self._is_aigc = False
        self._denied = False
        self._denial_reason = ""
        self._reply_text = ""
        self._reply_started = False
        self._reply_time = ""
        self._reply_last_wall = 0.0
        self._last_reply_seg = ""

    def start_collection(self, send_time: float = 0.0) -> None:
        """开始收集日志"""
        self.reset()
        self._start_time = time.time()
        self._send_time = send_time if send_time > 0 else self._start_time
        # 获取当前设备时间戳，用于过滤旧日志
        self._send_timestamp = self._get_device_timestamp()

    # ── 设备时钟 ──────────────────────────────
    def _get_device_timestamp(self) -> str:
        """获取当前设备时间戳（带短 TTL 缓存）。

        每条用例开始时查询一次 ``adb shell date``（开销约百毫秒级）；
        间隔模式（≥3s）下 TTL 内必然过期、每次刷新，顺序模式（连续灌入）下复用，
        避免不必要的 adb 往返。
        """
        now = time.time()
        if self._dev_ts_cache[0] and (now - self._dev_ts_cache[0]) < self.DEV_TS_TTL:
            return self._dev_ts_cache[1]
        ts = self._query_device_timestamp()
        self._dev_ts_cache = (now, ts)
        return ts

    def _query_device_timestamp(self) -> str:
        """执行一次设备时钟查询（无缓存）。默认走 AdbSession。"""
        if self._clock is not None:
            return self._clock()
        from .adb import session
        return session().device_timestamp()

    # ── 时间戳比较（设备时钟，格式 MM-DD HH:MM:SS.mmm）──
    def _is_timestamp_before(self, ts1: str, ts2: str) -> bool:
        """判断 ts1 是否在 ts2 之前"""
        try:
            parts1 = ts1.split()
            parts2 = ts2.split()
            if len(parts1) != 2 or len(parts2) != 2:
                return False

            if parts1[0] != parts2[0]:
                return parts1[0] < parts2[0]

            return parts1[1] < parts2[1]
        except Exception:
            return False

    def _is_timestamp_after(self, ts1: str, ts2: str) -> bool:
        """判断 ts1 是否在 ts2 之后"""
        try:
            parts1 = ts1.split()
            parts2 = ts2.split()
            if len(parts1) != 2 or len(parts2) != 2:
                return False

            if parts1[0] != parts2[0]:
                return parts1[0] > parts2[0]

            return parts1[1] > parts2[1]
        except Exception:
            return False

    # ── 日志消费 ──────────────────────────────
    def feed_log(self, line: str) -> None:
        """喂入一条日志进行分析"""
        self._collected_logs.append(line)
        self._parse_line(line)

    def _parse_dev_ts(self, ts: str):
        """解析设备时间戳 MM-DD HH:MM:SS.mmm 为 datetime（设备时钟）"""
        try:
            return datetime.datetime.strptime(
                f"{datetime.datetime.now().year}-{ts}", "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            return None

    def _calc_elapsed(self, timestamp: str) -> float:
        """计算从发送时刻到指定时间戳的耗时。

        时钟源统一：日志时间戳与 _send_timestamp 均为设备时钟，
        直接相减，避免主机时钟与设备时钟偏移导致的误差。
        无该阶段事件时返回已等待时长（用于超时判定）。
        """
        if not timestamp:
            return time.time() - self._start_time if self._start_time > 0 else 0.0
        # 设备时钟差值（同一时钟源，最可靠）
        if self._send_timestamp:
            d = self._parse_dev_ts(timestamp)
            s = self._parse_dev_ts(self._send_timestamp)
            if d is not None and s is not None:
                delta = (d - s).total_seconds()
                if delta >= 0:
                    return delta
        # 回退：主机时间差（设备/主机时钟同步时近似）
        return time.time() - self._start_time if self._start_time > 0 else 0.0

    def _parse_line(self, line: str) -> None:
        """解析日志行，提取关键信息"""
        # 提取时间戳 (格式: MM-DD HH:MM:SS.mmm)
        ts_match = re.match(r'(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)', line)
        timestamp = ts_match.group(1) if ts_match else ""

        # 时间戳过滤：只处理发送时间之后的日志
        if timestamp and self._send_timestamp:
            if self._is_timestamp_before(timestamp, self._send_timestamp):
                return  # 跳过旧日志

        # 生图链路标记：仅接受发送窗口后的 streamTextToPicture 特征行。
        # 「文生图」中文串过于宽泛，历史/串扰日志会把普通用例拖进生图等待。
        if not self._is_aigc and 'streamTextToPicture' in line:
            self._is_aigc = True

        # 拒识检测：CBM 路由 sparkDenial / cbm_denial（注入链路被拦截，未发送云端）
        #   特征行：sparkDenial 大模型拒识 / handle sparkDenial / pk_type=cbm_denial
        #   / cbm_tool_pk.domain = [cbm_denial]。注意排除系统预置参数行
        #   `cbm_json ... {"cbm_denial": {"skip":"skip"}}`（不含 sparkDenial，不受影响）。
        if not self._denied and self._deny_re.search(line):
            self._denied = True
            qm = re.search(r'query\s*=\s*\[([^\]]*)\]', line)
            self._denial_reason = self._denial_reason_base
            if qm:
                self._denial_reason += f", query={qm.group(1)[:40]}"

        # ASR 识别结果 - 只在发送时间之后采集
        m = re.search(r'"normal_text"\s*:\s*"([^"]+)"', line)
        if m:
            new_asr = m.group(1)
            # 只更新ASR文本，如果：
            # 1. 当前没有ASR文本，或者
            # 2. 新的ASR时间戳比当前的更新
            if not self._asr_text or (timestamp and self._is_timestamp_after(timestamp, self._asr_time)):
                self._asr_text = new_asr
                if timestamp:
                    self._asr_time = timestamp

        # 意图类型（落域） - 多种模式匹配，优先级从高到低
        # 模式1: 云端落域 domain（参考 CbmLogextractor）——权威来源，出现即覆盖
        #   行特征: 同时含 ASS-CbmToolPkStrategy 与 云端落域；格式 domain = [文生图助手]
        #   注意: 同一会话中 ASS-CbmToolPkStrategy 的 handle basicSparkMsg 行会先出现
        #   "intent":"QUERY" 等 PK 策略意图，若用"仅当为空才提取"会被其抢占，
        #   故此处不判空、直接以落域 domain 覆盖。
        if 'ASS-CbmToolPkStrategy' in line and '云端落域' in line:
            m = re.search(r'domain\s*=\s*\[([^\]]*)\]', line)
            if m:
                self._intent_type = m.group(1).strip()
                if timestamp:
                    self._intent_time = timestamp

        # 模式2: intent_type字段
        if not self._intent_type:
            m = re.search(r'"intent_type"\s*:\s*"([^"]+)"', line)
            if m:
                self._intent_type = m.group(1)
                if timestamp:
                    self._intent_time = timestamp

        # 模式3: intent字段（排除 ASS-CbmToolPkStrategy——其 intent 为 PK 策略占位
        #   QUERY/other 等，非落域 domain，避免污染落域栏）
        if not self._intent_type and 'ASS-CbmToolPkStrategy' not in line:
            m = re.search(r'"intent"\s*:\s*"([^"]+)"', line)
            if m:
                self._intent_type = m.group(1)

        # 模式4: domain字段
        if not self._intent_type:
            m = re.search(r'"domain"\s*:\s*"([^"]+)"', line)
            if m:
                self._intent_type = m.group(1)

        # 模式5: 落域信息
        if not self._intent_type:
            m = re.search(r'落域[=:]\s*(\w+)', line)
            if m:
                self._intent_type = m.group(1)

        # 模式6: 场景类型
        if not self._intent_type:
            m = re.search(r'scene_type[=:]\s*(\w+)', line)
            if m:
                self._intent_type = m.group(1)

        # 模式7: 插件类型作为意图
        if not self._intent_type:
            m = re.search(r'plugin[=:]\s*(\w+)', line)
            if m:
                plugin = m.group(1)
                if plugin in ['news', 'music', 'navigation', 'weather', 'phone']:
                    self._intent_type = plugin

        # 插件名称
        m = re.search(r'"plugin_name"\s*:\s*"([^"]+)"', line)
        if m:
            self._plugin_name = m.group(1)

        # TTS 文本（结果字段；部分车控/短确认通道以此下发回复）
        m = re.search(r'"tts_text"\s*:\s*"([^"]+)"', line)
        if m and m.group(1).strip():
            if not self._reply_started:
                self._reply_started = True
                if timestamp:
                    self._reply_time = timestamp
            if self._overlap_append(m.group(1)):
                self._reply_last_wall = time.time()
            self._last_reply_seg = m.group(1)
            self._tts_text = m.group(1)
            if timestamp:
                self._tts_time = timestamp

        # 车控 TTS 回复：CCA-TtsHelper / TtsServiceAgent startSpeak
        # 此前只认 tts_text/20204，车控「没问题，已帮你打开前排空调」会漏检导致误 FAIL
        if not self._tts_text:
            m = re.search(r'CCA-TtsHelper:.*?startSpeak\(text\s*=\s*<([^>]+)>', line)
            if not m:
                # text = [没问题...] 或 text = [[g0]没问题...[p360]]
                m = re.search(r'TtsServiceAgent[^\n]*startSpeak\(\)[^\n]*text\s*=\s*\[(.*)\]', line)
            if m:
                seg = re.sub(r'\[(?:g|p)\d+\]', '', m.group(1)).strip()
                if seg:
                    if not self._reply_started:
                        self._reply_started = True
                        if timestamp:
                            self._reply_time = timestamp
                    self._last_reply_seg = seg
                    self._tts_text = seg
                    self._reply_last_wall = time.time()
                    if timestamp:
                        self._tts_time = timestamp

        # 回复内容帧：讯飞结果帧 sparkSrResult srCode 20204 内 "nlp":{"text":"..."}
        # （流式分片下发，首片非空 = 回复开始；累积各片供结果展示）。
        # 锚定 "nlp":{ 避免匹配 cbm_meta 转义串（\"nlp\" 前缀带反斜杠，不受影响）。
        rm = re.search(r'"nlp":\{"text":"((?:[^"\\]|\\.)*?)"', line)
        if rm:
            seg = rm.group(1).strip()
            if seg:
                if not self._reply_started:
                    self._reply_started = True
                    if timestamp:
                        self._reply_time = timestamp
                if self._overlap_append(seg):
                    self._reply_last_wall = time.time()
                self._last_reply_seg = seg
        # 回复文本长度保护（防极端超长累积）
        if len(self._reply_text) > 20000:
            self._reply_text = self._reply_text[:20000]

        # 场景切换
        m = re.search(r'SCENE-\w+.*?scene[_=](\w+)', line, re.IGNORECASE)
        if m:
            self._scene = m.group(1)

        # 系统响应检测 - 检测到系统开始处理请求
        # 注意：由于车机系统的日志特性，系统响应和 ASR 识别可能几乎同时出现
        if not self._sys_response_detected:
            sys_response_patterns = (
                'sendAssistantMsg',  # 发送语音消息
                'sendSparkChatMsg',  # 发送Spark聊天消息
                'ASS-SparkManager',  # Spark管理器
                'ASS-SpeechState',   # 语音状态
                'ASS-SrManager',     # SR管理器
                'isssr_log',         # ASR开始识别
                'issse_log',         # ASR识别结果
            )
            if any(kw in line for kw in sys_response_patterns):
                if timestamp:
                    self._sys_response_time = timestamp
                    self._sys_response_detected = True

        # 错误检测 - 更精确，排除正常日志中的 "error" 字样
        ll = line.lower()
        error_patterns = (
            "exception", "crash", "fatal",
            "error:", "error =",  # 有明确分隔的 error
            "failed to", "failure",
            "nullpointer", "runtime error",
        )
        # 排除的正常日志（包含 error 但不是真正的错误）
        normal_patterns = (
            "no error", "errorcode=0", "success",
            "srSolutionError",  # ASR解决方案，不是真正的错误
            "error_code", "error_message",
            "errorcode", "errorinfo",
            "getStructured",  # API调用日志
            "handle()", "status =",
            # 系统噪音：与语音链路无关，不应覆盖超时/无回复判定
            "failed to find provider",
            "locationprovider",
            "activitythread",
            "accessibilitycache",
            "accessibilitynodeinfo",
            "view.accessibility",
        )
        if any(kw in ll for kw in error_patterns):
            if not any(fp in ll for fp in normal_patterns):
                self._error_found = True
                self._error_msg = line.strip()[:200]

        # AI 生图完成图 / 占位图（用于"端侧已生图"兜底判定）
        # 日志中字段可能被多层内嵌转义（\\\"imageUrl\\\"），统一把反斜杠+引号归一为引号
        # 事件时间优先取行内 timestamp（epoch ms），避免 DataPointImpl 批量上报的
        # 行首打印时间偏差与历史残留串扰（同一条 imageUrl 会被周期性重复上报）。
        unescaped = re.sub(r'\\+"', '"', line)
        event_ts = self._extract_event_ts(unescaped, timestamp)
        if event_ts and self._send_timestamp and self._is_timestamp_before(event_ts, self._send_timestamp):
            return  # 该图事件早于发送时刻 → 历史残留，忽略整行
        if not self._final_image_time:
            if re.search(r'(?<!expend)"imageUrl"\s*:\s*"\S{10,}', unescaped):
                self._final_image_time = event_ts or timestamp
        if not self._placeholder_time:
            if re.search(r'"expendImageUrl"\s*:\s*"\S{10,}', unescaped):
                self._placeholder_time = event_ts or timestamp

    def _extract_event_ts(self, unescaped: str, line_ts: str) -> str:
        """提取事件真实时间戳：优先行内 timestamp(epoch ms, 北京时间)，否则用行首时间"""
        m = re.search(r'"timestamp"\s*:\s*(\d{13})', unescaped)
        if m:
            try:
                evt = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000)
                return evt.strftime("%m-%d %H:%M:%S.%f")[:-3]
            except (ValueError, OSError):
                pass
        return line_ts

    def _overlap_append(self, seg: str) -> bool:
        """将回复分片追加到 _reply_text，自动去除与前文的重复。

        日志对回复分片存在两种重复模式：
        1. 整片重复推送（同文本 3~4 次）→ seg 与尾部相同，追加空串；
        2. 重叠增量推送（次帧以首帧尾部开头，如 "不在" 后接 "不在质保范围内。"）
           → 求最长公共后缀/前缀重叠长度，仅追加去重后的新增部分。
        返回是否发生了有效追加（用于更新回复帧墙钟）。
        """
        text = self._reply_text
        seg = seg.strip()
        if not seg:
            return False
        mlen = min(len(text), len(seg))
        overlap = 0
        for k in range(mlen, 0, -1):
            if text.endswith(seg[:k]):
                overlap = k
                break
        if overlap >= len(seg):
            return False  # 整片重复
        self._reply_text = text + seg[overlap:]
        return True

    # ── 判定 ─────────────────────────────────
    def judge(self, test_case: "TestCase") -> Judgment:
        """判定测试结果（优先级链，逐级短路返回）"""
        # 计算各阶段响应时间
        asr_elapsed = self._calc_elapsed(self._asr_time)
        sys_elapsed = self._calc_elapsed(self._sys_response_time)
        intent_elapsed = self._calc_elapsed(self._intent_time)
        tts_elapsed = self._calc_elapsed(self._tts_time)

        # 端到端响应时间（取最后出现的时间点）
        last_time = self._asr_time
        if self._intent_time and self._is_timestamp_after(self._intent_time, last_time):
            last_time = self._intent_time
        if self._tts_time and self._is_timestamp_after(self._tts_time, last_time):
            last_time = self._tts_time
        e2e_elapsed = self._calc_elapsed(last_time)

        # 兼容旧代码：使用 ASR 响应时间作为主响应时间
        elapsed = asr_elapsed

        result = Judgment(
            asr_text=self._asr_text,
            intent_type=self._intent_type,
            plugin_name=self._plugin_name,
            response_time=round(abs(elapsed), 2),
            asr_response_time=round(abs(asr_elapsed), 2),
            sys_response_time=round(abs(sys_elapsed), 2),
            intent_response_time=round(abs(intent_elapsed), 2),
            tts_response_time=round(abs(tts_elapsed), 2),
            e2e_response_time=round(abs(e2e_elapsed), 2),
            log_snippet="\n".join(self._collected_logs[-10:]),
            reply_text=self._reply_text,
        )

        has_response = bool(self._asr_text or self._intent_type)

        # 0.5 注入链路拒识（sparkDenial/cbm_denial）——优先标注原因，提示真实语音验证。
        #   注：语义注入（vr:// getSemantic mode=3）与真实语音走不同链路，
        #   含特殊字符（如英文缩写 L2）的 query 在注入链被 CBM 路由拦截，
        #   真实语音可能正常（chatEnhanced）。此类结果不代表车机缺陷。
        #   拒识优先于关键词 PASS：本地 ASR 有字不代表业务链路已打通。
        if self._denied:
            result.passed = False
            result.reason = (
                f"注入链路拒识：{self._denial_reason}，query 未发送云端。"
                f"注入链路与真实语音行为可能不一致，建议真实语音验证"
            )
            return result

        # 1. 关键词匹配判定
        if test_case.expected_keywords and has_response:
            # 匹配范围收敛：ASR/意图/插件/TTS 结构化字段 + 最近一段日志片段。
            # 不再拼接全部采集日志，避免无关行中的业务词（如"拦截"）造成假阳性。
            recent_logs = " ".join(self._collected_logs[-15:])
            all_text = f"{self._asr_text} {self._intent_type} {self._plugin_name} {self._tts_text} {recent_logs}"
            all_text_lower = all_text.lower()

            # 短关键词(<=10字)必须在日志文本中字面命中
            short_kws = [k for k in test_case.expected_keywords if len(k) <= 10]
            long_kws = [k for k in test_case.expected_keywords if len(k) > 10]

            matched = [k for k in short_kws if k.lower() in all_text_lower]
            missed = [k for k in short_kws if k.lower() not in all_text_lower]
            # 长描述性关键词无法字面匹配：须同时满足 ASR 识别 + 有回复文本，
            # 不能只验 ASR（多轮 Excel 整句期望会大面积误 PASS）
            desc_ok = bool(self._asr_text) and bool(
                self._reply_started or self._tts_text or self._last_reply_seg)

            if short_kws and missed:
                # 兜底1：端侧已生图完成（imageUrl 返回）→ 视为业务成功 PASS
                #   场景：期望关键词是"生成符合描述的图片/正常显示"等通用描述，
                #   端侧 TTS 不会念出这些字面，但生图本身成功了。
                # 兜底2（2026-09-14）：期望文案形如"落域到X意图"时改用实际落域判定，
                #   避免落域类用例因文案无法字面命中而假 FAIL；负向表述（不会落域到…）
                #   不参与该兜底，仍按字面判定。
                domain = self._intent_type or ""
                domain_hit = [k for k in missed
                              if self.domain_expect(k) and self.domain_expect(k).lower() in domain.lower()]
                if domain and len(domain_hit) == len(missed):
                    result.passed = True
                    result.reason = (
                        f"落域兜底命中：实际落域={domain}，期望文案 {', '.join(domain_hit)} 未字面命中"
                    )
                elif self._final_image_time or self._placeholder_time:
                    result.passed = True
                    result.reason = (
                        f"端侧已生图（{'完成图' if self._final_image_time else '占位图'}，"
                        f"关键词 {', '.join(missed)} 未字面命中但视为业务成功）"
                    )
                else:
                    # 短关键词未全部命中 → FAIL，暴露业务策略未生效
                    result.passed = False
                    result.reason = (
                        f"关键词未命中: {', '.join(missed)} "
                        f"(命中 {len(matched)}/{len(short_kws)})"
                    )
            elif short_kws and not missed:
                if long_kws and not desc_ok:
                    result.passed = False
                    result.reason = f"短关键词全命中({len(matched)}/{len(short_kws)})，但描述性期望无ASR/回复确认"
                else:
                    result.passed = True
                    result.reason = f"关键词命中 {len(matched)}/{len(short_kws)}"
            elif long_kws:
                # 只有描述性期望：ASR + 回复 都须到位
                if desc_ok:
                    result.passed = True
                    result.reason = "ASR识别成功且有回复（描述性期望）"
                elif self._asr_text:
                    result.passed = False
                    result.reason = "描述性期望：ASR已识别但无回复文本"
                else:
                    result.passed = False
                    result.reason = "描述性期望：无ASR识别"
            else:
                result.passed = False
                result.reason = "未匹配期望关键词"
            return result

        # 2. 意图匹配判定
        if test_case.expected_intent and self._intent_type:
            if test_case.expected_intent.lower() in self._intent_type.lower():
                result.passed = True
                result.reason = f"意图匹配: {self._intent_type}"
            else:
                result.passed = False
                result.reason = f"意图不匹配: 期望 {test_case.expected_intent}, 实际 {self._intent_type}"
            return result

        # 3. 有 ASR 识别结果，且已收到回复文本才通过（无期望关键词时）。
        #    修复：此前"ASR识别成功即 PASS"会放过无回复（被拒识/多轮追问未返回）
        #    与答非所问用例——回复内容未到达不允许判 PASS。
        if self._asr_text:
            if self._reply_started or self._tts_text:
                result.passed = True
                head = (self._last_reply_seg or self._tts_text).replace("\n", " ")[:36]
                result.reason = f"ASR识别成功，有回复: {head or '(文本见日志)'}"
            else:
                result.passed = False
                result.reason = (
                    f"{self._msg_no_reply}；"
                    f"intent={self._intent_type or '空'}"
                )
            return result

        # 4. 有场景切换也算通过
        if self._scene:
            result.passed = True
            result.reason = f"场景切换: {self._scene}"
            return result

        # 5. 检查是否有错误（只有在没有成功响应时才检查）
        if self._error_found and not has_response:
            result.passed = False
            result.reason = f"检测到错误: {self._error_msg}"
            return result

        # 6. 检查是否超时（没有收到任何有效响应）
        if elapsed >= self._timeout:
            result.passed = False
            result.reason = f"响应超时 ({elapsed:.1f}s)"
            return result

        # 还在等待中
        result.passed = False
        result.reason = "等待响应中..."
        return result

    def is_complete(self) -> bool:
        """判断是否已完成（收到完整响应或超时）"""
        # 生图链路：不能按"ASR+意图出现"即判定完成——完成图需 6~11s 才返回。
        # 必须等到 imageUrl/expendImageUrl 出现，或超时兜底。
        if self._is_aigc:
            if self._final_image_time or self._placeholder_time:
                return True
            if (time.time() - self._start_time) >= self._timeout:
                return True
            return False

        # 普通问答：需 ASR + 落域 + 已收到回复内容帧 才判定完成。
        #   回复帧 = srCode 20204 nlp.text 首片 / tts_text 结果字段（见 _parse_line）。
        #   不再"ASR+意图即完成"或"ASR+3s即完成"——否则无回复/答非所问会被提前判 PASS。
        # 拒识会话不会产生回复，出现拒识特征行即完成，交由 judge 输出拒识原因。
        if self._denied:
            return True

        asr_ok = bool(self._asr_text)
        # ASR 时间戳须在发送之后，防止旧会话串扰
        if asr_ok and self._asr_time and self._send_timestamp:
            if not self._is_timestamp_after(self._asr_time, self._send_timestamp):
                asr_ok = False
        has_reply = self._reply_started or bool(self._tts_text)
        # ASR+回复已齐即可进入静默收敛；intent 缺失只影响 judge 降级，
        # 不再强制依赖 intent（7 种意图模式都提不到时会白等满超时）
        if asr_ok and has_reply:
            # 已开始收到回复帧；需等待流式分片结束：
            # 连续 1.5s 无新回复帧（墙钟）即认为流结束（长回复分片会持续到达）
            if self._reply_last_wall and (time.time() - self._reply_last_wall) >= 1.5:
                return True
            # 无墙钟记录（仅 tts_text 置位）时直接认为完成
            if not self._reply_last_wall:
                return True

        # 超时
        if (time.time() - self._start_time) >= self._timeout:
            return True
        return False

    @staticmethod
    def domain_expect(keyword: str) -> str:
        """从期望文案中解析"落域到X意图"里的 X（用于落域兜底判定）。

        负向表述（如"不会落域到新闻意图"）返回空串——负向用例必须按字面判定，
        不能因为"实际落域=新闻"就误判通过。
        示例: "落域到新闻意图" → "新闻"；"不会落域到新闻意图" → ""
        """
        if not keyword:
            return ""
        if any(neg in keyword for neg in ("不会", "不应", "不能", "未落域", "不落域")):
            return ""
        m = re.search(r"落域(?:到|至)\s*([^，,。；;\s]+?)\s*意图", keyword)
        return m.group(1) if m else ""

    def get_progress_info(self) -> str:
        """获取当前进度信息"""
        parts = []
        if self._asr_text:
            parts.append(f"ASR: {self._asr_text}")
        if self._intent_type:
            parts.append(f"意图: {self._intent_type}")
        if self._plugin_name:
            parts.append(f"插件: {self._plugin_name}")
        if self._scene:
            parts.append(f"场景: {self._scene}")
        if not parts:
            elapsed = time.time() - self._start_time if self._start_time > 0 else 0
            parts.append(f"等待响应... {elapsed:.1f}s")
        return " | ".join(parts)
