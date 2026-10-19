# -*- coding: utf-8 -*-
"""触控映射 Tab - 截图预览 + 鼠标坐标→设备tap坐标映射 + 快捷按键

核心功能：
1. 点击「截图」获取车机当前屏幕
2. 在截图上**直接点击** → 自动将点击位置换算为车机坐标并发送 adb tap
3. 底部快捷按键：返回/Home/音量±/静音/电源/上一曲/下一曲
"""
from __future__ import annotations

import base64
import os
from typing import Any, Optional

import flet as ft
from .base_tab import BaseTab
from .constants import TOUCH_DEFAULT_WIDTH, TOUCH_DEFAULT_HEIGHT
from .adb_tool import ADBTool

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "__snapshots__")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
TOUCH_SHOT = os.path.join(SNAPSHOT_DIR, "touch_preview.png")


def _png_size(path: str) -> tuple[int, int] | None:
    """从 PNG 头部（IHDR）读取实际宽高（无需 Pillow）。

    用途：截图完成后同步真实分辨率——否则 tap 映射用默认值（1920x720）会全错位，
    状态行也会误报默认值（2026-09-17 用户反馈）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read(24)
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _data_uri(path: str) -> str:
    """读文件为原图 data URI 字符串（Pillow 不可用时的兜底）。

    Web 端浏览器无法加载服务端 ``file://`` 路径（ft.Image.src 会静默空白，用户
    反馈"点了没反应"）；flet 0.85 的 ``ft.Image`` 无 ``src_base64`` 参数，故用
    ``data:image/png;base64,…`` 内联（2026-09-17）。
    """
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:  # noqa: BLE001
        return ""


_PREVIEW_MAX_W = 1600          # 预览显示宽 800px，2x 足够清晰
_PREVIEW_CACHE: dict[tuple[str, float, int], str] = {}


def _preview_data_uri(path: str) -> str:
    """生成**缩放后**预览图 data URI（Pillow 可选依赖，同 log_tab 惯例）。

    背景（2026-09-18 用户反馈"切到触控映射明显卡顿"）：原图 2560x1440 PNG
    ≈3.5MB，base64 ≈4.6MB——每次切页都要重编码+经 WebSocket 传输+浏览器全尺寸
    解码。此处缩放为 ≤1600px JPEG（≈几百 KB）并按 (路径, mtime, 大小) 缓存，
    避免每次切页重复开销。Pillow 不可用 → 回退原图 data URI（不阻塞功能）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_mtime, st.st_size)
    cached = _PREVIEW_CACHE.get(key)
    if cached:
        return cached
    uri = ""
    try:
        import io

        from PIL import Image  # 可选依赖：预览图缩放
        with Image.open(path) as im:
            im = im.convert("RGB")
            if im.width > _PREVIEW_MAX_W:
                h = max(1, round(im.height * _PREVIEW_MAX_W / im.width))
                im = im.resize((_PREVIEW_MAX_W, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        uri = _data_uri(path)
    if uri:
        if len(_PREVIEW_CACHE) > 8:
            _PREVIEW_CACHE.clear()
        _PREVIEW_CACHE[key] = uri
    return uri


class TouchTab(BaseTab):
    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self._device_w: int = TOUCH_DEFAULT_WIDTH
        self._device_h: int = TOUCH_DEFAULT_HEIGHT
        self._img_ctrl: Optional[ft.Image] = None
        self._res_ctrl: Optional[ft.Text] = None
        self._coord_ctrl: Optional[ft.Text] = None
        self._img_container: Optional[ft.Control] = None
        self._img_gd: Optional[ft.GestureDetector] = None

    # ──────────────────────────────────────────
    #  Build
    # ──────────────────────────────────────────
    def build(self) -> ft.Control:
        t = self.t
        has_shot = os.path.exists(TOUCH_SHOT)
        self._img_ctrl = ft.Image(
            src=_preview_data_uri(TOUCH_SHOT) if has_shot else "",
            visible=has_shot,
            fit=ft.BoxFit.CONTAIN,
            width=800,
            height=400,
        )
        self._res_ctrl = ft.Text(
            f"{self._device_w}x{self._device_h}",
            size=12,
            color=t.c("accent"),
        )
        self._coord_ctrl = ft.Text(
            "设备坐标: --  (点击截图区域发送 tap)",
            size=11,
            color=t.c("text_secondary"),
            italic=True,
        )

        return ft.Container(
            content=ft.Column(
                controls=[
                    # ── 工具栏 ──
                    self._build_toolbar(),
                    # ── 截图预览区（可点击）──
                    self._build_preview_area(),
                    # ── 坐标提示 ──
                    ft.Container(
                        content=self._coord_ctrl,
                        padding=ft.padding.Padding(left=12, top=0, right=12, bottom=4),
                    ),
                    # ── 快捷按键 ──
                    self._build_key_bar(),
                ],
                spacing=4,
                expand=True,
            ),
            expand=True,
            padding=ft.padding.Padding(top=12),
        )

    def _build_toolbar(self) -> ft.Container:
        t = self.t
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.icons.Icons.FINGERPRINT, size=18, color=t.c("accent")),
                    ft.Text("触控映射", size=14, weight=ft.FontWeight.BOLD,
                           color=t.c("text_primary")),
                    ft.VerticalDivider(width=1, color=t.c("border")),
                    ft.ElevatedButton("截图预览",
                                       icon=ft.icons.Icons.PHOTO_CAMERA,
                                       style=ft.ButtonStyle(
                                           bgcolor=t.c("accent"),
                                           color=t.c("text_on_accent"),
                                       ),
                                       on_click=self._screenshot),
                    ft.ElevatedButton("获取分辨率",
                                       icon=ft.icons.Icons.SCREEN_SEARCH_DESKTOP,
                                       on_click=self._get_resolution),
                    ft.Text("分辨率:", size=12),
                    self._res_ctrl,
                    ft.Container(expand=True),
                    ft.Text("💡 在截图上点击即可发送 adb tap",
                           size=10, color=t.c("text_disabled"), italic=True),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, top=8, right=12, bottom=8),
        )

    def _build_preview_area(self) -> ft.Container:
        """截图预览区 — 点击图片发送 tap（桌面端坐标走 GestureDetector）

        2026-09-23 全按钮自测发现：原 ``Container.on_click`` 在**桌面端不带坐标**
        （``e.data`` 为空 → 静默 return，点击无任何反应）。改为仅包住图片的
        ``GestureDetector.on_tap_down``（提供 ``local_x/local_y``，桌面/Web 可靠）。
        """
        t = self.t
        self._img_gd = ft.GestureDetector(
            content=self._img_ctrl,
            on_tap_down=self._on_image_tap,
        )
        self._img_container = ft.Container(
            content=self._img_gd,
            bgcolor=t.c("bg_log"),
            alignment=ft.Alignment.CENTER,
            expand=True,
            border_radius=8,
            border=ft.border.Border.all(1, t.c("border")),
            margin=ft.margin.Margin(left=12, top=8, right=12, bottom=8),
        )
        return self._img_container

    def _build_key_bar(self) -> ft.Container:
        t = self.t
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text("快捷按键:", size=12, weight=ft.FontWeight.BOLD),
                    self._key_btn("返回", "KEYCODE_BACK"),
                    self._key_btn("Home", "KEYCODE_HOME"),
                    self._key_btn("音量+", "KEYCODE_VOLUME_UP"),
                    self._key_btn("音量-", "KEYCODE_VOLUME_DOWN"),
                    self._key_btn("静音", "KEYCODE_MUTE"),
                    self._key_btn("电源", "KEYCODE_POWER"),
                    self._key_btn("上一曲", "KEYCODE_MEDIA_PREVIOUS"),
                    self._key_btn("下一曲", "KEYCODE_MEDIA_NEXT"),
                ],
                spacing=6,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.Padding(left=12, top=8, right=12, bottom=12),
        )

    # ──────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────
    def _key_btn(self, label: str, keycode: str) -> ft.ElevatedButton:
        return ft.ElevatedButton(
            label,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            on_click=lambda e, kc=keycode: self._send_key(kc),
        )

    def _send_key(self, keycode: str) -> None:
        def _run() -> None:
            ADBTool.run_cmd(f"adb shell input keyevent {keycode}")
        self._run_in_thread(_run)

    # ──────────────────────────────────────────
    #  截图
    # ──────────────────────────────────────────
    def _screenshot(self, e: Any = None) -> None:
        def _run() -> None:
            ok, _ = ADBTool.run_cmd(f"adb shell screencap -p /sdcard/__touch_shot__.png")
            if not ok:
                self.app.set_status("截图失败", "danger")
                self._done("截图失败", ok=False)
                return
            ok2, _ = ADBTool.run_cmd(
                f'adb pull /sdcard/__touch_shot__.png "{TOUCH_SHOT}"'
            )
            if ok2 and os.path.exists(TOUCH_SHOT):
                def _update_ui():
                    size = _png_size(TOUCH_SHOT)
                    if size:
                        self._device_w, self._device_h = size
                        if self._res_ctrl:
                            self._res_ctrl.value = f"{self._device_w}x{self._device_h}"
                    uri = _preview_data_uri(TOUCH_SHOT)
                    if uri:
                        self._img_ctrl.src = uri
                        self._img_ctrl.visible = True
                    try:
                        self._img_ctrl.update()
                        if self._res_ctrl:
                            self._res_ctrl.update()
                    except Exception:
                        pass
                    self.app.set_status(f"截图完成 {self._device_w}x{self._device_h}", "success")
                    self._done("截图完成")
                try:
                    self.app.page.run_thread(_update_ui)
                except Exception:
                    pass
            else:
                self.app.set_status("拉取截图失败", "danger")
                self._done("截图失败", ok=False)

        self._run_in_thread(_run)

    # ──────────────────────────────────────────
    #  获取分辨率
    # ──────────────────────────────────────────
    def _get_resolution(self, e: Any = None) -> None:
        def _run() -> None:
            ok, out = ADBTool.run_cmd("adb shell wm size")
            if ok and out:
                line = out.strip().split("\n")[0]
                if ":" in line:
                    line = line.split(":", 1)[1]
                parts = line.strip().split("x")
                if len(parts) == 2:
                    try:
                        self._device_w = int(parts[0].strip())
                        self._device_h = int(parts[1].strip())

                        def _update_ui():
                            self._res_ctrl.value = f"{self._device_w}x{self._device_h}"
                            try:
                                self._res_ctrl.update()
                            except Exception:
                                pass
                            self.app.set_status(f"分辨率: {self._device_w}x{self._device_h}", "success")

                        try:
                            self.app.page.run_thread(_update_ui)
                        except Exception:
                            pass
                    except Exception:
                        pass

        self._run_in_thread(_run)

    # ──────────────────────────────────────────
    #  核心功能：点击截图 → 发送 adb tap
    # ──────────────────────────────────────────
    def _on_image_tap(self, e):
        """图片点击回调：换算设备坐标并发送 ``adb shell input tap``

        坐标通道（2026-09-23 修复）：
          · ``GestureDetector.on_tap_down`` → ``e.local_x / e.local_y``（桌面端可靠）
          · 兼容旧 ``Container.on_click`` 的 ``e.data``（"x,y"，部分平台）
        换算含 **BoxFit.CONTAIN 居中留边** 修正（此前忽略留边，边缘点击会偏移）。
        """
        if not os.path.exists(TOUCH_SHOT):
            self.app.set_status("请先截图", "warning")
            return

        tap_x = tap_y = None
        pos = getattr(e, "local_position", None)          # flet 0.85：TapEvent.local_position（Offset）
        if pos is not None:
            tap_x, tap_y = pos.x, pos.y
        if tap_x is None or tap_y is None:
            tap_x = getattr(e, "local_x", None)           # 兼容旧字段名
            tap_y = getattr(e, "local_y", None)
        if tap_x is None or tap_y is None:
            try:
                parts = (getattr(e, "data", "") or "").split(",")
                tap_x, tap_y = float(parts[0]), float(parts[1])
            except Exception:  # noqa: BLE001
                self.app.set_status("未获取到点击坐标，无法发送 tap", "warning")
                return

        # 图片渲染盒（Image 固定 800x400；CONTAIN → 居中留边）
        box_w = self._img_ctrl.width or 800
        box_h = self._img_ctrl.height or 400
        scale = min(box_w / max(self._device_w, 1), box_h / max(self._device_h, 1))
        off_x = (box_w - self._device_w * scale) / 2
        off_y = (box_h - self._device_h * scale) / 2
        dev_x = int((tap_x - off_x) / scale)
        dev_y = int((tap_y - off_y) / scale)

        # 边界保护
        dev_x = max(0, min(dev_x, self._device_w))
        dev_y = max(0, min(dev_y, self._device_h))

        self._coord_ctrl.value = f"设备坐标: ({dev_x}, {dev_y})  ✓ 已发送 tap"
        self._coord_ctrl.color = self.t.c("accent")
        try:
            self._coord_ctrl.update()
        except Exception:
            pass

        # 异步发送 adb tap
        def _do_tap() -> None:
            ADBTool.run_cmd(f"adb shell input tap {dev_x} {dev_y}")

        self._run_in_thread(_do_tap)
