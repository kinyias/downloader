"""
VieNeu-TTS Integration & Audio Alignment Pipeline for Video Dubbing.

Provides:
- VieNeuTTS client wrapper (v3 Turbo 48kHz, preset voices, CPU/GPU auto-detection)
- Duration measurement via ffprobe/ffmpeg
- Intelligent audio alignment & gap allocation strategy:
    * Audio is allowed to be longer than subtitle duration by utilizing available silence gaps
      between utterances, while strictly never colliding with previous or next audio clips.
    * Speed up audio up to 1.2x (via ffmpeg atempo) if audio exceeds subtitle duration.
    * Center padding (fill silence on both sides equally) if audio is shorter than subtitle.
- Strict deficit verification (> 0.3s):
    * Only if after utilizing all available space without touching adjacent audio and speeding up 1.2x,
      the space is STILL lacking by > 0.3s for the audio:
        - Attempt 1: Regenerate audio once with VieNeu-TTS
        - Attempt 2: Re-translate / condense text to be strictly shorter via LLM, then regenerate
- Full dubbing master track builder matching video timeline exactly
- Video dubbing renderer with background audio modification:
    * Original audio volume reduced to 0.25
    * Periodic mute: every 0.9s mute for 0.1s (1.0s cycle)
    * Pitch down 5% (via rubberband or asetrate/atempo fallback)
    * Multiplexed & mixed with dubbing audio
"""

import os
import sys
import json
import time
import math
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

# Preset voices available in VieNeu-TTS v3 Turbo
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VIENEU_PRESET_VOICES = [
    # Mặc định
    {"id": "Ngọc Huyền", "name": "Ngọc Huyền (Nữ - Truyền cảm, Mặc định)", "gender": "female", "region": "north"},
    # Northern (Bắc)
    {"id": "Minh Quân", "name": "Minh Quân (Nam Bắc - Chuẩn)", "gender": "male", "region": "north"},
    {"id": "Minh Đức", "name": "Minh Đức (Nam Bắc - Trầm ấm)", "gender": "male", "region": "north"},
    {"id": "Phạm Tuyên", "name": "Phạm Tuyên (Nam Bắc - Truyền cảm)", "gender": "male", "region": "north"},
    {"id": "Trúc Ly", "name": "Trúc Ly (Nữ Bắc - Nhẹ nhàng)", "gender": "female", "region": "north"},
    {"id": "Mai Anh", "name": "Mai Anh (Nữ Bắc - Tươi sáng)", "gender": "female", "region": "north"},
    {"id": "Quỳnh Anh", "name": "Quỳnh Anh (Nữ Bắc - Thanh thoát)", "gender": "female", "region": "north"},
    {"id": "Xuân Vĩnh", "name": "Xuân Vĩnh (Nam Bắc - Rõ ràng)", "gender": "male", "region": "north"},
    {"id": "Anh Khôi", "name": "Anh Khôi (Nam Bắc - Trẻ trung)", "gender": "male", "region": "north"},
    {"id": "Mạnh Dũng", "name": "Mạnh Dũng (Nam Bắc - Hùng hồn)", "gender": "male", "region": "north"},
    # Central (Trung)
    {"id": "Quang Sơn", "name": "Quang Sơn (Nam Trung - Truyền cảm)", "gender": "male", "region": "central"},
    {"id": "Ngọc Trân", "name": "Ngọc Trân (Nữ Trung - Ngọt ngào)", "gender": "female", "region": "central"},
    # Southern (Nam)
    {"id": "Adam", "name": "Adam (Nam Nam - Hiện đại)", "gender": "male", "region": "south"},
    {"id": "Thái Sơn", "name": "Thái Sơn (Nam Nam - Đĩnh đạc)", "gender": "male", "region": "south"},
    {"id": "Thùy Dung", "name": "Thùy Dung (Nữ Nam - Dịu dàng)", "gender": "female", "region": "south"},
    {"id": "Mỹ Duyên", "name": "Mỹ Duyên (Nữ Nam - Trong trẻo)", "gender": "female", "region": "south"},
]

DEFAULT_VOICE = "Ngọc Huyền"


