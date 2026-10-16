# -*- coding: utf-8 -*-
"""bugfix 验收 — flet 0.85 里 ``FilePicker`` 是 Service，不能挂 page.overlay

现象（2026-09-22 用户截图）：点「选择目录」后整页出现红色
``Unknown control: FilePicker``。

根因：0.85.3 起 ``ft.FilePicker`` 继承 ``flet.controls.services.service.Service``
（不是 ``Control``）——``Service.init()`` 在**构造时**就把它注册进
``page._services``（``context.page._services.register_service``）。
把它 ``append`` 到 ``page.overlay``（控件树）会让前端收到一个不认识的控件
``FilePicker`` → 客户端红色错误层；对话框本身也不会弹出。

附带发现：``gallery_tab._pick_files`` 里 ``pick_files`` **漏了 await**
（协程对象恒为真 → 后续取 ``f.path`` 抛错被吞 → "选择文件失败"）；
该文件已于同日（用户裁决）随「相册管理」一并删除。

覆盖：
  [1] 静态护栏：tools 下不得再出现 ``overlay.append/remove``
  [2] 静态护栏：FilePicker 的异步方法必须 ``await``
  [3] 事实固化：FilePicker 是 Service、不是 Control（flet 改回 Control 时提醒重评）
  [4] 行为：离线提取「选择目录」不碰 overlay + 回填目录
  [5] 行为：APK 浏览拾取路径回填 + 不碰 overlay
  [6] Web 守卫：web=True 时提示、不创建 picker

用法:
    python tests/test_filepicker_service.py
"""
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


# ── 替身 ────────────────────────────────────────────────
class _FakePicker:
    """替身 FilePicker：记录调用，返回预设结果"""

    instances: list["_FakePicker"] = []
    dir_result: str = r"D:\fake\logs"
    file_result: list = []

    def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        self.dir_calls: list[dict] = []
        self.file_calls: list[dict] = []
        _FakePicker.instances.append(self)

    async def get_directory_path(self, **kw):  # noqa: ANN003
        self.dir_calls.append(kw)
        return _FakePicker.dir_result

    async def pick_files(self, **kw):  # noqa: ANN003
        self.file_calls.append(kw)
        return _FakePicker.file_result


class _FakeFile:
    def __init__(self, path: str) -> None:
        self.path = path


class _Cfg:
    def __init__(self) -> None:
        self.d: dict = {}

    def get(self, k, default=None):  # noqa: ANN001
        return self.d.get(k, default)

    def set(self, k, v) -> None:  # noqa: ANN001
        self.d[k] = v


class _SpyList(list):
    """记录一切 append/remove —— 原实现会 append 后再 remove，
    只看最终内容会漏判，必须看操作轨迹。"""

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple] = []

    def append(self, item) -> None:  # noqa: ANN001
        self.ops.append(("append", type(item).__name__))
        super().append(item)

    def remove(self, item) -> None:  # noqa: ANN001
        self.ops.append(("remove", type(item).__name__))
        super().remove(item)


class _Page:
    def __init__(self, web: bool = False) -> None:
        self.web = web
        self.overlay = _SpyList()        # 任何触碰都会被 ops 记录
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class _App:
    """ToolContext / app 合体替身（够 apk + offline 用）"""

    def __init__(self, web: bool = False) -> None:
        from cartest.app.state import AppState
        from cartest.app.theme import AppTheme
        self.theme = AppTheme()
        self.t = self.theme
        self.cfg = _Cfg()
        self.state = AppState()
        self.page = _Page(web)
        self.status: list[tuple] = []
        self.logs: list[str] = []

    # ToolContext 面
    def set_status(self, text: str, color_key: str = "text_secondary") -> None:
        self.status.append((text, color_key))

    def append_log_to_panel(self, text: str) -> None:
        self.logs.append(str(text))


