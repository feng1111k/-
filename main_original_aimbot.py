import cv2
import numpy as np
import win32api, win32con, win32gui
from mss import mss
from ultralytics import YOLO
import keyboard
import ctypes
import threading
import tkinter as tk
from tkinter import ttk
import os
import time

ctypes.windll.user32.SetProcessDPIAware()
model = YOLO('yolov8n.pt')

class Settings:
    fov = 150
    speed = 0.4
    steps = 5          
    conf = 0.5         
    head_adjust = 0.15
    aim_enabled = False
    show_preview = False 
    show_overlay = True
    running = True

s = Settings()

def banner():
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[94m")
    print(r"""
 ██████╗ ██████╗ ██╗   ██╗██╗      █████╗       ████████╗██╗███████╗███████╗    ███████╗██╗     ██╗████████╗███████╗
██╔═══██╗██╔══██╗██║   ██║██║     ██╔══██╗      ╚══██╔══╝██║██╔════╝██╔════╝    ██╔════╝██║     ██║╚══██╔══╝██╔════╝
██║   ██║██████╔╝██║   ██║██║     ███████║ █████╗  ██║   ██║███████╗███████╗    █████╗  ██║     ██║   ██║   █████╗  
██║   ██║██╔═══╝ ██║   ██║██║     ██╔══██║ ╚════╝  ██║   ██║╚════██║╚════██║    ██╔══╝  ██║     ██║   ██║   ██╔══╝  
╚██████╔╝██║     ╚██████╔╝███████╗██║  ██║         ██║   ██║███████║███████║    ███████╗███████╗██║   ██║   ███████╗
 ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═╝         ╚═╝   ╚═╝╚══════╝╚══════╝    ╚══════╝╚══════╝╚═╝   ╚═╝   ╚══════╝
 MADE BY TISSEC                                                                                                   
    """)
    print("\033[0m\n[!] OPULA-TISS ELITE LOADED.")

def aimbot_logic():
    screen_w = win32api.GetSystemMetrics(0)
    screen_h = win32api.GetSystemMetrics(1)
    monitor = {"top": int((screen_h // 2) - 320), "left": int((screen_w // 2) - 320), "width": 640, "height": 640}
    
    cv2_window_active = False

    with mss() as sct:
        while s.running:
            raw_img = sct.grab(monitor)
            frame = np.array(raw_img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            results = model.predict(source=frame, conf=s.conf, device=0, stream=True, verbose=False)

            for r in results:
                if s.show_preview:
                    frame = r.plot()
                
                targets = []
                for box in r.boxes:
                    if int(box.cls[0]) == 0:
                        x, y, w, h = box.xywh[0]
                        head_y = (y - (h / 2)) + (h * s.head_adjust)
                        tx, ty = x.item(), head_y.item()
                        dist = np.sqrt((tx - 320)**2 + (ty - 320)**2)
                        if dist < s.fov:
                            targets.append((dist, tx, ty))

                targets.sort(key=lambda x: x[0])

                if targets and s.aim_enabled:
                    _, target_x, target_y = targets[0]
                    rel_x = (target_x - 320) * s.speed
                    rel_y = (target_y - 320) * s.speed
                    step_x = int(rel_x / s.steps)
                    step_y = int(rel_y / s.steps)

                    for _ in range(s.steps):
                        win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, step_x, step_y, 0, 0)
                        time.sleep(0.001) 

            if s.show_preview:
                cv2.imshow("Opula AI Vision", frame)
                cv2_window_active = True
                if cv2.waitKey(1) & 0xFF == ord('p'): s.running = False
            elif cv2_window_active:
                cv2.destroyAllWindows()
                cv2_window_active = False

class BeautifulGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Opula-Tiss Elite")
        self.root.geometry("340x650")
        self.root.configure(bg="#121212")
        self.root.attributes("-topmost", True)
        
        header = tk.Label(root, text="AI ASSIST CONFIG", bg="#121212", fg="#7289da", font=("Impact", 18))
        header.pack(pady=15)

        def create_slider(parent, label, from_val, to_val, res, default, attr):
            tk.Label(parent, text=label, bg="#121212", fg="white", font=("Arial", 9, "bold")).pack()
            scl = tk.Scale(parent, from_=from_val, to_=to_val, resolution=res, orient="horizontal", 
                           bg="#121212", fg="#7289da", highlightthickness=0, troughcolor="#2c2f33",
                           command=lambda v: setattr(s, attr, float(v) if res < 1 else int(v)))
            scl.set(default)
            scl.pack(pady=(0, 10), fill="x", padx=30)
            return scl

        create_slider(root, "FOV RADIUS", 50, 400, 1, s.fov, 'fov')
        create_slider(root, "SMOOTHING (STEPS)", 1, 20, 1, s.steps, 'steps')
        create_slider(root, "SENSITIVITY (SPEED)", 0.1, 1.0, 0.05, s.speed, 'speed')
        create_slider(root, "AI CONFIDENCE", 0.1, 0.9, 0.05, s.conf, 'conf')
        create_slider(root, "HEAD HEIGHT", 0.0, 0.4, 0.01, s.head_adjust, 'head_adjust')

        self.btn_hide = tk.Button(root, text="HIDE SCREEN CIRCLE", bg="#7289da", fg="white", relief="flat", 
                                    font=("Arial", 10, "bold"), command=self.toggle_overlay)
        self.btn_hide.pack(pady=5, fill="x", padx=40)

        self.btn_toggle = tk.Button(root, text="TOGGLE PREVIEW", bg="#2c2f33", fg="white", relief="flat", 
                                    font=("Arial", 10, "bold"), command=self.toggle_p)
        self.btn_toggle.pack(pady=5, fill="x", padx=40)

        self.btn_quit = tk.Button(root, text="TERMINATE PROGRAM", bg="#f04747", fg="white", relief="flat", 
                                   font=("Arial", 10, "bold"), command=self.quit_all)
        self.btn_quit.pack(pady=10, fill="x", padx=40)

        keyboard.add_hotkey('-', self.toggle_aim)

    def toggle_aim(self):
        s.aim_enabled = not s.aim_enabled

    def toggle_p(self):
        s.show_preview = not s.show_preview

    def toggle_overlay(self):
        s.show_overlay = not s.show_overlay
        btn_text = "SHOW SCREEN CIRCLE" if not s.show_overlay else "HIDE SCREEN CIRCLE"
        self.btn_hide.config(text=btn_text)

    def quit_all(self):
        s.running = False
        self.root.destroy()

def create_overlay():
    overlay = tk.Tk()
    overlay.title("Opula Overlay")
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    
    trans_color = '#000001' 
    overlay.config(bg=trans_color)
    overlay.attributes("-transparentcolor", trans_color)
    
    sw, sh = overlay.winfo_screenwidth(), overlay.winfo_screenheight()
    overlay.geometry(f"{sw}x{sh}+0+0")

    overlay.update()
    hwnd = win32gui.FindWindow(None, "Opula Overlay")
    if hwnd:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, 
                               ex_style | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE)

    canvas = tk.Canvas(overlay, width=sw, height=sh, bg=trans_color, highlightthickness=0)
    canvas.pack()

    def draw():
        canvas.delete("all")
        if s.running:
            if s.show_overlay: # Only draw if enabled
                cx, cy = sw // 2, sh // 2
                color = "#00FF00" if s.aim_enabled else "#FF0000"
                status = "ACTIVE" if s.aim_enabled else "STANDBY"

                canvas.create_oval(cx-s.fov, cy-s.fov, cx+s.fov, cy+s.fov, outline=color, width=2)
                canvas.create_line(cx-5, cy, cx+5, cy, fill=color, width=2)
                canvas.create_line(cx, cy-5, cx, cy+5, fill=color, width=2)
                canvas.create_text(cx, cy + s.fov + 20, text=status, fill=color, font=("Segoe UI", 9, "bold"))
            
            overlay.after(10, draw)
        else:
            overlay.destroy()

    draw()
    overlay.mainloop()

if __name__ == "__main__":
    banner()
    threading.Thread(target=aimbot_logic, daemon=True).start()
    threading.Thread(target=create_overlay, daemon=True).start()

    root = tk.Tk()
    app = BeautifulGUI(root)
    root.mainloop()