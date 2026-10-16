# -*- coding: utf-8 -*-
"""Monkey 框架验收 — 模型与纯逻辑（离线，无设备）

覆盖：MonkeyStatus.from_files / parse_status_line / build_start_cmd /
build_clean_cmds / eta_seconds / find_latest_report / 工具目录定位。

设计依据：19-Monkey压测集成设计.md §三。

用法:
    python tests/test_monkey.py
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("Monkey 框架验收 — 模型与纯逻辑（离线）")
    print("=" * 70)

    from cartest.core import monkey as mk

    # ── [1] 状态合成 ──
    st = mk.MonkeyStatus.from_files(
        lock=True, done=False,
        status_text="RUNNING|2026-09-16 10:00:00|12000|300|com.example.carvoice",
        pid="12345")
    check("running：lock 优先", st.phase == "running", st.phase)
    check("status 行解析",
          st.pid == "12345" and st.started == "2026-09-16 10:00:00"
          and st.count == "12000" and st.throttle == "300"
          and st.pkg == "com.example.carvoice",
          f"{st.pid}/{st.count}/{st.pkg}")
    check("done 状态", mk.MonkeyStatus.from_files(lock=False, done=True).phase == "done")
    check("idle 状态", mk.MonkeyStatus.from_files(lock=False, done=False).phase == "idle")
    check("空 status 容错",
          mk.MonkeyStatus.from_files(lock=True, done=False, status_text="").count == "")
    check("单字段行容错", mk.parse_status_line("RUNNING|t1") == {
        "state": "RUNNING", "started": "t1", "count": "", "throttle": "", "pkg": ""})

    # ── [2] 命令构造 ──
    cmd = mk.build_start_cmd("com.x", 100, 50)
    check("启动命令（nohup 脱离 shell）",
          cmd == "nohup sh /sdcard/monkey_runner.sh com.x 100 50 > /dev/null 2>&1 &", cmd)
    check("清理开关全关 → 空", mk.build_clean_cmds(False, False) == [])
    c1 = mk.build_clean_cmds(True, False)
    check("清理 monkey 日志", len(c1) == 1 and "monkey_running.lock" in c1[0])
    c2 = mk.build_clean_cmds(True, True)
    check("清理 dropbox（追加且顺序在后）",
          len(c2) == 2 and "dropbox" in c2[1] and "monkey_running.lock" in c2[0])
    check("eta 计算（50000×300ms = 15000s）", mk.eta_seconds(50000, 300) == 15000)

    # ── [3] 报告定位 ──
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        check("空目录 → None", mk.find_latest_report(d) is None)
        (d / "crash_test_report_a.html").write_text("x", encoding="utf-8")
        time.sleep(0.05)
        (d / "crash_test_report_b.html").write_text("y", encoding="utf-8")
        (d / "crash_test_report_b.md").write_text("z", encoding="utf-8")
        r = mk.find_latest_report(d)
        check("最新 html 命中", bool(r) and r.path_html.endswith("crash_test_report_b.html"),
              r.path_html if r else "None")
        check("同名 md 关联", bool(r) and r.path_md.endswith("crash_test_report_b.md"),
              r.path_md if r else "None")

    # ── [4] 工具目录 ──
    check("tool_dir 支持环境变量覆盖语义（默认含工具名）",
          "Monkey" in str(mk.tool_dir()))
    if mk.tool_dir().is_dir():
        check("available()（本机工具目录存在）", mk.available() is True)
    else:
        print("  [SKIP] 本机无 Monkey 工具目录（available 检查跳过）")

    # ── [5] Controller（替身 adb + 临时工具目录）──
    class _FakeAdb:
        def __init__(self, *, lock=False, done=False, pid="", status=""):
            self.calls = []
            self.lock = lock
            self.done = done
            self.pid = pid
            self.status = status

        def root(self, *a, **k):
            self.calls.append(("root",))
            return True

        def remount(self, *a, **k):
            self.calls.append(("remount",))
            return True

        def shell(self, cmd, *a, **k):
            self.calls.append(("shell", cmd))
            if cmd.startswith(f"test -f {mk.DEV_LOCK}"):
                marker = "LOCKED" if "LOCKED" in cmd else "1"
                return (self.lock, marker if self.lock else "")
            if cmd.startswith(f"test -f {mk.DEV_DONE}"):
                return (self.done, "1" if self.done else "")
            if cmd.startswith(f"cat {mk.DEV_STATUS}"):
                return (bool(self.status), self.status)
            if cmd.startswith(f"cat {mk.DEV_PID}"):
                return (bool(self.pid), self.pid)
            if cmd.startswith("logcat -d"):
                return (True, "09-16 log line")
            if cmd.startswith("ls /sdcard/monkey_output_"):
                return (True,
                        "/sdcard/monkey_output_20260917_163753.txt\n"
                        "/sdcard/monkey_status.txt\n"
                        "/sdcard/monkey_done.txt\n"
                        "/sdcard/monkey_runner.sh")
            return (True, "")

        def push(self, local, remote, *a, **k):
            self.calls.append(("push", str(local), remote))
            return (True, "1 file pushed")

        def pull(self, remote, local, *a, **k):
            self.calls.append(("pull", remote, str(local)))
            return (True, "pulled")

    with tempfile.TemporaryDirectory() as td2:
        tools = Path(td2)
        for n in ("monkey.jar", "monkey", "monkey_runner.sh"):
            (tools / n).write_text("x", encoding="utf-8")
        # 假分析器：向 cwd（工具目录）写报告 + 回放一行输出，验证 analyze 定位与转发链路
        (tools / "crash_anr_analyzer.py").write_text(
            "import pathlib\n"
            "print('fake-analyzer-line')\n"
            "pathlib.Path('crash_test_report_fake.html').write_text('r')\n"
            "pathlib.Path('crash_test_report_fake.md').write_text('r')\n",
            encoding="utf-8")

        # 部署
        adb = _FakeAdb()
        ctl = mk.MonkeyController(adb=adb, tool_dir_override=tools)
        r = ctl.deploy()
        kinds = [c[0] for c in adb.calls]
        check("部署：root→remount→3 push→chmod",
              r.ok and kinds[0] == "root" and kinds[1] == "remount"
              and kinds.count("push") == 3 and kinds[-1] == "shell"
              and "chmod 755" in adb.calls[-1][1], f"{r.detail}")
        with tempfile.TemporaryDirectory() as td3:
            r2 = mk.MonkeyController(
                adb=_FakeAdb(), tool_dir_override=td3).deploy()
            check("部署：缺文件报错", (not r2.ok) and "缺少文件" in r2.detail, r2.detail)

        # 部署：系统分区只读（发现 #4 —— remount 需重启场景指引）
        class _ReadOnlyAdb(_FakeAdb):
            def shell(self, cmd, *a, **k):
                if cmd.startswith("test -w /system/framework"):
                    return (False, "")
                return super().shell(cmd, *a, **k)

        r2b = mk.MonkeyController(
            adb=_ReadOnlyAdb(), tool_dir_override=tools).deploy()
        check("部署：只读 → 指引（root+remount/重启）",
              (not r2b.ok) and "只读" in r2b.detail and "重启" in r2b.detail,
              r2b.detail)

        # 启动
        r3 = mk.MonkeyController(
            adb=_FakeAdb(lock=True), tool_dir_override=tools
        ).start(mk.MonkeyConfig(pkg="com.x"))
        check("启动：lock 防重入", (not r3.ok) and "已有运行中" in r3.detail, r3.detail)

        adb4 = _FakeAdb()
        r4 = mk.MonkeyController(adb=adb4, tool_dir_override=tools).start(
            mk.MonkeyConfig(pkg="com.x", count=1000, throttle=100,
                            clean_logs=True, clean_dropbox=True))
        shells = [c[1] for c in adb4.calls if c[0] == "shell"]
        check("启动：清理 + nohup 启动 + 预计时长",
              r4.ok and any("rm -f" in s for s in shells)
              and any("dropbox" in s for s in shells)
              and any(s.startswith("nohup sh") for s in shells)
              and "分钟" in r4.detail, r4.detail)

        # 状态
        st5 = mk.MonkeyController(
            adb=_FakeAdb(lock=True, pid="777", status="RUNNING|T1|500|300|com.x"),
            tool_dir_override=tools).status()
        check("状态：running + 解析",
              st5.phase == "running" and st5.pid == "777" and st5.count == "500",
              f"{st5.phase}/{st5.pid}/{st5.count}")

        # 停止
        adb6 = _FakeAdb(pid="888")
        r6 = mk.MonkeyController(adb=adb6, tool_dir_override=tools).stop()
        shells6 = [c[1] for c in adb6.calls if c[0] == "shell"]
        check("停止：kill + 清锁（保留数据）",
              r6.ok and any("kill 888" in s for s in shells6)
              and any("rm -f" in s for s in shells6), r6.detail)
        adb7 = _FakeAdb()
        r7 = mk.MonkeyController(adb=adb7, tool_dir_override=tools).stop()
        shells7 = [c[1] for c in adb7.calls if c[0] == "shell"]
        check("停止：无 PID → grep 兜底",
              r7.ok and any("grep monkey.Monkey" in s for s in shells7))

        # 拉取
        with tempfile.TemporaryDirectory() as td4:
            dest = Path(td4) / "dropbox_archive"
            adb8 = _FakeAdb()
            r8 = mk.MonkeyController(adb=adb8, tool_dir_override=tools).pull(dest)
            pulls = [c[1] for c in adb8.calls if c[0] == "pull"]
            check("拉取：dropbox/输出/traces + logcat 落盘",
                  r8.ok and any("dropbox" in p for p in pulls)
                  and any("monkey_output" in p for p in pulls)
                  and any("traces" in p for p in pulls)
                  and (dest / "logcat.txt").is_file(), r8.detail)
            check("拉取：运行件显式拉回（发现 #6）",
                  all(any(p.endswith(n) for p in pulls) for n in
                      ("monkey_output_20260917_163753.txt", "monkey_status.txt",
                       "monkey_done.txt", "monkey_runner.sh")), str(pulls))

        # 分析 + 历史报告
        with tempfile.TemporaryDirectory() as td5:
            ctl9 = mk.MonkeyController(adb=_FakeAdb(), tool_dir_override=tools)
            got_an: list[str] = []
            rep = ctl9.analyze(Path(td5), "com.x",
                               on_log=lambda m, t="info": got_an.append(m))
            check("分析：子进程产出报告定位",
                  bool(rep) and rep.path_html.endswith("crash_test_report_fake.html"),
                  rep.path_html if rep else "None")
            check("分析输出回传（发现 #5）",
                  any("fake-analyzer-line" in m for m in got_an), str(got_an))
            lst = ctl9.list_reports()
            check("历史报告列表（md/html 配对）",
                  len(lst) >= 1 and lst[0].path_html.endswith(".html")
                  and lst[0].path_md.endswith(".md"))

    # ── [6] monkey_service 一键流（替身 controller）──
    from cartest.app.tools.services.monkey_service import MonkeyService

    class _FakeCtl:
        def __init__(self, phases=("running", "running", "done")):
            self.started = False
            self.pulled = False
            self.analyzed = False
            self._phases = list(phases)

        def start(self, cfg):  # noqa: ANN001
            self.started = True
            return mk.StepResult(True, "启动ok")

        def status(self):
            ph = self._phases.pop(0) if self._phases else "done"
            return mk.MonkeyStatus(phase=ph)

        def pull(self, dest):  # noqa: ANN001
            self.pulled = True
            return mk.StepResult(True, "拉取ok")

        def analyze(self, dest, pkg, on_log=None):  # noqa: ANN001
            self.analyzed = True
            return mk.MonkeyReport(path_html="r.html")

    def _join(svc, timeout=5.0):
        deadline = time.time() + timeout
        while svc.running and time.time() < deadline:
            time.sleep(0.02)

    logs: list[str] = []
    results: list[tuple] = []
    fc = _FakeCtl()
    svc = MonkeyService(controller=fc, on_log=logs.append,
                        on_result=lambda ok, d: results.append((ok, d)),
                        poll_interval=0.01)
    with tempfile.TemporaryDirectory() as td6:
        svc.run_auto(mk.MonkeyConfig(pkg="com.x"), td6)
        _join(svc)
    check("一键流：启动→轮询→拉取→分析", fc.started and fc.pulled and fc.analyzed)
    check("一键流：成功回调",
          bool(results) and results[-1][0] is True,
          str(results[-1] if results else None))
    check("一键流：阶段日志 1/4→4/4",
          any("[1/4]" in m for m in logs) and any("[4/4]" in m for m in logs))

    # 取消：永不 done → cancel 后不进拉取/分析
    slow = _FakeCtl(phases=[])
    slow._phases = []
    # 让 status 永远 running
    slow.status = lambda: mk.MonkeyStatus(phase="running")  # type: ignore[method-assign]
    svc2 = MonkeyService(controller=slow, poll_interval=0.02)
    with tempfile.TemporaryDirectory() as td7:
        svc2.run_auto(mk.MonkeyConfig(pkg="com.x"), td7)
        time.sleep(0.1)
        svc2.cancel()
        _join(svc2)
    check("一键流：取消生效（不拉取/不分析）",
          (not svc2.running) and not slow.pulled and not slow.analyzed)

    # ── [7] 接线（注册表 / 侧栏 / 页面壳）──
    import importlib

    tp = importlib.import_module("cartest.app.tools.pages")
    main_mod = importlib.import_module("cartest.app.main")
    pages_src = (ROOT / "cartest" / "app" / "tools" / "pages.py").read_text(encoding="utf-8")

    check("BUILDERS 含 tool_monkey", "tool_monkey" in tp.BUILDERS)
    check("build_monkey 页面壳（MonkeyTab + _wrap）",
          "def build_monkey(" in pages_src and "MonkeyTab" in pages_src)
    groups = {g: [k for k, _, _ in entries] for g, entries in main_mod.PAGE_GROUPS}
    check("侧栏：通用工具含 Monkey 压测", "tool_monkey" in groups.get("通用工具", []),
          str(groups.get("通用工具")))
    check("PAGE_META 注册", "tool_monkey" in main_mod.PAGE_META)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("Monkey 模型与纯逻辑检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
