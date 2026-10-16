# -*- coding: utf-8 -*-
"""输入框同步护栏 — 所有 ``ft.TextField`` 必须带 ``on_change``（发现 #14）

背景（2026-09-18 用户反馈）：
    flet 0.85 Web 端**只在控件有 ``on_change`` 时**才把用户输入回传服务端。
    没有回调的 ``TextField``，服务端 ``control.value`` 永远是构建时的旧值——
    表现：APK 路径框里明明有字，点「安装/推送」却提示"请先填写/选择路径"；
    间隔/超时/用例文件等输入同理（静默用了默认值）。

护栏做法：静态 AST 扫描 ``cartest/app`` 下所有 **字面量构造** 的
``ft.TextField(...)``，缺少 ``on_change=`` 即判失败。统一挂
``cartest.app.ui.components.sync_text_value``（值由 flet 写入 ``control.value``）。

已知局限：变量拼接/动态生成的输入框无法静态判定（当前无此用法）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "cartest" / "app"


def _is_textfield(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TextField")


def main() -> int:
    print("=" * 70)
    print("输入框同步护栏 — 扫描 ft.TextField 是否带 on_change")
    print("=" * 70)

    violations: list[str] = []
    scanned = 0
    found = 0
    for pyf in sorted(TARGET.rglob("*.py")):
        if "__pycache__" in pyf.parts:
            continue
        scanned += 1
        try:
            tree = ast.parse(pyf.read_text(encoding="utf-8"), filename=str(pyf))
        except SyntaxError as ex:
            violations.append(f"{pyf.name}: 语法错误 {ex}")
            continue
        for node in ast.walk(tree):
            if not _is_textfield(node):
                continue
            found += 1
            if not any(kw.arg == "on_change" for kw in node.keywords):
                rel = pyf.relative_to(ROOT)
                violations.append(
                    f"{rel}:{node.lineno} TextField 缺 on_change="
                    f"（服务端读不到用户输入；挂 components.sync_text_value）")

    print(f"扫描文件: {scanned} 个；TextField: {found} 个")
    if violations:
        print(f"\n发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"  [FAIL] {v}")
        return 1
    print("[PASS] 所有 TextField 均已挂 on_change（用户输入可达服务端）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
