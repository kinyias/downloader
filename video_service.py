"""
Video Processing Service using FFmpeg & FFprobe
Features:
  - Fast metadata probing with ffprobe
  - Directory video scanning with natural sorting (tap 1, tap 2... tap 10, tap 11)
  - Optimal video concatenation with optional end-cutting (trimming end seconds)
  - Stream copy fast-path when no trimming/re-encoding is required
  - Single-pass filter_complex concatenation with A/V sync preservation
  - Multi-threaded background execution with real-time progress parsing (0-100%, speed, ETA)
  - Safe task cancellation support
"""

from collections import Counter
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def get_runtime_base_dir() -> Path:
    """Return base directory of the running exe or script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_ffmpeg_binary() -> str:
    """Resolve ffmpeg binary path."""
    runtime_dir = get_runtime_base_dir()
    candidates = [
        Path("/usr/local/bin/ffmpeg"),
        Path("/usr/bin/ffmpeg"),
        Path("/tmp/colab-ffmpeg-cuda/bin/ffmpeg"),
        runtime_dir / "ffmpeg.exe",
        runtime_dir / "ffmpeg",
        runtime_dir / "bin" / "ffmpeg.exe",
        runtime_dir / "bin" / "ffmpeg",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "ffmpeg.exe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "ffmpeg",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "bin" / "ffmpeg.exe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "bin" / "ffmpeg",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    which_bin = shutil.which("ffmpeg")
    if which_bin:
        return which_bin
    return os.getenv("FFMPEG_BIN", "ffmpeg")


def get_ffprobe_binary() -> str:
    """Resolve ffprobe binary path."""
    runtime_dir = get_runtime_base_dir()
    candidates = [
        Path("/usr/local/bin/ffprobe"),
        Path("/usr/bin/ffprobe"),
        Path("/tmp/colab-ffmpeg-cuda/bin/ffprobe"),
        runtime_dir / "ffprobe.exe",
        runtime_dir / "ffprobe",
        runtime_dir / "bin" / "ffprobe.exe",
        runtime_dir / "bin" / "ffprobe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "ffprobe.exe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "ffprobe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "bin" / "ffprobe.exe",
        Path(getattr(sys, "_MEIPASS", runtime_dir)) / "bin" / "ffprobe",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    which_bin = shutil.which("ffprobe")
    if which_bin:
        return which_bin
    return os.getenv("FFPROBE_BIN", "ffprobe")


def format_duration(seconds: float) -> str:
    """Format duration in seconds into MM:SS or HH:MM:SS string."""
    if not seconds or seconds < 0:
        return "00:00"
    total_sec = int(round(seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_size(bytes_num: int) -> str:
    """Format size in bytes to human-readable string (KB, MB, GB)."""
    if not bytes_num or bytes_num <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_num < 1024.0:
            return f"{bytes_num:.1f} {unit}" if unit != "B" else f"{bytes_num} B"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} PB"


def _safe_log(msg: str) -> None:
    """Safely log a message without clashing with active tqdm progress bars."""
    try:
        from tqdm import tqdm
        tqdm.write(str(msg))
    except Exception:
        print(str(msg), flush=True)


def natural_sort_key(s: str) -> list:
    """Natural sorting key: e.g. tap_1, tap_2, ..., tap_10, tap_11."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]


def get_filter_complex_file_flag(ffmpeg_bin: str) -> str:
    """Return '-filter_complex_script' for universal compatibility across all FFmpeg versions on Windows, Linux, and Colab."""
    return "-filter_complex_script"


