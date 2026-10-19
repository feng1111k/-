# -*- coding: utf-8 -*-
"""
屏幕人物监视截图工具  (OpenCV / 控制台版)

完整流程:
  1. 启动后先选择保存文件夹
  2. 选择工作模式: 截图 或 录屏
  3. 在屏幕上拖拽框选【截图区域】/【录制区域】 -> 有人出现时要保存下来的那块区域
  4. 在屏幕上拖拽框选【监控区域】 -> 用来检测有没有人
  5. 后台持续检测监控区域:
       截图模式: 出现人 -> 抓取截图区域, 存一张 PNG (冷却 COOLDOWN 秒)
       录屏模式: 出现人 -> 开始录制录制区域; 连续 RECORD_STOP_DELAY 秒没人 -> 停止并保存 MP4
  6. 预览窗实时显示监控区域, 按 Q 或 Esc 退出

关键点:
  - 两个区域分开选择, 检测用监控区域, 保存/录制用截图(录制)区域
  - 全屏覆盖层用 tkinter 实现, 坐标 = 物理像素, 与 mss 抓屏坐标 1:1 对应
  - 录屏在独立线程里按固定帧率抓屏, 不受 YOLO 推理速度影响

命令行:
  python main_opencv.py                        正常使用
  python main_opencv.py --probe-dpi            只打印 DPI/屏幕信息, 不启动界面
  python main_opencv.py --test-detect <图片>    对一张图片跑一次检测, 打印原始结果
  python main_opencv.py --rectest <目录> [秒]   只测录屏链路(全屏录制几秒并读回校验)
  python main_opencv.py --selftest <保存目录>   跳过框选, 直接监视全屏 30 秒
  python main_opencv.py --mode shot|record     跳过模式选择(配合自检或自动化用)
"""

import os
import sys
import time
import threading
import traceback


# ---------------------------------------------------------------- 无控制台输出
# 打包成 exe 后没有 cmd 窗口, stdout/stderr 会是 None, 所有 print/log 都会丢。
# 这里把输出重定向到临时目录的日志文件, 方便出问题时排查。
# 注意: 日志只写 %TEMP%\ScreenPersonMonitor\, 不会污染保存文件夹。
_FROZEN_LOG = None


def _temp_log_dir():
    d = os.path.join(os.environ.get('TEMP', '.'), 'ScreenPersonMonitor')
    os.makedirs(d, exist_ok=True)
    return d


def _setup_frozen_io():
    global _FROZEN_LOG
    if not getattr(sys, 'frozen', False):
        return
    try:
        path = os.path.join(_temp_log_dir(), 'ScreenPersonMonitor.log')
        # 超过 2MB 就重开, 避免日志无限增长
        if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.remove(path)
        fp = open(path, 'a', encoding='utf-8', buffering=1)
        fp.write("\n===== %s =====\n" % time.strftime('%Y-%m-%d %H:%M:%S'))
        _FROZEN_LOG = fp
        if sys.stdout is None:
            sys.stdout = fp
        if sys.stderr is None:
            sys.stderr = fp
    except Exception:
        _FROZEN_LOG = None


_setup_frozen_io()


