"""CarTest core — 平台内核

约束（见 02-core接口设计.md）：
- 零 UI 依赖：不 import tkinter / customtkinter / flet
- 标准库优先
- 回调而非事件总线

模块依赖方向：injector → judger / logcat → adb；parser / source / progress 零内部依赖
"""
from . import adb, config, injector, judger, logcat, parser, progress, source

__all__ = ["adb", "config", "injector", "judger", "logcat", "parser", "progress", "source"]
