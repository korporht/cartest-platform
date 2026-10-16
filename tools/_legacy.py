# -*- coding: utf-8 -*-
"""迁移验证脚本的公共定位器 — 找「旧实现」工具目录

背景（2026-09-21）：5 个 ``tools/verify_*.py`` 把旧工具目录**写死**为
``D:\\BaiduNetdiskDownload\\Tools\\<name>``，而该目录后来被移动到
``D:\\AgentProject\\Tools\\<name>`` → 迁移验证全部 ImportError（误报为代码回归）。

本模块按 **候选顺序** 解析，并支持环境变量覆盖::

    CARTEST_LEGACY_ROOT   # 自定义根目录（其下应有 <name> 目录）
    CARTEST_LEGACY_DIR    # 直接指定某个工具的目录（单项覆盖）

找不到时由 :func:`require` 打印 ``[SKIP]`` 说明并让调用方 ``sys.exit(0)``——
与 ``check_all.py`` 对缺失项的 SKIP 语义一致（不把"环境缺件"误报成"回归"）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 候选根目录（按顺序探测；可用 CARTEST_LEGACY_ROOT 覆盖/前置）
ROOTS: list[Path] = [
    Path(r"D:\AgentProject\Tools"),
    Path(r"D:\BaiduNetdiskDownload\Tools"),
    Path(r"D:\SoftWare\WorkScript\Script"),
]

SPECIFIC_ENV = "CARTEST_LEGACY_DIR"
ROOT_ENV = "CARTEST_LEGACY_ROOT"


def candidates(name: str) -> list[Path]:
    """候选目录列表（env 优先）"""
    out: list[Path] = []
    specific = os.environ.get(SPECIFIC_ENV)
    if specific:
        out.append(Path(specific))
    root = os.environ.get(ROOT_ENV)
    if root:
        out.append(Path(root) / name)
    out += [r / name for r in ROOTS]
    return out


def find(name: str) -> Path | None:
    """找到即返回目录，否则 ``None``"""
    for p in candidates(name):
        try:
            if p.is_dir():
                return p
        except OSError:
            continue
    return None


def require(name: str, *, marker: str = "") -> Path:
    """解析旧工具目录；缺失则打印说明并 ``sys.exit(0)``（SKIP，不误报）"""
    d = find(name)
    if d is None:
        print(f"[SKIP] 未找到旧实现目录「{name}」（迁移一致性验证跳过）")
        print("       候选：" + "，".join(str(p) for p in candidates(name)))
        print(f"       可设 {ROOT_ENV}=<根目录> 或 {SPECIFIC_ENV}=<该工具目录> 指定")
        sys.exit(0)
    if marker and not (d / marker).exists():
        print(f"[SKIP] {d} 下缺少 {marker}（旧实现不完整，迁移验证跳过）")
        sys.exit(0)
    return d
