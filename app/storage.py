import csv
import os
import threading
from .config import settings

class AttendanceLogger:
    def __init__(self):
        self.csv_lock = threading.Lock()
        self.csv_path = settings.CAMERA_CSV_PATH
        self.init_csv()

    def init_csv(self):
        """Creates the CSV file and writes the header if it doesn't exist."""
        with self.csv_lock:
            # Check if file exists and is not empty
            file_exists = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0
            if not file_exists:
                try:
                    with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow(["Timestamp", "Subject", "Similarity"])
                    print(f"CSV file created at '{self.csv_path}'")
                except IOError as e:
                    print(f"Error creating CSV file: {e}")

    def write_record(self, timestamp, subject, similarity):
        """Appends a record to the CSV file in a thread-safe manner."""
        with self.csv_lock:
            try:
                with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([timestamp, subject, similarity])
            except IOError as e:
                print(f"Error writing to CSV: {e}")
