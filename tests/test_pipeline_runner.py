# -*- coding: utf-8 -*-
"""P2 流水线自测与验收（**全程无设备**）

对应 04-P2设计.md §5 的五项验收：
  ① CLI 与直接调用 core 的判定结果一致（防双入口逻辑分叉）
  ② 中断后能从断点续跑
  ③ 磁盘清理生效（keep 保护 / 最近 N 次保留）
  ④ judge 可单独重跑（不重新灌入）
  ⑤ 阶段幂等

外加：端到端跑通 7 阶段、产物齐全、run.json 状态正确。

用法:
    python tests/test_pipeline_runner.py
    pytest tests/test_pipeline_runner.py
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.injector import InjectMode, Injector, InjectState        # noqa: E402
from cartest.core.judger import Judger                                     # noqa: E402
from cartest.core.progress import ProgressStore, pairs_from_cases          # noqa: E402
from cartest.core.source import DataSource, TestCase                       # noqa: E402
from cartest.pipeline.cleanup import cleanup_runs                          # noqa: E402
from cartest.pipeline.context import DONE, FAILED, STAGES, RunContext      # noqa: E402
from cartest.pipeline.runner import resume_pipeline, run_pipeline          # noqa: E402
from cartest.pipeline.stages import STAGE_FUNCS, PipelineParams, stage_judge  # noqa: E402

FAILED_LIST = []
DEV_TS = "09-15 10:00:00.000"


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED_LIST.append(name)


# ── 替身 ──────────────────────────────────────
class ScriptedAdb:
    """发送 query 后按脚本推入日志行（模拟设备回复）"""

    def __init__(self, log_q: queue.Queue, script: dict, fail_on: set | None = None):
        self.q = log_q
        self.script = script
        self.fail_on = fail_on or set()
        self.sent: list[str] = []
        self.connect_ok = True

    def check_device(self, force=False):
        return (self.connect_ok, "FAKE-DEV" if self.connect_ok else "设备未连接")

    def device_timestamp(self):
        return DEV_TS

    def send_voice(self, text, device_id="", mode=3, on_log=None):
        self.sent.append(text)
        if text in self.fail_on:
            return False, "模拟发送失败"
        for line in self.script.get(text, []):
            self.q.put(("log", line))
            time.sleep(0.002)
        return True, "OK"

    def force_stop(self, pkg, device_id=""):
        return True

    def pull_logs(self, out_dir, device_id=""):
        return False, "替身模式无日志", 0


class SilentMonitor:
    def __init__(self, q):
        self.q = q
        self.started = False

    def start(self, device_id=""):
        self.started = True

    def stop(self):
        self.started = False

    def restart(self, reason=""):
        return reason

    def health_check(self, idle_timeout=25.0):
        return None

    def should_health_check(self, interval=2.0):
        return False

    @property
    def epoch(self):
        return 1

    @property
    def is_running(self):
        return self.started


LOG_SCRIPT = {
    "最近的新闻": [
        '09-15 10:00:01.100  1  2 I isssr_log: {"normal_text":"最近的新闻"}',
        '09-15 10:00:01.300  1  2 I ASS-CbmToolPkStrategy: 云端落域 query = [最近的新闻] sid = [S1] domain = [新闻]',
        '09-15 10:00:02.500  1  2 I sparkSrResult: {"srCode":20204,"nlp":{"text":"为您播报最近的新闻"}}',
    ],
    "播放音乐": [
        '09-15 10:00:01.100  1  2 I isssr_log: {"normal_text":"播放音乐"}',
        '09-15 10:00:01.300  1  2 I ASS-CbmToolPkStrategy: 云端落域 query = [播放音乐] sid = [S2] domain = [音乐]',
    ],
}

CASES = [("1", "最近的新闻", ["新闻"]), ("2", "播放音乐", [])]
EXPECT = {"1": "pass", "2": "fail"}


def write_cases_file(path: Path) -> Path:
    path.write_text(json.dumps(
        [{"id": cid, "query": q, "expected_keywords": kw} for cid, q, kw in CASES],
        ensure_ascii=False), encoding="utf-8")
    return path


def make_params(case_file: Path, fail_on: set | None = None):
    """构造参数 + 共享队列的替身（注入到 injector 内部）"""
    log_q = queue.Queue(maxsize=1000)
    adb = ScriptedAdb(log_q, LOG_SCRIPT, fail_on=fail_on)

    def factory():
        return Injector(adb=adb, log_q=log_q,
                        monitor_factory=lambda q: SilentMonitor(q))

    return PipelineParams(case_file=str(case_file), device="FAKE-DEV",
                          interval=0.05, timeout=6.0,
                          adb=adb, injector_factory=factory), adb


# ── [1] 端到端跑通 7 阶段 ──
def test_full_pipeline():
    print("[1] 端到端跑通 7 阶段（替身，无设备）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cases = write_cases_file(Path(td) / "cases.json")
        params, adb = make_params(cases)

        on_stage = []
        ctx = run_pipeline(params, root=root,
                           on_stage=lambda s, st, d: on_stage.append((s, st)))

        check("全部 7 阶段完成", ctx.is_complete(), str(ctx.summary()["阶段"]))
        check("阶段顺序正确",
              [s for s, st in on_stage if st == "done"] == STAGES,
              str([s for s, st in on_stage if st == "done"]))
        check("灌入了 2 条", len(adb.sent) == 2, str(adb.sent))

        # 产物齐全
        check("cases.json", ctx.cases_json.is_file())
        check("progress.jsonl", ctx.progress_jsonl.is_file())
        check("results.json", ctx.results_json.is_file())
        check("报告目录有产物", ctx.report_dir.is_dir() and any(ctx.report_dir.iterdir()),
              str(list(ctx.report_dir.iterdir()) if ctx.report_dir.is_dir() else []))

        payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        actual = {r["run_id"]: r["status"] for r in payload["results"]}
        check("判定结果与预期一致", actual == EXPECT, str(actual))
        check("统计字段正确",
              payload["total"] == 2 and payload["pass"] == 1 and payload["fail"] == 1,
              str({k: payload[k] for k in ("total", "pass", "fail")}))

        # 报告含关键信息
        md = next(ctx.report_dir.glob("*_报告.md")).read_text(encoding="utf-8")
        check("报告含汇总表", "| 总用例 | 2 |" in md or "总用例 | 2" in md, md[:80].replace("\n", " "))


# ── [2] 断点恢复 ──
def test_resume_after_failure():
    print("[2] 中断后从断点续跑（验收项 ②）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cases = write_cases_file(Path(td) / "cases.json")

        # 第一次：让 inject 阶段失败（发送失败会记为 error，但阶段本身不失败）
        # → 改用"设备未连接"让 prepare 失败，验证失败后不继续
        params, _adb = make_params(cases)
        params.adb.connect_ok = False
        try:
            run_pipeline(params, root=root)
            check("prepare 失败应抛异常", False, "未抛异常")
        except RuntimeError as e:
            check("prepare 失败抛异常", "设备检查失败" in str(e), str(e)[:60])

        runs = RunContext.list_runs(root)
        check("运行已创建", len(runs) == 1, str(len(runs)))
        ctx = RunContext.open(runs[0]["run_id"], root)
        check("prepare 标记为 failed", ctx.stage_status("prepare") == FAILED)
        check("后续阶段未执行",
              all(ctx.stage_status(s) == "pending" for s in STAGES[1:]),
              str(ctx.summary()["阶段"]))
        check("next_stage 指向 prepare（重跑）", ctx.next_stage() == "prepare")

        # 第二次：设备正常 → resume
        params2, adb2 = make_params(cases)
        ctx2 = resume_pipeline(ctx.run_id, params2, root=root)
        check("续跑后全部完成", ctx2.is_complete(), str(ctx2.summary()["阶段"]))
        check("续跑确实执行了 inject", len(adb2.sent) == 2, str(adb2.sent))


# ── [3] judge 可单独重跑 ──
def test_judge_rerun():
    print("[3] judge 可单独重跑（验收项 ④）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cases = write_cases_file(Path(td) / "cases.json")
        params, _adb = make_params(cases)
        ctx = run_pipeline(params, root=root)

        original = ctx.results_json.read_text(encoding="utf-8")
        payload = json.loads(original)

        # 篡改结果文件，再单独重跑 judge
        payload["pass"] = 999
        ctx.results_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        detail = stage_judge(ctx, params)
        rebuilt = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        check("judge 独立重跑成功", rebuilt["pass"] == 1, str(rebuilt["pass"]))
        check("重跑不需要重新灌入",
              rebuilt["total"] == len(payload["results"]), str(rebuilt["total"]))
        check("返回 detail 合理", "共" in detail, detail)


# ── [4] 阶段幂等 ──
def test_stage_idempotent():
    print("[4] 阶段幂等（验收项 ⑤）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cases = write_cases_file(Path(td) / "cases.json")
        params, _adb = make_params(cases)
        ctx = run_pipeline(params, root=root)

        # 重复执行 judge / fillback / report，产物应保持一致
        before_judge = ctx.results_json.read_text(encoding="utf-8")
        stage_judge(ctx, params)
        after_judge = ctx.results_json.read_text(encoding="utf-8")
        # generated_at 会变，比较 results 部分
        bj = json.loads(before_judge)["results"]
        aj = json.loads(after_judge)["results"]
        check("judge 幂等（结果部分一致）", bj == aj, "")

        before_files = sorted(p.name for p in ctx.report_dir.iterdir())
        STAGE_FUNCS["fillback"](ctx, params)
        STAGE_FUNCS["report"](ctx, params)
        after_files = sorted(p.name for p in ctx.report_dir.iterdir())
        check("fillback/report 幂等（不新增文件）", before_files == after_files,
              f"{before_files} → {after_files}")


# ── [5] 双路径一致性（验收项 ①）──
def test_dual_path_consistency():
    print("[5] 直接调 core 与走 pipeline 结果一致（验收项 ①）")
    with tempfile.TemporaryDirectory() as td:
        cases_path = write_cases_file(Path(td) / "cases.json")
        root = Path(td) / "runs"

        # 路径 A：直接调 Injector（GUI 将走的路径）
        log_q = queue.Queue(maxsize=1000)
        adb_a = ScriptedAdb(log_q, LOG_SCRIPT)
        inj = Injector(adb=adb_a, log_q=log_q,
                       monitor_factory=lambda q: SilentMonitor(q))
        inj.set_params(interval=0.05, timeout=6.0)
        inj.set_device("FAKE-DEV")
        ds = DataSource()
        ds.split_multi_round = True
        ds.load_file(str(cases_path))
        inj.start_batch(ds, mode=InjectMode.INTERVAL)
        t0 = time.time()
        while inj.state != InjectState.IDLE and time.time() - t0 < 60:
            time.sleep(0.05)
        path_a = {c.id: c.status for c in ds.cases}

        # 路径 B：走 pipeline
        params, _adb_b = make_params(cases_path)
        ctx = run_pipeline(params, root=root)
        payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        path_b = {r["run_id"]: r["status"] for r in payload["results"]}

        check("两条路径判定结果一致", path_a == path_b, f"A={path_a} B={path_b}")


# ── [6] 清理与 run.json 字段 ──
def test_cleanup_and_meta():
    print("[6] 清理集成 / run_id 冲突 / run.json 字段")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cases = write_cases_file(Path(td) / "cases.json")

        # 12 次快速创建 → run_id 必须各异（同秒内靠序号区分）
        ids = [RunContext.create(cases, root=root).run_id for _ in range(12)]
        check("12 次创建得到 12 个不同 run_id", len(set(ids)) == 12,
              f"唯一 {len(set(ids))}")

        check("造出 12 次运行", len(RunContext.list_runs(root)) == 12)
        deleted = cleanup_runs(root, keep_recent=10)
        check("清理掉 2 次", len(deleted) == 2, str(deleted))
        check("剩余 10 次", len(RunContext.list_runs(root)) == 10)

        # keep 保护
        RunContext.create(cases, root=root, keep=True)
        RunContext.create(cases, root=root)
        before = len(RunContext.list_runs(root))
        cleanup_runs(root, keep_recent=1)
        after = RunContext.list_runs(root)
        check("keep 的运行在激进清理下仍保留",
              any(r["keep"] for r in after), str([r["run_id"] for r in after]))

        # run.json 参数快照（跑一次真实 pipeline 才有完整 params）
        params, _adb = make_params(cases)
        ctx = run_pipeline(params, root=root, do_cleanup=False)
        ctx2 = RunContext.open(ctx.run_id, root)
        check("run.json 含 params 快照", bool(ctx2.params), str(ctx2.params)[:80])
        check("params 不含注入对象",
              "adb" not in ctx2.params and "injector_factory" not in ctx2.params,
              str(list(ctx2.params)))
        check("params 含可序列化字段",
              ctx2.params.get("interval") == 0.05 and ctx2.params.get("device") == "FAKE-DEV",
              str(ctx2.params))


# ── [8] 设备日志腿：同 query 多记录合并 ──
def test_device_logs_merge():
    print("[8] device_logs 解析：同 query 多条记录（ASR 无 domain + CBM 有 domain）合并")
    import types

    from cartest.pipeline.stages import _parse_device_logs

    with tempfile.TemporaryDirectory() as td:
        logs_dir = Path(td) / "device_logs"
        logs_dir.mkdir()
        # 复刻真机场景（2026-09-28 pilot 实测）：CBM 落域行在前（.409），
        # ASR normal_text 记录在后（.410，且真机上会重复多条）——
        # 消费端若按 query 简单覆盖，ASR 记录会把 CBM 的 domain 覆盖为空。
        (logs_dir / "main_00001.log").write_text(
            '09-28 16:20:55.409  4161  9189 I ASS-CbmToolPkStrategy: 20009::云端落域 '
            'query = [画个猫咪], sid = [wgw1@abc], cbm_tool_pk.type = [cbm_agent], '
            'domain = [文生图助手], intent = [QUERY]\n'
            '09-28 16:20:55.410  4161  9189 D ASS-SparkManager: sendAssistantMsg: '
            '{"normal_text":"画个猫咪","rc":0}\n'
            '09-28 16:20:55.416  4161  9189 D ASS-SparkManager: sendAssistantMsg: '
            '{"normal_text":"画个猫咪","rc":0}\n',
            encoding="utf-8")

        ctx = types.SimpleNamespace(device_logs_dir=logs_dir)
        out = _parse_device_logs(ctx)
        rec = out.get("画个猫咪", {})
        check("domain 未被 ASR 记录覆盖为空", rec.get("domain") == "文生图助手", repr(rec))
        check("query_time 保留", bool(rec.get("query_time")), repr(rec))


# ── [7] CLI 参数传递 ──
def test_cli_params():
    print("[7] CLI 参数正确传递到 pipeline")
    import contextlib
    import io

    import cartest.cli as cli

    prev_env = os.environ.get("ANDROID_SERIAL")
    with tempfile.TemporaryDirectory() as td:
        cases = write_cases_file(Path(td) / "cases.json")
        captured = {"runs": []}          # 两次 run 调用各自记录，断言取第一次

        def fake_run(params, keep=False, root=None, on_stage=None):
            captured["runs"].append((params, keep))
            return RunContext.create(params.case_file, keep=keep, root=Path(td) / "runs")

        orig = cli.run_pipeline
        cli.run_pipeline = fake_run
        try:
            os.environ.pop("ANDROID_SERIAL", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["run", "--cases", str(cases), "--device", "DEV9",
                               "--resume", "redo_all", "--interval", "1.5",
                               "--timeout", "9", "--keep"])
            # 设备绑定：--device 写入进程级 ANDROID_SERIAL（多设备时设备时钟查询依赖它）
            check("--device 写入 ANDROID_SERIAL", os.environ.get("ANDROID_SERIAL") == "DEV9",
                  repr(os.environ.get("ANDROID_SERIAL")))

            # 不带 --device 时不得覆盖（尊重 shell 显式设置；此处先清空再跑）
            os.environ.pop("ANDROID_SERIAL", None)
            with contextlib.redirect_stdout(buf):
                rc_nod = cli.main(["run", "--cases", str(cases)])
            check("无 --device 不动 ANDROID_SERIAL", "ANDROID_SERIAL" not in os.environ,
                  repr(os.environ.get("ANDROID_SERIAL")))
        finally:
            cli.run_pipeline = orig
            if prev_env is None:
                os.environ.pop("ANDROID_SERIAL", None)
            else:
                os.environ["ANDROID_SERIAL"] = prev_env

        check("返回码 0", rc == 0, str(rc))
        check("无设备返回码 0", rc_nod == 0, str(rc_nod))
        p, keep_flag = captured["runs"][0]
        check("cases 传递", p and p.case_file == str(cases), str(p.case_file) if p else "")
        check("device 传递", p and p.device == "DEV9", str(p.device) if p else "")
        check("resume_mode 传递", p and p.resume_mode == "redo_all", str(p.resume_mode) if p else "")
        check("interval 传递", p and p.interval == 1.5, str(p.interval) if p else "")
        check("timeout 传递", p and p.timeout == 9.0, str(p.timeout) if p else "")
        check("keep 传递", keep_flag is True, str(keep_flag))

        # 用例文件不存在 → 返回码 2
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc2 = cli.main(["run", "--cases", str(Path(td) / "nope.json")])
        check("用例文件不存在 → 返回码 2", rc2 == 2, str(rc2))

        # resume：设备绑定从 run 快照恢复（monkeypatch RunContext.open 与 resume_pipeline）
        import types
        os.environ.pop("ANDROID_SERIAL", None)

        class _FakeCtx:
            device = "DEV-RESUME"

        class _FakeRC:
            @staticmethod
            def open(run_id, root=None):
                return _FakeCtx()

        orig_rc, orig_resume = cli.RunContext, cli.resume_pipeline
        cli.RunContext = _FakeRC
        cli.resume_pipeline = lambda run_id, on_stage=None: types.SimpleNamespace(
            run_dir=Path(td) / "fake_run")
        try:
            with contextlib.redirect_stdout(buf):
                rc3 = cli.main(["resume", "some-run"])
            check("resume 返回码 0", rc3 == 0, str(rc3))
            check("resume 从快照绑定 ANDROID_SERIAL",
                  os.environ.get("ANDROID_SERIAL") == "DEV-RESUME",
                  repr(os.environ.get("ANDROID_SERIAL")))
        finally:
            cli.RunContext, cli.resume_pipeline = orig_rc, orig_resume
            if prev_env is None:
                os.environ.pop("ANDROID_SERIAL", None)
            else:
                os.environ["ANDROID_SERIAL"] = prev_env


# ── [9] start_index 贯通（2026-10-15 灌入范围「从指定序号起重灌」）──
def test_start_index():
    print("[9] start_index 贯通（前段不灌、不误报；整条流水线）")
    cases4 = [("1", "最近的新闻", ["新闻"]), ("2", "播放音乐", []),
              ("3", "最近的新闻", ["新闻"]), ("4", "播放音乐", [])]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        cf = Path(td) / "cases.json"
        cf.write_text(json.dumps(
            [{"id": c, "query": q, "expected_keywords": k} for c, q, k in cases4],
            ensure_ascii=False), encoding="utf-8")

        log_q = queue.Queue(maxsize=1000)
        adb = ScriptedAdb(log_q, LOG_SCRIPT)
        params = PipelineParams(
            case_file=str(cf), device="FAKE-DEV", interval=0.05, timeout=6.0,
            resume_mode="redo_all",      # GUI from_index 档会解析成 redo_all + start_index
            start_index=2,
            adb=adb,
            injector_factory=lambda: Injector(
                adb=adb, log_q=log_q, monitor_factory=lambda q: SilentMonitor(q)))
        ctx = run_pipeline(params, root=root)

        check("仅第 3 条起被灌入（前 2 条不发）",
              adb.sent == ["最近的新闻", "播放音乐"], str(adb.sent))
        payload = json.loads(ctx.results_json.read_text(encoding="utf-8"))
        actual = {r["run_id"]: r["status"] for r in payload["results"]}
        check("前段未灌入 → pending（不误报失败）",
              actual["1"] == "pending" and actual["2"] == "pending", str(actual))
        check("后段正常判定", actual["3"] == "pass" and actual["4"] == "fail", str(actual))
        check("统计只计已判定（pass+fail=2）",
              payload["pass"] == 1 and payload["fail"] == 1,
              str({k: payload[k] for k in ("total", "pass", "fail")}))
        check("run.json 快照含 start_index（续跑可复现）",
              ctx.params.get("start_index") == 2, str(ctx.params.get("start_index")))


ALL_TESTS = (test_full_pipeline, test_resume_after_failure, test_judge_rerun,
             test_stage_idempotent, test_dual_path_consistency,
             test_cleanup_and_meta, test_cli_params, test_device_logs_merge,
             test_start_index)


if __name__ == "__main__":
    # 输出隔离（2026-09-24 全按钮点验发现）：extract 阶段走真实 CbmLogextractor，
    # 不重定向会把夹具日志的报告写进工具真实 reports/（污染用户报告列表）；
    # 夹具提取报告统一落到本临时目录
    with tempfile.TemporaryDirectory(prefix="cartest_runner_out_") as _out:
        os.environ["CARTEST_LOGEXTRACT_OUT"] = _out
        # 环境隔离（2026-10-10）：断言以「内置 plt1 默认」为前提——
        # 断开真实 settings.json 与项目档案，免受本机多平台切换（plt2/plt3）影响
        from cartest.core import config_center as _cc        # noqa: PLC0415
        from cartest.core import project as _pj              # noqa: PLC0415
        _iso = Path(_out) / "iso"
        _iso.mkdir()
        _cc.set_settings_file(_iso / "settings.json")
        _pj.set_projects_dir(_iso / "projects")
        try:
            for fn in ALL_TESTS:
                fn()
        finally:
            os.environ.pop("CARTEST_LOGEXTRACT_OUT", None)
    print()
    if FAILED_LIST:
        print(f"失败 {len(FAILED_LIST)} 项: {FAILED_LIST}")
        sys.exit(1)
    print("P2 流水线自测与验收全部通过（全程无设备）")
