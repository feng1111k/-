"""
屏幕人物监视截图工具
- 启动后在屏幕上拖拽一个矩形作为监视区
- 后台持续检测该区域内是否出现人(YOLO COCO class 0 = person)
- 一旦检测到人,立即对整个屏幕全屏截图,按时间戳保存到 screenshots/ 文件夹
- 可配置置信度阈值与截图冷却时间,避免同一画面反复截图
"""

import cv2
import numpy as np
import os
import time
import threading
import tkinter as tk
from mss import mss
from ultralytics import YOLO
import ctypes

# 高DPI下保证 tkinter 坐标与 mss 截屏坐标一致
ctypes.windll.user32.SetProcessDPIAware()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, 'yolov8n.pt')
SCREENSHOT_DIR = os.path.join(BASE_DIR, 'screenshots')
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

print("正在加载 YOLO 模型...")
model = YOLO(MODEL_PATH)
print("模型加载完成。")


class State:
    roi = None                 # (left, top, width, height) 物理像素
    conf = 0.45                # person 置信度阈值
    cooldown = 5.0             # 两次截图之间最小间隔(秒)
    running = True
    monitoring = False
    last_capture = 0.0
    detect_count = 0
    last_event = "尚未开始"


s = State()


# ---------------- 区域选择窗口 ----------------
def select_roi_on_screen():
    """弹出全屏半透明窗口,让用户拖拽鼠标画矩形。返回 (left, top, w, h) 或 None。"""
    win = tk.Tk()
    win.title("拖拽选择监视区域")
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{sw}x{sh}+0+0")
    win.attributes('-topmost', True)
    win.attributes('-alpha', 0.35)
    win.attributes('-fullscreen', True)
    win.configure(bg='black')

    canvas = tk.Canvas(win, bg='gray20', highlightthickness=0, cursor='cross')
    canvas.pack(fill='both', expand=True)

    canvas.create_text(
        sw // 2, 40,
        text="按住鼠标左键拖拽框选监视区域   |   Enter 确认   |   Esc 取消",
        fill='yellow', font=('Segoe UI', 16, 'bold')
    )

    start = [0, 0]
    rect_id = [None]
    result = [None]

    def on_press(e):
        start[0], start[1] = e.x, e.y
        if rect_id[0] is not None:
            canvas.delete(rect_id[0])
        rect_id[0] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline='red', width=3
        )

    def on_drag(e):
        if rect_id[0] is not None:
            canvas.coords(rect_id[0], start[0], start[1], e.x, e.y)

    def confirm(_=None):
        if rect_id[0] is None:
            return  # 还没画,不关闭
        l, t, r, b = canvas.coords(rect_id[0])
        l, r = int(min(l, r)), int(max(l, r))
        t, b = int(min(t, b)), int(max(t, b))
        if r - l >= 20 and b - t >= 20:
            result[0] = (l, t, r - l, b - t)
        win.destroy()

    def cancel(_=None):
        result[0] = None
        win.destroy()

    canvas.bind('<ButtonPress-1>', on_press)
    canvas.bind('<B1-Motion>', on_drag)
    win.bind('<Return>', confirm)
    win.bind('<Escape>', cancel)
    win.focus_force()
    win.mainloop()
    return result[0]


