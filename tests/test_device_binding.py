# -*- coding: utf-8 -*-
"""设备绑定验收（2026-10-19 用户诉求）

「配置自定义绑定设备，连接/切换到对应设备时能自动切换配置」：
方案档案 ``device.serials``（列表，一方案多台架）↔ 设备序列号；配置页可绑定/解绑；
设备切换/启动恢复/新设备接入三处触发自动切方案。

覆盖：
  [1] 绑定/反查/一方案多设备（``scheme_devices`` / ``scheme_for_device``）
  [2] 唯一性：同一设备再绑到另一方案 → 自动从旧方案移除
  [3] 兼容单值 ``device.serial``（旧档案手写值也能识别）
  [4] 解绑（幂等 + 兼容字段同步）/ 内置方案不可绑定
  [5] 别名匹配扩展到多设备（``alias_for_serial``）
  [6] 源码接线：配置页绑定入口 / main 三处触发 / 设备接入轮询 / 防抢切

用法:
    python tests/test_device_binding.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def _read(key: str) -> dict:
    from cartest.core import project
    return json.loads((project.projects_dir() / f"{key}.json").read_text(encoding="utf-8"))


def main() -> int:
    print("=" * 70)
    print("设备绑定验收 — 方案 ↔ 设备（自动切换配置）")
    print("=" * 70)

    from cartest.core import config_center, project

    tmp = Path(mkdtemp(prefix="bind_"))
    config_center.set_settings_file(tmp / "settings.json")
    project.set_projects_dir(tmp / "projects")

    # ── [1] 建方案 + 绑定/反查 ──
    ok1, k1 = project.create_scheme("台架A")
    ok2, k2 = project.create_scheme("台架B")
    check("新建两个方案", ok1 and ok2 and k1 != k2, f"{k1}/{k2}")

    done, msg = project.bind_device(k1, "SER-A")
    check("绑定设备成功", done is True and msg == k1, f"{done}/{msg}")
    check("scheme_devices 读到绑定", project.scheme_devices(k1) == ["SER-A"],
          str(project.scheme_devices(k1)))
    check("scheme_for_device 反查方案", project.scheme_for_device("SER-A") == k1,
          project.scheme_for_device("SER-A"))
    check("未知设备 → 空串", project.scheme_for_device("NOPE") == "")
    check("空 serial → 空串", project.scheme_for_device("") == "")
    check("档案写 serials + 兼容 serial",
          _read(k1).get("device", {}).get("serials") == ["SER-A"]
          and _read(k1).get("device", {}).get("serial") == "SER-A",
          str(_read(k1).get("device")))

    project.bind_device(k1, "SER-A2")
    check("一方案可绑多台设备", project.scheme_devices(k1) == ["SER-A", "SER-A2"],
          str(project.scheme_devices(k1)))

    # ── [2] 唯一性：设备改绑另一方案 → 从旧方案移除 ──
    project.bind_device(k2, "SER-A")
    check("唯一性：旧方案自动移除该设备",
          "SER-A" not in project.scheme_devices(k1)
          and "SER-A" in project.scheme_devices(k2),
          f"k1={project.scheme_devices(k1)} k2={project.scheme_devices(k2)}")
    check("唯一性：反查指向新方案", project.scheme_for_device("SER-A") == k2)

    # ── [3] 兼容单值 device.serial（旧档案/手写值）──
    data = _read(k1)
    data.setdefault("device", {})["serial"] = "LEGACY-1"
    project.save(k1, data)
    check("兼容单值 device.serial（读取）",
          "LEGACY-1" in project.scheme_devices(k1),
          str(project.scheme_devices(k1)))
    check("兼容单值反查方案", project.scheme_for_device("LEGACY-1") == k1)

    # ── [4] 解绑 / 内置拒绝 ──
    project.bind_device(k2, "SER-B")
    done, _ = project.unbind_device(k2, "SER-A")
    check("解绑成功", done is True and project.scheme_for_device("SER-A") == "")
    check("解绑幂等（重复解绑仍成功）",
          project.unbind_device(k2, "SER-A")[0] is True)
    check("解绑后兼容 serial 同步更新",
          _read(k2).get("device", {}).get("serial") == "SER-B",
          str(_read(k2).get("device")))
    done, msg = project.bind_device(project.BUILTIN_KEY, "X-1")
    check("内置方案不可绑定", done is False and "内置" in msg, msg)

    # ── [5] 别名匹配扩展到多设备 ──
    data = _read(k1)
    data.setdefault("device", {})["alias"] = "A台架"
    project.save(k1, data)
    check("多设备别名匹配（非首项）",
          project.alias_for_serial("SER-A2") == "A台架"
          or project.alias_for_serial("LEGACY-1") == "A台架",
          f"{project.alias_for_serial('SER-A2')}/{project.alias_for_serial('LEGACY-1')}")

    # ── [6] 源码接线 ──
    settings_src = (ROOT / "cartest" / "app" / "pages" / "settings.py").read_text(
        encoding="utf-8")
    main_src = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")
    check("配置页：绑定设备入口（行 + 对话框 + 手动序列号）",
          "绑定设备…" in settings_src and "def _open_bind_dialog" in settings_src
          and "手动输入设备序列号" in settings_src
          and "def _toggle_bind" in settings_src)
    check("配置页：绑定行渲染进方案卡",
          "_binding_row()" in settings_src)
    check("main：三处触发自动切方案（手动切换/启动恢复/设备接入）",
          main_src.count("_auto_scheme_for_device(") >= 3,
          str(main_src.count("_auto_scheme_for_device(")))
    check("main：设备接入轮询（后台线程 + 5s）",
          "def _device_poll_loop" in main_src
          and "Thread(target=_device_poll_loop" in main_src
          and "time.sleep(5.0)" in main_src)
    check("main：防抢切（当前设备属于方案时不联动设备）",
          "state.device in project_mod.scheme_devices(key)" in main_src)
    check("main：自动切换写操作日志",
          "自动切换方案" in main_src)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("设备绑定检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
