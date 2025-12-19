import os
from .api import CompreFaceClient

class EnrollmentManager:
    def __init__(self):
        self.client = CompreFaceClient()

    def enroll_from_folder(self, root: str = "data/subjects"):
        if not os.path.isdir(root):
            print(f"Error: Enrollment directory not found at '{root}'")
            return
        
        print(f"Starting enrollment from '{root}'...")
        for name in os.listdir(root):
            person_dir = os.path.join(root, name)
            if not os.path.isdir(person_dir): continue
            try:
                self.client.create_subject(name)
                for img_file in os.listdir(person_dir):
                    if img_file.lower().endswith((".jpg", ".jpeg", ".png")):
                        self.client.add_face_image(name, os.path.join(person_dir, img_file))
            except Exception as e:
                print(f"Could not process subject '{name}'. Error: {e}")
