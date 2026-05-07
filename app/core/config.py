from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Church Auto Check-in API")
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    sqlite_path: str = os.getenv("SQLITE_PATH", "./data/checkin.db")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///./data/checkin.db")
    compre_face_base_url: str = os.getenv("COMPRE_FACE_BASE_URL", "http://localhost:8000")
    face_storage_dir: str = os.getenv("FACE_STORAGE_DIR", "./data/face_gallery")
    app_timezone: str = os.getenv("APP_TIMEZONE", "Europe/Rome")
    debug_basic_user: str = os.getenv("DEBUG_BASIC_USER", "tech")
    debug_basic_password: str = os.getenv("DEBUG_BASIC_PASSWORD", "9090")


settings = Settings()
