# -*- coding: utf-8 -*-
"""测试临时目录护栏 — 禁止裸 ``tempfile.mkdtemp``（防 %TEMP% 累积回归）

背景（2026-09-23）：
    仓库里二十二个测试曾用裸 ``tempfile.mkdtemp()`` 建临时目录却从不删除，每跑一次
    ``tools/check_all.py`` 就往 ``%TEMP%`` 里累积一批 —— 实测堆积 **128 个目录 /
    14.68 MB**。已统一改为 ``tests/_tmpdir.py`` 的 ``mkdtemp``（``atexit`` 回收）。

护栏做法：静态 AST 扫描 ``tests`` 下所有 ``*.py``，命中即为回归：

  ① ``tempfile.mkdtemp(...)``        —— 绕过回收的裸调用（含 ``_tempfile`` 别名）
  ② ``from tempfile import mkdtemp`` —— 同上，换个写法绕过

另核对两条：
  · 调用 ``mkdtemp(...)`` 的文件必须 ``from _tmpdir import mkdtemp``（防漏导入/自造同名函数）
  · ``tests/_tmpdir.py``（唯一被许可直接调 stdlib 的实现处）必须存在

已知局限：``getattr(tempfile, "mkde" + "mptemp")`` 这类动态构造无法静态判定
（当前无此用法）；``with tempfile.TemporaryDirectory()`` 本就会回收，**不在禁止之列**。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests"
#: 唯一允许直接调 stdlib mkdtemp 的文件（回收逻辑就在它里面）
SANCTIONED = TARGET / "_tmpdir.py"
HELPER_IMPORT = "from _tmpdir import mkdtemp"
#: 被视为"裸调 stdlib"的属性基名（_tempfile 是别名写法）
STDLIB_ALIASES = ("tempfile", "_tempfile")


def _bare_stdlib(node: ast.AST) -> str | None:
    """命中裸用 stdlib mkdtemp 则返回违规描述，否则 None"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        base = node.func.value
        if (node.func.attr == "mkdtemp" and isinstance(base, ast.Name)
                and base.id in STDLIB_ALIASES):
            return f"{base.id}.mkdtemp(...)"
    if isinstance(node, ast.ImportFrom) and node.module == "tempfile":
        if any(a.name == "mkdtemp" for a in node.names):
            return "from tempfile import mkdtemp"
    return None


def _calls_bare_mkdtemp(node: ast.AST) -> bool:
    """是否为 ``mkdtemp(...)`` 调用（名字形态，即应来自 _tmpdir）"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "mkdtemp")


def main() -> int:
    print("=" * 70)
    print("测试临时目录护栏 — 扫描 tests/ 是否裸用 tempfile.mkdtemp")
    print("=" * 70)

    violations: list[str] = []
    if not SANCTIONED.is_file():
        violations.append(f"缺少 {SANCTIONED.relative_to(ROOT)}（回收实现处不存在）")

    scanned = helper_users = 0
    for pyf in sorted(TARGET.rglob("*.py")):
        if "__pycache__" in pyf.parts:
            continue
        scanned += 1
        src = pyf.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(pyf))
        except SyntaxError as ex:
            violations.append(f"{pyf.name}: 语法错误 {ex}")
            continue

        rel = pyf.relative_to(ROOT)
        if pyf == SANCTIONED:
            continue                      # 实现处本身要用 stdlib，豁免

        for node in ast.walk(tree):
            bad = _bare_stdlib(node)
            if bad:
                violations.append(
                    f"{rel}:{node.lineno} {bad}"
                    f"（绕过回收 → %TEMP% 累积；改用 tests/_tmpdir.mkdtemp）")
            if _calls_bare_mkdtemp(node):
                helper_users += 1
        if "mkdtemp(" in src and HELPER_IMPORT not in src:
            violations.append(f"{rel}: 用了 mkdtemp( 但缺 `{HELPER_IMPORT}`")

    print(f"扫描文件: {scanned} 个；使用 _tmpdir.mkdtemp 的调用点: {helper_users} 个")
    if violations:
        print(f"\n发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"  [FAIL] {v}")
        return 1
    print("[PASS] tests/ 无裸用 tempfile.mkdtemp（临时目录均走 _tmpdir 回收）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
