# -*- coding: utf-8 -*-
"""parser/offline 薄封装自测（不实际执行 CbmLogextractor）

覆盖（runner 注入；执行形态＝**子进程**——见 2026-09-17 修订/发现 #3）：
  1) tool_dir 环境变量覆盖 / available 探测
  2) extract_report 的 argv 构造（all 子命令、--jobs、路径）
  3) 返回码非 0：无本轮新产物 → RuntimeError；有本轮新产物 → 降级为告警（集成指南 §4.2，
     工具 ``all`` 的 failed_files 语义；陈旧产物不冒充成功）
  4) 未找到报告产物 → RuntimeError
  5) latest_report 取最新（只认 all_report_*.html 一份）
  6) on_log 捕获工具输出并转发
  7) 缺入口 → LogextractUnavailable
  8) 输出重定向：out_dir 参数 / CARTEST_LOGEXTRACT_OUT 环境变量（替身防污染）
  9) 产物定位只认根目录 HTML：_raw_/ 归档 md 不被误选（2026-10-08 用户裁决）
 10) 默认隔离运行目录（reports/_run_*）执行 → 提升到 reports/ 根，运行目录清理
 11) 产物定位优先取 stdout 契约行「全量报告已写入:」而非 glob 最新（集成指南 §5.1）

用法:
    python tests/test_offline.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cartest.core.parser import offline                       # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeRunner:
    """替身执行器：记录 argv，按需产出报告（尊重 --out），可选回放输出/契约行"""

    def __init__(self, rc: int = 0, make_report: bool = True, echo: str = "提取完成",
                 emit_full_line: bool = False, notify_path: Path | None = None):
        self.rc = rc
        self.make_report = make_report
        self.echo = echo
        self.emit_full_line = emit_full_line
        self.notify_path = notify_path
        self.calls: list[list] = []

    def __call__(self, argv, on_log):
        self.calls.append(list(argv))
        if on_log and self.echo:
            on_log(self.echo, "info")
        path = None
        if self.make_report:
            if "--out" in argv:
                out = Path(argv[argv.index("--out") + 1])
            else:
                out = offline.reports_dir()
            out.mkdir(parents=True, exist_ok=True)
            path = out / "all_report_20260915_120000.html"
            path.write_text("<html>report</html>", encoding="utf-8")
        if self.emit_full_line:
            # stdout 契约行（集成指南 §5.1）：默认指向本轮产物；未产报告时用 notify_path
            target = path or self.notify_path
            if on_log and target is not None:
                on_log(f"全量报告已写入: {target}", "info")
        return self.rc


def main() -> int:
    print("=" * 70)
    print("parser/offline 薄封装自测（runner 注入）")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / "logextract.py").write_text("# fake entry", encoding="utf-8")
        os.environ["CARTEST_LOGEXTRACT_DIR"] = str(tdir)
        try:
            # ── [1] 定位与可用性 ──
            check("tool_dir 环境变量覆盖", offline.tool_dir() == tdir, str(offline.tool_dir()))
            check("reports_dir 锚点", offline.reports_dir() == tdir / "reports")
            check("available=True（有入口）", offline.available() is True)
            (tdir / "logextract.py").rename(tdir / "x.py")
            check("available=False（缺入口）", offline.available() is False)

            # ── [7] 缺入口 → LogextractUnavailable ──
            raised = False
            try:
                offline.extract_report(tdir, runner=FakeRunner())
            except offline.LogextractUnavailable:
                raised = True
            check("缺入口 → LogextractUnavailable", raised)
            (tdir / "x.py").rename(tdir / "logextract.py")

            # ── [2] argv 构造 ──
            log_dir = tdir / "logs"
            log_dir.mkdir()
            runner = FakeRunner()
            report = offline.extract_report(log_dir, runner=runner)
            argv = runner.calls[0]
            check("argv: python + 入口 + all + 目录",
                  argv[0] == sys.executable and argv[1].endswith("logextract.py")
                  and argv[2] == "all" and argv[-1] == str(log_dir), str(argv))
            check("返回报告路径",
                  report.name == "all_report_20260915_120000.html", report.name)

            runner2 = FakeRunner()
            offline.extract_report(log_dir, jobs=2, runner=runner2)
            check("--jobs 传递", runner2.calls[0][3:5] == ["--jobs", "2"],
                  str(runner2.calls[0]))

            # ── [3] 返回码非 0 ──
            raised = False
            try:
                offline.extract_report(log_dir, runner=FakeRunner(rc=1, make_report=False))
            except RuntimeError as ex:
                raised = "返回码 1" in str(ex)
            check("rc=1 → RuntimeError（含返回码）", raised)

            # ── [3b] rc=1 但本轮已产出新报告 → 降级为告警（工具 all 的 failed_files 语义）──
            #    对齐集成指南 §4.2：rc=1 有两义，产物存在时是「带告警的成功」
            out_rc1 = tdir / "out_rc1"
            warned: list[str] = []
            rep_rc1 = offline.extract_report(
                log_dir, out_dir=out_rc1,
                on_log=lambda m, t="info": warned.append(m),
                runner=FakeRunner(rc=1, make_report=True))
            check("rc=1+新产物 → 不抛错并返回报告",
                  rep_rc1.parent == out_rc1 and rep_rc1.name.startswith("all_report_"),
                  str(rep_rc1))
            check("rc=1+新产物 → on_log 收到 WARN 告警",
                  any("WARN" in m for m in warned), str(warned))

            # ── [3c] rc=1 且只有陈旧产物 → 仍判失败（新鲜度守卫：旧报告不得冒充成功）──
            out_stale = tdir / "out_stale"
            out_stale.mkdir(parents=True, exist_ok=True)
            (out_stale / "all_report_20260101_000000.html").write_text(
                "<html>stale</html>", encoding="utf-8")
            raised = False
            try:
                offline.extract_report(log_dir, out_dir=out_stale,
                                       runner=FakeRunner(rc=1, make_report=False))
            except RuntimeError as ex:
                raised = "返回码 1" in str(ex)
            check("rc=1+仅陈旧产物 → RuntimeError（新鲜度守卫）", raised)

            # ── [4] 无报告产物（先清空历史报告，保证隔离）──
            if offline.reports_dir().is_dir():
                shutil.rmtree(offline.reports_dir())
            raised = False
            try:
                offline.extract_report(log_dir, runner=FakeRunner(make_report=False))
            except RuntimeError as ex:
                raised = "未找到报告产物" in str(ex)
            check("无报告 → RuntimeError", raised)

            # ── [5] latest_report（只认 all_report_*.html；md/他名 html 不干扰）──
            rd = offline.reports_dir()
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "all_report_old.html").write_text("<html>old</html>", encoding="utf-8")
            time.sleep(0.05)
            (rd / "all_report_new.html").write_text("<html>new</html>", encoding="utf-8")
            (rd / "all_extracted_new.md").write_text("# md 干扰项", encoding="utf-8")
            (rd / "role_dialog_new.html").write_text("<html>role</html>", encoding="utf-8")
            latest = offline.latest_report()
            check("latest 只取 all_report_*.html 最新",
                  latest is not None and latest.name == "all_report_new.html",
                  latest.name if latest else "None")

            # ── [6] on_log 转发 ──
            got: list[str] = []
            offline.extract_report(log_dir, on_log=lambda m, t="info": got.append(m),
                                   runner=FakeRunner(echo="hello-tool"))
            check("on_log 转发工具输出", got == ["hello-tool"], str(got))

            # ── [8] 输出重定向（2026-09-24 全按钮点验发现：替身装置污染真实 reports/）──
            out1 = tdir / "out1"
            r3 = FakeRunner()
            rep3 = offline.extract_report(log_dir, out_dir=out1, runner=r3)
            check("--out 传入 argv", r3.calls[0][-3:-1] == ["--out", str(out1)],
                  str(r3.calls[0]))
            check("报告从 out 目录收集", rep3.parent == out1, str(rep3))

            out2 = tdir / "out2"
            os.environ["CARTEST_LOGEXTRACT_OUT"] = str(out2)
            try:
                rep4 = offline.extract_report(log_dir, runner=FakeRunner())
            finally:
                os.environ.pop("CARTEST_LOGEXTRACT_OUT", None)
            check("环境变量回退生效", rep4.parent == out2, str(rep4))

            demo_src = (Path(__file__).resolve().parents[1]
                        / "tools" / "gui_demo.py").read_text(encoding="utf-8")
            check("替身装置已设输出隔离（防污染真实 reports/）",
                  "CARTEST_LOGEXTRACT_OUT" in demo_src)

            # ── [9] 产物定位只认根目录 HTML（2026-10-08 用户裁决：保持一份）──
            arch = tdir / "reports_arch"
            raw = arch / "_raw_20260929_100000"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "all_extracted_20260929_100000.md").write_text("# raw", encoding="utf-8")
            (arch / "all_report_20260929_100000.html").write_text("<html>", encoding="utf-8")
            found = offline._find_latest_html(arch)
            check("根目录 all_report_*.html 被定位",
                  found is not None and found.name == "all_report_20260929_100000.html",
                  found.name if found else "None")
            check("_raw_/ 内归档 md 不被误选（不进子目录）",
                  found is not None and found.parent == arch,
                  str(found.parent) if found else "None")

            # ── [10] 默认走隔离运行目录 + 提升到 reports/ 根（并发隔离；UX 不变）──
            if offline.reports_dir().is_dir():
                shutil.rmtree(offline.reports_dir())
            r5 = FakeRunner()
            rep5 = offline.extract_report(log_dir, runner=r5)
            out5 = Path(r5.calls[0][r5.calls[0].index("--out") + 1])
            check("默认 --out 指向 reports/ 下的隔离运行目录",
                  out5.parent == offline.reports_dir() and out5.name.startswith("_run_"),
                  str(out5))
            check("总报告已提升到 reports/ 根",
                  rep5.parent == offline.reports_dir()
                  and rep5.name.startswith("all_report_"), str(rep5))
            check("隔离运行目录已清理", not out5.exists(), str(out5))

            # ── [11] 定位优先取 stdout 契约行（不被 glob 的更新诱饵抢占）──
            out_ct = tdir / "out_ct"
            out_ct.mkdir(parents=True, exist_ok=True)
            older = out_ct / "all_report_20260101_000000.html"
            older.write_text("<html>contract</html>", encoding="utf-8")
            time.sleep(0.05)
            (out_ct / "all_report_20260101_000001.html").write_text(
                "<html>glob-newest</html>", encoding="utf-8")
            r6 = FakeRunner(make_report=False, emit_full_line=True, notify_path=older)
            rep6 = offline.extract_report(log_dir, out_dir=out_ct, runner=r6)
            check("stdout 契约行优先于 glob 最新", rep6 == older, str(rep6))
        finally:
            os.environ.pop("CARTEST_LOGEXTRACT_DIR", None)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("离线提取封装检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
