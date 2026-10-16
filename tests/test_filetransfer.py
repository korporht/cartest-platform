# -*- coding: utf-8 -*-
"""P3e 验收 — 大文件推送（core/filetransfer 库化 + 行为）

设计依据：13-P3e设计.md §三。FakeRunner 模拟设备文件系统（push 落盘 / md5sum / wc / 合并脚本执行）。

用法:
    python tests/test_filetransfer.py
"""
import hashlib
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core.filetransfer import FilePusher, build_merge_script, chunk_size_mb  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


class FakeResult:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRunner:
    """模拟 adb：push 落盘、md5sum/wc 读模拟盘、sh 合并脚本真合并"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.disk: dict[str, bytes] = {}
        self.device_present = True
        self.push_fail: dict[str, int] = {}

    def __call__(self, cmd, timeout=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if len(cmd) >= 2 and cmd[0] == "adb" and cmd[1] == "devices":
            out = "List of devices attached\n"
            if self.device_present:
                out += "SN_TEST\tdevice\n"
            return FakeResult(0, out)
        if len(cmd) >= 4 and cmd[1] == "push":
            local, remote = cmd[2], cmd[3]
            for sub, n in self.push_fail.items():
                if sub in remote and n > 0:
                    self.push_fail[sub] = n - 1
                    return FakeResult(1, "", "adb: error: failed to copy")
            self.disk[remote] = Path(local).read_bytes()
            return FakeResult(0, "1 file pushed")
        if len(cmd) >= 3 and cmd[1] == "shell":
            sh = cmd[2]
            m = re.match(r"md5sum (.+)$", sh)
            if m:
                p = m.group(1).strip().strip("'\"")
                if p in self.disk:
                    return FakeResult(0, hashlib.md5(self.disk[p]).hexdigest() + f"  {p}", "")
                return FakeResult(1, "", "No such file")
            m = re.match(r"wc -c < (.+)$", sh)
            if m:
                p = m.group(1).strip().strip("'\"")
                if p in self.disk:
                    return FakeResult(0, str(len(self.disk[p])), "")
                return FakeResult(1, "", "No such file")
            m = re.match(r"rm -f (.+)$", sh)
            if m:
                for p in m.group(1).split():
                    self.disk.pop(p.strip().strip("'\""), None)
                return FakeResult(0, "")
            if sh.startswith("mkdir -p") or sh.startswith("df "):
                return FakeResult(0, "99999999" if sh.startswith("df ") else "")
            if sh.startswith("sh "):
                script_path = sh[3:].strip()
                script = self.disk.get(script_path, b"").decode("utf-8", "ignore")
                if not script:
                    return FakeResult(1, "", "script missing")
                get = lambda k: re.search(rf"^{k}='?([^'\n]+)'?", script, re.M).group(1)  # noqa: E731
                chunk_dir, filename = get("CHUNK_DIR"), get("FILENAME")
                final, n = get("FINAL_PATH"), int(get("NUM_CHUNKS"))
                total = b""
                for i in range(n):
                    p = f"{chunk_dir}/{filename}.chunk{i:04d}"
                    if p not in self.disk:
                        return FakeResult(1, "", f"missing chunk: {p}")
                    total += self.disk[p]
                self.disk[final] = total
                for i in range(n):
                    self.disk.pop(f"{chunk_dir}/{filename}.chunk{i:04d}", None)
                return FakeResult(0, f"[$(date)] Merge OK size={len(total)}", "")
        if len(cmd) >= 2 and cmd[1] in ("root", "remount"):
            return FakeResult(0, "restarted as root" if cmd[1] == "root" else "")
        return FakeResult(0, "")


def make_file(tmp: Path, name: str, size: int) -> Path:
    p = tmp / name
    p.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return p


def main() -> int:
    print("=" * 70)
    print("P3e 验收 — 大文件推送（filetransfer）")
    print("=" * 70)
    tmp = Path(mkdtemp(prefix="p3e_push_"))

    print("\n[1] 分块策略")
    check("3GB → 500MB", chunk_size_mb(3 * 1024**3) == 500, str(chunk_size_mb(3 * 1024**3)))
    check("50GB（102 块）→ 上调 512MB", chunk_size_mb(50 * 1024**3) == 512,
          str(chunk_size_mb(50 * 1024**3)))
    check("≤100 块保持 500MB", chunk_size_mb(50_000 * 1024**2) == 500,
          str(chunk_size_mb(50_000 * 1024**2)))
    check("120GB → 封顶 1024MB", chunk_size_mb(120 * 1024**3) == 1024,
          str(chunk_size_mb(120 * 1024**3)))

    print("\n[2] 合并脚本内容")
    script = build_merge_script("/sdcard", "a.bin", 3, "/sdcard/a.bin")
    for token in ("NUM_CHUNKS=3", "FINAL_PATH=", "Merge OK", "rm -f", "exit 0"):
        check(f"含 {token}", token in script)

    print("\n[3] 小文件直推 + 校验")
    runner = FakeRunner()
    small = make_file(tmp, "small.bin", 64 * 1024)
    logs: list[str] = []
    events: list[tuple] = []
    r = FilePusher(runner=runner, on_log=lambda lv, m: logs.append(m),
                   on_progress=lambda *a: events.append(a)).push(small, "/sdcard/small.bin")
    check("ok=True", r.ok, r.message)
    check("落盘一致", runner.disk.get("/sdcard/small.bin") == small.read_bytes())
    check("push 恰好 1 次", sum(1 for c in runner.calls if c[1] == "push") == 1)

    print("\n[4] 小文件 MD5 不符 → 失败")
    runner2 = FakeRunner()
    orig_md5 = hashlib.md5(small.read_bytes()).hexdigest()

    class BadMd5(FakeRunner):
        def __call__(self, cmd, timeout=None):
            res = super().__call__(cmd, timeout)
            if cmd[1] == "shell" and cmd[2].startswith("md5sum") and res.returncode == 0:
                return FakeResult(0, "0" * 32 + "  x", "")
            return res

    runner2 = BadMd5()
    r2 = FilePusher(runner=runner2).push(small, "/sdcard/small2.bin")
    check("ok=False 且提示校验", (not r2.ok) and ("校验" in r2.message or "MD5" in r2.message),
          r2.message)

    print("\n[5] 大文件全流程（3 块）")
    big = make_file(tmp, "big.bin", 2 * 1024 * 1024 + 12345)  # ~2MB → chunk_mb=1 时 3 块
    runner3 = FakeRunner()
    progress: list[tuple] = []
    pusher = FilePusher(runner=runner3, on_progress=lambda *a: progress.append(a))
    r3 = pusher.push(big, "/sdcard/big.bin", chunk_mb=1)
    check("ok=True", r3.ok, r3.message)
    pushes = [c for c in runner3.calls if c[1] == "push"]
    check("push 次数 = 3 块 + 1 脚本", len(pushes) == 4, f"{len(pushes)}")
    check("设备端合并结果一致", runner3.disk.get("/sdcard/big.bin") == big.read_bytes())
    check("本地分块已清理", not list(tmp.glob("big.bin.chunk*")))
    check("进度回调递增", bool(progress) and progress[-1][2] >= 60 and
          all(progress[i][2] <= progress[i + 1][2] for i in range(len(progress) - 1)),
          str(progress[-1] if progress else []))

    print("\n[6] 重试（第 2 块失败 2 次后成功）")
    runner4 = FakeRunner()
    runner4.push_fail[".chunk0001"] = 2
    pusher4 = FilePusher(runner=runner4, retry=3, retry_delay=0.0)
    r4 = pusher4.push(big, "/sdcard/retry.bin", chunk_mb=1)
    chunk1_pushes = [c for c in runner4.calls if c[1] == "push" and c[3].endswith(".chunk0001")]
    check("最终成功", r4.ok, r4.message)
    check("chunk0001 共推送 3 次", len(chunk1_pushes) == 3, str(len(chunk1_pushes)))

    print("\n[7] 取消：清理本地与设备端分块")
    runner5 = FakeRunner()
    cancel = threading.Event()

    def on_prog(i, total, pct, mbps, eta):
        if i >= 1:
            cancel.set()

    pusher5 = FilePusher(runner=runner5, cancel=cancel, on_progress=on_prog)
    r5 = pusher5.push(big, "/sdcard/cancel.bin", chunk_mb=1)
    check("ok=False 且提示取消", (not r5.ok) and "取消" in r5.message, r5.message)
    check("本地无残留分块", not list(tmp.glob("big.bin.chunk*")))
    check("设备端无残留分块",
          not [k for k in runner5.disk if k.endswith(".chunk0000") or
               k.endswith(".chunk0001") or k.endswith(".chunk0002")],
          str([k for k in runner5.disk]))

    print("\n[8] 无设备 → 快速失败")
    runner6 = FakeRunner()
    runner6.device_present = False
    r6 = FilePusher(runner=runner6).push(small, "/sdcard/x.bin")
    check("ok=False 且提示设备", (not r6.ok) and ("设备" in r6.message), r6.message)
    check("未发生流传输", all(c[1] != "push" for c in runner6.calls))

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("大文件推送检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
