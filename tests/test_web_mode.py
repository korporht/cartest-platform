# -*- coding: utf-8 -*-
"""P3d-4 验收 — Web 模式常驻修复（"静默死亡"根因）

背景：flet 0.85 `AppView.FLET_APP_WEB` 会额外拉起 flet_desktop 桌面壳作为主客户端：
`open_flet_view_async` → `fvp.wait()`；壳断连/关闭时 wait 返回 → `finally: conn.close()`
→ 整个应用退出。表现为启动后数十秒~数分钟「静默死亡」（无任何日志/traceback）。

修复：web 模式设置 `FLET_FORCE_WEB_SERVER=1`（flet 内部据此强制 view=WEB_BROWSER：
不拉桌面壳、不自动开浏览器、`terminate.wait()` 常驻）。

用法:
    python tests/test_web_mode.py
"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import flet as ft  # noqa: E402

# 注意：cartest.app 包属性 main 被同名函数遮蔽 → 用 importlib 取模块
main_mod = importlib.import_module("cartest.app.main")

SRC = (ROOT / "cartest" / "app" / "main.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 70)
    print("P3d-4 验收 — Web 模式常驻修复（静默死亡根因）")
    print("=" * 70)

    calls: list = []

    def fake_run(*args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs, os.environ.get("FLET_FORCE_WEB_SERVER")))

    orig_run = main_mod.ft.run
    main_mod.ft.run = fake_run
    try:
        print("\n[1] web=True：强制纯 Web 服务器")
        os.environ.pop("FLET_FORCE_WEB_SERVER", None)
        main_mod.run_app(web=True, port=8611)
        check("调用 ft.run 一次", len(calls) == 1, str(len(calls)))
        _, kwargs, env_at_call = calls[-1]
        check("调用时已设置 FLET_FORCE_WEB_SERVER=1（flet 将跳过桌面壳）",
              env_at_call == "1", str(env_at_call))
        check("传入端口", kwargs.get("port") == 8611, str(kwargs.get("port")))
        check("view=FLET_APP_WEB（由 env 强制为纯服务器）",
              kwargs.get("view") == ft.AppView.FLET_APP_WEB, str(kwargs.get("view")))

        print("\n[2] web=False：桌面模式不受影响")
        calls.clear()
        os.environ.pop("FLET_FORCE_WEB_SERVER", None)
        main_mod.run_app(web=False)
        check("桌面模式不设置该环境变量",
              os.environ.get("FLET_FORCE_WEB_SERVER") is None,
              str(os.environ.get("FLET_FORCE_WEB_SERVER")))
        args2, kwargs2, _ = calls[-1]
        check("桌面模式不传 port/view（默认桌面窗口）",
              kwargs2.get("port") is None and kwargs2.get("view") is None, str(kwargs2))
    finally:
        main_mod.ft.run = orig_run
        os.environ.pop("FLET_FORCE_WEB_SERVER", None)

    print("\n[3] 源码断言")
    check("main.py 含 FLET_FORCE_WEB_SERVER", "FLET_FORCE_WEB_SERVER" in SRC)
    check("main.py 引入 os 模块", "import os" in SRC)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("Web 模式常驻修复检查通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
