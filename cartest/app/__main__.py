"""``python -m cartest.app`` 入口 —— 启动 GUI（单例：同机只允许一个实例）

用法：
    python -m cartest.app              桌面窗口（默认）
    python -m cartest.app --web        浏览器模式（便于自动化验证），默认端口 8560
    python -m cartest.app --web --port 8600
    python -m cartest.app --web --restart   先清理已运行实例再启动（承接 bat 的"杀旧起新"）
"""
import argparse
import sys

from ..core import logsetup
from .main import run_app
from .singleton import ensure_single


def main(argv: list[str] | None = None) -> int:
    logsetup.setup()
    parser = argparse.ArgumentParser(prog="python -m cartest.app", description="CarTest 驾驶舱")
    parser.add_argument("--web", action="store_true", help="以 Web 模式启动")
    parser.add_argument("--port", type=int, default=8560, help="Web 模式端口（默认 8560）")
    parser.add_argument("--restart", action="store_true",
                        help="先清理已在运行的实例（单例守卫），再启动")
    args = parser.parse_args(argv)

    conflict = ensure_single("web" if args.web else "desktop",
                             port=args.port if args.web else None,
                             restart=args.restart)
    if conflict:
        print(f"[CarTest] {conflict}")
        return 1

    run_app(web=args.web, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
