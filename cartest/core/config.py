"""CarTest core — 配置与常量

路径、ADB 常量、监控关键词、默认参数、JSON 配置持久化。

设计约束（见 02-core接口设计.md）：
- 零 UI 依赖；本模块不含任何展示层内容（颜色表已移出）
- 不在导入时创建目录，改为显式调用 ``ensure_dirs()``
- 标准库优先
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────
# 打包(onefile)后：可写目录 = exe 所在目录；打包资源(用例 data) = 临时解压目录 _MEIPASS
# 开发时两者相同。


def _resolve_dirs() -> tuple[Path, Path]:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        resource = Path(getattr(sys, "_MEIPASS", base))
    else:
        # cartest/core/config.py → parents[2] = 项目根
        base = Path(__file__).resolve().parents[2]
        resource = base
    return base, resource


APP_DIR, RESOURCE_DIR = _resolve_dirs()
BASE_DIR = APP_DIR
DATA_DIR = RESOURCE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"
RUNS_DIR = BASE_DIR / "runs"


def ensure_dirs() -> None:
    """显式创建运行所需目录（替代旧实现"导入即 mkdir"的副作用）。"""
    for d in (DATA_DIR, REPORTS_DIR, LOGS_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── 配置持久化 ────────────────────────────────────────
SETTINGS_FILE = BASE_DIR / "settings.json"


class Store:
    """JSON 配置持久化（读写失败不中断主流程；**mtime 感知**：外部修改后读取即新鲜）。

    P3c-1 改造：
      · get/data 访问前检查文件 mtime，变化（或文件被删）则重读；
      · set/save 返回是否落盘成功（调用方可显式处理失败，不静默）；
      · 写失败时内存回滚为磁盘真相，避免"内存有值、磁盘没有"的假象。
    """

    def __init__(self, path: str | Path | None = None, defaults: dict | None = None):
        self._path = Path(path) if path else SETTINGS_FILE
        self._defaults = dict(defaults or {})
        self._mtime_ns: int | None = None
        self._data = self._load()

    # ── 新鲜度 ────────────────────────────────────────
    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        if self._stat_mtime_ns() != self._mtime_ns:
            self._data = self._load()

    def _load(self) -> dict:
        data = dict(self._defaults)
        try:
            if self._path.exists():
                data.update(json.loads(self._path.read_text(encoding="utf-8")))
        except Exception:
            pass
        self._mtime_ns = self._stat_mtime_ns()
        return data

    # ── 读 ────────────────────────────────────────────
    def get(self, key: str, default=None):
        self._maybe_reload()
        return self._data.get(key, default)

    @property
    def data(self) -> dict:
        self._maybe_reload()
        return dict(self._data)

    # ── 写 ────────────────────────────────────────────
    def set(self, key: str, value) -> bool:
        self._maybe_reload()
        self._data[key] = value
        if self.save():
            return True
        self._data = self._load()      # 写失败：回滚为磁盘真相
        return False

    def update(self, **kwargs) -> bool:
        self._maybe_reload()
        self._data.update(kwargs)
        if self.save():
            return True
        self._data = self._load()
        return False

    def pop(self, key: str) -> bool:
        """删除键（回到"未设置"状态，而不是写 ``null``）——用于清除旧覆盖"""
        self._maybe_reload()
        self._data.pop(key, None)
        if self.save():
            return True
        self._data = self._load()
        return False

    def save(self) -> bool:
        ok = True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            ok = False
        finally:
            self._mtime_ns = self._stat_mtime_ns()
        return ok


# ── ADB 配置 ─────────────────────────────────────────
ADB_TIMEOUT = 30.0
ADB_SEND_TIMEOUT = 10.0
ADB_CACHE_TTL = 30.0

# 默认设备 ID（留空 = 自动检测第一台在线设备）
DEFAULT_DEVICE_ID = ""

# VR 语义注入 URI
VR_URI_BASE = "vr://com.iflytek.autofly.vr/interaction/getSemantic"

# 语音相关包名
VOICE_PACKAGES = [
    "com.iflytek.autofly.platformservice",
    "com.iflytek.autofly.sceneengine",
    "com.iflytek.autofly.vr",
    "com.example.carvoice",
]

# 日志远程目录
LOG_REMOTE_DIR = "/data/log/voice/"

# ── 日志监控关键词 ────────────────────────────────────
# 内容审核/评论拦截类（品牌正向引导策略）——下列两组共用
_AUDIT_KEYWORDS = [
    "拦截", "审核", "负面", "评论审核", "评论流",
    "contentaudit", "commentaudit", "commentfilter", "contentfilter",
    "reviewfilter", "moderation", "moderate",
    "negativecomment", "negativereview",
    "hitrule", "hit_rule", "rulename", "rule_name",
    "blocked", "blocklist", "sensitiveword",
    "risklevel", "risk_level",
    "brandstrategy", "strategyguide", "brandguide",
    "censorship", "censor",
]

#: 实时监控关键词（LogcatMonitor 使用；按**小写**匹配）
#: 注意：这是 log_monitor.py 中实际生效的那一份，原 config.MONITOR_KEYWORDS
#: 是未被引用的死代码，此处已用实际生效值取代（2026-09-14 整理）
MONITOR_KEYWORDS = [
    "isssr", "issse", "asr", "tts", "scene", "intent",
    "voice", "speech", "iflytek", "autofly", "vr",
    "normal_text", "intent_type", "plugin_name",
    "carvoice", "carvoice", "platformservice",
] + _AUDIT_KEYWORDS

#: 全量落盘关键词（FullLogMonitor 使用；按小写包含匹配）
FULL_LOG_KEYWORDS = [
    "ASS-", "SCENE-", "VoiceProcessor", "TtsServiceAgent",
    "isssr", "issse", "DataPointImpl", "AppUsageTracker",
    "VisualizerManager", "carvoice", "CarVoice",
    "iflytek", "autofly", "vr", "speech", "asr", "tts",
    "PlatformService", "SceneEngine", "AgentAssistant",
    "MsManagerAdapter", "AIKIT", "CiatFunc",
] + _AUDIT_KEYWORDS

# ADB daemon 故障特征
ADB_DAEMON_FAIL_KWS = (
    "daemon", "cannot connect", "protocol fault",
    "connection reset", "failed to start", "could not read",
)

# ── 默认参数 ─────────────────────────────────────────
DEFAULT_INTERVAL = 3.0          # 灌入间隔（秒）
DEFAULT_RESULT_TIMEOUT = 20.0   # 结果判定超时（秒）
