"""PyInstaller 打包入口（onefile）。

`python -m cartest.app` 的模块形式无法直接作为 PyInstaller 入口脚本
（相对导入需要包上下文），本 stub 以绝对导入转发到同一 main()。
"""
import sys

from cartest.app.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