def probe_video_info(file_path: Path) -> Dict[str, Any]:
    """Probe video file metadata using ffprobe."""
    p = Path(file_path).resolve()
    if not p.exists() or not p.is_file():
        return {
            "error": "File không tồn tại",
            "is_valid": False,
            "exists": False,
            "filename": p.name,
            "path": str(p),
            "size": 0,
            "size_str": "0 B",
            "duration": 0.0,
            "duration_str": "00:00",
            "resolution": "Unknown",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "video_codec": "",
            "audio_codec": "",
            "bitrate": 0,
            "has_audio": False,
        }

    file_size = p.stat().st_size
    if file_size == 0:
        return {
            "error": "File 0-byte (file rỗng / chưa tải hoàn tất)",
            "is_valid": False,
            "exists": True,
            "filename": p.name,
            "path": str(p),
            "size": 0,
            "size_str": "0 B",
            "duration": 0.0,
            "duration_str": "00:00",
            "resolution": "Unknown",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "video_codec": "",
            "audio_codec": "",
            "bitrate": 0,
            "has_audio": False,
        }

    ffprobe_bin = get_ffprobe_binary()
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]

    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
            startupinfo=startupinfo,
        )

        stderr_msg = (proc.stderr or "").strip()
        if proc.returncode != 0 or not proc.stdout.strip():
            err_desc = "Lỗi đọc định dạng video"
            if "moov atom not found" in stderr_msg or "moov atom" in stderr_msg:
                err_desc = "Lỗi file MP4 bị hỏng hoặc chưa tải hoàn tất (moov atom not found)"
            elif stderr_msg:
                err_desc = f"FFprobe lỗi: {stderr_msg[:120]}"

            return {
                "error": err_desc,
                "is_valid": False,
                "exists": True,
                "filename": p.name,
                "path": str(p),
                "size": file_size,
                "size_str": format_size(file_size),
                "duration": 0.0,
                "duration_str": "00:00",
                "resolution": "Unknown",
                "width": 0,
                "height": 0,
                "fps": 0.0,
                "video_codec": "",
                "audio_codec": "",
                "bitrate": 0,
                "has_audio": False,
            }

        data = json.loads(proc.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        if not video_stream:
            return {
                "error": "Không tìm thấy luồng video (file không chứa dữ liệu hình ảnh)",
                "is_valid": False,
                "exists": True,
                "filename": p.name,
                "path": str(p),
                "size": file_size,
                "size_str": format_size(file_size),
                "duration": 0.0,
                "duration_str": "00:00",
                "resolution": "Unknown",
                "width": 0,
                "height": 0,
                "fps": 0.0,
                "video_codec": "",
                "audio_codec": "",
                "bitrate": 0,
                "has_audio": False,
            }

        duration = float(fmt.get("duration") or (video_stream.get("duration") if video_stream else 0) or 0)
        width = int(video_stream.get("width") or 0) if video_stream else 0
        height = int(video_stream.get("height") or 0) if video_stream else 0
        video_codec = (video_stream.get("codec_name") or "") if video_stream else ""
        audio_codec = (audio_stream.get("codec_name") or "") if audio_stream else ""

        # Parse FPS
        fps = 0.0
        if video_stream:
            r_fps = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate") or ""
            if "/" in r_fps:
                try:
                    num, den = r_fps.split("/", 1)
                    if float(den) > 0:
                        fps = round(float(num) / float(den), 2)
                except Exception:
                    pass
            elif r_fps:
                try:
                    fps = round(float(r_fps), 2)
                except Exception:
                    pass

        bitrate = int(fmt.get("bit_rate") or (video_stream.get("bit_rate") if video_stream else 0) or 0)
        resolution = f"{width}x{height}" if width and height else "Unknown"

        is_valid = duration > 0 and width > 0 and height > 0

        return {
            "is_valid": is_valid,
            "exists": True,
            "filename": p.name,
            "path": str(p),
            "size": file_size,
            "size_str": format_size(file_size),
            "duration": round(duration, 3),
            "duration_str": format_duration(duration),
            "resolution": resolution,
            "width": width,
            "height": height,
            "fps": fps,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "bitrate": bitrate,
            "has_audio": audio_stream is not None,
            "error": "" if is_valid else "Thông số video không hợp lệ (thời lượng hoặc kích thước bằng 0)",
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "is_valid": False,
            "exists": True,
            "filename": p.name,
            "path": str(p),
            "size": file_size,
            "size_str": format_size(file_size),
            "duration": 0.0,
            "duration_str": "00:00",
            "resolution": "Unknown",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "video_codec": "",
            "audio_codec": "",
            "bitrate": 0,
            "has_audio": False,
        }


# ───────────────────────── GPU Hardware Acceleration (Auto Detection & Fallback) ─────────────────────────

_DETECTED_GPU_ENCODERS: Optional[Dict[str, Any]] = None


def get_nvidia_gpu_model_name() -> str:
    """Get the specific NVIDIA GPU name (e.g. Tesla T4, A100-SXM4-40GB, RTX 3060) via nvidia-smi."""
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            startupinfo=startupinfo,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return ""


def ensure_cuda_ffmpeg_on_colab():
    """If running in Linux with NVIDIA GPU and default FFmpeg lacks NVENC, auto-install CUDA-enabled FFmpeg."""
    if sys.platform != "linux":
        return
    if not shutil.which("nvidia-smi"):
        return
    try:
        r = subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        if r.returncode != 0:
            return
    except Exception:
        return

    ffmpeg_bin = get_ffmpeg_binary()
    try:
        r = subprocess.run([ffmpeg_bin, "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        if "h264_nvenc" in r.stdout:
            return
    except Exception:
        pass

    print("\n⚡ [GPU T4 Setup] Phát hiện card NVIDIA GPU nhưng FFmpeg mặc định chưa có module NVENC.")
    print("⏳ Đang tự động nạp FFmpeg CUDA / NVENC (chỉ mất ~5-10s)...")
    try:
        temp_dir = Path("/tmp/colab-ffmpeg-cuda")
        if not temp_dir.exists():
            subprocess.run(["git", "clone", "-q", "https://github.com/rokibulislaam/colab-ffmpeg-cuda.git", str(temp_dir)], timeout=30)
        if temp_dir.exists():
            for binary in ["ffmpeg", "ffprobe"]:
                src = temp_dir / "bin" / binary
                if src.exists():
                    for dest_dir in [Path("/usr/local/bin"), Path("/usr/bin")]:
                        try:
                            dest = dest_dir / binary
                            shutil.copy2(src, dest)
                            os.chmod(dest, 0o755)
                        except Exception:
                            pass
            print("✅ Đã kích hoạt thành công FFmpeg CUDA & NVIDIA NVENC!\n")
    except Exception as exc:
        print(f"⚠️ Không thể tải FFmpeg CUDA: {exc}\n")


def detect_available_gpu_encoders() -> Dict[str, Any]:
    """Dynamically test and detect available hardware GPU encoders (NVIDIA NVENC, Intel QSV, AMD AMF, Apple VideoToolbox, etc.)."""
    global _DETECTED_GPU_ENCODERS
    if _DETECTED_GPU_ENCODERS is not None:
        return _DETECTED_GPU_ENCODERS

    ensure_cuda_ffmpeg_on_colab()

    ffmpeg_bin = get_ffmpeg_binary()
    encoders = set()
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        res = subprocess.run([ffmpeg_bin, "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo, timeout=5)
        for line in res.stdout.splitlines():
            m = re.search(r"^\s*V\S*\s+(\S+)", line)
            if m:
                encoders.add(m.group(1).lower())
    except Exception:
        pass

    def _test_encoder(encoder_name: str) -> bool:
        if encoder_name not in encoders:
            return False
        try:
            # Note: NVENC requires minimum resolution 144x144 and supported pixel format (yuv420p / nv12)
            test_cmd = [
                ffmpeg_bin, "-y", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.05",
                "-pix_fmt", "yuv420p", "-c:v", encoder_name, "-f", "null", "-"
            ]
            t_res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo, timeout=4)
            return t_res.returncode == 0
        except Exception:
            return False

    has_nvenc_h264 = _test_encoder("h264_nvenc")
    has_nvenc_hevc = _test_encoder("hevc_nvenc")
    has_qsv_h264 = _test_encoder("h264_qsv")
    has_qsv_hevc = _test_encoder("hevc_qsv")
    has_amf_h264 = _test_encoder("h264_amf")
    has_amf_hevc = _test_encoder("hevc_amf")
    has_vt_h264 = _test_encoder("h264_videotoolbox")
    has_vt_hevc = _test_encoder("hevc_videotoolbox")

    gpu_model = get_nvidia_gpu_model_name() if (has_nvenc_h264 or has_nvenc_hevc) else ""
    nvenc_label = f"NVIDIA NVENC (GPU {gpu_model})" if gpu_model else "NVIDIA NVENC (GPU)"

    h264_list = []
    hevc_list = []

    if has_nvenc_h264:
        h264_list.append({"name": "h264_nvenc", "label": nvenc_label})
    if has_nvenc_hevc:
        hevc_list.append({"name": "hevc_nvenc", "label": nvenc_label})

    if has_qsv_h264:
        h264_list.append({"name": "h264_qsv", "label": "Intel QuickSync (GPU)"})
    if has_qsv_hevc:
        hevc_list.append({"name": "hevc_qsv", "label": "Intel QuickSync (GPU)"})

    if has_amf_h264:
        h264_list.append({"name": "h264_amf", "label": "AMD AMF (GPU)"})
    if has_amf_hevc:
        hevc_list.append({"name": "hevc_amf", "label": "AMD AMF (GPU)"})

    if has_vt_h264:
        h264_list.append({"name": "h264_videotoolbox", "label": "Apple VideoToolbox (GPU)"})
    if has_vt_hevc:
        hevc_list.append({"name": "hevc_videotoolbox", "label": "Apple VideoToolbox (GPU)"})

    # Always provide CPU software encoders
    h264_list.append({"name": "libx264", "label": "CPU Software (x264)"})
    hevc_list.append({"name": "libx265", "label": "CPU Software (x265)"})

    has_gpu = bool(has_nvenc_h264 or has_qsv_h264 or has_amf_h264 or has_vt_h264)
    if has_nvenc_h264:
        primary = nvenc_label
    elif has_qsv_h264:
        primary = "Intel QuickSync (GPU)"
    elif has_vt_h264:
        primary = "Apple VideoToolbox (GPU)"
    elif has_amf_h264:
        primary = "AMD AMF (GPU)"
    else:
        primary = "CPU Software (x264)"

    result = {
        "has_gpu": has_gpu,
        "primary_gpu": primary,
        "gpu_model": gpu_model,
        "has_nvenc": has_nvenc_h264,
        "has_qsv": has_qsv_h264,
        "has_amf": has_amf_h264,
        "has_videotoolbox": has_vt_h264,
        "h264_encoders": h264_list,
        "hevc_encoders": hevc_list,
    }
    _DETECTED_GPU_ENCODERS = result
    return result


def select_best_encoder(codec_type: str = "h264", gpu_preference: str = "nvenc") -> Tuple[str, List[str], str]:
    """
    Select video encoder and FFmpeg flags with dynamic fallback.
    Configured for visually lossless / maximum original fidelity.
    Returns: (encoder_name, encoder_flags, display_label)
    """
    codec_lower = str(codec_type or "h264").lower()
    is_hevc = codec_lower in ["h265", "hevc"]
    pref = str(gpu_preference or "nvenc").lower()

    gpu_info = detect_available_gpu_encoders()

    if pref in ["cpu", "software"]:
        chosen_encoder = "libx265" if is_hevc else "libx264"
        display_label = f"CPU Software ({chosen_encoder})"
    elif pref in ["qsv", "intel"] and gpu_info.get("has_qsv"):
        chosen_encoder = "hevc_qsv" if is_hevc else "h264_qsv"
        display_label = "Intel QuickSync (GPU)"
    elif pref in ["amf", "amd"] and gpu_info.get("has_amf"):
        chosen_encoder = "hevc_amf" if is_hevc else "h264_amf"
        display_label = "AMD AMF (GPU)"
    elif pref in ["videotoolbox", "apple"] and gpu_info.get("has_videotoolbox"):
        chosen_encoder = "hevc_videotoolbox" if is_hevc else "h264_videotoolbox"
        display_label = "Apple VideoToolbox (GPU)"
    else:  # Default / nvenc / auto
        if gpu_info.get("has_nvenc"):
            chosen_encoder = "hevc_nvenc" if is_hevc else "h264_nvenc"
            display_label = "NVIDIA NVENC (GPU)"
        elif gpu_info.get("has_qsv"):
            chosen_encoder = "hevc_qsv" if is_hevc else "h264_qsv"
            display_label = "Intel QuickSync (GPU)"
        elif gpu_info.get("has_videotoolbox"):
            chosen_encoder = "hevc_videotoolbox" if is_hevc else "h264_videotoolbox"
            display_label = "Apple VideoToolbox (GPU)"
        elif gpu_info.get("has_amf"):
            chosen_encoder = "hevc_amf" if is_hevc else "h264_amf"
            display_label = "AMD AMF (GPU)"
        else:
            # Fallback smoothly to CPU
            chosen_encoder = "libx265" if is_hevc else "libx264"
            display_label = f"CPU Software ({chosen_encoder})"

    flags: List[str] = ["-c:v", chosen_encoder]

    if "nvenc" in chosen_encoder:
        flags.extend([
            "-preset", "p4",           # High throughput HQ preset (P4)
            "-tune", "hq",
            "-pix_fmt", "yuv420p",
        ])
        if is_hevc:
            flags.extend(["-tag:v", "hvc1"])
    elif "qsv" in chosen_encoder:
        flags.extend([
            "-preset", "medium",
            "-pix_fmt", "nv12",
        ])
        if is_hevc:
            flags.extend(["-tag:v", "hvc1"])
    elif "amf" in chosen_encoder:
        flags.extend([
            "-quality", "quality",
            "-pix_fmt", "yuv420p",
        ])
        if is_hevc:
            flags.extend(["-tag:v", "hvc1"])
    elif "videotoolbox" in chosen_encoder:
        flags.extend([
            "-pix_fmt", "yuv420p",
        ])
        if is_hevc:
            flags.extend(["-tag:v", "hvc1"])
    else:  # libx264 / libx265
        flags.extend([
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
        ])
        if is_hevc:
            flags.extend(["-tag:v", "hvc1"])

    return chosen_encoder, flags, display_label


# ───────────────────────── Video Color Filter Presets ─────────────────────────

COLOR_PRESETS: Dict[str, str] = {
    "none": "",
    "cinematic": "eq=contrast=1.15:saturation=0.9:brightness=-0.02,vignette=PI/5",
    "warm": "eq=contrast=1.08:saturation=1.1:brightness=0.02,colorbalance=rs=0.08:gs=0.02:bs=-0.08",
    "cold": "eq=contrast=1.1:saturation=0.9:brightness=-0.01,colorbalance=rs=-0.08:gs=0.0:bs=0.12",
    "vintage": "eq=contrast=1.1:saturation=0.75:brightness=0.02,colorbalance=rs=0.08:gs=0.02:bs=-0.08,vignette=PI/4",
    "anime": "eq=contrast=1.25:saturation=1.35:brightness=0.03",
    "anime_sharp": "eq=contrast=1.3:saturation=1.4:brightness=0.02,unsharp=5:5:0.8:5:5:0",
    "chinese_drama_warm": "eq=contrast=1.08:saturation=1.08:brightness=0.015,colorbalance=rs=0.06:gs=0.02:bs=-0.04",
    "chinese_drama_night": "eq=contrast=1.15:saturation=0.88:brightness=-0.04,colorbalance=rs=-0.05:gs=-0.01:bs=0.12,vignette=PI/6",
    "chinese_romance": "eq=contrast=1.03:saturation=1.08:brightness=0.04,colorbalance=rs=0.07:gs=0.01:bs=-0.03",
}

# ───────────────────────── Audio Distortion & Voice Effect Presets ─────────────────────────

AUDIO_PRESETS: Dict[str, str] = {
    "none": "",
    "anti_copyright": "asetrate=44100*1.04,aresample=44100,atempo=1/1.04,volume=1.05",
    "pitch_up_light": "asetrate=44100*1.06,aresample=44100,atempo=1/1.06",
    "pitch_up_chipmunk": "asetrate=44100*1.15,aresample=44100,atempo=1/1.15",
    "pitch_down_light": "asetrate=44100*0.94,aresample=44100,atempo=1/0.94",
    "pitch_down_deep": "asetrate=44100*0.88,aresample=44100,atempo=1/0.88",
    "echo_reverb": "aecho=0.8:0.88:30:0.4",
    "robot_tremolo": "tremolo=f=12:d=0.8",
    "radio_telephone": "highpass=f=300,lowpass=f=3400,volume=1.2",
    "bass_boost": "bass=g=8:f=110:w=0.6",
}


def list_directory_videos(dir_path: Path) -> List[Dict[str, Any]]:
    """List all video files in directory with natural sorting and metadata."""
    p = Path(dir_path).resolve()
    if not p.exists() or not p.is_dir():
        return []

    valid_exts = {".mp4", ".mkv", ".ts", ".mov", ".flv", ".webm", ".avi", ".m4v"}
    video_files = [
        f for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in valid_exts and not f.name.startswith("temp_")
    ]

    # Sort with natural sort order
    video_files.sort(key=lambda f: natural_sort_key(f.name))

    results = []
    for idx, f in enumerate(video_files, 1):
        meta = probe_video_info(f)
        meta["index"] = idx
        meta["mtime"] = f.stat().st_mtime
        meta["mtime_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))
        results.append(meta)

    return results


# ───────────────────────── Task Manager for Merge Jobs ─────────────────────────

MERGE_TASKS: Dict[str, Dict[str, Any]] = {}
MERGE_LOCK = threading.Lock()
LAST_MERGE_RESULT: Dict[str, Any] = {}


def get_task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve current merge task status."""
    with MERGE_LOCK:
        task = MERGE_TASKS.get(task_id)
        if task:
            return dict(task)
    return None


def get_last_merge_result() -> Dict[str, Any]:
    """Retrieve result of the most recently finished merge job."""
    with MERGE_LOCK:
        return dict(LAST_MERGE_RESULT)


def cancel_merge_task(task_id: str) -> bool:
    """Cancel an ongoing merge task by killing its subprocess."""
    with MERGE_LOCK:
        task = MERGE_TASKS.get(task_id)
        if not task:
            return False
        task["cancelled"] = True
        proc = task.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        task["status"] = "cancelled"
        task["message"] = "Đã hủy tiến trình ghép video."
        return True


# ───────────────────────── Helper Functions for Normalization & Single-Pass Pipeline ─────────────────────────


def determine_target_resolution(
    probed_infos: List[Dict[str, Any]],
    user_resolution: str = "original",
    custom_resolution: str = "",
) -> Tuple[int, int]:
    """
    Determine target (width, height) to ensure all concatenated clips match perfectly
    without altering the original resolution.
    """
    if user_resolution and user_resolution != "original":
        res_val = custom_resolution if user_resolution == "custom" and custom_resolution else user_resolution
        if "x" in res_val:
            try:
                w, h = [x.strip() for x in res_val.split("x", 1)]
                w_int, h_int = int(w), int(h)
                return w_int + (w_int % 2), h_int + (h_int % 2)
            except Exception:
                pass

    # Pick the most frequent (mode) resolution from probed files
    res_counts: Dict[Tuple[int, int], int] = {}
    for info in probed_infos:
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        if w > 0 and h > 0:
            key = (w + (w % 2), h + (h % 2))
            res_counts[key] = res_counts.get(key, 0) + 1

    if res_counts:
        sorted_res = sorted(res_counts.keys(), key=lambda r: (res_counts[r], r[0] * r[1]), reverse=True)
        return sorted_res[0]

    return (1080, 1920)


def determine_target_fps(probed_infos: List[Dict[str, Any]], user_fps: str = "original") -> Optional[float]:
    """Determine target fps from input clips to preserve original frame rate."""
    if user_fps and user_fps != "original":
        try:
            return float(user_fps)
        except Exception:
            return None
    valid_fps = [float(info["fps"]) for info in probed_infos if info.get("fps") and float(info["fps"]) > 0]
    if not valid_fps:
        return None
    if max(valid_fps) - min(valid_fps) > 0.5:
        from collections import Counter
        most_common = Counter([round(f, 1) for f in valid_fps]).most_common(1)[0][0]
        return most_common
    return None


def generate_batch_filter_script(
    batch_files: List[Path],
    batch_infos: List[Dict[str, Any]],
    target_w: int,
    target_h: int,
    target_fps: Optional[float],
    color_filter_str: str,
    audio_filter_str: str,
    cut_end_seconds: float = 0.0,
    mirror: bool = False,
    video_speed: float = 0.9,
) -> str:
    """Generate FFmpeg filter_complex script content with ultra-fast size/SAR normalization, speed scaling (default 0.9x), and frame-accurate sync."""
    filter_parts = []
    concat_inputs = []

    mirror_part = ",hflip" if mirror else ""

    for idx, (f, info) in enumerate(zip(batch_files, batch_infos)):
        dur = info["effective_duration"]
        clip_dur = info.get("clip_duration", dur)
        has_audio = info.get("has_audio", False)
        in_w = int(info.get("width") or 0)
        in_h = int(info.get("height") or 0)
        in_fps = float(info.get("fps") or 0.0)

        # Video stream: only scale/pad if dimensions actually differ to eliminate CPU bottlenecks!
        v_filters = []
        if cut_end_seconds > 0:
            v_filters.append(f"trim=start=0:duration={clip_dur:.3f}")

        # Tốc độ phát video (mặc định 0.9x -> video chậm lại 10%)
        if abs(video_speed - 1.0) > 0.005 and video_speed > 0:
            v_pts = 1.0 / video_speed
            v_filters.append(f"setpts=(PTS-STARTPTS)*{v_pts:.6f}")
        else:
            v_filters.append("setpts=PTS-STARTPTS")

        if in_w != target_w or in_h != target_h:
            v_filters.append(
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=bilinear,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1"
            )

        if target_fps and abs(in_fps - target_fps) > 0.5:
            v_filters.append(f"fps={target_fps}")

        if mirror:
            v_filters.append("hflip")
        if color_filter_str:
            clean_cf = color_filter_str.lstrip(",")
            if clean_cf:
                v_filters.append(clean_cf)

        v_filters.append("format=yuv420p")
        v_filter_combined = ",".join(v_filters)
        filter_parts.append(f"[{idx}:v]{v_filter_combined}[v{idx}]")

        # Audio stream: setpts, atempo (đồng bộ với video_speed 0.9x), resample with async sync, format stereo 44100Hz
        tempo_filter = f",atempo={video_speed:.4f}" if (abs(video_speed - 1.0) > 0.005 and video_speed > 0) else ""
        if has_audio:
            if cut_end_seconds > 0:
                a_filter = f"atrim=start=0:duration={clip_dur:.3f},asetpts=PTS-STARTPTS{tempo_filter},aresample=44100:async=1000:first_pts=0,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{audio_filter_str}"
            else:
                a_filter = f"asetpts=PTS-STARTPTS{tempo_filter},aresample=44100:async=1000:first_pts=0,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{audio_filter_str}"
            filter_parts.append(f"[{idx}:a]{a_filter}[a{idx}]")
        else:
            filter_parts.append(f"aevalsrc=0:d={dur:.3f}:s=44100:c=stereo[a{idx}]")

        concat_inputs.append(f"[v{idx}][a{idx}]")

    n_clips = len(batch_files)
    filter_parts.append(f"{''.join(concat_inputs)}concat=n={n_clips}:v=1:a=1[outv][outa]")
    return ";\n".join(filter_parts)


def build_encoding_args(
    codec: str = "h264",
    gpu_pref: str = "nvenc",
    probed_infos: Optional[List[Dict[str, Any]]] = None,
    **kwargs,
) -> Tuple[str, List[str], List[str], str]:
    """
    Configure video encoder and bitrate/CRF flags for Balanced High Quality.
    Provides crisp sharpness while maximizing hardware encoding throughput.
    Returns: (chosen_encoder, enc_flags, bitrate_flags, display_label)
    """
    chosen_encoder, enc_flags, display_label = select_best_encoder(codec, gpu_pref)

    # Calculate original video average bitrate
    probed = probed_infos or []
    total_orig_bytes = sum(info.get("size", 0) for info in probed)
    total_orig_dur = sum(info.get("duration", 0) for info in probed)

    if total_orig_dur > 0 and total_orig_bytes > 0:
        avg_total_bps = int((total_orig_bytes * 8) / total_orig_dur)
        orig_video_bps = max(500_000, avg_total_bps - 128_000)
    else:
        orig_video_bps = 2_000_000

    target_video_kbps = max(2000, int(orig_video_bps * 2.50) // 1000)
    maxrate_kbps = max(3500, int(orig_video_bps * 4.00) // 1000)
    bufsize_kbps = max(4000, int(orig_video_bps * 5.00) // 1000)

    bitrate_flags = []
    if "nvenc" in chosen_encoder:
        bitrate_flags.extend([
            "-cq:v", "16",                     # High Quality Balanced CQ
            "-b:v", f"{target_video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
            "-rc", "vbr",
        ])
    elif "qsv" in chosen_encoder:
        bitrate_flags.extend([
            "-global_quality", "14",
            "-b:v", f"{target_video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
        ])
    elif "amf" in chosen_encoder:
        bitrate_flags.extend([
            "-rc", "cqp",
            "-qp_i", "14",
            "-qp_p", "14",
            "-b:v", f"{target_video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
        ])
    else:  # libx264 / libx265
        bitrate_flags.extend([
            "-crf", "14",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
        ])

    return chosen_encoder, enc_flags, bitrate_flags, display_label


def run_single_pass_encode(
    batch_files: List[Path],
    batch_infos: List[Dict[str, Any]],
    batch_output_file: Path,
    target_w: int,
    target_h: int,
    target_fps: Optional[float],
    color_filter_str: str,
    audio_filter_str: str,
    enc_flags: List[str],
    bitrate_flags: List[str],
    out_format: str,
    temp_dir: Path,
    ffmpeg_bin: str,
    task_id: str,
    update_task: Callable[..., None],
    prev_processed_dur: float,
    total_effective_dur: float,
    cut_end_seconds: float = 0.0,
    mirror: bool = False,
    video_speed: float = 0.9,
    progress_scale: float = 1.0,
    progress_base: float = 0.0,
) -> None:
    """Encode all clips in a single direct pass using -filter_complex_script for 100% stability & high quality."""
    script_content = generate_batch_filter_script(
        batch_files, batch_infos, target_w, target_h, target_fps, color_filter_str, audio_filter_str, cut_end_seconds, mirror=mirror, video_speed=video_speed
    )
    script_file = temp_dir / f"filter_{uuid.uuid4().hex[:8]}.txt"
    with open(script_file, "w", encoding="utf-8") as sf:
        sf.write(script_content)

    batch_dur = sum(info["effective_duration"] for info in batch_infos)

    cmd = [ffmpeg_bin, "-y", "-nostats", "-loglevel", "warning"]
    for f in batch_files:
        cmd.extend(["-i", str(f)])

    filter_flag = get_filter_complex_file_flag(ffmpeg_bin)
    cmd.extend([
        filter_flag, str(script_file),
        "-map", "[outv]",
        "-map", "[outa]",
    ])
    cmd.extend(enc_flags)
    cmd.extend(bitrate_flags)
    cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "44100"])

    if out_format in {"mp4", "mov"}:
        cmd.extend(["-movflags", "+faststart"])

    is_gdrive = str(batch_output_file).startswith("/content/drive")
    if is_gdrive:
        actual_output_target = Path("/tmp") / f"nvme_merge_{uuid.uuid4().hex[:8]}_{batch_output_file.name}"
    else:
        actual_output_target = batch_output_file

    cmd.extend([
        "-threads", "0",
        "-progress", "pipe:1",
        str(actual_output_target),
    ])

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=startupinfo,
        )
        update_task(proc=proc)

        stderr_lines: List[str] = []
        def _read_stderr():
            try:
                for s_line in proc.stderr:
                    if s_line:
                        stderr_lines.append(s_line.strip())
                        if len(stderr_lines) > 50:
                            stderr_lines.pop(0)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        last_update_time = time.time()
        while True:
            with MERGE_LOCK:
                task_status = MERGE_TASKS.get(task_id, {})
            if task_status.get("cancelled"):
                proc.terminate()
                raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")

            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break

            line_str = line.strip()
            if not line_str or "=" not in line_str:
                continue

            k, v = line_str.split("=", 1)
            k = k.strip()
            v = v.strip()

            if k == "out_time_us":
                try:
                    out_us = int(v)
                    cur_batch_sec = min(batch_dur, out_us / 1000000.0)
                    cur_overall_sec = prev_processed_dur + cur_batch_sec
                    raw_ratio = min(1.0, max(0.0, cur_overall_sec / total_effective_dur))
                    scaled_pct = progress_base + (raw_ratio * progress_scale * 100.0)
                    pct = min(progress_base + progress_scale * 100.0 - 0.1, max(progress_base, scaled_pct))
                    now = time.time()
                    if now - last_update_time >= 0.25:
                        last_update_time = now
                        update_task(
                            progress=round(pct, 1),
                            current_duration=cur_overall_sec,
                            current_duration_str=format_duration(cur_overall_sec),
                        )
                except Exception:
                    pass
            elif k == "speed":
                speed_str = v.replace("x", "").strip()
                try:
                    speed_val = float(speed_str)
                    if speed_val > 0:
                        with MERGE_LOCK:
                            cur_d = MERGE_TASKS.get(task_id, {}).get("current_duration", prev_processed_dur)
                        rem_sec = max(0, (total_effective_dur - cur_d) / speed_val)
                        update_task(speed=f"{speed_val:.1f}x", eta=f"{int(rem_sec)}s")
                except Exception:
                    update_task(speed=v)
            elif k == "progress" and v == "end":
                pass

        proc.wait()
        update_task(
            progress=round(progress_base + progress_scale * 100.0, 1),
            speed="-",
            eta="-",
        )

        with MERGE_LOCK:
            task_status = MERGE_TASKS.get(task_id, {})
        if task_status.get("cancelled"):
            raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")

        if proc.returncode != 0:
            err_msg = " \n".join(stderr_lines) if stderr_lines else "Lỗi không xác định"
            if "moov atom not found" in err_msg or "moov atom" in err_msg:
                m_in = re.search(r"\[in#(\d+)@", err_msg)
                if m_in:
                    clip_idx = int(m_in.group(1))
                    err_msg = f"Tập thứ {clip_idx + 1} (đầu vào #{clip_idx}) bị hỏng/lỗi 'moov atom not found'. Vui lòng xóa hoặc tải lại tập này!\nChi tiết: {err_msg[:250]}"
                else:
                    err_msg = f"File video đầu vào bị hỏng/lỗi 'moov atom not found' (file rỗng hoặc chưa tải xong). Vui lòng kiểm tra danh sách tập!\nChi tiết: {err_msg[:250]}"
            raise RuntimeError(f"FFmpeg xử lý thất bại (mã lỗi {proc.returncode}): {err_msg[:350]}")

        # Transfer local temporary file to Google Drive
        if is_gdrive and actual_output_target.exists():
            update_task(message="Đang chuyển file video hoàn tất vào Google Drive...")
            batch_output_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(actual_output_target), str(batch_output_file))

    finally:
        try:
            if script_file.exists():
                script_file.unlink(missing_ok=True)
        except Exception:
            pass
        if is_gdrive and actual_output_target.exists():
            try:
                actual_output_target.unlink(missing_ok=True)
            except Exception:
                pass


def execute_merge_job(task_id: str, files: List[str], options: Dict[str, Any]) -> None:
    """Core video merge execution function running in background thread."""
    with MERGE_LOCK:
        task = MERGE_TASKS.get(task_id)
        if not task:
            return

    def update_task(**kwargs):
        with MERGE_LOCK:
            if task_id in MERGE_TASKS:
                MERGE_TASKS[task_id].update(kwargs)

    temp_merge_dir: Optional[Path] = None

    try:
        update_task(status="running", progress=0, message="Đang phân tích thông số các video đầu vào...")

        valid_files = [Path(f).resolve() for f in files if Path(f).is_file()]
        if not valid_files:
            raise ValueError("Không tìm thấy file video hợp lệ nào để ghép.")

        cut_end_seconds = float(options.get("cut_end_seconds") or 0.0)
        mirror = options.get("mirror") in (True, "true", "True", "1", 1)
        quality = str(options.get("quality", "original")).lower()
        resolution = str(options.get("resolution", "original")).lower()
        custom_resolution = str(options.get("custom_resolution", "")).strip()
        fps = str(options.get("fps", "original")).lower()
        bitrate = str(options.get("bitrate", "auto")).lower()
        custom_bitrate = str(options.get("custom_bitrate", "")).strip()
        codec = str(options.get("codec", "h264")).lower()
        color_filter_key = str(options.get("color_filter", "none")).strip().lower()
        custom_color_filter = str(options.get("custom_color_filter", "")).strip()
        audio_effect_key = str(options.get("audio_effect", "none")).strip().lower()
        custom_audio_effect = str(options.get("custom_audio_effect", "")).strip()
        out_format = str(options.get("format", "mp4")).lower().lstrip(".")
        output_dir = Path(options.get("output_dir") or valid_files[0].parent).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        output_filename = options.get("output_name") or f"merged_{int(time.time())}.{out_format}"
        if not output_filename.lower().endswith(f".{out_format}"):
            output_filename = f"{output_filename}.{out_format}"
        output_path = output_dir / output_filename

        update_task(output_path=str(output_path), output_name=output_filename)

        # Build color filter string
        color_filter_str = ""
        if color_filter_key == "custom" and custom_color_filter:
            color_filter_str = f",{custom_color_filter.strip().lstrip(',')}"
        elif color_filter_key in COLOR_PRESETS and COLOR_PRESETS[color_filter_key]:
            color_filter_str = f",{COLOR_PRESETS[color_filter_key]}"

        # Build audio distortion / effect filter string
        audio_filter_str = ""
        if audio_effect_key == "custom" and custom_audio_effect:
            audio_filter_str = f",{custom_audio_effect.strip().lstrip(',')}"
        elif audio_effect_key in AUDIO_PRESETS and AUDIO_PRESETS[audio_effect_key]:
            audio_filter_str = f",{AUDIO_PRESETS[audio_effect_key]}"

        # Tốc độ video ghép từ các tập (mặc định chậm đi 0.9x theo yêu cầu)
        video_speed = float(options.get("video_speed") or options.get("speed") or 0.9)
        if video_speed <= 0:
            video_speed = 0.9

        # 1. Probe all input files to obtain exact durations and stream info
        probed_infos = []
        corrupt_files = []
        total_effective_duration = 0.0

        for i, f in enumerate(valid_files):
            with MERGE_LOCK:
                task_status = MERGE_TASKS.get(task_id, {})
            if task_status.get("cancelled"):
                raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")
            info = probe_video_info(f)
            dur = info.get("duration", 0.0)

            if not info.get("is_valid", True) or info.get("error") or dur <= 0 or info.get("size", 0) == 0:
                fname = info.get("filename") or Path(f).name
                err_reason = info.get("error") or "File bị lỗi hoặc chưa tải hoàn tất"
                corrupt_files.append(f"• Tập {i+1} ({fname}): {err_reason}")

            if cut_end_seconds > 0:
                clip_dur = max(0.5, dur - cut_end_seconds) if dur > cut_end_seconds else max(0.2, dur * 0.5)
            else:
                clip_dur = max(0.2, dur)
            effective_dur = clip_dur / video_speed if video_speed > 0 else clip_dur
            info["clip_duration"] = clip_dur
            info["effective_duration"] = effective_dur
            probed_infos.append(info)
            total_effective_duration += effective_dur

        if corrupt_files:
            err_summary = "\n".join(corrupt_files[:10])
            if len(corrupt_files) > 10:
                err_summary += f"\nvà {len(corrupt_files) - 10} tập khác..."
            raise ValueError(
                f"Không thể ghép video do phát hiện {len(corrupt_files)} file bị hỏng hoặc chưa tải xong:\n{err_summary}\n\n"
                f"-> Hướng xử lý: Vui lòng xóa hoặc tải lại các tập bị lỗi trên trước khi ghép!"
            )

        if total_effective_duration <= 0:
            total_effective_duration = 1.0

        update_task(
            total_duration=total_effective_duration,
            total_duration_str=format_duration(total_effective_duration),
            file_count=len(valid_files),
            video_speed=video_speed,
        )

        ffmpeg_bin = get_ffmpeg_binary()

        # Direct Single-Pass Video & Audio Pipeline (100% A/V Sync, No Glitches)
        # Determine target resolution & FPS to guarantee uniform input pads to concat filter
        target_w, target_h = determine_target_resolution(probed_infos, resolution, custom_resolution)
        target_fps = determine_target_fps(probed_infos, fps)

        # Determine active features and dynamic phase structure
        upload_to_storage = options.get("upload_to_storage", True) in (True, "true", "True", 1, "1")
        generate_subtitles = options.get("generate_subtitles", True) in (True, "true", "True", 1, "1")
        translate_subtitles = options.get("translate_subtitles", True) in (True, "true", "True", 1, "1") if generate_subtitles else False
        enable_dubbing = options.get("dubbing", True) in (True, "true", "True", 1, "1") if translate_subtitles else False
        storage_api_token = options.get("storage_token") or os.getenv("STORAGE_TO_API_TOKEN")

        if enable_dubbing:
            phase_1 = "🎬 [1/4] Ghép video"
            phase_2 = "📝 [2/4] Phụ đề & Dịch thuật"
            phase_3 = "🎙️ [3/4] Lồng tiếng VieNeu-TTS"
            phase_4 = "🎬 [4/4] Render & Xuất bản"
            enc_scale = 0.40
        elif generate_subtitles:
            phase_1 = "🎬 [1/2] Ghép video"
            phase_2 = "📝 [2/2] Phụ đề & Dịch thuật"
            phase_3 = ""
            phase_4 = ""
            enc_scale = 0.60
        else:
            phase_1 = "🎬 Ghép video"
            phase_2 = ""
            phase_3 = ""
            phase_4 = ""
            enc_scale = 0.90

        gpu_pref = str(options.get("gpu", "nvenc")).lower()
        chosen_encoder, enc_flags, bitrate_flags, display_label = build_encoding_args(
            codec=codec,
            gpu_pref=gpu_pref,
            probed_infos=probed_infos,
        )

        speed_label = f" (Tốc độ {video_speed:.2f}x)" if abs(video_speed - 1.0) > 0.005 else ""
        update_task(
            phase=phase_1,
            progress=0.0,
            message=f"Đang ghép 1 lần trực tiếp ({len(valid_files)} video){speed_label} với {display_label} ({target_w}x{target_h})...",
            gpu_encoder=display_label,
        )

        # Encode directly to the final output file in a single pass without intermediate batch chunks
        temp_script_dir = output_dir
        try:
            run_single_pass_encode(
                valid_files,
                probed_infos,
                output_path,
                target_w,
                target_h,
                target_fps,
                color_filter_str,
                audio_filter_str,
                enc_flags,
                bitrate_flags,
                out_format,
                temp_script_dir,
                ffmpeg_bin,
                task_id,
                update_task,
                0.0,
                total_effective_duration,
                cut_end_seconds=cut_end_seconds,
                mirror=mirror,
                video_speed=video_speed,
                progress_scale=enc_scale,
                progress_base=0.0,
            )
        except Exception as encode_err:
            err_text = str(encode_err)
            if any(k in err_text.lower() for k in ["mfx", "qsv", "nvenc", "cuda", "amf", "videotoolbox", "opening encoder", "encoder for output stream"]):
                _safe_log(f"[Encoder Fallback] GPU encoder failed ({err_text[:100]}). Falling back to CPU libx264...")
                update_task(
                    phase=phase_1,
                    message="Bộ mã hóa GPU không khởi động được. Đang chuyển sang CPU (libx264)...",
                    gpu_encoder="CPU Software (libx264)"
                )
                cpu_encoder, cpu_enc_flags, cpu_bitrate_flags, cpu_label = build_encoding_args(
                    codec=codec,
                    gpu_pref="cpu",
                    probed_infos=probed_infos,
                )
                run_single_pass_encode(
                    valid_files,
                    probed_infos,
                    output_path,
                    target_w,
                    target_h,
                    target_fps,
                    color_filter_str,
                    audio_filter_str,
                    cpu_enc_flags,
                    cpu_bitrate_flags,
                    out_format,
                    temp_script_dir,
                    ffmpeg_bin,
                    task_id,
                    update_task,
                    0.0,
                    total_effective_duration,
                    cut_end_seconds=cut_end_seconds,
                    mirror=mirror,
                    video_speed=video_speed,
                    progress_scale=enc_scale,
                    progress_base=0.0,
                )
            else:
                raise encode_err

        with MERGE_LOCK:
            task_status = MERGE_TASKS.get(task_id, {})
        if task_status.get("cancelled"):
            raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")

        out_size = output_path.stat().st_size if output_path.exists() else 0
        enc_done_pct = round(enc_scale * 100.0, 1)
        update_task(
            phase=phase_1,
            progress=enc_done_pct,
            speed="-",
            eta="-",
            message="Ghép video hoàn tất!",
            output_size=out_size,
            output_size_str=format_size(out_size),
        )

        video_url = None
        srt_url = None
        srt_path = None
        translated_srt_url = None
        translated_srt_path = None
        dubbed_audio_url = None
        dubbed_audio_path = None
        dubbed_video_url = None
        dubbed_video_path = None
        storage_info = {}

        # 1. Tải video đã ghép lên storage.to
        if upload_to_storage and output_path.exists():
            try:
                active_phase = phase_2 if (enable_dubbing or generate_subtitles) else phase_1
                update_task(phase=active_phase, message="Đang kết nối và tải video gốc lên storage.to...")
                from storage_service import upload_file_to_storage_to

                def _upload_video_cb(pct, msg):
                    if enable_dubbing:
                        up_p = 40.0 + (pct / 100.0) * 3.0
                    elif generate_subtitles:
                        up_p = 60.0 + (pct / 100.0) * 10.0
                    else:
                        up_p = 90.0 + (pct / 100.0) * 10.0
                    update_task(
                        phase=active_phase,
                        progress=round(up_p, 1),
                        message=f"Đang tải video gốc lên storage.to ({pct:.0f}%)...",
                        upload_progress=pct,
                    )

                storage_res = upload_file_to_storage_to(
                    output_path,
                    api_token=storage_api_token,
                    on_progress=_upload_video_cb,
                )
                video_url = storage_res.get("url")
                storage_info["video"] = storage_res
                done_v_p = 43.0 if enable_dubbing else (70.0 if generate_subtitles else 100.0)
                update_task(
                    phase=active_phase,
                    progress=done_v_p,
                    video_url=video_url,
                    message="Đã tải video gốc lên storage.to",
                )
            except Exception as up_err:
                _safe_log(f"[Storage.to Error] Lỗi tải video lên storage.to: {up_err}")
                update_task(upload_video_error=str(up_err))

        # 2. Nhận diện giọng nói với CapCut ASR & xuất file phụ đề .srt
        if generate_subtitles and output_path.exists():
            try:
                update_task(phase=phase_2, message="Đang trích xuất phụ đề với CapCut ASR...")
                from helper_service import transcribe_video_to_srt

                def _asr_cb(msg):
                    update_task(phase=phase_2, message=f"[CapCut ASR] {msg}")

                capcut_tdid = options.get("capcut_tdid")
                source_lang = options.get("source_lang", "auto")
                srt_file, segs = transcribe_video_to_srt(
                    str(output_path),
                    engine="capcut",
                    source_lang=source_lang,
                    tdid=capcut_tdid,
                    ffmpeg_bin=ffmpeg_bin,
                    on_status=_asr_cb,
                )
                srt_path = str(srt_file)
                asr_done_p = 48.0 if enable_dubbing else 80.0
                update_task(
                    phase=phase_2,
                    progress=asr_done_p,
                    srt_path=srt_path,
                    subtitle_segments_count=len(segs),
                    message=f"Đã trích xuất phụ đề ({len(segs)} câu)",
                )

                # 3. Tải file phụ đề gốc (.srt) lên storage.to
                if upload_to_storage and srt_file.exists():
                    try:
                        update_task(phase=phase_2, message="Đang tải phụ đề gốc lên storage.to...")
                        from storage_service import upload_file_to_storage_to

                        def _upload_srt_cb(pct, msg):
                            update_task(phase=phase_2, message=f"Đang tải phụ đề gốc lên storage.to ({pct:.0f}%)...")

                        srt_storage_res = upload_file_to_storage_to(
                            srt_file,
                            api_token=storage_api_token,
                            on_progress=_upload_srt_cb,
                        )
                        srt_url = srt_storage_res.get("url")
                        storage_info["subtitle"] = srt_storage_res
                        update_task(
                            phase=phase_2,
                            srt_url=srt_url,
                            message="Đã tải phụ đề gốc lên storage.to",
                        )
                    except Exception as srt_up_err:
                        _safe_log(f"[Storage.to Error] Lỗi tải phụ đề: {srt_up_err}")
                        update_task(upload_srt_error=str(srt_up_err))

                # 4. Dịch phụ đề sang tiếng Việt & Kiểm tra loại bỏ chữ Trung, lọc 1 từ, xóa dấu câu cuối
                if translate_subtitles and segs:
                    try:
                        update_task(phase=phase_2, message="Đang dịch phụ đề sang tiếng Việt...")
                        from helper_service import translate_and_clean_subtitles

                        def _trans_cb(msg):
                            update_task(phase=phase_2, message=f"[Dịch phụ đề] {msg}")

                        trans_prompt = options.get("translate_prompt") or options.get("prompt") or "ai_tong_hop_thong_minh"
                        custom_endpoint = options.get("custom_endpoint") or options.get("endpoint")
                        custom_api_key = options.get("custom_api_key") or options.get("api_key") or options.get("apikey")
                        translate_model = options.get("translate_model") or options.get("model")

                        dest_vi_srt = srt_file.with_name(f"{srt_file.stem}_vi.srt")
                        translated_file, cleaned_segs = translate_and_clean_subtitles(
                            segments=segs,
                            dest_srt_path=dest_vi_srt,
                            target_lang="vi",
                            preset=trans_prompt,
                            model=translate_model,
                            api_key=custom_api_key,
                            custom_endpoint=custom_endpoint,
                            on_status=_trans_cb,
                        )

                        if translated_file and translated_file.exists():
                            translated_srt_path = str(translated_file)
                            trans_done_p = 53.0 if enable_dubbing else 95.0
                            update_task(
                                phase=phase_2,
                                progress=trans_done_p,
                                translated_srt_path=translated_srt_path,
                                translated_segments_count=len(cleaned_segs),
                                message=f"Đã dịch phụ đề tiếng Việt ({len(cleaned_segs)} câu)",
                            )

                            # 5. Tải file phụ đề dịch tiếng Việt (.srt) lên storage.to
                            if upload_to_storage:
                                try:
                                    update_task(phase=phase_2, message="Đang tải phụ đề tiếng Việt lên storage.to...")

                                    def _upload_vi_srt_cb(pct, msg):
                                        update_task(phase=phase_2, message=f"Đang tải phụ đề tiếng Việt lên storage.to ({pct:.0f}%)...")

                                    vi_srt_storage_res = upload_file_to_storage_to(
                                        translated_file,
                                        api_token=storage_api_token,
                                        on_progress=_upload_vi_srt_cb,
                                    )
                                    translated_srt_url = vi_srt_storage_res.get("url")
                                    storage_info["translated_subtitle"] = vi_srt_storage_res
                                    phase2_final_p = 55.0 if enable_dubbing else 100.0
                                    update_task(
                                        phase=phase_2,
                                        progress=phase2_final_p,
                                        translated_srt_url=translated_srt_url,
                                        message="Đã tải phụ đề tiếng Việt lên storage.to",
                                    )
                                except Exception as vi_up_err:
                                    _safe_log(f"[Storage.to Error] Lỗi tải phụ đề tiếng Việt: {vi_up_err}")
                                    update_task(upload_translated_srt_error=str(vi_up_err))

                            # 6. Lồng tiếng (Dubbing) video với VieNeu-TTS & Căn chỉnh âm thanh
                            dubbing_segs = cleaned_segs if cleaned_segs else segs
                            if enable_dubbing and dubbing_segs and output_path.exists():
                                try:
                                    update_task(
                                        phase=phase_3,
                                        progress=55.0,
                                        speed="-",
                                        eta="-",
                                        message="Đang chuẩn bị lồng tiếng với VieNeu-TTS...",
                                    )
                                    import vieneu_tts

                                    tts_voice = options.get("tts_voice") or options.get("voice") or "Ngọc Huyền"
                                    tts_batch_size = int(options.get("tts_batch_size") or options.get("batch_size") or 64)
                                    tts_engine = vieneu_tts.VieNeuTTS(voice=tts_voice, max_batch_size=max(64, tts_batch_size))

                                    video_meta = probe_video_info(output_path)
                                    video_duration = video_meta.get("duration") or 0.0

                                    from config import DEFAULT_SETTINGS
                                    resolved_ep = (
                                        (custom_endpoint or "").strip()
                                        or os.getenv("CUSTOM_API_ENDPOINT")
                                        or DEFAULT_SETTINGS.get("customApiEndpoint", "")
                                    )
                                    if resolved_ep:
                                        ep = resolved_ep.rstrip("/")
                                        chat_url = f"{ep}/chat/completions" if not ep.endswith("/chat/completions") else ep
                                    else:
                                        chat_url = "https://api.deepseek.com/v1/chat/completions"

                                    key = (
                                        (custom_api_key or "").strip()
                                        or os.getenv("CUSTOM_API_KEY")
                                        or os.getenv("DEEPSEEK_API_KEY")
                                        or DEFAULT_SETTINGS.get("customApiKey", "")
                                    )
                                    headers = {"Content-Type": "application/json"}
                                    if key and key != "dummy":
                                        headers["Authorization"] = f"Bearer {key}"

                                    resolved_model = (
                                        (translate_model or "").strip()
                                        or os.getenv("CUSTOM_MODEL")
                                        or DEFAULT_SETTINGS.get("customModel", "gemini-lite")
                                    )

                                    dest_dubbed_audio = output_path.with_name(f"{output_path.stem}_dubbing.mp3")

                                    def _dub_progress_cb(pct, msg):
                                        scaled_dub_p = round(55.0 + (pct / 100.0) * 23.0, 1)
                                        update_task(
                                            phase=phase_3,
                                            progress=scaled_dub_p,
                                            speed="-",
                                            eta="-",
                                            message=f"[Lồng tiếng] {msg}",
                                        )

                                    vieneu_tts.build_full_dubbed_audio(
                                        segments=dubbing_segs,
                                        total_duration=video_duration,
                                        output_audio_path=dest_dubbed_audio,
                                        tts=tts_engine,
                                        voice=tts_voice,
                                        batch_size=tts_batch_size,
                                        chat_url=chat_url,
                                        headers=headers,
                                        model=resolved_model,
                                        max_speedup=float(options.get("max_speedup") or options.get("tts_speedup") or 1.35),
                                        tolerance=float(options.get("tolerance") or options.get("tts_tolerance") or 0.3),
                                        on_progress=_dub_progress_cb,
                                    )

                                    if dest_dubbed_audio.exists():
                                        dubbed_audio_path = str(dest_dubbed_audio)
                                        update_task(
                                            phase=phase_3,
                                            progress=78.0,
                                            dubbed_audio_path=dubbed_audio_path,
                                            message=f"Đã tạo file audio dubbing hoàn chỉnh: {dest_dubbed_audio.name}",
                                        )

                                        # Upload file audio dubbing hoàn chỉnh lên storage.to
                                        if upload_to_storage:
                                            try:
                                                update_task(phase=phase_3, message="Đang tải audio dubbing lên storage.to...")

                                                def _upload_dub_audio_cb(pct, msg):
                                                    scaled_up_p = round(78.0 + (pct / 100.0) * 2.0, 1)
                                                    update_task(
                                                        phase=phase_3,
                                                        progress=scaled_up_p,
                                                        message=f"Đang tải audio dubbing lên storage.to ({pct:.0f}%)...",
                                                    )

                                                dub_audio_storage_res = upload_file_to_storage_to(
                                                    dest_dubbed_audio,
                                                    api_token=storage_api_token,
                                                    on_progress=_upload_dub_audio_cb,
                                                )
                                                dubbed_audio_url = dub_audio_storage_res.get("url")
                                                storage_info["dubbed_audio"] = dub_audio_storage_res
                                                update_task(
                                                    phase=phase_3,
                                                    progress=80.0,
                                                    dubbed_audio_url=dubbed_audio_url,
                                                    message="Đã tải audio dubbing lên storage.to",
                                                )
                                            except Exception as dub_audio_up_err:
                                                _safe_log(f"[Storage.to Error] Lỗi tải audio dubbing: {dub_audio_up_err}")
                                                update_task(upload_dubbed_audio_error=str(dub_audio_up_err))

                                        # 7. Render ghép audio dubbing vào video (âm gốc: giảm còn 0.1, mute 0.1s mỗi 0.9s, pitch down 5%; âm dubbing: khuếch đại 3.0, giữ nguyên cao độ tự nhiên - KHÔNG pitch down)
                                        try:
                                            update_task(
                                                phase=phase_4,
                                                progress=80.0,
                                                speed="-",
                                                eta="-",
                                                message="Đang render video lồng tiếng (âm nền 0.1x, pitch down 5%, mute 0.1s/0.9s)...",
                                            )
                                            dest_dubbed_video = output_path.with_name(f"{output_path.stem}_dubbed.mp4")

                                            def _render_vid_cb(pct, msg):
                                                scaled_render_p = round(80.0 + (pct / 100.0) * 15.0, 1)
                                                update_task(
                                                    phase=phase_4,
                                                    progress=scaled_render_p,
                                                    speed="-",
                                                    eta="-",
                                                    message=f"[Render Dubbed Video] {msg}",
                                                )

                                            vieneu_tts.render_dubbed_video(
                                                video_path=output_path,
                                                dubbed_audio_path=dest_dubbed_audio,
                                                output_video_path=dest_dubbed_video,
                                                bg_volume=float(options.get("bg_volume") if options.get("bg_volume") is not None else options.get("dub_bg_volume", 0.1)),
                                                dub_volume=float(options.get("dub_volume") if options.get("dub_volume") is not None else options.get("dubbing_volume", 3.0)),
                                                pitch_down_pct=float(options.get("dub_pitch_down_pct") if options.get("dub_pitch_down_pct") is not None else 5.0),
                                                enable_periodic_mute=bool(options.get("enable_periodic_mute") if options.get("enable_periodic_mute") is not None else True),
                                                on_progress=_render_vid_cb,
                                            )

                                            if dest_dubbed_video.exists():
                                                dubbed_video_path = str(dest_dubbed_video)
                                                update_task(
                                                    phase=phase_4,
                                                    progress=95.0,
                                                    dubbed_video_path=dubbed_video_path,
                                                    message=f"Đã render video lồng tiếng thành công: {dest_dubbed_video.name}",
                                                )

                                                # Upload video lồng tiếng lên storage.to
                                                if upload_to_storage:
                                                    try:
                                                        update_task(phase=phase_4, message="Đang tải video lồng tiếng lên storage.to...")

                                                        def _upload_dub_vid_cb(pct, msg):
                                                            scaled_up_p = round(95.0 + (pct / 100.0) * 4.0, 1)
                                                            update_task(
                                                                phase=phase_4,
                                                                progress=scaled_up_p,
                                                                message=f"Đang tải video lồng tiếng lên storage.to ({pct:.0f}%)...",
                                                            )

                                                        dub_vid_storage_res = upload_file_to_storage_to(
                                                            dest_dubbed_video,
                                                            api_token=storage_api_token,
                                                            on_progress=_upload_dub_vid_cb,
                                                        )
                                                        dubbed_video_url = dub_vid_storage_res.get("url")
                                                        storage_info["dubbed_video"] = dub_vid_storage_res
                                                        update_task(
                                                            phase=phase_4,
                                                            progress=99.0,
                                                            dubbed_video_url=dubbed_video_url,
                                                            message="Đã tải video lồng tiếng lên storage.to",
                                                        )
                                                    except Exception as dub_vid_up_err:
                                                        _safe_log(f"[Storage.to Error] Lỗi tải video lồng tiếng: {dub_vid_up_err}")
                                                        update_task(upload_dubbed_video_error=str(dub_vid_up_err))
                                        except Exception as render_err:
                                            _safe_log(f"[Dubbing Render Error] Lỗi render video lồng tiếng: {render_err}")
                                            update_task(render_dubbed_video_error=str(render_err))

                                except Exception as dub_err:
                                    _safe_log(f"[Dubbing Error] Lỗi trong quá trình lồng tiếng VieNeu-TTS: {dub_err}")
                                    update_task(dubbing_error=str(dub_err))

                    except Exception as trans_err:
                        _safe_log(f"[Translation Error] Lỗi dịch phụ đề tiếng Việt: {trans_err}")
                        update_task(translation_error=str(trans_err))

            except Exception as asr_err:
                _safe_log(f"[CapCut ASR Error] Lỗi nhận diện CapCut ASR: {asr_err}")
                update_task(asr_error=str(asr_err))

        final_msg = "Ghép video thành công!"
        if video_url and dubbed_video_url:
            final_msg = "Ghép video, dịch phụ đề & lồng tiếng VieNeu-TTS thành công! Đã tải lên storage.to"
        elif video_url and translated_srt_url:
            final_msg = "Ghép video & dịch phụ đề tiếng Việt thành công! Đã tải lên storage.to"
        elif video_url and srt_url:
            final_msg = "Ghép video & trích xuất phụ đề thành công! Đã tải lên storage.to"
        elif video_url:
            final_msg = "Ghép video thành công! Đã tải lên storage.to"

        global LAST_MERGE_RESULT
        result_dict = {
            "task_id": task_id,
            "output_path": str(output_path),
            "video_url": video_url,
            "srt_url": srt_url,
            "srt_path": srt_path,
            "translated_srt_url": translated_srt_url,
            "translated_srt_path": translated_srt_path,
            "dubbed_audio_url": dubbed_audio_url,
            "dubbed_audio_path": dubbed_audio_path,
            "dubbed_video_url": dubbed_video_url,
            "dubbed_video_path": dubbed_video_path,
            "storage_info": storage_info,
            "output_size": out_size,
            "output_size_str": format_size(out_size),
        }
        with MERGE_LOCK:
            LAST_MERGE_RESULT = dict(result_dict)
            options.update(result_dict)

        final_phase = phase_4 if enable_dubbing else (phase_2 if generate_subtitles else phase_1)
        update_task(
            phase=final_phase,
            status="done",
            progress=100,
            eta="0s",
            message=final_msg,
            video_url=video_url,
            srt_url=srt_url,
            srt_path=srt_path,
            translated_srt_url=translated_srt_url,
            translated_srt_path=translated_srt_path,
            dubbed_audio_url=dubbed_audio_url,
            dubbed_audio_path=dubbed_audio_path,
            dubbed_video_url=dubbed_video_url,
            dubbed_video_path=dubbed_video_path,
            storage_info=storage_info,
            output_size=out_size,
            output_size_str=format_size(out_size),
        )

    except Exception as exc:
        is_cancel = task.get("cancelled") or "hủy bỏ" in str(exc)
        update_task(
            status="cancelled" if is_cancel else "error",
            progress=0,
            message=str(exc),
        )
        # Cleanup incomplete output file on error or cancel
        if "output_path" in task and Path(task["output_path"]).exists():
            try:
                Path(task["output_path"]).unlink(missing_ok=True)
            except Exception:
                pass
    finally:
        pass


def start_merge_task(files: List[str], options: Dict[str, Any]) -> str:
    """Start background video merge task and return task_id."""
    task_id = uuid.uuid4().hex
    with MERGE_LOCK:
        MERGE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "progress": 0,
            "speed": "-",
            "eta": "-",
            "message": "Đang khởi tạo tác vụ ghép video...",
            "created_at": time.time(),
            "cancelled": False,
            "files": files,
            "options": options,
        }

    thread = threading.Thread(
        target=execute_merge_job,
        args=(task_id, files, options),
        daemon=True,
    )
    thread.start()
    return task_id


def execute_online_merge_job(task_id: str, video_ids: List[str], options: Dict[str, Any]) -> None:
    """Download episodes to temporary folder and merge them seamlessly."""
    with MERGE_LOCK:
        task = MERGE_TASKS.get(task_id)
        if not task:
            return

    def update_task(**kwargs):
        with MERGE_LOCK:
            if task_id in MERGE_TASKS:
                MERGE_TASKS[task_id].update(kwargs)

    temp_dir = None
    try:
        update_task(status="running", progress=0, message=f"Đang phân tích thông tin {len(video_ids)} tập...")
        import importlib
        parser_module = importlib.import_module("1")

        # 1. Resolve stream infos for all episodes
        stream_infos = parser_module.resolve_batch_stream_infos(video_ids)
        if not stream_infos:
            raise ValueError("Không thể lấy thông tin giải mã các tập video.")

        # Create temporary working directory
        temp_dir = Path(get_runtime_base_dir() / "temp_online_merge" / task_id).resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)

        downloaded_files = []
        total_vids = len(stream_infos)

        # 2. Multi-threaded download and decrypt each episode to temp_dir
        concurrency = int(options.get("concurrency") or options.get("threads") or options.get("workers") or 5)
        concurrency = max(1, min(concurrency, 16))

        update_task(
            message=f"Đang tải đa luồng {total_vids} tập ({concurrency} luồng đồng thời)...",
            progress=5,
        )

        from concurrent.futures import ThreadPoolExecutor, as_completed
        dl_lock = threading.Lock()
        downloaded_map: Dict[int, str] = {}
        completed_count = 0

        def _download_stream_worker(idx: int, sinfo: Dict[str, Any]) -> str:
            nonlocal completed_count
            with MERGE_LOCK:
                if MERGE_TASKS.get(task_id, {}).get("cancelled"):
                    return ""

            vid = sinfo.get("video_id") or f"ep_{idx}"
            target_file = temp_dir / f"ep_{idx:03d}_{vid}.mp4"
            content_key = bytes.fromhex(sinfo["content_key_hex"]) if sinfo.get("content_key_hex") else None

            parser_module.stream_copy_video_with_ffmpeg(
                request_or_domain="http://127.0.0.1",
                video_url=sinfo["url"],
                content_key=content_key,
                filename=target_file.name,
                save_dir=str(temp_dir),
            )

            if not (target_file.exists() and target_file.stat().st_size > 0):
                raise RuntimeError(f"Tải tập {idx} ({vid}) thất bại (file rỗng hoặc không tải được).")

            with dl_lock:
                downloaded_map[idx] = str(target_file)
                completed_count += 1
                prog = round(5 + ((completed_count / total_vids) * 30), 1)  # 5% to 35%
                update_task(
                    message=f"Đang tải đa luồng ({concurrency} luồng): {completed_count}/{total_vids} tập hoàn tất...",
                    progress=prog,
                )
            return str(target_file)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_idx = {
                executor.submit(_download_stream_worker, idx, sinfo): idx
                for idx, sinfo in enumerate(stream_infos, 1)
            }
            for fut in as_completed(future_to_idx):
                with MERGE_LOCK:
                    if MERGE_TASKS.get(task_id, {}).get("cancelled"):
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")
                fut.result()

        # Sort files in strict chronological order 1..N
        downloaded_files = [downloaded_map[i] for i in range(1, total_vids + 1) if i in downloaded_map]
        if len(downloaded_files) != total_vids:
            raise RuntimeError(f"Chỉ tải thành công {len(downloaded_files)}/{total_vids} tập.")

        # 3. Now merge the downloaded files using the standard merge pipeline
        update_task(message="Đang tiến hành ghép các tập đã tải về...", progress=35)

        # Delegate to merge job
        execute_merge_job(task_id, downloaded_files, options)

    except Exception as exc:
        is_cancel = task.get("cancelled") or "hủy bỏ" in str(exc)
        update_task(
            status="cancelled" if is_cancel else "error",
            progress=0,
            message=str(exc),
        )
    finally:
        # Cleanup temporary files
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


def start_online_merge_task(video_ids: List[str], options: Dict[str, Any]) -> str:
    """Start background online video merge task and return task_id."""
    task_id = uuid.uuid4().hex
    with MERGE_LOCK:
        MERGE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "progress": 0,
            "speed": "-",
            "eta": "-",
            "message": "Đang khởi tạo tác vụ ghép video trực tuyến...",
            "created_at": time.time(),
            "cancelled": False,
            "video_ids": video_ids,
            "options": options,
        }

    thread = threading.Thread(
        target=execute_online_merge_job,
        args=(task_id, video_ids, options),
        daemon=True,
    )
    thread.start()
    return task_id


def merge_videos_sync(
    files: List[str],
    options: Dict[str, Any],
    progress_callback: Optional[Callable[..., None]] = None,
) -> Path:
    """Synchronous video merge function designed for CLI and scripts with real-time progress callbacks."""
    task_id = uuid.uuid4().hex
    with MERGE_LOCK:
        MERGE_TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "progress": 0,
            "speed": "-",
            "eta": "-",
            "message": "Đang chuẩn bị ghép video...",
            "created_at": time.time(),
            "cancelled": False,
            "files": files,
            "options": options,
        }

    def _runner():
        execute_merge_job(task_id, files, options)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    last_pct = -1.0
    last_msg = ""
    last_phase = ""
    while thread.is_alive():
        with MERGE_LOCK:
            st = dict(MERGE_TASKS.get(task_id, {}))
            options.update(st)
        cur_pct = float(st.get("progress") or 0.0)
        speed = str(st.get("speed") or "-")
        msg = str(st.get("message") or "")
        phase = str(st.get("phase") or "")
        if progress_callback and (cur_pct != last_pct or msg != last_msg or phase != last_phase or cur_pct == 100):
            last_pct = cur_pct
            last_msg = msg
            last_phase = phase
            try:
                progress_callback(cur_pct, speed, msg, phase)
            except TypeError:
                progress_callback(cur_pct, speed, msg)
        time.sleep(0.15)

    thread.join(timeout=2.0)

    with MERGE_LOCK:
        final_st = dict(MERGE_TASKS.get(task_id, {}))

    # Propagate all result attributes back to options dict
    options.update(final_st)

    if final_st.get("status") == "error":
        raise RuntimeError(final_st.get("message") or "Lỗi ghép video")

    out_p = final_st.get("output_path")
    if out_p and Path(out_p).exists():
        return Path(out_p)
    raise RuntimeError(final_st.get("message") or "Không tạo được file video đầu ra")


def download_series_episodes_concurrent(
    episodes: List[Dict[str, Any]],
    series_id: str,
    save_folder: Path,
    clean_name: str,
    concurrency: int = 5,
    on_episode_finished: Optional[Callable[[int, int, Dict[str, Any], bool, Optional[str]], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Path], int, int]:
    """
    Download a list of episodes concurrently with ThreadPoolExecutor,
    guaranteeing strict 1..N chronological ordering of the returned downloaded_files list.
    Returns: (downloaded_files_ordered, success_count, fail_count)
    """
    import importlib
    parser_module = importlib.import_module("1")

    concurrency = max(1, min(int(concurrency or 5), 16))
    save_folder = Path(save_folder).resolve()
    save_folder.mkdir(parents=True, exist_ok=True)

    # 1. Pre-resolve batch video models for fast connection reuse
    vids = [ep.get("vid") for ep in episodes if ep.get("vid")]
    if vids:
        try:
            parser_module.resolve_batch_video_models(vids, batch_size=30)
        except Exception:
            pass

    downloaded_map: Dict[int, Path] = {}
    lock = threading.Lock()
    success_count = 0
    fail_count = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _worker(ep: Dict[str, Any]) -> Tuple[int, Optional[Path], bool, Optional[str]]:
        if is_cancelled and is_cancelled():
            return ep.get("episode_num", 1), None, False, "Cancelled"

        vid = ep.get("vid")
        ep_num = int(ep.get("episode_num") or ep.get("index") or 1)
        filename = f"{clean_name}_Tap_{ep_num:03d}.mp4"
        file_path = save_folder / filename

        # Skip existing non-empty file
        if file_path.exists() and file_path.stat().st_size > 50000:
            return ep_num, file_path, True, None

        try:
            parser_module.handle_video_request(
                vid,
                series_id=series_id,
                episode=ep_num,
                filename=filename,
                save_dir=str(save_folder),
            )
            if file_path.exists() and file_path.stat().st_size > 0:
                return ep_num, file_path, True, None
            else:
                return ep_num, None, False, "File rỗng sau khi tải"
        except Exception as exc:
            return ep_num, None, False, str(exc)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_worker, ep) for ep in episodes]
        for fut in as_completed(futures):
            if is_cancelled and is_cancelled():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                ep_num, fpath, ok, err_msg = fut.result()
                with lock:
                    if ok and fpath:
                        downloaded_map[ep_num] = fpath
                        success_count += 1
                    else:
                        fail_count += 1
                    if on_episode_finished:
                        total = len(episodes)
                        current_done = success_count + fail_count
                        ep_item = next((e for e in episodes if int(e.get("episode_num") or e.get("index") or 0) == ep_num), {})
                        on_episode_finished(current_done, total, ep_item, ok, err_msg)
            except Exception:
                with lock:
                    fail_count += 1

    # Preserve exact order
    downloaded_files = []
    for ep in episodes:
        num = int(ep.get("episode_num") or ep.get("index") or 0)
        if num in downloaded_map:
            downloaded_files.append(downloaded_map[num])

    return downloaded_files, success_count, fail_count



