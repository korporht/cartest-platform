# -*- coding: utf-8 -*-
"""P3c-2 验收 — AdbService 路径接线（离线：替身 ADB，不连设备）

覆盖：
  1) pull_logs 默认配置 → 与旧行为一致（1 条 pull，Android 目录）
  2) pull_logs 启用三项 → 3 条 pull（别名命名目录 logs_<ts>_<别名>）
  3) pull_logs 别名缺失 → 回退路径末段命名
  4) clear_logs 默认只清主路径；启用 SYS1 → 追加清理
  5) check_log_size 按方案 log.size_paths 的启用项逐条 du（跳过未启用）

注：日志路径自 2026-10-12 起**属于方案**（``project.log.*``），故这里经
``project.save_values`` 写方案文件（＝配置页保存走的同一条链路），不再写 settings。

用法:
    python tests/test_wiring_adb_service.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

import os  # noqa: E402

from cartest.core import config_center, project                 # noqa: E402
from cartest.app.tools.services import adb_service as svc_mod   # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def set_paths(kind: str, value: list) -> None:
    """写**方案值**（配置页保存同链路：``project.save_values`` → 方案文件）"""
    ok, why = project.save_values({f"project.log.{kind}": value})
    if not ok:
        raise RuntimeError(f"写方案值失败：{why}")


class FakeADB:
    """替身：记录命令、恒成功；`outputs` 可按命令定制回显（供 initialize 用）。"""
    calls: list[str] = []
    outputs: dict[str, str] = {}
    fail_on: set[str] = set()

    @staticmethod
    def run_cmd(cmd, is_binary=False, timeout=None):
        FakeADB.calls.append(cmd)
        if cmd in FakeADB.fail_on:
            return False, FakeADB.outputs.get(cmd, "[fake-fail]")
        return True, FakeADB.outputs.get(cmd, "[fake]")


def main() -> int:
    tmp = Path(mkdtemp(prefix="p3c2_wiring_"))
    config_center.set_settings_file(tmp / "settings.json")   # 隔离：不碰真实 settings.json
    project.set_projects_dir(tmp / "projects")               # 隔离：不碰真实方案文件
    svc_mod.ADBTool = FakeADB                                 # 注入替身

    print("=" * 70)
    print("P3c-2 验收 — AdbService 路径接线（离线替身）")
    print(f"临时配置: {tmp / 'settings.json'}")
    print("=" * 70)

    logs: list[str] = []
    svc = svc_mod.AdbService(on_log=lambda m: logs.append(m))
    base = tmp / "out"

    print("\n[1] pull_logs 默认配置（无 settings.json → schema 默认，仅主路径）")
    FakeADB.calls.clear()
    svc.pull_logs(str(base), "T1")
    check("仅 1 条 pull", len(FakeADB.calls) == 1, str(FakeADB.calls))
    check("pull 命令与旧行为一致",
          FakeADB.calls[0] == f"adb pull /data/log/voice/ {base / 'logs_T1_Android'}",
          str(FakeADB.calls))
    check("Android 目录已建", (base / "logs_T1_Android").is_dir())

    print("\n[2] pull_logs 启用三项（写方案值后立即生效）")
    set_paths("pull_paths", [
        {"path": "/data/log/voice/", "alias": "Android", "enabled": True},
        {"path": "/mnt/log/sys1/", "alias": "SYS1", "enabled": True},
        {"path": "/mnt/log/sys2/", "alias": "SYS2", "enabled": True},
    ])
    FakeADB.calls.clear()
    logs.clear()
    svc.pull_logs(str(base), "T2")
    check("3 条 pull", len(FakeADB.calls) == 3, str(FakeADB.calls))
    check("SYS1 目录按别名命名", (base / "logs_T2_SYS1").is_dir())
    check("SYS2 目录按别名命名", (base / "logs_T2_SYS2").is_dir())
    check("第 2 条 pull 指向 SYS1 路径",
          FakeADB.calls[1] == f"adb pull /mnt/log/sys1/ {base / 'logs_T2_SYS1'}",
          str(FakeADB.calls))
    check("日志行含别名", "[INFO] 拉取 SYS1 日志…" in logs, str(logs))

    print("\n[3] pull_logs 别名缺失 → 回退路径末段")
    set_paths("pull_paths", [
        {"path": "/custom/log/dir/", "alias": "", "enabled": True},
    ])
    FakeADB.calls.clear()
    svc.pull_logs(str(base), "T3")
    check("目录名用路径末段 dir", (base / "logs_T3_dir").is_dir(),
          str([p.name for p in base.iterdir()] if base.exists() else "base 不存在"))

    print("\n[4] clear_logs 按启用项")
    set_paths("pull_paths", [
        {"path": "/data/log/voice/", "alias": "Android", "enabled": True},
        {"path": "/mnt/log/sys1/", "alias": "SYS1", "enabled": False},
    ])
    FakeADB.calls.clear()
    svc.clear_logs()
    check("未启用 → 只清主路径（与旧默认一致）",
          FakeADB.calls == ["adb shell rm -rf /data/log/voice/*"],
          str(FakeADB.calls))

    set_paths("pull_paths", [
        {"path": "/data/log/voice/", "alias": "Android", "enabled": True},
        {"path": "/mnt/log/sys1/", "alias": "SYS1", "enabled": True},
    ])
    FakeADB.calls.clear()
    svc.clear_logs()
    check("启用 SYS1 → 追加清理",
          FakeADB.calls == [
              "adb shell rm -rf /data/log/voice/*",
              "adb shell rm -rf /mnt/log/sys1/*",
          ], str(FakeADB.calls))

    print("\n[5] check_log_size 按方案 log.size_paths 启用项")
    set_paths("size_paths", [
        {"path": "/data/log/voice/", "alias": "Android", "enabled": True},
        {"path": "/mnt/log/sys1/", "alias": "SYS1", "enabled": True},
        {"path": "/mnt/log/sys2/", "alias": "SYS2", "enabled": False},
    ])
    FakeADB.calls.clear()
    svc.check_log_size()
    check("2 条 du（跳过未启用）", len(FakeADB.calls) == 2, str(FakeADB.calls))
    check("du 目标为前两条",
          FakeADB.calls == [
              "adb shell du -sh /data/log/voice/",
              "adb shell du -sh /mnt/log/sys1/",
          ], str(FakeADB.calls))

    print("\n[6] initialize 一键初始化连接（devices → root → remount）")
    logs.clear()
    FakeADB.outputs = {
        "adb devices": "List of devices attached\n5ad9194b\tdevice\n",
        "adb root": "adbd is already running as root",
        "adb remount": "remount succeeded",
    }
    FakeADB.calls.clear()
    ok, why = svc.initialize()
    check("有设备时初始化成功", ok is True, str(why))
    check("依次执行 devices → root → remount",
          FakeADB.calls == ["adb devices", "adb root", "adb remount"],
          str(FakeADB.calls))
    check("过程输出进事件窗口（on_log 含 devices/root/remount 回显）",
          sum(1 for x in logs if "devices" in x or "root" in x or "remount" in x) >= 3,
          str(logs[-3:]))

    FakeADB.outputs = {"adb devices": "List of devices attached\n"}
    FakeADB.calls.clear()
    ok2, why2 = svc.initialize()
    check("无在线设备 → 失败且不再 root/remount",
          ok2 is False and "未发现在线设备" in why2 and FakeADB.calls == ["adb devices"],
          f"{why2} / {FakeADB.calls}")
    check("offline/unauthorized 不算在线",
          svc._has_online_device("List of devices attached\nX\toffline\n") is False
          and svc._has_online_device("List of devices attached\nX\tunauthorized\n") is False
          and svc._has_online_device("List of devices attached\nX\tdevice\n") is True)

    FakeADB.outputs = {"adb devices": "List of devices attached\nX\tdevice\n",
                       "adb root": "adbd cannot run as root in production builds"}
    FakeADB.fail_on = {"adb root"}
    FakeADB.calls.clear()
    ok3, why3 = svc.initialize()
    FakeADB.fail_on = set()
    check("root 失败 → 停止（不执行 remount）",
          ok3 is False and "adb root 失败" in why3
          and FakeADB.calls == ["adb devices", "adb root"],
          f"{why3} / {FakeADB.calls}")

    print("\n[7] probe_state 设备状态探测（root/分区/overlay/verity）")
    # ① 只读 + verity enforcing + 非 root → 需重启提示
    FakeADB.outputs = {
        "adb shell id": "uid=2000(shell) gid=2000(shell) context=u:r:shell:s0",
        "adb shell mount": (
            "/dev/block/dm-12 on / type ext4 (ro,seclabell,discard)\n"
            "/dev/block/dm-15 on /vendor type ext4 (ro,seclabel,discard)\n"
            "/dev/block/dm-55 on /data type f2fs (rw,lazytime)\n"),
        "adb shell getprop ro.boot.veritymode": "enforcing",
        "adb shell getprop ro.build.type": "userdebug",
    }
    FakeADB.calls.clear()
    st1 = svc.probe_state()
    check("非 root 识别", st1["adbd_root"] is False, st1["uid"][:24])
    check("分区 ro/rw 解析（/ 与 /vendor 只读、/data 可写）",
          st1["partitions"]["/"] == "ro" and st1["partitions"]["/vendor"] == "ro"
          and st1["partitions"]["/product"] == "—", str(st1["partitions"]))
    check("无 overlay → 系统分区只读", st1["overlay"] == [] and st1["system_writable"] is False)
    check("verity/build 透传", st1["verity"] == "enforcing" and st1["build_type"] == "userdebug")
    check("结论：需要重启才可写", st1["need_reboot"] is True and st1["hint"])
    check("summary 一句话说清", "非 root" in st1["summary"] and "只读" in st1["summary"],
          st1["summary"])
    check("lines 逐条明细 ≥4 行", len(st1["lines"]) >= 4, str(st1["lines"]))

    # ② root + overlay 可写层 → 不需重启，但提示重启会失效
    FakeADB.outputs = {
        "adb shell id": "uid=0(root) gid=0(root) context=u:r:su:s0",
        "adb shell mount": (
            "overlay on /system type overlay (rw,seclabel)\n"
            "/dev/block/dm-12 on / type ext4 (ro,seclabel)\n"),
        "adb shell getprop ro.boot.veritymode": "enforcing",
        "adb shell getprop ro.build.type": "userdebug",
    }
    st2 = svc.probe_state()
    check("root 识别", st2["adbd_root"] is True)
    check("overlay 挂载点解析", st2["overlay"] == ["/system"], str(st2["overlay"]))
    check("有 overlay → 视为可写且无需重启",
          st2["system_writable"] is True and st2["need_reboot"] is False)
    check("overlay 提示重启可能失效", "失效" in (st2["hint"] or ""), st2["hint"])
    check("summary 标注可写层", "root" in st2["summary"] and "overlay" in st2["summary"],
          st2["summary"])

    # ③ 真可写分区（disable-verity 后）→ 不需重启
    FakeADB.outputs = {
        "adb shell id": "uid=0(root) gid=0(root)",
        "adb shell mount": "/dev/block/dm-13 on /system type ext4 (rw,seclabel)\n",
        "adb shell getprop ro.boot.veritymode": "disabled",
        "adb shell getprop ro.build.type": "userdebug",
    }
    st3 = svc.probe_state()
    check("可写分区 + verity disabled → 无需重启",
          st3["system_writable"] is True and st3["need_reboot"] is False
          and st3["partitions"]["/system"] == "rw", str(st3["partitions"]))

    # ④ 命令失败不炸（部分信息缺失也给结论）
    FakeADB.fail_on = {"adb shell mount", "adb shell id"}
    FakeADB.outputs = {}
    st4 = svc.probe_state()
    check("探测失败不抛异常且有结论", isinstance(st4, dict) and st4["summary"],
          st4["summary"])
    FakeADB.fail_on = set()

    print("\n[8] screenshot 只产出一张图（水印开启时删中间原图）")
    shot = tmp / "shots"
    shot.mkdir(parents=True, exist_ok=True)

    def _wm(src, dst):                      # 替身水印：写个成品文件即可
        Path(dst).write_bytes(b"watermarked")

    local_tmp = str(shot / "car_x_raw.png")
    final_dst = str(shot / "car_x_ts.png")
    Path(local_tmp).write_bytes(b"raw")     # 模拟 adb pull 落盘
    FakeADB.calls.clear()
    out = svc.screenshot(timestamp_enabled=True, has_pil=True,
                         remote_tmp="/sdcard/screenshot_tmp.png",
                         local_tmp=local_tmp, final_dst=final_dst,
                         add_watermark=_wm)
    check("水印开启 → 返回成品路径", out == final_dst, out)
    check("水印开启 → 中间原图已删（目录只剩一张）",
          not os.path.exists(local_tmp) and os.path.exists(final_dst)
          and [p.name for p in shot.iterdir()] == ["car_x_ts.png"],
          str(sorted(p.name for p in shot.iterdir())))

    shot2 = tmp / "shots2"
    shot2.mkdir(parents=True, exist_ok=True)
    single = str(shot2 / "car_y.png")
    Path(single).write_bytes(b"raw")
    FakeADB.calls.clear()
    out2 = svc.screenshot(timestamp_enabled=False, has_pil=True,
                          remote_tmp="/sdcard/screenshot_tmp.png",
                          local_tmp=single, final_dst=single,
                          add_watermark=_wm)
    check("无水印 → 直接落成品名且只一张",
          out2 == single and os.path.exists(single)
          and [p.name for p in shot2.iterdir()] == ["car_y.png"],
          str(sorted(p.name for p in shot2.iterdir())))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("AdbService 路径接线测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