def _log(msg: str):
    """Log with timestamp."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"[{now_str}] [VieNeu-TTS] {msg}", flush=True)
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", "backslashreplace").decode("ascii")
        print(f"[{now_str}] [VieNeu-TTS] {safe_msg}", flush=True)


def get_ffmpeg_bin() -> str:
    """Resolve ffmpeg binary executable."""
    for b in [os.getenv("FFMPEG_BIN"), "ffmpeg"]:
        if b and shutil.which(b):
            return b
    return "ffmpeg"


def get_ffprobe_bin() -> str:
    """Resolve ffprobe binary executable."""
    for b in [os.getenv("FFPROBE_BIN"), "ffprobe"]:
        if b and shutil.which(b):
            return b
    return "ffprobe"


def get_audio_duration_ffprobe(file_path: Path | str) -> float:
    """
    Measure exact duration of an audio file using ffprobe.
    Falls back to ffmpeg decode duration if ffprobe is unavailable.
    """
    p = Path(file_path).resolve()
    if not p.exists() or p.stat().st_size == 0:
        return 0.0

    ffprobe = get_ffprobe_bin()
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass

    # Fallback to ffmpeg null muxer
    ffmpeg = get_ffmpeg_bin()
    cmd_fb = [ffmpeg, "-i", str(p), "-f", "null", "-"]
    try:
        res = subprocess.run(cmd_fb, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        import re
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", res.stderr)
        if m:
            h, m_val, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + m_val * 60 + s
    except Exception:
        pass

    return 0.0


def create_silent_audio(out_path: Path | str, duration_sec: float, sample_rate: int = 44100) -> Path:
    """Generate a valid silent WAV audio file using ffmpeg or direct PCM."""
    out_p = Path(out_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = max(0.01, float(duration_sec))

    ffmpeg = get_ffmpeg_bin()
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{duration_sec:.4f}",
        "-c:a", "pcm_s16le",
        str(out_p),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        # Fallback binary PCM header
        num_samples = int(sample_rate * duration_sec)
        data_size = num_samples * 2
        with open(out_p, "wb") as f:
            f.write(b"RIFF")
            f.write((36 + data_size).to_bytes(4, "little"))
            f.write(b"WAVEfmt ")
            f.write((16).to_bytes(4, "little"))
            f.write((1).to_bytes(2, "little"))  # PCM
            f.write((1).to_bytes(2, "little"))  # 1 channel
            f.write(sample_rate.to_bytes(4, "little"))
            f.write((sample_rate * 2).to_bytes(4, "little"))
            f.write((2).to_bytes(2, "little"))
            f.write((16).to_bytes(2, "little"))
            f.write(b"data")
            f.write(data_size.to_bytes(4, "little"))
            f.write(b"\x00" * data_size)
    return out_p


def _apply_audio_speed(audio_path: Path | str, speed: float) -> None:
    """Adjust playback speed of an audio file using ffmpeg atempo."""
    p = Path(audio_path).resolve()
    if not p.exists() or p.stat().st_size == 0 or abs(speed - 1.0) <= 0.03:
        return
    sped_temp = p.with_name(f"{p.stem}_speed_tmp.wav")
    ffmpeg = get_ffmpeg_bin()
    cmd = [
        ffmpeg, "-y", "-i", str(p),
        "-af", f"atempo={speed:.3f}",
        str(sped_temp)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0 and sped_temp.exists():
        sped_temp.replace(p)


class VieNeuTTS:
    """
    Wrapper for VieNeu-TTS SDK (v3 Turbo 48kHz, on-device, instant voice cloning).
    Gracefully handles environment initialization and provides fallback if library not installed.
    Supports GPU-accelerated batch inference via infer_batch.
    """
    _instance = None

    def __init__(self, mode: str = "v3turbo", voice: str = DEFAULT_VOICE, backend: Optional[str] = None):
        self.mode = mode
        self.voice = voice or DEFAULT_VOICE
        self.backend = backend
        self._vieneu_client = None
        self._is_available = False
        self._init_engine()

    def _init_engine(self):
        """Attempt to initialize Vieneu SDK client."""
        try:
            from vieneu import Vieneu  # type: ignore
            _log(f"Đang nạp mô hình VieNeu-TTS (mode='{self.mode}', backend={self.backend or 'auto'})...")
            kwargs = {"mode": self.mode}
            if self.backend:
                kwargs["backend"] = self.backend
            self._vieneu_client = Vieneu(**kwargs)
            self._is_available = True
            _log(f"✅ Đã khởi tạo VieNeu-TTS thành công (Giọng mặc định: '{self.voice}')!")
        except ImportError:
            self._vieneu_client = None
            self._is_available = False
            _log("⚠️ Chưa cài đặt thư viện 'vieneu'. Hãy chạy: pip install vieneu")
        except Exception as exc:
            self._vieneu_client = None
            self._is_available = False
            _log(f"⚠️ Lỗi khởi tạo mô hình VieNeu-TTS: {exc}")

    @property
    def is_available(self) -> bool:
        return self._is_available and self._vieneu_client is not None

    def infer_batch(self, texts: List[str], voice: Optional[str] = None, batch_size: int = 30) -> List[Any]:
        """
        Directly invoke Vieneu infer_batch to leverage GPU parallel forward passes.
        Returns list of audio waveforms (numpy arrays).
        """
        if not self.is_available:
            raise RuntimeError("VieNeu-TTS client is not initialized or unavailable.")
        v = voice or self.voice or DEFAULT_VOICE
        bs = max(1, int(batch_size or 30))
        return self._vieneu_client.infer_batch(
            texts=texts,
            voice=v,
            batch_size=bs,
        )

    def synthesize(self, text: str, out_path: Path | str, voice: Optional[str] = None, speed: float = 1.0) -> Path:
        """
        Synthesize speech from text and save to out_path.
        """
        out_p = Path(out_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        v = voice or self.voice or DEFAULT_VOICE
        clean_text = str(text or "").strip()

        if not clean_text:
            return create_silent_audio(out_p, 0.5)

        if not self.is_available:
            _log(f"⚠️ VieNeu-TTS chưa sẵn sàng. Tạo audio giả lập cho phân đoạn: '{clean_text[:40]}...'")
            est_dur = max(0.8, len(clean_text.split()) * 0.28)
            return create_silent_audio(out_p, est_dur)

        try:
            # Call Vieneu inference
            audio = self._vieneu_client.infer(clean_text, voice=v)
            self._vieneu_client.save(audio, str(out_p))

            # Apply custom base speed if speed != 1.0
            if abs(speed - 1.0) > 0.03:
                _apply_audio_speed(out_p, speed)

            return out_p
        except Exception as e:
            _log(f"❌ Lỗi khi synthesize qua VieNeu-TTS: {e}")
            est_dur = max(0.8, len(clean_text.split()) * 0.28)
            return create_silent_audio(out_p, est_dur)

    def synthesize_batch(
        self,
        items: List[Tuple[str, Path | str]],
        voice: Optional[str] = None,
        batch_size: int = 30,
        speed: float = 1.0,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Path]:
        """
        Synthesize speech from multiple texts in batches using Vieneu SDK's `infer_batch`
        to maximize GPU utilization and throughput.

        - Divides items into batches of `batch_size` (default: 30 segments).
        - Runs neural forward passes simultaneously across segments on GPU.
        - Automatically writes output audio files and applies speed adjustment if needed.
        - Gracefully falls back to sequential synthesize if infer_batch errors.
        """
        if not items:
            return []

        v = voice or self.voice or DEFAULT_VOICE
        results: List[Path] = []
        total_items = len(items)
        bs = max(1, int(batch_size or 30))

        # Check if batch inference is supported by engine
        has_infer_batch = self.is_available and hasattr(self._vieneu_client, "infer_batch")

        for start_idx in range(0, total_items, bs):
            chunk = items[start_idx : start_idx + bs]
            chunk_paths: List[Path] = []

            if not has_infer_batch:
                # Fallback: Sequential synthesis
                for text, out_p in chunk:
                    p = self.synthesize(text, out_p, voice=v, speed=speed)
                    chunk_paths.append(p)
                results.extend(chunk_paths)
                if on_progress:
                    on_progress(min(start_idx + bs, total_items), total_items)
                continue

            # Separate non-empty texts to batch infer
            valid_indices: List[int] = []
            valid_texts: List[str] = []
            prepared_paths: List[Path] = []

            for rel_idx, (text, out_path) in enumerate(chunk):
                out_p = Path(out_path).resolve()
                out_p.parent.mkdir(parents=True, exist_ok=True)
                prepared_paths.append(out_p)
                clean_text = str(text or "").strip()
                if clean_text:
                    valid_indices.append(rel_idx)
                    valid_texts.append(clean_text)
                else:
                    create_silent_audio(out_p, 0.5)

            if valid_texts:
                try:
                    end_idx = min(start_idx + len(chunk), total_items)
                    _log(f"⚡ Đang infer_batch {len(valid_texts)} câu trên GPU (Batch {start_idx + 1}-{end_idx}/{total_items}, batch_size={bs})...")
                    waveforms = self._vieneu_client.infer_batch(
                        texts=valid_texts,
                        voice=v,
                        batch_size=min(len(valid_texts), bs),
                    )

                    for idx_in_valid, orig_rel_idx in enumerate(valid_indices):
                        out_p = prepared_paths[orig_rel_idx]
                        txt = valid_texts[idx_in_valid]
                        audio_wave = waveforms[idx_in_valid] if idx_in_valid < len(waveforms) else None

                        if audio_wave is not None and len(audio_wave) > 0:
                            self._vieneu_client.save(audio_wave, str(out_p))
                            if abs(speed - 1.0) > 0.03:
                                _apply_audio_speed(out_p, speed)
                        else:
                            est_dur = max(0.8, len(txt.split()) * 0.28)
                            create_silent_audio(out_p, est_dur)

                except Exception as exc:
                    _log(f"⚠️ infer_batch gặp lỗi ({exc}). Fallback sang synthesize tuần tự cho batch này...")
                    for orig_rel_idx in valid_indices:
                        txt, out_path = chunk[orig_rel_idx]
                        self.synthesize(txt, out_path, voice=v, speed=speed)

            results.extend(prepared_paths)
            if on_progress:
                on_progress(min(start_idx + bs, total_items), total_items)

        return results


def calculate_segment_timing_and_excess(
    raw_dur: float,
    seg_start: float,
    seg_end: float,
    prev_audio_end: float = 0.0,
    next_seg_start: Optional[float] = None,
    max_speedup: float = 1.2,
    tolerance: float = 0.3,
    min_gap: float = 0.05,
) -> Dict[str, Any]:
    """
    Tính toán chiến lược căn chỉnh audio theo không gian trống thực tế:
    - Audio có thể dài hơn subtitle nhưng tuyệt đối không chạm audio phía trước hoặc phía sau.
    - Không gian khả dụng được giới hạn giữa (prev_audio_end + min_gap) và (next_seg_start - min_gap).
    - Tăng tốc audio tối đa max_speedup (1.2x).
    - Chỉ khi sau khi đã tận dụng hết khoảng trống khả dụng và tăng tốc 1.2x mà vẫn thiếu > tolerance (0.3s)
      thì mới báo deficit > 0.3s để kích hoạt tạo lại audio hoặc rút gọn LLM.
    """
    target_dur = max(0.05, seg_end - seg_start)
    raw_dur = max(0.01, float(raw_dur))
    max_speedup = max(1.0, float(max_speedup or 1.2))
    tolerance = max(0.0, float(tolerance if tolerance is not None else 0.3))
    min_gap = max(0.0, float(min_gap if min_gap is not None else 0.05))

    earliest_start = (prev_audio_end + min_gap) if prev_audio_end > 0 else 0.0
    latest_end = (next_seg_start - min_gap) if (next_seg_start is not None and next_seg_start > 0) else (seg_end + 3600.0)

    # Đảm bảo latest_end không nhỏ hơn earliest_start
    if latest_end <= earliest_start:
        latest_end = earliest_start + target_dur

    # Trường hợp 1: Audio ngắn hơn hoặc bằng subtitle -> Căn giữa trong [seg_start, seg_end]
    if raw_dur <= target_dur:
        pad_total = target_dur - raw_dur
        pad_left = pad_total / 2.0
        actual_start = max(earliest_start, seg_start)
        actual_end = min(latest_end, actual_start + target_dur)
        return {
            "speed_factor": 1.0,
            "actual_start": actual_start,
            "actual_end": actual_end,
            "allocated_dur": max(0.05, actual_end - actual_start),
            "pad_left": pad_left,
            "deficit": 0.0,
            "needs_retry": False,
        }

    # Trường hợp 2: Audio dài hơn subtitle nhưng có thể vừa khít subtitle với speedup <= max_speedup
    desired_speed = raw_dur / target_dur
    if desired_speed <= max_speedup:
        actual_start = max(earliest_start, seg_start)
        actual_end = min(latest_end, actual_start + target_dur)
        return {
            "speed_factor": desired_speed,
            "actual_start": actual_start,
            "actual_end": actual_end,
            "allocated_dur": max(0.05, actual_end - actual_start),
            "pad_left": 0.0,
            "deficit": 0.0,
            "needs_retry": False,
        }

    # Trường hợp 3: Audio dài hơn subtitle vượt quá max_speedup (1.2x)
    # Tăng tốc tối đa 1.2x, thời lượng đạt được là sped_dur = raw_dur / max_speedup
    sped_dur = raw_dur / max_speedup

    # Audio có thể dài hơn subtitle nhưng không chạm audio phía trước hoặc phía sau
    # Thử đặt audio bắt đầu tại seg_start
    tentative_start = max(earliest_start, seg_start)
    tentative_end = tentative_start + sped_dur

    if tentative_end <= latest_end:
        # Vừa vặn không gian phía sau, không chạm audio sau
        actual_start = tentative_start
        actual_end = tentative_end
        deficit = 0.0
    else:
        # Bị tràn qua latest_end -> thử lùi nhẹ về phía trước vào khoảng trống trước seg_start
        shift = tentative_end - latest_end
        new_start = tentative_start - shift
        if new_start >= earliest_start:
            # Lùi về trước thành công mà không chạm audio trước!
            actual_start = new_start
            actual_end = latest_end
            deficit = 0.0
        else:
            # Kể cả lùi tối đa về earliest_start vẫn không đủ chỗ
            actual_start = earliest_start
            actual_end = latest_end
            avail_slot = max(0.05, actual_end - actual_start)
            deficit = max(0.0, sped_dur - avail_slot)

    needs_retry = deficit > tolerance
    return {
        "speed_factor": max_speedup,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "allocated_dur": max(0.05, actual_end - actual_start),
        "pad_left": 0.0,
        "deficit": deficit,
        "needs_retry": needs_retry,
    }


def align_and_pad_audio_segment(
    input_audio_path: Path | str,
    target_duration: float,
    output_audio_path: Path | str,
    speed_factor: float = 1.0,
    pad_left: float = 0.0,
) -> Tuple[Path, float]:
    """
    Căn chỉnh audio segment theo target_duration và cấu hình speed/pad đã tính toán:
    1. Tăng tốc bằng ffmpeg 'atempo' nếu speed_factor > 1.01.
    2. Chèn khoảng lặng bên trái (pad_left) nếu có (center-padding).
    3. Nếu thời lượng audio ngắn hơn target_duration: bổ sung khoảng lặng bên phải (apad) và trim về target_duration.
    4. Nếu thời lượng audio dài hơn target_duration (dư <= 0.3s): fade-out 0.05s ở đuôi và trim về target_duration.
    """
    in_p = Path(input_audio_path).resolve()
    out_p = Path(output_audio_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    target_dur = max(0.05, float(target_duration))

    if not in_p.exists() or in_p.stat().st_size == 0:
        create_silent_audio(out_p, target_dur)
        return out_p, target_dur

    orig_dur = get_audio_duration_ffprobe(in_p)
    if orig_dur <= 0.001:
        create_silent_audio(out_p, target_dur)
        return out_p, target_dur

    ffmpeg = get_ffmpeg_bin()
    current_audio = in_p
    current_dur = orig_dur

    # Bước 1: Áp dụng speedup nếu speed_factor > 1.01
    if speed_factor > 1.01:
        sped_p = out_p.with_name(f"{out_p.stem}_sped_{int(time.time()*1000)%10000}.wav")
        cmd_speed = [
            ffmpeg, "-y", "-i", str(current_audio),
            "-af", f"atempo={speed_factor:.4f}",
            str(sped_p)
        ]
        res = subprocess.run(cmd_speed, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and sped_p.exists():
            current_audio = sped_p
            current_dur = get_audio_duration_ffprobe(sped_p)

    # Bước 2: Padding & Trim để khớp chính xác target_dur
    pad_left_ms = max(0, int(round(pad_left * 1000)))
    if pad_left_ms > 5 or current_dur < target_dur:
        pad_right = max(0.0, target_dur - (current_dur + pad_left))
        if pad_left_ms > 5:
            af_filter = f"adelay={pad_left_ms}|{pad_left_ms},apad=pad_dur={pad_right + 0.5:.4f},atrim=0:{target_dur:.4f}"
        else:
            af_filter = f"apad=pad_dur={pad_right + 0.5:.4f},atrim=0:{target_dur:.4f}"
        cmd_pad = [
            ffmpeg, "-y", "-i", str(current_audio),
            "-af", af_filter,
            str(out_p)
        ]
        res = subprocess.run(cmd_pad, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0 or not out_p.exists():
            shutil.copyfile(current_audio, out_p)
    else:
        # Audio dài hơn hoặc bằng target_dur (dư <= 0.3s) -> fade-out 0.05s ở đuôi và trim
        fade_out_start = max(0.0, target_dur - 0.05)
        af_filter = f"atrim=0:{target_dur:.4f},afade=t=out:st={fade_out_start:.4f}:d=0.05"
        cmd_trim = [
            ffmpeg, "-y", "-i", str(current_audio),
            "-af", af_filter,
            str(out_p)
        ]
        res = subprocess.run(cmd_trim, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0 or not out_p.exists():
            shutil.copyfile(current_audio, out_p)

    # Dọn dẹp file tạm sped_p nếu có
    if current_audio != in_p and current_audio.exists():
        try:
            current_audio.unlink(missing_ok=True)
        except Exception:
            pass

    final_dur = get_audio_duration_ffprobe(out_p)
    return out_p, final_dur


def condense_segment_via_llm(
    segment: Dict[str, Any],
    target_duration: float,
    chat_url: str,
    headers: Dict[str, str],
    model: str = "gemini-lite",
) -> Optional[str]:
    """
    Rút gọn hoặc dịch lại câu thoại qua LLM để khớp với thời lượng khả dụng.
    Tính toán ngân sách âm tiết hợp lý (target_duration * 1.2 * 3.3).
    """
    import requests
    full_text = segment.get("spokenText") or segment.get("translation") or segment.get("text") or ""
    if not full_text:
        return None

    # Tính ngân sách âm tiết dựa trên thời lượng khả dụng với tốc độ nói tiếng Việt tự nhiên
    budget_syl = max(2, int(math.floor(target_duration * 1.2 * 3.3)))

    prompt = (
        f"Bạn là chuyên gia biên tập lồng tiếng phim. "
        f"Câu thoại sau hiện đang quá dài, thời lượng video khả dụng là khoảng {target_duration:.2f} giây. "
        f"Hãy dịch hoặc rút gọn câu này sang tiếng Việt thật tự nhiên, súc tích, "
        f"BẮT BUỘC KHÔNG VƯỢT QUÁ {budget_syl} ÂM TIẾT nhưng vẫn giữ được ý cốt lõi của câu thoại.\n\n"
        f"Câu gốc: {segment.get('text', '')}\n"
        f"Bản dịch hiện tại: {full_text}\n\n"
        f"Chỉ trả về DUY NHẤT câu tiếng Việt rút gọn, không thêm bất kỳ từ giải thích hay ký hiệu nào khác."
    )

    payload = {
        "model": model or "gemini-lite",
        "messages": [
            {"role": "system", "content": "Bạn là chuyên gia biên tập kịch bản lồng tiếng. Chỉ trả về câu thoại đã rút gọn."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "stream": False
    }

    try:
        resp = requests.post(chat_url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            res_json = resp.json()
            short_text = res_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            short_text = short_text.strip('"`\' \n')
            if short_text:
                return short_text
    except Exception as e:
        _log(f"⚠️ Lỗi khi gọi LLM rút gọn câu segment #{segment.get('id')}: {e}")

    return None


def process_segment_dubbing_with_retry(
    segment: Dict[str, Any],
    tts: VieNeuTTS,
    temp_dir: Path,
    voice: str = DEFAULT_VOICE,
    pre_generated_audio: Optional[Path | str] = None,
    prev_audio_end: float = 0.0,
    next_seg_start: Optional[float] = None,
    chat_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    model: str = "gemini-lite",
    max_speedup: float = 1.2,
    tolerance: float = 0.3,
    min_gap: float = 0.05,
    on_status: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, float, float]:
    """
    Xử lý lồng tiếng cho một phân đoạn phụ đề với chiến lược căn chỉnh không gian trống:
    1. Kiểm tra audio đã tạo sẵn từ batch GPU hoặc tổng hợp mới với VieNeu-TTS.
    2. Căn chỉnh audio theo không gian trống trước/sau và tăng tốc tối đa 1.2x.
    3. Nếu sau khi align mà vẫn thiếu > tolerance (0.3s):
       - Attempt 1: Tạo lại audio 1 lần nữa.
       - Attempt 2: Nếu vẫn thiếu > tolerance (0.3s): Gọi LLM rút gọn câu và tạo lại audio.
    4. Căn chỉnh chính xác và trả về: (aligned_audio_path, actual_start, actual_end).
    """
    s_id = str(segment.get("id", "0"))
    start_t = float(segment.get("startTime", 0.0))
    end_t = float(segment.get("endTime", 0.0))
    target_dur = max(0.05, end_t - start_t)

    text = str(segment.get("spokenText") or segment.get("subtitleText") or segment.get("translation") or "").strip()
    seg_raw_path = temp_dir / f"seg_{s_id}_raw.wav"
    seg_aligned_path = temp_dir / f"seg_{s_id}_aligned.wav"

    if not text:
        plan = calculate_segment_timing_and_excess(0.0, start_t, end_t, prev_audio_end, next_seg_start, max_speedup, tolerance, min_gap)
        create_silent_audio(seg_aligned_path, plan["allocated_dur"])
        return seg_aligned_path, plan["actual_start"], plan["actual_end"]

    # Bước 1: Sử dụng audio từ batch phase hoặc synthesize theo yêu cầu
    if pre_generated_audio and Path(pre_generated_audio).exists() and Path(pre_generated_audio).stat().st_size > 0:
        seg_raw_path = Path(pre_generated_audio).resolve()
    else:
        tts.synthesize(text, seg_raw_path, voice=voice)

    audio_dur = get_audio_duration_ffprobe(seg_raw_path)

    # Bước 2: Lập kế hoạch căn chỉnh và kiểm tra độ thiếu hụt thời gian
    plan = calculate_segment_timing_and_excess(
        raw_dur=audio_dur,
        seg_start=start_t,
        seg_end=end_t,
        prev_audio_end=prev_audio_end,
        next_seg_start=next_seg_start,
        max_speedup=max_speedup,
        tolerance=tolerance,
        min_gap=min_gap,
    )

    # Bước 3: Nếu thiếu > tolerance (0.3s) sau khi đã tăng tốc 1.2x và tận dụng khoảng trống
    if plan["needs_retry"]:
        msg1 = (
            f"[Dubbing Segment #{s_id}] Audio ({audio_dur:.2f}s) sau khi tăng tốc tối đa {max_speedup:.1f}x "
            f"và tận dụng khoảng trống vẫn thiếu {plan['deficit']:.2f}s (> {tolerance:.1f}s). Đang tạo lại audio lần 1..."
        )
        _log(msg1)
        if on_status:
            on_status(msg1)

        # Retry lần 1: Tạo lại audio
        seg_retry1_path = temp_dir / f"seg_{s_id}_retry1.wav"
        tts.synthesize(text, seg_retry1_path, voice=voice)
        audio_dur1 = get_audio_duration_ffprobe(seg_retry1_path)

        if audio_dur1 > 0:
            plan1 = calculate_segment_timing_and_excess(
                raw_dur=audio_dur1,
                seg_start=start_t,
                seg_end=end_t,
                prev_audio_end=prev_audio_end,
                next_seg_start=next_seg_start,
                max_speedup=max_speedup,
                tolerance=tolerance,
                min_gap=min_gap,
            )
            # Nếu audio mới ngắn hơn hoặc không còn thiếu > 0.3s thì cập nhật
            if audio_dur1 < audio_dur or not plan1["needs_retry"]:
                seg_raw_path = seg_retry1_path
                audio_dur = audio_dur1
                plan = plan1

        # Retry lần 2: Nếu vẫn thiếu > 0.3s -> Gọi LLM rút gọn câu
        if plan["needs_retry"] and chat_url and headers:
            msg2 = (
                f"[Dubbing Segment #{s_id}] Audio tạo lại vẫn thiếu {plan['deficit']:.2f}s (> {tolerance:.1f}s). "
                f"Đang rút gọn / dịch lại câu qua LLM..."
            )
            _log(msg2)
            if on_status:
                on_status(msg2)

            condensed = condense_segment_via_llm(segment, plan["allocated_dur"], chat_url, headers, model)
            if condensed and condensed != text:
                _log(f"    • Segment #{s_id} Bản rút gọn: '{condensed}' (thay cho '{text}')")
                segment["spokenText"] = condensed
                seg_condensed_path = temp_dir / f"seg_{s_id}_condensed.wav"
                tts.synthesize(condensed, seg_condensed_path, voice=voice)
                audio_dur2 = get_audio_duration_ffprobe(seg_condensed_path)
                if audio_dur2 > 0:
                    seg_raw_path = seg_condensed_path
                    audio_dur = audio_dur2
                    plan = calculate_segment_timing_and_excess(
                        raw_dur=audio_dur2,
                        seg_start=start_t,
                        seg_end=end_t,
                        prev_audio_end=prev_audio_end,
                        next_seg_start=next_seg_start,
                        max_speedup=max_speedup,
                        tolerance=tolerance,
                        min_gap=min_gap,
                    )

    # Bước 4: Căn chỉnh chính xác và xuất file audio
    align_and_pad_audio_segment(
        input_audio_path=seg_raw_path,
        target_duration=plan["allocated_dur"],
        output_audio_path=seg_aligned_path,
        speed_factor=plan["speed_factor"],
        pad_left=plan["pad_left"],
    )
    return seg_aligned_path, plan["actual_start"], plan["actual_end"]


def build_full_dubbed_audio(
    segments: List[Dict[str, Any]],
    total_duration: float,
    output_audio_path: Path | str,
    tts: VieNeuTTS,
    voice: str = DEFAULT_VOICE,
    batch_size: int = 30,
    chat_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    model: str = "gemini-lite",
    max_speedup: float = 1.2,
    tolerance: float = 0.3,
    min_gap: float = 0.05,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """
    Generate dubbing audio for each segment, align precisely with gap-aware placement,
    and assemble into a single seamless audio track matching total video duration.
    Utilizes GPU batch inference via `infer_batch` (batch_size=30) for high throughput.
    """
    out_audio = Path(output_audio_path).resolve()
    out_audio.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="vieneu_dub_"))
    ffmpeg = get_ffmpeg_bin()

    total_segs = len(segments)
    bs = max(1, int(batch_size or 30))
    _log(f"Bắt đầu lồng tiếng {total_segs} phân đoạn với VieNeu-TTS (infer_batch GPU, batch_size={bs}, Giọng: '{voice}')...")

    # Sort segments by start time
    sorted_segs = sorted(segments, key=lambda x: float(x.get("startTime", 0.0)))
    concat_list = []
    current_time = 0.0

    try:
        # Phase 1: Batch inference across all segments using GPU infer_batch (chunks of batch_size)
        batch_items: List[Tuple[str, Path]] = []
        raw_audio_map: Dict[str, Path] = {}

        for idx, seg in enumerate(sorted_segs):
            s_id = str(seg.get("id", idx + 1))
            seg_raw = temp_dir / f"seg_{s_id}_raw.wav"
            text = str(seg.get("spokenText") or seg.get("subtitleText") or seg.get("translation") or "").strip()
            batch_items.append((text, seg_raw))
            raw_audio_map[s_id] = seg_raw

        def _batch_prog_cb(done_count: int, total_count: int):
            pct = 10 + (done_count / max(1, total_count)) * 50  # 10% -> 60%
            msg = f"Đang tạo giọng đọc batch GPU ({done_count}/{total_count} phân đoạn, batch={bs})..."
            if on_progress:
                on_progress(pct, msg)

        tts.synthesize_batch(
            items=batch_items,
            voice=voice,
            batch_size=bs,
            on_progress=_batch_prog_cb,
        )

        # Phase 2: Alignment, duration verification, LLM retries, timeline assembly
        for idx, seg in enumerate(sorted_segs):
            pct = 60 + (idx / max(1, total_segs)) * 35  # 60% -> 95%
            seg_start = float(seg.get("startTime", 0.0))
            seg_end = float(seg.get("endTime", 0.0))
            seg_id = str(seg.get("id", idx + 1))
            next_start = float(sorted_segs[idx + 1].get("startTime", 0.0)) if idx + 1 < total_segs else total_duration

            status_msg = f"Đang căn chỉnh thời lượng phân đoạn #{seg_id} ({idx+1}/{total_segs})..."
            if on_progress:
                on_progress(pct, status_msg)

            # Process segment dubbing with retry & alignment, reusing pre-generated raw audio
            aligned_seg_audio, actual_start, actual_end = process_segment_dubbing_with_retry(
                segment=seg,
                tts=tts,
                temp_dir=temp_dir,
                voice=voice,
                pre_generated_audio=raw_audio_map.get(seg_id),
                prev_audio_end=current_time,
                next_seg_start=next_start,
                chat_url=chat_url,
                headers=headers,
                model=model,
                max_speedup=max_speedup,
                tolerance=tolerance,
                min_gap=min_gap,
                on_status=lambda msg: on_progress(pct, msg) if on_progress else None,
            )

            # Insert silence if there is an empty gap before actual_start
            if actual_start > current_time + 0.005:
                silence_dur = actual_start - current_time
                silence_file = temp_dir / f"gap_{idx}_silence.wav"
                create_silent_audio(silence_file, silence_dur)
                concat_list.append(silence_file)
                current_time = actual_start

            concat_list.append(aligned_seg_audio)
            current_time = actual_end

        # Pad remaining silence at the end of video if needed
        if total_duration > current_time + 0.05:
            tail_silence_dur = total_duration - current_time
            tail_file = temp_dir / "tail_silence.wav"
            create_silent_audio(tail_file, tail_silence_dur)
            concat_list.append(tail_file)

        # Write concat demuxer text file
        concat_txt = temp_dir / "concat_list.txt"
        with open(concat_txt, "w", encoding="utf-8") as f:
            for piece in concat_list:
                escaped_path = str(piece.resolve()).replace("\\", "/")
                f.write(f"file '{escaped_path}'\n")

        if on_progress:
            on_progress(95, "Đang ghép các phân đoạn thành file audio dubbing hoàn chỉnh...")

        # Concat all segments into the final master audio (mp3 / wav)
        codec_flag = ["-c:a", "libmp3lame", "-b:a", "192k"] if out_audio.suffix.lower() == ".mp3" else ["-c:a", "pcm_s16le"]
        cmd_concat = [
            ffmpeg, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt),
            *codec_flag,
            str(out_audio)
        ]
        res = subprocess.run(cmd_concat, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg concat audio thất bại: {res.stderr}")

        final_dur = get_audio_duration_ffprobe(out_audio)
        _log(f"✅ Hoàn tất file audio dubbing hoàn chỉnh: {out_audio} ({final_dur:.2f}s)")
        return out_audio

    finally:
        # Cleanup temporary audio files
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def render_dubbed_video(
    video_path: Path | str,
    dubbed_audio_path: Path | str,
    output_video_path: Path | str,
    bg_volume: float = 0.25,
    pitch_down_pct: float = 5.0,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """
    Render final dubbed video by merging video with dubbed audio track:
    - Original background audio:
        * Volume reduced to bg_volume (default 0.25)
        * Periodic mute: every 0.9s mute for 0.1s (cycle: 1.0s)
        * Pitch down by pitch_down_pct (default 5% -> pitch 0.95)
    - Dubbed audio mixed in at full volume via amix.
    - Video stream copied without re-encoding (-c:v copy) for maximum speed.
    """
    in_v = Path(video_path).resolve()
    in_a = Path(dubbed_audio_path).resolve()
    out_v = Path(output_video_path).resolve()
    out_v.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = get_ffmpeg_bin()

    if not in_v.exists():
        raise FileNotFoundError(f"Video input file not found: {in_v}")
    if not in_a.exists():
        raise FileNotFoundError(f"Dubbed audio file not found: {in_a}")

    _log(f"Đang render video lồng tiếng ({out_v.name})...")
    if on_progress:
        on_progress(5, f"Bắt đầu render video lồng tiếng: {out_v.name}...")

    # Calculate pitch factor (e.g. 5% down -> 0.95)
    pitch_ratio = max(0.8, min(1.0, 1.0 - (pitch_down_pct / 100.0)))
    atempo_comp = 1.0 / pitch_ratio

    # Periodic mute every 0.9s for 0.1s: if(lt(mod(t,1.0),0.9),0.25,0)
    vol_expr = f"volume='if(lt(mod(t,1.0),0.9),{bg_volume:.3f},0)':eval=frame"

    # Try librubberband filter first, fallback to asetrate/atempo
    rubberband_filter = (
        f"[0:a]{vol_expr},rubberband=pitch={pitch_ratio:.4f}[a_bg];"
        f"[a_bg][1:a]amix=inputs=2:duration=first:weights=1 1[a_out]"
    )

    fallback_filter = (
        f"[0:a]{vol_expr},asetrate=44100*{pitch_ratio:.4f},atempo={atempo_comp:.4f},aresample=44100[a_bg];"
        f"[a_bg][1:a]amix=inputs=2:duration=first:weights=1 1[a_out]"
    )

    cmd_rubberband = [
        ffmpeg, "-y",
        "-i", str(in_v),
        "-i", str(in_a),
        "-filter_complex", rubberband_filter,
        "-map", "0:v",
        "-map", "[a_out]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_v)
    ]

    res = subprocess.run(cmd_rubberband, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        _log(f"Rubberband filter không khả dụng hoặc gặp lỗi ({res.stderr[:100]}). Thử fallback filter...")
        cmd_fallback = [
            ffmpeg, "-y",
            "-i", str(in_v),
            "-i", str(in_a),
            "-filter_complex", fallback_filter,
            "-map", "0:v",
            "-map", "[a_out]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            str(out_v)
        ]
        res_fb = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res_fb.returncode != 0:
            raise RuntimeError(f"FFmpeg render video lồng tiếng thất bại: {res_fb.stderr}")

    _log(f"✅ Render ghép video lồng tiếng hoàn tất thành công: {out_v}")
    if on_progress:
        on_progress(100, "Render video lồng tiếng hoàn tất 100%!")

    return out_v
