# -*- coding: utf-8 -*-
"""CarTest tools — Monkey 一键流后台编排

职责：启动 → 轮询 done → 拉取 → 分析（后台线程；事件回调驱动 UI）。
线程生命周期：``cancel()`` 置停止事件；UI 在 ``on_hide`` 必须调用。

设计依据：``19-Monkey压测集成设计.md`` §3.2。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ....core import monkey as monkey_mod


class MonkeyService:
    """一键流：``run_auto(cfg, dest)`` 非阻塞；``cancel()`` 可中断等待"""

    def __init__(self, controller, *, on_log: Callable[[str], None] | None = None,
                 on_result: Callable[[bool, str], None] | None = None,
                 poll_interval: float = 60.0) -> None:
        self._ctl = controller
        self._on_log = on_log or (lambda msg: None)
        self._on_result = on_result or (lambda ok, detail: None)
        self._poll_interval = poll_interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        """请求取消（不再拉取/分析；车机端 monkey 不受影响）"""
        self._stop.set()

    def run_auto(self, cfg: monkey_mod.MonkeyConfig, dest_dir: str | Path) -> None:
        """启动后台一键流（已在运行则拒绝并记日志）"""
        if self.running:
            self._on_log("[WARN] 已有 monkey 任务在执行")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._work, args=(cfg, Path(dest_dir)), daemon=True)
        self._thread.start()

    # ── 内部 ──
    def _work(self, cfg: monkey_mod.MonkeyConfig, dest: Path) -> None:
        ctl = self._ctl
        try:
            self._on_log("[1/4] 启动 monkey…")
            r = ctl.start(cfg)
            if not r.ok:
                self._on_result(False, r.detail)
                return
            self._on_log(r.detail)

            total = monkey_mod.eta_seconds(cfg.count, cfg.throttle)
            self._on_log(f"[2/4] 等待完成（预计 {total // 60} 分钟；可取消）…")
            waited = 0.0
            while not self._stop.is_set():
                st = ctl.status()
                if st.phase == "done":
                    break
                if st.phase == "idle" and waited >= self._poll_interval:
                    self._on_log("[WARN] 未检测到运行锁（可能异常退出/手动停止），转入拉取分析")
                    break
                if self._stop.wait(self._poll_interval):
                    break
                waited += self._poll_interval

            if self._stop.is_set():
                self._on_result(False, "已取消（monkey 仍在车机端运行，可稍后手动拉取）")
                return

            self._on_log("[3/4] 拉取日志…")
            r = ctl.pull(dest)
            self._on_log(r.detail)
            if not r.ok:
                self._on_result(False, r.detail)
                return

            self._on_log("[4/4] 分析（crash/ANR）…")
            rep = ctl.analyze(dest, cfg.pkg,
                              on_log=lambda m, t="info": self._on_log(m))
            if rep:
                self._on_result(True, rep.path_html or rep.path_md or "报告已生成")
            else:
                self._on_result(True, f"已拉取（分析未产出报告，请查看 {dest}）")
        except Exception as ex:  # noqa: BLE001
            self._on_result(False, f"异常：{ex}")
