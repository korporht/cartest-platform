# -*- coding: utf-8 -*-
"""ADB 基础操作 Service — ADB 连接 / Root+Remount / 重启 / 日志管理"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Callable, List, Optional, Tuple

from ..adb_tool import ADBTool
from ....core import config_center

_log = logging.getLogger("adb_service")


class AdbService:
    """ADB 设备基础操作与日志管理的纯业务逻辑封装。

    所有方法均为 **同步阻塞** 的，调用方需自行确保线程安全（例如通过 ``_run_in_thread``）。
    """

    def __init__(
        self,
        on_log: Callable[[str], None],
        on_status: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """
        Args:
            on_log: 日志回调，接收一行文本。
            on_status: 状态栏回调，接收 (文本, 颜色键)。
        """
        self._on_log = on_log
        self._on_status = on_status
        _log.info("[__init__] AdbService created, _on_log=%s", type(on_log).__name__)

    # ── 设备连接与权限 ──────────────────────────────────────

    def probe_state(self) -> dict:
        """探测设备当前**权限/分区/叠加**状态（一次点击说清楚，2026-09-22 用户要求）

        返回::

            {"adbd_root": bool, "uid": str,
             "partitions": {"//system"/"vendor"/…: "ro"/"rw"/"—"},
             "overlay": [挂载点…], "verity": "enforcing", "build_type": "userdebug",
             "system_writable": bool, "need_reboot": bool,
             "summary": 一行结论, "lines": [逐条明细…], "hint": 下一步建议}

        说明（为什么需要它）：`adb root` 不是持久的（adbd 一重启就回落），
        `adb remount` 在 Android 10+ 常走 **overlayfs**——底层分区仍只读，
        写入落可写上层且**需重启才生效**、重启后可能失效。把这些状态显式说出来，
        避免"push 失败才发现"。
        """
        uid = ""
        try:
            _ok, uid = ADBTool.run_cmd("adb shell id")
        except Exception:  # noqa: BLE001
            uid = ""
        adbd_root = "uid=0(" in (uid or "")

        mounts: list[str] = []
        try:
            _ok2, out2 = ADBTool.run_cmd("adb shell mount")
            mounts = [ln for ln in (out2 or "").splitlines() if " on " in ln]
        except Exception:  # noqa: BLE001
            mounts = []

        parts: dict[str, str] = {}
        for target in ("/", "/system", "/vendor", "/product", "/system_ext", "/odm"):
            parts[target] = self._mount_mode(mounts, target)

        overlay: list[str] = []
        for ln in mounts:
            if " type overlay " in ln:
                try:
                    overlay.append(ln.split(" on ", 1)[1].split(" type ", 1)[0])
                except Exception:  # noqa: BLE001
                    continue

        def _prop(name: str) -> str:
            try:
                _o, v = ADBTool.run_cmd(f"adb shell getprop {name}")
                return (v or "").strip()
            except Exception:  # noqa: BLE001
                return ""

        verity = _prop("ro.boot.veritymode")
        build_type = _prop("ro.build.type")

        sys_modes = [m for t, m in parts.items() if t != "/"]
        system_writable = any(m == "rw" for m in sys_modes) or bool(overlay)
        # 只读 + verity enforcing → 要做系统写入需 remount（并常常需重启生效）
        need_reboot = (not system_writable) and verity.lower() == "enforcing"

        if system_writable and overlay:
            where = "可写层（overlay）"
        elif system_writable:
            where = "可写分区"
        else:
            where = "只读"
        summary = (f"{'root' if adbd_root else '非 root'} · 系统分区{where}"
                   f"{' · verity=' + verity if verity else ''}")

        lines = [
            f"[状态] adbd: {'root（uid=0）' if adbd_root else '非 root（shell）—— 需 root 的系统操作会失败'}",
            "[状态] 分区: " + " ".join(f"{t}={m}" for t, m in parts.items()),
            f"[状态] overlay 可写层: {'、'.join(overlay) if overlay else '无'}",
            f"[状态] verity={verity or '未知'} build={build_type or '未知'}",
        ]
        hint = ""
        if need_reboot:
            hint = ("要做系统分区写入：先 `adb root` + `adb remount`，"
                    "并按设备提示**重启设备**使 remount 生效（重启后需再 root/remount）")
        elif overlay:
            hint = "overlay 已启用：写入落可写层，**重启后可能失效**（需持久请 disable-verity）"
        return {
            "adbd_root": adbd_root, "uid": (uid or "").strip(),
            "partitions": parts, "overlay": overlay,
            "verity": verity, "build_type": build_type,
            "system_writable": system_writable, "need_reboot": need_reboot,
            "summary": summary, "lines": lines, "hint": hint,
        }

    @staticmethod
    def _mount_mode(mounts: list[str], target: str) -> str:
        """从 ``mount`` 输出里取某挂载点的 ro/rw（无独立挂载 → ``—``）"""
        for ln in mounts:
            try:
                after_on = ln.split(" on ", 1)[1]
                point, rest = after_on.split(" type ", 1)
            except Exception:  # noqa: BLE001
                continue
            if point.strip() != target:
                continue
            opts = rest.split("(", 1)[1] if "(" in rest else rest
            tokens = [t.strip() for t in opts.split(",")]
            return "rw" if tokens and tokens[0] == "rw" else "ro"
        return "—"

    def initialize(self) -> Tuple[bool, str]:
        """**一键初始化连接**：``adb devices`` → ``adb root`` → ``adb remount``

        2026-09-22 用户裁决：原「ADB 连接」与「Root+Remount」两颗按钮合并为一颗
        ——root/remount 本就必须先确认连接，分两步是多余动作。

        语义：
          · 先 `adb devices` 校验**有在线设备**（无设备则直接失败，不继续 root）
          · 再 `adb root` 提权（已是 root 会返回 "adbd is already running as root"，
            视作成功）；失败即停
          · 最后 `adb remount` 挂载可写
        返回 ``(ok, 失败说明)``；过程输出经 ``on_log`` 进事件窗口。
        """
        _log.info("[initialize] adb devices → root → remount")
        ok, out = ADBTool.run_cmd("adb devices")
        self._on_log(out if out.strip() else "[INFO] adb devices")
        if not ok:
            return False, "adb devices 执行失败（检查 adb 是否在 PATH）"
        if not self._has_online_device(out):
            return False, "未发现在线设备（adb devices 为空）——请检查 USB / 网络连接"

        ok_root, out_root = ADBTool.run_cmd("adb root")
        self._on_log(out_root if out_root.strip() else "[INFO] adb root")
        if not ok_root:
            return False, "adb root 失败（设备可能未解锁 userdebug/eng 版本）"
        time.sleep(1)

        ok_rm, out_rm = ADBTool.run_cmd("adb remount")
        self._on_log(out_rm if out_rm.strip() else "[INFO] adb remount")
        if not ok_rm:
            return False, "adb remount 失败（分区可能未禁用 verity）"
        return True, ""

    @staticmethod
    def _has_online_device(devices_out: str) -> bool:
        """``adb devices`` 输出里是否有 ``device``（非 ``offline``/``unauthorized``）行"""
        for line in (devices_out or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
        return False

    def check_adb(self) -> str:
        """检查 ADB 设备连接状态，返回 ``adb devices`` 输出。"""
        self._on_log("[INFO] ADB 连接检查中...")
        _log.info("[check_adb] before run_cmd")
        _ok, out = ADBTool.run_cmd("adb devices")
        _log.info("[check_adb] after run_cmd, ok=%s, out_len=%d, first=%.100s", _ok, len(out), out)
        return out

    def root_remount(self) -> None:
        """依次执行 ``adb root`` 和 ``adb remount``。"""
        _log.info("[root_remount] calling _on_log for root output")
        ok1, out1 = ADBTool.run_cmd("adb root")
        self._on_log(out1)
        time.sleep(1)
        _log.info("[root_remount] calling _on_log for remount output")
        ok2, out2 = ADBTool.run_cmd("adb remount")
        self._on_log(out2)

    def reboot(self) -> None:
        """执行 ``adb reboot`` 重启车机。"""
        _log.info("[reboot] calling _on_log for reboot")
        _ok, out = ADBTool.run_cmd("adb reboot")
        self._on_log(out if out else "[OK] 设备重启中")

    # ── 日志管理 ──────────────────────────────────────────

    def pull_logs(self, base_dir: str, ts: str) -> None:
        """按配置逐条拉取**启用项**日志到本地（目录名 ``logs_{ts}_{别名}``；失败自动重试 1 次）。

        Args:
            base_dir: 本地保存根目录（如 ``LOCAL_SAVE_ROOT``）。
            ts: 时间戳字符串（目录名后缀，如 ``20260916_120000``）。
        """
        tasks: List[Tuple[str, str, str]] = []
        for item in (config_center.effective_paths("pull") or []):
            path = str(item.get("path", "")).strip()
            if not item.get("enabled") or not path:
                continue
            alias = self._alias_of(item)
            dst = os.path.join(base_dir, f"logs_{ts}_{alias}")
            os.makedirs(dst, exist_ok=True)
            tasks.append((alias, path, dst))

        _log.info("[pull_logs] base=%s, ts=%s, tasks=%d", base_dir, ts, len(tasks))
        if not tasks:
            self._on_log("[WARN] 没有启用的日志路径（请在「配置」页添加）")
            return

        # ── 首轮抓取 ──
        failed = self._pull_tasks(tasks)

        # ── 失败重试：清理残留 → root+remount → 重试 ──
        if failed:
            self._on_log("[WARN] 部分路径抓取失败，清理残留后重试…")

            for _, _, dst in failed:
                self._clear_local_dir(dst)

            self._on_log("[INFO] 执行 adb root + remount …")
            ADBTool.run_cmd("adb root")
            time.sleep(1)
            ADBTool.run_cmd("adb remount")
            time.sleep(1)

            self._on_log("[INFO] 重新拉取失败路径…")
            failed_again = self._pull_tasks(failed)

            if failed_again:
                names = ", ".join(l for l, _, _ in failed_again)
                self._on_log(f"[FAIL] {names} 重试后仍然失败")
            else:
                self._on_log("[OK] 重试成功")

    @staticmethod
    def _alias_of(item: dict) -> str:
        """别名；留空时回退路径末段（如 ``/mnt/log/sys1/`` → ``SYS1``）。"""
        alias = str(item.get("alias", "")).strip()
        if alias:
            return alias
        seg = str(item.get("path", "")).rstrip("/").rsplit("/", 1)[-1]
        return seg or "path"

    def _pull_tasks(self, tasks: List[Tuple[str, str, str]]) -> List[Tuple[str, str, str]]:
        """批量执行 adb pull 任务，返回失败的子集。"""
        failed = []
        for label, remote, dst in tasks:
            self._on_log(f"[INFO] 拉取 {label} 日志…")
            ok, out = ADBTool.run_cmd(f"adb pull {remote} {dst}")
            self._on_log(out or ("[OK]" if ok else "[FAIL]"))
            if not ok:
                failed.append((label, remote, dst))
        return failed

    @staticmethod
    def _clear_local_dir(path: str) -> None:
        """清空本地目录内容（保留空目录）。"""
        if not path or not os.path.isdir(path):
            return
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception:
                pass

    def clear_logs(self) -> None:
        """按配置清除**启用项**的远端日志（首条记 ``[CMD]``，其余记 ``[INFO]``）。"""
        targets = [str(i.get("path", "")).strip()
                   for i in (config_center.effective_paths("pull") or [])
                   if i.get("enabled") and str(i.get("path", "")).strip()]
        _log.info("[clear_logs] targets=%s", targets)
        if not targets:
            self._on_log("[WARN] 没有启用的日志路径（请在「配置」页添加）")
            return
        for idx, path in enumerate(targets):
            if idx == 0:
                self._on_log(f"[CMD] adb shell rm -rf {path}*")
            else:
                self._on_log(f"[INFO] 清除 {path} …")
                time.sleep(0.3)
            ok, out = ADBTool.run_cmd(f"adb shell rm -rf {path}*")
            self._on_log(out or ("[OK]" if ok else "[FAIL]"))

    def check_log_size(self) -> None:
        """按配置逐条查询**启用项**的目录大小。"""
        paths = [str(i.get("path", "")).strip()
                 for i in (config_center.effective_paths("size") or [])
                 if i.get("enabled") and str(i.get("path", "")).strip()]
        if not paths:
            self._on_log("[WARN] 没有启用的日志大小检查路径（请在「配置」页添加）")
            return
        for path in paths:
            name = path.rstrip("/")
            ok, out = ADBTool.run_cmd(f"adb shell du -sh {path}")
            if ok and out.strip():
                self._on_log(f"  {name}: {out.strip()}")
            else:
                self._on_log(f"  {name}: {out.strip() if out else '无法获取'}")

    # ── 截图 ─────────────────────────────────────────────

    def screenshot(self, timestamp_enabled: bool, has_pil: bool,
                   remote_tmp: str, local_tmp: str, final_dst: str,
                   add_watermark: Callable[[str, str], None]) -> str:
        """执行车机截图并拉取到本地。

        Args:
            timestamp_enabled: 是否叠加时间戳水印。
            has_pil: 当前环境是否安装了 Pillow。
            remote_tmp: 车机端临时路径。
            local_tmp: 本地临时路径。
            final_dst: 最终保存路径。
            add_watermark: 水印回调 (src, dst)。

        Returns:
            最终保存的文件路径，失败返回 ``""``。
        """
        _log.info("[screenshot] calling _on_log")
        self._on_log("[INFO] 正在截图…")
        ok1, _ = ADBTool.run_cmd(f"adb shell screencap {remote_tmp}")
        if not ok1:
            self._on_log("[FAIL] screencap 失败")
            return ""

        ok2, _ = ADBTool.run_cmd(f'adb pull {remote_tmp} "{local_tmp}"')
        if not ok2 or not os.path.exists(local_tmp):
            self._on_log("[FAIL] pull 失败")
            return ""

        if timestamp_enabled and has_pil:
            try:
                add_watermark(local_tmp, final_dst)
                try:                       # 中间原图用完即删（否则同一次截图留两张）
                    os.remove(local_tmp)
                except OSError:
                    pass
                self._on_log(f"[OK] 截图+时间戳: {final_dst}")
                return final_dst
            except Exception as ex:
                self._on_log(f"[WARN] 水印失败，使用原图: {ex}")
        elif timestamp_enabled and not has_pil:
            self._on_log("[WARN] Pillow 未安装，跳过时间戳水印")

        self._on_log(f"[OK] 截图保存: {local_tmp}")
        return local_tmp
