# -*- coding: utf-8 -*-
"""P3c 验收 — 配置中心：默认值对照 / 方案值往返 / primary_pull_path / 恢复默认

对照对象（旧硬编码值）：
  · core.config.LOG_REMOTE_DIR
  · app/tools/constants.REMOTE_LOG_PATH
  · adb_service.check_log_size() 内联的三条路径（本测试直接比对字符串）

2026-10-12 用户裁决：日志路径从 **settings 覆盖键**（``log.pull_paths``）改为
**方案值**（``project.log.pull_paths`` / ``project.log.size_paths``）——保存写进方案
文件，切换方案自然跟随（原先切方案会被清掉，用户感知为"保存的设置失效了"）。
老 settings 键仍作为遗留兜底被 :func:`effective_paths` 认，切一次方案即迁移完成。

用法:
    python tests/test_config_center.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core import config as core_config          # noqa: E402
from cartest.core import config_center                  # noqa: E402
from cartest.core import project as core_project        # noqa: E402
from cartest.app.tools import constants as tool_const   # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    tmp = Path(mkdtemp(prefix="p3c_center_"))
    cfg = tmp / "settings.json"
    config_center.set_settings_file(cfg)          # 隔离：不触碰真实 settings.json
    proj_root = tmp / "projects"
    proj_root.mkdir(exist_ok=True)
    core_project.set_projects_dir(proj_root)      # 隔离：不触碰真实 data/projects

    print("=" * 70)
    print("P3c 验收 — 配置中心")
    print(f"临时配置: {cfg}")
    print("=" * 70)

    print("\n[1] 内置默认值与旧硬编码逐字节对照")
    pull = config_center.get("project.log.pull_paths")
    check("公开版：内置默认 pull_paths 清空", pull == [], repr(pull))
    size = config_center.get("project.log.size_paths")
    check("公开版：内置默认 size_paths 清空", size == [], repr(size))
    check("两处默认不许漂移（内置档案 ⇄ config_center 常量）",
          core_project.builtin_value("log.pull_paths") == config_center._DEFAULT_PULL_PATHS
          and core_project.builtin_value("log.size_paths") == config_center._DEFAULT_SIZE_PATHS)

    check("公开版：内置默认 pull_paths 清空（深拷贝语义由方案值往返段覆盖）",
          config_center.get("project.log.pull_paths") == [])

    print("\n[2] 方案值往返（保存 → 写方案文件；不再走 settings）")
    modified = [
        {"path": "/custom/path/", "alias": "Custom", "enabled": True},
        {"path": "/mnt/log/sys1/", "alias": "SYS1", "enabled": True},
    ]
    ok, used = core_project.save_values({"project.log.pull_paths": modified})
    check("save_values 成功", ok is True, str(used))
    pf = proj_root / f"{used}.json"
    check("写进方案文件",
          pf.is_file()
          and json.loads(pf.read_text(encoding="utf-8"))["log"]["pull_paths"] == modified,
          str(pf))
    check("get 读到方案值", config_center.get("project.log.pull_paths") == modified)
    check("settings.json 不参与（该键未落盘）",
          config_center.stored("project.log.pull_paths", None) is None)

    print("\n[3] primary_pull_path")
    check("取第一条启用项", config_center.primary_pull_path() == "/custom/path/")
    core_project.save_values({"project.log.pull_paths": [
        {"path": "/p1/", "alias": "a", "enabled": False},
        {"path": "/p2/", "alias": "b", "enabled": True},
    ]})
    check("跳过未启用项", config_center.primary_pull_path() == "/p2/")
    core_project.save_values(
        {"project.log.pull_paths": [{"path": "  ", "alias": "x", "enabled": True}]})
    check("空白路径视为无 → 回退 LOG_REMOTE_DIR",
          config_center.primary_pull_path() == core_config.LOG_REMOTE_DIR)
    core_project.save_values({"project.log.pull_paths": []})
    check("空列表 → 回退 LOG_REMOTE_DIR",
          config_center.primary_pull_path() == core_config.LOG_REMOTE_DIR)

    print("\n[4] 恢复默认（从方案里删该 path → 回落到内置档案）")
    core_project.save_values({"project.log.pull_paths": modified})
    check("先写入自定义", config_center.get("project.log.pull_paths") == modified)
    core_project.save_values({"project.log.pull_paths": None})
    check("恢复默认后 == 内置默认",
          config_center.get("project.log.pull_paths") == config_center._DEFAULT_PULL_PATHS)

    print("\n[5] effective_paths 取值链（遗留 settings → 方案/档案 → 内置）")
    core_project.save("plt2x", {
        "key": "plt2x", "name": "PLT2 测试台架",
        "log": {
            "pull_paths": [{"path": "/log/android/logcat/", "alias": "logcat", "enabled": True}],
            "size_paths": [{"path": "/log/android/logcat/", "alias": "logcat", "enabled": True}],
        },
    })
    core_project.set_active("plt2x")
    check("档案提供抓取路径",
          config_center.effective_paths("pull")[0]["path"] == "/log/android/logcat/")
    check("档案提供大小检查路径",
          config_center.effective_paths("size")[0]["path"] == "/log/android/logcat/")
    check("primary_pull_path 跟随档案",
          config_center.primary_pull_path() == "/log/android/logcat/")

    config_center.set("log.pull_paths", [{"path": "/legacy/", "alias": "e", "enabled": True}])
    check("遗留 settings 键不参与取值（只认方案，避免显示/生效不一致）",
          config_center.effective_paths("pull")[0]["path"] == "/log/android/logcat/")
    core_project.set_active("plt1")
    check("切方案后遗留键被清理",
          config_center.stored("log.pull_paths", None) is None)
    check("切回内置方案 → 内置默认",
          config_center.primary_pull_path() == core_config.LOG_REMOTE_DIR)

    print("\n[6] 用户场景：保存 → 切方案 → 切回来（值不丢）")
    core_project.set_active("s1")
    core_project.save_values({"project.log.pull_paths": modified})
    check("s1 上保存生效", config_center.primary_pull_path() == "/custom/path/")
    core_project.set_active("plt2x")
    check("切到 plt2x → 用 plt2x 档案路径",
          config_center.primary_pull_path() == "/log/android/logcat/")
    core_project.set_active("s1")
    check("切回 s1 → 自定义值还在（2026-10-12 修复点：不再被清）",
          config_center.primary_pull_path() == "/custom/path/")

    print("\n[7] 本地输出根目录 local.output_root（2026-10-12：一个根目录管所有产物）")
    default_root = str(core_config.APP_DIR / "logs")
    check("留空 → 工具所在目录下的 logs",
          config_center.output_root() == default_root, config_center.output_root())
    check("无参 output_dir() = 根目录", config_center.output_dir() == default_root)
    check("产物子目录 logcat / screenshots / recordings",
          config_center.output_dir("logcat") == str(Path(default_root) / "logcat")
          and config_center.output_dir("screenshots").endswith("screenshots")
          and config_center.output_dir("recordings").endswith("recordings"))
    config_center.set("local.output_root", str(tmp / "my_logs"))
    check("配置生效", config_center.output_root() == str(tmp / "my_logs"))
    check("子目录跟随",
          config_center.output_dir("logcat") == str(tmp / "my_logs" / "logcat")
          and config_center.output_dir("recordings") == str(tmp / "my_logs" / "recordings"))
    config_center.clear("local.output_root")
    check("清掉 → 回落默认", config_center.output_root() == default_root)

    # 复位隔离
    config_center.clear("project.active")
    core_project.set_projects_dir(None)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("配置中心测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
