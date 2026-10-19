# -*- coding: utf-8 -*-
"""CarTest core — 项目档案（ProjectProfile）

设计依据：``30-功能分类与可配置分离.md`` §5.2（用户 2026-09-21 选定）。

**要解决的问题**：品牌 / 语音栈 / 日志路径 / 监控关键词等项目定制点原先**硬编码在
通用内核**（``core/config.py`` / ``core/adb.py`` / ``app/tools/monkey_tab.py`` …），
换一个车机品牌要动 6 个内核文件。现在改为**三层取值**：

    ① ``settings.json`` 的 ``project.<path>``   —— 本机微调（最高优先）
    ② ``data/projects/<project>.json``          —— 项目档案（换项目＝换文件）
    ③ ``core/config.py`` 常量                   —— 内置兜底（无档案时行为与改造前一致）

用法::

    from cartest.core import project
    uri = project.get("voice.inject_uri", VR_URI_BASE)   # 声明的 default 即内置兜底
    project.set_active("e0v")
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import config as _config
from . import config_center

_log = logging.getLogger(__name__)

__all__ = ["BUILTIN_KEY", "BUILTIN_SERVICES", "PROJECT_SCOPED_PREFS",
           "PROJECT_SCOPED_SETTINGS", "reset_scoped_settings",
           "builtin_profile", "projects_dir", "set_projects_dir", "list_projects",
           "load", "active_key", "set_active", "save", "get", "profile_value",
           "alias_for_serial", "reset_scoped_prefs", "tools_dir",
           "add_active_listener", "remove_active_listener",
           # 设备绑定（2026-10-19）：方案 ↔ 设备序列号
           "scheme_devices", "scheme_for_device", "bind_device", "unbind_device",
           # 配置方案生命周期（2026-09-22）
           "MAX_SCHEMES", "DEFAULT_LABEL", "list_schemes", "scheme_count",
           "scheme_exists", "create_scheme", "rename_scheme", "remove_scheme",
           "builtin_value", "save_values"]

#: 内置档案键（E0V 奇瑞；无档案文件时即用它的值）
BUILTIN_KEY = "e0v"

#: 默认项目档案目录（可用 :func:`set_projects_dir` 覆盖——测试隔离用）
_PROJECTS_DIR: Path | None = None

#: 档案加载缓存：key → ((mtime_ns, size), data)（热路径避免反复读盘/深合并）
_LOAD_CACHE: dict[str, tuple] = {}

#: 内置语音服务组件（``core/adb.py`` 原字面量的单一来源）
BUILTIN_SERVICES: list[dict] = [
    {"label": "语音引擎",
     "component": "com.iflytek.autofly.platformservice/"
                  "com.iflytek.autofly.platformservice.PlatformService"},
    {"label": "场景引擎", "component": "com.iflytek.autofly.sceneengine/.SceneEngineService"},
    {"label": "VR 服务", "component": "com.iflytek.autofly.vr/.VrService"},
]


def projects_dir() -> Path:
    """项目档案目录（默认 ``data/projects``）"""
    return _PROJECTS_DIR if _PROJECTS_DIR is not None else (_config.DATA_DIR / "projects")


def set_projects_dir(path: str | Path | None) -> None:
    """覆盖档案目录（**测试隔离用**；``None`` 复位为默认）"""
    global _PROJECTS_DIR
    _PROJECTS_DIR = Path(path) if path is not None else None
    _LOAD_CACHE.clear()


def builtin_profile() -> dict:
    """内置档案（逐值等于 ``core/config.py`` 常量——无档案时行为不变）"""
    return {
        "key": BUILTIN_KEY,
        "name": "E0V 奇瑞（内置默认）",
        "device": {
            "main_package": "com.lioncarmind.car",          # = monkey_tab.DEFAULT_PKG
            "voice_packages": list(_config.VOICE_PACKAGES),
            "services": copy.deepcopy(BUILTIN_SERVICES),
        },
        "voice": {
            "inject_uri": _config.VR_URI_BASE,
            "inject_mode": 3,
        },
        "log": {
            "monitor_keywords": list(_config.MONITOR_KEYWORDS),
            "fulllog_keywords": list(_config.FULL_LOG_KEYWORDS),
            # 日志路径（2026-10-12 用户裁决：从 settings 覆盖键改为**方案值**）——
            # 值 = ``config_center._DEFAULT_PULL_PATHS`` / ``_DEFAULT_SIZE_PATHS``，
            # 两处不许漂移（tests/test_config_center.py 钉死）
            "pull_paths": [
                {"path": _config.LOG_REMOTE_DIR, "alias": "Android", "enabled": True},
                {"path": "/mnt/log/AIOS/", "alias": "AIOS", "enabled": False},
                {"path": "/mnt/log/DLT/", "alias": "DLT", "enabled": False},
            ],
            "size_paths": [
                {"path": _config.LOG_REMOTE_DIR, "alias": "Android", "enabled": True},
                {"path": "/mnt/log/AIOS/", "alias": "AIOS", "enabled": True},
                {"path": "/mnt/log/DLT/", "alias": "DLT", "enabled": True},
            ],
        },
        # 判定规则与文案（代码兜底；值与 core/judger.py 的 DEFAULT_* 一致，
        # 由 tests/test_judge_rules.py 钉死"两处不许漂移"）
        "judge": {
            "deny_pattern": (
                r'sparkDenial|大模型拒识|'
                r'pk_type["\']?\s*[:=]\s*["\']?cbm_denial|domain\s*=\s*\[cbm_denial\]'
            ),
            "denial_reason": "车机CBM路由拒识(sparkDenial)",
            "messages": {
                "no_reply": "ASR识别成功但无回复文本（拒识/多轮追问未返回/长回复超时）",
            },
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并（override 覆盖 base；list 整体替换，不做元素级合并）"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 损坏档案按"不存在"处理（有测试钉死）
        return None


def list_projects() -> list[dict]:
    """可用项目列表（内置 + 档案目录下每个 ``*.json``；同键**去重**，文件优先）

    UI 请优先用 :func:`list_schemes`（「配置方案」下拉只列方案文件）。
    """
    by_key: dict[str, dict] = {
        BUILTIN_KEY: {"key": BUILTIN_KEY, "name": builtin_profile()["name"],
                      "path": None, "builtin": True}}
    d = projects_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            data = _read_json(f)
            key = (data or {}).get("key") or f.stem
            by_key[str(key)] = {"key": str(key),
                                "name": (data or {}).get("name", f.stem),
                                "path": f, "builtin": False}
    items = list(by_key.values())
    items.sort(key=lambda p: (p["key"] != BUILTIN_KEY, p["key"]))
    return items


# ══════════════════════════════════════════════════════════════
#  配置方案生命周期（2026-09-22 用户裁决：可自定义增/删/改，上限 3 组）
#  「方案」＝ data/projects/<key>.json；「默认（内置）」＝无文件时的回退态。
# ══════════════════════════════════════════════════════════════

#: 方案数量上限（2026-09-22 用户裁决最多 3 组；2026-10-16 放宽到 5 组，全部可删）
MAX_SCHEMES = 5

#: 无方案时的下拉显示名（回退内置常量，不占额度、不可删）
DEFAULT_LABEL = "默认（内置）"


def list_schemes() -> list[dict]:
    """**只列方案文件**（``data/projects/*.json``，按 key 排序）

    返回 ``{key, name, path}``——供「配置方案」下拉使用（不含内置项）。
    """
    out: list[dict] = []
    d = projects_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            data = _read_json(f) or {}
            out.append({"key": str(data.get("key") or f.stem),
                        "name": str(data.get("name") or f.stem),
                        "path": f})
    out.sort(key=lambda p: p["key"])
    return out


def scheme_count() -> int:
    """方案数量（额度判断用）"""
    return len(list_schemes())


def scheme_exists(key: str) -> bool:
    """该键是否已有方案文件"""
    return (projects_dir() / f"{str(key)}.json").is_file()


def _used_keys() -> set[str]:
    return {p["key"] for p in list_schemes()} | {BUILTIN_KEY}


def create_scheme(name: str) -> tuple[bool, str]:
    """新建**空白方案**（只写 ``{key, name}``，不含任何值 → 全走内置默认）

    返回 ``(ok, key_or_原因)``。失败：超额度 / 名称为空 / 名称重复。
    新建后**自动激活**由调用方决定（UI 会切过去）。
    """
    label = (name or "").strip()
    if not label:
        return False, "方案名不能为空"
    existing = list_schemes()
    if len(existing) >= MAX_SCHEMES:
        return False, f"最多 {MAX_SCHEMES} 组方案（请先删除一个）"
    if any(p["name"].strip() == label for p in existing):
        return False, f"方案名已存在：{label}"
    used = _used_keys()
    key = ""
    for i in range(1, MAX_SCHEMES + 1):
        if f"s{i}" not in used:
            key = f"s{i}"
            break
    if not key:
        return False, f"最多 {MAX_SCHEMES} 组方案（请先删除一个）"
    if not save(key, {"key": key, "name": label}):
        return False, "写入方案文件失败"
    return True, key


def rename_scheme(key: str, name: str) -> tuple[bool, str]:
    """重命名方案（只改 ``name``，**key 不变** → 激活引用不悬空）"""
    label = (name or "").strip()
    if not label:
        return False, "方案名不能为空"
    if not scheme_exists(key):
        return False, "方案不存在"
    if any(p["name"].strip() == label and p["key"] != str(key)
           for p in list_schemes()):
        return False, f"方案名已存在：{label}"
    data = _read_json(projects_dir() / f"{key}.json") or {}
    data["name"] = label
    data.setdefault("key", str(key))
    if not save(str(key), data):
        return False, "写入方案文件失败"
    return True, label


def remove_scheme(key: str) -> tuple[bool, str]:
    """删除方案；**若删的正是激活方案 → 自动切到剩余第一个**（都没了 → 回内置默认）

    返回 ``(ok, 说明/切换到的 key)``。
    """
    key = str(key)
    path = projects_dir() / f"{key}.json"
    if not path.is_file():
        return False, "方案不存在"
    was_active = (key == active_key())
    try:
        path.unlink()
    except OSError as ex:  # noqa: BLE001
        return False, f"删除失败：{ex}"
    _LOAD_CACHE.pop(key, None)
    if not was_active:
        return True, key
    rest = list_schemes()
    nxt = rest[0]["key"] if rest else BUILTIN_KEY
    set_active(nxt)
    return True, nxt


def migrate_builtin_file() -> str:
    """一次性迁移：旧的物化 ``e0v.json`` 让位给「默认（内置）」（幂等，可每次启动调用）

    32 号起 ``e0v`` 专指"无方案时的内置回退态"。磁盘上若存在旧版物化的
    ``e0v.json``：

      · 与内置值**完全一致** → 直接删除（等价，回退内置）
      · 有真实差异 → 另存为 ``s<n>``（保留原 name），激活键跟着改（不丢用户数据）
      · 已满 3 组 → 原样不动（不擅自删用户数据）

    返回 ``"none" | "removed" | "renamed:<key>"``。
    """
    path = projects_dir() / f"{BUILTIN_KEY}.json"
    if not path.is_file():
        return "none"
    raw = _read_json(path)
    if not isinstance(raw, dict):
        try:
            path.unlink()
        except OSError:
            return "none"
        _LOAD_CACHE.pop(BUILTIN_KEY, None)
        return "removed"

    base = {k: v for k, v in builtin_profile().items() if k not in ("key", "name")}
    probe = {k: v for k, v in raw.items() if k not in ("key", "name", "_readme")}
    if not probe or _deep_merge(base, probe) == base:      # 无真实差异 → 删（等于内置）
        try:
            path.unlink()
        except OSError:
            return "none"
        _LOAD_CACHE.pop(BUILTIN_KEY, None)
        if active_key() == BUILTIN_KEY:
            config_center.clear("project.active")          # 回到内置态
        return "removed"

    used = _used_keys()
    new_key = ""
    for i in range(1, MAX_SCHEMES + 1):
        if f"s{i}" not in used:
            new_key = f"s{i}"
            break
    if not new_key:
        return "none"
    raw["key"] = new_key
    raw.setdefault("name", "默认方案")
    if not save(new_key, raw):
        return "none"
    if active_key() == BUILTIN_KEY:
        set_active(new_key)
    try:
        path.unlink()
    except OSError:
        pass
    _LOAD_CACHE.pop(BUILTIN_KEY, None)
    return f"renamed:{new_key}"


def builtin_value(path: str, default: Any = None) -> Any:
    """**内置常量层**取值（不看不合并任何文件）——差异存档的比较基准"""
    node: Any = builtin_profile()
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = str(path).split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _del_path(data: dict, path: str) -> None:
    parts = str(path).split(".")
    stack = [data]
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return
        node = nxt
        stack.append(node)
    node.pop(parts[-1], None)
    for parent, part in reversed(list(zip(stack, parts))):   # 清掉变空的父节点
        if isinstance(parent.get(part), dict) and not parent[part]:
            parent.pop(part, None)


def save_values(flat: dict) -> tuple[bool, str]:
    """把配置页的 ``project.*`` 值**写进当前方案文件**（只存与内置默认不同的差异）

    ``flat`` 形如 ``{"project.device.main_package": "com.x", ...}``：

      · 值 == 内置默认（或为空）→ 从方案里**删掉**该 path（＝回到「留空/跟随」）
      · 否则写入方案文件
      · 顺带**清掉 ``settings.json`` 里残留的 ``project.*`` 覆盖键**（一次性吸收迁移：
        旧版本保存的是 settings 覆盖，语义改为"值只属于方案"）

    无激活方案（回退内置态）时，改值会**自动物化成方案**（key 取最小未占用）。
    """
    entries: list[tuple[str, str, Any]] = []     # (settings 全键, 档案 path, 值)
    prefix = config_center.PROJECT_PREFIX
    for k, v in (flat or {}).items():
        if not str(k).startswith(prefix) or str(k) == "project.active":
            continue                                 # project.active 是"当前方案"，不是档案值
        entries.append((str(k), str(k)[len(prefix):], v))
    if not entries:
        return True, "无 project.* 值需写入"

    key = active_key()
    if not scheme_exists(key):
        # 回退内置态下保存 → 物化为方案（沿用内置名，避免"改了没存处"）
        used = _used_keys()
        new_key = ""
        for i in range(1, MAX_SCHEMES + 1):
            if f"s{i}" not in used:
                new_key = f"s{i}"
                break
        if not new_key:
            return False, f"最多 {MAX_SCHEMES} 组方案（请先删除一个）"
        key = new_key
        save(key, {"key": key, "name": "默认方案"})

    path = projects_dir() / f"{key}.json"
    data = _read_json(path) or {}
    data.setdefault("key", key)
    data.setdefault("name", key)
    for _full, prof_path, value in entries:
        if value in (None, "", [], {}) or value == builtin_value(prof_path):
            _del_path(data, prof_path)
        else:
            _set_path(data, prof_path, value)
    if not save(key, data):
        return False, "写入方案文件失败"

    # 吸收并清除 settings 里的旧覆盖（值已属于方案）
    for full_key, _, _ in entries:
        try:
            if config_center.get(full_key, None) is not None:
                config_center.clear(full_key)
        except Exception:  # noqa: BLE001
            continue
    if key != active_key():
        set_active(key)
    return True, key



def load(key: str | None = None) -> dict:
    """加载档案（深合并到内置之上；缺失/损坏 → 内置）

    带 **mtime 缓存**：judger / logcat 等热路径每次判定都要读规则，不能反复读盘
    （档案文件 mtime 变化即失效）。
    """
    key = key or active_key()
    path = projects_dir() / f"{key}.json"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    cached = _LOAD_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    base = builtin_profile()
    data = _read_json(path)
    out = _deep_merge(base, data) if data else base
    _LOAD_CACHE[key] = (stamp, out)
    return out


def active_key() -> str:
    """当前激活的项目键（``settings.json`` 的 ``project.active``；缺省内置）"""
    try:
        v = config_center.get("project.active", None)
    except Exception:  # noqa: BLE001
        v = None
    return str(v) if isinstance(v, str) and v.strip() else BUILTIN_KEY


#: 与项目绑定的工具偏好（**切项目时必须清掉**，否则旧值会挡住档案值）
#: 键 = 偏好文件名（``data/tools/<file>``），值 = 其中的键名
PROJECT_SCOPED_PREFS: dict[str, list[str]] = {
    "monkey_config.json": ["monkey_pkg"],
}

#: 与项目绑定的 **settings 覆盖键**（切项目时清掉）
#: 语义＝"日志路径跟随项目"：本机微调过的路径若不清，会让项目档案里的
#: ``log.pull_paths``（如 T28 的 /log/android/logcat/）形同虚设。
#:
#: 2026-10-12 变更：日志路径已改为**方案值**（``project.log.pull_paths`` / ``project.log.size_paths``
#: ——保存写进方案文件，切换方案自然跟随）。本清单自此只承担**遗留迁移**：
#: 清掉 2026-10-12 前写进 settings 的老键（``effective_paths`` 仍认它们，切一次方案即完成迁移）。
PROJECT_SCOPED_SETTINGS: list[str] = ["log.pull_paths", "log.size_paths"]


def reset_scoped_settings() -> list[str]:
    """清掉 settings 里**显式写过**的项目绑定覆盖键；返回被清键列表。

    "显式写过" = settings.json 真实存在该键（schema 默认值不清、也不误报）。
    """
    removed: list[str] = []
    for key in PROJECT_SCOPED_SETTINGS:
        try:
            if config_center.stored(key, None) is not None:
                config_center.clear(key)
                removed.append(key)
        except Exception:  # noqa: BLE001 - 清理失败不影响切项目
            continue
    return removed


def tools_dir() -> Path:
    """工具偏好目录（``data/tools``；与 ``app.tools.constants.LOCAL_SAVE_ROOT`` 同处）"""
    return _config.DATA_DIR / "tools"


def reset_scoped_prefs() -> list[str]:
    """清掉与项目绑定的工具偏好键；返回被清理的 ``"文件:键"`` 列表。

    场景：Monkey 包名这类"项目专属输入"一旦存进工具偏好，就会以**偏好优先**
    挡住新项目档案的值（2026-09-21 真机验证发现）。切项目时清掉，语义＝
    "换项目后项目相关输入回到档案默认"。
    """
    removed: list[str] = []
    for fname, keys in PROJECT_SCOPED_PREFS.items():
        f = tools_dir() / fname
        try:
            if not f.is_file():
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            hit = [k for k in keys if k in data]
            if not hit:
                continue
            for k in hit:
                data.pop(k, None)
                removed.append(f"{fname}:{k}")
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - 清理失败不影响切项目
            continue
    return removed


#: 激活项目变更监听器（set_active 且 key 实际变化时回调；GUI 设备联动用）
_ACTIVE_LISTENERS: list[Callable[[str], None]] = []


def add_active_listener(cb: Callable[[str], None]) -> None:
    """注册激活项目变更监听器（重复注册忽略；回调参数 = 新项目 key）"""
    if cb not in _ACTIVE_LISTENERS:
        _ACTIVE_LISTENERS.append(cb)


def remove_active_listener(cb: Callable[[str], None]) -> None:
    """移除激活项目变更监听器（测试隔离用）"""
    try:
        _ACTIVE_LISTENERS.remove(cb)
    except ValueError:
        pass


def set_active(key: str) -> bool:
    """激活项目（写 ``settings.json``；切换时清理项目相关工具偏好与 settings 覆盖键）

    实际发生切换时通知 :func:`add_active_listener` 注册的监听器（单个失败不影响
    切换本体——失败记入 debug 日志，不静默吞掉）。
    """
    key = str(key)
    changed = key != active_key()
    ok = config_center.set("project.active", key)
    if ok and changed:
        reset_scoped_prefs()
        reset_scoped_settings()
        for cb in list(_ACTIVE_LISTENERS):
            try:
                cb(key)
            except Exception:  # noqa: BLE001
                _log.warning("active 监听器失败: %r", cb, exc_info=True)
    return ok


def save(key: str, data: dict) -> bool:
    """写档案文件（``data['key']`` 缺省补 key）"""
    try:
        d = projects_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = copy.deepcopy(data or {})
        payload.setdefault("key", key)
        payload.setdefault("name", key)
        (d / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _LOAD_CACHE.pop(str(key), None)          # 失效缓存（同 key 立即生效）
        return True
    except Exception:  # noqa: BLE001
        return False


_MISSING = object()


def profile_value(path: str, default: Any = None) -> Any:
    """**仅档案层**取值（深合并内置；不看 ``settings.json``）——供 config_center 兜底用

    :func:`get` 会先看 settings 覆盖，故这里刻意不碰 settings，避免相互递归。
    """
    node: Any = load()
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    if node in (None, "", [], {}):
        return default
    return node


def alias_for_serial(serial: str) -> str:
    """按设备序列号查方案别名（``device.serials`` / ``device.serial`` 匹配；无匹配返回空串）

    别名优先取档案 ``device.alias``；回退 ``name`` 的括号前段
    （如 ``"T28（desaysv 8255 台架）"`` → ``"T28"``）。供顶栏设备胶囊显示。

    2026-10-19：匹配范围从单值 ``device.serial`` 扩展到绑定列表
    （:func:`scheme_devices`），支持一个方案绑定多台台架。
    """
    serial = str(serial or "").strip()
    if not serial:
        return ""
    for p in list_projects():
        key = str(p.get("key", ""))
        try:
            if serial not in scheme_devices(key):
                continue
            prof = load(key)
        except Exception:  # noqa: BLE001
            continue
        dev = prof.get("device", {}) if isinstance(prof, dict) else {}
        alias = str(dev.get("alias", "") or "").strip()
        if not alias:
            name = str(prof.get("name", "") or key)
            alias = name.replace("（", "(").split("(")[0].strip()
        return alias or key
    return ""


# ══════════════════════════════════════════════════════════════
#  设备绑定（2026-10-19 用户诉求：连接/切换设备时自动切配置方案）
#  「方案」＝ data/projects/<key>.json；绑定存在档案 ``device.serials``
#  （列表，一台方案可绑多台台架）；``device.serial`` 保留为「默认设备」
#  兼容字段（项目→设备联动与别名显示仍读它，取 serials[0]）。
#  一设备只属一个方案：绑定时自动从其他方案移除。
# ══════════════════════════════════════════════════════════════

def scheme_devices(key: str) -> list[str]:
    """方案的绑定设备列表（``device.serials`` ∪ 兼容单值 ``device.serial``）"""
    try:
        prof = load(str(key))
    except Exception:  # noqa: BLE001
        return []
    dev = prof.get("device", {}) if isinstance(prof, dict) else {}
    out: list[str] = []
    raw = dev.get("serials")
    if isinstance(raw, list):
        out.extend(str(x).strip() for x in raw if str(x).strip())
    single = str(dev.get("serial", "") or "").strip()
    if single and single not in out:
        out.append(single)
    return out


def scheme_for_device(serial: str) -> str:
    """设备 → 绑定方案 key（无绑定返回 ``""``；只认方案文件，内置不参与）"""
    serial = str(serial or "").strip()
    if not serial:
        return ""
    for p in list_schemes():
        try:
            if serial in scheme_devices(p["key"]):
                return str(p["key"])
        except Exception:  # noqa: BLE001 - 单个方案损坏不影响其余
            continue
    return ""


def _write_scheme_devices(key: str, serials: list[str]) -> bool:
    """写回方案的设备绑定（``serials`` 列表 + ``serial`` 兼容首项）"""
    path = projects_dir() / f"{key}.json"
    data = _read_json(path) or {}
    data.setdefault("key", key)
    data.setdefault("name", key)
    dev = data.get("device")
    if not isinstance(dev, dict):
        dev = {}
    dev["serials"] = list(serials)
    dev["serial"] = serials[0] if serials else ""
    data["device"] = dev
    return save(key, data)


def bind_device(key: str, serial: str) -> tuple[bool, str]:
    """把设备绑定到方案（一设备只属一个方案：先从其他方案移除）

    返回 ``(ok, 说明)``。内置方案（无文件）不可绑定。
    """
    key = str(key)
    serial = str(serial or "").strip()
    if not serial:
        return False, "设备序列号为空"
    if not scheme_exists(key):
        return False, "仅方案文件可绑定设备（内置方案请先新建方案）"

    # 唯一性：同一设备从其他方案移除（避免一份配置抢两台/互相覆盖）
    for p in list_schemes():
        other = str(p["key"])
        if other == key:
            continue
        try:
            cur = scheme_devices(other)
            if serial in cur:
                _write_scheme_devices(other, [x for x in cur if x != serial])
        except Exception:  # noqa: BLE001 - 清理失败不阻断主绑定
            continue

    cur = scheme_devices(key)
    if serial not in cur:
        cur.append(serial)
    if not _write_scheme_devices(key, cur):
        return False, "写入方案文件失败"
    return True, key


def unbind_device(key: str, serial: str) -> tuple[bool, str]:
    """从方案解绑设备（不存在时视为成功——幂等）"""
    key = str(key)
    serial = str(serial or "").strip()
    if not scheme_exists(key):
        return False, "仅方案文件可解绑"
    cur = scheme_devices(key)
    if serial not in cur:
        return True, key
    if not _write_scheme_devices(key, [x for x in cur if x != serial]):
        return False, "写入方案文件失败"
    return True, key


def get(path: str, default: Any = None) -> Any:
    """三层取值（dotted path）——``settings.json`` → 项目档案 → ``default``（内置兜底）

    ``default`` 由调用方给（通常是 ``core/config.py`` 的模块常量），
    这样"无档案、无配置"时返回的就是改造前那份字面量。
    """
    try:
        override = config_center.get(f"project.{path}", None)
    except Exception:  # noqa: BLE001
        override = None
    if isinstance(override, bool) or override not in (None, "", [], {}):
        return override
    return profile_value(path, default)
