# -*- coding: utf-8 -*-
"""P3b/UI-1 布局护栏 — 禁止 wrap 行/列内出现 expand 子项

背景（P3b-5a 实测，2026-09-16）：
    ``ft.Row(..., wrap=True)`` 的控件列表里若出现 ``ft.Container(expand=True)``，
    Flutter 侧布局断言失败（Wrap 不支持 flex/Expanded 子项）。release 模式下
    表现为**整块灰色 ErrorWidget**，且连带其后兄弟控件（状态行、日志面板）一起
    不渲染——静默、无控制台报错。旧版 ``apk_tab`` 快捷行即中招（已修复）。

本测试做静态 AST 扫描（UI 目录：``cartest/app/tools`` 与 ``cartest/app/ui``——
UI-1 起扩展覆盖）：
    凡字面量构造 ``ft.Row([...], wrap=True)`` 且列表内直接出现
    ``ft.Container(expand=<真值>)`` 即判失败。

已知局限：
    变量拼接/动态生成的 controls 列表无法静态判定；
    如需更强覆盖，应在运行时构造控件树遍历（当前不做）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_DIRS = [
    ROOT / "cartest" / "app" / "tools",
    ROOT / "cartest" / "app" / "ui",
]


def _is_attr_call(node: ast.AST, obj: str, attr: str) -> bool:
    """判断是否为 ``<obj>.<attr>(...)`` 调用（如 ft.Row(...)）"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == obj
    )


def _expr_is_truthy(node: ast.AST) -> bool:
    """expand=<常量> 是否为真值（排除 False/None/0/''）"""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    return True  # 非常量（变量/表达式）一律视为风险


def _find_wrap_rows(tree: ast.AST) -> list[ast.Call]:
    hits = []
    for node in ast.walk(tree):
        if not _is_attr_call(node, "ft", "Row"):
            continue
        for kw in node.keywords:
            if kw.arg == "wrap" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                hits.append(node)
                break
    return hits


def _controls_list(row_call: ast.Call) -> list[ast.AST] | None:
    """取出 Row 调用的控件列表（位置参数或 controls= 关键字）"""
    if row_call.args and isinstance(row_call.args[0], ast.List):
        return row_call.args[0].elts
    for kw in row_call.keywords:
        if kw.arg == "controls" and isinstance(kw.value, ast.List):
            return kw.value.elts
    return None


def _find_expand_children(elts: list[ast.AST]) -> list[ast.Call]:
    bad = []
    for el in elts:
        if not _is_attr_call(el, "ft", "Container"):
            continue
        for kw in el.keywords:
            if kw.arg == "expand" and _expr_is_truthy(kw.value):
                bad.append(el)
    return bad


def main() -> int:
    print("布局护栏 — 扫描 wrap=True 行内的 expand 子项")

    violations: list[str] = []
    scanned = 0
    for base in UI_DIRS:
        if not base.is_dir():
            continue
        print(f"目录: {base}")
        for pyf in sorted(base.rglob("*.py")):
            if "__pycache__" in pyf.parts:
                continue
            scanned += 1
            try:
                tree = ast.parse(pyf.read_text(encoding="utf-8"), filename=str(pyf))
            except SyntaxError as ex:
                violations.append(f"{pyf.name}: 语法错误 {ex}")
                continue
            for row_call in _find_wrap_rows(tree):
                elts = _controls_list(row_call)
                if not elts:
                    continue
                for node in _find_expand_children(elts):
                    violations.append(
                        f"{pyf.name}:{node.lineno} wrap=True 的 Row 内出现 "
                        f"ft.Container(expand=...)，会导致整页灰色渲染（Flutter Wrap 不支持 flex 子项）"
                    )

    print(f"扫描文件: {scanned} 个")
    if violations:
        print(f"\n发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"  [FAIL] {v}")
        return 1

    print("[PASS] 未发现 wrap 行内含 expand 子项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
