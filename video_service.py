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


def detect_available_gpu_encoders() -> Dict[str, Any]:
    """Dynamically test and detect available hardware GPU encoders (NVIDIA NVENC, Intel QSV, AMD AMF, Apple VideoToolbox, etc.)."""
    global _DETECTED_GPU_ENCODERS
    if _DETECTED_GPU_ENCODERS is not None:
        return _DETECTED_GPU_ENCODERS

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
            test_cmd = [
                ffmpeg_bin, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.05",
                "-c:v", encoder_name, "-f", "null", "-"
            ]
            t_res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo, timeout=3)
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

    h264_list = []
    hevc_list = []

    if has_nvenc_h264:
        h264_list.append({"name": "h264_nvenc", "label": "NVIDIA NVENC (GPU)"})
    if has_nvenc_hevc:
        hevc_list.append({"name": "hevc_nvenc", "label": "NVIDIA NVENC (GPU)"})

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
        primary = "NVIDIA NVENC (GPU)"
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
            "-preset", "p6",
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


def get_task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve current merge task status."""
    with MERGE_LOCK:
        task = MERGE_TASKS.get(task_id)
        if task:
            return dict(task)
    return None


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
) -> str:
    """Generate FFmpeg filter_complex script content with universal size/SAR/audio normalization and frame-accurate sync."""
    filter_parts = []
    concat_inputs = []

    fps_part = f",fps={target_fps}" if target_fps else ""
    mirror_part = ",hflip" if mirror else ""
    # Safe scale and pad filter: guarantees 100% exact size, SAR 1:1, pixel format yuv420p, sharp Lanczos scaling
    video_norm_filter = (
        f",scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,format=yuv420p"
    )

    for idx, (f, info) in enumerate(zip(batch_files, batch_infos)):
        dur = info["effective_duration"]
        has_audio = info.get("has_audio", False)

        # Video stream: setpts, scale/pad/sar/format normalization, fps, mirror, color
        if cut_end_seconds > 0:
            v_filter = f"trim=start=0:duration={dur:.3f},setpts=PTS-STARTPTS{video_norm_filter}{fps_part}{mirror_part}{color_filter_str}"
        else:
            v_filter = f"setpts=PTS-STARTPTS{video_norm_filter}{fps_part}{mirror_part}{color_filter_str}"
        filter_parts.append(f"[{idx}:v]{v_filter}[v{idx}]")

        # Audio stream: setpts, resample with async sync, format stereo 44100Hz
        if has_audio:
            if cut_end_seconds > 0:
                a_filter = f"atrim=start=0:duration={dur:.3f},asetpts=PTS-STARTPTS,aresample=44100:async=1000:first_pts=0,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{audio_filter_str}"
            else:
                a_filter = f"asetpts=PTS-STARTPTS,aresample=44100:async=1000:first_pts=0,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo{audio_filter_str}"
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
    Configure video encoder and bitrate/CRF flags for Balanced High Quality (CQ 14 / CRF 14).
    Provides crisp sharpness while keeping file size reasonably compact.
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

    # Exactly 3x boost of original video bitrate for clean sharpness
    target_video_kbps = max(1800, int(orig_video_bps * 3.00) // 1000)
    maxrate_kbps = max(3000, int(orig_video_bps * 5.00) // 1000)
    bufsize_kbps = max(3600, int(orig_video_bps * 6.00) // 1000)

    bitrate_flags = []
    if "nvenc" in chosen_encoder:
        bitrate_flags.extend([
            "-cq:v", "14",                     # High Quality Balanced CQ
            "-b:v", f"{target_video_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
            "-rc-lookahead", "32",              # 32-frame lookahead
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
) -> None:
    """Encode all clips in a single direct pass using -filter_complex_script for 100% stability & high quality."""
    script_content = generate_batch_filter_script(
        batch_files, batch_infos, target_w, target_h, target_fps, color_filter_str, audio_filter_str, cut_end_seconds, mirror=mirror
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

    cmd.extend([
        "-threads", "0",
        "-progress", "pipe:1",
        str(batch_output_file),
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
                    pct = min(99.0, max(0.0, (cur_overall_sec / total_effective_dur) * 100.0))
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

    finally:
        try:
            if script_file.exists():
                script_file.unlink(missing_ok=True)
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
                effective_dur = max(0.5, dur - cut_end_seconds) if dur > cut_end_seconds else max(0.2, dur * 0.5)
            else:
                effective_dur = max(0.2, dur)
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
        )

        ffmpeg_bin = get_ffmpeg_binary()

        # Direct Single-Pass Video & Audio Pipeline (100% A/V Sync, No Glitches)
        # Determine target resolution & FPS to guarantee uniform input pads to concat filter
        target_w, target_h = determine_target_resolution(probed_infos, resolution, custom_resolution)
        target_fps = determine_target_fps(probed_infos, fps)

        gpu_pref = str(options.get("gpu", "nvenc")).lower()
        chosen_encoder, enc_flags, bitrate_flags, display_label = build_encoding_args(
            codec=codec,
            gpu_pref=gpu_pref,
            probed_infos=probed_infos,
        )

        update_task(
            message=f"Đang ghép 1 lần trực tiếp ({len(valid_files)} video) với {display_label} ({target_w}x{target_h})...",
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
            )
        except Exception as encode_err:
            err_text = str(encode_err)
            if any(k in err_text.lower() for k in ["mfx", "qsv", "nvenc", "cuda", "amf", "videotoolbox", "opening encoder", "encoder for output stream"]):
                print(f"[Encoder Fallback] GPU encoder failed ({err_text[:100]}). Falling back to CPU libx264...")
                update_task(
                    message="Bộ mã hóa GPU không khởi động được. Đang tự động chuyển sang CPU (libx264) để hoàn tất ghép video...",
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
                )
            else:
                raise encode_err

        with MERGE_LOCK:
            task_status = MERGE_TASKS.get(task_id, {})
        if task_status.get("cancelled"):
            raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")

        out_size = output_path.stat().st_size if output_path.exists() else 0
        update_task(
            status="done",
            progress=100,
            eta="0s",
            message="Ghép video thành công!",
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

        # 2. Download and decrypt each episode to temp_dir
        for idx, sinfo in enumerate(stream_infos, 1):
            with MERGE_LOCK:
                if MERGE_TASKS.get(task_id, {}).get("cancelled"):
                    raise RuntimeError("Tiến trình đã bị người dùng hủy bỏ.")

            vid = sinfo.get("video_id") or f"ep_{idx}"
            update_task(
                message=f"Đang tải & giải mã tập {idx}/{total_vids}...",
                progress=round(((idx - 1) / total_vids) * 35, 1)  # 0-35% for download phase
            )

            target_file = temp_dir / f"ep_{idx:03d}_{vid}.mp4"

            content_key = bytes.fromhex(sinfo["content_key_hex"]) if sinfo.get("content_key_hex") else None
            parser_module.stream_copy_video_with_ffmpeg(
                request_or_domain="http://127.0.0.1",
                video_url=sinfo["url"],
                content_key=content_key,
                filename=target_file.name,
                save_dir=str(temp_dir),
            )
            if target_file.exists() and target_file.stat().st_size > 0:
                downloaded_files.append(str(target_file))
            else:
                raise RuntimeError(f"Tải tập {idx} thất bại (file rỗng hoặc không tồn tại).")

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
    progress_callback: Optional[Callable[[float, str, str], None]] = None,
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
    while thread.is_alive():
        with MERGE_LOCK:
            st = dict(MERGE_TASKS.get(task_id, {}))
        cur_pct = float(st.get("progress") or 0.0)
        speed = str(st.get("speed") or "-")
        msg = str(st.get("message") or "")
        if progress_callback and (cur_pct != last_pct or cur_pct == 100):
            last_pct = cur_pct
            progress_callback(cur_pct, speed, msg)
        time.sleep(0.15)

    thread.join(timeout=2.0)

    with MERGE_LOCK:
        final_st = dict(MERGE_TASKS.get(task_id, {}))

    if final_st.get("status") == "error":
        raise RuntimeError(final_st.get("message") or "Lỗi ghép video")

    out_p = final_st.get("output_path")
    if out_p and Path(out_p).exists():
        return Path(out_p)
    raise RuntimeError(final_st.get("message") or "Không tạo được file video đầu ra")



