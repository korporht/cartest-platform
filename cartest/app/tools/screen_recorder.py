# -*- coding: utf-8 -*-
"""PC 端屏幕录制 —— 视频 + 麦克风音频，ffmpeg 合成，时间戳水印

来源：``GUI_Done.py`` 的 ``ScreenRecorder`` 类（tkinter 版；L425-938 原样平移）。
该类与 UI 完全无关（第三方库均为函数内延迟 import），可直接复用。

**P3b 移植的三处真实现**（用户裁决，见 06-P3b设计.md §4.6）：
  ① 窗口置顶：``_set_window_topmost`` / ``_restore_window_z_order`` 在旧版**从未被调用**，
     现由 ``start()`` / ``stop()`` 真正接入
  ② 音频开关：新增 ``record_audio`` 参数并生效（旧版复选框未被读取，音频恒录）
  ③ 显示器选择：``selected_monitor`` 参数生效（旧版忽略，恒 ``monitors[1]``）

依赖（运行时自检见 check_dependencies）：mss / opencv-python / numpy / sounddevice /
soundfile / imageio-ffmpeg（或系统 ffmpeg）
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

# ── 可选依赖（与旧版 GUI_Done.py 的模块级 try/except 对齐）──
# 说明：类内部分位置用函数内 import，但 cv2 / windll / win32gui / HAS_* 标记
# 依赖模块级名字——提取时若漏掉会静默失败（表现为"录制出空文件"）。
try:
    import cv2
    HAS_CV2 = True
except ImportError:  # noqa: BLE001
    cv2 = None
    HAS_CV2 = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # noqa: BLE001
    np = None
    HAS_NUMPY = False

try:
    import mss
    HAS_MSS = True
except ImportError:  # noqa: BLE001
    mss = None
    HAS_MSS = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:  # noqa: BLE001
    sd = None
    HAS_SOUNDDEVICE = False

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:  # noqa: BLE001
    sf = None
    HAS_SOUNDFILE = False

try:
    import pygetwindow as gw
    import pygetwindow
    HAS_PYGETWINDOW = True
except ImportError:  # noqa: BLE001
    gw = None
    pygetwindow = None
    HAS_PYGETWINDOW = False

try:
    import win32gui
    import win32con
    HAS_PYWIN32 = True
except ImportError:  # noqa: BLE001
    win32gui = None
    win32con = None
    HAS_PYWIN32 = False

try:
    from ctypes import windll
except ImportError:  # noqa: BLE001
    windll = None

#: 音频参数（原 Config.RECORDER_AUDIO_*）
RECORDER_AUDIO_SAMPLE_RATE = 44100
RECORDER_AUDIO_CHANNELS = 2


def _find_ffmpeg():
    """定位 ffmpeg：系统 PATH 优先，退到 imageio-ffmpeg 内置二进制"""
    from shutil import which
    exe = which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def check_dependencies():
    """返回缺失依赖名列表（空 = 齐全）。

    旧版缺库时异常被宽 except 吞掉、只产出空文件（用户无感知）；这里显式暴露。
    """
    missing = []
    for mod, pip_name in (("mss", "mss"), ("cv2", "opencv-python"), ("numpy", "numpy"),
                          ("sounddevice", "sounddevice"), ("soundfile", "soundfile")):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(pip_name)
    if not _find_ffmpeg():
        missing.append("ffmpeg（系统 PATH 或 imageio-ffmpeg）")
    return missing


class ScreenRecorder:
    """屏幕录制：视频+麦克风音频，自动合成，支持时间戳水印，支持窗口录制"""

    def __init__(self, fps: int = 15, show_timestamp: bool = True):
        self.fps = fps
        self._running = False
        self._audio_data = []
        self._video_writer = None
        self._audio_stream = None
        self._temp_video = None
        self._temp_audio = None
        self._save_path = None
        self._start_time = 0
        self._frame_interval = 1.0 / fps
        self._show_timestamp = show_timestamp  # 是否显示时间戳水印
        self._capture_mode = "fullscreen"  # 录制模式: fullscreen / window
        self._target_window = None  # 目标窗口对象（窗口模式时使用）
        self._window_rect = None  # 窗口区域 (left, top, width, height)
        self._rect_cache_time = 0  # 窗口rect缓存时间戳
        self._window_hwnd = None  # 窗口句柄（用于置顶）
        self._window_was_visible = True  # 窗口置顶前的可见状态
        self._monitor_index = 1  # P3b：生效的显示器索引（monitors[] 下标）
        self._record_audio_enabled = True  # P3b：音频开关是否生效

    def _get_audio_devices(self):
        """获取可用音频输入设备"""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            mic_devices = []
            for i, dev in enumerate(devices):
                if dev['max_input_channels'] > 0:
                    mic_devices.append((i, dev['name']))
            return mic_devices
        except ImportError:
            return []
        except Exception:
            return []

    def _audio_callback(self, indata, frames, time_info, status):
        """音频回调：录制麦克风输入"""
        if status:
            print(f"Audio status: {status}")
        if self._running:
            self._audio_data.append(indata.copy())

    def _record_audio(self):
        """音频录制线程"""
        try:
            import sounddevice as sd
            devices = self._get_audio_devices()

            if not devices:
                print("No audio input devices found, skipping audio recording")
                return

            device_id = devices[0][0]  # 默认使用第一个麦克风
            print(f"Using audio device: {devices[0][1]} (id={device_id})")

            with sd.InputStream(
                samplerate=RECORDER_AUDIO_SAMPLE_RATE,
                channels=RECORDER_AUDIO_CHANNELS,
                dtype='int16',
                device=device_id,
                callback=self._audio_callback
            ):
                print("Audio stream started, recording...")
                while self._running:
                    sd.sleep(100)
                print(f"Audio stream stopped, recorded {len(self._audio_data)} chunks")
        except Exception as e:
            print(f"Audio recording error: {e}")
            import traceback
            traceback.print_exc()

    def _get_window_rect(self):
        """获取目标窗口的当前位置和大小"""
        if self._capture_mode == "window" and self._target_window:
            try:
                # 使用 pygetwindow 获取窗口信息
                if HAS_PYGETWINDOW:
                    win = gw.getWindowsWithTitle(self._target_window)
                    if win:
                        w = win[0]
                        return {
                            "left": w.left,
                            "top": w.top,
                            "width": w.width,
                            "height": w.height
                        }
                # 备用方案：使用 pywin32
                elif HAS_PYWIN32:
                    import win32gui
                    import win32con
                    def callback(hwnd, extra):
                        if win32gui.IsWindowVisible(hwnd):
                            title = win32gui.GetWindowText(hwnd)
                            if self._target_window in title:
                                rect = win32gui.GetWindowRect(hwnd)
                                extra.append({
                                    "left": rect[0],
                                    "top": rect[1],
                                    "width": rect[2] - rect[0],
                                    "height": rect[3] - rect[1]
                                })
                    windows = []
                    win32gui.EnumWindows(callback, windows)
                    if windows:
                        return windows[0]
            except Exception as e:
                print(f"Get window rect error: {e}")
        return None

    def _set_window_topmost(self):
        """将目标窗口置顶（录制期间遮挡其他窗口）"""
        if not self._target_window:
            print("[置顶] target_window 为空，跳过")
            return
        try:
            hwnd = None
            # 方式1：pygetwindow
            if HAS_PYGETWINDOW:
                wins = gw.getWindowsWithTitle(self._target_window)
                if wins:
                    hwnd = wins[0]._hWnd
                    self._window_was_visible = wins[0].visible
                    print(f"[置顶] pygetwindow 找到窗口 hwnd={hwnd}, title='{wins[0].title}'")
            # 方式2：pywin32
            if hwnd is None and HAS_PYWIN32:
                import win32gui
                def callback(h, extra):
                    if win32gui.IsWindowVisible(h):
                        title = win32gui.GetWindowText(h)
                        if self._target_window in title:
                            extra.append(h)
                found = []
                win32gui.EnumWindows(callback, found)
                if found:
                    hwnd = found[0]
                    self._window_was_visible = win32gui.IsWindowVisible(hwnd)
                    print(f"[置顶] win32gui 找到窗口 hwnd={hwnd}")

            if hwnd is None:
                print(f"[置顶] 未能找到窗口 title='{self._target_window}'，跳过置顶")
                return

            self._window_hwnd = hwnd
            user32 = windll.user32

            # 尝试将窗口激活（前置到前台）
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)

            # SetWindowPos 返回值检查：非零=成功
            result = user32.SetWindowPos(
                hwnd, HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            )
            if result:
                print(f"[置顶] 成功 → hwnd={hwnd}")
            else:
                err = windll.kernel32.GetLastError()
                print(f"[置顶] SetWindowPos 返回 0，GetLastError={err}")
        except Exception as e:
            print(f"[置顶] 异常: {e}")
            import traceback
            traceback.print_exc()

    def _restore_window_z_order(self):
        """恢复窗口原来的 Z 轴顺序"""
        if not self._window_hwnd:
            return
        try:
            user32 = windll.user32
            result = user32.SetWindowPos(
                self._window_hwnd, HWND_NOTOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE
            )
            if result:
                print(f"[置顶恢复] 成功 hwnd={self._window_hwnd}")
            else:
                print(f"[置顶恢复] 失败 hwnd={self._window_hwnd}")
        except Exception as e:
            print(f"[置顶恢复] 异常: {e}")
        finally:
            self._window_hwnd = None

    def _record_screen(self):
        """屏幕录制线程"""
        try:
            import mss
            import numpy as np

            # 时间戳水印常量（缓存，避免每帧重复计算）
            _font = cv2.FONT_HERSHEY_SIMPLEX
            _thickness = 2
            _font_scale_window = 1.0
            _font_scale_full = 1.2

            with mss.mss() as sct:
                while self._running:
                    frame_start = time.time()

                    # 确定捕获区域（窗口模式下每秒刷新一次rect，避免每帧EnumWindows）
                    if self._capture_mode == "window" and self._target_window:
                        now = time.time()
                        if now - self._rect_cache_time >= 1.0:
                            self._window_rect = self._get_window_rect()
                            self._rect_cache_time = now
                        rect = self._window_rect
                        if rect:
                            monitor = rect
                        else:
                            monitor = sct.monitors[self._monitor_index]
                    else:
                        monitor = sct.monitors[self._monitor_index]

                    if monitor.get("width", 0) <= 0 or monitor.get("height", 0) <= 0:
                        monitor = sct.monitors[self._monitor_index]

                    try:
                        img = sct.grab(monitor)
                    except Exception as e:
                        print(f"Grab error: {e}, using fullscreen")
                        monitor = sct.monitors[self._monitor_index]
                        img = sct.grab(monitor)

                    frame = np.array(img)[:, :, :3]  # RGBA -> RGB (view, no copy yet)

                    # 添加时间戳水印（每秒更新一次字符串）
                    if self._show_timestamp:
                        cur_sec = int(frame_start)
                        if not hasattr(self, '_ts_cache_sec') or self._ts_cache_sec != cur_sec:
                            self._ts_cache_sec = cur_sec
                            self._ts_cache_str = time.strftime("%m%d  %H:%M:%S")
                        time_str = self._ts_cache_str

                        font_scale = _font_scale_window if self._capture_mode == "window" else _font_scale_full
                        text_size = cv2.getTextSize(time_str, _font, font_scale, _thickness)[0]
                        x, y = 20, text_size[1] + 25

                        # 需要连续内存才能 putText，此时做唯一一次拷贝
                        if not frame.flags['C_CONTIGUOUS']:
                            frame = np.ascontiguousarray(frame)
                        cv2.putText(frame, time_str, (x, y), _font, font_scale,
                                   (0, 0, 0), _thickness + 1, cv2.LINE_AA)
                        cv2.putText(frame, time_str, (x, y), _font, font_scale,
                                   (255, 255, 255), _thickness, cv2.LINE_AA)
                    else:
                        if not frame.flags['C_CONTIGUOUS']:
                            frame = np.ascontiguousarray(frame)

                    # 初始化视频写入器
                    if self._video_writer is None:
                        h, w = frame.shape[:2]
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        self._video_writer = cv2.VideoWriter(
                            self._temp_video, fourcc, self.fps, (w, h)
                        )

                    self._video_writer.write(frame)

                    # 控制帧率
                    elapsed = time.time() - frame_start
                    sleep_time = self._frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
        except Exception as e:
            print(f"Screen recording error: {e}")

    def start(self, save_path: str, selected_monitor: int = 1, show_timestamp: bool = True,
              capture_mode: str = "fullscreen", target_window: str = None,
              record_audio: bool = True) -> bool:
        """开始录制

        Args:
            save_path: 保存路径
            selected_monitor: 显示器索引（**P3b：真正生效**，旧版该参数被忽略）
            show_timestamp: 是否显示时间戳
            capture_mode: 录制模式 - "fullscreen" 全屏, "window" 窗口
            target_window: 目标窗口标题（窗口模式时使用）
            record_audio: 是否录制麦克风（**P3b：真正生效**，旧版音频恒录）
        """
        if self._running:
            return False

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        self._save_path = save_path
        self._temp_video = save_path + ".tmp.mp4"
        self._temp_audio = save_path + ".tmp.wav"
        self._audio_data = []
        self._video_writer = None
        self._running = True
        self._start_time = time.time()
        self._show_timestamp = show_timestamp
        self._capture_mode = capture_mode
        self._target_window = target_window
        self._monitor_index = max(1, int(selected_monitor or 1))  # ① 显示器选择生效
        self._record_audio_enabled = bool(record_audio)           # ② 音频开关生效

        # ③ 窗口模式：把目标窗口置顶（旧版写了这函数却从未调用）
        if self._capture_mode == "window" and self._target_window:
            try:
                self._set_window_topmost()
            except Exception:  # noqa: BLE001
                pass

        # 启动录制线程
        self._screen_thread = threading.Thread(target=self._record_screen, daemon=True)
        self._screen_thread.start()
        if self._record_audio_enabled:                            # ② 未启用则不采集音频
            self._audio_thread = threading.Thread(target=self._record_audio, daemon=True)
            self._audio_thread.start()
        return True

    def stop(self) -> str:
        """停止录制并合成音视频"""
        if not self._running:
            return ""

        self._running = False
        if hasattr(self, '_screen_thread'):
            self._screen_thread.join(timeout=2)
        if hasattr(self, '_audio_thread') and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=2)

        # ③ 恢复窗口层级（与 start 的置顶配对）
        if self._capture_mode == "window":
            try:
                self._restore_window_z_order()
            except Exception:  # noqa: BLE001
                pass

        # 保存视频
        if self._video_writer:
            self._video_writer.release()
            self._video_writer = None

        # 保存音频
        audio_valid = False
        if self._audio_data:
            try:
                import sounddevice as sd
                import soundfile as sf
                import numpy as np

                print(f"Processing {len(self._audio_data)} audio chunks...")
                audio = np.concatenate(self._audio_data, axis=0)
                print(f"Audio shape: {audio.shape}, dtype: {audio.dtype}")
                sf.write(self._temp_audio, audio, RECORDER_AUDIO_SAMPLE_RATE)

                # 检查音频文件是否有效
                if os.path.exists(self._temp_audio) and os.path.getsize(self._temp_audio) > 0:
                    audio_valid = True
                    print(f"Audio saved: {os.path.getsize(self._temp_audio)} bytes")
                else:
                    print("Audio file is empty or invalid")

                # 合成音视频
                self._merge_audio_video()
            except Exception as e:
                print(f"Audio save error: {e}")
                import traceback
                traceback.print_exc()
                # 没有音频时直接重命名视频
                if os.path.exists(self._temp_video):
                    os.rename(self._temp_video, self._save_path)
        else:
            print("No audio data recorded")
            # 没有音频数据，直接使用视频
            if os.path.exists(self._temp_video):
                os.rename(self._temp_video, self._save_path)

        # 如果最终文件不存在，说明合并失败
        if not os.path.exists(self._save_path):
            print("Merge failed, checking if video exists...")
            if os.path.exists(self._temp_video):
                os.rename(self._temp_video, self._save_path)

        # 清理剩余的临时文件
        for f in [self._temp_video, self._temp_audio]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

        return self._save_path

    def _merge_audio_video(self):
        """使用 ffmpeg 合成音视频（优先用系统 ffmpeg，再尝试 imageio-ffmpeg）"""
        if not os.path.exists(self._temp_video):
            print("Temp video not found")
            return

        # 检查音频文件是否存在且有效
        audio_valid = os.path.exists(self._temp_audio) and os.path.getsize(self._temp_audio) > 0

        if not audio_valid:
            print("Audio file not valid, using video only")
            if os.path.exists(self._temp_video):
                os.rename(self._temp_video, self._save_path)
            self._cleanup_temp_files()
            return

        merge_success = False
        ffmpeg_path = None

        # 方法1：优先使用系统 ffmpeg
        try:
            result = subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                ffmpeg_path = 'ffmpeg'
                print("Using system ffmpeg")
        except FileNotFoundError:
            pass

        # 方法2：使用 imageio-ffmpeg
        if not ffmpeg_path:
            try:
                import imageio_ffmpeg
                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                print(f"Using imageio-ffmpeg: {ffmpeg_path}")
            except ImportError:
                print("imageio-ffmpeg not installed")

        if not ffmpeg_path:
            print("No ffmpeg found, using video only")
            if os.path.exists(self._temp_video):
                os.rename(self._temp_video, self._save_path)
            self._cleanup_temp_files()
            return

        # 执行 ffmpeg 合并
        try:
            print(f"Merging with ffmpeg: video={self._temp_video}, audio={self._temp_audio}")

            ffmpeg_cmd = [
                ffmpeg_path,
                '-y',  # 覆盖输出文件
                '-i', self._temp_video,
                '-i', self._temp_audio,
                '-map', '0:v',
                '-map', '1:a',
                '-c:v', 'copy',  # 直接复制视频流，不重新编码
                '-c:a', 'aac',   # 音频转 aac
                '-shortest',     # 以短的那个为准
                '-async', '1',   # 音视频同步
                self._save_path
            ]

            result = subprocess.run(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            if result.returncode == 0:
                print("ffmpeg merge successful")
                merge_success = True
                # 删除原视频文件
                if os.path.exists(self._temp_video):
                    os.remove(self._temp_video)
            else:
                err_msg = result.stderr.decode('utf-8', errors='ignore')
                print(f"ffmpeg merge failed (code {result.returncode}): {err_msg[:500]}")
        except Exception as e:
            print(f"Merge error: {e}")
            import traceback
            traceback.print_exc()

        # 如果合并失败，使用纯视频
        if not merge_success:
            print("Using video without audio")
            if os.path.exists(self._temp_video):
                os.rename(self._temp_video, self._save_path)

        # 清理所有临时文件
        self._cleanup_temp_files()

    def _cleanup_temp_files(self):
        """清理录屏过程中产生的所有临时文件"""
        temp_files = [
            self._temp_video,
            self._temp_audio,
            self._temp_audio + ".m4a" if self._temp_audio else None,
        ]
        for f in temp_files:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                    print(f"Cleaned up temp file: {f}")
                except Exception as e:
                    print(f"Failed to cleanup {f}: {e}")

    def is_running(self) -> bool:
        return self._running

    def get_elapsed_time(self) -> float:
        if self._running:
            return time.time() - self._start_time
        return 0

    @staticmethod
    def get_available_windows() -> list:
        """获取所有可见窗口列表"""
        windows = []
        try:
            if HAS_PYGETWINDOW:
                all_windows = gw.getAllWindows()
                for w in all_windows:
                    if w.visible and w.width > 100 and w.height > 100 and w.title.strip():
                        windows.append(w.title)
            elif HAS_PYWIN32:
                import win32gui
                import win32con
                def callback(hwnd, extra):
                    if win32gui.IsWindowVisible(hwnd):
                        title = win32gui.GetWindowText(hwnd)
                        if title.strip():
                            # 获取窗口大小
                            rect = win32gui.GetWindowRect(hwnd)
                            width = rect[2] - rect[0]
                            height = rect[3] - rect[1]
                            if width > 100 and height > 100:
                                extra.append(title)
                win32gui.EnumWindows(callback, windows)
        except Exception as e:
            print(f"Get windows error: {e}")
        return sorted(list(set(windows)))  # 去重并排序


# ====================== Tooltip 组件 ======================
