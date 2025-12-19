'''
CompreFace Face Recognition Check-in GUI Application

This application provides a graphical user interface for the facial recognition
attendance system using a live camera feed.

Features:
- Live video display from the camera.
- A "Start/Pause" button to control the recognition process.
- A text box to log all successful check-in events.
- All settings are managed through the `config.ini` file.

Prerequisites:
- All prerequisites from the previous script.
- Pillow library: `pip install Pillow`
'''

import os
import cv2
import time
import csv
import requests
import configparser
import threading
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import font as tkFont
from tkinter import scrolledtext
from PIL import Image, ImageTk

# --- Configuration Loading ---
config = configparser.ConfigParser()
if not os.path.exists('config.ini'):
    raise FileNotFoundError("Configuration file 'config.ini' not found.")
config.read('config.ini', encoding='utf-8-sig')

# CompreFace Settings
API_KEY = config.get('CompreFace', 'ApiKey')
BASE_URL = config.get('CompreFace', 'BaseUrl', fallback='http://localhost:8000')

# Script Settings
MODE = config.get('Script', 'Mode', fallback='camera').lower()
THRESHOLD = config.getfloat('Script', 'Threshold', fallback=0.85)
MARGIN = config.getfloat('Script', 'Margin', fallback=0.05)
DEDUPE_SECONDS = config.getint('Script', 'DedupeSeconds', fallback=60)
FRAME_SKIP = config.getint('Script', 'FrameSkip', fallback=10)

# DataSource Settings
CAMERA_INDEX = config.getint('DataSource', 'CameraIndex', fallback=0)
VIDEO_PATH = config.get('DataSource', 'VideoPath')
RTSP_URL = config.get('DataSource', 'RTSP_URL', fallback='')

# GUI Settings
WINDOW_TITLE = config.get('GUI', 'WindowTitle', fallback='CompreFace Check-in System')
DISPLAY_WIDTH = config.getint('GUI', 'DisplayWidth', fallback=960)
DISPLAY_HEIGHT = config.getint('GUI', 'DisplayHeight', fallback=540)

# --- Sanity Checks ---
if not API_KEY or API_KEY == 'YOUR_API_KEY_HERE':
    raise ValueError("ApiKey is not set in config.ini. Please update it.")
HEADERS = {"x-api-key": API_KEY}


# --- Functions from previous script (for enrollment mode) ---
def create_subject(name: str):
    url = f"{BASE_URL}/api/v1/recognition/subjects"
    try:
        response = requests.post(url, headers=HEADERS, json={"subject": name})
        response.raise_for_status()
        print(f"Subject '{name}' created or already exists.")
        return response.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400 and "already exists" in e.response.text:
            print(f"Subject '{name}' already exists, skipping creation.")
        else:
            print(f"Error creating subject '{name}': {e.response.text}")
            raise

def add_face_image(subject: str, image_path: str):
    url = f"{BASE_URL}/api/v1/recognition/faces?subject={subject}"
    print(f"  - Adding sample image '{os.path.basename(image_path)}' to subject '{subject}'...")
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/jpeg")}
        response = requests.post(url, headers=HEADERS, files=files)
        response.raise_for_status()
        return response.json()

def enroll_from_folder(root: str = "data/subjects"):
    if not os.path.isdir(root):
        print(f"Error: Enrollment directory not found at '{root}'")
        return
    for name in os.listdir(root):
        person_dir = os.path.join(root, name)
        if not os.path.isdir(person_dir): continue
        try:
            create_subject(name)
            for img_file in os.listdir(person_dir):
                if img_file.lower().endswith((".jpg", ".jpeg", ".png")):
                    add_face_image(name, os.path.join(person_dir, img_file))
        except Exception as e:
            print(f"Could not process subject '{name}'. Error: {e}")

