"""``python -m cartest`` 入口"""
import sys

from .cli import main
from .core import logsetup

if __name__ == "__main__":
    logsetup.setup()
    sys.exit(main())
