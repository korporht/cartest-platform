# -*- coding: utf-8 -*-
"""30/31 验收 — 项目档案 ProjectProfile（值级定制点外提；三层取值）

覆盖：
  [1] 内置档案 == core/config.py 常量（PLT1 默认，无档案时行为与改造前一致）
  [2] 三层取值：内置兜底 → 项目档案 → settings.json 覆盖（后者优先）
  [3] 换项目=换 JSON：写 alt.json 并激活 → 包名/URI/关键词随之变（不改代码）
  [4] 消费方接通：monkey 默认包名 / adb 灌入 URI+组件 / logcat 关键词随档案
  [5] 健壮性：未知 path / 损坏 JSON / 未激活项目 → 回退内置，不抛

用法:
    python tests/test_project_profile.py
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
    print("30/31 验收 — 项目档案 ProjectProfile")
    print("=" * 70)

    from cartest.core import config, config_center, project

    tmp = Path(mkdtemp(prefix="profile_"))
    config_center.set_settings_file(tmp / "settings.json")
    project.set_projects_dir(tmp / "projects")          # 测试隔离（避免动生产档案）

    # ── [1] 内置档案 == 代码常量 ──
    b = project.builtin_profile()
    check("内置 profile key/name", b["key"] == project.BUILTIN_KEY and bool(b["name"]))
    check("内置 main_package == monkey 常量默认",
          project.get("device.main_package") in (b["device"]["main_package"],)
          and b["device"]["main_package"] == "com.example.carvoice")
    check("内置 inject_uri == config.VR_URI_BASE",
          b["voice"]["inject_uri"] == config.VR_URI_BASE, b["voice"]["inject_uri"])
    check("内置 voice_packages == config.VOICE_PACKAGES",
          b["device"]["voice_packages"] == list(config.VOICE_PACKAGES))
    check("内置 monitor_keywords == config.MONITOR_KEYWORDS",
          b["log"]["monitor_keywords"] == list(config.MONITOR_KEYWORDS))
    check("内置 fulllog_keywords == config.FULL_LOG_KEYWORDS",
          b["log"]["fulllog_keywords"] == list(config.FULL_LOG_KEYWORDS))
    check("内置 services 三段（语音引擎/场景引擎/VR 服务）",
          [s["label"] for s in b["device"]["services"]]
          == ["语音引擎", "场景引擎", "VR 服务"])

    # ── [2] 三层取值 ──
    check("无档案：get 回退内置", project.get("voice.inject_uri") == config.VR_URI_BASE)
    check("未知 path → default（不抛）",
          project.get("nothing.here", "fallback") == "fallback")

    alt = project.builtin_profile()
    alt.update({"key": "alt", "name": "替身车机"})
    alt["device"]["main_package"] = "com.example.alt"
    alt["voice"]["inject_uri"] = "vr://com.example.alt/interaction/getSemantic"
    alt["log"]["monitor_keywords"] = ["altkw"]
    check("save(alt) 成功", project.save("alt", alt) is True)
    check("档案文件已落盘", (project.projects_dir() / "alt.json").is_file())
    check("list_projects 含 builtin + alt",
          {p["key"] for p in project.list_projects()} >= {"plt1", "alt"})

    check("未激活时仍是内置值", project.get("device.main_package") == "com.example.carvoice")
    check("set_active(alt) 成功", project.set_active("alt") is True)
    check("激活后 main_package 随档案变",
          project.get("device.main_package") == "com.example.alt")
    check("激活后 inject_uri 随档案变",
          project.get("voice.inject_uri") == "vr://com.example.alt/interaction/getSemantic")
    check("激活后关键词随档案变", project.get("log.monitor_keywords") == ["altkw"])
    check("未覆盖字段仍取内置兜底（voice_packages）",
          project.get("device.voice_packages") == list(config.VOICE_PACKAGES))

    # settings 覆盖（最高优先）
    config_center.set("project.voice.inject_uri", "vr://override/inject")
    check("settings 覆盖档案", project.get("voice.inject_uri") == "vr://override/inject")
    check("settings 只覆盖该键（其它仍取档案）",
          project.get("device.main_package") == "com.example.alt")
    config_center.set("project.voice.inject_uri", None)

    # ── [4] 消费方接通 ──
    from cartest.app.tools import monkey_tab
    check("monkey 默认包名来自档案",
          monkey_tab.default_package() == "com.example.alt", monkey_tab.default_package())
    check("monkey DEFAULT_PKG 常量仍在（兜底，向后兼容）",
          monkey_tab.DEFAULT_PKG == "com.example.carvoice")

    from cartest.core import adb
    check("adb 灌入 URI 来自档案",
          adb.voice_uri_base() == "vr://com.example.alt/interaction/getSemantic")
    check("adb 语音包名来自档案（继承内置）",
          adb.voice_packages() == list(config.VOICE_PACKAGES))
    check("adb 语音服务组件来自档案",
          [s[0] for s in adb.service_starts()] == ["语音引擎", "场景引擎", "VR 服务"])

    from cartest.core import logcat
    check("logcat 实时关键词来自档案", logcat.monitor_keywords() == ["altkw"])
    check("logcat 全量关键词未覆盖 → 内置",
          logcat.fulllog_keywords() == list(config.FULL_LOG_KEYWORDS))

    # ── [6] 切项目清理项目相关工具偏好（真机验证发现的坑）──
    monkey_cfg = project.tools_dir() / "monkey_config.json"
    monkey_cfg.parent.mkdir(parents=True, exist_ok=True)
    monkey_cfg.write_text(json.dumps(
        {"monkey_pkg": "com.example.carvoice", "monkey_count": "200"}), encoding="utf-8")
    project.set_active("plt1")
    project.set_active("alt")                       # 切换 → 应清掉 monkey_pkg
    left = json.loads(monkey_cfg.read_text(encoding="utf-8"))
    check("切项目清理 monkey_pkg（旧值不再挡住档案）", "monkey_pkg" not in left, str(left))
    check("非项目相关偏好保留（monkey_count）", left.get("monkey_count") == "200")
    monkey_cfg.write_text(json.dumps(
        {"monkey_pkg": "com.example.carvoice"}), encoding="utf-8")
    project.set_active("alt")                       # 同项目再保存 → 不触发清理
    left2 = json.loads(monkey_cfg.read_text(encoding="utf-8"))
    check("未切换项目时不清偏好", left2.get("monkey_pkg") == "com.example.carvoice")
    check("reset_scoped_prefs 可直接调用（返回清理清单）",
          isinstance(project.reset_scoped_prefs(), list))

    # ── [5] 健壮性 ──
    (project.projects_dir() / "broken.json").write_text("{ not json", encoding="utf-8")
    project.set_active("broken")
    check("损坏档案 → 回退内置（不抛）",
          project.get("device.main_package") == "com.example.carvoice")
    project.set_active("nonexistent")
    check("不存在项目 → 回退内置",
          project.get("voice.inject_uri") == config.VR_URI_BASE)
    project.set_active("alt")

    # 存档格式：json 可解析
    if (project.projects_dir() / "alt.json").is_file():
        data = json.loads((project.projects_dir() / "alt.json").read_text(encoding="utf-8"))
        check("档案为可解析 JSON 且含 name", data.get("name") == "替身车机")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("项目档案检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