# --- GUI Application Class (Updated) ---
class FaceCheckInApp:
    def __init__(self, window):
        self.window = window
        self.window.title(WINDOW_TITLE)

         # 允许手动缩放窗口（可选）
        self.window.geometry(f"{DISPLAY_WIDTH}x{DISPLAY_WIDTH}")  # 初始大小可按需调整
        self.window.resizable(True, True)

        self.is_recognizing = False
        self.frame_count = 0
        self.last_seen = {}
        
        # 新增: 用于线程安全地写入CSV文件
        self.csv_lock = threading.Lock()
        self.csv_path = config.get('Script', 'CsvPath', fallback='live_attendance.csv')

        # --- Video Panel ---
        self.video_label = tk.Label(window)
        self.video_label.pack(padx=10, pady=10)

        # --- Video Panel 容器 ---
        self.video_frame = tk.Frame(
            window,
            width=DISPLAY_WIDTH,
            height=DISPLAY_HEIGHT,
            bg="black"
        )
        self.video_frame.pack(padx=10, pady=10)
        # 关键：不要让 Frame 根据内部内容自动调整大小
        self.video_frame.pack_propagate(False)

        self.video_label = tk.Label(self.video_frame, bg="black")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        # --- Controls Panel ---
        controls_frame = tk.Frame(window)
        controls_frame.pack(fill=tk.X, padx=10, pady=5)

        self.toggle_button = tk.Button(controls_frame, text="Start Recognition", command=self.toggle_recognition, width=20)
        self.toggle_button.pack(side=tk.LEFT, expand=True)

        # --- Log Panel ---
        log_font = tkFont.Font(family="Courier New", size=10)
        self.log_text = scrolledtext.ScrolledText(window, height=10, font=log_font, state=tk.DISABLED)
        self.log_text.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

        # --- Initialize CSV file ---
        self.init_csv()

        # --- Initialize Video Capture ---
        if RTSP_URL:
            self.log_message(f"Connecting to RTSP stream at {RTSP_URL}...")
            self.cap = cv2.VideoCapture(RTSP_URL)
            if not self.cap.isOpened():
                self.log_message(f"Error: Could not open RTSP stream at {RTSP_URL}")
                return
            else:
                self.log_message("RTSP stream opened successfully.")
        else:
            self.cap = cv2.VideoCapture(CAMERA_INDEX)
            if not self.cap.isOpened():
                self.log_message(f"Error: Could not open camera at index {CAMERA_INDEX}")
                return

        self.update_frame()
        
    def init_csv(self):
        """Creates the CSV file and writes the header if it doesn't exist."""
        with self.csv_lock:
            # 检查文件是否存在且非空
            file_exists = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0
            if not file_exists:
                try:
                    with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Timestamp", "Subject", "Similarity"])
                    self.log_message(f"CSV file created at '{self.csv_path}'")
                except IOError as e:
                    self.log_message(f"Error creating CSV file: {e}")

    def write_to_csv(self, record):
        """Appends a record to the CSV file in a thread-safe manner."""
        with self.csv_lock:
            try:
                with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(record)
            except IOError as e:
                self.log_message(f"Error writing to CSV: {e}")


    def toggle_recognition(self):
        self.is_recognizing = not self.is_recognizing
        if self.is_recognizing:
            self.toggle_button.config(text="Pause Recognition")
            self.log_message("Recognition started...")
        else:
            self.toggle_button.config(text="Start Recognition")
            self.log_message("Recognition paused.")

    def log_message(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def recognize_frame_threaded(self, frame):
        """
        This function runs in a separate thread to perform recognition.
        This final version explicitly catches and ignores the expected 400 error
        when no faces are found in the frame.
        """
        try:
            url = f"{BASE_URL}/api/v1/recognition/recognize"
            _, buf = cv2.imencode(".jpg", frame)
            files = {"file": ("frame.jpg", buf.tobytes(), "image/jpeg")}
            
            response = requests.post(url, headers=HEADERS, files=files, timeout=10)
            
            # 检查响应状态码，如果不成功，则引发HTTPError
            response.raise_for_status() 
            recognition_result = response.json()

            for item in recognition_result.get("result", []):
                subjects = item.get("subjects", [])
                if not subjects:
                    continue

                best = subjects[0]  # 最相似
                second = subjects[1] if len(subjects) > 1 else None

                best_sub = best.get("subject")
                best_sim = best.get("similarity", 0.0)
                second_sim = second.get("similarity", 0.0) if second else 0.0

                # 只有当：
                # 1) best_sim 足够高
                # 2) 并且比第二名至少高 MARGIN
                # 才认为是可信匹配
                if best_sub and best_sim >= THRESHOLD and (best_sim - second_sim) >= MARGIN:
                    # 再进入您现有的时间窗去重 + 写 CSV 逻辑
                    now = datetime.now()
                    if best_sub not in self.last_seen or (now - self.last_seen[best_sub]) > timedelta(seconds=DEDUPE_SECONDS):
                        timestamp_str = now.isoformat(timespec="seconds")
                        log_msg = f"[CHECK-IN] {timestamp_str} - {best_sub} (Similarity: {best_sim:.3f})"
                        csv_record = [timestamp_str, best_sub, f"{best_sim:.3f}"]
                        
                        self.window.after(0, self.log_message, log_msg)
                        self.write_to_csv(csv_record)
                        self.last_seen[best_sub] = now
                else:
                    # 视为 unknown/不可靠，不记签到
                    continue
                
                # for candidate in item.get("subjects", []):
                #     subject = candidate.get("subject")
                #     similarity = candidate.get("similarity", 0.0)
                    
                #     if subject and similarity >= THRESHOLD:
                #         now = datetime.now()
                #         if subject not in self.last_seen or (now - self.last_seen[subject]) > timedelta(seconds=DEDUPE_SECONDS):
                #             timestamp_str = now.isoformat(timespec="seconds")
                #             log_msg = f"[CHECK-IN] {timestamp_str} - {subject} (Similarity: {similarity:.3f})"
                #             csv_record = [timestamp_str, subject, f"{similarity:.3f}"]
                            
                #             self.window.after(0, self.log_message, log_msg)
                #             self.write_to_csv(csv_record)
                #             self.last_seen[subject] = now

        except requests.exceptions.HTTPError as e:
            # --- 关键修改在这里 ---
            # 我们捕获所有 HTTP 错误，然后判断它是不是我们想忽略的那一个。
            # 检查状态码是否为 400，并且响应内容中是否包含 "face is not found"
            if e.response.status_code == 400:
                try:
                    error_json = e.response.json()
                    # CompreFace 在找不到人脸时会返回 code 28
                    if error_json.get("code") == 28 or "face is not found" in error_json.get("message", "").lower():
                        pass  # 静默忽略，这是预期的行为
                    else:
                        # 如果是其他类型的 400 错误，则打印
                        self.window.after(0, self.log_message, f"Error: An unexpected 400 error occurred: {error_json.get('message')}")
                except ValueError: # 如果响应不是有效的JSON
                     self.window.after(0, self.log_message, f"Error: An unhandled 400 error occurred.")
            else:
                # 对于所有其他非 400 的 HTTP 错误 (如 500, 401, 404), 我们依然打印出来
                self.window.after(0, self.log_message, f"Error: An HTTP error occurred: {e}")
                
        except requests.exceptions.RequestException as e:
            # 对于网络层面的错误 (如超时, DNS解析失败)
            self.window.after(0, self.log_message, f"Error: A network error occurred: {e}")

    def update_frame(self):
        ok, frame = self.cap.read()
        if ok:
            if self.is_recognizing:
                self.frame_count += 1
                if self.frame_count % FRAME_SKIP == 0:
                    threading.Thread(target=self.recognize_frame_threaded, 
                                     args=(frame.copy(),), 
                                     daemon=True).start()

            cv2image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(cv2image)

            # 按比例缩放到不超过 DISPLAY_* 的大小
            img_w, img_h = img.size
            scale = min(DISPLAY_WIDTH / img_w, DISPLAY_HEIGHT / img_h)
            new_w = int(img_w * scale)
            new_h = int(img_h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk
            self.video_label.configure(image=imgtk)
        
        self.window.after(15, self.update_frame)

    def on_closing(self):
        if self.cap.isOpened():
            self.cap.release()
        self.window.destroy()

if __name__ == "__main__":
    if MODE == 'enroll':
        print("--- Running in ENROLL mode ---")
        enroll_from_folder("data/subjects")
        print("Enrollment finished. Set Mode to 'camera' in config.ini to run the GUI.")
    elif MODE == 'camera':
        root = tk.Tk()
        app = FaceCheckInApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)
        root.mainloop()
    else:
        print(f"ERROR: Invalid Mode '{MODE}' for GUI application.")
        print("Please set Mode to 'camera' in config.ini to run the GUI, or 'enroll' for batch registration.")
