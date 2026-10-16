# -*- coding: utf-8 -*-
"""
屏幕人物监视截图工具  (OpenCV / 控制台版)

完整流程:
  1. 启动后先选择截图保存文件夹
  2. 在屏幕上拖拽框选【截图区域】  -> 有人经过时要保存下来的那块区域
  3. 在屏幕上拖拽框选【监控区域】  -> 用来检测有没有人
  4. 后台持续检测监控区域, 一旦出现人, 就抓取【截图区域】并按时间戳保存
  5. 预览窗实时显示监控区域, 按 Q 或 Esc 退出

关键点:
  - 两个区域分开选择, 检测用监控区域, 保存用截图区域
  - 全屏覆盖层用 tkinter 实现, 坐标 = 物理像素, 与 mss 抓屏坐标 1:1 对应
  - 强制进程 DPI 感知(每显示器 v2), 125%/150% 缩放下坐标也不会偏移
  - 全程写日志到 <保存目录>\\monitor_log.txt, 出问题时可以直接看日志

命令行:
  python main_opencv.py                        正常使用
  python main_opencv.py --probe-dpi            只打印 DPI/屏幕信息, 不启动界面
  python main_opencv.py --test-detect <图片>    对一张图片跑一次检测, 打印原始结果
  python main_opencv.py --selftest <保存目录>   跳过框选, 直接监视全屏 30 秒
"""

import os
import sys
import time
import threading
import traceback


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
from ultralytics import YOLO

# ---------------- 可调参数 ----------------
CONF_THRESHOLD = 0.45   # person 置信度阈值
COOLDOWN = 5.0          # 两次截图最小间隔(秒)
MIN_SIDE = 20           # 区域最小边长


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
    """同时写控制台和 <保存目录>\\monitor_log.txt, 方便排查问题"""

    def __init__(self):
        self.path = None
        self._lock = threading.Lock()

    def bind(self, directory):
        try:
            os.makedirs(directory, exist_ok=True)
            self.path = os.path.join(directory, 'monitor_log.txt')
        except Exception:
            self.path = os.path.join(os.environ.get('TEMP', '.'), 'monitor_log.txt')

    def __call__(self, msg):
        line = "[%s] %s" % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
        with self._lock:
            try:
                print(line)
            except Exception:
                pass
            if self.path:
                try:
                    with open(self.path, 'a', encoding='utf-8') as f:
                        f.write(line + "\n")
                except Exception:
                    pass


log = Log()

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
            title="第 1 步 / 共 3 步: 选择截图保存文件夹 (取消 = 用默认 screenshots)"
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


