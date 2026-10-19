# -*- coding: utf-8 -*-
"""管理工具全局常量配置

来源：``flet_Tool/constants.py`` 平移（2026-09-15，见 06-P3b设计.md §4.4）。
**唯一改动**：产物根目录由 ``flet_Tool/car_logs`` 改为平台数据目录 ``<DATA_DIR>/tools``。
"""
import os

from ...core import config as _platform

# ADB
ADB_TIMEOUT = 300
# 旧值冻结引用（P3c-2 起消费方改读 config_center；此常量仅供跨层对照测试，
# 见 tests/test_config_center.py，勿在业务代码中引用）
REMOTE_LOG_PATH = "/la_log/vstgloballogcontrol/"
REMOTE_SCREENSHOT_TMP = "/sdcard/screenshot_tmp.png"

# 本地保存（平台数据目录；原为 flet_Tool/car_logs）——**工具偏好 / 历史文件**用
LOCAL_SAVE_ROOT = os.path.join(str(_platform.DATA_DIR), "tools")
#: 注：**产物**目录（logcat / screenshots / recordings）2026-10-12 起统一走
#: 「本地输出根目录」``config_center.output_dir(...)``——留空＝工具所在目录下的 ``logs/``，
#: 不再用写死的 ``data/tools/...``。此处的 LOCAL_SAVE_ROOT 只服务配置与历史文件。
LOCAL_RUNTIME_LOG_DIR = os.path.join(LOCAL_SAVE_ROOT, "runtime_logs")
# 时间段提取默认输出根（与 core.logextract.default_output_base 同址）
LOCAL_LOGEXTRACT_ROOT = os.path.join(LOCAL_SAVE_ROOT, "logextract")

# 运行日志
RUNTIME_LOG_MAX_DAYS = 30  # 自动清理超过此天数的日志文件

# Logcat
LOGCAT_MAX_DURATION = 180
LOGCAT_BUFFER_SIZE = "16M"
LOGCAT_BUFFERS = [
    ("全部 (all)", "-b all"),
    ("主+系统+崩溃", "-b main -b system -b crash"),
    ("仅主缓冲区", "-b main"),
    ("主+事件+崩溃", "-b main -b events -b crash"),
    ("主+系统+事件+崩溃", "-b main -b system -b events -b crash"),
]

# APK 管理
APK_PKG_HISTORY_FILE = os.path.join(LOCAL_SAVE_ROOT, "apk_pkg_history.json")

# 触控映射默认分辨率
TOUCH_DEFAULT_WIDTH = 1920
TOUCH_DEFAULT_HEIGHT = 720

# 时间戳 Badge
TIMESTAMP_BADGE_WIDTH = 52

# UI 日志行数上限（防止内存膨胀）
UI_LOG_MAX_LINES = 3000
