import os
import configparser

class Config:
    def __init__(self, config_file='config.ini'):
        self.config = configparser.ConfigParser()
        if not os.path.exists(config_file):
            # Try looking in parent directory if running from app/
            if os.path.exists(os.path.join('..', config_file)):
                config_file = os.path.join('..', config_file)
            elif not os.path.exists(config_file):
                raise FileNotFoundError(f"Configuration file '{config_file}' not found.")
        
        self.config.read(config_file, encoding='utf-8-sig')

        # CompreFace Settings
        self.API_KEY = self.config.get('CompreFace', 'ApiKey')
        self.BASE_URL = self.config.get('CompreFace', 'BaseUrl', fallback='http://localhost:8000')

        # Script Settings
        self.MODE = self.config.get('Script', 'Mode', fallback='camera').lower()
        self.THRESHOLD = self.config.getfloat('Script', 'Threshold', fallback=0.85)
        self.MARGIN = self.config.getfloat('Script', 'Margin', fallback=0.05)
        self.DEDUPE_SECONDS = self.config.getint('Script', 'DedupeSeconds', fallback=60)
        self.FRAME_SKIP = self.config.getint('Script', 'FrameSkip', fallback=10)
        self.MAX_QUEUE_SIZE = self.config.getint('Script', 'MaxQueueSize', fallback=3)
        self.PREDICTION_COUNT = self.config.getint('Script', 'PredictionCount', fallback=5)

        # DataSource Settings
        self.CAMERA_INDEX = self.config.getint('DataSource', 'CameraIndex', fallback=0)
        self.VIDEO_PATH = self.config.get('DataSource', 'VideoPath', fallback='')
        self.RTSP_URL = self.config.get('DataSource', 'RTSP_URL', fallback='')
        self.RTSP_TCP = self.config.getboolean('DataSource', 'RtspTcp', fallback=True)
        # Corrected to read from DataSource section as per config.ini structure
        self.CAMERA_CSV_PATH = self.config.get('DataSource', 'CameraCsvPath', fallback='live_attendance.csv')

        # GUI Settings
        self.WINDOW_TITLE = self.config.get('GUI', 'WindowTitle', fallback='CompreFace Check-in System')
        self.DISPLAY_WIDTH = self.config.getint('GUI', 'DisplayWidth', fallback=960)
        self.DISPLAY_HEIGHT = self.config.getint('GUI', 'DisplayHeight', fallback=540)
        self.PREVIEW = self.config.getboolean('GUI', 'Preview', fallback=True)

        # Sanity Checks
        if not self.API_KEY or self.API_KEY == 'YOUR_API_KEY_HERE':
            raise ValueError("ApiKey is not set in config.ini. Please update it.")

# Create a global instance
settings = Config()
