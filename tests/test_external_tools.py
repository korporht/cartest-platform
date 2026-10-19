# -*- coding: utf-8 -*-
"""36 验收 — 外部工具目录定位（候选解析 + 可配置）

背景：外部工具目录被移动过（``D:\\BaiduNetdiskDownload\\Tools`` →
``D:\\AgentProject\\Tools``），写死路径让「离线提取」「Monkey 压测」静默不可用。
本测试钉住解析顺序与"缺失不静默"：

  [1] 候选根顺序：AgentProject/Tools 优先于 BaiduNetdiskDownload/Tools
  [2] env 单项覆盖（含旧变量 CARTEST_LOGEXTRACT_DIR 兼容）
  [3] 配置项覆盖（settings.json 的 tools.*）
  [4] marker 校验（同名空目录不算可用）
  [5] offline / monkey 已接通定位器（用 env 替身验证，**与是否安装无关**）；
      本机没装外部工具时，真机存在性检查 SKIP——不把"环境缺件"误报成回归
      （与 `tools/_legacy.py` 的 SKIP 语义、`check_all.py` 的缺失 SKIP 一致）
  [6] 配置页 schema 暴露「外部工具目录」组（TEXT）

用法:
    python tests/test_external_tools.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def skip(name: str, reason: str = "") -> None:
    """环境缺件（非回归）：打印 ``[SKIP]`` 并记录，**不计入失败**。

    外部工具目录在 ``D:\\`` 下、随环境变化（BaiduNetdiskDownload → AgentProject），
    不是每台机器都装了。断言"本机存在"会把环境缺件误报成代码回归 —— 与
    ``tools/_legacy.py`` 的 ``require()``、``check_all.py`` 对缺失项的 SKIP 语义保持一致。
    """
    print("  [SKIP] " + name + (("  " + reason) if reason else ""))
    SKIPPED.append(name)


def main() -> int:
    print("=" * 70)
    print("36 验收 — 外部工具目录定位")
    print("=" * 70)

    from cartest.core import config_center, tools_locator as tl

    tmp = Path(mkdtemp(prefix="ext_tools_"))
    config_center.set_settings_file(tmp / "settings.json")

    # ── [1] 候选根顺序 ──
    root_names = [str(p) for p in tl.ROOTS]
    check("AgentProject/Tools 优先于 BaiduNetdiskDownload/Tools",
          root_names.index(r"D:\AgentProject\Tools")
          < root_names.index(r"D:\BaiduNetdiskDownload\Tools"), str(root_names))
    cands = [str(p) for p in tl.candidates("cbm_logextractor", "CbmLogextractor")]
    check("候选含两条根的拼接",
          any(s.endswith(r"AgentProject\Tools\CbmLogextractor") for s in cands)
          and any(s.endswith(r"BaiduNetdiskDownload\Tools\CbmLogextractor") for s in cands),
          str(cands))

    # ── [2] env 覆盖（含旧变量兼容）──
    fake = tmp / "CbmLogextractor"
    (fake).mkdir(parents=True, exist_ok=True)
    (fake / "logextract.py").write_text("# stub\n", encoding="utf-8")
    os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"] = str(fake)
    got = tl.resolve("cbm_logextractor", "CbmLogextractor", marker="logextract.py")
    check("env 单项覆盖生效", got == fake, str(got))
    check("env 覆盖出现在候选首位",
          str(tl.candidates("cbm_logextractor", "CbmLogextractor")[0]) == str(fake))
    del os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"]
    os.environ["CARTEST_LOGEXTRACT_DIR"] = str(fake)
    check("旧变量 CARTEST_LOGEXTRACT_DIR 兼容",
          tl.resolve("cbm_logextractor", "CbmLogextractor",
                     "CARTEST_LOGEXTRACT_DIR", marker="logextract.py") == fake)
    del os.environ["CARTEST_LOGEXTRACT_DIR"]

    # ── [3] 配置项覆盖 ──
    check("写配置", config_center.set("tools.cbm_logextractor_dir", str(fake)) is True)
    check("配置项覆盖生效",
          tl.resolve("cbm_logextractor", "CbmLogextractor", marker="logextract.py") == fake)
    cands2 = [str(p) for p in tl.candidates("cbm_logextractor", "CbmLogextractor")]
    check("配置项排在 env 之后、候选根之前",
          cands2[0] == str(fake) and not cands2[0].endswith("Tools\\CbmLogextractor"),
          str(cands2[:2]))
    config_center.set("tools.cbm_logextractor_dir", None)

    # ── [4] marker 校验 ──
    # env/配置 = 显式指定 → 权威照办（空目录也返回它，让 available() 给出明确报错）
    empty = tmp / "empty" / "CbmLogextractor"
    empty.mkdir(parents=True, exist_ok=True)
    os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"] = str(empty)
    check("显式指定（env）权威：空目录原样返回，不静默回退",
          tl.resolve("cbm_logextractor", "CbmLogextractor", marker="logextract.py")
          == empty)
    del os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"]
    # 自动探测才做 marker 过滤：把候选根指向含空目录的临时根 → 不应命中它
    # 注意同时屏蔽 CARTEST_TOOLS_ROOT：它会被插到候选根**之前**（tools_locator._roots_probe），
    # 不屏蔽则本项在用户按 describe() 提示设过该变量时会误报。
    roots_backup = list(tl.ROOTS)
    env_root_backup = os.environ.pop("CARTEST_TOOLS_ROOT", None)
    try:
        tl.ROOTS[:] = [tmp / "empty"]
        check("自动探测：同名空目录（缺 logextract.py）被跳过",
              tl.resolve("cbm_logextractor", "CbmLogextractor",
                         marker="logextract.py") is None)
    finally:
        tl.ROOTS[:] = roots_backup
        if env_root_backup is not None:
            os.environ["CARTEST_TOOLS_ROOT"] = env_root_backup

    # ── [5] 消费方接通 ──
    # 拆两层：**接通性**（消费方是否走了 tools_locator）用 env 替身验证，与本机是否
    # 装了外部工具无关；**真机存在性**才需要工具在场，缺件则 SKIP。
    from cartest.core.parser import offline
    from cartest.core import monkey

    # (a) 接通性：env 指向替身目录 → 消费方必须跟过去（同时验证 available() 认入口文件）
    stub_off = tmp / "stub" / "CbmLogextractor"
    stub_off.mkdir(parents=True, exist_ok=True)
    (stub_off / "logextract.py").write_text("# stub\n", encoding="utf-8")
    os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"] = str(stub_off)
    try:
        check("offline 接通定位器（env 指向替身即命中）",
              offline.tool_dir() == stub_off and offline.available() is True,
              str(offline.tool_dir()))
    finally:
        del os.environ["CARTEST_CBM_LOGEXTRACTOR_DIR"]

    stub_mk = tmp / "stub" / "Monkey 自动化测试"
    stub_mk.mkdir(parents=True, exist_ok=True)
    (stub_mk / "crash_anr_analyzer.py").write_text("# stub\n", encoding="utf-8")
    (stub_mk / "monkey_runner.sh").write_text("# stub\n", encoding="utf-8")
    os.environ[monkey.TOOL_DIR_ENV] = str(stub_mk)
    try:
        check("monkey 接通定位器（env 指向替身即命中）",
              monkey.tool_dir() == stub_mk and monkey.available() is True,
              str(monkey.tool_dir()))
    finally:
        del os.environ[monkey.TOOL_DIR_ENV]

    # (b) 真机存在性：本机没装就 SKIP（不计失败），避免"环境缺件"被读成"回归"
    d_off = offline.tool_dir()
    if offline.available():
        check("offline.tool_dir 命中真实位置",
              d_off.is_dir() and (d_off / "logextract.py").is_file(), str(d_off))
    else:
        skip("offline.tool_dir 命中真实位置",
             f"{d_off}（本机未安装 CbmLogextractor）")
    d_mk = monkey.tool_dir()
    if monkey.available():
        check("monkey.tool_dir 命中真实位置", d_mk.is_dir(), str(d_mk))
    else:
        skip("monkey.tool_dir 命中真实位置",
             f"{d_mk}（本机未安装 Monkey 自动化测试）")

    check("describe 给出候选与覆盖方式（不静默）",
          "候选" in tl.describe("cbm_logextractor", "CbmLogextractor")
          and "CARTEST_CBM_LOGEXTRACTOR_DIR" in
          tl.describe("cbm_logextractor", "CbmLogextractor"))

    # ── [6] 配置页 schema ──
    items = {i["key"]: i for i in config_center.schema()}
    check("schema 含 tools.cbm_logextractor_dir（TEXT）",
          items.get("tools.cbm_logextractor_dir", {}).get("type")
          == config_center.TYPE_TEXT)
    check("schema 含 tools.monkey_tool_dir（TEXT）",
          items.get("tools.monkey_tool_dir", {}).get("type")
          == config_center.TYPE_TEXT)
    groups = {i.get("group") for i in config_center.schema()}
    check("存在「外部工具目录」分组", "外部工具目录" in groups, str(sorted(groups)))

    print()
    print("=" * 70)
    if SKIPPED:
        print(f"SKIP {len(SKIPPED)} 项（本机未安装外部工具——环境缺件，非回归）:")
        for n in SKIPPED:
            print(f"  - {n}")
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("外部工具目录定位检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
