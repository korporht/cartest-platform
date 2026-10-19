# -*- coding: utf-8 -*-
"""配置页实时保存验收（2026-10-15：取消「保存」按钮）

用户裁决：*"取消配置页的保存按钮，用户点击配置开关能实时保存"*——原 P3d-3 的
「显式保存 + 加载缓冲 + 弹窗」语义作废（保留「恢复默认」）。

覆盖（行为级，替身注入 settings.PathListEditor / config_center / project）：
  1) 开关改动 → 一次 tick 即落盘 + 状态行 + 绿色轻提示
  2) 文本改动 → 单键写盘（只写改动的那一个键）
  3) 校验失败 → 不落盘、行内红字 + 状态行红字、**不弹**轻提示
  4) 写盘失败 → 状态行红字（不崩）
  5) project.* → 单键走 save_values（不写 settings）
  6) 离开页面（before_page_leave）→ 未结算改动强制落盘
  7) 「恢复默认」保留：忙碌（恢复中…）→ 回显 → 基线同步（之后 tick 不再重复写）
  8) 防抖（类级，假时钟）：改动期间不写、停手 settle 秒后只写一次；改回原值不写
  9) 源码断言：无保存按钮 / 无「保存中…」/ 有即时保存文案 + tick 钩子 + flush 装配

用法:
    python tests/test_settings_feedback.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flet as ft  # noqa: E402

from cartest.app.pages import settings as settings_mod  # noqa: E402
from cartest.app.ui import toast as toast_mod  # noqa: E402

SRC = (ROOT / "cartest" / "app" / "pages" / "settings.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeTheme:
    COLORS = {"success": "#00AA00", "danger": "#CC0000"}

    def c(self, key: str) -> str:
        return self.COLORS.get(key, "#333333")


class FakePage:
    def __init__(self) -> None:
        self.opened: list = []
        self.tasks: list = []            # Toast 自动隐藏定时器（不执行）
        self.busy_snap: dict | None = None
        self.run_thread_calls = 0
        self.buttons: dict = {}
        self.overlay: list = []

    def update(self) -> None:
        pass

    def show_dialog(self, dialog) -> None:  # noqa: ANN001
        self.opened.append(dialog)

    def run_task(self, handler, *args, **kwargs) -> None:  # noqa: ANN001
        self.tasks.append(handler)

    def run_thread(self, handler, *args, **kwargs) -> None:  # noqa: ANN001
        self.run_thread_calls += 1
        self.busy_snap = {name: (btn.disabled, btn_label(btn))
                          for name, btn in self.buttons.items()}
        handler(*args, **kwargs)


class FakeEditor:
    """path_list 替身（其余类型走真实编辑器：headless 可构造）

    ``on_change``：路径行开关/增删行的即时落盘回调（2026-10-15）——替身保存下来，
    测试里 fire() 模拟"点了开关"。
    """
    instances: list = []

    def __init__(self, value, on_change=None) -> None:  # noqa: ANN001
        self.value = value
        self.validate_ok = True
        self.set_values: list = []
        self._on_change = on_change
        FakeEditor.instances.append(self)

    def fire(self) -> None:
        """模拟点击类改动（开关 / 增删行）"""
        if self._on_change:
            self._on_change()

    def build(self):
        return ft.Container(content=ft.Text("fake-editor"))

    def validate(self) -> str:
        return "" if self.validate_ok else "路径重复：/a 与 /b"

    def get_value(self):
        return self.value

    def set_value(self, value) -> None:  # noqa: ANN001
        self.value = value
        self.set_values.append(value)


DEFAULT_VALUE = [{"path": "/default", "alias": "d", "enabled": True}]


class FakeCC:
    SETTINGS_FILE = Path("X:/fake/settings.json")
    TYPE_PATH_LIST = "path_list"
    TYPE_BOOL = "bool"
    TYPE_INT = "int"
    TYPE_TEXT = "text"
    TYPE_TEXTAREA = "textarea"
    TYPE_TAGS = "tags"

    def __init__(self) -> None:
        self.set_calls: list = []
        self.reset_calls: list = []
        self.set_ok = True
        self.values = {
            "pull_paths": [{"path": "/a", "alias": "a", "enabled": True}],
            "log.window_secs": "10",
            "log.event.enabled": True,
            "project.device.main_package": "com.fake.car",
        }

    def schema(self):
        return [
            {"key": "pull_paths", "group": "日志抓取", "type": "path_list", "desc": "d"},
            {"key": "log.window_secs", "group": "日志抓取", "type": "text",
             "desc": "时间窗（秒）", "placeholder": "留空＝默认"},
            {"key": "log.event.enabled", "group": "事件窗口", "type": "bool", "desc": "总开关"},
            # 32 号：project.* 值不走 settings，改由 save_values 写进方案文件
            {"key": "project.device.main_package", "group": "车机应用",
             "type": "text", "desc": "主机包名"},
        ]

    def get(self, key: str):  # noqa: ANN201
        return self.values.get(key)

    def set(self, key: str, value) -> bool:  # noqa: ANN001
        if not self.set_ok:
            return False
        self.set_calls.append((key, value))
        self.values[key] = value
        return True

    def reset(self, key: str) -> bool:
        self.reset_calls.append(key)
        self.values[key] = DEFAULT_VALUE
        return True


class FakeProject:
    """替身配置方案模块（32 号：卡片 = 下拉 + 新增/重命名/删除 + 恢复默认）"""

    BUILTIN_KEY = "e0v"
    DEFAULT_LABEL = "默认（内置）"
    MAX_SCHEMES = 5
    saved_flat: list = []

    @staticmethod
    def list_schemes():
        return []

    @staticmethod
    def scheme_count() -> int:
        return 0

    @staticmethod
    def active_key() -> str:
        return "e0v"

    @staticmethod
    def set_active(key: str) -> bool:
        return True

    @staticmethod
    def scheme_devices(key: str) -> list:      # noqa: ARG004 — 2026-10-19 设备绑定
        return []

    @staticmethod
    def bind_device(key: str, serial: str):    # noqa: ANN201 — 设备绑定
        return True, key

    @staticmethod
    def unbind_device(key: str, serial: str):  # noqa: ANN201 — 设备绑定
        return True, key

    @staticmethod
    def alias_for_serial(serial: str) -> str:  # noqa: ARG004
        return ""

    @staticmethod
    def create_scheme(name: str):  # noqa: ANN201
        return True, "s1"

    @staticmethod
    def rename_scheme(key: str, name: str):  # noqa: ANN201
        return True, name

    @staticmethod
    def remove_scheme(key: str):  # noqa: ANN201
        return True, "e0v"

    @staticmethod
    def save_values(flat: dict):  # noqa: ANN201
        FakeProject.saved_flat.append(dict(flat))
        return True, "e0v"

    @staticmethod
    def projects_dir():  # noqa: ANN201
        return Path(".")

    @staticmethod
    def profile_value(path: str):  # noqa: ANN201
        return None


def walk(control):  # noqa: ANN001, ANN201
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from walk(content)
    controls = getattr(control, "controls", None)
    if controls:
        for child in controls:
            yield from walk(child)


def toast_text(host) -> str:  # noqa: ANN001
    """Toast 文案（ToastHost 内层胶囊的 Text）"""
    return host._text.value or ""


def btn_label(btn) -> str:  # noqa: ANN001
    """按钮文案（flet 0.85：文案存于 content；忙碌态为 Row[ProgressRing, Text]）"""
    content = getattr(btn, "content", None)
    if isinstance(content, str):
        return content
    controls = getattr(content, "controls", None)
    if controls:
        return "".join(c.value for c in controls if isinstance(c, ft.Text))
    return ""


def build_page(settle: float = 0.0):  # noqa: ANN201
    """建页并返回 (page, cc, root, status, host, tick, err_texts)"""
    FakeEditor.instances.clear()
    FakeProject.saved_flat.clear()
    page = FakePage()
    cc = FakeCC()
    settings_mod.config_center = cc
    settings_mod.PathListEditor = FakeEditor
    settings_mod.project_mod = FakeProject
    settings_mod._MIN_BUSY_MS = 5
    settings_mod._TEXT_SETTLE_SEC = settle      # 页面级测试默认用「立即」，防抖另测
    state = SimpleNamespace(theme=FakeTheme(), tick_hooks=[], before_page_leave=None)
    host = toast_mod.attach(page, state.theme)
    root = settings_mod.build(page, state)
    nodes = list(walk(root))
    resets = [c for c in nodes
              if isinstance(c, ft.TextButton) and c.content == "恢复默认"]
    page.buttons = {"reset": resets[1], "reset_project": resets[0]}
    status = next(c for c in nodes
                  if isinstance(c, ft.Text) and c.key == "settings_status")
    tick = state.tick_hooks[0]
    err_texts = {}
    # 行内错误：按 schema 顺序（每组末尾一个红字 Text）——用值为空的 danger 色 Text 收集
    for c in nodes:
        if isinstance(c, ft.Text) and c.value == "" and c.color == "#CC0000":
            err_texts.setdefault(len(err_texts), c)
    return page, cc, root, status, host, tick, err_texts, state, nodes


def find_switch(nodes):  # noqa: ANN001, ANN201
    return next(c for c in nodes if isinstance(c, ft.Switch))


def find_textfield(nodes, value: str):  # noqa: ANN001, ANN201
    return next(c for c in nodes
                if isinstance(c, ft.TextField) and (c.value or "") == value)


def main() -> int:
    print("=" * 70)
    print("配置页实时保存验收（2026-10-15：取消「保存」按钮）")
    print("=" * 70)

    print("\n[1] 开关改动 → 一次 tick 即落盘 + 状态行 + 绿色轻提示")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    sw = find_switch(nodes)
    sw.value = False                       # 关掉「事件窗口 · 总开关」
    tick(1)
    check("开关改动立即写盘", ("log.event.enabled", False) in cc.set_calls, str(cc.set_calls))
    check("状态行显示已保存（含时间）",
          "已保存" in (status.value or ""), status.value or "")
    check("成功为绿色状态", status.color == "#00AA00", str(status.color))
    check("开关类给轻提示（Toast 可见）",
          host.control.visible is True and "已保存" in toast_text(host),
          f"visible={host.control.visible}, text={toast_text(host)}")
    check("无行内错误", not any(t.value for t in errs.values()),
          str([t.value for t in errs.values()]))

    print("\n[2] 文本改动 → 单键写盘（只写改动的那一个键）")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    cc.set_calls.clear()
    find_textfield(nodes, "10").value = "20"
    tick(1)
    check("文本键落盘", cc.set_calls == [("log.window_secs", "20")], str(cc.set_calls))
    check("未改动的键不被重写（无 pull_paths）",
          all(k != "pull_paths" for k, _ in cc.set_calls), str(cc.set_calls))
    check("文本类不弹轻提示（避免打字骚扰）",
          not (host.control.visible is True and "已保存" in toast_text(host)),
          f"visible={host.control.visible}, text={toast_text(host)}")

    print("\n[3] 校验失败 → 不落盘 + 行内红字 + 状态行红字、不弹提示")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    FakeEditor.instances[0].validate_ok = False
    FakeEditor.instances[0].value = [{"path": "/x", "alias": "x", "enabled": True}]
    tick(1)
    check("未写盘", cc.set_calls == [], str(cc.set_calls))
    check("状态行红字含未保存原因",
          "未保存" in (status.value or "") and "路径重复" in (status.value or ""),
          status.value or "")
    check("状态为红色", status.color == "#CC0000", str(status.color))
    check("行内出现同一错误", any("路径重复" in (t.value or "") for t in errs.values()),
          str([t.value for t in errs.values()]))
    check("不弹轻提示", not (host.control.visible is True and toast_text(host)),
          f"visible={host.control.visible}, text={toast_text(host)}")

    print("\n[4] 写盘失败 → 状态行红字（不崩）")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    cc.set_ok = False
    find_textfield(nodes, "10").value = "20"
    tick(1)
    check("状态行红字提示写入出错",
          status.color == "#CC0000" and "写入" in (status.value or ""), status.value or "")

    print("\n[5] project.* → 单键走 save_values（不写 settings）")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    cc.set_calls.clear()
    find_textfield(nodes, "com.fake.car").value = "com.new.car"
    tick(1)
    check("project.* 单键写入方案文件",
          FakeProject.saved_flat == [{"project.device.main_package": "com.new.car"}],
          str(FakeProject.saved_flat))
    check("project.* 不进 settings", cc.set_calls == [], str(cc.set_calls))

    print("\n[6] 离开页面 flush → 未结算改动强制落盘（防抖中也不丢）")
    page, cc, root, status, host, tick, errs, state, nodes = build_page(settle=10.0)
    find_textfield(nodes, "10").value = "30"
    tick(1)
    check("防抖未到 → 暂未落盘", cc.set_calls == [], str(cc.set_calls))
    state.before_page_leave()
    check("flush 后落盘", ("log.window_secs", "30") in cc.set_calls, str(cc.set_calls))

    print("\n[6b] 点击类改动（开关/增删行）→ 不等防抖，下一次 tick 立即落盘 + 轻提示")
    page, cc, root, status, host, tick, errs, state, nodes = build_page(settle=10.0)
    ed = FakeEditor.instances[0]
    cc.set_calls.clear()
    ed.value = [{"path": "/a", "alias": "a", "enabled": False}]   # 开关关掉
    ed.fire()                                                    # 模拟点开关
    tick(1)
    check("开关改动即时落盘（settle 未到也写）",
          ("pull_paths", [{"path": "/a", "alias": "a", "enabled": False}]) in cc.set_calls,
          str(cc.set_calls))
    check("点击类改动给轻提示",
          host.control.visible is True and "已保存" in toast_text(host),
          f"visible={host.control.visible}, text={toast_text(host)}")

    print("\n[7] 「恢复默认」保留：忙碌 → 回显 → 基线同步（不再重复写）")
    page, cc, root, status, host, tick, errs, state, nodes = build_page()
    editor = FakeEditor.instances[0]
    page.buttons["reset"].on_click(None)
    check("reset 被调用", cc.reset_calls == ["pull_paths"], str(cc.reset_calls))
    check("忙碌时禁用且文案=恢复中…",
          page.busy_snap is not None and page.busy_snap["reset"] == (True, "恢复中…"),
          str(page.busy_snap))
    check("编辑器回显默认值",
          bool(editor.set_values) and editor.set_values[-1] == DEFAULT_VALUE,
          str(editor.set_values))
    cc.set_calls.clear()
    tick(1)
    check("恢复后 tick 不再重复写（基线已同步）", cc.set_calls == [], str(cc.set_calls))

    print("\n[8] 防抖（类级，假时钟）：改动期间不写、停手后只写一次；改回原值不写")
    clock = {"t": 100.0}
    sent: list = []

    class _Ed:
        def __init__(self, v) -> None:  # noqa: ANN001
            self.v = v

        def get_value(self):  # noqa: ANN201
            return self.v

        def validate(self) -> str:
            return ""

    ed = _Ed("a")
    saver = settings_mod._AutoSaver(
        {"k": ed}, {"k": "text"},
        lambda k, v: (sent.append((k, v)), (True, "已保存"))[1],
        lambda *a: None, settle=0.5, now=lambda: clock["t"])
    saver.poll()
    check("无改动 → 不写", sent == [], str(sent))
    ed.v = "b"
    saver.poll()                       # 起算
    clock["t"] += 0.3
    saver.poll()                       # 未到 0.5s
    check("防抖窗口内不写", sent == [], str(sent))
    ed.v = "c"
    saver.poll()                       # 重新计时
    clock["t"] += 0.4
    saver.poll()
    check("继续改动会重置计时（仍不写）", sent == [], str(sent))
    clock["t"] += 0.2                  # 距最后一次改动 0.6s
    saver.poll()
    check("停手 settle 秒后写一次且值为最新", sent == [("k", "c")], str(sent))
    ed.v = "c"
    clock["t"] += 5.0
    saver.poll()
    check("值未再变 → 不再写", sent == [("k", "c")], str(sent))
    ed.v = "d"
    saver.poll()
    ed.v = "c"                         # 改回已保存值
    clock["t"] += 5.0
    saver.poll()
    check("改回已保存值 → 不写", sent == [("k", "c")], str(sent))

    # mark_instant：点击类改动 → 下一次 poll 立即写（settle 再大也不等）
    ed.v = "e"
    saver.mark_instant("k")
    saver.poll()
    check("mark_instant → 下一次 poll 立即写", sent == [("k", "c"), ("k", "e")], str(sent))
    ed.v = "e"
    clock["t"] += 9.0
    saver.poll()
    check("即时写后不重复写", len(sent) == 2, str(sent))

    print("\n[9] 源码断言")
    check("已无「保存」按钮（ElevatedButton/Save 图标）",
          "ft.ElevatedButton" not in SRC and "Icons.SAVE" not in SRC)
    check("文案说明改动即时保存",
          "改动即时保存" in SRC and "修改后点击「保存」" not in SRC)
    check("自动保存挂 tick + 离开页 flush",
          "tick_hooks" in SRC and "before_page_leave = _saver.flush" in SRC)
    check("开关/下拉走即时档（INSTANT_KINDS）",
          'INSTANT_KINDS = ("bool", "choice")' in SRC)
    check("防抖常量存在且 >=0.3s",
          "_TEXT_SETTLE_SEC = 0.5" in SRC)
    check("「恢复默认」仍走加载缓冲（ProgressRing 保留）",
          "ProgressRing" in SRC and "恢复中…" in SRC)
    check("提示走统一 toast 入口", "toast_ui.toast(" in SRC)
    check("settings 不再自建 SnackBar（且无 page.open）",
          "SnackBar(" not in SRC and "page.open(" not in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("配置页实时保存检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