# ---------------------------------------------------------------- DPI 感知
# 必须在 import cv2 / mss / tkinter 之前完成, 否则 125% 缩放下进程看到的屏幕
# 会是 1536x864 而不是 1920x1080, 框选坐标就会整体错位(差了 1.25 倍)。
def _set_dpi_awareness():
    """返回 (是否成功, 说明)"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore
    except Exception as exc:
        return False, "加载 user32/shcore 失败: %r" % (exc,)

    # 已经是 DPI 感知就不用再折腾
    try:
        if hasattr(user32, 'GetThreadDpiAwarenessContext'):
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            ctx = user32.GetThreadDpiAwarenessContext()
            if ctx:
                shcore.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
                shcore.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                aw = shcore.GetAwarenessFromDpiAwarenessContext(ctx)
                if aw and aw > 0:
                    return True, "启动时已是 DPI 感知 (awareness=%d)" % aw
    except Exception:
        pass

    # 首选: 每显示器 DPI 感知 v2 (Win10 1703+)
    # 注意顺序: 先设 DPI_AWARENESS_CONTEXT, 最后才退回老 API。
    # 因为一旦调用过 SetProcessDPIAware(), shcore.SetProcessDpiAwareness 就会失败。
    try:
        if hasattr(user32, 'SetProcessDpiAwarenessContext'):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            for value, name in ((-4, "PER_MONITOR_AWARE_V2"), (-3, "PER_MONITOR_AWARE")):
                if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(value)):
                    return True, "已设置 %s" % name
    except Exception:
        pass

    # 次选: shcore.SetProcessDpiAwareness(2)
    try:
        if shcore.SetProcessDpiAwareness(2) == 0:
            return True, "已设置 shcore PER_MONITOR_AWARE"
    except Exception:
        pass

    # 兜底: 老 API
    try:
        if user32.SetProcessDPIAware():
            return True, "已设置 user32 SetProcessDPIAware (system aware)"
    except Exception:
        pass

    return False, "所有 DPI 感知设置都失败了"


_DPI_OK, _DPI_MSG = _set_dpi_awareness()

import ctypes
import tkinter as tk

# 再补一次老 API, 保证 mss 的坐标等于真实物理像素
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# noconsole 模式下 stdout/stderr 可能为 None
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 避免 ultralytics 联网做版本检查/遥测 (打包后无控制台时尤其重要)
os.environ.setdefault('YOLO_OFFLINE', '1')
os.environ.setdefault('YOLO_VERBOSE', 'false')

import cv2
import numpy as np
from mss import mss

# ---------------- 可调参数 ----------------
CONF_THRESHOLD = 0.45   # person 置信度阈值
COOLDOWN = 5.0          # 两次截图最小间隔(秒)
MIN_SIDE = 20           # 区域最小边长

RECORD_FPS = 20         # 录屏帧率
RECORD_STOP_DELAY = 2.0 # 录屏模式: 连续这么多秒没检测到人就停止录制

MODE_SHOT = 'shot'      # 截图模式
MODE_RECORD = 'record'  # 录屏模式

# 预览窗状态条最大高度 (像素)。单行紧凑设计, 尽量少遮挡监控画面。
BAR_H = 34


# ------------------------------------------------- 中文字体与文本绘制
# OpenCV 自带的 Hershey 字体画不了中文(会变成问号), 所以用系统的
# 微软雅黑等字体文件 + PIL 渲染中文。PIL 不可用时自动退回英文。
_FONT_CACHE = {}
_PIL_OK = None


def _find_cn_font(bold=False):
    """找一个可用的中文字体文件"""
    import glob
    names = ([r'C:\Windows\Fonts\msyhbd.ttc', r'C:\Windows\Fonts\msyhbd.ttf',
              r'C:\Windows\Fonts\simhei.ttf'] if bold else
             [r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyh.ttf',
              r'C:\Windows\Fonts\simhei.ttf', r'C:\Windows\Fonts\simsun.ttc'])
    for p in names:
        if os.path.exists(p):
            return p
    for pat in ('msyh*.tt*', 'simhei.ttf', 'Deng.ttf', 'simsun.ttc'):
        hits = glob.glob(os.path.join(r'C:\Windows\Fonts', pat))
        if hits:
            return hits[0]
    return None


def _get_font(size, bold=False):
    key = (int(size), bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    try:
        from PIL import ImageFont
        path = _find_cn_font(bold)
        if path:
            font = ImageFont.truetype(path, int(size))
    except Exception:
        font = None
    _FONT_CACHE[key] = font
    return font


def _pil_available():
    global _PIL_OK
    if _PIL_OK is None:
        try:
            from PIL import Image, ImageDraw      # noqa: F401
            _PIL_OK = _get_font(20) is not None
        except Exception:
            _PIL_OK = False
    return _PIL_OK


def draw_text(img, text, org, size=28, color=(0, 255, 0), bold=False, shadow=True):
    """
    在 OpenCV 图像上写字。有中文字体时用 PIL(支持中文), 否则退回 cv2.putText(英文)。
    org 是文字左上角坐标。
    """
    if not text:
        return
    if not _pil_available():
        cv2.putText(img, text, (org[0], org[1] + size), cv2.FONT_HERSHEY_SIMPLEX,
                    size / 32.0, color, max(1, int(size / 14)))
        return
    try:
        from PIL import Image, ImageDraw
        font = _get_font(size, bold)
        # 先量出文字大小(含描边余量)
        box = font.getbbox(text)
        tw = box[2] - box[0]
        th = box[3] - box[1]
        pad = 4
        w, h = tw + pad * 2, th + pad * 2
        if w <= 0 or h <= 0:
            return
        tile = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        # 黑色描边让文字在任何背景上都清晰
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, -1), (-1, 1), (1, 1)):
            d.text((pad - box[0] + dx, pad - box[1] + dy), text, font=font,
                   fill=(0, 0, 0, 255))
        d.text((pad - box[0], pad - box[1]), text, font=font, fill=tuple(color) + (255,))
        tile = np.array(tile)
        rgb, alpha = tile[:, :, :3], tile[:, :, 3:4].astype(np.float32) / 255.0
        x, y = int(org[0]), int(org[1])
        H, W = img.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        sx0, sy0 = x0 - x, y0 - y
        sub = img[y0:y1, x0:x1].astype(np.float32)
        patch_rgb = rgb[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)].astype(np.float32)
        a = alpha[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
        img[y0:y1, x0:x1] = (sub * (1.0 - a) + patch_rgb * a).astype(np.uint8)
    except Exception:
        # 任何意外都退回英文, 保证不影响主流程
        cv2.putText(img, text, (org[0], org[1] + size), cv2.FONT_HERSHEY_SIMPLEX,
                    size / 32.0, color, max(1, int(size / 14)))



# ------------------------------------------------- 锁屏 / 安全桌面检测
# 按 Win+L 锁屏后, Windows 会切到 Winlogon 安全桌面, 此时 BitBlt 会返回
# "拒绝访问", 抓不到任何画面。这是系统的安全设计, 普通程序无法绕过。
# 这里做两件事: 1) 识别出"被锁屏"而不是"抓屏坏了"  2) 锁屏期间优雅等待
def is_desktop_locked():
    """
    尝试打开当前输入桌面。正常解锁时能打开 Default 桌面;
    锁屏时会失败(输入桌面已切到 Winlogon, 普通权限打不开)。
    返回 (是否锁定, 说明)
    """
    try:
        user32 = ctypes.windll.user32
        h = user32.OpenInputDesktop(0, False, 0x0001)   # DESKTOP_READOBJECTS
        if not h:
            return True, "无法打开输入桌面(已锁屏或切到安全桌面)"
        try:
            need = ctypes.c_ulong(0)
            # UOI_NAME = 2
            if user32.GetUserObjectInformationW(h, 2, None, 0, ctypes.byref(need)) or need.value:
                buf = ctypes.create_unicode_buffer(need.value // 2 + 2)
                if user32.GetUserObjectInformationW(h, 2, buf, need.value, ctypes.byref(need)):
                    name = buf.value
                    if name.lower() != 'default':
                        return True, "当前输入桌面=%s" % name
                    return False, "输入桌面=Default"
            return False, "输入桌面可打开"
        finally:
            user32.CloseDesktop(h)
    except Exception as exc:
        return False, "检测异常: %r" % (exc,)



# ---------------------------------------------------------------- 路径工具
def resource_path(rel):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


MODEL_PATH = resource_path('yolov8n.pt')


# ---------------------------------------------------------------- 日志
class Log:
    """
    输出到控制台; 打包成 exe(无控制台)时 _setup_frozen_io() 已把 stdout
    重定向到 %TEMP%\\ScreenPersonMonitor\\ScreenPersonMonitor.log。
    不会在保存目录里留任何日志文件。
    """

    def __call__(self, msg):
        try:
            print("[%s] %s" % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
        except Exception:
            pass


log = Log()


# ---------------------------------------------------------------- 崩溃处理
def _report_crash(exc_type, exc, tb, source='主线程'):
    """打包后没有控制台: 未捕获异常时写日志并弹窗, 不让程序默默消失。"""
    try:
        text = ''.join(traceback.format_exception(exc_type, exc, tb))
    except Exception:
        text = repr(exc)
    path = ''
    try:
        path = os.path.join(_temp_log_dir(),
                            time.strftime('crash_%Y%m%d_%H%M%S.log'))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception:
        path = ''
    try:
        log("!! %s 未捕获异常: %r" % (source, exc))
        log(text)
    except Exception:
        pass
    try:
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showerror(
            "ScreenPersonMonitor 遇到错误",
            "程序遇到未处理的错误, 已停止运行。\n\n%r\n\n详细信息:\n%s"
            % (exc, path or '(临时目录不可写, 请从源码运行查看错误)'))
        root.destroy()
    except Exception:
        pass


def _install_crash_handler():
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        _report_crash(exc_type, exc, tb)

    sys.excepthook = hook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        _report_crash(args.exc_type, args.exc_value, args.exc_traceback, '后台线程')

    threading.excepthook = thread_hook

# 屏幕尺寸只取一次, 避免反复创建/销毁 tkinter 根窗口
_SCREEN = {}


def tk_screen_size():
    """返回 tkinter 看到的屏幕尺寸(物理像素)"""
    if 'size' not in _SCREEN:
        try:
            root = tk.Tk()
            root.withdraw()
            _SCREEN['size'] = (root.winfo_screenwidth(), root.winfo_screenheight())
            _SCREEN['scaling'] = str(root.tk.call('tk', 'scaling'))
            root.destroy()
        except Exception as exc:
            _SCREEN['size'] = "err %r" % (exc,)
            _SCREEN['scaling'] = "?"
    return _SCREEN['size']


def mss_screen_size():
    try:
        with mss() as sct:
            m = sct.monitors[0]
            return (m['width'], m['height'])
    except Exception as exc:
        return "err %r" % (exc,)


def screen_info():
    """收集屏幕相关的诊断信息, 用于判断坐标是否会错位"""
    return {
        "dpi_ok": _DPI_OK,
        "dpi_msg": _DPI_MSG,
        "tk_screen": tk_screen_size(),
        "tk_scaling": _SCREEN.get('scaling', '?'),
        "mss_screen": mss_screen_size(),
    }


# ---------------------------------------------------------------- 录屏
class ScreenRecorder:
    """
    在独立线程里按固定帧率录制指定屏幕区域。

    - 检测线程/主线程只调用 request_start()/request_stop() 改标志,
      真正打开和关闭 VideoWriter 都在录制线程里做, 避免跨线程操作 cv2 对象。
    - 录屏线程和检测线程各用一个 mss 实例 (mss 对象不是线程安全的)。
    - 编码优先 mp4v + .mp4, 失败自动退 MJPG/XVID + .avi。
    """

    CODECS = (('mp4v', '.mp4'), ('MJPG', '.avi'), ('XVID', '.avi'))

    def __init__(self, box, out_dir, fps=RECORD_FPS):
        # 宽高取偶数, 部分编码器遇到奇数尺寸会失败或写出坏文件
        self.box = {
            'left': int(box['left']),
            'top': int(box['top']),
            'width': max(2, int(box['width']) // 2 * 2),
            'height': max(2, int(box['height']) // 2 * 2),
        }
        self.out_dir = out_dir
        self.fps = max(1, int(round(fps)))

        self._lock = threading.Lock()
        self._want = False       # 检测线程要求录制
        self._active = False     # VideoWriter 已打开
        self._running = True
        self._writer = None
        self._path = ''
        self._codec = ''
        self._frames = 0
        self._started_at = 0.0
        self._segments = 0
        self._last_path = ''
        self._last_seconds = 0.0
        self._last_frames = 0
        self._error = ''
        self._last_error_msg = ''
        self._last_error_log = 0.0

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---------------- 检测线程 / 主线程调用 ----------------
    def request_start(self):
        with self._lock:
            self._want = True

    def request_stop(self):
        with self._lock:
            self._want = False

    def is_recording(self):
        """True = 正在录制, 或已请求开始但文件还没打开"""
        with self._lock:
            return self._want or self._active

    def snapshot(self):
        """给界面用的状态快照 (加锁, 不会和录制线程抢对象)"""
        with self._lock:
            return {
                'active': self._active,
                'path': self._path,
                'codec': self._codec,
                'frame': self._frames,
                'seconds': (time.time() - self._started_at) if self._active else 0.0,
                'segments': self._segments,
                'last_path': self._last_path,
                'last_seconds': self._last_seconds,
                'last_frames': self._last_frames,
                'error': self._error,
            }

    def close(self):
        """退出程序前调用: 停止录制, 把最后一段写完"""
        with self._lock:
            self._running = False
            self._want = False
        self._thread.join(timeout=5.0)
        self._close_writer()    # 兜底: 线程没来得及关时由主线程关

    # ---------------- 录制线程 ----------------
    def _loop(self):
        try:
            with mss() as sct:
                next_t = time.time()
                while self._running:
                    with self._lock:
                        want = self._want
                    if not self._running:
                        break
                    if not want:
                        self._close_writer()
                        time.sleep(0.03)
                        next_t = time.time()
                        continue

                    if not self._active:
                        try:
                            raw = sct.grab(self.box)
                        except Exception as exc:
                            self._fail("抓取录制区域失败: %r" % (exc,))
                            time.sleep(0.2)
                            continue
                        frame = np.ascontiguousarray(np.array(raw)[:, :, :3])
                        if not self._open_writer(frame.shape[1], frame.shape[0]):
                            time.sleep(0.5)
                            continue
                        self._write(frame)
                        next_t = time.time() + 1.0 / self.fps
                        continue

                    now = time.time()
                    if next_t > now:
                        time.sleep(min(next_t - now, 0.05))
                        continue
                    try:
                        raw = sct.grab(self.box)
                    except Exception as exc:
                        self._fail("抓取录制区域失败: %r" % (exc,))
                        next_t = time.time()
                        time.sleep(0.1)
                        continue
                    frame = np.ascontiguousarray(np.array(raw)[:, :, :3])
                    self._write(frame)
                    next_t += 1.0 / self.fps
                    # 编码跟不上时不要疯狂补帧, 直接对齐时间轴 (相当于丢帧)
                    if next_t < time.time() - 1.0:
                        next_t = time.time()
        except Exception as exc:
            self._fail("录屏线程崩溃: %r" % (exc,))
            log(traceback.format_exc())
        finally:
            self._close_writer()
            with self._lock:
                self._running = False

    # ---------------- 内部 ----------------
    def _new_path(self, ext):
        base = time.strftime("record_%Y%m%d_%H%M%S")
        path = os.path.join(self.out_dir, base + ext)
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.out_dir, "%s_%d%s" % (base, n, ext))
        return path

    def _open_writer(self, w, h):
        for codec, ext in self.CODECS:
            path = self._new_path(ext)
            writer = None
            try:
                writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*codec),
                                         self.fps, (w, h))
                if writer.isOpened():
                    with self._lock:
                        self._writer = writer
                        self._path = path
                        self._codec = codec
                        self._frames = 0
                        self._started_at = time.time()
                        self._active = True
                        self._error = ''
                    log("开始录屏: %s (%dx%d @ %d fps, %s)"
                        % (path, w, h, self.fps, codec))
                    return True
            except Exception as exc:
                log("!! 打开编码器 %s 失败: %r" % (codec, exc))
            if writer is not None:
                writer.release()
        self._fail("没有可用的视频编码器 (mp4v/MJPG/XVID 都失败)")
        return False

    def _write(self, frame):
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(frame)
            with self._lock:
                self._frames += 1
        except Exception as exc:
            self._fail("写入视频失败: %r" % (exc,))

    def _close_writer(self):
        with self._lock:
            writer = self._writer
            if not self._active:
                return
            path = self._path
            frames = self._frames
            seconds = time.time() - self._started_at
            self._writer = None
            self._active = False
            self._segments += 1
            self._last_path = path
            self._last_frames = frames
            self._last_seconds = seconds
        try:
            writer.release()
        except Exception:
            pass
        log("录屏已保存: %s (%.1f 秒, %d 帧)" % (path, seconds, frames))

    def _fail(self, msg):
        with self._lock:
            self._error = msg
        now = time.time()
        # 同一个错误最多每 5 秒记一次, 避免锁屏/抓屏失败时刷爆控制台
        if msg != self._last_error_msg or now - self._last_error_log >= 5.0:
            self._last_error_msg = msg
            self._last_error_log = now
            log("!! %s" % msg)


def _run_record_test(argv):
    """诊断: 不加载模型、不弹框, 直接录制整个屏幕 N 秒并校验文件能否读回"""
    i = argv.index('--rectest')
    out_dir = (argv[i + 1] if len(argv) > i + 1
               else os.path.join(os.environ.get('TEMP', '.'), 'spm_rectest'))
    seconds = 4.0
    if len(argv) > i + 2:
        try:
            seconds = max(1.0, float(argv[i + 2]))
        except ValueError:
            pass
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        out_dir = os.path.join(os.environ.get('TEMP', '.'), 'spm_rectest')
        os.makedirs(out_dir, exist_ok=True)

    with mss() as sct:
        m = sct.monitors[0]
        box = {'left': m['left'], 'top': m['top'],
               'width': m['width'], 'height': m['height']}
    print("=== 录屏链路测试: 全屏 %dx%d, 录 %.1f 秒, 输出目录 %s ==="
          % (box['width'], box['height'], seconds, out_dir))
    rec = ScreenRecorder(box, out_dir, RECORD_FPS)
    rec.request_start()
    time.sleep(seconds)
    rec.request_stop()
    deadline = time.time() + 5.0
    while rec.is_recording() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    rec.close()

    snap = rec.snapshot()
    path = snap['last_path']
    print("RECTEST path=%s segments=%d frames=%d seconds=%.2f codec=%s error=%s"
          % (path, snap['segments'], snap['last_frames'], snap['last_seconds'],
             snap['codec'], snap['error']))
    if not path or not os.path.exists(path):
        print("RECTEST FAIL: 没有生成视频文件")
        return
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    size = "%dx%d" % (frame.shape[1], frame.shape[0]) if ok else "-"
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    print("RECTEST read_ok=%s size=%s fps=%.1f frames=%.0f file=%.1f KB"
          % (ok, size, fps, total, os.path.getsize(path) / 1024.0))
    print("RECTEST %s" % ("OK" if ok else "FAIL: 视频读不回来"))


# ---------------------------------------------------------------- 第 1 步
def choose_save_dir():
    default_dir = os.path.join(app_dir(), 'screenshots')
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    chosen = None
    try:
        from tkinter import filedialog
        chosen = filedialog.askdirectory(
            parent=root,
            title="第 1 步 / 共 4 步: 选择保存文件夹 (截图/录屏都存这里; 取消 = 默认 screenshots)"
        )
    finally:
        root.destroy()

    if not chosen:
        chosen = default_dir

    candidates = [chosen, default_dir,
                  os.path.join(os.path.expanduser('~'), 'Pictures', 'ScreenPersonMonitor')]
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, '.write_test')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
            return candidate
        except Exception:
            continue

    err = tk.Tk()
    err.withdraw()
    from tkinter import messagebox
    messagebox.showerror("错误", "没有可写入的文件夹, 请把程序放到有写权限的位置再运行。")
    err.destroy()
    sys.exit(1)


# ---------------------------------------------------------------- 界面工具
def _pick_font(win, size, weight='bold'):
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(win))
        for name in ('Microsoft YaHei UI', 'Microsoft YaHei', 'SimHei', 'SimSun'):
            if name in families:
                return (name, size, weight)
    except Exception:
        pass
    return ('Segoe UI', size, weight)


# ---------------------------------------------------------------- 第 2 步
def choose_mode():
    """
    选择工作模式。返回 MODE_SHOT / MODE_RECORD; 直接关窗或按 Esc 返回 None。
    """
    root = tk.Tk()
    root.title("第 2 步 / 共 4 步: 选择工作模式")
    root.attributes('-topmost', True)
    root.resizable(False, False)
    root.configure(bg='#1e1e1e')
    root.geometry("560x420")

    font_title = _pick_font(root, 17)
    font_sub = _pick_font(root, 10, 'normal')
    font_btn = _pick_font(root, 14)
    font_desc = _pick_font(root, 9, 'normal')

    result = {'mode': None}

    def pick(mode):
        result['mode'] = mode
        root.destroy()

    tk.Label(root, text="选择工作模式", bg='#1e1e1e', fg='#7289da',
             font=font_title).pack(pady=(18, 4))
    tk.Label(root, text="第 2 步 / 共 4 步 · 保存位置已选好, 接下来选截图还是录屏",
             bg='#1e1e1e', fg='#aaaaaa', font=font_sub).pack()

    wrap = tk.Frame(root, bg='#1e1e1e')
    wrap.pack(expand=True, fill='both', padx=22, pady=12)
    wrap.columnconfigure(0, weight=1)
    wrap.columnconfigure(1, weight=1)
    wrap.rowconfigure(0, weight=1)

    def make_card(column, title, color, lines, mode):
        card = tk.Frame(wrap, bg='#2a2a2a', bd=0, highlightthickness=1,
                        highlightbackground='#3a3a3a')
        card.grid(row=0, column=column, sticky='nsew', padx=8, pady=4)
        tk.Button(card, text=title, command=lambda: pick(mode),
                  bg=color, fg='white', activebackground=color,
                  relief='flat', cursor='hand2', font=font_btn, height=2
                  ).pack(fill='x', padx=12, pady=(14, 8))
        for text in lines:
            tk.Label(card, text=text, bg='#2a2a2a', fg='#cccccc',
                     font=font_desc, justify='left', anchor='w'
                     ).pack(fill='x', padx=14, pady=1)
        tk.Frame(card, bg='#2a2a2a', height=10).pack()

    make_card(0, "截图模式", '#3b6fd4',
              ["监控区域出现人", "→ 保存一张 PNG 截图", "和原来的用法完全一样"], MODE_SHOT)
    make_card(1, "录屏模式", '#c0392b',
              ["监控区域出现人", "→ 开始录制 MP4",
               "→ 连续 %.0f 秒没人自动停止" % RECORD_STOP_DELAY], MODE_RECORD)

    tk.Label(root, text="选完模式后, 下一步拖拽框选区域; 按 Esc 可取消",
             bg='#1e1e1e', fg='#888888', font=font_sub).pack(pady=(0, 14))

    root.protocol('WM_DELETE_WINDOW', root.destroy)
    root.bind('<Escape>', lambda e: root.destroy())
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 2)
    root.geometry("+%d+%d" % (x, y))
    root.mainloop()
    return result['mode']


def _parse_mode(argv):
    """命令行 --mode shot|record, 用于跳过模式选择 (自检/自动化)"""
    if '--mode' in argv:
        i = argv.index('--mode')
        val = argv[i + 1].strip().lower() if len(argv) > i + 1 else ''
        if val in ('record', 'rec', 'video', '录屏'):
            return MODE_RECORD
        if val in ('shot', 'screenshot', 'image', '截图'):
            return MODE_SHOT
    return None


# ---------------------------------------------------------------- 第 3/4 步
def select_region(step, title, hint):
    """
    全屏半透明覆盖层, 拖拽框选一个矩形。
    返回 (left, top, width, height) 物理像素; 按 Esc/右键取消则返回 None。
    """
    win = tk.Tk()
    win.title("区域选择")
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.attributes('-alpha', 0.30)
    win.configure(bg='black')

    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry("%dx%d+0+0" % (sw, sh))

    canvas = tk.Canvas(win, bg='black', highlightthickness=0, cursor='crosshair')
    canvas.pack(fill='both', expand=True)

    font_big = _pick_font(win, 20)
    font_small = _pick_font(win, 13, 'normal')
    font_hint = _pick_font(win, 11, 'normal')

    canvas.create_rectangle(0, 0, sw, 104, fill='black', outline='')
    canvas.create_text(24, 26, anchor='w', fill='#ffd400', font=font_big,
                       text="%s  %s" % (step, title))
    canvas.create_text(24, 64, anchor='w', fill='#ffffff', font=font_small, text=hint)
    canvas.create_text(24, 90, anchor='w', fill='#8fd6ff', font=font_hint,
                       text="屏幕 %dx%d | DPI 感知 %s" % (sw, sh, "OK" if _DPI_OK else "失败!"))
    warn = canvas.create_text(sw // 2, sh - 40, fill='#ff6060', font=font_small, text="")

    vline = canvas.create_line(0, 0, 0, sh, fill='#00ff88', width=1)
    hline = canvas.create_line(0, 0, sw, 0, fill='#00ff88', width=1)
    size_text = canvas.create_text(0, 0, anchor='nw', fill='#ff2020', font=font_small, text="")

    state = {'sx': 0, 'sy': 0, 'dragging': False, 'rect': None, 'result': None}

    def on_press(e):
        state['dragging'] = True
        state['sx'], state['sy'] = e.x, e.y
        if state['rect'] is not None:
            canvas.delete(state['rect'])
        state['rect'] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline='#ff2020', width=3, dash=(6, 4))
        canvas.itemconfigure(size_text, text="")
        canvas.itemconfigure(warn, text="")

    def on_move(e):
        canvas.coords(vline, e.x, 0, e.x, sh)
        canvas.coords(hline, 0, e.y, sw, e.y)
        if state['dragging'] and state['rect'] is not None:
            canvas.coords(state['rect'], state['sx'], state['sy'], e.x, e.y)
            canvas.itemconfigure(state['rect'], dash=())
            canvas.coords(size_text, min(state['sx'], e.x) + 6, min(state['sy'], e.y) - 24)
            canvas.itemconfigure(size_text, text="%d x %d" % (abs(e.x - state['sx']),
                                                              abs(e.y - state['sy'])))

    def on_release(e):
        state['dragging'] = False

    def on_right(e):
        if state['rect'] is not None:
            canvas.delete(state['rect'])
            state['rect'] = None
        canvas.itemconfigure(size_text, text="")

    def confirm(_=None):
        if state['rect'] is None:
            canvas.itemconfigure(warn, text="还没有框选区域, 请按住鼠标左键拖拽")
            return
        l, t, r, b = canvas.coords(state['rect'])
        l, r = int(min(l, r)), int(max(l, r))
        t, b = int(min(t, b)), int(max(t, b))
        if r - l >= MIN_SIDE and b - t >= MIN_SIDE:
            state['result'] = (l, t, r - l, b - t)
            win.destroy()
        else:
            canvas.itemconfigure(warn, text="区域太小 (至少 %d x %d), 请重画" % (MIN_SIDE, MIN_SIDE))

    def cancel(_=None):
        state['result'] = None
        win.destroy()

    canvas.bind('<ButtonPress-1>', on_press)
    canvas.bind('<B1-Motion>', on_move)
    canvas.bind('<ButtonRelease-1>', on_release)
    canvas.bind('<Motion>', on_move)
    canvas.bind('<Button-3>', on_right)
    canvas.bind('<Button-2>', on_right)
    win.bind('<Return>', confirm)
    win.bind('<KP_Enter>', confirm)
    win.bind('<Escape>', cancel)

    win.lift()
    win.focus_force()
    canvas.focus_set()
    win.mainloop()
    return state['result']


# ---------------------------------------------------------------- 主流程
def main():
    argv = sys.argv[1:]
    selftest_dir = None

    # 录屏链路自测: 不加载模型、不弹框, 直接录全屏几秒并读回校验
    if '--rectest' in argv:
        _run_record_test(argv)
        return

    if '--probe-dpi' in argv:
        info = screen_info()
        print("DPI 设置 : %s" % info['dpi_msg'])
        print("tkinter  : %s  scaling=%s" % (info['tk_screen'], info.get('tk_scaling')))
        print("mss      : %s" % (info['mss_screen'],))
        return

    if '--test-detect' in argv:
        # 诊断: 对一张图片直接推理, 打印原始检测结果(不做阈值过滤)
        i = argv.index('--test-detect')
        path = argv[i + 1] if len(argv) > i + 1 else ''
        print("frozen       : %s" % getattr(sys, 'frozen', False))
        print("模型路径     : %s" % MODEL_PATH)
        print("模型存在     : %s" % os.path.exists(MODEL_PATH))
        try:
            import hashlib
            with open(MODEL_PATH, 'rb') as f:
                print("模型 MD5     : %s  (大小 %d 字节)"
                      % (hashlib.md5(f.read()).hexdigest(), os.path.getsize(MODEL_PATH)))
        except Exception as exc:
            print("模型 MD5     : 计算失败 %r" % (exc,))
        print("torchvision  : %s" % __import__('torchvision').__version__)
        try:
            import torch
            print("torch::nms   : %s" % (torch.ops.torchvision.nms is not None))
        except Exception as exc:
            print("torch::nms   : 不可用 %r" % (exc,))
        img = cv2.imread(path)
        print("图片         : %s -> %s" % (path, None if img is None else img.shape))
        if img is not None:
            from ultralytics import YOLO
            m = YOLO(MODEL_PATH)
            raw = m.predict(source=img, conf=0.01, device='cpu', verbose=False)[0]
            items = sorted(((float(b.conf[0]), int(b.cls[0])) for b in raw.boxes), reverse=True)
            print("原始检出数   : %d" % len(items))
            for cf, cls in items[:10]:
                print("    %-10s %.4f" % (raw.names.get(cls, cls), cf))
            hits = [cf for cf, cls in items if cls == 0 and cf >= CONF_THRESHOLD]
            print(">=阈值(%.2f)的人数: %d" % (CONF_THRESHOLD, len(hits)))
        return

    locktest_dir = None
    if '--locktest' in argv:
        i = argv.index('--locktest')
        locktest_dir = argv[i + 1] if len(argv) > i + 1 else os.path.join(app_dir(), 'screenshots')

    if '--selftest' in argv:
        i = argv.index('--selftest')
        selftest_dir = argv[i + 1] if len(argv) > i + 1 else os.path.join(app_dir(), 'screenshots')

    cli_mode = _parse_mode(argv)

    if locktest_dir:
        # 锁屏测试: 监视全屏 120 秒, 期间用户可以按 Win+L 锁屏再解锁
        save_dir = locktest_dir
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception:
            save_dir = os.path.join(os.environ.get('TEMP', '.'), 'spm_locktest')
            os.makedirs(save_dir, exist_ok=True)
        log("=== 锁屏测试模式 (监视全屏 120 秒, 请按 Win+L 锁屏 20 秒再解锁) ===")
        info = screen_info()
        log("DPI: %s" % info['dpi_msg'])
        log("屏幕: tkinter=%s  mss=%s" % (info['tk_screen'], info['mss_screen']))
        with mss() as sct:
            m = sct.monitors[0]
            shot_roi = (m['left'], m['top'], m['width'], m['height'])
        mon_roi = shot_roi
        mode = cli_mode or MODE_SHOT
    elif selftest_dir:
        save_dir = selftest_dir
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception:
            save_dir = os.path.join(os.environ.get('TEMP', '.'), 'spm_selftest')
            os.makedirs(save_dir, exist_ok=True)
        log("=== 自检模式 (跳过框选, 监视整个屏幕 30 秒) ===")
        info = screen_info()
        log("DPI: %s" % info['dpi_msg'])
        log("屏幕: tkinter=%s  mss=%s" % (info['tk_screen'], info['mss_screen']))
        with mss() as sct:
            m = sct.monitors[0]
            shot_roi = (m['left'], m['top'], m['width'], m['height'])
        mon_roi = shot_roi
        mode = cli_mode or MODE_SHOT
    else:
        # ---- 步骤 1: 保存文件夹 ----
        save_dir = choose_save_dir()
        log("=== 启动 ===")
        info = screen_info()
        log("DPI: %s" % info['dpi_msg'])
        log("屏幕: tkinter=%s  mss=%s  scaling=%s"
            % (info['tk_screen'], info['mss_screen'], info.get('tk_scaling')))
        log("保存目录: %s" % save_dir)
        if not _DPI_OK:
            log("!! 警告: DPI 感知设置失败, 125%/150% 缩放下框选坐标可能偏移")
        tk_screen, mss_screen = info.get('tk_screen'), info.get('mss_screen')
        if isinstance(tk_screen, tuple) and isinstance(mss_screen, tuple) and tk_screen != mss_screen:
            log("!! 警告: tkinter 屏幕 %s 与抓屏尺寸 %s 不一致, 框选坐标会偏移!"
                % (tk_screen, mss_screen))

        # ---- 步骤 2: 工作模式 ----
        mode = cli_mode or choose_mode()
        if mode is None:
            _abort("已取消选择工作模式, 程序退出。")
        log("工作模式: %s" % ("录屏" if mode == MODE_RECORD else "截图"))

        # ---- 步骤 3: 截图区域 / 录屏区域 ----
        if mode == MODE_RECORD:
            shot_roi = select_region(
                "第 3 步 / 共 4 步", "框选【录屏区域】",
                "按住鼠标左键拖拽框选要录制下来的画面 | 右键重画 | 回车确认 | Esc 取消")
            if shot_roi is None:
                _abort("已取消选择【录屏区域】, 程序退出。")
            log("录屏区域 (left,top,w,h) = %s" % (shot_roi,))
        else:
            shot_roi = select_region(
                "第 3 步 / 共 4 步", "框选【截图区域】",
                "按住鼠标左键拖拽框选要保存下来的画面 | 右键重画 | 回车确认 | Esc 取消")
            if shot_roi is None:
                _abort("已取消选择【截图区域】, 程序退出。")
            log("截图区域 (left,top,w,h) = %s" % (shot_roi,))

        # ---- 步骤 4: 监控区域 ----
        if mode == MODE_RECORD:
            mon_hint = ("按住鼠标左键拖拽框选要监视的位置"
                        "(有人出现就开始录屏, 连续 %.0f 秒没人后停止)"
                        " | 右键重画 | 回车确认 | Esc 取消" % RECORD_STOP_DELAY)
        else:
            mon_hint = ("按住鼠标左键拖拽框选要监视的位置(有人经过就触发截图)"
                        " | 右键重画 | 回车确认 | Esc 取消")
        mon_roi = select_region("第 4 步 / 共 4 步", "框选【监控区域】", mon_hint)
        if mon_roi is None:
            _abort("已取消选择【监控区域】, 程序退出。")
        log("监控区域 (left,top,w,h) = %s" % (mon_roi,))

    # 调试图(首帧/锁屏证据)统一放到系统临时目录, 不污染用户的保存文件夹
    debug_dir = os.path.join(os.environ.get('TEMP', '.'), 'ScreenPersonMonitor_debug')
    try:
        os.makedirs(debug_dir, exist_ok=True)
    except Exception:
        debug_dir = os.environ.get('TEMP', '.')

    # ---- 加载模型 ----
    log("正在加载 YOLO 模型: %s" % MODEL_PATH)
    log("模型文件存在: %s" % os.path.exists(MODEL_PATH))
    try:
        from ultralytics import YOLO
        model = YOLO(MODEL_PATH)
    except Exception as exc:
        log("!! 模型加载失败: %r" % (exc,))
        log(traceback.format_exc())
        _abort("模型加载失败:\n%r" % (exc,))
        return
    log("模型加载完成。")

    mon_box = {"left": mon_roi[0], "top": mon_roi[1],
               "width": mon_roi[2], "height": mon_roi[3]}
    shot_box = {"left": shot_roi[0], "top": shot_roi[1],
                "width": shot_roi[2], "height": shot_roi[3]}

    # ---- 录屏器 (只有录屏模式才创建) ----
    recorder = None
    if mode == MODE_RECORD:
        recorder = ScreenRecorder(shot_box, save_dir, RECORD_FPS)
        log("录屏设置: 帧率 %d fps, 连续 %.1f 秒没人自动停止, 输出目录 %s"
            % (RECORD_FPS, RECORD_STOP_DELAY, save_dir))
    else:
        log("截图设置: 冷却 %.0f 秒, 输出目录 %s" % (COOLDOWN, save_dir))

    state = {
        'running': True,
        'mode': mode,
        'person': False,
        'shots': 0,
        'last_capture': 0.0,
        'last_name': '',
        'preview': None,
        'boxes': [],
        'frames': 0,
        'locked': False,
        'error': '',
    }
    lock = threading.Lock()

    # ---- 后台检测线程 ----
    def monitor_loop():
        if mode == MODE_RECORD:
            log("监视线程启动: 监控区域=%s 录屏区域=%s 阈值=%.2f (有人开始录, 连续 %.1f 秒没人停)"
                % (mon_roi, shot_roi, CONF_THRESHOLD, RECORD_STOP_DELAY))
        else:
            log("监视线程启动: 监控区域=%s 截图区域=%s 阈值=%.2f 冷却=%.0fs"
                % (mon_roi, shot_roi, CONF_THRESHOLD, COOLDOWN))
        grab_fail = 0
        lock_fail_streak = 0    # 连续抓屏失败次数(锁屏判定要连续失败, 避免误报)
        ok_streak = 0           # 连续抓屏成功次数(解锁判定也要连续成功)
        lock_reported = False
        last_person = 0.0       # 录屏模式: 最后一次检测到人的时间
        stop_announced = False  # 避免"停止录屏"日志反复打印
        try:
            with mss() as sct:
                while state['running']:
                    try:
                        frame = cv2.cvtColor(np.array(sct.grab(mon_box)), cv2.COLOR_BGRA2BGR)
                    except Exception as exc:
                        grab_fail += 1
                        lock_fail_streak += 1
                        ok_streak = 0
                        now_locked, why = is_desktop_locked()
                        # 滞后判断: 要连续 3 次失败(抓屏失败 + 桌面确实打不开)才算锁屏,
                        # 因为切换桌面的一瞬间偶尔会有一次侥幸成功, 会误报"解锁"。
                        if now_locked and lock_fail_streak >= 3:
                            with lock:
                                state['locked'] = True
                            if not lock_reported:
                                log("检测到屏幕已锁定(Win+L) -> 系统禁止抓屏, "
                                    "锁定期间无法监视, 解锁后会自动继续 [%s]" % why)
                                lock_reported = True
                            with lock:
                                state['error'] = "屏幕已锁定, 等待解锁..."
                            # 锁屏时抓不到画面, 正在录的也先停下来, 免得留下空文件
                            if mode == MODE_RECORD and recorder.is_recording():
                                recorder.request_stop()
                                log("屏幕锁定 -> 停止录屏; 解锁后检测到人会重新开始")
                            # 自检模式: 锁屏时再抓一帧, 作为"锁定期间抓不到"的证据
                            if (selftest_dir or locktest_dir) and not state.get('lock_probe_done'):
                                try:
                                    probe = cv2.cvtColor(np.array(sct.grab(mon_box)),
                                                         cv2.COLOR_BGRA2BGR)
                                    cv2.imwrite(os.path.join(debug_dir, 'debug_locked_frame.png'), probe)
                                    log("锁定期间居然抓到画面? 已存到临时目录")
                                except Exception as exc2:
                                    log("锁定期间抓屏确认失败: %r" % (exc2,))
                                state['lock_probe_done'] = True
                            time.sleep(1.0)
                            continue
                        # 真正异常: 前面 3 次详细记录, 之后每 60 次记一次, 避免刷爆日志
                        if grab_fail <= 3 or grab_fail % 60 == 0:
                            log("!! 抓取监控区域失败(%d): %r" % (grab_fail, exc))
                        with lock:
                            state['error'] = "抓取监控区域失败: %s" % exc
                        time.sleep(0.5)
                        continue

                    # 抓屏成功
                    lock_fail_streak = 0
                    ok_streak += 1
                    # 滞后判断: 要连续若干次成功才认定真的解锁了
                    if lock_reported and ok_streak >= 3:
                        log("屏幕已解锁, 恢复监视")
                        lock_reported = False

                    with lock:
                        state['locked'] = False
                        state['preview'] = frame
                        first_frame = state['frames'] == 0
                    # 自检模式把第一帧存下来, 方便确认屏幕上到底有什么
                    if selftest_dir and first_frame:
                        try:
                            cv2.imwrite(os.path.join(debug_dir, 'debug_first_frame.png'), frame)
                            log("已保存首帧预览: %s"
                                % os.path.join(debug_dir, 'debug_first_frame.png'))
                        except Exception as exc:
                            log("保存首帧失败: %r" % (exc,))

                    try:
                        results = model.predict(source=frame, conf=CONF_THRESHOLD,
                                                device='cpu', verbose=False)
                    except Exception as exc:
                        log("!! 模型推理失败: %r" % (exc,))
                        with lock:
                            state['error'] = "模型推理失败: %s" % exc
                        time.sleep(0.5)
                        continue

                    person = False
                    best = 0.0
                    found = []
                    for r in results:
                        for box in r.boxes:
                            if int(box.cls[0]) == 0:
                                person = True
                                cf = float(box.conf[0])
                                best = max(best, cf)
                                found.append(tuple(list(map(float, box.xyxy[0])) + [cf]))

                    now = time.time()
                    trigger = False
                    with lock:
                        state['person'] = person
                        state['frames'] += 1
                        state['boxes'] = found
                        if (mode == MODE_SHOT and person
                                and (now - state['last_capture']) >= COOLDOWN):
                            state['last_capture'] = now
                            trigger = True

                    # 录屏模式: 监控区域出现人就开始录, 连续 RECORD_STOP_DELAY 秒没人就停
                    if mode == MODE_RECORD:
                        if person:
                            last_person = now
                            stop_announced = False
                            if not recorder.is_recording():
                                log("检测到人 (置信度 %.3f) -> 开始录屏" % best)
                                recorder.request_start()
                        elif (recorder.is_recording()
                              and (now - last_person) >= RECORD_STOP_DELAY):
                            if not stop_announced:
                                log("已连续 %.0f 秒未检测到人 -> 停止录屏"
                                    % RECORD_STOP_DELAY)
                                stop_announced = True
                            recorder.request_stop()

                    # 自检模式: 每 3 秒报告一次最高置信度(不做阈值过滤),
                    # 用来判断"是没检测到, 还是检测到了但没过阈值"
                    if selftest_dir and now - state.get('last_report', 0) >= 3:
                        state['last_report'] = now
                        do_raw = True
                    else:
                        do_raw = False

                    if do_raw:
                        try:
                            raw = model.predict(source=frame, conf=0.02,
                                                device='cpu', verbose=False)[0]
                            raw_ppl = [(float(b.conf[0]), int(b.cls[0])) for b in raw.boxes]
                            raw_ppl.sort(reverse=True)
                            top = ", ".join("%s %.3f" % (raw.names.get(c, c), v)
                                            for v, c in raw_ppl[:5]) or "无任何检出"
                            log("自检: 第 %d 帧 最高置信度(阈值0.02): %s"
                                % (state['frames'], top))
                        except Exception as exc:
                            log("自检原始推理失败: %r" % (exc,))

                    if trigger:
                        log("检测到人 (置信度 %.3f) -> 抓取截图区域并保存" % best)
                        try:
                            shot = cv2.cvtColor(np.array(sct.grab(shot_box)), cv2.COLOR_BGRA2BGR)
                            fname = time.strftime("person_%Y%m%d_%H%M%S.png")
                            fpath = os.path.join(save_dir, fname)
                            if cv2.imwrite(fpath, shot):
                                with lock:
                                    state['shots'] += 1
                                    state['last_name'] = fname
                                    n = state['shots']
                                log("已保存 #%d: %s (%dx%d)"
                                    % (n, fpath, shot.shape[1], shot.shape[0]))
                            else:
                                with lock:
                                    state['error'] = "写入失败: %s" % fpath
                                log("!! 写入失败: %s" % fpath)
                        except Exception as exc:
                            with lock:
                                state['error'] = "保存截图失败: %s" % exc
                            log("!! 保存截图失败: %r" % (exc,))
                            log(traceback.format_exc())

                    time.sleep(0.05)
        except Exception as exc:
            log("!! 监视线程崩溃: %r" % (exc,))
            log(traceback.format_exc())
        finally:
            log("监视线程结束")

    threading.Thread(target=monitor_loop, daemon=True).start()

    # ---- 预览窗 (主线程) ----
    win_name = ("MONITORING [REC] - press Q or Esc to quit" if mode == MODE_RECORD
                else "MONITORING - press Q or Esc to quit")
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_name, max(240, min(mon_roi[2], 900)), max(160, min(mon_roi[3], 700)))
    cv2.setWindowProperty(win_name, cv2.WND_PROP_TOPMOST, 1)
    cv2.moveWindow(win_name, 40, 40)

    blank = np.zeros((mon_roi[3], mon_roi[2], 3), np.uint8)
    if locktest_dir:
        selftest_deadline = time.time() + 120
    elif selftest_dir:
        selftest_deadline = time.time() + 30
    else:
        selftest_deadline = None

    while state['running']:
        with lock:
            frame = state['preview']
            person = state['person']
            shots = state['shots']
            last_name = state['last_name']
            err = state['error']
            found = list(state['boxes'])
            locked = state['locked']

        rec = recorder.snapshot() if recorder is not None else None

        disp = frame.copy() if frame is not None else blank.copy()
        if disp.shape[1] != mon_roi[2] or disp.shape[0] != mon_roi[3]:
            disp = cv2.resize(disp, (mon_roi[2], mon_roi[3]))
        H, W = disp.shape[:2]

        # 状态条: 单行紧凑设计, 只占 34px; 画面太矮时自动缩短或不显示
        font_sz = max(13, min(24, int(W / 48)))
        bar_h = min(BAR_H, int(H * 0.20))
        if bar_h < 16:
            bar_h = 0
        if bar_h > 0:
            overlay = disp.copy()
            cv2.rectangle(overlay, (0, 0), (W, bar_h), (0, 0, 0), -1)
            disp = cv2.addWeighted(overlay, 0.55, disp, 0.45, 0)

        scale = max(0.45, min(0.9, W / 700.0))
        thick = max(1, int(round(scale * 2)))

        # 检测框(画在提示条下方, 方便肉眼确认"到底有没有检测到人")
        if found and mon_roi[2]:
            sc = W / float(mon_roi[2])
            sh_ = H / float(mon_roi[3]) if mon_roi[3] else sc
            for (bx1, by1, bx2, by2, cf) in found:
                p1 = (int(bx1 * sc), int(by1 * sh_) + bar_h)
                p2 = (int(bx2 * sc), int(by2 * sh_) + bar_h)
                cv2.rectangle(disp, p1, p2, (0, 255, 255), thick)
                draw_text(disp, "人 %.0f%%" % (cf * 100.0),
                          (p1[0], max(bar_h + 2, p1[1] - int(font_sz * 1.2))),
                          size=max(12, int(font_sz * 0.7)), color=(0, 255, 255))

        # 状态文字: 中文单行, 尽量少遮挡画面
        if locked:
            line, color = "已锁定 · 暂停监视", (0, 200, 255)
        elif rec is not None and rec['active']:
            line, color = ("录制中 · %.1f 秒 · 第 %d 段"
                           % (rec['seconds'], rec['segments'] + 1)), (0, 0, 255)
        elif mode == MODE_RECORD and person:
            line, color = "检测到人 · 开始录制", (0, 0, 255)
        elif mode == MODE_RECORD:
            line, color = ("监视中 · 等待出现人 · 已录 %d 段"
                           % (rec['segments'] if rec else 0)), (0, 255, 0)
        elif person:
            line, color = "检测到人 · 正在截图", (0, 0, 255)
        else:
            line, color = "监视中 · 已存 %d 张" % shots, (0, 255, 0)

        if bar_h > 0 and line:
            draw_text(disp, line, (6, max(1, (bar_h - font_sz) // 2)),
                      size=font_sz, color=color, bold=True)

        # 录屏时右上角亮一个红点, 一眼就能看出在录
        if rec is not None and rec['active'] and bar_h >= 24:
            r = max(4, bar_h // 5)
            cv2.circle(disp, (W - r - 8, bar_h // 2), r, (0, 0, 255), -1)

        if not err and rec is not None:
            err = rec['error']

        if err:
            draw_text(disp, err[:40], (6, H - font_sz - 6),
                      size=max(12, int(font_sz * 0.7)), color=(0, 165, 255))

        cv2.rectangle(disp, (1, 1), (W - 2, H - 2), (0, 0, 255), thick)
        cv2.imshow(win_name, disp)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord('q'), ord('Q'), 27):
            state['running'] = False
            break
        try:
            if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1:
                state['running'] = False
                break
        except Exception:
            state['running'] = False
            break

        if selftest_deadline and time.time() > selftest_deadline:
            state['running'] = False
            break

    state['running'] = False
    if recorder is not None:
        recorder.close()
    time.sleep(0.2)
    cv2.destroyAllWindows()
    with lock:
        frames, shots = state['frames'], state['shots']
    log("已退出。共检测 %d 帧, 保存 %d 张截图到: %s" % (frames, shots, save_dir))
    if recorder is not None:
        snap = recorder.snapshot()
        log("录屏结束: 共 %d 段, 最后一段 %s (%.1f 秒, %d 帧)"
            % (snap['segments'], snap['last_path'] or '无',
               snap['last_seconds'], snap['last_frames']))
        print("RECORD segments=%d last=%s" % (snap['segments'], snap['last_path']))
    if selftest_dir:
        print("SELFTEST frames=%d shots=%d" % (frames, shots))


def _abort(msg):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        from tkinter import messagebox
        messagebox.showinfo("ScreenPersonMonitor", msg)
    finally:
        root.destroy()
    sys.exit(0)


if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        _install_crash_handler()
    main()
