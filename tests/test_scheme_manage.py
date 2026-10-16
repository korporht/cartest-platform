# -*- coding: utf-8 -*-
"""32 验收 — 配置方案管理（原「项目档案」自定义增/删/改；上限 5 组）

设计依据：``32-配置方案管理设计.md``（用户 2026-09-22 裁决；2026-10-16 上限 3 → 5）。
核心变化：
  · 方案 = ``data/projects/<key>.json``，**最多 5 个、全部可删**；无方案 → 回退内置常量
  · 新增 = **空白方案**（不写任何值）；重命名只改 name（key 不变）
  · 删除激活方案 → **自动切到剩余第一个**（都没了 → 回内置默认）
  · 保存 = 把 ``project.*`` 值**写进当前方案文件**（只存与内置默认不同的差异），
    并**清掉 settings.json 里残留的 project.* 覆盖**（一次性吸收迁移）

覆盖：
  [1] 生命周期：create（空白/额度/重名/空名）/ rename（key 不变）/ remove（自动切/回内置）
  [2] save_values：差异存档（等于内置默认的键不落盘）+ 吸收并清除 settings 覆盖
  [3] 回退内置态保存 → 自动物化成方案（不是静默丢失）
  [4] 配置页卡片：标题「配置方案」+ 新增/重命名/删除按钮 + 下拉只列方案

用法:
    python tests/test_scheme_manage.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("32 验收 — 配置方案管理（增/删/改 + 上限 5 组）")
    print("=" * 70)

    from cartest.core import config_center, project

    tmp = Path(mkdtemp(prefix="scheme_"))
    settings = tmp / "settings.json"
    config_center.set_settings_file(settings)
    project.set_projects_dir(tmp / "projects")

    # ── [1] 生命周期 ──
    check("初始 0 个方案（回退内置态）", project.scheme_count() == 0,
          str(project.list_schemes()))
    check("上限常量 = 5", project.MAX_SCHEMES == 5)
    check("内置回退名", project.DEFAULT_LABEL == "默认（内置）")

    ok1, k1 = project.create_scheme("方案甲")
    check("新增方案甲（空白）", ok1 and k1 == "s1", f"{ok1} {k1}")
    f1 = tmp / "projects" / "s1.json"
    raw1 = json.loads(f1.read_text(encoding="utf-8"))
    check("空白方案只写 key/name（不含任何值）",
          set(raw1) == {"key", "name"} and raw1["name"] == "方案甲", str(raw1))

    check("空名被拒", project.create_scheme("   ")[0] is False)
    check("重名被拒", project.create_scheme("方案甲")[0] is False)

    ok2, k2 = project.create_scheme("方案乙")
    ok3, k3 = project.create_scheme("方案丙")
    ok4, k4 = project.create_scheme("方案丁")
    ok5, k5 = project.create_scheme("方案戊")
    check("建满 5 组（s1..s5）",
          ok2 and ok3 and ok4 and ok5
          and {k1, k2, k3, k4, k5} == {"s1", "s2", "s3", "s4", "s5"},
          f"{k1} {k2} {k3} {k4} {k5}")
    ok6, why6 = project.create_scheme("方案己")
    check("第 6 组被拒（额度）", ok6 is False and "5" in why6, why6)

    okr, msg = project.rename_scheme(k2, "方案乙改")
    names = {p["key"]: p["name"] for p in project.list_schemes()}
    check("重命名生效且 key 不变", okr and names.get("s2") == "方案乙改", str(names))
    check("重名重命名被拒", project.rename_scheme(k2, "方案甲")[0] is False)

    # 删除激活方案 → 自动切剩余
    project.set_active(k2)
    check("激活 s2", project.active_key() == "s2")
    okd, nxt = project.remove_scheme(k2)
    check("删除激活方案 → 自动切到剩余第一个",
          okd and nxt == "s1" and project.active_key() == "s1",
          f"{okd} nxt={nxt} active={project.active_key()}")
    check("删除后只剩 4 个", project.scheme_count() == 4)

    # 逐个删光（每次删激活方案 → 自动切剩余第一个）
    project.remove_scheme(k1)
    project.remove_scheme(k3)
    project.remove_scheme(k4)
    project.set_active(k5)
    okd3, nxt3 = project.remove_scheme(k5)
    check("删光 → 回内置默认键", okd3 and nxt3 == project.BUILTIN_KEY
          and project.scheme_count() == 0, f"nxt={nxt3}")
    check("删除不存在的方案 → 失败", project.remove_scheme("nope")[0] is False)

    # ── [2] save_values：差异存档 + 吸收 settings 覆盖 ──
    okn, kk = project.create_scheme("差异甲")
    project.set_active(kk)
    b = project.builtin_profile()
    default_pkg = b["device"]["main_package"]
    flat = {
        "project.device.main_package": "com.custom.app",       # 差异 → 落盘
        "project.voice.inject_uri": b["voice"]["inject_uri"],  # == 内置 → 不落盘
        "project.voice.inject_mode": "",                       # 空 → 不落盘
        "log.keep": "ignore-me",                               # 非 project.* → 忽略
    }
    oks, key_used = project.save_values(flat)
    saved = json.loads((tmp / "projects" / f"{kk}.json").read_text(encoding="utf-8"))
    check("save_values 成功", oks and key_used == kk, f"{oks} {key_used}")
    check("只存差异（custom 包名落盘）",
          saved.get("device", {}).get("main_package") == "com.custom.app", str(saved))
    check("等于内置默认的键不落盘（inject_uri 不在文件里）",
          "voice" not in saved or "inject_uri" not in saved.get("voice", {}), str(saved))
    check("空值不落盘（inject_mode 不在文件里）",
          "voice" not in saved or "inject_mode" not in saved.get("voice", {}), str(saved))
    check("读取链生效（get 拿到方案里的值）",
          project.get("device.main_package") == "com.custom.app",
          str(project.get("device.main_package")))

    # 旧覆盖吸收：先人为写一个 settings 覆盖，再保存 → 应被清掉
    config_center.set("project.voice.inject_uri", "vr://override/x")
    check("覆盖已写入 settings", config_center.get("project.voice.inject_uri") == "vr://override/x")
    project.save_values({"project.voice.inject_uri": "vr://override/x"})
    disk = json.loads(settings.read_text(encoding="utf-8"))
    saved2 = json.loads((tmp / "projects" / f"{kk}.json").read_text(encoding="utf-8"))
    check("settings 里的 project.* 覆盖键被清除（不留 null）",
          "project.voice.inject_uri" not in disk,
          str([k for k in disk if k.startswith("project.")]))
    check("值已落到方案文件", saved2["voice"]["inject_uri"] == "vr://override/x", str(saved2))
    check("清覆盖后读取仍正确（走方案文件）",
          config_center.get("project.voice.inject_uri") == "vr://override/x")

    # 保存回默认值 → 从方案里删掉（回到"跟随/内置默认"）
    project.save_values({"project.voice.inject_uri": b["voice"]["inject_uri"]})
    saved3 = json.loads((tmp / "projects" / f"{kk}.json").read_text(encoding="utf-8"))
    check("保存回内置默认 → 该 path 从方案移除",
          "voice" not in saved3 or "inject_uri" not in saved3.get("voice", {}), str(saved3))

    # ── [3] 回退内置态保存 → 物化为方案 ──
    project.remove_scheme(kk)
    check("已删光（回退内置态）", project.scheme_count() == 0)
    okz, kz = project.save_values({"project.device.main_package": "com.z"})
    check("回退态保存 → 自动物化成方案（不静默丢失）",
          okz and project.scheme_count() == 1 and project.active_key() == kz
          and project.get("device.main_package") == "com.z", f"{okz} {kz}")

    # ── [4] 配置页卡片 ──
    src = (ROOT / "cartest" / "app" / "pages" / "settings.py").read_text(encoding="utf-8")
    check("卡片标题改为「配置方案」（旧「项目档案」卡已不在 UI）",
          'ft.Text("配置方案"' in src and 'ft.Text("项目档案"' not in src)
    check("三个管理按钮（新增/重命名/删除）",
          all(x in src for x in ("新增方案", "重命名", "删除方案")), "")
    check("下拉只列方案（list_schemes）", "list_schemes" in src)
    check("保存走 save_values（值写入方案）", "save_values" in src)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("配置方案管理检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
