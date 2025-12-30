import requests
import os
from .config import settings

class CompreFaceClient:
    def __init__(self):
        self.base_url = settings.BASE_URL
        self.headers = {"x-api-key": settings.API_KEY}

    def create_subject(self, name: str):
        url = f"{self.base_url}/api/v1/recognition/subjects"
        try:
            response = requests.post(url, headers=self.headers, json={"subject": name})
            response.raise_for_status()
            print(f"Subject '{name}' created or already exists.")
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400 and "already exists" in e.response.text:
                print(f"Subject '{name}' already exists, skipping creation.")
            else:
                print(f"Error creating subject '{name}': {e.response.text}")
                raise

    def add_face_image(self, subject: str, image_path: str):
        url = f"{self.base_url}/api/v1/recognition/faces?subject={subject}"
        print(f"  - Adding sample image '{os.path.basename(image_path)}' to subject '{subject}'...")
        with open(image_path, "rb") as f:
            files = {"file": (os.path.basename(image_path), f, "image/jpeg")}
            response = requests.post(url, headers=self.headers, files=files)
            response.raise_for_status()
            return response.json()

    def recognize_image(self, image_bytes):
        url = f"{self.base_url}/api/v1/recognition/recognize"
        files = {"file": ("frame.jpg", image_bytes, "image/jpeg")}
        # Timeout set to 20 seconds as in original script
        response = requests.post(url, 
                                 headers=self.headers, 
                                 files=files,
                                 params={"prediction_count": settings.PREDICTION_COUNT}, 
                                 timeout=20)
        response.raise_for_status()
        return response.json()