# ---------------------------------------------------------------- 第 2/3 步
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
            m = YOLO(MODEL_PATH)
            raw = m.predict(source=img, conf=0.01, device='cpu', verbose=False)[0]
            items = sorted(((float(b.conf[0]), int(b.cls[0])) for b in raw.boxes), reverse=True)
            print("原始检出数   : %d" % len(items))
            for cf, cls in items[:10]:
                print("    %-10s %.4f" % (raw.names.get(cls, cls), cf))
            hits = [cf for cf, cls in items if cls == 0 and cf >= CONF_THRESHOLD]
            print(">=阈值(%.2f)的人数: %d" % (CONF_THRESHOLD, len(hits)))
        return

    if '--selftest' in argv:
        i = argv.index('--selftest')
        selftest_dir = argv[i + 1] if len(argv) > i + 1 else os.path.join(app_dir(), 'screenshots')

    if selftest_dir:
        save_dir = selftest_dir
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception:
            save_dir = os.path.join(os.environ.get('TEMP', '.'), 'spm_selftest')
            os.makedirs(save_dir, exist_ok=True)
        log.bind(save_dir)
        log("=== 自检模式 (跳过框选, 监视整个屏幕 30 秒) ===")
        info = screen_info()
        log("DPI: %s" % info['dpi_msg'])
        log("屏幕: tkinter=%s  mss=%s" % (info['tk_screen'], info['mss_screen']))
        with mss() as sct:
            m = sct.monitors[0]
            shot_roi = (m['left'], m['top'], m['width'], m['height'])
        mon_roi = shot_roi
    else:
        # ---- 步骤 1: 保存文件夹 ----
        save_dir = choose_save_dir()
        log.bind(save_dir)
        log("=== 启动 ===")
        info = screen_info()
        log("DPI: %s" % info['dpi_msg'])
        log("屏幕: tkinter=%s  mss=%s  scaling=%s"
            % (info['tk_screen'], info['mss_screen'], info.get('tk_scaling')))
        log("截图保存目录: %s" % save_dir)
        if not _DPI_OK:
            log("!! 警告: DPI 感知设置失败, 125%/150% 缩放下框选坐标可能偏移")
        tk_screen, mss_screen = info.get('tk_screen'), info.get('mss_screen')
        if isinstance(tk_screen, tuple) and isinstance(mss_screen, tuple) and tk_screen != mss_screen:
            log("!! 警告: tkinter 屏幕 %s 与抓屏尺寸 %s 不一致, 框选坐标会偏移!"
                % (tk_screen, mss_screen))

        # ---- 步骤 2: 截图区域 ----
        shot_roi = select_region(
            "第 2 步 / 共 3 步", "框选【截图区域】",
            "按住鼠标左键拖拽框选要保存下来的画面 | 右键重画 | 回车确认 | Esc 取消")
        if shot_roi is None:
            _abort("已取消选择【截图区域】, 程序退出。")
        log("截图区域 (left,top,w,h) = %s" % (shot_roi,))

        # ---- 步骤 3: 监控区域 ----
        mon_roi = select_region(
            "第 3 步 / 共 3 步", "框选【监控区域】",
            "按住鼠标左键拖拽框选要监视的位置(有人经过就触发截图) | 右键重画 | 回车确认 | Esc 取消")
        if mon_roi is None:
            _abort("已取消选择【监控区域】, 程序退出。")
        log("监控区域 (left,top,w,h) = %s" % (mon_roi,))

    # ---- 加载模型 ----
    log("正在加载 YOLO 模型: %s" % MODEL_PATH)
    log("模型文件存在: %s" % os.path.exists(MODEL_PATH))
    try:
        model = YOLO(MODEL_PATH)
    except Exception as exc:
        log("!! 模型加载失败: %r" % (exc,))
        log(traceback.format_exc())
        _abort("模型加载失败:\n%r\n\n详见日志: %s" % (exc, log.path))
        return
    log("模型加载完成。")

    mon_box = {"left": mon_roi[0], "top": mon_roi[1],
               "width": mon_roi[2], "height": mon_roi[3]}
    shot_box = {"left": shot_roi[0], "top": shot_roi[1],
                "width": shot_roi[2], "height": shot_roi[3]}

    state = {
        'running': True,
        'person': False,
        'shots': 0,
        'last_capture': 0.0,
        'last_name': '',
        'preview': None,
        'boxes': [],
        'frames': 0,
        'error': '',
    }
    lock = threading.Lock()

    # ---- 后台检测线程 ----
    def monitor_loop():
        log("监视线程启动: 监控区域=%s 截图区域=%s 阈值=%.2f 冷却=%.0fs"
            % (mon_roi, shot_roi, CONF_THRESHOLD, COOLDOWN))
        grab_fail = 0
        try:
            with mss() as sct:
                while state['running']:
                    try:
                        frame = cv2.cvtColor(np.array(sct.grab(mon_box)), cv2.COLOR_BGRA2BGR)
                    except Exception as exc:
                        grab_fail += 1
                        if grab_fail <= 3:
                            log("!! 抓取监控区域失败(%d): %r" % (grab_fail, exc))
                        with lock:
                            state['error'] = "抓取监控区域失败: %s" % exc
                        time.sleep(0.5)
                        continue

                    with lock:
                        state['preview'] = frame
                        first_frame = state['frames'] == 0
                    # 自检模式把第一帧存下来, 方便确认屏幕上到底有什么
                    if selftest_dir and first_frame:
                        try:
                            cv2.imwrite(os.path.join(save_dir, 'debug_first_frame.png'), frame)
                            log("已保存首帧预览: debug_first_frame.png (%dx%d)"
                                % (frame.shape[1], frame.shape[0]))
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
                        if person and (now - state['last_capture']) >= COOLDOWN:
                            state['last_capture'] = now
                            trigger = True
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
    win_name = "MONITORING - press Q or Esc to quit"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_name, max(240, min(mon_roi[2], 900)), max(160, min(mon_roi[3], 700)))
    cv2.setWindowProperty(win_name, cv2.WND_PROP_TOPMOST, 1)
    cv2.moveWindow(win_name, 40, 40)

    blank = np.zeros((mon_roi[3], mon_roi[2], 3), np.uint8)
    selftest_deadline = time.time() + 30 if selftest_dir else None

    while state['running']:
        with lock:
            frame = state['preview']
            person = state['person']
            shots = state['shots']
            last_name = state['last_name']
            err = state['error']
            found = list(state['boxes'])

        disp = frame.copy() if frame is not None else blank.copy()
        if disp.shape[1] != mon_roi[2] or disp.shape[0] != mon_roi[3]:
            disp = cv2.resize(disp, (mon_roi[2], mon_roi[3]))

        bar_h = 56 if disp.shape[0] > 120 else max(24, disp.shape[0] // 4)
        overlay = disp.copy()
        cv2.rectangle(overlay, (0, 0), (disp.shape[1], bar_h), (0, 0, 0), -1)
        disp = cv2.addWeighted(overlay, 0.7, disp, 0.3, 0)

        scale = max(0.45, min(0.9, disp.shape[1] / 700.0))
        thick = max(1, int(round(scale * 2)))

        # 检测框(画在提示条下方, 方便肉眼确认"到底有没有检测到人")
        if found and mon_roi[2]:
            sc = disp.shape[1] / float(mon_roi[2])
            sh_ = disp.shape[0] / float(mon_roi[3]) if mon_roi[3] else sc
            for (bx1, by1, bx2, by2, cf) in found:
                p1 = (int(bx1 * sc), int(by1 * sh_) + bar_h)
                p2 = (int(bx2 * sc), int(by2 * sh_) + bar_h)
                cv2.rectangle(disp, p1, p2, (0, 255, 255), thick)
                cv2.putText(disp, "%.2f" % cf, (p1[0], max(bar_h + 14, p1[1] - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, scale * 0.6, (0, 255, 255), 1)

        if disp.shape[0] > 60:
            if person:
                line, color = "PERSON DETECTED - saving!", (0, 0, 255)
            else:
                line, color = "monitoring... shots: %d" % shots, (0, 255, 0)
            cv2.putText(disp, line, (12, int(bar_h * 0.68)),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick)
            if last_name and not person:
                cv2.putText(disp, "last: %s" % last_name[:24],
                            (12, int(bar_h * 0.68) + int(bar_h * 0.5)),
                            cv2.FONT_HERSHEY_SIMPLEX, scale * 0.6, (200, 200, 200), 1)
            if err:
                cv2.putText(disp, err[:60], (12, disp.shape[0] - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, scale * 0.6, (0, 165, 255), 1)

        cv2.rectangle(disp, (1, 1), (disp.shape[1] - 2, disp.shape[0] - 2), (0, 0, 255), thick)
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
    time.sleep(0.4)
    cv2.destroyAllWindows()
    with lock:
        frames, shots = state['frames'], state['shots']
    log("已退出。共检测 %d 帧, 保存 %d 张截图到: %s" % (frames, shots, save_dir))
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
    main()
