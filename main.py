import tkinter as tk
from app.config import settings
from app.enrollment import EnrollmentManager
from app.gui import FaceCheckInApp

def main():
    if settings.MODE == 'enroll':
        print("--- Running in ENROLL mode ---")
        manager = EnrollmentManager()
        manager.enroll_from_folder("data/subjects/[unregistered]")
        print("Enrollment finished. Set Mode to 'camera' in config.ini to run the GUI.")
    elif settings.MODE == 'camera':
        root = tk.Tk()
        app = FaceCheckInApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)
        root.mainloop()
    else:
        print(f"ERROR: Invalid Mode '{settings.MODE}' for GUI application.")
        print("Please set Mode to 'camera' in config.ini to run the GUI, or 'enroll' for batch registration.")

if __name__ == "__main__":
    main()
