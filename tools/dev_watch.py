# -*- coding: utf-8 -*-
"""CarTest 开发热重载 —— 改代码自动重启，浏览器按 F5 即见效果

用法：
    python tools/dev_watch.py                  # Web 模式（默认 8560），监视 cartest/
    python tools/dev_watch.py --port 8600
    python tools/dev_watch.py --desktop        # 桌面窗口模式（重启=关窗开窗）
    python tools/dev_watch.py --interval 0.5   # 扫描间隔（秒）

流程：本脚本常驻 → 子进程跑 ``python -m cartest.app`` → 检测 ``cartest/`` 下 .py 变化
→ 重启子进程 → 浏览器 F5 看新效果。改出语法错误也不会卡死：子进程崩了就等下次修改。

**为什么不用 ``flet run -r``**：本项目入口 ``cartest.app.__main__`` 带 argparse + 单例守卫，
flet CLI 无法把 ``--web/--port`` 透传给脚本；且 web 启动逻辑（``FLET_FORCE_WEB_SERVER``、
端口、单例）是自有的（P3d-4 修复）。自写脚本反而更简单可控。

**注意**：重启走 ``--restart``（单例守卫清理旧实例），所以本机**其他** CarTest 实例会被关掉
——dev 场景属预期，别在用户正常使用中同时开。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH_DIR = ROOT / "cartest"
#: 不参与监视的目录名（运行产物 / 缓存 / 非源码）
IGNORE_PARTS = {"__pycache__", ".git", "logs", "runs", "data", "evidence", "archive"}


def fingerprint(root: Path) -> dict[str, tuple[float, int]]:
    """源码指纹：路径 → (mtime, size)。标准库轮询，无第三方依赖。"""
    out: dict[str, tuple[float, int]] = {}
    for p in root.rglob("*.py"):
        if any(part in IGNORE_PARTS for part in p.parts):
            continue
        try:
            st = p.stat()
        except OSError:                      # 文件刚被删/改名
            continue
        out[str(p)] = (st.st_mtime, st.st_size)
    return out


def diff_keys(before: dict, after: dict) -> list[str]:
    """变化的文件（新增 / 修改 / 删除）"""
    keys = [k for k, v in after.items() if before.get(k) != v]
    keys += [k for k in before if k not in after]
    return keys


class Runner:
    """子进程管理（启动 / 优雅终止 / 兜底强杀）"""

    def __init__(self, web: bool, port: int) -> None:
        self.web = web
        self.port = port
        self.proc: subprocess.Popen | None = None

    def cmd(self) -> list[str]:
        cmd = [sys.executable, "-m", "cartest.app"]
        if self.web:
            cmd += ["--web", "--port", str(self.port)]
        cmd.append("--restart")              # 兜底：清掉可能残留的旧实例
        return cmd

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.stop()
        self.proc = subprocess.Popen(self.cmd(), cwd=str(ROOT))

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None


def main(argv: list[str] | None = None) -> int:
    try:                                     # 非 tty（重定向/后台）时块缓冲会让日志迟迟不出现
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(prog="python tools/dev_watch.py",
                                 description="CarTest 开发热重载（改代码自动重启）")
    ap.add_argument("--desktop", action="store_true", help="桌面窗口模式（默认 Web）")
    ap.add_argument("--port", type=int, default=8560, help="Web 模式端口（默认 8560）")
    ap.add_argument("--interval", type=float, default=0.8, help="扫描间隔秒（默认 0.8）")
    ap.add_argument("--debounce", type=float, default=0.5,
                    help="防抖秒：连续写入等稳定后再重启（默认 0.5）")
    args = ap.parse_args(argv)

    runner = Runner(web=not args.desktop, port=args.port)
    fp = fingerprint(WATCH_DIR)
    print(f"[dev] 监视 {WATCH_DIR}（{len(fp)} 个 .py）")
    print(f"[dev] 启动：{' '.join(runner.cmd())}")
    runner.start()
    if not args.desktop:
        print(f"[dev] 浏览器打开 http://127.0.0.1:{args.port}"
              "（改代码后自动重启，按 F5 看新效果）")
    print("[dev] Ctrl+C 退出")

    try:
        while True:
            time.sleep(args.interval)

            if runner.proc is not None and runner.proc.poll() is not None:
                print(f"\n[dev] 子进程已退出（code={runner.proc.returncode}）"
                      "—— 修好代码后会自动重启")
                runner.proc = None

            new = fingerprint(WATCH_DIR)
            hits = diff_keys(fp, new)
            if not hits:
                continue

            time.sleep(args.debounce)         # 防抖：编辑器连续写盘时只重启一次
            new = fingerprint(WATCH_DIR)
            hits = diff_keys(fp, new)
            if not hits:
                continue
            fp = new

            rel = "、".join(str(Path(k).relative_to(ROOT)) for k in hits[:5])
            more = f" 等 {len(hits)} 处" if len(hits) > 5 else ""
            print(f"\n[dev] 变化：{rel}{more} → 重启 …")
            runner.start()
            print("[dev] 已重启" + ("" if args.desktop else "，浏览器按 F5 查看"))
    except KeyboardInterrupt:
        print("\n[dev] 退出，清理子进程 …")
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
