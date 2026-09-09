import os
import json
import subprocess
from typing import Dict, Any, Optional
from config import FFMPEG_PATH, FFPROBE_PATH

def get_media_info(file_path: str, ffprobe_bin: Optional[str] = None) -> Dict[str, Any]:
    """
    Extract video/audio stream metadata (duration, dimensions, fps, bitrate, audio tracks)
    using ffprobe.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    probe_bin = ffprobe_bin or FFPROBE_PATH or "ffprobe"
    cmd = [
        probe_bin,
        "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels",
        "-of", "json",
        file_path
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        info = json.loads(res.stdout)
    except Exception as e:
        # Fallback basic stats
        return {
            "duration": 0.0,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "hasAudio": True,
            "hasVideo": True,
            "size": os.path.getsize(file_path),
            "filename": os.path.basename(file_path),
            "error": str(e)
        }

    streams = info.get("streams", [])
    format_info = info.get("format", {})

    duration = float(format_info.get("duration") or 0.0)
    size = int(format_info.get("size") or os.path.getsize(file_path))
    bitrate = int(format_info.get("bit_rate") or 0)

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = int(video_stream.get("width") or 1920) if video_stream else 0
    height = int(video_stream.get("height") or 1080) if video_stream else 0
    
    fps = 30.0
    if video_stream:
        r_fps = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate") or "30/1"
        try:
            if "/" in r_fps:
                num, den = r_fps.split("/")
                fps = round(float(num) / float(den), 2) if float(den) > 0 else 30.0
            else:
                fps = float(r_fps)
        except Exception:
            fps = 30.0

    return {
        "filename": os.path.basename(file_path),
        "filepath": os.path.abspath(file_path),
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "size": size,
        "bitrate": bitrate,
        "videoCodec": video_stream.get("codec_name") if video_stream else None,
        "audioCodec": audio_stream.get("codec_name") if audio_stream else None,
        "hasVideo": video_stream is not None,
        "hasAudio": audio_stream is not None,
        "sampleRate": int(audio_stream.get("sample_rate") or 0) if audio_stream else 0,
        "channels": int(audio_stream.get("channels") or 0) if audio_stream else 0
    }

def get_audio_duration(audio_path: str, ffmpeg_bin: Optional[str] = None) -> float:
    """Get accurate duration of an audio file in seconds."""
    if not os.path.exists(audio_path):
        return 0.0
    try:
        info = get_media_info(audio_path)
        return float(info.get("duration", 0.0))
    except Exception:
        # Fallback reading WAV header or estimate from size
        if audio_path.endswith(".wav"):
            try:
                import wave
                with wave.open(audio_path, 'r') as w:
                    frames = w.getnframes()
                    rate = w.getframerate()
                    return frames / float(rate)
            except Exception:
                pass
        return 0.0

def pick_media_file_dialog() -> Optional[str]:
    """
    Open native Windows File Dialog to select video/audio file directly
    from the computer without HTTP upload.
    Uses Python Tkinter in a standalone subprocess with topmost attribute,
    with PowerShell OpenFileDialog fallback.
    """
    import sys

    # 1. Standalone Python Tkinter subprocess with topmost window
    tk_script = '''
import sys
import tkinter as tk
from tkinter import filedialog

try:
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", 1)
    root.focus_force()
    file_path = filedialog.askopenfilename(
        parent=root,
        title="Chọn video hoặc âm thanh từ máy tính",
        filetypes=[
            ("Tất cả tệp video & âm thanh", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.ts *.m4v *.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
            ("Tệp Video (*.mp4, *.mkv, *.mov...)", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.ts *.m4v"),
            ("Tệp Âm thanh (*.mp3, *.wav...)", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
            ("Tất cả tệp (*.*)", "*.*")
        ]
    )
    root.destroy()
    if file_path:
        print(file_path)
except Exception:
    sys.exit(1)
'''
    try:
        res = subprocess.run(
            [sys.executable, "-c", tk_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180
        )
        if res.returncode == 0:
            selected = res.stdout.strip()
            if selected and os.path.exists(selected):
                return os.path.normpath(selected)
            elif not selected:
                return None  # User canceled dialog
    except Exception:
        pass

    # 2. PowerShell OpenFileDialog fallback
    ps_cmd = '''
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Filter = "Media Files (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.flv;*.wmv;*.ts;*.mp3;*.wav;*.m4a)|*.mp4;*.mkv;*.mov;*.avi;*.webm;*.flv;*.wmv;*.ts;*.mp3;*.wav;*.m4a|All Files (*.*)|*.*"
$d.Title = "Chọn video từ máy tính"
$d.RestoreDirectory = $true
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $d.FileName
}
'''
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180
        )
        if res.returncode == 0:
            selected = res.stdout.strip()
            if selected and os.path.exists(selected):
                return os.path.normpath(selected)
    except Exception:
        pass

    return None

