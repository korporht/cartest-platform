# -*- coding: utf-8 -*-
"""P3c 验收 — Store 读取新鲜度（mtime 感知）

覆盖：
  1) 正常读写往返（含落盘校验）
  2) 外部修改文件 → get() 立即读到新值
  3) 文件被删除 → get() 回退默认值
  4) 写失败可见：目标不可写 → set 返回 False 且内存回滚为磁盘真相

用法:
    python tests/test_config_store_fresh.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _tmpdir import mkdtemp  # noqa: E402 — 临时目录退出即回收（tests/_tmpdir.py）

from cartest.core.config import Store  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    tmp = Path(mkdtemp(prefix="p3c_store_"))
    cfg = tmp / "settings.json"

    print("=" * 70)
    print("P3c 验收 — Store 读取新鲜度（mtime 感知）")
    print(f"临时配置: {cfg}")
    print("=" * 70)

    print("\n[1] 正常读写往返")
    st = Store(cfg, defaults={"a": 1})
    check("缺省值生效", st.get("a") == 1)
    ok = st.set("b", "hello")
    check("set 返回 True", ok is True)
    check("set 后可读", st.get("b") == "hello")
    check("已落盘且内容正确",
          cfg.exists() and json.loads(cfg.read_text(encoding="utf-8")).get("b") == "hello")

    print("\n[2] 外部修改 → get 立即可见")
    cfg.write_text(json.dumps({"a": 1, "b": "changed-externally"}), encoding="utf-8")
    future = time.time() + 5
    os.utime(cfg, (future, future))          # 消除同刻 mtime 竞态（确定性）
    check("get 读到外部新值", st.get("b") == "changed-externally", str(st.get("b")))

    print("\n[3] 文件删除 → 回退默认")
    cfg.unlink()
    check("b 回退（无默认 → None）", st.get("b") is None)
    check("a 回退默认 1", st.get("a") == 1)

    print("\n[4] 写失败可见（目标为目录 → 写入失败）")
    bad = tmp / "as_dir"
    bad.mkdir()
    st2 = Store(bad, defaults={"x": 0})
    ok2 = st2.set("x", 99)
    check("set 返回 False", ok2 is False)
    check("内存回滚为磁盘真相（默认 0）", st2.get("x") == 0)

    print()
    print("=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("Store 新鲜度测试通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
