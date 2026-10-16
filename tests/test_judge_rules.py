# -*- coding: utf-8 -*-
"""34 验收 — 判定规则外提（P6–P8：拒识特征 / 判定文案）

``30-功能分类与可配置分离.md`` §5.4：把 judger 里写死的**规则级**项目定制点
外提为项目档案（``judge.*``），代码保留兜底；规则调整不再改代码。

覆盖：
  [1] 代码兜底 == 项目档案内置值（两处不许漂移）
  [2] 无档案/坏档案 → judger 用兜底（行为与改造前一致）
  [3] 档案覆盖拒识正则：自定义特征行也能判拒识，原特征行不再命中
  [4] 档案覆盖判定文案：judge() 输出的 reason 用新文案
  [5] 配置页 schema 暴露「判定规则」组（可编辑 + 类型正确）
  [6] 档案 load 缓存：同 mtime 不重复读盘、save 后立即生效

用法:
    python tests/test_judge_rules.py
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


def _case(query: str):
    from cartest.core.source import TestCase
    return TestCase(id="T1", query=query)


def main() -> int:
    print("=" * 70)
    print("34 验收 — 判定规则外提（judge.*）")
    print("=" * 70)

    from cartest.core import config_center, project
    from cartest.core import judger as jm

    tmp = Path(mkdtemp(prefix="judge_rules_"))
    config_center.set_settings_file(tmp / "settings.json")
    project.set_projects_dir(tmp / "projects")

    # ── [1] 兜底一致性 ──
    b = project.builtin_profile()["judge"]
    check("内置档案 deny_pattern == judger 默认",
          b["deny_pattern"] == jm.DEFAULT_DENY_PATTERN)
    check("内置档案 denial_reason == judger 默认",
          b["denial_reason"] == jm.DEFAULT_DENIAL_REASON)
    check("内置档案 messages.no_reply == judger 默认",
          b["messages"]["no_reply"] == jm.DEFAULT_MSG_NO_REPLY)
    check("无档案时 getter 返回默认",
          jm.deny_pattern() == jm.DEFAULT_DENY_PATTERN
          and jm.denial_reason() == jm.DEFAULT_DENIAL_REASON
          and jm.msg_no_reply() == jm.DEFAULT_MSG_NO_REPLY)

    # ── [2] 无档案时行为不变（原特征行仍命中、文案不变）──
    j = jm.Judger()
    j.feed_log("sparkDenial 大模型拒识 query=[导航去公司]")
    check("默认特征行 → 拒识", j._denied is True)  # noqa: SLF001
    check("默认拒识原因文案",
          j._denial_reason.startswith("车机CBM路由拒识"), j._denial_reason)  # noqa: SLF001
    check("默认无回复文案", j._msg_no_reply == jm.DEFAULT_MSG_NO_REPLY)  # noqa: SLF001

    # ── [3] 档案覆盖拒识正则 ──
    alt = project.builtin_profile()
    alt.update({"key": "alt", "name": "替身项目"})
    alt["judge"]["deny_pattern"] = r"MY_DENY_MARK|自定义拒识"
    alt["judge"]["denial_reason"] = "本项目路由拒识"
    alt["judge"]["messages"]["no_reply"] = "识别成功但未收到回复（本项目口径）"
    check("save(alt)", project.save("alt", alt) is True)
    check("set_active(alt)", project.set_active("alt") is True)
    check("getter 随档案变",
          "MY_DENY_MARK" in jm.deny_pattern()
          and jm.denial_reason() == "本项目路由拒识"
          and jm.msg_no_reply().startswith("识别成功但未收到回复"))

    j2 = jm.Judger()
    j2.feed_log("MY_DENY_MARK 触发了自定义拒识")
    check("自定义特征行 → 拒识", j2._denied is True)  # noqa: SLF001
    check("自定义拒识原因", j2._denial_reason.startswith("本项目路由拒识"),  # noqa: SLF001
          j2._denial_reason)  # noqa: SLF001
    j3 = jm.Judger()
    j3.feed_log("sparkDenial 大模型拒识 query=[旧特征]")
    check("旧特征行不再命中（规则已被替换）", j3._denied is False)  # noqa: SLF001

    # ── [4] 档案覆盖判定文案（走完整 judge()）──
    j4 = jm.Judger(timeout=0.0)
    j4.start_collection()
    j4._asr_text = "导航去公司"          # 模拟 ASR 成功但无回复
    res = j4.judge(_case("导航去公司"))
    check("judge() 用新文案", res.reason.startswith("识别成功但未收到回复"), res.reason)
    check("无回复仍判失败（口径不变）", res.passed is False)

    # 拒识场景的 reason 也随档案
    j5 = jm.Judger(timeout=0.0)
    j5.start_collection()
    j5.feed_log("MY_DENY_MARK query=[导航去公司]")
    res5 = j5.judge(_case("导航去公司"))
    check("拒识 reason 随档案", "本项目路由拒识" in res5.reason, res5.reason)

    project.set_active("plt1")
    j6 = jm.Judger()
    check("切回 plt1 后规则复原（缓存已失效）",
          j6._deny_re.pattern == jm.DEFAULT_DENY_PATTERN)  # noqa: SLF001

    # ── [5] 配置页 schema ──
    items = {i["key"]: i for i in config_center.schema()}
    check("schema 含拒识正则（TEXT）",
          items.get("project.judge.deny_pattern", {}).get("type")
          == config_center.TYPE_TEXT)
    check("schema 含无回复文案（TEXT）",
          items.get("project.judge.messages.no_reply", {}).get("type")
          == config_center.TYPE_TEXT)
    grp = {i.get("group") for i in config_center.schema()}
    check("存在「判定规则」分组", "判定规则" in grp, str(sorted(grp)))
    check("schema 默认值随档案（留空＝跟随）",
          config_center.get("project.judge.deny_pattern")
          == project.profile_value("judge.deny_pattern"))

    # ── [6] 缓存与失效 ──
    a = project.load("plt1")
    c = project.load("plt1")
    check("同 mtime 复用同一对象（缓存生效）", a is c)
    alt2 = project.builtin_profile()
    alt2.update({"key": "plt1", "name": "PLT1 改名"})
    project.save("plt1", alt2)
    check("save 后缓存失效（读到新 name）",
          project.load("plt1")["name"] == "PLT1 改名")
    # 复原 plt1 名称，避免影响生产档案
    orig = project.builtin_profile()
    orig["name"] = "PLT1"
    project.save("plt1", orig)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("判定规则外提检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
