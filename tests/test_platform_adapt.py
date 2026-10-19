# -*- coding: utf-8 -*-
"""平台适配测试（2026-10-09）— T28/T1MA（desaysv 8255 双台架）

设计依据：``37-T28-T1MA平台支持设计.md`` §六 验收标准。

覆盖：
  [1] 文件过滤 glob（默认 main_* == 旧行为；T28 模式 logcat.txt/*.txt.gz）
  [2] 流式分片聚合（**真实日志夹具**：appendText + 20204 nlp → 完整回复）
  [3] fragments 默认关（E0V 零漂移：20204/appendText 不产生回复记录）
  [4] 多会话交错不串（按 sid / 静默分段）
  [5] 静默边界与重叠去重（合成样本）
  [6] 拒识规则随档案（T28：handle sparkDenial 不误报；cbm_denial 真报；内置对比）
  [7] 预置档案契约（t28.json / t1ma.json 关键字段钉死）

用法:
    python tests/test_platform_adapt.py
"""
import gzip
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core import config_center, project  # noqa: E402
from cartest.core.judger import Judger, default_timeout as judge_default_timeout  # noqa: E402
from cartest.core.parser import live as parser_live  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HELLO_LOG = FIXTURES / "t28_hello_window.log"
YEAR = 2026

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    tmp = Path(mkdtemp(prefix="platform_adapt_"))
    config_center.set_settings_file(tmp / "settings.json")   # 隔离，不碰真实 settings

    print("=" * 70)
    print("平台适配测试 — T28/T1MA（desaysv 8255）")
    print("=" * 70)

    # ── [1] 文件过滤 glob ──
    print("\n[1] 文件过滤 glob")
    d = tmp / "logs"
    d.mkdir()
    (d / "main_a.log").write_text("x\n", encoding="utf-8")
    (d / "logcat.txt").write_text("y\n", encoding="utf-8")
    (d / "002_x.txt.gz").write_bytes(gzip.compress(b"z\n"))
    (d / "other.json").write_text("{}\n", encoding="utf-8")

    def names(ps):
        return sorted(p.name for p in ps)

    check("默认 == main_*（旧行为逐字保持）",
          names(parser_live.iter_log_files(str(d))) == ["main_a.log"])
    check("T28 模式（logcat.txt + *.txt.gz）",
          names(parser_live.iter_log_files(str(d), ["logcat.txt", "*.txt.gz"]))
          == ["002_x.txt.gz", "logcat.txt"])
    check("不误纳其他文件（.json）",
          "other.json" not in names(parser_live.iter_log_files(str(d), ["logcat.txt", "*.txt.gz"])))

    # ── [2] 流式分片聚合（真实日志夹具）──
    print("\n[2] 流式分片聚合（T28 真实日志夹具）")
    check("夹具存在", HELLO_LOG.is_file(), str(HELLO_LOG))
    matched, _ = parser_live.parse_logs(str(FIXTURES), ["t28_hello_window.log"], fragments=True)
    hits = [m for m in matched if m.query == "你好"]
    dom = next((m.domain for m in hits if m.domain), "")
    rep = next((m.reply for m in hits if m.reply), "")
    check("落域=其他技能", dom == "其他技能", dom)
    check("回复完整（首尾均在）",
          "你好呀" in rep and "都可以随时和我说" in rep, repr(rep[:70]))

    # ── [3] fragments 默认关（E0V 零漂移）──
    print("\n[3] fragments 默认关（E0V 零漂移保证）")
    matched0, _ = parser_live.parse_logs(str(FIXTURES), ["t28_hello_window.log"])
    hits0 = [m for m in matched0 if m.query == "你好"]
    dom0 = next((m.domain for m in hits0 if m.domain), "")
    rep0 = next((m.reply for m in hits0 if m.reply), "")
    check("默认不提取分片回复", "你好呀" not in rep0, repr(rep0[:50]))
    check("落域仍可提取（与分片开关无关）", dom0 == "其他技能")

    # ── [4] 多会话交错不串 ──
    print("\n[4] 多会话交错不串（按 sid / 静默分段）")
    p = parser_live.LogParser(fragments=True)
    res = p.parse_log_file(str(HELLO_LOG))
    texts = [r.reply for r in res["replies"]]
    hello_rec = next((t for t in texts if "你好呀" in t), "")
    title_rec = next((t for t in texts if "没办法直接判断" in t), "")
    check("两路会话各自成流", bool(hello_rec) and bool(title_rec))
    check("你好流不含他人内容", "标题" not in hello_rec and "没办法" not in hello_rec)
    check("他人流不含你好内容", "你好呀" not in title_rec)

    # ── [5] 静默边界与重叠去重（合成样本）──
    print("\n[5] 静默边界与重叠去重（合成样本）")
    base = datetime(YEAR, 10, 9, 18, 0, 0)
    lines = []

    def apx(t: float, text: str, state: int) -> None:
        ts = (base + timedelta(seconds=t)).strftime("%m-%d %H:%M:%S.%f")[:-3]
        lines.append(f"{ts}  4254  4254 D ASS-TTSEngine: appendText() called with: "
                     f"text = [{text}], textState = [{state}]")

    apx(0.0, "片A", 0)
    apx(0.3, "片B", 1)      # 同一流（间隔 0.3s）
    apx(2.5, "片C", 0)
    apx(2.7, "片D", 1)      # 间隔 2.2s > 1.5s → 新流
    apx(5.0, "你好", 0)
    apx(5.3, "你好世界", 1)  # 重叠增量（"你好" + "世界"）
    synth = tmp / "synth.log"
    synth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reps = [r.reply for r in parser_live.LogParser(fragments=True).parse_log_file(str(synth))["replies"]]
    check("静默>1.5s 分段（3 流）", len(reps) == 3, str(reps))
    check("流内合并", reps[0] == "片A片B" and reps[1] == "片C片D", str(reps[:2]))
    check("重叠增量去重", reps[2] == "你好世界", str(reps[2]))

    # ── [6] 拒识规则随档案 ──
    print("\n[6] 拒识规则随档案（T28 版 vs 内置）")
    proj = tmp / "projects"
    proj.mkdir()
    shutil.copy2(ROOT / "data" / "projects" / "t28.json", proj / "t28.json")
    project.set_projects_dir(proj)
    project.set_active("t28")

    j1 = Judger(clock=lambda: "")
    j1.feed_log("10-09 18:19:55.630830  4254  6887 I ASS-CbmToolPkStrategy: handle sparkDenial")
    check("T28：例行 handle sparkDenial 不误报拒识", j1._denied is False)

    j2 = Judger(clock=lambda: "")
    j2.feed_log("10-09 18:20:07.993  4254  6887 I ASS-CbmToolPkStrategy: 20009::云端落域 "
                "query = [x], sid = [y], cbm_tool_pk.type = [cbm_agent], "
                "domain = [cbm_denial], intent = [other]")
    check("T28：domain=[cbm_denial] 正确判拒识", j2._denied is True)

    project.set_active("e0v")
    project.set_projects_dir(None)
    j3 = Judger(clock=lambda: "")
    j3.feed_log("ASS-CbmToolPkStrategy: handle sparkDenial")
    check("内置（E0V）规则：bare sparkDenial 会命中（差异真实存在，档案隔离必要）",
          j3._denied is True)

    # 判定超时默认值随档案（judge.timeout，2026-10-09）
    check("超时兜底 20s（内置）", judge_default_timeout() == 20.0)
    project.set_projects_dir(proj)
    project.set_active("t28")
    check("超时默认随档案（t28=30s）", judge_default_timeout() == 30.0)
    project.set_active("e0v")
    project.set_projects_dir(None)

    # ── [7] 预置档案契约 ──
    print("\n[7] 预置档案契约（t28.json / t1ma.json）")
    for key in ("t28", "t1ma"):
        prof = json.loads((ROOT / "data" / "projects" / f"{key}.json").read_text(encoding="utf-8"))
        lg = prof.get("log", {})
        # 平台契约：路径落在 /log/android 下（具体到不到 logcat/ 子目录属用户可调值，
        # 2026-10-12 起该键为「方案值」，用户可在配置页改——故只钉平台前缀）
        check(f"{key}：日志路径在 /log/android 下",
              str(lg.get("pull_paths", [{}])[0].get("path", "")).startswith("/log/android"))
        check(f"{key}：file_glob", lg.get("file_glob") == ["logcat.txt", "*.txt.gz"])
        check(f"{key}：reply_fragments 开启", lg.get("reply_fragments") is True)
        check(f"{key}：deny_pattern 不含裸 sparkDenial（无 'sparkDenial' 子串）",
              "sparkDenial" not in prof.get("judge", {}).get("deny_pattern", ""))
        check(f"{key}：timeout=30", prof.get("judge", {}).get("timeout") == 30)
        check(f"{key}：settle_flush 关闭（防 force-stop 事故）", lg.get("settle_flush") is False)
        check(f"{key}：services 空（待真机验证）", prof.get("device", {}).get("services") == [])
        check(f"{key}：extract 开关关闭", prof.get("extract", {}).get("enabled") is False)

    # ── [8] 设备日志仲裁修复（2026-10-10 真机暴露）──
    print("\n[8] 设备日志仲裁修复（权威合并 + 运行时间窗）")
    import types as _types
    from datetime import datetime as _dt, timedelta as _td
    from cartest.pipeline.stages import _parse_device_logs

    today = _dt.now()
    ymd = today.strftime("%m-%d")
    yest = (today - _td(days=1)).strftime("%m-%d")

    # 8a 权威合并：同 query 的 ASR 记录（无 sid，回复只能时间邻近——拿到"他人的"错配）
    # 与 CBM 记录（有 sid，回复池无自己的）——修复后高权威覆盖：domain 取 CBM、错配回复被弃
    d8 = tmp / "devlogs_merge"
    d8.mkdir()
    (d8 / "main_fix.log").write_text(
        f'{ymd} 09:00:00.900  1  1 I isssr_log: {{"normal_text":"查询甲"}}\n'
        f'{ymd} 09:00:01.000  1  1 I TtsSpeaker: startFlowSpeak sid=SX, text=他人回复, traceId=t\n'
        f'{ymd} 09:00:02.000  1  1 I ASS-CbmToolPkStrategy: 云端落域 '
        f'query = [查询甲] sid = [SA] cbm_tool_pk.type = [cbm_agent] domain = [新闻]\n',
        encoding="utf-8")
    ctx8 = _types.SimpleNamespace(device_logs_dir=d8,
                                  created_at=f"{today:%Y-%m-%d} 09:00:00")
    rec8 = _parse_device_logs(ctx8).get("查询甲", {})
    check("高权威（CBM）覆盖：domain=新闻", rec8.get("domain") == "新闻", repr(rec8))
    check("低权威错配回复被弃（宁可空勿错）", rec8.get("reply") == "", repr(rec8))

    # 8b 运行时间窗：历史归档记录（早于 run 创建 -60s）不参与合并
    d9 = tmp / "devlogs_hist"
    d9.mkdir()
    (d9 / "main_hist.log").write_text(
        f'{yest} 18:00:00.000  1  1 I ASS-CbmToolPkStrategy: 云端落域 '
        f'query = [旧会话] sid = [S0] cbm_tool_pk.type = [cbm_agent] domain = [历史域]\n'
        f'{ymd} 09:00:10.000  1  1 I ASS-CbmToolPkStrategy: 云端落域 '
        f'query = [新会话] sid = [S1] cbm_tool_pk.type = [cbm_agent] domain = [新域]\n',
        encoding="utf-8")
    ctx9 = _types.SimpleNamespace(device_logs_dir=d9,
                                  created_at=f"{today:%Y-%m-%d} 09:00:00")
    out9 = _parse_device_logs(ctx9)
    check("历史记录被时间窗过滤", "旧会话" not in out9, str(list(out9.keys())))
    check("本次记录保留", out9.get("新会话", {}).get("domain") == "新域", repr(out9.get("新会话")))

    # ── [9] settle flush 平台开关（2026-10-10 真机事故修复）──
    # 事故：T28 上 settle 的 force-stop(主应用) 致其不自动复活 → 车机回落 Launcher、
    # 语音服务进入"输入法期间"拦截态 → 后续注入全被拒。档案 log.settle_flush=false 跳过。
    print("\n[9] settle flush 开关（force-stop 事故修复）")
    from cartest.pipeline.stages import stage_settle

    class _FakeAdb:
        def __init__(self):
            self.force_stopped: list[str] = []

        def force_stop(self, package, device_id=""):
            self.force_stopped.append(package)
            return True

        def pull_logs(self, out_dir, device_id=""):
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            return True, "已保存 0/0", 0

    def _run_settle(profile_key: str) -> list[str]:
        proj9 = tmp / f"proj9_{profile_key}"
        proj9.mkdir()
        if profile_key == "t28":
            shutil.copy2(ROOT / "data" / "projects" / "t28.json", proj9 / "t28.json")
        project.set_projects_dir(proj9)
        project.set_active(profile_key)
        fake = _FakeAdb()
        ctx9 = _types.SimpleNamespace(device_logs_dir=tmp / f"dl9_{profile_key}")
        p9 = _types.SimpleNamespace(
            adb_session=lambda: fake, voice_package="com.lioncarmind.car",
            settle_wait=0.0, device="", log=lambda m, t="info": None)
        stage_settle(ctx9, p9)
        return fake.force_stopped

    stops_t28 = _run_settle("t28")
    check("t28：settle 不 force-stop（防主应用不复活事故）", stops_t28 == [], str(stops_t28))
    stops_e0v = _run_settle("e0v")
    check("e0v（内置）：settle 照旧 force-stop", stops_e0v == ["com.lioncarmind.car"],
          str(stops_e0v))
    project.set_active("e0v")
    project.set_projects_dir(None)

    # ── [10] 20204 长文本截断容错 + 空片段清理（2026-10-10 T1MA 真机暴露）──
    # 真机：cbm_tool_news 长新闻列表撞 logcat 单行长度上限被物理截断（行尾无闭合
    # 引号）→ 原正则提取失败 → 整条长文本丢失（"新闻热榜"用例 reply 空）。
    # 另：状态型 20204（无正文）会产生空流，浪费回复配对轮次——聚合器应丢弃。
    print("\n[10] 长文本截断容错 + 空片段清理")
    t10 = (base + timedelta(seconds=10)).strftime("%m-%d %H:%M:%S.%f")[:-3]
    line_trunc = (
        f'{t10}  6006  7402 I ASS-SrBusinessManager: sparkSrResult srCode: [20204], '
        'srIndex:[1], s = {"cbm_meta":{"text":"{\\"nlp\\":{\\"intent\\":0}}"'
        ',"status":1},"nlp":{"text":"第1条新闻：标题：测试甲, 参考内容：内容甲。'
        '\\n第2条新闻：标题：测试乙, 参考内容：内容乙被截断')
    f_trunc = tmp / "trunc.log"
    f_trunc.write_text(line_trunc + "\n", encoding="utf-8")
    reps10 = [r.reply for r in
              parser_live.LogParser(fragments=True).parse_log_file(str(f_trunc))["replies"]]
    check("截断文本可提取（降级到行尾前缀）",
          len(reps10) == 1 and "第1条新闻" in reps10[0] and "内容乙被截断" in reps10[0],
          str(reps10)[:120])

    t10b = (base + timedelta(seconds=11)).strftime("%m-%d %H:%M:%S.%f")[:-3]
    line_empty = (
        f'{t10b}  6006  7402 I ASS-SrBusinessManager: sparkSrResult srCode: [20204], '
        'srIndex:[1], s = {"cbm_meta":{"text":"{\\"nlp\\":{\\"intent\\":0}}"'
        ',"status":1},"nlp":{"text":"","status":2},"header":{"code":0}}')
    f_empty = tmp / "empty_frag.log"
    f_empty.write_text(line_empty + "\n", encoding="utf-8")
    reps10b = [r.reply for r in
               parser_live.LogParser(fragments=True).parse_log_file(str(f_empty))["replies"]]
    check("空文本 20204 片段不产出记录（空流清理）", reps10b == [], str(reps10b))
    reps10c = [r.reply for r in
               parser_live.LogParser(fragments=False).parse_log_file(str(f_trunc))["replies"]]
    check("fragments=False 时截断行不产出（E0V 零漂移不变）", reps10c == [], str(reps10c))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("平台适配测试全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