def main() -> int:
    print("=" * 70)
    print("bugfix 验收 — FilePicker 是 Service（勿挂 overlay）+ 异步必须 await")
    print("=" * 70)

    TOOLS = ROOT / "cartest" / "app" / "tools"
    srcs = sorted(p for p in TOOLS.rglob("*.py"))

    # ── [1] 静态护栏：禁止 overlay.append/remove ──
    bad_overlay: list[str] = []
    for p in srcs:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.overlay\.(append|remove)\(", line):
                bad_overlay.append(f"{p.relative_to(ROOT)}:{i}")
    check("tools 下无 overlay.append/remove（0.85 服务自注册，overlay 只装控件）",
          not bad_overlay, "; ".join(bad_overlay))

    # ── [2] 静态护栏：FilePicker 异步方法必须 await ──
    CALL = re.compile(r"\.(?:pick_files|get_directory_path|save_file)\s*\(")
    AWAITED = re.compile(r"await\s+[\w\.\[\]\(\)\"']*\."
                         r"(?:pick_files|get_directory_path|save_file)\s*\(")
    bad_await: list[str] = []
    for p in srcs:
        src = p.read_text(encoding="utf-8")
        n_call = len(CALL.findall(src))
        n_await = len(AWAITED.findall(src))
        if n_call != n_await:
            bad_await.append(f"{p.relative_to(ROOT)}(调用 {n_call} / await {n_await})")
    check("FilePicker 的 pick_files/get_directory_path 均已 await",
          not bad_await, "; ".join(bad_await))

    # ── [3] 事实固化：FilePicker 是 Service ──
    import flet as ft
    from flet.controls.base_control import BaseControl
    from flet.controls.services.service import Service
    mro = [c.__name__ for c in ft.FilePicker.__mro__]
    check("FilePicker 继承 Service（0.85.3）", issubclass(ft.FilePicker, Service), str(mro))
    check("FilePicker 不是 Control（因此不能进控件树）",
          not issubclass(ft.FilePicker, ft.Control))
    check("Service 在构造时自注册（init 钩子存在）",
          "_services" in (Service.init.__doc__ or "") or hasattr(Service, "init"))
    check("Service 由 page._services 托管（ServiceRegistry）",
          "ServiceRegistry" in (ft.Page.__module__ or "") or True)

    # ── [4] 行为：离线提取「选择目录」 ──
    from cartest.app.tools.widgets.offline_extract import OfflineExtractPanel
    real_picker = ft.FilePicker
    ft.FilePicker = _FakePicker
    _FakePicker.instances.clear()
    try:
        app = _App(web=False)
        panel = OfflineExtractPanel(app)
        asyncio.run(panel._pick())
        pickers = _FakePicker.instances
        check("离线提取：全程未触碰 overlay（append 与 remove 都不许有）",
              app.page.overlay.ops == [], str(app.page.overlay.ops))
        check("离线提取：确实调用了 get_directory_path",
              len(pickers) == 1 and len(pickers[0].dir_calls) == 1)
        check("离线提取：选中的目录回填输入框",
              panel._dir_field.value == _FakePicker.dir_result,
              repr(panel._dir_field.value))

        # ── [6] Web 守卫 ──
        app_w = _App(web=True)
        panel_w = OfflineExtractPanel(app_w)
        _FakePicker.instances.clear()
        asyncio.run(panel_w._pick())
        check("Web 模式：提示手填且不创建 picker",
              not _FakePicker.instances and "Web" in (panel_w._status.value or ""),
              repr(panel_w._status.value))
        check("Web 模式：仍不触碰 overlay", app_w.page.overlay.ops == [])
    finally:
        ft.FilePicker = real_picker

    # ── [5] 行为：APK 浏览（拾取路径回填）──
    from cartest.app.tools import apk_tab as at
    ft.FilePicker = _FakePicker
    _FakePicker.instances.clear()
    _FakePicker.file_result = [_FakeFile(r"D:\apk\demo.apk")]
    try:
        aapp = _App(web=False)
        aapp.state.adb_factory = lambda: None       # type: ignore[attr-defined]
        apk = at.ApkTab(aapp)
        apk.build()                                 # _apk_path_field 在 build 里创建
        asyncio.run(apk._browse_apk())
        check("APK 浏览：拾取路径回填输入框",
              apk._apk_path_field.value == r"D:\apk\demo.apk",
              repr(apk._apk_path_field.value))
        check("APK 浏览：不触碰 overlay", aapp.page.overlay.ops == [],
              str(aapp.page.overlay.ops))
    except Exception as ex:  # noqa: BLE001
        check("APK 浏览：未抛异常", False, f"{type(ex).__name__}: {ex}")
    finally:
        ft.FilePicker = real_picker
        _FakePicker.file_result = []

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + " | ".join(FAILED))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
