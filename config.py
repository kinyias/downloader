import os
import shutil
from pathlib import Path

# Base Paths
BACKEND_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = WORKSPACE_ROOT / "frontend"

UPLOAD_DIR = BACKEND_DIR / "uploads"
TEMP_DIR = BACKEND_DIR / "temp"
EXPORT_DIR = BACKEND_DIR / "exports"

# Create directories if they don't exist
for folder in [UPLOAD_DIR, TEMP_DIR, EXPORT_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# Detect FFmpeg and FFprobe binary
DEFAULT_FFMPEG_CANDIDATES = [
    r"D:\Delete\del\tools\ffmpeg\ffmpeg.exe",
    shutil.which("ffmpeg"),
    "ffmpeg"
]
DEFAULT_FFPROBE_CANDIDATES = [
    r"D:\Delete\del\tools\ffmpeg\ffprobe.exe",
    shutil.which("ffprobe"),
    "ffprobe"
]

FFMPEG_PATH = next((p for p in DEFAULT_FFMPEG_CANDIDATES if p and (os.path.exists(p) or p == "ffmpeg")), "ffmpeg")
FFPROBE_PATH = next((p for p in DEFAULT_FFPROBE_CANDIDATES if p and (os.path.exists(p) or p == "ffprobe")), "ffprobe")

# Default settings
DEFAULT_SETTINGS = {
    "ffmpegPath": FFMPEG_PATH,
    "ffprobePath": FFPROBE_PATH,
    "groqApiKey": os.getenv("GROQ_API_KEY", ""),
    "deepseekApiKey": os.getenv("DEEPSEEK_API_KEY", ""),
    "openaiApiKey": os.getenv("OPENAI_API_KEY", ""),
    "customApiEndpoint": os.getenv("CUSTOM_API_ENDPOINT", "http://localhost:20128/v1"),
    "customApiKey": os.getenv("CUSTOM_API_KEY", "sk-0d1d295eb7ccebbd-2qy7kh-e2934c49"),
    "customModel": "gemini-lite",
    "translateTargetLang": "vi",
    "defaultVoice": "vi-VN-HoaiMyNeural",
    "defaultSpeed": 1.0,
    "exportPreset": "veryfast",
    "exportCrf": 22
}
