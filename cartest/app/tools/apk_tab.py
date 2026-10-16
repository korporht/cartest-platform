# -*- coding: utf-8 -*-
"""APK 管理 Tab - 安装(含完整5参数)/卸载/信息/强制停止/Push/Pull
                / 浏览选择APK / 包名下拉列表持久化 /
                列三方包/列系统包 / 状态标签 / 彩色日志
                / 查询版本（P3d：作用于所选包名的通用版本查询）
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Optional

import flet as ft

from .base_tab import BaseTab
from ..ui.components import sync_text_value
from .adb_tool import ADBTool
from .constants import APK_PKG_HISTORY_FILE

#: 合法包名守卫（P3d 查询版本入参；与 Android 包名规范一致）
_PKG_RE = re.compile(r"^[A-Za-z0-9._]+$")

#: 命令 → 操作行标题（三期：耗时全量接入；未命中给通用标题）
_OP_TITLES: tuple[tuple[str, str], ...] = (
    ("adb install", "安装 APK"),
    ("adb uninstall", "卸载 APK"),
    ("aapt dump badging", "APK 信息"),
    ("am force-stop", "强制停止"),
    ("pm list packages", "包列表"),
    ("adb push", "推送文件"),
    ("adb pull", "拉取文件"),
)

#: 包列表过滤参数 → 事件参数摘要（避免事件行出现 `-3` 这类黑话）
_PKG_FLAG_LABEL: dict[str, str] = {
    "-3": "三方包",
    "-s": "系统包",
    "-d": "已停用",
    "-e": "已启用",
    "-u": "含已卸载",
}


def _op_title(cmd: str) -> str:
    for prefix, title in _OP_TITLES:
        if prefix in cmd:
            return title
    return "ADB 命令"


class ApkTab(BaseTab):
    def __init__(self, app) -> None:
        super().__init__(app)
        # 包名历史列表（持久化；平台适配：原锚在工具目录，改锚平台数据目录）
        self._pkg_list_file = APK_PKG_HISTORY_FILE
        self._pkg_list = self._load_pkg_list()
        # FilePicker 实例（0.85 是 Service：构造即自注册进 page._services，勿挂 overlay）
        self._file_picker = None

    def save_state(self) -> dict:
        """保存运行时状态"""
        apk_path = ""
        pkg_name = ""
        if hasattr(self, "_apk_path_field") and self._apk_path_field:
            apk_path = self._apk_path_field.value or ""
        if hasattr(self, "_pkg_field") and self._pkg_field:
            pkg_name = self._pkg_field.value or ""
        return {"apk_path": apk_path, "pkg_name": pkg_name}

    def restore_state(self, state: dict) -> None:
        """恢复运行时状态"""
        if "apk_path" in state and hasattr(self, "_apk_path_field") and self._apk_path_field:
            self._apk_path_field.value = state["apk_path"]
        if "pkg_name" in state and hasattr(self, "_pkg_field") and self._pkg_field:
            self._pkg_field.value = state["pkg_name"]

    def _load_pkg_list(self) -> list:
        if os.path.exists(self._pkg_list_file):
            try:
                with open(self._pkg_list_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_pkg_list(self) -> None:
        try:
            with open(self._pkg_list_file, "w", encoding="utf-8") as f:
                json.dump(self._pkg_list, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ──────────────────────────────────────────
    #  Build
    # ──────────────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        return ft.Container(
            content=ft.Column(
                controls=[
                    # ── 顶部：APK路径 + 安装/卸载/Info ──
                    self._build_install_bar(),
                    # ── 安装参数行（5个Checkbox）──
                    self._build_install_opts(),
                    # ── 包名行 + 卸载/强停/添加包名 ──
                    self._build_pkg_bar(),
                    # ── 快捷操作行 ──
                    self._build_quick_bar(),
                    # ── 状态标签 ──
                    self._status_row,
                    # 日志输出已统一到工具页「操作日志面板」（23 设计；原内联日志条移除）
                ],
                spacing=4,
                expand=True,
            ),
            expand=True,
            padding=ft.padding.Padding(top=12),
        )

    def _build_install_bar(self) -> ft.Container:
        t = self.t
        self._apk_path_field = ft.TextField(
            on_change=sync_text_value,          # 发现 #14：无回调则服务端读不到输入
            hint_text="APK 文件路径（拖放或粘贴）",
            expand=True,
            text_size=12,
            dense=True,
            border_radius=8,
            filled=True,
            bgcolor=t.c("bg_panel"),
        )
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text("APK文件:", size=12, weight=ft.FontWeight.BOLD),
                    self._apk_path_field,
                    ft.ElevatedButton(
                        "📂 浏览",
                        icon=ft.icons.Icons.FOLDER_OPEN,
                        on_click=self._browse_apk,
                        style=ft.ButtonStyle(bgcolor=t.c("bg_sidebar"), color=t.c("text_on_sidebar")),
                    ),
                    ft.ElevatedButton(
                        "安装",
                        icon=ft.icons.Icons.INSTALL_MOBILE,
                        style=ft.ButtonStyle(
                            bgcolor=t.c("success"), color=t.c("text_on_accent"),
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=self._install,
                    ),
                    ft.ElevatedButton(
                        "卸载",
                        icon=ft.icons.Icons.DELETE,
                        style=ft.ButtonStyle(
                            bgcolor=t.c("danger"), color=t.c("text_on_accent"),
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=self._uninstall,
                    ),
                    ft.ElevatedButton(
                        "APK Info",
                        icon=ft.icons.Icons.INFO,
                        on_click=self._apk_info,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, right=12),
        )

    def _build_install_opts(self) -> ft.Container:
        t = self.t
        # 5个安装参数 Checkbox（与 Tkinter 版完全对齐）
        self._chk_r = ft.Checkbox(label="-r 覆盖", value=True)
        self._chk_g = ft.Checkbox(label="-g 授权", value=True)
        self._chk_d = ft.Checkbox(label="-d 允许降级", value=False)
        self._chk_t = ft.Checkbox(label="-t 测试包", value=False)
        self._chk_s = ft.Checkbox(label="-s SD卡", value=False)
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text("参数:", size=11, color=t.c("text_secondary")),
                    self._chk_r, self._chk_g, self._chk_d,
                    self._chk_t, self._chk_s,
                    ft.Container(expand=True),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, right=12),
        )

    def _build_pkg_bar(self) -> ft.Container:
        t = self.t
        self._pkg_field = ft.Dropdown(
            label="包名",
            width=320,
            text_size=12,
            dense=True,
            editable=True,
            hint_text="选择或输入包名",
            border_radius=8,
            options=[ft.dropdown.Option(p) for p in self._pkg_list],
            filled=True,
            bgcolor=t.c("bg_panel"),
            on_text_change=self._on_pkg_text_change,
        )
        # ➕ 添加按钮
        add_btn = ft.IconButton(
            icon=ft.icons.Icons.ADD,
            icon_size=16,
            tooltip="将当前包名添加到下拉列表",
            on_click=self._add_pkg_to_list,
        )
        return ft.Container(
            content=ft.Row(
                controls=[
                    self._pkg_field,
                    add_btn,
                    ft.ElevatedButton(
                        "🗑️ 卸载",
                        icon=ft.icons.Icons.DELETE,
                        style=ft.ButtonStyle(
                            bgcolor=t.c("danger"), color=t.c("text_on_accent"),
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=self._uninstall,
                    ),
                    ft.ElevatedButton(
                        "🛑 强制停止",
                        icon=ft.icons.Icons.BLOCK,
                        on_click=self._force_stop,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, right=12),
        )

    def _build_quick_bar(self) -> ft.Container:
        t = self.t
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text("快捷:", size=12, weight=ft.FontWeight.BOLD),
                    ft.ElevatedButton(
                        "📋 列三方包",
                        icon=ft.icons.Icons.LIST,
                        style=ft.ButtonStyle(
                            bgcolor=t.c("accent"), color=t.c("text_on_accent"),
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=lambda e: self._list_pkgs("-3"),
                    ),
                    ft.ElevatedButton(
                        "📋 列系统包",
                        icon=ft.icons.Icons.LIST_ALT,
                        style=ft.ButtonStyle(
                            bgcolor="#8B5CF6", color="#FFFFFF",
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                        on_click=lambda e: self._list_pkgs("-s"),
                    ),
                    ft.ElevatedButton(
                        "🔍 查询版本",
                        icon=ft.icons.Icons.SEARCH,
                        on_click=self._query_version,
                    ),
                    ft.ElevatedButton(
                        "🧹 清空日志",
                        icon=ft.icons.Icons.CLEAR_ALL,
                        on_click=lambda e: self._clear_log(),
                    ),
                    ft.VerticalDivider(width=1, color=t.c("border")),
                    ft.ElevatedButton("Push", icon=ft.icons.Icons.UPLOAD,
                                       on_click=self._push),
                    ft.ElevatedButton("Pull", icon=ft.icons.Icons.DOWNLOAD,
                                       on_click=self._pull),
                    # 平台适配：移除原版末尾的 ft.Container(expand=True) 占位——
                    # wrap=True 的 Row 内含 expand 子项会使 Flutter 布局断言失败，
                    # 整页（含状态行与日志区）静默渲染为灰块（旧版缺陷）。视觉无变化。
                ],
                spacing=6,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, top=6, right=12, bottom=6),
        )

    def _ensure_controls(self) -> None:
        if not hasattr(self, "_status_ctrl"):
            self._status_ctrl = ft.Text(
                "状态: 就绪",
                size=11,
                color=self.t.c("success"),
                weight=ft.FontWeight.BOLD,
            )
        if not hasattr(self, "_log_text_ctrl"):
            self._log_text_ctrl = ft.Text(
                selectable=True, size=12,
                font_family="Consolas",
                color=self.t.c("log_text"),
                value="APK 管理就绪\n",
            )

    @property
    def _status_row(self):
        self._ensure_controls()
        return ft.Container(
            content=self._status_ctrl,
            padding=ft.padding.Padding(left=12, top=2, right=12, bottom=4),
        )

    @property
    def _log_text(self):
        self._ensure_controls()
        return self._log_text_ctrl

    def _log(self, text: str) -> None:
        """日志统一入口 → 工具页「操作日志面板」（23 设计；原内联日志条已移除）"""
        try:
            self.app.append_log_to_panel(text)
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, text: str, color_key: str = "success") -> None:
        def _do():
            try:
                self._status_ctrl.value = f"状态: {text}"
                self._status_ctrl.color = self.t.c(color_key)
                self._status_ctrl.update()
                self.app.page.update()
            except Exception:
                pass

        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            on_main = getattr(self.app, "on_main", None)
            try:
                (on_main or self.app.page.run_thread)(_do)
            except Exception:
                pass

    def _apk_path(self) -> str:
        return self._apk_path_field.value.strip().strip('"')

    def _pkg_name(self) -> str:
        val = (self._pkg_field.value or "").strip()
        if not val:
            val = (getattr(self._pkg_field, "text", "") or "").strip()
        return val

    def _on_pkg_text_change(self, e) -> None:
        """editable 下拉：输入文本同步到 value（否则服务端读到空——真机验证 #2）"""
        try:
            txt = (getattr(e.control, "text", None) or e.control.value or "").strip()
        except Exception:  # noqa: BLE001
            return
        if txt and txt != (self._pkg_field.value or ""):
            self._pkg_field.value = txt

    # ══════════════════════════════════════
    #  操作实现
    # ══════════════════════════════════════
    async def _browse_apk(self, e=None):
        """浏览选择 APK 文件"""
        # 平台适配：Web 模式不支持 FilePicker（调用会使会话异常退出），给出明确提示
        if self.app.page.web:
            self._log("[WARN] 浏览文件仅支持桌面模式（Web 模式不支持文件选择）")
            return
        # 0.85：FilePicker 是 Service（构造即注册进 page._services），**不要挂 overlay**
        # （否则前端报 Unknown control: FilePicker —— 2026-09-22 用户截图 bug）
        try:
            self._file_picker = ft.FilePicker()
            files = await self._file_picker.pick_files(
                dialog_title="选择 APK 文件",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["apk"],
            )
            if files and len(files) > 0:
                path = files[0].path
                self._apk_path_field.value = path
                self._apk_path_field.update()
                self._log(f"[INFO] 选择文件: {path}")
        except Exception as ex:  # noqa: BLE001
            self._log(f"[ERR] 选择文件失败：{ex}")
        finally:
            self._file_picker = None

    def _install(self, e=None) -> None:
        path = self._apk_path()
        if not path:
            self._log("[ERR] 请输入或浏览选择 APK 路径")
            return
        args = []
        if self._chk_r.value: args.append("-r")
        if self._chk_g.value: args.append("-g")
        if self._chk_d.value: args.append("-d")
        if self._chk_t.value: args.append("-t")
        if self._chk_s.value: args.append("-s")
        arg_str = " ".join(args) if args else ""
        safe_path = ADBTool.sanitize_shell_arg(path)
        cmd = f'adb install {arg_str} {safe_path}'
        self._run_adb(cmd, installing=True,
                      param=f"{os.path.basename(path)} {arg_str}".strip())

    def _uninstall(self, e=None) -> None:
        pkg = self._pkg_name()
        if not pkg:
            self._log("[ERR] 请输入或选择包名")
            return
        safe_pkg = ADBTool.sanitize_shell_arg(pkg)
        self._run_adb(f"adb uninstall {safe_pkg}", uninstalling=True, param=pkg)

    def _apk_info(self, e=None) -> None:
        path = self._apk_path()
        if not path:
            self._log("[ERR] 请输入 APK 路径")
            return
        safe_path = ADBTool.sanitize_shell_arg(path)
        self._run_adb(f'aapt dump badging {safe_path}', param=os.path.basename(path))

    def _force_stop(self, e=None) -> None:
        pkg = self._pkg_name()
        if not pkg:
            self._log("[ERR] 请输入包名")
            return
        safe_pkg = ADBTool.sanitize_shell_arg(pkg)
        self._run_adb(f"adb shell am force-stop {safe_pkg}", param=pkg)

    def _list_pkgs(self, flag: str, e=None) -> None:
        _f = flag.strip()
        self._run_adb(f"adb shell pm list packages {flag}",
                      param=_PKG_FLAG_LABEL.get(_f, _f or "全部包"))

    def _push(self, e=None) -> None:
        path = self._apk_path()
        if not path:
            self._log("[ERR] 请输入要推送的本地文件路径")
            return
        safe_path = ADBTool.sanitize_shell_arg(path)
        self._run_adb(f'adb push {safe_path} /sdcard/',
                      param=os.path.basename(path))

    def _pull(self, e=None) -> None:
        pkg = self._pkg_name()
        if not pkg:
            self._log("[INFO] Pull — 用法: 在包名栏输入车机路径")
            return
        safe_pkg = ADBTool.sanitize_shell_arg(pkg)
        self._run_adb(f'adb pull {safe_pkg} .', param=pkg)

    @staticmethod
    def _version_cmd(pkg: str) -> str:
        """查询命令（整串引号包裹 → 管道在**设备侧**执行；旧实现 host 侧 grep 恒失败）。"""
        return f'adb shell "dumpsys package {pkg} | grep -m1 -E \'versionName|Unable to find package\'"'

    def _query_version(self, e=None) -> None:
        """查询**所选包名**的版本号（P3d：替代原「相机」「相册」两个硬编码按钮）。"""
        pkg = self._pkg_name()
        if not pkg:
            self._log("[WARN] 请先选择或输入包名")
            self._set_status("请先选择或输入包名", "danger")
            return
        if not _PKG_RE.match(pkg):
            self._log(f"[ERR] 包名含非法字符：{pkg}")
            self._set_status("包名非法", "danger")
            return

        def _run():
            with self.app.op("查询版本", tag="APK", param=pkg) as handle:
                self._log(f"[CMD] {self._version_cmd(pkg)}")
                _, out = ADBTool.run_cmd(self._version_cmd(pkg))
                text = (out or "").strip()
                if "Unable to find package" in text:
                    handle.fail()
            if "Unable to find package" in text:
                self._log(f"[FAIL] 未找到包：{pkg}")
                self._set_status("未找到包", "danger")
                self._done("未找到包", ok=False)
                return
            line = next((l.strip() for l in text.splitlines() if "versionName" in l), "")
            if not line:
                self._log(text if text else "[FAIL] adb 执行失败或无输出")
                self._log(f"[FAIL] 查询失败：{pkg}（无 versionName 信息）")
                self._set_status("查询失败", "danger")
                self._done("查询失败", ok=False)
                return
            self._log(line)
            self._log(f"📦 {pkg}: {line}")
            self._set_status("查询完成", "success")
            self._done("查询完成")

        self._run_in_thread(_run)

    def _add_pkg_to_list(self, e=None) -> None:
        """将当前包名添加到下拉列表并持久化"""
        pkg = self._pkg_name()
        if not pkg:
            return
        if pkg not in self._pkg_list:
            self._pkg_list.append(pkg)
            self._save_pkg_list()
            # 更新 Dropdown 选项
            self._pkg_field.options = [ft.dropdown.Option(p) for p in self._pkg_list]
            self._pkg_field.update()
            self._log(f"[OK] 已添加包名: {pkg}")
        else:
            self._log("[WARN] 包名已在列表中")

    def _clear_log(self, e=None) -> None:
        try:
            self.app.clear_log_panel()
        except Exception:  # noqa: BLE001
            pass

    def _run_adb(self, cmd: str, **kwargs) -> None:
        """执行 adb 命令（三期：统一包一层操作计时行，标题由命令推导/可显式指定）"""
        title = kwargs.get("title") or _op_title(cmd)
        status_key = kwargs.get("status_key") or None
        if kwargs.get("installing"):
            self._set_status("安装中…", "warning")
        elif kwargs.get("uninstalling"):
            self._set_status("卸载中…", "danger")

        def _run():
            with self.app.op(title, tag="APK", param=kwargs.get("param", "")) as handle:
                self._log(f"[CMD] {cmd}")
                ok, out = ADBTool.run_cmd(cmd)
                if not ok:
                    handle.fail()
                # 输出属该事件块（「只看这个 / 导出该事件块 / 明细计数」才完整）
                self._log(out.strip() or (f"[OK] 执行完成（无输出）" if ok
                                          else "[FAIL] 执行失败（无输出）"))
            if kwargs.get("installing"):
                self._set_status("安装成功" if ok else "安装失败",
                                 "success" if ok else "danger")
                self._done("安装完成" if ok else "安装失败", ok=ok)
            elif kwargs.get("uninstalling"):
                if ok:
                    self._set_status("卸载完成", "success")
                else:
                    self._set_status("卸载失败", "danger")
                self._done("卸载完成" if ok else "卸载失败", ok=ok)
            elif status_key:
                self._set_status("完成" if ok else "失败", status_key if status_key else "success" if ok else "danger")

        self._run_in_thread(_run)
