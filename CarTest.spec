# -*- mode: python ; coding: utf-8 -*-
"""CarTest 打包配置（2026-10-16）

PyInstaller onefile 打包，参考 CarVoiceTestTool.spec（已验证可行）：
  - 单文件 + 无控制台窗口
  - flet 0.85 全量收集（flet / flet-desktop / flet-web，含 web 客户端与桌面运行时资源）
  - data/ 用例作为只读资源打进包（config.py frozen 分支：RESOURCE_DIR → _MEIPASS）
  - 可写目录 = exe 所在目录（reports/logs/runs/data 状态）
构建命令: python -m PyInstaller CarTest.spec --noconfirm --clean
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

APP_DIR = Path(SPECPATH)

datas = []
binaries = []
hiddenimports = []

# flet 全家桶：flet-desktop（桌面客户端二进制）、flet-web（浏览器客户端静态资源）
# 注意用 import 名（下划线），dist 名（连字符）collect_all 匹配不到
for pkg in ("flet", "flet_desktop", "flet_web"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",
]

# data/ 用例目录打进包（_MEIPASS/data）
_data_dir = APP_DIR / "data"
if _data_dir.is_dir():
    datas.append((str(_data_dir), "data"))

_icon = APP_DIR / "app.ico"
icon = str(_icon) if _icon.exists() else None

excludes = [
    # 测试 / 开发工具
    "pytest", "playwright", "IPython", "notebook",
    # recorder 可选依赖组（screen_recorder 内部懒导入，缺失时仅功能降级）
    "cv2", "numpy", "matplotlib", "pandas", "scipy",
    "sounddevice", "soundfile", "mss", "imageio", "pygetwindow",
    # 其他 GUI 框架
    "PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter",
]

a = Analysis(
    [str(APP_DIR / "tools" / "exe_entry.py")],
    pathex=[str(APP_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CarTest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
