# -*- coding: utf-8 -*-
"""CarTest core — 外部工具目录定位（候选解析 + 可配置）

**背景**（2026-09-21）：外部工具原先写死在 ``D:\\BaiduNetdiskDownload\\Tools\\<name>``，
但该目录被移动到了 ``D:\\AgentProject\\Tools\\<name>`` → 「离线提取」与「Monkey 压测」
直接不可用（找不到工具目录）。这与 ``tools/_legacy.py`` 是同一类脆弱点，故统一收敛：

解析顺序（先命中先用）::

    ① 环境变量（单项覆盖）   CARTEST_<TOOLKEY>_DIR   （保留 CARTEST_LOGEXTRACT_DIR 兼容）
    ② settings.json 配置项   tools.<tool_key>        （配置页可编辑，留空＝自动探测）
    ③ 候选根目录 + 工具名    CARTEST_TOOLS_ROOT ▸ D:\\AgentProject\\Tools
                             ▸ D:\\BaiduNetdiskDownload\\Tools ▸ D:\\SoftWare\\WorkScript\\Script

用法::

    from cartest.core import tools_locator
    d = tools_locator.resolve("cbm_logextractor", "CbmLogextractor")
    if d is None: ...            # 调用方给出"不可用"的显式提示（不静默）
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config_center

#: 候选根目录（按顺序探测；CARTEST_TOOLS_ROOT 会插到最前）
ROOTS: list[Path] = [
    Path(r"D:\AgentProject\Tools"),
    Path(r"D:\BaiduNetdiskDownload\Tools"),
    Path(r"D:\SoftWare\WorkScript\Script"),
]

#: 工具键 → 环境变量名（①）/ 配置键（②）
#: 例：tool_key="cbm_logextractor" → CARTEST_CBM_LOGEXTRACTOR_DIR / tools.cbm_logextractor_dir
ENV_TEMPLATE = "CARTEST_{key}_DIR"
CONFIG_TEMPLATE = "tools.{key}_dir"


def _env_names(tool_key: str, legacy_env: str | None) -> list[str]:
    names = [ENV_TEMPLATE.format(key=tool_key.upper())]
    if legacy_env:
        names.append(legacy_env)
    return names


def candidates(tool_key: str, dir_name: str, legacy_env: str | None = None) -> list[Path]:
    """候选目录列表（env → 配置 → 候选根），按优先级排序（不含过滤）"""
    out: list[Path] = []
    for env in _env_names(tool_key, legacy_env):
        raw = os.environ.get(env)
        if raw:
            out.append(Path(raw))
    try:
        cfg = config_center.get(CONFIG_TEMPLATE.format(key=tool_key), None)
        if isinstance(cfg, str) and cfg.strip():
            out.append(Path(cfg.strip()))
    except Exception:  # noqa: BLE001 - 配置不可读时退回候选根
        pass
    roots = list(ROOTS)
    env_root = os.environ.get("CARTEST_TOOLS_ROOT")
    if env_root:
        roots.insert(0, Path(env_root))
    out += [r / dir_name for r in roots]
    return out


def resolve(tool_key: str, dir_name: str, legacy_env: str | None = None,
            *, marker: str = "") -> Path | None:
    """解析外部工具目录；找到即返回，否则 ``None``

    **优先级与"权威性"**（2026-09-21 定，避免歧义）：

      ① ``环境变量`` / ② ``配置项`` —— 用户**显式指定** → **权威**：直接采用，
         不做 marker 过滤、也不继续探测（指错了会得到明确的"不可用"报错，
         而不是静默跑到别的目录上）
      ③ 候选根目录 —— **自动探测**：要求目录存在且（若给了 ``marker``）入口文件存在，
         避免"同名空目录"被误认为可用
    """
    for p in _explicit(tool_key, legacy_env):
        return p
    for p in _roots_probe(dir_name):
        try:
            if not p.is_dir():
                continue
            if marker and not (p / marker).exists():
                continue
            return p
        except OSError:
            continue
    return None


def _explicit(tool_key: str, legacy_env: str | None) -> list[Path]:
    """显式指定（env → 配置）——权威，不做存在性/入口过滤"""
    out: list[Path] = []
    for env in _env_names(tool_key, legacy_env):
        raw = os.environ.get(env)
        if raw:
            out.append(Path(raw))
            return out
    try:
        cfg = config_center.get(CONFIG_TEMPLATE.format(key=tool_key), None)
        if isinstance(cfg, str) and cfg.strip():
            out.append(Path(cfg.strip()))
    except Exception:  # noqa: BLE001 - 配置不可读时退回自动探测
        pass
    return out


def _roots_probe(dir_name: str) -> list[Path]:
    """候选根目录拼接（自动探测用）"""
    roots = list(ROOTS)
    env_root = os.environ.get("CARTEST_TOOLS_ROOT")
    if env_root:
        roots.insert(0, Path(env_root))
    return [r / dir_name for r in roots]


def candidates(tool_key: str, dir_name: str, legacy_env: str | None = None) -> list[Path]:
    """候选目录列表（显式指定在前，其后为候选根；用于报错文案与解释顺序）"""
    return _explicit(tool_key, legacy_env) + _roots_probe(dir_name)


def describe(tool_key: str, dir_name: str, legacy_env: str | None = None) -> str:
    """给用户看的"没找到"说明（列出候选与覆盖方式），便于直接粘进日志/状态行"""
    cands = "，".join(str(p) for p in candidates(tool_key, dir_name, legacy_env))
    envs = "/".join(_env_names(tool_key, legacy_env))
    return (f"未找到「{dir_name}」；候选：{cands}。"
            f"可用环境变量 {envs} 或配置项 {CONFIG_TEMPLATE.format(key=tool_key)} 指定")
