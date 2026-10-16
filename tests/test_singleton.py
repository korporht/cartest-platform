# -*- coding: utf-8 -*-
"""P3d-2 验收 — 单例守卫（命名互斥 + 锁文件 + --restart 定向清理）

覆盖：
  1) 首次获取成功并写入锁文件（pid/mode）
  2) 同进程二次获取 → 拦截消息（含"已在运行"）
  3) 子进程持有互斥 → 拦截；--restart → 终止子进程后获取成功
  4) 陈旧锁文件（互斥空闲）→ 正常获取并覆盖
  5) Web 端口被占（本进程监听）→ 端口占用提示（不误杀）
  6) probe_instances / cleanup_instances → 探测残留实例并定向清理（不误报自身）
  7) 互斥被占但无 pid（旧版实例）→ 哨兵条目，cleanup 不误杀

用法:
    python tests/test_singleton.py
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.app import singleton  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def _spawn_holder(name: str, lock_file: Path, port: int) -> tuple[subprocess.Popen, int]:
    """启动一个真正持有命名互斥的子进程，返回 ``(启动器句柄, 持锁进程 pid)``。

    ``pid`` 取自子进程**自报**的 ``os.getpid()``（与它写进锁文件的值同源）。

    **不要用 ``Popen.pid`` 代替它**：本机 venv 的 ``python.exe`` 是启动器，会再拉起
    真正的解释器作为子进程，实测 ``Popen.pid`` ≠ 解释器自身 ``os.getpid()``
    （两个进程同时存活，是父子关系）。产品语义上要判定的、``--restart`` 要杀的，
    都是"持锁进程"——即锁文件里记录的那个 pid。
    """
    code = (
        "import ctypes, json, os, time\n"
        "h = ctypes.WinDLL('kernel32', use_last_error=True)"
        f".CreateMutexW(None, False, {name!r})\n"
        f"open({str(lock_file)!r}, 'w', encoding='utf-8').write("
        f"json.dumps({{'pid': os.getpid(), 'mode': 'web', 'port': {port}}}))\n"
        "print('ready', os.getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    line = (proc.stdout.readline() or "").strip()
    if not line.startswith("ready"):
        proc.kill()
        raise RuntimeError(f"子进程未就绪: {line!r}")
    return proc, int(line.split()[1])


def _join(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """等启动器进程退出（轮询，替代固定 sleep 的脆时序）；返回是否已退出。

    启动器会等待其子进程（真正的解释器）结束，所以持锁进程被杀后它随即退出。
    """
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    return proc.poll() is not None


def main() -> int:
    tmp = Path(mkdtemp(prefix="p3d2_single_"))
    print("=" * 70)
    print("P3d-2 验收 — 单例守卫")
    print(f"临时目录: {tmp}")
    print("=" * 70)

    print("\n[1] 首次获取 + 锁文件")
    lf1 = tmp / "a.lock"
    name1 = "Local\\CarTest_Test_First"
    r = singleton.ensure_single("desktop", lock_file=lf1, name=name1)
    check("首次获取成功（None）", r is None, str(r))
    info = json.loads(lf1.read_text(encoding="utf-8"))
    check("锁文件 pid 为本进程", info.get("pid") == os.getpid(), str(info))
    check("锁文件 mode=desktop", info.get("mode") == "desktop", str(info))

    print("\n[2] 同进程二次获取 → 拦截")
    r2 = singleton.ensure_single("desktop", lock_file=lf1, name=name1)
    check("返回拦截消息", isinstance(r2, str) and "已在运行" in r2, str(r2))
    check("消息含模式", "mode=desktop" in (r2 or ""), str(r2))

    print("\n[3] 子进程持有互斥 → 拦截；--restart 终止后获取成功")
    name3 = "Local\\CarTest_Test_Sub"
    lf3 = tmp / "c.lock"
    sub = None
    try:
        sub, holder = _spawn_holder(name3, lf3, port=9999)
        check("子进程已就绪（持有互斥）", singleton.is_running(name3) is True)
        r3 = singleton.ensure_single("desktop", lock_file=lf3, name=name3)
        check("拦截消息含持锁进程 PID", isinstance(r3, str) and str(holder) in r3, str(r3))
        r4 = singleton.ensure_single("desktop", restart=True, lock_file=lf3, name=name3)
        check("--restart 后获取成功（None）", r4 is None, str(r4))
        # 本进程此时已接管互斥，不能用 is_running 判旧持锁者是否死（同进程也会恒 True）
        check("持锁进程已被终止", _join(sub) and not singleton._alive(holder),
              f"holder={holder} poll={sub.poll()}")
    finally:
        if sub is not None and sub.poll() is None:
            sub.kill()

    print("\n[4] 陈旧锁文件（互斥空闲）→ 正常获取并覆盖")
    name4 = "Local\\CarTest_Test_Stale"
    lf4 = tmp / "s.lock"
    lf4.write_text('{"pid": 999999, "mode": "web", "port": 1}', encoding="utf-8")
    r5 = singleton.ensure_single("desktop", lock_file=lf4, name=name4)
    check("获取成功", r5 is None, str(r5))
    info4 = json.loads(lf4.read_text(encoding="utf-8"))
    check("锁文件已被覆盖为本进程", info4.get("pid") == os.getpid(), str(info4))

    print("\n[5] Web 端口被占（本进程监听）→ 友好提示（不误杀）")
    name5 = "Local\\CarTest_Test_Port"
    lf5 = tmp / "p.lock"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        r6 = singleton.ensure_single("web", port=port, lock_file=lf5, name=name5)
        check("返回端口占用提示", isinstance(r6, str) and f"端口 {port}" in r6, str(r6))
        check("提示含占用者 PID", str(os.getpid()) in (r6 or ""), str(r6))
    finally:
        srv.close()

    print("\n[6] 探测 / 清理残留实例（probe_instances / cleanup_instances）")
    name6 = "Local\\CarTest_Test_Probe"
    lf6 = tmp / "probe.lock"
    sub6 = None
    try:
        sub6, holder6 = _spawn_holder(name6, lf6, port=9998)
        check("子进程已就绪（持有互斥）", singleton.is_running(name6) is True)
        found = singleton.probe_instances((), name=name6, lock_file=lf6)
        pids = [i["pid"] for i in found]
        check("探测到锁文件 pid", holder6 in pids, str(found))
        check("来源标注为锁文件", any("锁文件" in i["source"] for i in found), str(found))
        check("不误报自身", os.getpid() not in pids, str(found))

        done = singleton.cleanup_instances((), name=name6, lock_file=lf6, wait=5.0)
        check("清理项标记 killed", bool(done) and all(i.get("killed") for i in done), str(done))
        check("持锁进程已被终止", _join(sub6) and not singleton._alive(holder6),
              f"holder={holder6} poll={sub6.poll()}")
        check("清理后 is_running=False", singleton.is_running(name6) is False)

        empty = singleton.probe_instances((), name=name6, lock_file=lf6)
        check("清理后探测为空（互斥空闲）", empty == [], str(empty))
    finally:
        if sub6 is not None and sub6.poll() is None:
            sub6.kill()

    print("\n[7] 互斥被占但无 pid（旧版实例）→ 哨兵条目（probe 不炸、cleanup 不误杀）")
    name7 = "Local\\CarTest_Test_Sentinel"
    lf7 = tmp / "sentinel.lock"
    h7 = ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW(None, False, name7)
    try:
        sentinel = singleton.probe_instances((), name=name7, lock_file=lf7)
        check("返回哨兵（pid=0）", len(sentinel) == 1 and sentinel[0]["pid"] == 0, str(sentinel))
        check("哨兵标注互斥体", "互斥体" in sentinel[0]["source"], str(sentinel))
        singleton.cleanup_instances((), name=name7, lock_file=lf7, wait=0.5)
        check("本进程仍存活（未自杀）", True)
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(h7)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("单例守卫测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
