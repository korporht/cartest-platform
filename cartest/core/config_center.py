# -*- coding: utf-8 -*-
"""CarTest core — 配置中心（schema + 统一读写入口）

设计依据：``08-P3c设计.md`` §4。
  · **schema 驱动**：配置项以数据描述（分组/键/类型/默认值/说明），配置页据此自动渲染
  · **单一真相源**：全部读写经 ``settings.json``（``config.Store``，mtime 感知）
  · **类型收敛**：``path_list``（本期唯一实现渲染）；``bool/text/int`` 预留
  · 零 UI 依赖（core 层，可被流水线与工具共同引用）

用法::

    from cartest.core import config_center
    paths = config_center.effective_paths("pull")    # 日志路径（属于方案）
    config_center.get("tools.monkey_tool_dir")       # 普通键：settings 存储
    config_center.get("project.log.pull_paths")      # project.* 键：方案档案兜底
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from . import config as _config
from .config import Store

# ── 字段类型 ──────────────────────────────────────────
TYPE_PATH_LIST = "path_list"   # list[{"path": str, "alias": str, "enabled": bool}]
TYPE_BOOL = "bool"             # 开关（配置页渲染 ft.Switch）
TYPE_TEXT = "text"             # 单行文本
TYPE_INT = "int"               # 整数（带校验）
TYPE_TEXTAREA = "textarea"     # 多行文本 → list[str]（一行一词；关键词表用）
TYPE_TAGS = "tags"             # 标签式列表（可点删的胶囊 + 回车添加；关键词首选）

# ── 默认值（= 旧硬编码值，逐字节一致；由 tests/test_config_center.py 钉死）──
_DEFAULT_PULL_PATHS: list[dict] = [
    {"path": _config.LOG_REMOTE_DIR, "alias": "Android", "enabled": True},
    {"path": "/mnt/log/AIOS/", "alias": "AIOS", "enabled": False},
    {"path": "/mnt/log/DLT/", "alias": "DLT", "enabled": False},
]
_DEFAULT_SIZE_PATHS: list[dict] = [
    {"path": _config.LOG_REMOTE_DIR, "alias": "Android", "enabled": True},
    {"path": "/mnt/log/AIOS/", "alias": "AIOS", "enabled": True},
    {"path": "/mnt/log/DLT/", "alias": "DLT", "enabled": True},
]

#: **本地输出根目录**默认值（2026-10-12 用户裁决：不填＝工具所在目录下 ``logs/``）
#: ``APP_DIR`` 开发时＝项目根、打包后＝exe 所在目录（``config._resolve_dirs``）。
#: 产物按子目录落：``logcat`` / ``screenshots`` / ``recordings``（见 :func:`output_dir`）。
_DEFAULT_OUTPUT_ROOT: str = str(_config.APP_DIR / "logs")

#: 配置 schema（加配置 = 加条目；配置页遍历渲染）
SCHEMA: list[dict] = [
    {
        "group": "日志抓取路径",
        "desc": "抓取与清理日志的远端路径列表（只保留一条即单路径；别名用于产物命名）。"
                "**属于当前方案**——保存即写进方案文件，切换方案随方案切换、切回来还在",
        "key": "project.log.pull_paths",
        "type": TYPE_PATH_LIST,
        "default": None,          # None = 跟随方案档案（内置档案已带默认值）
    },
    {
        "group": "日志抓取路径",
        "label": "本地输出根目录",
        "desc": "本机产物的保存根目录，按子目录分：logcat（实时抓取）/ screenshots（截图）"
                "/ recordings（录屏）；拉取日志直接落根目录。留空＝工具所在目录下的 logs",
        "key": "local.output_root",
        "type": TYPE_TEXT,
        "default": _DEFAULT_OUTPUT_ROOT,
        "placeholder": _DEFAULT_OUTPUT_ROOT,
    },
    {
        "group": "日志大小检查路径",
        "desc": "「查看大小」逐条查询的远端路径列表。**属于当前方案**（同上）",
        "key": "project.log.size_paths",
        "type": TYPE_PATH_LIST,
        "default": None,
    },
    # ── 事件窗口（操作日志）整组已于 2026-10-12 下线 ──
    # 用户裁决「事件窗口只作展示 app 的操作，用不到搜索等功能」→ 工具条（模式过滤/
    # 详细输出/跟随/搜索/正则/清空）与 `ui.oplog.*` 六项配置全部移除（含此前的
    # `ui.oplog.module` 死配置）。事件窗口现在是纯展示面板：恒自动跟随最新行、
    # 噪音/底层行永久隐藏、模式恒「全部」、无检索与清空入口。
    # ── 项目档案（30/31 号：值级定制点外提；默认值 = data/projects/<active>.json）──
    {
        "group": "车机应用",
        "label": "主包名",
        "desc": "Monkey 压测默认包名、抓包过滤与结果页标识。留空＝跟随项目档案",
        "key": "project.device.main_package",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "车机应用",
        "label": "设备序列号",
        "desc": "该项目对应车机的 adb 序列号（adb devices 中的 SN）。留空＝跟随项目档案；"
                "切换项目时顶栏自动选它（离线则保留并标记）",
        "key": "project.device.serial",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "车机应用",
        "label": "设备别名",
        "desc": "顶栏设备胶囊显示的名字（如 T28 / T1MA）。留空＝跟随项目档案的 "
                "device.alias；档案也没有则取项目名括号前段。改完保存，顶栏即时更新",
        "key": "project.device.alias",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "语音灌入",
        "label": "注入 URI",
        "desc": "语义灌入所用 URI（vr://…getSemantic）。留空＝跟随项目档案",
        "key": "project.voice.inject_uri",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "日志关键词",
        "label": "实时监控关键词",
        "desc": "命中即投递到事件窗口；点标签上的 ✕ 删除，输入后回车添加",
        "key": "project.log.monitor_keywords",
        "type": TYPE_TAGS,
        "recommended_path": "log.monitor_keywords",
        "default": None,
    },
    # ── 外部工具目录（留空＝自动探测；见 core/tools_locator.py）──
    {
        "group": "外部工具目录",
        "label": "CbmLogextractor",
        "desc": "对话还原所用工具目录（内含 logextract.py）。留空＝自动探测",
        "key": "tools.cbm_logextractor_dir",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "外部工具目录",
        "label": "Monkey 自动化测试",
        "desc": "Monkey 压测所用工具目录。留空＝自动探测",
        "key": "tools.monkey_tool_dir",
        "type": TYPE_TEXT,
        "default": None,
    },
    # ── 判定规则（30 号 §5.4：规则级定制点外提）──
    {
        "group": "判定规则",
        "label": "拒识特征正则",
        "desc": "命中即判「注入链路拒识」（CBM 路由拦截 sparkDenial/cbm_denial）。"
                "留空＝跟随项目档案",
        "key": "project.judge.deny_pattern",
        "type": TYPE_TEXT,
        "default": None,
    },
    {
        "group": "判定规则",
        "label": "无回复判定文案",
        "desc": "ASR 识别成功但无回复时写入判定原因/报告的文案。留空＝跟随项目档案",
        "key": "project.judge.messages.no_reply",
        "type": TYPE_TEXT,
        "default": None,
    },
]

# ── 存储（单一真相源；测试可用 set_settings_file 隔离）──
SETTINGS_FILE: Path = _config.SETTINGS_FILE
_STORE: Store | None = None


def set_settings_file(path: str | Path) -> None:
    """覆盖配置文件路径并重置缓存（**测试隔离用**，正常运行无需调用）。"""
    global SETTINGS_FILE, _STORE
    SETTINGS_FILE = Path(path)
    _STORE = None


def _store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store(SETTINGS_FILE)
    return _STORE


# ── 查询 ──────────────────────────────────────────────
def schema() -> list[dict]:
    """schema 深拷贝（防调用方误改框架数据）。"""
    return copy.deepcopy(SCHEMA)


def defaults() -> dict[str, Any]:
    """全部键 → 默认值（深拷贝）。"""
    return {item["key"]: copy.deepcopy(item["default"]) for item in SCHEMA}


_MISSING = object()

#: ``project.*`` 键前缀（值级定制点：留空则跟随项目档案）
PROJECT_PREFIX = "project."


def _profile_default(key: str):
    """档案兜底：``project.*`` 键剥前缀取档案值；**非 project. 键按同路径取**
    （如 ``log.pull_paths`` 对应档案 ``log.pull_paths`` 字段——T28/T1MA 平台路径随档案）

    延迟导入避免 core 内循环。
    """
    try:
        from . import project as _project
        path = key[len(PROJECT_PREFIX):] if key.startswith(PROJECT_PREFIX) else key
        # 深拷贝：档案带 mtime 缓存（`_LOAD_CACHE`），直接返回内部引用会让调用方
        # 改返回值污染缓存（2026-10-12 加，见 tests/test_config_center.py 深拷贝断言）
        return copy.deepcopy(_project.profile_value(path))
    except Exception:  # noqa: BLE001 - 档案不可用时按"未设置"处理
        return None


def get(key: str, default: Any = _MISSING) -> Any:
    """读取配置；不传 default 时使用 schema 默认值（深拷贝安全）

    ``project.*`` 键：未落盘（或落盘为 ``None``）时兜底到**项目档案**，
    从而实现「settings 覆盖档案、档案覆盖内置」的三层取值。
    """
    stored = _store().get(key, _MISSING)
    if stored is not _MISSING and stored is not None:
        return stored
    if default is not _MISSING:
        return stored if stored is not _MISSING else default
    d = defaults().get(key)
    if d is None and key.startswith(PROJECT_PREFIX):
        d = _profile_default(key)
    return d



# ── 写入 ──────────────────────────────────────────────
def set(key: str, value: Any) -> bool:
    """写入配置；返回是否落盘成功（失败由调用方显式处理，不静默）。"""
    return _store().set(key, value)


def reset(key: str) -> bool:
    """恢复某键为 schema 默认值（显式写回默认值，不删键）。"""
    return set(key, defaults().get(key))


def clear(key: str) -> bool:
    """**删除**某键（回到"未设置"状态）——清掉旧覆盖用（`project.*` 值改存方案文件后）"""
    return _store().pop(key)


# ── 便捷消费接口 ──────────────────────────────────────
def stored(key: str, default: Any = None) -> Any:
    """**仅原始存储值**（不看 schema 默认、不经档案兜底）

    用途：判定"用户是否显式写过该键"——:func:`effective_paths` 三层取值的第一层。
    """
    return _store().get(key, default)


#: 日志路径键与 kind 的映射（**方案值**；2026-10-12 起从 settings 覆盖键改为 project.*）
_PATH_KEYS: dict[str, str] = {"pull": "project.log.pull_paths",
                              "size": "project.log.size_paths"}


def effective_paths(kind: str) -> list[dict]:
    """日志路径取值（供抓取/清理/大小检查消费）

    ``kind``: ``"pull"``（抓取与清理）｜ ``"size"``（大小检查）

    取值链（2026-10-12 用户裁决改为"属于方案"）：

      ① **方案/档案值**（``project.log.*`` → 档案 ``log.*``；内置档案带默认值）
      ② schema 默认兜底

    改动动因：原先路径存在 settings 覆盖里，**切换方案会被清掉**，用户感知为
    "保存的设置失效了"；改为方案值后，保存即写进方案文件、切方案自然跟随。

    ⚠️ 老键（``log.pull_paths`` / ``log.size_paths``）**刻意不再参与取值**：
    若继续兼容，会出现"配置页显示方案值、实际却用遗留 settings 值"的不一致
    （2026-10-12 实测踩到）。遗留键由 :func:`cartest.core.project.set_active`
    在切方案时清掉。
    """
    key = _PATH_KEYS.get(kind)
    if not key:
        return []
    prof = _profile_default(key)
    if prof:
        return list(prof)
    return copy.deepcopy(_DEFAULT_PULL_PATHS if kind == "pull" else _DEFAULT_SIZE_PATHS)


def output_root() -> str:
    """**本地输出根目录**（留空 → 工具所在目录下的 ``logs/``）

    2026-10-12 用户裁决：原先是「抓取日志只有目标路径、没有输出路径」，且各工具产物
    分散写死在 ``data/tools/`` 下（logcat / screenshots / recordings 各一份）；
    现统一为一个可配置根目录——不填时用 ``APP_DIR/logs``
    （开发＝项目根，打包＝exe 所在目录）。
    """
    val = get("local.output_root")
    text = str(val).strip() if val else ""
    return text or _DEFAULT_OUTPUT_ROOT


def output_dir(*parts: str) -> str:
    """产物子目录：``output_dir("logcat")`` → ``<root>/logcat``；无参＝根目录"""
    return str(Path(output_root()).joinpath(*parts)) if parts else output_root()


def primary_pull_path() -> str:
    """流水线用：第一条**启用且路径非空**的抓取路径；否则回退 ``LOG_REMOTE_DIR``。"""
    for item in effective_paths("pull"):
        if item.get("enabled") and str(item.get("path", "")).strip():
            return str(item["path"])
    return _config.LOG_REMOTE_DIR
