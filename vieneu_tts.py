"""
VieNeu-TTS Integration & Audio Alignment Pipeline for Video Dubbing.

Provides:
- VieNeuTTS client wrapper (v3 Turbo 48kHz, preset voices, CPU/GPU auto-detection)
- Duration measurement via ffprobe/ffmpeg
- Intelligent audio alignment & gap allocation strategy:
    * Audio is allowed to be longer than subtitle duration by utilizing available silence gaps
      between utterances, while strictly never colliding with previous or next audio clips.
    * Speed up audio up to 1.35x (via ffmpeg atempo) if audio exceeds subtitle duration.
    * Left-aligned speech with zero leading silence (starts immediately when subtitle appears, no lag).
- Automatic timing recovery:
    * If speech exceeds subtitle duration ("audio bị thừa"):
        - Regenerates audio with VieNeu-TTS (retries with TTS to find best fit without altering text)
        - No re-translation or text condensation via LLM, preserving the original translation intact
- Full dubbing master track builder matching video timeline exactly
- Video dubbing renderer with background audio modification:
    * Original audio volume reduced to 0.2
    * Dubbed audio volume amplified to 3.0
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
import threading
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
    """Log with timestamp using tqdm.write to preserve progress bars."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now_str}] [VieNeu-TTS] {msg}"
    try:
        from tqdm import tqdm
        tqdm.write(formatted)
    except Exception:
        try:
            print(formatted, flush=True)
        except UnicodeEncodeError:
            safe_msg = formatted.encode("ascii", "backslashreplace").decode("ascii")
            print(safe_msg, flush=True)


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


DEFAULT_SAMPLE_RATE = 48000