# ---------------- 监控后台线程 ----------------
def monitor_loop(status_var):
    with mss() as sct:
        # monitors[0] = 整个虚拟桌面(多屏合并); 取它做全屏截图,保证所有显示器都抓到
        full_monitor = sct.monitors[0]

        while s.running:
            if not s.monitoring or s.roi is None:
                time.sleep(0.2)
                continue

            left, top, w, h = s.roi
            roi_monitor = {"top": top, "left": left, "width": w, "height": h}

            try:
                raw = sct.grab(roi_monitor)
                frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
            except Exception:
                time.sleep(0.3)
                continue

            results = model.predict(
                source=frame, conf=s.conf, device='cpu', verbose=False
            )

            person_found = False
            for r in results:
                for box in r.boxes:
                    if int(box.cls[0]) == 0:   # COCO class 0 = person
                        person_found = True
                        break
                if person_found:
                    break

            now = time.time()
            if person_found and (now - s.last_capture) >= s.cooldown:
                # 全屏截图
                full_raw = sct.grab(full_monitor)
                full_img = cv2.cvtColor(np.array(full_raw), cv2.COLOR_BGRA2BGR)
                fname = time.strftime("person_%Y%m%d_%H%M%S.png")
                fpath = os.path.join(SCREENSHOT_DIR, fname)
                cv2.imwrite(fpath, full_img)
                s.last_capture = now
                s.detect_count += 1
                s.last_event = f"已保存截图: {fname}"

            time.sleep(0.1)


# ---------------- 主控制面板 ----------------
def main():
    root = tk.Tk()
    root.title("屏幕人物监视截图")
    root.geometry("380x460")
    root.attributes('-topmost', True)
    root.configure(bg='#1e1e1e')

    fg = '#eeeeee'
    tk.Label(root, text="屏幕人物监视截图", bg='#1e1e1e', fg='#7289da',
             font=('Segoe UI', 16, 'bold')).pack(pady=12)

    roi_var = tk.StringVar(value="监视区: 未设置")
    tk.Label(root, textvariable=roi_var, bg='#1e1e1e', fg='#aaaaaa').pack()

    status_var = tk.StringVar(value="点击下方按钮选择监视区域")
    tk.Label(root, textvariable=status_var, bg='#1e1e1e', fg='#5cd65c',
             wraplength=350, justify='left', font=('Segoe UI', 10)).pack(pady=8, padx=10)

    def choose_roi():
        root.withdraw()
        time.sleep(0.3)
        roi = select_roi_on_screen()
        root.deiconify()
        if roi:
            s.roi = roi
            s.monitoring = True
            roi_var.set(f"监视区: left={roi[0]}, top={roi[1]}, {roi[2]}x{roi[3]}")
            status_var.set("监视中... 检测到人将自动全屏截图")
        else:
            status_var.set("已取消选择,请重试")

    tk.Button(root, text="① 选择 / 重选监视区域", command=choose_roi,
              bg='#7289da', fg='white', relief='flat',
              font=('Segoe UI', 10, 'bold')).pack(pady=8, fill='x', padx=30)

    tk.Label(root, text="检测置信度阈值", bg='#1e1e1e', fg=fg).pack()
    tk.Scale(root, from_=0.1, to=0.9, resolution=0.05, orient='horizontal',
             bg='#1e1e1e', fg=fg, highlightthickness=0, troughcolor='#333333',
             command=lambda v: setattr(s, 'conf', float(v))
             ).set(s.conf)

    tk.Label(root, text="截图冷却时间(秒)", bg='#1e1e1e', fg=fg).pack()
    tk.Scale(root, from_=1, to=30, resolution=1, orient='horizontal',
             bg='#1e1e1e', fg=fg, highlightthickness=0, troughcolor='#333333',
             command=lambda v: setattr(s, 'cooldown', float(v))
             ).set(s.cooldown)

    def refresh():
        if s.monitoring:
            remain = max(0, s.cooldown - (time.time() - s.last_capture))
            status_var.set(
                f"监视中...\n累计截图: {s.detect_count}\n"
                f"{s.last_event}\n冷却剩余: {remain:.1f}s"
            )
        root.after(500, refresh)

    threading.Thread(target=monitor_loop, args=(status_var,), daemon=True).start()
    refresh()

    def on_quit():
        s.running = False
        root.destroy()

    tk.Button(root, text="退出", command=on_quit,
              bg='#f04747', fg='white', relief='flat',
              font=('Segoe UI', 10, 'bold')).pack(pady=12, fill='x', padx=30)

    root.mainloop()


if __name__ == '__main__':
    main()
