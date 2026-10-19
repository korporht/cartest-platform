# -*- coding: utf-8 -*-
"""P3c 验收 — 路径列表编辑器（无 UI：控件构造 + 值收集 + 校验 + 增删）

用法:
    python tests/test_path_list_editor.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartest.app.tools.widgets.path_list_editor import PathListEditor  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("P3c 验收 — 路径列表编辑器（无 UI）")
    print("=" * 70)

    base = [
        {"path": "/la_log/vstgloballogcontrol/", "alias": "Android", "enabled": True},
        {"path": "/mnt/log/AIOS/", "alias": "AIOS", "enabled": False},
    ]

    print("\n[1] 初始往返一致")
    ed = PathListEditor(base)
    check("get_value == 输入", ed.get_value() == base)
    check("初始校验通过", ed.validate() is None)

    print("\n[2] 启用项空路径 → 校验失败")
    ed._rows[0]["path"].value = "   "          # 模拟用户清空（项目测试惯例：允许访问私有成员）
    check("校验报第 1 行", ed.validate() == "第 1 行：启用状态下路径不能为空", str(ed.validate()))
    ed._rows[0]["enabled"].value = False       # 关掉启用 → 允许为空
    check("关闭启用后校验通过", ed.validate() is None)

    print("\n[3] 添加行（默认启用）→ 新行空路径应拦截")
    ed._on_add()
    check("行数 +1", len(ed.get_value()) == 3)
    check("新增启用行触发校验", ed.validate() == "第 3 行：启用状态下路径不能为空")

    print("\n[4] 删除行")
    ed._on_remove(ed._rows[2])
    check("行数还原", len(ed.get_value()) == 2)

    print("\n[5] set_value 整体替换（恢复默认路径的场景）")
    ed.set_value(base)
    check("替换后值一致", ed.get_value() == base)

    print("\n[6] on_change 回调（2026-10-15 实时保存：开关/增删行＝点击类改动）")
    hits: list = []
    ed2 = PathListEditor(base, on_change=lambda: hits.append(1))
    ed2.build()
    ed2._rows[0]["enabled"].value = False
    ed2._rows[0]["enabled"].on_change(None)          # 模拟点开关
    check("开关变更触发回调", len(hits) == 1, str(len(hits)))
    ed2._on_add()
    check("添加行触发回调", len(hits) == 2, str(len(hits)))
    ed2._on_remove(ed2._rows[0])
    check("移除行触发回调", len(hits) == 3, str(len(hits)))
    check("未传回调时不报错（向后兼容）",
          PathListEditor(base).build() is not None)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("路径列表编辑器测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