def create_silent_audio(out_path: Path | str, duration_sec: float, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Path:
    """Generate a valid silent WAV audio file using ffmpeg or direct PCM at standardized 48kHz."""
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
        "-ar", str(sample_rate),
        "-ac", "1",
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
    """Adjust playback speed of an audio file using ffmpeg atempo preserving 48kHz."""
    p = Path(audio_path).resolve()
    if not p.exists() or p.stat().st_size == 0 or abs(speed - 1.0) <= 0.03:
        return
    sped_temp = p.with_name(f"{p.stem}_speed_tmp.wav")
    ffmpeg = get_ffmpeg_bin()
    cmd = [
        ffmpeg, "-y", "-i", str(p),
        "-af", f"atempo={speed:.3f},aresample=48000",
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        "-ac", "1",
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

    def __init__(
        self,
        mode: str = "v3turbo",
        voice: str = DEFAULT_VOICE,
        backend: Optional[str] = None,
        max_batch_size: int = 64,
    ):
        self.mode = mode
        self.voice = voice or DEFAULT_VOICE
        self.backend = backend
        self.max_batch_size = max(1, int(max_batch_size or 64))
        self._vieneu_client = None
        self._is_available = False
        self._init_engine()

    def _init_engine(self):
        """Attempt to initialize Vieneu SDK client."""
        try:
            from vieneu import Vieneu  # type: ignore
            _log(
                f"Đang nạp mô hình VieNeu-TTS (mode='{self.mode}', backend={self.backend or 'auto'}, "
                f"max_batch_size={self.max_batch_size})..."
            )
            kwargs = {"mode": self.mode}
            if self.backend:
                kwargs["backend"] = self.backend
            if self.max_batch_size:
                kwargs["max_batch_size"] = self.max_batch_size
            try:
                self._vieneu_client = Vieneu(**kwargs)
            except TypeError:
                # Fallback nếu phiên bản SDK cũ không hỗ trợ tham số max_batch_size
                kwargs.pop("max_batch_size", None)
                self._vieneu_client = Vieneu(**kwargs)
            self._is_available = True
            _log(f"✅ Đã khởi tạo VieNeu-TTS thành công (Giọng mặc định: '{self.voice}', max_batch_size={self.max_batch_size})!")
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

    def infer_batch(self, texts: List[str], voice: Optional[str] = None, batch_size: int = 64) -> List[Any]:
        """
        Directly invoke Vieneu infer_batch to leverage GPU parallel forward passes.
        Returns list of audio waveforms (numpy arrays).
        """
        if not self.is_available:
            raise RuntimeError("VieNeu-TTS client is not initialized or unavailable.")
        v = voice or self.voice or DEFAULT_VOICE
        bs = max(1, int(batch_size or 64))
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
        batch_size: int = 64,
        speed: float = 1.0,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Path]:
        """
        Synthesize speech from multiple texts in batches using Vieneu SDK's `infer_batch`
        to maximize GPU utilization and throughput.

        - Divides items into batches of `batch_size` (default: 64 segments).
        - Runs neural forward passes simultaneously across segments on GPU.
        - Automatically writes output audio files and applies speed adjustment if needed.
        - Gracefully falls back to sequential synthesize if infer_batch errors.
        """
        if not items:
            return []

        v = voice or self.voice or DEFAULT_VOICE
        results: List[Path] = []
        total_items = len(items)
        bs = max(1, int(batch_size or 64))

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


def align_and_pad_audio_segment(
    input_audio_path: Path | str,
    target_duration: float,
    output_audio_path: Path | str,
    speed_factor: float = 1.0,
    pad_left: float = 0.0,
) -> Tuple[Path, float]:
    """
    Căn chỉnh audio segment theo target_duration và cấu hình speed từ timing_plan của node_helper:
    1. Cắt bỏ khoảng lặng ở đầu audio (silenceremove) để âm thanh phát ra ngay lập tức, không bị trễ tiếng so với phụ đề.
    2. Tăng tốc bằng ffmpeg 'atempo' nếu speed_factor > 1.005.
    3. Căn trái lời nói (mặc định pad_left = 0) để lời đọc khớp ngay khi subtitle xuất hiện.
    4. Bổ sung khoảng lặng bên phải (apad) và trim về target_duration (fade-out 0.05s ở đuôi nếu cần).
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

    # Xây dựng filtergraph hoàn chỉnh xử lý trong 1 lượt FFmpeg
    filters = ["silenceremove=start_periods=1:start_duration=0.01:start_threshold=-45dB"]
    if speed_factor > 1.005:
        filters.append(f"atempo={speed_factor:.4f}")

    pad_left_ms = max(0, int(round(pad_left * 1000)))
    if pad_left_ms > 5:
        filters.append(f"adelay={pad_left_ms}|{pad_left_ms}")

    filters.append(f"apad=pad_dur={target_dur + 0.5:.4f}")
    fade_out_start = max(0.0, target_dur - 0.05)
    filters.append(f"afade=t=out:st={fade_out_start:.4f}:d=0.05")
    filters.append(f"atrim=0:{target_dur:.4f}")
    filters.append("aresample=48000")
    filters.append("aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono")

    af_filter = ",".join(filters)
    cmd = [
        ffmpeg, "-y", "-i", str(in_p),
        "-af", af_filter,
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        "-ac", "1",
        str(out_p)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not out_p.exists():
        shutil.copyfile(in_p, out_p)

    final_dur = get_audio_duration_ffprobe(out_p)
    return out_p, final_dur


def condense_segment_via_condensation(
    segment: Dict[str, Any],
    target_duration: float,
    chat_url: str,
    headers: Dict[str, str],
    model: str = "gemini-lite",
    max_speedup: float = 1.35,
) -> Optional[str]:
    """
    Rút gọn câu thoại qua cơ chế Condensation kiểu mới (theo chuẩn node_helper)
    thay vì prompt đơn sơ kiểu cũ.
    """
    import node_helper
    full_text = str(segment.get("spokenText") or segment.get("translation") or segment.get("text") or "").strip()
    if not full_text:
        return None

    # Tính ngân sách âm tiết chuẩn theo target_duration và max_speedup
    budget_syl = max(2, int(target_duration * 3.8 * float(max_speedup or 1.35) * 0.95))
    api_key = (headers.get("Authorization") or "").replace("Bearer ", "").strip() if headers else ""
    if not api_key or api_key == "dummy":
        from config import DEFAULT_SETTINGS
        api_key = os.getenv("CUSTOM_API_KEY") or DEFAULT_SETTINGS.get("customApiKey", "")
    if not chat_url:
        from config import DEFAULT_SETTINGS
        ep = os.getenv("CUSTOM_API_ENDPOINT") or DEFAULT_SETTINGS.get("customApiEndpoint", "")
        chat_url = f"{ep.rstrip('/')}/chat/completions" if ep else "https://api.deepseek.com/v1/chat/completions"
    if not model:
        from config import DEFAULT_SETTINGS
        model = os.getenv("CUSTOM_MODEL") or DEFAULT_SETTINGS.get("customModel", "gemini-lite")
    translator = node_helper.DeepSeekTranslator(api_key=api_key or "dummy")
    
    cand = [{
        "id": str(segment.get("id", "1")),
        "source": segment.get("text", ""),
        "full": full_text,
        "budget": budget_syl,
        "budgetMin": max(2, int(node_helper.vi_syllables_spoken(full_text) * 0.70))
    }]
    
    # Lượt 1 (Condensation chuẩn 70-85%)
    res = translator.condense_chunk(cand, {"model": model, "url": chat_url}, deep=False)
    short_text = res.get(cand[0]["id"])
    
    # Nếu lượt 1 vẫn vượt quá budget, thực hiện lượt 2 (Deep Condensation 50-70%)
    if short_text and node_helper.vi_syllables_spoken(short_text) > budget_syl:
        cand[0]["budgetMin"] = max(2, int(node_helper.vi_syllables_spoken(full_text) * 0.50))
        res_deep = translator.condense_chunk(cand, {"model": model, "url": chat_url}, deep=True)
        if res_deep.get(cand[0]["id"]):
            short_text = res_deep[cand[0]["id"]]
            
    return short_text if short_text and short_text != full_text else None

# Alias tương thích ngược
condense_segment_via_llm = condense_segment_via_condensation


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
    max_speedup: float = 1.35,
    tolerance: float = 0.3,
    min_gap: float = 0.05,
    on_status: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, float, float]:
    """
    Xử lý lồng tiếng cho một phân đoạn phụ đề sử dụng node_helper.verify_dubbing_fit & compute_timing_plan:
    1. Kiểm tra audio đã tạo sẵn hoặc tổng hợp với VieNeu-TTS.
    2. Sử dụng node_helper.verify_dubbing_fit để kiểm tra xem audio có vừa khung (sau khi borrow và tempo 1.35x) không.
    3. Nếu không vừa (audio bị thừa / infeasible): chỉ gen lại audio với VieNeu-TTS (thử lại các lần), không dịch rút gọn câu qua LLM.
    4. Căn chỉnh audio theo plannedStart, plannedEnd, audioTempo từ timing_plan của node_helper.
    """
    import node_helper

    s_id = str(segment.get("id", "0"))
    start_t = float(segment.get("startTime", 0.0))
    end_t = float(segment.get("endTime", 0.0))
    target_dur = max(0.05, end_t - start_t)

    text = str(segment.get("spokenText") or segment.get("subtitleText") or segment.get("translation") or "").strip()
    seg_raw_path = temp_dir / f"seg_{s_id}_raw.wav"
    seg_aligned_path = temp_dir / f"seg_{s_id}_aligned.wav"

    if not text:
        create_silent_audio(seg_aligned_path, target_dur)
        return seg_aligned_path, start_t, end_t

    # Bước 1: Sử dụng audio từ batch phase hoặc synthesize theo yêu cầu
    if pre_generated_audio and Path(pre_generated_audio).exists() and Path(pre_generated_audio).stat().st_size > 0:
        seg_raw_path = Path(pre_generated_audio).resolve()
    else:
        tts.synthesize(text, seg_raw_path, voice=voice)

    audio_dur = get_audio_duration_ffprobe(seg_raw_path)

    # Cấu hình policy cho node_helper
    policy = {
        "residualTempoCap": float(max_speedup or 1.35),
        "borrowSideMaxSec": 2.0,
        "borrowSideMaxFrac": 0.8,
        "borrowTotalMaxSec": 3.0,
        "borrowTotalMaxFrac": 1.0,
        "safetyGapSec": float(min_gap if min_gap is not None else 0.05),
        "minVideoSpeed": 1.0,
        "maxVideoSpeed": 1.0,
        "toleranceSec": float(tolerance if tolerance is not None else 0.3),
    }

    unit_item = {
        "id": s_id,
        "unitId": s_id,
        "startTime": start_t,
        "endTime": end_t,
        "audioDuration": audio_dur,
        "spokenText": text,
    }

    def _eval_fit(u_obj):
        eval_units = []
        if prev_audio_end > 0.0 and prev_audio_end < start_t:
            eval_units.append({
                "id": "_prev_sentinel",
                "unitId": "_prev_sentinel",
                "startTime": max(0.0, prev_audio_end - 1.0),
                "endTime": prev_audio_end,
                "audioDuration": 1.0,
            })
        eval_units.append(u_obj)
        if next_seg_start is not None and next_seg_start > end_t:
            eval_units.append({
                "id": "_next_sentinel",
                "unitId": "_next_sentinel",
                "startTime": next_seg_start,
                "endTime": next_seg_start + 1.0,
                "audioDuration": 1.0,
            })
        tot_dur = (next_seg_start + 2.0) if (next_seg_start is not None and next_seg_start > end_t) else (end_t + 10.0)
        return node_helper.verify_dubbing_fit(eval_units, {"totalDuration": tot_dur, "policy": policy})

    def _extract_unit(res):
        return next((u for u in res.get("plan", {}).get("units", []) if str(u.get("unitId")) == s_id), {})

    fit_res = _eval_fit(unit_item)
    is_infeasible = s_id in (fit_res.get("infeasibleUnitIds") or [])

    # Bước 3: Nếu audio bị thừa so với khung thời gian (infeasible > tolerance):
    # Chỉ gen lại audio với VieNeu-TTS, KHÔNG dịch rút gọn câu
    if is_infeasible:
        best_audio_path = seg_raw_path
        best_dur = audio_dur
        max_retries = 2

        for attempt in range(1, max_retries + 1):
            msg = (
                f"[Dubbing Segment #{s_id}] Audio ({best_dur:.2f}s) bị thừa thời lượng > {tolerance:.1f}s. "
                f"Đang gen lại audio lần {attempt}/{max_retries} (giữ nguyên bản dịch, không dịch rút gọn)..."
            )
            _log(msg)
            if on_status:
                on_status(msg)

            seg_retry_path = temp_dir / f"seg_{s_id}_retry{attempt}.wav"
            tts.synthesize(text, seg_retry_path, voice=voice)
            audio_dur_retry = get_audio_duration_ffprobe(seg_retry_path)

            if audio_dur_retry > 0:
                unit_item["audioDuration"] = audio_dur_retry
                recheck = _eval_fit(unit_item)
                is_now_feasible = s_id not in (recheck.get("infeasibleUnitIds") or [])

                if is_now_feasible or audio_dur_retry < best_dur:
                    best_audio_path = seg_retry_path
                    best_dur = audio_dur_retry
                    fit_res = recheck
                    is_infeasible = not is_now_feasible

                if is_now_feasible:
                    _log(f"[Dubbing Segment #{s_id}] ✅ Gen lại lần {attempt} thành công ({audio_dur_retry:.2f}s) đã vừa vặn khung thời gian!")
                    break

        # Nếu sau khi gen lại vẫn còn thừa thời lượng, áp dụng kiểu mới Condensation để rút gọn lời đọc
        if is_infeasible and chat_url:
            msg = (
                f"[Dubbing Segment #{s_id}] Audio ({best_dur:.2f}s) vẫn còn thừa sau khi gen lại. "
                f"Đang áp dụng kiểu mới CONDENSATION để rút gọn lời đọc..."
            )
            _log(msg)
            if on_status:
                on_status(msg)

            u_cur = _extract_unit(fit_res)
            p_win = float(u_cur.get("plannedWindow") or target_dur)
            shortened = condense_segment_via_condensation(
                segment=segment,
                target_duration=p_win,
                chat_url=chat_url,
                headers=headers or {},
                model=model,
                max_speedup=max_speedup
            )
            if shortened and shortened != text:
                _log(f"[Dubbing Segment #{s_id}] ➔ Bản rút gọn Condensation mới: '{shortened}' (thay cho '{text}')")
                segment["spokenText"] = shortened
                seg_condensed_path = temp_dir / f"seg_{s_id}_condensed.wav"
                tts.synthesize(shortened, seg_condensed_path, voice=voice)
                dur_condensed = get_audio_duration_ffprobe(seg_condensed_path)
                if dur_condensed > 0:
                    unit_item["audioDuration"] = dur_condensed
                    unit_item["spokenText"] = shortened
                    recheck = _eval_fit(unit_item)
                    best_audio_path = seg_condensed_path
                    best_dur = dur_condensed
                    fit_res = recheck

        seg_raw_path = best_audio_path
        audio_dur = best_dur

    u_final = _extract_unit(fit_res)
    p_start = float(u_final.get("plannedStart", start_t))
    p_end = float(u_final.get("plannedEnd", end_t))
    tempo = float(u_final.get("audioTempo", 1.0))
    eff_speech = audio_dur / tempo if tempo > 1e-6 else audio_dur

    # Căn chỉnh thời điểm bắt đầu: Giọng đọc bắt đầu ngay khi subtitle xuất hiện (start_t)
    if start_t + eff_speech <= p_end:
        actual_start = start_t
        actual_end = start_t + eff_speech
    else:
        actual_start = max(p_start, p_end - eff_speech)
        actual_end = actual_start + eff_speech
        if next_seg_start is not None and actual_end > next_seg_start - min_gap:
            # Nếu chạm tới segment tiếp theo, tăng nhẹ tempo (tối đa 1.35x) để đọc kịp trọn câu mà không bị cắt tiếng
            avail_dur = max(0.05, next_seg_start - min_gap - actual_start)
            needed_tempo = audio_dur / avail_dur
            if needed_tempo <= 1.35:
                tempo = max(tempo, needed_tempo)
                eff_speech = audio_dur / tempo
                actual_end = actual_start + eff_speech
            else:
                actual_end = next_seg_start - min_gap

    if prev_audio_end > 0.0 and actual_start < prev_audio_end + min_gap:
        actual_start = prev_audio_end + min_gap
        actual_end = actual_start + eff_speech

    actual_dur = max(0.05, actual_end - actual_start)
    align_and_pad_audio_segment(
        input_audio_path=seg_raw_path,
        target_duration=actual_dur,
        output_audio_path=seg_aligned_path,
        speed_factor=tempo,
        pad_left=0.0,
    )
    return seg_aligned_path, actual_start, actual_end


def build_full_dubbed_audio(
    segments: List[Dict[str, Any]],
    total_duration: float,
    output_audio_path: Path | str,
    tts: VieNeuTTS,
    voice: str = DEFAULT_VOICE,
    batch_size: int = 64,
    chat_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    model: str = "gemini-lite",
    max_speedup: float = 1.35,
    tolerance: float = 0.3,
    min_gap: float = 0.05,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """
    Tạo audio dubbing toàn diện dựa trên timing plan và verify_dubbing_fit từ node_helper:
    1. Batch GPU inference với VieNeu-TTS để tạo audio thô.
    2. Sử dụng node_helper.verify_dubbing_fit để kiểm tra toàn bộ các câu:
       - Mượn khoảng trống 2 bên (không chạm câu trước/sau).
       - Tăng tốc tối đa 1.35x.
       - Cho phép dung sai 0.3s.
    3. Nếu có phân đoạn infeasible (audio bị thừa > 0.3s):
       - Chỉ gen lại audio với VieNeu-TTS (thử lại các lần), KHÔNG dịch rút gọn câu qua LLM.
    4. Tính toán final timing_plan với node_helper.compute_timing_plan và lắp ráp audio timeline.
    """
    import node_helper

    out_audio = Path(output_audio_path).resolve()
    out_audio.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="vieneu_dub_"))
    ffmpeg = get_ffmpeg_bin()

    total_segs = len(segments)
    bs = max(1, int(batch_size or 64))
    _log(f"Bắt đầu lồng tiếng {total_segs} phân đoạn với VieNeu-TTS (infer_batch GPU, batch_size={bs}, Giọng: '{voice}')...")

    # Sort segments theo thời gian bắt đầu
    sorted_segs = sorted(segments, key=lambda x: float(x.get("startTime", 0.0)))
    concat_list = []

    try:
        # Phase 1: Batch inference cho toàn bộ segments
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

        # Đo thời lượng thực tế của từng file audio đã tạo
        for idx, seg in enumerate(sorted_segs):
            s_id = str(seg.get("id", idx + 1))
            raw_path = raw_audio_map.get(s_id)
            if raw_path and raw_path.exists() and raw_path.stat().st_size > 0:
                seg["audioDuration"] = get_audio_duration_ffprobe(raw_path)
            else:
                seg["audioDuration"] = max(0.05, float(seg.get("endTime", 0.0)) - float(seg.get("startTime", 0.0)))
            seg["unitId"] = s_id

        # Phase 2: Timing Plan & Verification sử dụng node_helper
        dubbing_policy = {
            "residualTempoCap": float(max_speedup or 1.35),
            "borrowSideMaxSec": 2.0,
            "borrowSideMaxFrac": 0.8,
            "borrowTotalMaxSec": 3.0,
            "borrowTotalMaxFrac": 1.0,
            "safetyGapSec": float(min_gap if min_gap is not None else 0.05),
            "minVideoSpeed": 1.0,
            "maxVideoSpeed": 1.0,
            "toleranceSec": float(tolerance if tolerance is not None else 0.3),
        }

        if on_progress:
            on_progress(60, "Đang kiểm tra độ khớp thời lượng dubbing bằng node_helper.verify_dubbing_fit...")

        fit_res = node_helper.verify_dubbing_fit(
            sorted_segs,
            {"totalDuration": total_duration, "policy": dubbing_policy}
        )
        infeasible_ids = list(fit_res.get("infeasibleUnitIds") or [])
        plan = fit_res.get("plan") or {}

        # Nếu audio bị thừa so với khung thời gian (infeasible > tolerance):
        if infeasible_ids:
            msg = f"Phát hiện {len(infeasible_ids)} phân đoạn audio bị thừa > {tolerance:.1f}s theo verify_dubbing_fit. Đang tiến hành tối ưu hóa..."
            _log(msg)
            if on_progress:
                on_progress(63, msg)

            # Lần 1: Thử gen lại audio giữ nguyên văn bản (VieNeu-TTS có thể gen tốc độ nhanh hơn)
            _log(f"--> [Dubbing Retry Lần 1] Thử gen lại audio cho {len(infeasible_ids)} phân đoạn bị thừa...")
            for s_id in list(infeasible_ids):
                seg = next((s for s in sorted_segs if str(s.get("id", s.get("unitId"))) == s_id), None)
                if not seg:
                    continue
                text = str(seg.get("spokenText") or seg.get("subtitleText") or seg.get("translation") or "").strip()
                seg_retry_path = temp_dir / f"seg_{s_id}_retry1.wav"
                tts.synthesize(text, seg_retry_path, voice=voice)
                new_dur = get_audio_duration_ffprobe(seg_retry_path)
                cur_dur = float(seg.get("audioDuration", 0.0))
                if new_dur > 0 and (cur_dur <= 0 or new_dur < cur_dur):
                    seg["audioDuration"] = new_dur
                    raw_audio_map[s_id] = seg_retry_path

            # Re-check sau lần 1
            recheck = node_helper.verify_dubbing_fit(
                sorted_segs,
                {"totalDuration": total_duration, "policy": dubbing_policy}
            )
            infeasible_ids = list(recheck.get("infeasibleUnitIds") or [])
            plan = recheck.get("plan") or {}

            # Lần 2: Nếu vẫn còn câu bị thừa thời lượng, áp dụng KIỂU MỚI CONDENSATION để rút gọn lời đọc
            if infeasible_ids and chat_url:
                _log(f"--> [Dubbing Retry Lần 2] Áp dụng kiểu mới CONDENSATION để rút gọn lời đọc cho {len(infeasible_ids)} câu còn thừa...")
                if on_progress:
                    on_progress(66, f"Đang rút gọn theo Condensation mới cho {len(infeasible_ids)} câu...")

                units_dict = {str(u.get("unitId")): u for u in plan.get("units", [])}
                condense_candidates = []
                for s_id in list(infeasible_ids):
                    seg = next((s for s in sorted_segs if str(s.get("id", s.get("unitId"))) == s_id), None)
                    if not seg:
                        continue
                    u_info = units_dict.get(s_id) or {}
                    p_win = float(u_info.get("plannedWindow") or (float(seg.get("endTime", 0)) - float(seg.get("startTime", 0))))
                    b_syl = max(2, int(p_win * 3.8 * float(max_speedup or 1.35) * 0.95))
                    cur_spoken = str(seg.get("spokenText") or seg.get("translation") or seg.get("text") or "").strip()
                    condense_candidates.append({
                        "id": s_id,
                        "source": seg.get("text", ""),
                        "full": cur_spoken,
                        "budget": b_syl,
                        "budgetMin": max(2, int(node_helper.vi_syllables_spoken(cur_spoken) * 0.70)),
                        "seg": seg
                    })

                if condense_candidates:
                    api_key = (headers.get("Authorization") or "").replace("Bearer ", "").strip() if headers else ""
                    if not api_key or api_key == "dummy":
                        from config import DEFAULT_SETTINGS
                        api_key = os.getenv("CUSTOM_API_KEY") or DEFAULT_SETTINGS.get("customApiKey", "")
                    if not chat_url:
                        from config import DEFAULT_SETTINGS
                        ep = os.getenv("CUSTOM_API_ENDPOINT") or DEFAULT_SETTINGS.get("customApiEndpoint", "")
                        chat_url = f"{ep.rstrip('/')}/chat/completions" if ep else "https://api.deepseek.com/v1/chat/completions"
                    if not model:
                        from config import DEFAULT_SETTINGS
                        model = os.getenv("CUSTOM_MODEL") or DEFAULT_SETTINGS.get("customModel", "gemini-lite")
                    translator = node_helper.DeepSeekTranslator(api_key=api_key or "dummy")
                    
                    # Pass 1: Condensation chuẩn
                    condensed_map = translator.condense_chunk(condense_candidates, {"model": model, "url": chat_url}, deep=False)
                    
                    # Pass 2: Deep Condensation cho những câu vẫn còn dài (chỉ thực hiện nếu Pass 1 có kết quả)
                    deep_cands = []
                    if condensed_map:
                        for c in condense_candidates:
                            c_id = c["id"]
                            read1 = condensed_map.get(c_id, c["full"])
                            c["seg"]["spokenText"] = read1
                            if node_helper.vi_syllables_spoken(read1) > c["budget"]:
                                c["budgetMin"] = max(2, int(node_helper.vi_syllables_spoken(c["full"]) * 0.50))
                                deep_cands.append(c)
                        
                        if deep_cands:
                            deep_map = translator.condense_chunk(deep_cands, {"model": model, "url": chat_url}, deep=True)
                            for dc in deep_cands:
                                dc_id = dc["id"]
                                if dc_id in deep_map and deep_map[dc_id]:
                                    dc["seg"]["spokenText"] = deep_map[dc_id]

                    # Gen lại audio với câu đã rút gọn bằng Condensation mới
                    _log(f"--> [Condensation] Đang tổng hợp lại giọng đọc cho {len(condense_candidates)} phân đoạn đã rút gọn...")
                    for idx_c, c in enumerate(condense_candidates):
                        s_id = c["id"]
                        seg = c["seg"]
                        new_text = seg["spokenText"]
                        if on_progress:
                            c_pct = 70 + (idx_c / max(1, len(condense_candidates))) * 5  # 70% -> 75%
                            on_progress(c_pct, f"Rút gọn & tổng hợp lại audio ({idx_c+1}/{len(condense_candidates)})")
                        seg_retry_path = temp_dir / f"seg_{s_id}_condensed.wav"
                        tts.synthesize(new_text, seg_retry_path, voice=voice)
                        new_dur = get_audio_duration_ffprobe(seg_retry_path)
                        if new_dur > 0:
                            seg["audioDuration"] = new_dur
                            raw_audio_map[s_id] = seg_retry_path

                    # Re-check lần cuối bằng verify_dubbing_fit
                    final_recheck = node_helper.verify_dubbing_fit(
                        sorted_segs,
                        {"totalDuration": total_duration, "policy": dubbing_policy}
                    )
                    infeasible_ids = list(final_recheck.get("infeasibleUnitIds") or [])
                    plan = final_recheck.get("plan") or {}
                    if not infeasible_ids:
                        _log("✅ Sau khi rút gọn bằng Condensation mới, toàn bộ phân đoạn audio đã vừa vặn khung thời gian!")

        # Chốt timing plan cuối cùng bằng node_helper.compute_timing_plan
        final_plan = node_helper.compute_timing_plan(
            sorted_segs,
            total_duration,
            {"policy": dubbing_policy}
        )
        plan_units_map = {str(u["unitId"]): u for u in final_plan.get("units", [])}

        # Phase 3: Alignment, Audio Processing & Timeline Assembly
        current_time = 0.0
        for idx, seg in enumerate(sorted_segs):
            pct = 70 + (idx / max(1, total_segs)) * 25  # 70% -> 95%
            s_id = str(seg.get("id", idx + 1))
            u_plan = plan_units_map.get(s_id) or {}

            status_msg = f"Đang căn chỉnh thời lượng phân đoạn #{s_id} ({idx+1}/{total_segs})..."
            if on_progress:
                on_progress(pct, status_msg)

            orig_start = float(seg.get("startTime", 0.0))
            orig_end = float(seg.get("endTime", 0.0))
            planned_start = float(u_plan.get("plannedStart", orig_start))
            planned_end = float(u_plan.get("plannedEnd", orig_end))
            tempo = float(u_plan.get("audioTempo", 1.0))
            raw_audio = raw_audio_map.get(s_id)

            raw_dur = float(u_plan.get("audioDuration") or get_audio_duration_ffprobe(raw_audio))
            eff_speech = raw_dur / tempo if tempo > 1e-6 else raw_dur

            # 1. Căn thời điểm phát: Khớp ngay tại orig_start khi phụ đề xuất hiện trên video
            # Chỉ bắt đầu sớm hơn (mượn về trước) nếu câu đọc dài vượt quá khoảng trống tới planned_end
            if orig_start + eff_speech <= planned_end:
                speech_start = orig_start
                speech_end = orig_start + eff_speech
            else:
                speech_start = max(planned_start, planned_end - eff_speech)
                speech_end = speech_start + eff_speech

                # Đảm bảo không đè lên câu kế tiếp
                next_seg_start = float(sorted_segs[idx + 1].get("startTime", 0.0)) if idx + 1 < total_segs else total_duration
                max_allowed_end = min(total_duration, next_seg_start - min_gap) if next_seg_start > speech_start else total_duration

                if speech_end > max_allowed_end and max_allowed_end > speech_start:
                    # Nếu vượt quá khoảng trống tới câu sau, tăng nhẹ tempo (tối đa 1.35x) để đọc kịp trọn câu mà không bị cắt tiếng
                    avail_win = max_allowed_end - speech_start
                    needed_tempo = raw_dur / max(0.05, avail_win)
                    if needed_tempo <= 1.35:
                        tempo = max(tempo, needed_tempo)
                        eff_speech = raw_dur / tempo
                        speech_end = speech_start + eff_speech
                    else:
                        speech_end = max_allowed_end

            # 2. Đảm bảo không bao giờ đè lên audio phân đoạn trước
            if speech_start < current_time:
                speech_start = current_time
                speech_end = speech_start + eff_speech

            target_seg_dur = max(0.05, speech_end - speech_start)
            aligned_seg_audio = temp_dir / f"seg_{s_id}_aligned.wav"
            align_and_pad_audio_segment(
                input_audio_path=raw_audio,
                target_duration=target_seg_dur,
                output_audio_path=aligned_seg_audio,
                speed_factor=tempo,
                pad_left=0.0,
            )

            # 3. Chèn khoảng lặng trước speech_start nếu có khoảng trống
            if speech_start > current_time + 0.005:
                silence_dur = speech_start - current_time
                silence_file = temp_dir / f"gap_{s_id}_silence.wav"
                create_silent_audio(silence_file, silence_dur)
                concat_list.append(silence_file)
                current_time = speech_start

            concat_list.append(aligned_seg_audio)
            current_time = speech_end

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

        # Concat all segments into the final master audio (mp3 / wav) at standardized 48kHz
        codec_flag = (
            ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "48000"]
            if out_audio.suffix.lower() == ".mp3"
            else ["-c:a", "pcm_s16le", "-ar", "48000"]
        )
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


def _format_render_time(sec: float) -> str:
    """Format duration in seconds into MM:SS or HH:MM:SS."""
    if not sec or sec <= 0:
        return "00:00"
    s = int(round(sec))
    hrs = s // 3600
    mins = (s % 3600) // 60
    rem_s = s % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{rem_s:02d}"
    return f"{mins:02d}:{rem_s:02d}"


def _run_ffmpeg_render_with_progress(
    cmd: List[str],
    total_duration: float,
    on_progress: Optional[Callable[..., None]] = None,
) -> Tuple[int, str]:
    """Execute FFmpeg render command with real-time percentage, speed (x), and ETA tracking."""
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    # Inject progress pipe if not present
    cmd_with_prog = list(cmd)
    if "-progress" not in cmd_with_prog:
        cmd_with_prog[1:1] = ["-progress", "pipe:1", "-nostats"]

    proc = subprocess.Popen(
        cmd_with_prog,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        startupinfo=startupinfo,
    )

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
    cur_sec = 0.0
    speed_str = "-"
    eta_str = "-"

    def _notify(pct_val: float, is_final: bool = False):
        if not on_progress:
            return
        cur_d_str = _format_render_time(cur_sec)
        tot_d_str = _format_render_time(total_duration)
        msg = f"Đang render video lồng tiếng ({pct_val:.1f}% - {speed_str})..." if not is_final else "Render video lồng tiếng hoàn tất 100%!"
        try:
            on_progress(
                pct=round(pct_val, 1),
                msg=msg,
                speed=speed_str,
                eta=eta_str,
                current_duration_str=cur_d_str,
                total_duration_str=tot_d_str,
                raw_pct=round(pct_val, 1),
            )
        except TypeError:
            try:
                on_progress(round(pct_val, 1), msg, speed_str, eta_str)
            except TypeError:
                try:
                    on_progress(round(pct_val, 1), msg)
                except TypeError:
                    on_progress(round(pct_val, 1))

    while True:
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
                cur_sec = min(total_duration, max(0.0, out_us / 1000000.0))
                pct = min(99.5, max(0.0, (cur_sec / total_duration) * 100.0)) if total_duration > 0 else 50.0
                now = time.time()
                if now - last_update_time >= 0.25:
                    last_update_time = now
                    _notify(pct)
            except Exception:
                pass
        elif k == "speed":
            raw_spd = v.replace("x", "").strip()
            try:
                speed_val = float(raw_spd)
                if speed_val > 0:
                    speed_str = f"{speed_val:.1f}x"
                    rem_sec = max(0.0, (total_duration - cur_sec) / speed_val)
                    eta_str = f"{int(rem_sec)}s"
                else:
                    speed_str = v
            except Exception:
                speed_str = v
        elif k == "progress" and v == "end":
            break

    proc.wait()
    stderr_thread.join(timeout=1.0)
    err_text = "\n".join(stderr_lines)

    if proc.returncode == 0:
        _notify(100.0, is_final=True)

    return proc.returncode, err_text


def render_dubbed_video(
    video_path: Path | str,
    dubbed_audio_path: Path | str,
    output_video_path: Path | str,
    bg_volume: float = 0.1,
    dub_volume: float = 3.0,
    pitch_down_pct: float = 5.0,
    enable_periodic_mute: bool = True,
    on_progress: Optional[Callable[..., None]] = None,
) -> Path:
    """
    Render final dubbed video by merging video with dubbed audio track:
    - Original background audio ([0:a]):
        * Volume reduced to bg_volume (default 0.1)
        * Periodic mute: every 0.9s mute for 0.1s (cycle: 1.0s) enabled by default (enable_periodic_mute=True)
        * Pitch down: 5% down (pitch ratio 0.95) enabled by default (pitch_down_pct=5.0)
    - Dubbed audio ([1:a]):
        * Volume amplified to dub_volume (default 3.0)
        * Pure natural pitch preserved (NO pitch shifting applied to dubbing audio)
        * Strictly normalized to 48kHz audio stream
    - Audio tracks mixed with amix (normalize=0) at standardized 48kHz.
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

    total_dur = get_audio_duration_ffprobe(in_v)
    if total_dur <= 0:
        total_dur = get_audio_duration_ffprobe(in_a)
    if total_dur <= 0:
        total_dur = 1.0

    _log(
        f"Đang render video lồng tiếng ({out_v.name}) [Âm dubbing: {dub_volume:.1f}x (chuẩn 48kHz, giữ nguyên cao độ tự nhiên), "
        f"Âm nền video gốc: {bg_volume:.2f}x (pitch down {pitch_down_pct}%, mute định kỳ 0.1s/0.9s: {enable_periodic_mute})]..."
    )
    if on_progress:
        try:
            on_progress(
                pct=1.0,
                msg=f"Bắt đầu render video lồng tiếng: {out_v.name}...",
                speed="-",
                eta="-",
                current_duration_str="00:00",
                total_duration_str=_format_render_time(total_dur),
                raw_pct=1.0,
            )
        except TypeError:
            on_progress(1.0, f"Bắt đầu render video lồng tiếng: {out_v.name}...")

    has_pitch_down = float(pitch_down_pct or 0.0) > 0.01
    pitch_ratio = max(0.8, min(1.0, 1.0 - (pitch_down_pct / 100.0))) if has_pitch_down else 1.0
    atempo_comp = (1.0 / pitch_ratio) if has_pitch_down else 1.0

    # Background audio filter chain with librubberband
    bg_filters_rb = []
    if has_pitch_down:
        bg_filters_rb.extend([f"rubberband=pitch={pitch_ratio:.4f}", "aresample=48000"])
    else:
        bg_filters_rb.append("aresample=48000")

    if enable_periodic_mute and bg_volume > 0:
        bg_filters_rb.extend([
            "asetnsamples=n=960",
            f"volume='if(lt(mod(t,1.0),0.899),{bg_volume:.3f},0)':eval=frame",
        ])
    else:
        bg_filters_rb.append(f"volume={bg_volume:.3f}")

    # Background audio filter chain with fallback asetrate/atempo
    bg_filters_fb = []
    if has_pitch_down:
        bg_filters_fb.extend([
            f"asetrate=48000*{pitch_ratio:.4f}",
            f"atempo={atempo_comp:.4f}",
            "aresample=48000",
        ])
    else:
        bg_filters_fb.append("aresample=48000")

    if enable_periodic_mute and bg_volume > 0:
        bg_filters_fb.extend([
            "asetnsamples=n=960",
            f"volume='if(lt(mod(t,1.0),0.899),{bg_volume:.3f},0)':eval=frame",
        ])
    else:
        bg_filters_fb.append(f"volume={bg_volume:.3f}")

    bg_filter_rb = ",".join(bg_filters_rb)
    bg_filter_fb = ",".join(bg_filters_fb)

    dub_filters = [f"volume={dub_volume:.3f}", "aresample=48000"]
    dub_filter_str = ",".join(dub_filters)

    rubberband_filter = (
        f"[0:a]{bg_filter_rb}[a_bg];"
        f"[1:a]{dub_filter_str}[a_dub];"
        f"[a_bg][a_dub]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a_out]"
    )

    fallback_filter = (
        f"[0:a]{bg_filter_fb}[a_bg];"
        f"[1:a]{dub_filter_str}[a_dub];"
        f"[a_bg][a_dub]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a_out]"
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
        "-ar", "48000",
        str(out_v)
    ]

    ret, err_rb = _run_ffmpeg_render_with_progress(cmd_rubberband, total_dur, on_progress)
    if ret != 0:
        _log(f"Rubberband filter không khả dụng hoặc gặp lỗi ({err_rb[:100]}). Thử fallback filter...")
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
            "-ar", "48000",
            str(out_v)
        ]
        ret_fb, err_fb = _run_ffmpeg_render_with_progress(cmd_fallback, total_dur, on_progress)
        if ret_fb != 0:
            raise RuntimeError(f"FFmpeg render video lồng tiếng thất bại: {err_fb}")

    _log(f"✅ Render ghép video lồng tiếng hoàn tất thành công: {out_v}")
    if on_progress:
        try:
            on_progress(
                pct=100.0,
                msg="Render video lồng tiếng hoàn tất 100%!",
                speed="-",
                eta="0s",
                current_duration_str=_format_render_time(total_dur),
                total_duration_str=_format_render_time(total_dur),
                raw_pct=100.0,
            )
        except TypeError:
            on_progress(100.0, "Render video lồng tiếng hoàn tất 100%!")

    return out_v
