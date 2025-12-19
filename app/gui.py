import os
import queue
import threading
import time
import cv2
import tkinter as tk
from tkinter import font as tkFont
from tkinter import scrolledtext
from PIL import Image, ImageTk
from datetime import datetime, timedelta
import requests

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

from .config import settings
from .api import CompreFaceClient
from .storage import AttendanceLogger


class CameraWorker:
    def __init__(self, source, max_queue_size=3, force_tcp=False):
        self.source = source
        self.max_queue_size = max_queue_size or 3
        self.force_tcp = force_tcp
        self.q = queue.Queue(maxsize=self.max_queue_size)
        self.cap = None
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
        self._release_capture()

    def read_nowait(self):
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def ensure_capture(self):
        return self._ensure_capture()

    def _run(self):
        while self.running:
            if not self._ensure_capture():
                time.sleep(0.5)
                continue
            try:
                grabbed = self.cap.grab()
                if not grabbed:
                    time.sleep(0.02)
                    continue
                if self.q.full():
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                ok, frame = self.cap.retrieve()
                if ok:
                    self.q.put(frame)
                else:
                    time.sleep(0.02)
            except Exception:
                time.sleep(0.25)
                self._release_capture()

    def _ensure_capture(self):
        if self.cap and self.cap.isOpened():
            return True
        self._release_capture()
        self.cap = self._open_capture()
        return self.cap is not None and self.cap.isOpened()

    def _open_capture(self):
        backend = cv2.CAP_ANY
        is_rtsp = isinstance(self.source, str) and self.source.lower().startswith("rtsp")
        if is_rtsp:
            backend = cv2.CAP_FFMPEG
            if self.force_tcp:
                os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        try:
            if backend == cv2.CAP_ANY:
                cap = cv2.VideoCapture(self.source)
            else:
                cap = cv2.VideoCapture(self.source, backend)
        except Exception:
            cap = None
        if cap and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        return cap

    def _release_capture(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

class FaceCheckInApp:
    def __init__(self, window):
        self.window = window
        self.window.title(settings.WINDOW_TITLE)

        # Allow manual resizing
        self.window.geometry(f"{settings.DISPLAY_WIDTH}x{settings.DISPLAY_HEIGHT}")
        self.window.resizable(True, True)

        self.is_recognizing = False
        self.frame_count = 0
        self.last_seen = {}
        
        self.api_client = CompreFaceClient()
        self.recognition_sem = threading.Semaphore(1)
        self.logger = AttendanceLogger()

        # --- Video Panel ---
        # Container for video
        self.video_frame = tk.Frame(
            window,
            width=settings.DISPLAY_WIDTH,
            height=settings.DISPLAY_HEIGHT,
            bg="black"
        )
        self.video_frame.pack(padx=10, pady=10)
        # Prevent frame from shrinking to fit content
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

        # --- Initialize Video Capture ---
        self.preview_enabled = getattr(settings, "PREVIEW", True)
        self._preview_label_state = None
        force_tcp = bool(settings.RTSP_URL and getattr(settings, "RTSP_TCP", True))

        if settings.RTSP_URL:
            capture_source = settings.RTSP_URL
            source_description = f"RTSP stream {settings.RTSP_URL}"
        elif settings.MODE == 'video' and settings.VIDEO_PATH:
            capture_source = settings.VIDEO_PATH
            source_description = f"video file {settings.VIDEO_PATH}"
        else:
            capture_source = settings.CAMERA_INDEX
            source_description = f"camera index {settings.CAMERA_INDEX}"

        self.log_message(f"Connecting to {source_description}...")
        self.cam_worker = CameraWorker(
            capture_source,
            max_queue_size=getattr(settings, "MAX_QUEUE_SIZE", 3),
            force_tcp=force_tcp,
        )
        if self.cam_worker.ensure_capture():
            self.log_message("Video source opened successfully.")
        else:
            self.log_message("Video source not ready yet, retrying in background...")
        self.cam_worker.start()
        self.update_frame()

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
        """
        try:
            _, buf = cv2.imencode(".jpg", frame)
            recognition_result = self.api_client.recognize_image(buf.tobytes())

            for item in recognition_result.get("result", []):
                subjects = item.get("subjects", [])
                if not subjects:
                    continue

                best = subjects[0]  # Most similar
                second = subjects[1] if len(subjects) > 1 else None

                best_sub = best.get("subject")
                best_sim = best.get("similarity", 0.0)
                second_sim = second.get("similarity", 0.0) if second else 0.0

                # Only consider valid match if:
                # 1) best_sim is high enough
                # 2) AND it is higher than the second best by at least MARGIN
                if best_sub and best_sim >= settings.THRESHOLD and (best_sim - second_sim) >= settings.MARGIN:
                    # Deduplication logic
                    now = datetime.now()
                    if best_sub not in self.last_seen or (now - self.last_seen[best_sub]) > timedelta(seconds=settings.DEDUPE_SECONDS):
                        timestamp_str = now.isoformat(timespec="seconds")
                        log_msg = f"[CHECK-IN] {timestamp_str} - {best_sub} (Similarity: {best_sim:.3f})"
                        
                        self.window.after(0, self.log_message, log_msg)
                        self.logger.write_record(timestamp_str, best_sub, f"{best_sim:.3f}")
                        self.last_seen[best_sub] = now
                else:
                    # Unknown or unreliable match
                    continue

        except requests.exceptions.HTTPError as e:
            # Handle 400 errors specifically for "face not found"
            if e.response.status_code == 400:
                try:
                    error_json = e.response.json()
                    # CompreFace returns code 28 when no face is found
                    if error_json.get("code") == 28 or "face is not found" in error_json.get("message", "").lower():
                        pass  # Silently ignore
                    else:
                        self.window.after(0, self.log_message, f"Error: An unexpected 400 error occurred: {error_json.get('message')}")
                except ValueError:
                     self.window.after(0, self.log_message, f"Error: An unhandled 400 error occurred.")
            else:
                self.window.after(0, self.log_message, f"Error: An HTTP error occurred: {e}")
                
        except requests.exceptions.RequestException as e:
            self.window.after(0, self.log_message, f"Error: A network error occurred: {e}")

        finally:
            try:
                self.recognition_sem.release()
            except ValueError as e:
                # 如果出现重复 release（理论上不应发生），避免线程崩掉
                pass
    
    def update_frame(self):
        frame = None
        if hasattr(self, "cam_worker") and self.cam_worker:
            frame = self.cam_worker.read_nowait()

        if frame is not None:
            if self.is_recognizing:
                self.frame_count += 1
                if self.frame_count % max(1, settings.FRAME_SKIP) == 0:
                    aquired = self.recognition_sem.acquire(blocking=False)
                    if aquired:
                        threading.Thread(
                            target=self.recognize_frame_threaded,
                            args=(frame.copy(),),
                            daemon=True,
                        ).start()

            if self.preview_enabled:
                self._render_preview(frame)
            else:
                self._show_preview_disabled()
        elif not self.preview_enabled:
            self._show_preview_disabled()

        self.window.after(30, self.update_frame)

    def _render_preview(self, frame):
        cv2image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(cv2image)

        img_w, img_h = img.size
        scale = min(settings.DISPLAY_WIDTH / img_w, settings.DISPLAY_HEIGHT / img_h)
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        imgtk = ImageTk.PhotoImage(image=img)
        self.video_label.imgtk = imgtk
        self.video_label.configure(image=imgtk, text="", bg="black", fg="white")
        self._preview_label_state = "video"

    def _show_preview_disabled(self):
        if self._preview_label_state == "disabled":
            return
        self.video_label.configure(
            image="",
            text="Preview disabled",
            fg="white",
            bg="black",
        )
        self.video_label.imgtk = None
        self._preview_label_state = "disabled"

    def on_closing(self):
        if hasattr(self, "cam_worker") and self.cam_worker:
            self.cam_worker.stop()
        self.window.destroy()
