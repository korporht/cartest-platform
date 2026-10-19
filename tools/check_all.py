"""一键运行全部自测与迁移验证

用法：
    python tools/check_all.py

适用于每个阶段收尾时确认"没弄坏东西"。覆盖：
  内核自测（P1）→ 端到端（P1）→ 流水线（P2）→ 驾驶舱（P3a）→ 工具迁移（P3b）
  → 配置（P3c）→ P3d/P3e → UI-1/UI-2/UI-3 → UI-4（合并页）→ 迁移验证
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (显示名, 脚本相对路径, 附加参数)
TARGETS: list[tuple] = [
    ("内核自测 — AdbSession（离线）", "tests/test_adb.py"),
    ("内核自测 — LogcatMonitor（离线）", "tests/test_logcat.py"),
    ("工具自测 — Logcat 实时抓取服务 / 批量上屏（离线）", "tests/test_logcat_service.py"),
    ("内核自测 — Judger 判定链（离线）", "tests/test_judger.py"),
    ("内核自测 — DataSource 数据源（离线）", "tests/test_source.py"),
    ("内核自测 — LogParser 日志解析（离线）", "tests/test_parser.py"),
    ("内核自测 — Injector 编排（**无设备**）", "tests/test_injector.py"),
    ("内核自测 — parser/offline 封装（离线）", "tests/test_offline.py"),
    ("端到端验收 — 无设备跑通流水线", "tests/test_pipeline_e2e.py"),
    ("P2 — RunContext / 清理（离线）", "tests/test_pipeline_context.py"),
    ("P2 — 流水线 7 阶段 + 5 项验收（无设备）", "tests/test_pipeline_runner.py"),
    ("P3a — AppState 驱动链路：启动/停止/续跑（无设备）", "tests/test_app_state.py"),
    ("P3a 验收② — GUI 与 CLI 判定结果一致（无设备）", "tests/test_app_consistency.py"),
    ("P3a 验收①③④ — 七阶段 / 判定路径 / 翻案场景（替身）",
     "tools/gui_demo.py", ["--check"]),
    ("P3b 验收① — 时间戳工具三方对照（新/旧/标准库）", "tests/test_tools_timestamp.py"),
    ("P3b 布局护栏 — wrap 行内禁 expand 子项", "tests/test_tools_layout_guard.py"),
    ("P3c — Store 读取新鲜度（mtime）", "tests/test_config_store_fresh.py"),
    ("P3c — 配置中心（默认值对照/往返/primary）", "tests/test_config_center.py"),
    ("P3c — 路径列表编辑器（无 UI）", "tests/test_path_list_editor.py"),
    ("P3c-2 — adb_service 路径接线（离线替身）", "tests/test_wiring_adb_service.py"),
    ("P3c-2 — log_tab 去开关（源码断言）", "tests/test_wiring_log_tab.py"),
    ("P3c-2 — core.adb 配置主路径（离线替身）", "tests/test_wiring_core_adb.py"),
    ("P3d — 侧栏分组（单组「通用工具」）", "tests/test_sidebar_split.py"),
    ("P3d — APK 查询版本通用化（源码断言）", "tests/test_apk_query_version.py"),
    ("P3d-2 — 单例守卫（互斥/锁文件/restart）", "tests/test_singleton.py"),
    ("P3d-3 — 配置页保存反馈（加载缓冲+轻提示）", "tests/test_settings_feedback.py"),
    ("UI-8 — 轻提示 Toast（顶部浮层/自动隐藏/顶替/回退）", "tests/test_ui_toast.py"),
    ("P3d-4 — Web 模式常驻修复（静默死亡根因）", "tests/test_web_mode.py"),
    ("P3e — 大文件推送（分块/合并/取消）", "tests/test_filetransfer.py"),
    ("UI-1 — 设计令牌（双主题）", "tests/test_ui_tokens.py"),
    ("UI-1 — 壳与标签模型（无 UI）", "tests/test_ui_shell.py"),
    ("UI-4 — 合并页（应用与传输；无 UI）", "tests/test_ui_merged_pages.py"),
    ("Monkey — 模型与纯逻辑（离线）", "tests/test_monkey.py"),
    ("UI-5 — 动效打磨与配置页收尾（无 UI）", "tests/test_ui_polish.py"),
    ("OPLOG — 操作日志窗口一期（模型/渲染/接线）", "tests/test_oplog.py"),
    ("UI-9 — 动作完成反馈：强提示档 + 全局操作日志", "tests/test_notify_done.py"),
    ("TASKS — 全局任务登记表（40 号批 1）", "tests/test_task_registry.py"),
    ("TASKS — 设备操作队列（40 号批 1）", "tests/test_device_queue.py"),
    ("TASKS — 任务跨页存活 + 顶栏指示器（40 号批 2）", "tests/test_leave_survive.py"),
    ("TASKS — 设备操作队列落地（40 号批 3）", "tests/test_queue_flow.py"),
    ("TASKS — 其余四页接入全局任务（40 号批 4）", "tests/test_batch4_tasks.py"),
    ("TASKS — 流水线暂停/继续（40 号批 5）", "tests/test_pipeline_pause.py"),
    ("TASKS — 切设备「停止任务并切换」（40 号批 6）", "tests/test_stop_switch.py"),
    ("TASKS — 阶段显示中文化（40 号批 7）", "tests/test_stage_labels.py"),
    ("UI 护栏 — 输入框必须挂 on_change（发现 #14）", "tests/test_textfield_sync_guard.py"),
    ("UI-6 — 日志终端合并页三 tab（常驻槽；无 UI）", "tests/test_ui_logcat_tabs.py"),
    ("CFG-1 — 项目档案（值级定制点外提 + 三层取值）", "tests/test_project_profile.py"),
    ("CFG-2 — 配置页渲染补齐（bool/text/int/textarea + 项目选择）", "tests/test_settings_render.py"),
    ("OFFLINE — 报告解析（CbmLogextractor 产物 → 结构化）", "tests/test_report_view.py"),
    ("INJECT — 单条灌入（快灌；内核通路 + 页面）", "tests/test_single_inject.py"),
    ("BINDING — 设备绑定（方案 ↔ 设备；自动切配置）", "tests/test_device_binding.py"),
    ("RULES — 判定规则外提（judge.* 项目档案覆盖）", "tests/test_judge_rules.py"),
    ("SCHEME — 配置方案管理（增/删/改 + 上限 3 组；保存写方案）", "tests/test_scheme_manage.py"),
    ("BUGFIX — FilePicker 是 Service（勿挂 overlay）+ 异步必须 await", "tests/test_filepicker_service.py"),
    ("BUGFIX — UI 更新竞态（工作线程改控件必须回主线程）", "tests/test_ui_main_dispatch.py"),
    ("TOOLS — 外部工具目录定位（候选解析 + 可配置）", "tests/test_external_tools.py"),
    ("TREXTRACT — 时间段提取（选取/切割/容错；离线）", "tests/test_logextract.py"),
    ("PLATFORM — T28/T1MA 平台适配（文件 glob/分片聚合/拒识规则/档案契约）", "tests/test_platform_adapt.py"),
    ("DEVSWITCH — 全局设备切换（set_device/锁定/联动/prepare 校验）", "tests/test_device_switch.py"),
    ("GUARD — 测试临时目录禁裸 mkdtemp（防 %TEMP% 累积回归）", "tests/test_tmpdir_guard.py"),
    ("迁移验证 — progress（对照旧实现）", "tools/verify_migration.py"),
    ("迁移验证 — adb（对照旧实现）", "tools/verify_adb.py"),
    ("迁移验证 — judger（对照旧实现）", "tools/verify_judger.py"),
    ("迁移验证 — source（对照旧实现）", "tools/verify_source.py"),
    ("迁移验证 — parser（对照旧实现）", "tools/verify_parser.py"),
]


def main() -> int:
    results: list[tuple[str, bool, float]] = []
    for item in TARGETS:
        name, rel = item[0], item[1]
        extra: list[str] = item[2] if len(item) > 2 else []
        path = ROOT / rel
        print(f"\n{'=' * 70}")
        print(f"▶ {name}")
        print(f"  {rel}{' ' + ' '.join(extra) if extra else ''}")
        print("=" * 70)
        if not path.exists():
            print("  [SKIP] 文件不存在")
            results.append((name, True, 0.0))
            continue
        t0 = time.time()
        r = subprocess.run([sys.executable, str(path), *extra], cwd=ROOT)
        results.append((name, r.returncode == 0, time.time() - t0))

    print(f"\n{'=' * 70}")
    print("汇总")
    print("=" * 70)
    for name, ok, cost in results:
        print(f"  {'[PASS]' if ok else '[FAIL]'} {name}  ({cost:.1f}s)")
    failed = [n for n, ok, _ in results if not ok]
    print()
    if failed:
        print(f"共 {len(failed)} 项失败：{failed}")
        return 1
    print(f"全部 {len(results)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
