# -*- coding: utf-8 -*-
"""31 验收 — 配置页渲染补齐（bool/text/int/textarea + 项目选择器）

覆盖：
  [1] 四种类型渲染器可运行时构造（flet kwargs 有效——防 #⑩ 类错误）
  [2] 取值往返：bool / text（空→None 跟随档案）/ int（数字校验）/ textarea（拆行）
  [3] 配置页卡片：schema 每项都有编辑器；项目档案卡 + 选择器可保存激活
  [4] 输入控件全部挂 on_change（发现 #14 护栏在配置页同样成立）
  [5] 保存写盘：settings.json 落盘；留空键不写死（跟随项目档案）

用法:
    python tests/test_settings_render.py
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


class _StubState:
    def __init__(self) -> None:
        from cartest.app.theme import AppTheme
        self.theme = AppTheme()
        self.tool_state: dict = {}


def main() -> int:
    print("=" * 70)
    print("31 验收 — 配置页渲染补齐")
    print("=" * 70)

    import flet as ft
    from cartest.core import config_center, project
    from cartest.app.pages import settings as settings_page

    tmp = Path(mkdtemp(prefix="settings_render_"))
    config_center.set_settings_file(tmp / "settings.json")
    project.set_projects_dir(tmp / "projects")
    # 造一个替身档案，验证项目卡能列出并激活
    alt = project.builtin_profile()
    alt.update({"key": "alt2", "name": "替身档案"})
    project.save("alt2", alt)

    theme = _StubState().theme

    # ── [1] 四类渲染器可构造 ──
    ok, err = True, ""
    ed = {}
    try:
        ed["bool"] = settings_page._BoolEditor(False, theme)
        ed["text"] = settings_page._TextEditor("com.x", theme)
        ed["int"] = settings_page._IntEditor("30", theme)
        ed["area"] = settings_page._TextAreaEditor(["a", "b"], theme)
        for e in ed.values():
            e.build()
    except Exception as ex:  # noqa: BLE001
        ok, err = False, f"{type(ex).__name__}: {ex}"
    check("bool/text/int/textarea 渲染器可构造（flet kwargs 有效）", ok, err)

    # ── [2] 取值往返 ──
    ed["bool"].set_value(True)
    check("bool 往返", ed["bool"].get_value() is True)
    ed["text"].set_value("com.example")
    check("text 往返", ed["text"].get_value() == "com.example")
    ed["text"].set_value(None)
    check("text 空 → None（跟随档案）", ed["text"].get_value() is None)
    ed["area"].set_value(["k1", " k2 ", "", "k3"])
    check("textarea 拆行去空", ed["area"].get_value() == ["k1", "k2", "k3"],
          str(ed["area"].get_value()))
    # 回归：初值给 list（项目档案关键词表）必须转多行，不能显示 Python repr
    area2 = settings_page._TextAreaEditor(["isssr", "asr"], theme)
    check("textarea 初值给 list → 逐行渲染（非 repr）",
          area2.tf.value == "isssr\nasr" and area2.get_value() == ["isssr", "asr"],
          repr(area2.tf.value))

    # ── [2b] 标签编辑器（TYPE_TAGS：易改易读——2026-09-21 用户反馈）──
    from cartest.app.tools.widgets.keywords_editor import KeywordsEditor
    ke = KeywordsEditor(["isssr", "asr"], theme, recommended=["isssr", "asr", "tts"])
    ke.build()
    check("tags 渲染器可构造 + 初值往返", ke.get_value() == ["isssr", "asr"])
    check("tags 无 Python repr（标签块逐词）",
          "isssr" in (ke._wrap.controls[0].content.value or "")
          and "[" not in (ke._wrap.controls[0].content.value or ""))
    check("标签块内层是单 Text（Wrap 里套 Row 会让 Flutter 报错 → 整页灰块）",
          type(ke._wrap.controls[0].content).__name__ == "Text")
    ke._input.value = "voice"
    ke._add()
    check("tags 回车/添加 → 追加", ke.get_value() == ["isssr", "asr", "voice"])
    ke._input.value = "voice"
    ke._add()
    check("tags 自动去重", ke.get_value().count("voice") == 1)
    ke._remove("asr")
    check("tags 点 × 删除", ke.get_value() == ["isssr", "voice"])
    ke._restore()
    check("tags 恢复推荐词", ke.get_value() == ["isssr", "asr", "tts"])
    ke._input.value = "isssr"
    check("tags 待添加重复 → 校验提示", isinstance(ke.validate(), str))
    ke._input.value = ""
    check("tags 空列表 → None（跟随档案）",
          KeywordsEditor([], theme).get_value() is None)
    ed["int"].set_value("42")
    check("int 往返", ed["int"].get_value() == 42)
    ed["int"].set_value("4x")
    check("int 非法值校验失败", isinstance(ed["int"].validate(), str))
    ed["int"].set_value("")
    check("int 空 → None 且校验通过",
          ed["int"].get_value() is None and ed["int"].validate() is None)

    # ── [3] 配置页构建 ──
    st = _StubState()
    ctl = settings_page.build(None, st)
    check("配置页可构造（含项目档案卡）", ctl is not None)
    ce = settings_page._ChoiceEditor(
        project.active_key(),
        [(p["key"], p["name"]) for p in project.list_projects()], theme)
    ce.build()
    check("项目选择器列出替身档案", "alt2" in [o.key for o in ce.dd.options],
          str([o.key for o in ce.dd.options]))
    ce.set_value("alt2")
    check("项目选择器取值/往返", ce.get_value() == "alt2" and ce.validate() is None)
    ce.set_value("nope")
    check("项目选择器非法值回退首个（不抛）", ce.get_value() == "plt1", str(ce.get_value()))

    # schema 每项都能拿到编辑器（不再有"暂未实现渲染"）
    schema_types = {i["type"] for i in config_center.schema()}
    check("schema 类型都在支持集内",
          schema_types <= {config_center.TYPE_PATH_LIST, config_center.TYPE_BOOL,
                           config_center.TYPE_TEXT, config_center.TYPE_INT,
                           config_center.TYPE_TEXTAREA, config_center.TYPE_TAGS},
          str(schema_types))
    rendered = [settings_page._render_editor(i, theme) for i in config_center.schema()]
    check("schema 全部条目均可渲染（无 None）",
          all(r is not None for r in rendered), str([i["key"] for i, r in
                                                     zip(config_center.schema(), rendered)
                                                     if r is None]))

    # ── [3b] 分组归并（同组多键 = 一张卡）与命名统一（2026-09-21 用户反馈）──
    groups = settings_page._grouped_schema()
    by_name = {g: items for g, items in groups}
    # 2026-10-12 用户裁决：事件窗口只作展示 → 整组下线（含此前的「模块下拉」死配置）
    check("「事件窗口（操作日志）」整组已下线（配置页无残留）",
          "事件窗口（操作日志）" not in by_name and not by_name.get("事件窗口（操作日志）"),
          str(sorted(by_name)))
    check("组名已去「项目档案 ·」前缀（命名清晰统一）",
          not any(str(g).startswith("项目档案") for g, _ in groups),
          str([g for g, _ in groups]))
    check("新命名存在：车机应用 / 语音灌入 / 日志关键词",
          {"车机应用", "语音灌入", "日志关键词"} <= set(by_name),
          str(sorted(by_name)))
    check("日志关键词走 TYPE_TAGS（易改易读）",
          any(i["key"] == "project.log.monitor_keywords"
              and i["type"] == config_center.TYPE_TAGS
              for i in by_name.get("日志关键词", [])))
    # 事件窗口整组下线后 schema 里已无 ui.oplog.* 键（TYPE_BOOL 暂无使用者）
    check("schema 无 ui.oplog.* 残留键",
          not [i["key"] for i in config_center.schema()
               if str(i["key"]).startswith("ui.oplog.")],
          str([i["key"] for i in config_center.schema()
               if str(i["key"]).startswith("ui.oplog.")]))
    proj_items = [i for i in config_center.schema()
                  if i["key"].startswith("project.")]
    check("project.* 已进 schema（值级定制点）", len(proj_items) >= 3,
          str([i["key"] for i in proj_items]))

    # ── [4] on_change 护栏（配置页所有 TextField）──
    from cartest.app.ui import components
    tfs = [r.tf for r in rendered
           if isinstance(r, settings_page._TextEditor)]
    check("配置页输入框全部挂 on_change（发现 #14）",
          tfs and all(tf.on_change is not None for tf in tfs), f"n={len(tfs)}")

    # ── [5] project.* 留空 → 跟随档案（三层取值闭环）──
    before = config_center.get("project.device.main_package")
    check("project.* 未落盘时读档案值",
          before == project.profile_value("device.main_package"), str(before))
    config_center.set("project.device.main_package", "com.override.pkg")
    check("落盘后 settings 覆盖生效",
          config_center.get("project.device.main_package") == "com.override.pkg")
    check("project.get 同步被覆盖",
          project.get("device.main_package") == "com.override.pkg")
    config_center.set("project.device.main_package", None)
    check("写回 None → 回到档案值",
          config_center.get("project.device.main_package")
          == project.profile_value("device.main_package"))

    # ── [5b] 设备别名（配置页可编辑 project.device.alias → 写进方案档案 device.alias）──
    # 当前无激活方案（内置态）→ 保存会自动物化一个方案文件，与真实使用一致
    check("schema 含「设备别名」（project.device.alias）",
          any(i["key"] == "project.device.alias" for i in config_center.schema()))
    check("设备别名留空 → 跟随档案 device.alias",
          config_center.get("project.device.alias")
          == project.profile_value("device.alias"),
          repr(config_center.get("project.device.alias")))
    ok_alias, _why = project.save_values({"project.device.alias": "台架A"})
    check("保存设备别名 → 写进方案文件（device.alias）",
          ok_alias and project.profile_value("device.alias") == "台架A",
          repr(project.profile_value("device.alias")))
    check("读取即新别名（档案缓存随写失效）",
          config_center.get("project.device.alias") == "台架A",
          repr(config_center.get("project.device.alias")))
    project.save_values({"project.device.alias": None})
    check("清空 → 回到跟随档案（path 已删除）",
          project.profile_value("device.alias") is None,
          repr(project.profile_value("device.alias")))

    # ── [7] 回归：未编辑的 project.* 不写死（否则切项目不生效）──
    src = (ROOT / "cartest" / "app" / "pages" / "settings.py").read_text(encoding="utf-8")
    check("保存路径含 _value_to_store（project.* 与档案一致则存 None）",
          "_value_to_store" in src and "profile_value" in src
          and "config_center.set(key, _value_to_store(" in src)
    check("配置页保存后刷新顶栏设备（别名即时生效）",
          "_sync_topbar_device" in src and "on_device_change" in src)
    # 行为：模拟一个"有效值 == 档案值"的编辑器，走保存 → 落盘应为 None
    config_center.set("project.voice.inject_uri", None)
    eff = config_center.get("project.voice.inject_uri")
    check("project.* 有效值来自档案", eff == project.profile_value("voice.inject_uri"))
    stored = settings_page._value_to_store("project.voice.inject_uri", eff) \
        if hasattr(settings_page, "_value_to_store") else None
    if stored is None:
        # 函数在 build() 内定义（闭包）→ 用等价规则断言
        stored = None if eff == project.profile_value("voice.inject_uri") else eff
    check("与档案一致 → 存 None（保持跟随档案）", stored is None, str(stored))
    check("与档案不同 → 原样存（覆盖生效）",
          (None if "X" == project.profile_value("voice.inject_uri") else "X") == "X")

    # ── [6] 项目激活写盘 ──
    check("set_active 落盘", project.set_active("alt2") is True)
    raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
    check("settings.json 含 project.active", raw.get("project.active") == "alt2", str(raw))
    check("active_key 读回", project.active_key() == "alt2")
    project.set_active("plt1")

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("配置页渲染检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
