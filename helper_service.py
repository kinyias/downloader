import os
import sys
import re
import uuid
import time
import json
import shutil
import tempfile
import asyncio
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Union, Tuple

# Add workspace root to sys.path to import node_helper
WORKSPACE_ROOT = str(Path(__file__).resolve().parent)
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import node_helper
from config import FFMPEG_PATH, TEMP_DIR, EXPORT_DIR, DEFAULT_SETTINGS
from media_service import get_media_info, get_audio_duration

# Chinese / CJK character detection regex (Unified Ideographs, Ext A, Compatibility)
CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')

# Ending punctuation regex (both Latin and CJK terminal punctuation marks)
TRAILING_PUNCTUATION_PATTERN = re.compile(r'[\s.,?!;:…~—–\-"\'"“”„`·•。！？，；：～]+$')

# Common single-word filler words / gasps in Vietnamese drama subtitles
SINGLE_WORD_FILLERS = {
    "á", "a", "à", "ả", "ã", "ạ",
    "ơ", "ớ", "ờ", "ở", "ỡ", "ợ", "ơi",
    "ừ", "ứ", "ừm", "um", "uh", "ah", "oh",
    "ô", "ồ", "ố", "hả", "hở", "ê", "hế",
    "hừ", "ư", "ứ", "úi", "ôi", "ủa", "ha",
    "hic", "o", "ó", "ò", "chậc", "ơ kìa",
    "ha ha", "a a", "á á", "úi chà", "ôi chao", "ôi trời"
}

def contains_chinese(text: str) -> bool:
    """Return True if text contains any Chinese / Hanzi character."""
    if not text:
        return False
    return bool(CHINESE_PATTERN.search(text))

def clean_subtitle_text(text: str) -> str:
    """
    Filter out all trailing punctuation marks at the end of the subtitle line.
    Preserves inner punctuation if any, but strips terminal . ! ? , ; : ... etc.
    """
    if not text:
        return ""
    cleaned = TRAILING_PUNCTUATION_PATTERN.sub("", text).strip()
    return cleaned

def is_single_word_filler(text: str) -> bool:
    """
    Detect if a subtitle line consists of only 1 word standing alone (like 'á', 'a')
    or is a known meaningless exclamation/filler.
    """
    cleaned = clean_subtitle_text(text).strip()
    if not cleaned:
        return True

    lower = cleaned.lower()
    if lower in SINGLE_WORD_FILLERS:
        return True

    words = cleaned.split()
    # Segments that have only 1 word standing alone (e.g. "á", "a", "ừ", etc.)
    if len(words) <= 1:
        return True

    return False

def load_prompt_by_preset(preset: str = "ai_tong_hop_thong_minh", target_lang: str = "vi") -> str:
    """Load prompt preset from translation/prompts.json."""
    prompts_file = Path(__file__).resolve().parent / "translation" / "prompts.json"
    if prompts_file.exists():
        try:
            with open(prompts_file, "r", encoding="utf-8") as f:
                all_prompts = json.load(f)
                p_obj = all_prompts.get(preset) or all_prompts.get("ai_tong_hop_thong_minh") or all_prompts.get("default")
                if isinstance(p_obj, dict):
                    return p_obj.get(target_lang) or p_obj.get("vi") or ""
                elif isinstance(p_obj, str):
                    return p_obj
        except Exception:
            pass
    return "Bạn là chuyên gia dịch thuật phụ đề phim chuyên nghiệp."


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Helper] {msg}", flush=True)

# In-memory background job manager
JOBS: Dict[str, Dict[str, Any]] = {}

def create_job(job_type: str, initial_message: str = "Đang khởi tạo...") -> str:
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "type": job_type,
        "status": "processing",  # pending, processing, completed, failed
        "progress": 0,
        "message": initial_message,
        "result": None,
        "error": None,
        "createdAt": time.time(),
        "updatedAt": time.time()
    }
    _log(f"[Job Created] ID={job_id[:8]} Type={job_type} Msg='{initial_message}'")
    return job_id

def update_job(job_id: str, progress: Optional[int] = None, message: Optional[str] = None,
               status: Optional[str] = None, result: Optional[Any] = None, error: Optional[str] = None,
               batch_info: Optional[Dict[str, Any]] = None):
    if job_id in JOBS:
        old_status = JOBS[job_id]["status"]
        if progress is not None:
            JOBS[job_id]["progress"] = max(0, min(100, progress))
        if message is not None:
            JOBS[job_id]["message"] = message
        if status is not None:
            JOBS[job_id]["status"] = status
        if result is not None:
            JOBS[job_id]["result"] = result
        if error is not None:
            JOBS[job_id]["error"] = error
        if batch_info is not None:
            JOBS[job_id]["batchInfo"] = batch_info
        JOBS[job_id]["updatedAt"] = time.time()

        if status == "failed" or error:
            _log(f"[Job Failed] ID={job_id[:8]} Error='{error or message}'")
        elif status == "completed":
            dur = time.time() - JOBS[job_id]["createdAt"]
            _log(f"[Job Completed] ID={job_id[:8]} Type={JOBS[job_id]['type']} ({dur:.2f}s)")
        elif status and status != old_status:
            _log(f"[Job Status] ID={job_id[:8]} -> {status} ({JOBS[job_id]['progress']}%) - {message or ''}")

def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return JOBS.get(job_id)

# ==============================================================================
# Helper Service Core Methods
# ==============================================================================

def run_transcribe(video_path: str, source_lang: str = "auto", target_lang: str = "vi",
                   engine: str = "auto", groq_key: Optional[str] = None,
                   tdid: Optional[str] = None, ffmpeg_bin: Optional[str] = None,
                   job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Transcribe video speech to subtitle segments."""
    start_t = time.perf_counter()
    _log(f"[ASR Start] Video='{video_path}', Engine='{engine}', Lang='{source_lang}'->'{target_lang}' (JobID: {job_id[:8] if job_id else 'sync'})")

    def status_callback(msg: str):
        _log(f"[ASR Progress] {msg}")
        if job_id:
            # Dynamically estimate progress if chunk percentage is reported
            pct = 35
            m = re.search(r"(\d+)%", msg)
            if m:
                try:
                    pct = 35 + int(int(m.group(1)) * 0.55)
                except Exception:
                    pass
            update_job(job_id, progress=pct, message=msg)

    if job_id:
        update_job(job_id, 10, "Đang trích xuất và lọc nhiễu âm thanh (FFmpeg Denoise)...")

    settings = {
        "ffmpegPath": ffmpeg_bin or FFMPEG_PATH or "ffmpeg",
        "groqApiKey": groq_key or os.getenv("GROQ_API_KEY", ""),
        "capcutTdid": tdid,
        "onStatus": status_callback
    }
    data = {
        "videoPath": video_path,
        "sourceLang": source_lang,
        "targetLang": target_lang,
        "transcribeEngine": engine,
        "onStatus": status_callback
    }

    if job_id:
        update_job(job_id, 35, f"Đang nhận dạng giọng nói bằng engine '{engine}'...")

    try:
        segments = node_helper.action_transcribe_video(data, settings)
        
        # If segments are empty (e.g. mock/no speech detected), generate default placeholder segment
        if not segments:
            _log(f"[ASR Warning] Không có phân đoạn nào từ '{video_path}'. Đang tạo phân đoạn mẫu.")
            info = get_media_info(video_path, ffmpeg_bin)
            dur = info.get("duration", 5.0)
            segments = [
                {
                    "id": "1",
                    "startTime": 0.0,
                    "endTime": round(min(dur, 4.0), 2),
                    "text": "Phân đoạn mẫu (Chưa phát hiện giọng nói hoặc cần cấu hình Groq Whisper API)",
                    "translation": ""
                }
            ]
        
        elapsed = time.perf_counter() - start_t
        _log(f"[ASR Success] Đã nhận diện {len(segments)} phân đoạn trong {elapsed:.2f}s")

        if job_id:
            update_job(job_id, 100, f"Đã nhận diện thành công {len(segments)} phân đoạn!", status="completed", result=segments)
        return segments
    except Exception as e:
        elapsed = time.perf_counter() - start_t
        _log(f"[ASR Error] Lỗi nhận diện sau {elapsed:.2f}s: {e}")
        if job_id:
            update_job(job_id, 0, f"Lỗi phiên âm: {str(e)}", status="failed", error=str(e))
        raise

def format_srt_timestamp(seconds: float) -> str:
    """Format duration in seconds into SRT timestamp HH:MM:SS,mmm."""
    if not seconds or seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    minute = total_min % 60
    hour = total_min // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"

def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    """Convert a list of transcribed segments into standard SubRip (.srt) subtitle text."""
    srt_blocks = []
    block_idx = 1
    for seg in segments:
        text = str(seg.get("subtitleText") or seg.get("translation") or seg.get("text") or "").strip()
        if not text:
            continue
        st = float(seg.get("startTime", 0.0))
        et = float(seg.get("endTime", 0.0))
        if et <= st:
            et = st + 1.5
        time_str = f"{format_srt_timestamp(st)} --> {format_srt_timestamp(et)}"
        srt_blocks.append(f"{block_idx}\n{time_str}\n{text}\n")
        block_idx += 1

    if not srt_blocks:
        srt_blocks.append(f"1\n00:00:00,000 --> 00:00:05,000\n[Không phát hiện giọng nói rõ ràng]\n")

    return "\n".join(srt_blocks)

def save_srt_file(segments: List[Dict[str, Any]], srt_path: Union[str, Path]) -> Path:
    """Save segments to a .srt subtitle file on disk."""
    path = Path(srt_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    srt_content = segments_to_srt(segments)
    with open(path, "w", encoding="utf-8") as f:
        f.write(srt_content)
    return path

def transcribe_video_to_srt(
    video_path: str,
    srt_path: Optional[str] = None,
    engine: str = "capcut",
    source_lang: str = "auto",
    tdid: Optional[str] = None,
    ffmpeg_bin: Optional[str] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, List[Dict[str, Any]]]:
    """
    Transcribe a video's speech using CapCut ASR (or specified engine) and generate a .srt file.
    Returns (Path(srt_path), segments).
    """
    v_path = Path(video_path).resolve()
    if not v_path.exists() or not v_path.is_file():
        raise FileNotFoundError(f"Video file không tồn tại: {v_path}")

    target_srt = Path(srt_path).resolve() if srt_path else v_path.with_suffix(".srt")

    if on_status:
        on_status(f"[CapCut ASR] Bắt đầu nhận diện giọng nói cho '{v_path.name}'...")

    # Configure callback and run transcription
    settings = {
        "ffmpegPath": ffmpeg_bin or FFMPEG_PATH or "ffmpeg",
        "groqApiKey": os.getenv("GROQ_API_KEY", ""),
        "capcutTdid": tdid,
        "onStatus": on_status or _log
    }
    data = {
        "videoPath": str(v_path),
        "sourceLang": source_lang,
        "targetLang": "vi",
        "transcribeEngine": engine,
        "onStatus": on_status or _log
    }

    segments = node_helper.action_transcribe_video(data, settings)
    saved_path = save_srt_file(segments, target_srt)
    if on_status:
        on_status(f"[CapCut ASR] Đã lưu file phụ đề: '{saved_path.name}' ({len(segments)} câu)")
    return saved_path, segments

def resolve_chat_url(provider: str = "custom", custom_endpoint: Optional[str] = None) -> str:
    """Resolve the OpenAI-compatible chat completions URL."""
    ep = (custom_endpoint or "").strip()
    if ep:
        clean_ep = ep.rstrip("/")
        if clean_ep.lower().endswith("/chat/completions"):
            return clean_ep
        if clean_ep.lower().endswith("/v1"):
            return f"{clean_ep}/chat/completions"
        return f"{clean_ep}/v1/chat/completions"

    prov = (provider or "custom").lower()
    if prov == "deepseek":
        return "https://api.deepseek.com/chat/completions"
    if prov == "openai":
        return "https://api.openai.com/v1/chat/completions"
    if prov == "ollama":
        return "http://localhost:11434/v1/chat/completions"
    if prov == "lmstudio":
        return "http://localhost:1234/v1/chat/completions"
    return "http://localhost:11434/v1/chat/completions"

def fetch_llm_models(endpoint: Optional[str] = None, api_key: Optional[str] = None, provider: Optional[str] = "custom") -> List[Dict[str, str]]:
    """Fetch model list from custom LLM endpoint, Ollama, LM Studio, DeepSeek, or OpenAI."""
    ep = (endpoint or "").strip()
    prov = (provider or "custom").lower()

    # Pre-populate known models for cloud providers if no custom endpoint given
    if not ep:
        if prov == "deepseek":
            ep = "https://api.deepseek.com"
        elif prov == "openai":
            ep = "https://api.openai.com/v1"
        elif prov == "ollama":
            ep = "http://localhost:11434"
        elif prov == "lmstudio":
            ep = "http://localhost:1234/v1"
        elif prov == "ezmax":
            return [{"value": m, "label": m} for m in node_helper.EZMAX_TRANSLATE_MODELS]
        else:
            ep = "http://localhost:11434"

    clean_url = re.sub(r"/chat/completions/?$", "", ep, flags=re.IGNORECASE).rstrip("/")

    urls_to_try = []
    if clean_url.endswith("/v1"):
        urls_to_try.append(f"{clean_url}/models")
        urls_to_try.append(f"{clean_url[:-3]}/models")
        urls_to_try.append(f"{clean_url[:-3]}/api/tags")
    else:
        urls_to_try.append(f"{clean_url}/models")
        urls_to_try.append(f"{clean_url}/v1/models")
        urls_to_try.append(f"{clean_url}/api/tags")

    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key and key != "dummy":
        headers["Authorization"] = f"Bearer {key}"

    last_error = None
    for u in urls_to_try:
        try:
            resp = requests.get(u, headers=headers, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                model_ids = []

                # Format 1: OpenAI standard {"data": [{"id": "model_name"}, ...]}
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    for item in data["data"]:
                        if isinstance(item, dict) and "id" in item:
                            model_ids.append(str(item["id"]))
                        elif isinstance(item, str):
                            model_ids.append(item)

                # Format 2: Ollama native {"models": [{"name": "llama3:latest", "model": "..."}, ...]}
                elif isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
                    for item in data["models"]:
                        if isinstance(item, dict):
                            name = item.get("name") or item.get("model")
                            if name:
                                model_ids.append(str(name))
                        elif isinstance(item, str):
                            model_ids.append(item)

                # Format 3: Raw list: [{"id": "..."}] or ["model1", "model2"]
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "id" in item:
                            model_ids.append(str(item["id"]))
                        elif isinstance(item, str):
                            model_ids.append(item)

                if model_ids:
                    seen = set()
                    unique_models = []
                    for m in model_ids:
                        if m not in seen:
                            seen.add(m)
                            unique_models.append({"value": m, "label": m})
                    _log(f"[Fetch Models] Fetched {len(unique_models)} models from {u}")
                    return unique_models
        except Exception as e:
            last_error = e

    err_msg = f"Không thể lấy model từ endpoint '{ep}'. Vui lòng kiểm tra lại URL hoặc xem server đã khởi động chưa."
    if last_error:
        err_msg += f" (Chi tiết: {last_error})"
    _log(f"[Fetch Models Error] {err_msg}")
    raise RuntimeError(err_msg)

def estimate_tokens(text: str) -> int:
    """Estimate token count matching node_helper.js: CJK = 1 token, Latin/other = len/3.5."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3040 <= cp <= 0x30FF) or (0xAC00 <= cp <= 0xD7AF):
            cjk += 1
        else:
            other += 1
    return int(cjk + other / 3.5) + 1

def chunk_segments_for_translation(segments: List[Dict[str, Any]], 
                                   max_lines: int = 60, 
                                   min_lines_threshold: int = 30,
                                   token_budget: int = 3500) -> List[List[Dict[str, Any]]]:
    """
    Intelligent batching matching node_helper.js standard:
    - Target batch size: 40 to 60 segments (fluctuating based on natural pause / scene break)
    - Hard cap: max_lines = 60 segments per batch
    - When batch size >= min_lines_threshold (40), split at natural break points:
      * Gap between segments (silence >= 1.0s)
      * Strong sentence ending punctuation (. ? ! 。 ！ ？ …)
    - Token budget safeguard to prevent context overflow
    """
    if not segments:
        return []
    
    chunks = []
    current_chunk = []
    current_tokens = 0
    per_line_overhead = 12

    for i, seg in enumerate(segments):
        txt = seg.get("text", "")
        line_tokens = estimate_tokens(txt) + per_line_overhead

        should_split = False
        cur_count = len(current_chunk)

        if cur_count >= max_lines:
            # Reached hard limit of 60 segments
            should_split = True
        elif current_tokens + line_tokens > token_budget and cur_count >= 20:
            # Token budget exceeded
            should_split = True
        elif cur_count >= min_lines_threshold:
            # In the 40-60 fluctuation zone: look for natural boundaries
            prev_seg = current_chunk[-1]
            try:
                time_gap = float(seg.get("startTime", 0)) - float(prev_seg.get("endTime", 0))
            except Exception:
                time_gap = 0.0
            prev_text = prev_seg.get("text", "").strip()
            ends_sentence = any(prev_text.endswith(p) for p in [".", "!", "?", "。", "！", "？", "…"])

            if time_gap >= 1.0 or (ends_sentence and time_gap >= 0.5) or cur_count >= 55:
                should_split = True

        if should_split and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(seg)
        current_tokens += line_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks

def count_vi_syllables(text: str) -> int:
    """Count spoken syllables in Vietnamese matching node_helper.js."""
    if not text:
        return 0
    if hasattr(node_helper, "vi_syllables_spoken"):
        try:
            return node_helper.vi_syllables_spoken(text)
        except Exception:
            pass
    return len([w for w in text.strip().split() if w])

def calculate_segment_budgets(segments: List[Dict[str, Any]], syl_per_sec: float = 4.3) -> List[Dict[str, Any]]:
    """
    Compute budgetSyl for each segment matching node_helper.js:
    dur = endTime - startTime
    borrowableGap = 0.5 * gapBefore + 0.5 * gapAfter (capped at 0.6s each)
    budgetSyl = max(2, int((dur + borrowableGap) * syl_per_sec * 0.95))
    """
    n = len(segments)
    result = []
    for i, s in enumerate(segments):
        start = float(s.get("startTime", 0.0))
        end = float(s.get("endTime", start + 1.0))
        dur = max(0.2, end - start)
        
        gap_before = 0.0
        if i > 0:
            prev_end = float(segments[i-1].get("endTime", 0.0))
            gap_before = max(0.0, start - prev_end - 0.08)
        else:
            gap_before = max(0.0, start)
        gap_before = min(0.6, gap_before * 0.5)
        
        gap_after = 0.0
        if i + 1 < n:
            next_start = float(segments[i+1].get("startTime", end))
            gap_after = max(0.0, next_start - end - 0.08)
        else:
            gap_after = 0.5
        gap_after = min(0.6, gap_after * 0.5)
        
        borrowable_gap = gap_before + gap_after
        budget_syl = max(2, int((dur + borrowable_gap) * syl_per_sec * 0.95))
        
        seg_copy = dict(s)
        seg_copy["budgetSyl"] = budget_syl
        result.append(seg_copy)
    return result

def run_condense_chunk(candidates: List[Dict[str, Any]], 
                       chat_url: str, 
                       headers: Dict[str, str], 
                       model: str, 
                       deep: bool = False) -> Dict[str, str]:
    """
    Perform condensation pass matching node_helper.js:
    - Normal condense (deep=False): 70-85% length
    - Deep condense (deep=True): up to 50% length for lines exceeding video frame
    """
    if not candidates:
        return {}
    
    n_lines = len(candidates)
    if deep:
        system_prompt = (
            f"Bạn là biên tập LỜI ĐỌC lồng tiếng. Các dòng dưới đây ĐÃ rút gọn một lần mà vẫn quá dài so với khung hình, "
            f"nên cần NÉN SÂU: viết lại BẢN ĐỌC từ BẢN ĐỦ, độ dài NẰM TRONG khoảng âm tiết cho phép của dòng đó "
            f"(1 từ tiếng Việt = 1 âm tiết) — khoảng 50–70% BẢN ĐỦ, KHÔNG được ngắn hơn mức tối thiểu. "
            f"BẮT BUỘC GIỮ: ý cốt lõi (ai làm gì), con số, tên riêng, phủ định và cách xưng hô. "
            f"Được phép: bỏ mệnh đề phụ/chi tiết bổ trợ, bỏ ví von nếu buộc phải chọn, gộp ý bằng cách nói ngắn tự nhiên. "
            f"KHÔNG thêm ý mới, KHÔNG đổi nghĩa, KHÔNG viết cụt lủn kiểu điện tín — vẫn là câu nói trọn vẹn, đủ dấu câu.\n"
            f"Trả về ĐÚNG {n_lines} dòng, mỗi dòng 'ID|bản đọc'. KHÔNG markdown, KHÔNG giải thích."
        )
    else:
        system_prompt = (
            f"Bạn là biên tập LỜI ĐỌC lồng tiếng. Với mỗi dòng bên dưới, viết BẢN ĐỌC gọn hơn từ BẢN ĐỦ, "
            f"độ dài NẰM TRONG khoảng âm tiết cho phép của dòng đó (1 từ tiếng Việt = 1 âm tiết) — "
            f"tức khoảng 70–85% BẢN ĐỦ, KHÔNG được ngắn hơn mức tối thiểu. "
            f"GIỮ NGUYÊN: ý chính, hành động, sắc thái/so sánh, con số, tên riêng, phủ định và cách xưng hô. "
            f"Được phép: bỏ từ đưa đẩy/đệm, rút gọn cấu trúc, thay cụm dài bằng cách nói ngắn tự nhiên. "
            f"KHÔNG thêm ý mới, KHÔNG đổi nghĩa, KHÔNG viết cụt lủn kiểu điện tín. Văn nói tự nhiên, đủ dấu câu.\n"
            f"Trả về ĐÚNG {n_lines} dòng, mỗi dòng 'ID|bản đọc'. KHÔNG markdown, KHÔNG giải thích."
        )

    user_lines = []
    for idx, c in enumerate(candidates):
        seg = c["seg"]
        full_trans = c["full"]
        full_syl = count_vi_syllables(full_trans)
        
        if deep:
            budget_min = max(2, int(full_syl * 0.50))
            budget_max = max(budget_min, min(full_syl - 1, seg.get("budgetSyl", full_syl)))
        else:
            budget_min = max(2, int(full_syl * 0.70))
            budget_max = max(budget_min, min(full_syl - 1, max(seg.get("budgetSyl", full_syl), int(full_syl * 0.85))))

        range_str = f"{budget_min}–{budget_max}" if budget_min < budget_max else f"≤{budget_max}"
        src_text = seg.get("text", "")
        line_str = f"{idx + 1}|[{range_str} âm tiết] GỐC: {src_text} | BẢN ĐỦ: {full_trans}"
        user_lines.append(line_str)

    user_prompt = "\n".join(user_lines)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "stream": False
    }

    try:
        resp = requests.post(chat_url, headers=headers, json=payload, timeout=120)
        content = ""
        if resp.status_code == 200:
            raw_text = resp.text.strip()
            if raw_text.startswith("data:"):
                for line in raw_text.splitlines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        try:
                            c_obj = json.loads(line[5:].strip())
                            content += c_obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        except Exception:
                            pass
            else:
                resp_json = resp.json()
                content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        results = {}
        for line in content.splitlines():
            line = line.strip()
            if "|" in line:
                parts = line.split("|", 1)
                try:
                    line_num = int(parts[0].strip())
                    if 1 <= line_num <= len(candidates):
                        seg_id = str(candidates[line_num - 1]["seg"]["id"])
                        results[seg_id] = parts[1].strip()
                except Exception:
                    pass
        return results
    except Exception as e:
        _log(f"[Condense Error] Lỗi khi gọi LLM rút gọn (deep={deep}): {e}")
        return {}

def run_translate_segments(segments: List[Dict[str, Any]], target_lang: str = "vi",
                           source_lang: str = "auto", preset: str = "ai_tong_hop_thong_minh",
                           model: str = "deepseek-chat", provider: str = "deepseek",
                           glossary: Optional[List[Dict[str, str]]] = None,
                           custom_prompt: Optional[str] = None,
                           api_key: Optional[str] = None,
                           custom_endpoint: Optional[str] = None,
                           job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Translate subtitle segments using LLM with support for custom endpoints and OpenAI-compatible APIs."""
    start_t = time.perf_counter()
    total_segs = len(segments)
    chat_url = resolve_chat_url(provider, custom_endpoint)
    _log(f"[Translate Start] Segments={total_segs}, Provider='{provider}', Endpoint='{chat_url}', Model='{model}'")

    if job_id:
        update_job(job_id, 10, f"Đang chuẩn bị dịch {total_segs} phân đoạn bằng {model}...")

    # Load prompt preset
    prompt_text = custom_prompt
    if not prompt_text:
        prompt_text = load_prompt_by_preset(preset, target_lang)

    if not prompt_text:
        prompt_text = "Bạn là chuyên gia dịch thuật phụ đề phim chuyên nghiệp."

    # Inject glossary rules
    if glossary and len(glossary) > 0:
        valid_items = [g for g in glossary if g.get("src") and g.get("tgt")]
        if valid_items:
            glossary_lines = [f"- '{g['src']}' ➔ '{g['tgt']}'" for g in valid_items]
            prompt_text += "\n\n★ THUẬT NGỮ BẮT BUỘC DỊCH CHUẨN (GLOSSARY):\n" + "\n".join(glossary_lines)

    # Inject strict JSON output rules
    prompt_text += (
        f"\n\n★ QUY TẮC ĐẦU RA BẮT BUỘC:\n"
        f"1. Dịch chuẩn xác sang ngôn ngữ đích: {target_lang}.\n"
        f"2. Trả về DUY NHẤT một chuỗi JSON hợp lệ dạng danh sách các object [{{'id': '<id>', 'translation': '<bản dịch>'}}].\n"
        f"3. TUYỆT ĐỐI KHÔNG giải thích, KHÔNG thêm lời chào hay ghi chú. Giữ nguyên 100% ID và thứ tự các phân đoạn."
    )

    # Resolve API key
    key = (api_key or "").strip()
    if not key:
        if custom_endpoint:
            key = os.getenv("CUSTOM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
        else:
            prov = (provider or "").lower()
            if prov == "deepseek":
                key = os.getenv("DEEPSEEK_API_KEY", "")
            elif prov == "openai":
                key = os.getenv("OPENAI_API_KEY", "")
            elif prov == "custom":
                key = os.getenv("CUSTOM_API_KEY", "")
    if not key:
        key = DEFAULT_SETTINGS.get("customApiKey", "")

    headers = {"Content-Type": "application/json"}
    if key and key != "dummy":
        headers["Authorization"] = f"Bearer {key}"

    results_map: Dict[str, str] = {}
    # Smart batching matching node_helper.js (40-60 segments per batch)
    chunks = chunk_segments_for_translation(segments, max_lines=60, min_lines_threshold=40)
    total_batches = len(chunks)
    batch_sizes_str = ", ".join(f"B{idx+1}:{len(c)}" for idx, c in enumerate(chunks))

    _log("=" * 70)
    _log(f"[Translate KHỞI TẠO] Bắt đầu dịch thuật {total_segs} phân đoạn (segments)")
    _log(f"  • Cơ chế chia batch (Chuẩn node_helper.js): Dao động 40-60 câu/batch (Tối đa 60 câu)")
    _log(f"  • Tổng cộng: {total_batches} batch(es) [{batch_sizes_str}]")
    _log(f"  • Model: '{model}' | Provider: '{provider}'")
    _log(f"  • Endpoint: '{chat_url}'")
    _log("=" * 70)

    if job_id:
        update_job(
            job_id,
            5,
            f"Khởi tạo dịch {total_segs} phân đoạn (chia {total_batches} batch, mỗi batch 40-60 câu)...",
            batch_info={
                "totalBatches": total_batches,
                "currentBatch": 0,
                "totalSegments": total_segs,
                "completedSegments": 0,
                "batchStartSeg": 0,
                "batchEndSeg": 0
            }
        )

    # Pre-calculate budgets for spokenText condensation if Vietnamese
    spoken_map: Dict[str, str] = {}
    budget_lookup: Dict[str, int] = {}
    budgeted_segments_map: Dict[str, Dict[str, Any]] = {}
    is_vietnamese = (target_lang or "").lower().startswith("vi")
    if is_vietnamese:
        budgeted_segments = calculate_segment_budgets(segments)
        for s in budgeted_segments:
            s_id = str(s.get("id"))
            budget_lookup[s_id] = s.get("budgetSyl", 0)
            budgeted_segments_map[s_id] = s

    cur_start_seg = 1
    for chunk_idx, chunk in enumerate(chunks):
        batch_num = chunk_idx + 1
        batch_start_seg = cur_start_seg
        batch_end_seg = min(cur_start_seg + len(chunk) - 1, total_segs)
        cur_start_seg += len(chunk)
        pct = int(5 + (chunk_idx / total_batches) * 90)

        _log(
            f"--> [Translate Batch {batch_num}/{total_batches}] Đang dịch {len(chunk)} câu "
            f"(Segment #{batch_start_seg} ➔ #{batch_end_seg} / {total_segs}) qua LLM [{model}]..."
        )

        if job_id:
            update_job(
                job_id,
                pct,
                f"Đang dịch batch {batch_num}/{total_batches} (phân đoạn #{batch_start_seg} ➔ #{batch_end_seg}/{total_segs} câu)...",
                batch_info={
                    "totalBatches": total_batches,
                    "currentBatch": batch_num,
                    "totalSegments": total_segs,
                    "completedSegments": max(0, batch_start_seg - 1),
                    "batchStartSeg": batch_start_seg,
                    "batchEndSeg": batch_end_seg
                }
            )

        batch_t0 = time.perf_counter()

        chunk_input = [{"id": s.get("id", str(idx + 1)), "text": s.get("text", "")} for idx, s in enumerate(chunk)]

        payload = {
            "model": model or "gemini-lite",
            "messages": [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": json.dumps(chunk_input, ensure_ascii=False)}
            ],
            "temperature": 0.3,
            "stream": False
        }

        try:
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=600)
            if resp.status_code != 200:
                err_detail = resp.text
                try:
                    err_json = resp.json()
                    err_detail = err_json.get("error", {}).get("message") or err_json.get("message") or resp.text
                except Exception:
                    pass
                raise RuntimeError(f"Lỗi API ({resp.status_code}): {err_detail}")

            raw_text = (resp.text or "").strip()
            if not raw_text:
                raise RuntimeError("Server LLM trả về phản hồi rỗng (0 bytes). Vui lòng kiểm tra lại cấu hình endpoint và model.")

            # Check if response is SSE (Server-Sent Events) or raw streaming text
            content = ""
            is_sse = resp.headers.get("content-type", "").startswith("text/event-stream") or raw_text.startswith("data:")

            if is_sse:
                content_parts = []
                for line in raw_text.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk_obj = json.loads(data_str)
                        if "error" in chunk_obj:
                            err_msg = chunk_obj["error"].get("message") if isinstance(chunk_obj["error"], dict) else str(chunk_obj["error"])
                            raise RuntimeError(f"Lỗi từ LLM: {err_msg}")
                        delta = chunk_obj.get("choices", [{}])[0].get("delta", {})
                        c = delta.get("content", "")
                        if c:
                            content_parts.append(c)
                    except Exception as e:
                        if "Lỗi từ LLM:" in str(e):
                            raise
                content = "".join(content_parts).strip()
            else:
                try:
                    resp_data = resp.json()
                except Exception as json_err:
                    raise RuntimeError(f"Phản hồi từ LLM không đúng định dạng JSON: {raw_text[:200]}") from json_err

                choices = resp_data.get("choices") or []
                if not choices:
                    if "error" in resp_data:
                        err_msg = resp_data["error"].get("message") if isinstance(resp_data["error"], dict) else str(resp_data["error"])
                        raise RuntimeError(f"Lỗi từ LLM: {err_msg}")
                    raise RuntimeError(f"LLM không trả về kết quả choices: {raw_text[:200]}")

                content = choices[0].get("message", {}).get("content", "").strip()

            if not content:
                raise RuntimeError("Nội dung dịch từ LLM bị rỗng.")

            # Strip markdown fence if present
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0].strip()

            # Parse JSON
            parsed_items = None
            try:
                parsed_items = json.loads(content)
            except Exception:
                # Fallback regex extraction of {"id": ..., "translation": ...}
                matches = re.findall(r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"(?:translation|text|target|vi)"\s*:\s*"([^"]*)"\s*\}', content)
                if matches:
                    parsed_items = [{"id": m[0], "translation": m[1]} for m in matches]

            if isinstance(parsed_items, list):
                for item in parsed_items:
                    if isinstance(item, dict) and "id" in item:
                        trans_val = item.get("translation") or item.get("text") or item.get("target") or item.get("vi") or ""
                        results_map[str(item["id"])] = trans_val
            else:
                # Fallback: line-by-line pairing if LLM answered in raw text
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                for idx, s in enumerate(chunk):
                    s_id = str(s.get("id", idx + 1))
                    if idx < len(lines):
                        results_map[s_id] = lines[idx]

            # Kiểm tra và thực hiện rút gọn lời đọc ngay sau mỗi batch hoàn thành
            if is_vietnamese:
                pass1_candidates = []
                for s in chunk:
                    s_id = str(s.get("id", ""))
                    budgeted_s = budgeted_segments_map.get(s_id, s)
                    full_trans = results_map.get(s_id) or s.get("translation") or s.get("text", "")
                    syl = count_vi_syllables(full_trans)
                    b_syl = budgeted_s.get("budgetSyl", 0)
                    if b_syl > 0 and syl > b_syl * 1.06:
                        pass1_candidates.append({"seg": budgeted_s, "full": full_trans, "idx": s_id})

                if pass1_candidates:
                    _log(f"--> [Batch {batch_num}/{total_batches}] Rút gọn lời đọc cho {len(pass1_candidates)} câu vượt khung thời lượng...")
                    if job_id:
                        update_job(
                            job_id,
                            int(pct_so_far * 0.95),
                            f"Batch {batch_num}/{total_batches}: Đang rút gọn lời đọc cho {len(pass1_candidates)} câu...",
                        )

                    p1_results = run_condense_chunk(pass1_candidates, chat_url, headers, model, deep=False)
                    for c in pass1_candidates:
                        c_id = str(c["seg"]["id"])
                        if c_id in p1_results and p1_results[c_id]:
                            spoken_map[c_id] = p1_results[c_id]

                    # Pass 2: Rút gọn sâu (tối đa 50%) cho những câu trong batch vẫn còn vượt thời lượng hình
                    deep_candidates = []
                    for c in pass1_candidates:
                        c_id = str(c["seg"]["id"])
                        cur_read = spoken_map.get(c_id) or c["full"]
                        cur_syl = count_vi_syllables(cur_read)
                        b_syl = c["seg"].get("budgetSyl", 0)
                        if b_syl > 0 and cur_syl > b_syl * 1.06:
                            deep_candidates.append({
                                "seg": c["seg"],
                                "full": c["full"],
                                "current_read": cur_read,
                                "idx": c_id
                            })

                    if deep_candidates:
                        _log(f"--> [Batch {batch_num}/{total_batches}] Rút gọn sâu (tối đa 50%) cho {len(deep_candidates)} câu vượt thời lượng hình...")
                        deep_results = run_condense_chunk(deep_candidates, chat_url, headers, model, deep=True)
                        for c in deep_candidates:
                            c_id = str(c["seg"]["id"])
                            if c_id in deep_results and deep_results[c_id]:
                                condensed_text = deep_results[c_id]
                                orig_syl = count_vi_syllables(c["full"])
                                new_syl = count_vi_syllables(condensed_text)
                                if new_syl < orig_syl:
                                    spoken_map[c_id] = condensed_text
                                    _log(
                                        f"    • Segment #{c_id}: BẢN ĐỦ ({orig_syl} âm tiết) ➔ "
                                        f"RÚT GỌN SÂU ({new_syl} âm tiết / Ngân sách {c['seg'].get('budgetSyl')} âm tiết): '{condensed_text}'"
                                    )

            batch_dur = time.perf_counter() - batch_t0
            completed_so_far = batch_end_seg
            pct_so_far = (completed_so_far / total_segs) * 100
            _log(
                f"<-- [Translate Batch {batch_num}/{total_batches} HOÀN THÀNH] Xong {len(chunk)} câu trong {batch_dur:.2f}s "
                f"| Đã dịch & rút gọn: {completed_so_far}/{total_segs} segments ({pct_so_far:.1f}%)"
            )
            if job_id:
                update_job(
                    job_id,
                    int(pct_so_far * 0.95),
                    f"Đã xong batch {batch_num}/{total_batches} ({completed_so_far}/{total_segs} câu, {pct_so_far:.0f}%)...",
                    batch_info={
                        "totalBatches": total_batches,
                        "currentBatch": batch_num,
                        "totalSegments": total_segs,
                        "completedSegments": completed_so_far,
                        "batchStartSeg": batch_start_seg,
                        "batchEndSeg": batch_end_seg
                    }
                )

        except Exception as e:
            _log(f"[Translate Error] Batch {chunk_idx + 1}/{total_batches} thất bại: {e}")
            if job_id:
                update_job(job_id, 0, f"Lỗi dịch phân đoạn: {str(e)}", status="failed", error=str(e))
            raise RuntimeError(f"Lỗi khi dịch qua LLM ({chat_url}): {str(e)}")

    # Assemble final segments list
    final_segments = []
    for s in segments:
        s_id = str(s.get("id", ""))
        translated_text = results_map.get(s_id) or s.get("translation") or s.get("text", "")
        spoken_text = spoken_map.get(s_id) or s.get("spokenText") or translated_text
        b_syl = budget_lookup.get(s_id) or s.get("budgetSyl")

        final_segments.append({
            **s,
            "translation": translated_text,
            "spokenText": spoken_text,
            "subtitleText": s.get("subtitleText") or translated_text,
            "budgetSyl": b_syl
        })

    elapsed = time.perf_counter() - start_t
    _log("=" * 70)
    _log(
        f"[Translate THÀNH CÔNG] Hoàn tất 100% dịch {total_segs}/{total_segs} phân đoạn qua {total_batches} batch(es) "
        f"trong {elapsed:.2f}s (Tốc độ trung bình: {elapsed/total_batches:.2f}s/batch)!"
    )
    _log("=" * 70)

    if job_id:
        update_job(
            job_id,
            100,
            f"✓ Hoàn thành dịch 100% ({total_segs}/{total_segs} phân đoạn qua {total_batches} batch) trong {elapsed:.1f}s!",
            status="completed",
            result=final_segments,
            batch_info={
                "totalBatches": total_batches,
                "currentBatch": total_batches,
                "totalSegments": total_segs,
                "completedSegments": total_segs,
                "batchStartSeg": 1,
                "batchEndSeg": total_segs
            }
        )

    return final_segments


def translate_and_clean_subtitles(
    segments: List[Dict[str, Any]],
    dest_srt_path: Optional[Union[str, Path]] = None,
    target_lang: str = "vi",
    source_lang: str = "auto",
    preset: str = "ai_tong_hop_thong_minh",
    model: Optional[str] = None,
    provider: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    api_key: Optional[str] = None,
    custom_endpoint: Optional[str] = None,
    on_status: Optional[Callable[[str], None]] = None,
    max_retries: int = 2,
) -> Tuple[Optional[Path], List[Dict[str, Any]]]:
    """
    Translate subtitle segments to Vietnamese with automated Chinese character detection,
    targeted re-translation of any segment still containing Chinese characters,
    removal of 1-word standalone filler segments ("á", "a", etc.),
    removal of ending punctuation, and saving to an SRT file.
    """
    if not segments:
        return None, []

    def _status(msg: str):
        _log(msg)
        if on_status:
            on_status(msg)

    # Resolve default configuration values
    resolved_endpoint = (custom_endpoint or "").strip() or os.getenv("CUSTOM_API_ENDPOINT") or DEFAULT_SETTINGS.get("customApiEndpoint", "")
    resolved_key = (api_key or "").strip() or os.getenv("CUSTOM_API_KEY") or DEFAULT_SETTINGS.get("customApiKey", "")
    resolved_model = (model or "").strip() or os.getenv("CUSTOM_MODEL") or DEFAULT_SETTINGS.get("customModel", "gemini-lite")
    resolved_provider = provider or ("custom" if resolved_endpoint else "deepseek")

    _status(
        f"[Dịch phụ đề] Bắt đầu dịch {len(segments)} phân đoạn sang tiếng Việt "
        f"(Model: '{resolved_model}', Preset: '{preset}')..."
    )

    # Step 1: Initial translation pass
    translated_segs = run_translate_segments(
        segments=segments,
        target_lang=target_lang,
        source_lang=source_lang,
        preset=preset,
        model=resolved_model,
        provider=resolved_provider,
        custom_prompt=custom_prompt,
        api_key=resolved_key,
        custom_endpoint=resolved_endpoint,
    )

    # Step 2: Verification pass & targeted re-translation for segments containing Chinese characters
    for retry_round in range(1, max_retries + 1):
        chinese_indices = [
            idx for idx, s in enumerate(translated_segs)
            if contains_chinese(s.get("translation") or s.get("subtitleText") or "")
        ]
        if not chinese_indices:
            _status("[Dịch phụ đề] ✅ Toàn bộ phân đoạn đã sạch chữ tiếng Trung!")
            break

        _status(
            f"[Dịch phụ đề] ⚠️ Phát hiện {len(chinese_indices)} phân đoạn còn chứa chữ tiếng Trung "
            f"(Lần kiểm tra {retry_round}/{max_retries}). Đang tiến hành dịch lại..."
        )

        retry_segments = [
            {
                "id": str(idx + 1),
                "text": translated_segs[idx].get("text") or translated_segs[idx].get("translation") or "",
                "startTime": translated_segs[idx].get("startTime", 0.0),
                "endTime": translated_segs[idx].get("endTime", 0.0),
            }
            for idx in chinese_indices
        ]

        base_prompt = custom_prompt or load_prompt_by_preset(preset, target_lang)
        retranslate_prompt = (
            f"{base_prompt}\n\n"
            f"★ YÊU CẦU ĐẶC BIỆT BẮT BUỘC (DỊCH LẠI CHỮ TRUNG CÒN SÓT):\n"
            f"Các câu sau trước đó vẫn còn sót chữ Hán/tiếng Trung. "
            f"Bạn BẮT BUỘC phải dịch 100% sang tiếng Việt, TUYỆT ĐỐI KHÔNG ĐƯỢC để lại bất kỳ chữ Hán "
            f"hay ký tự tiếng Trung nào trong bản dịch (chuyển ngữ nghĩa hoàn toàn hoặc phiên âm Hán-Việt chuẩn xác nếu là danh từ riêng)."
        )

        try:
            retranslated_results = run_translate_segments(
                segments=retry_segments,
                target_lang=target_lang,
                source_lang=source_lang,
                preset=preset,
                model=resolved_model,
                provider=resolved_provider,
                custom_prompt=retranslate_prompt,
                api_key=resolved_key,
                custom_endpoint=resolved_endpoint,
            )
            for r in retranslated_results:
                orig_idx = int(r.get("id", 0)) - 1
                if 0 <= orig_idx < len(translated_segs):
                    t_val = r.get("translation") or r.get("subtitleText") or r.get("text", "")
                    translated_segs[orig_idx]["translation"] = t_val
                    translated_segs[orig_idx]["subtitleText"] = t_val
        except Exception as retry_err:
            _status(f"[Dịch phụ đề] Lỗi trong quá trình dịch lại phân đoạn tiếng Trung: {retry_err}")
            break

    # If any Chinese character still remains after retries, strip residual Chinese characters
    for s in translated_segs:
        cur_t = s.get("translation") or ""
        if contains_chinese(cur_t):
            cleaned_zh = CHINESE_PATTERN.sub("", cur_t).strip()
            s["translation"] = cleaned_zh
            s["subtitleText"] = cleaned_zh

    # Step 3: Filtering and cleaning
    # - Loại bỏ các segment chỉ có 1 từ đứng một mình như "á", "a"
    # - Lọc hết toàn bộ dấu câu cuối câu
    cleaned_segs = []
    removed_fillers_count = 0
    for s in translated_segs:
        raw_text = str(s.get("subtitleText") or s.get("translation") or s.get("text") or "").strip()

        # Check if segment has only 1 word standing alone (like "á", "a") or is filler
        if is_single_word_filler(raw_text):
            removed_fillers_count += 1
            continue

        # Strip all ending punctuation
        cleaned_text = clean_subtitle_text(raw_text)
        if not cleaned_text:
            removed_fillers_count += 1
            continue

        s_copy = dict(s)
        s_copy["translation"] = cleaned_text
        s_copy["subtitleText"] = cleaned_text
        s_copy["spokenText"] = clean_subtitle_text(s.get("spokenText") or cleaned_text)
        cleaned_segs.append(s_copy)

    _status(
        f"[Dịch phụ đề] Đã làm sạch dấu câu cuối câu và loại bỏ {removed_fillers_count} phân đoạn 1 từ/từ đệm thừa. "
        f"Còn lại {len(cleaned_segs)} phân đoạn phụ đề hoàn chỉnh."
    )

    # Re-index segments 1..N
    for i, s in enumerate(cleaned_segs, 1):
        s["id"] = str(i)

    # Save to disk if dest_srt_path is provided
    out_file = None
    if dest_srt_path:
        out_file = Path(dest_srt_path).resolve()
        save_srt_file(cleaned_segs, out_file)
        _status(f"[Dịch phụ đề] Đã lưu file phụ đề tiếng Việt: {out_file.name}")

    return out_file, cleaned_segs



def run_generate_single_tts(text: str, voice_id: str = "vi-VN-HoaiMyNeural",
                            speed: float = 1.0, dest_path: Optional[str] = None,
                            settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Synthesize speech audio for a single piece of text."""
    start_t = time.perf_counter()
    if not dest_path:
        dest_path = str(TEMP_DIR / f"tts_{uuid.uuid4().hex[:8]}.wav")
    
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    router = node_helper.TtsRouter(settings or {})
    router.synthesize(text, dest_path, {"voiceId": voice_id, "speed": speed})

    dur = get_audio_duration(dest_path)
    if dur <= 0.0:
        # Fallback calculate from syllables
        syls = node_helper.vi_syllables_spoken(text)
        dur = max(0.6, round(syls / (3.8 * speed), 2))

    elapsed = time.perf_counter() - start_t
    _log(f"[TTS Single] Voice='{voice_id}' -> Audio={dur:.2f}s ({elapsed:.2f}s elapsed)")

    return {
        "success": True,
        "outputPath": os.path.abspath(dest_path),
        "duration": dur,
        "voiceId": voice_id,
        "speed": speed
    }

def run_batch_tts(segments: List[Dict[str, Any]], default_voice: str = "vi-VN-HoaiMyNeural",
                  default_speed: float = 1.0, speaker_map: Optional[Dict[str, str]] = None,
                  job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Synthesize TTS for all segments in batch."""
    start_t = time.perf_counter()
    speaker_map = speaker_map or {}
    total_segs = len(segments)
    results = []

    _log(f"[Batch TTS Start] {total_segs} phân đoạn, DefaultVoice='{default_voice}' (JobID: {job_id[:8] if job_id else 'sync'})")

    if job_id:
        update_job(job_id, 5, f"Bắt đầu tạo giọng đọc TTS cho {total_segs} phân đoạn...")

    for i, seg in enumerate(segments):
        speaker = seg.get("speakerId")
        voice = speaker_map.get(speaker) or default_voice
        speed = default_speed
        
        spoken = seg.get("spokenText") or seg.get("translation") or seg.get("text") or ""
        out_wav = str(TEMP_DIR / f"seg_{seg.get('id', i+1)}_{uuid.uuid4().hex[:6]}.wav")
        
        tts_res = run_generate_single_tts(spoken, voice, speed, out_wav)
        
        res_seg = {
            **seg,
            "spokenText": spoken,
            "audioPath": tts_res["outputPath"],
            "audioDuration": tts_res["duration"]
        }
        results.append(res_seg)

        if job_id:
            pct = int(5 + (i + 1) / total_segs * 90)
            update_job(job_id, pct, f"Đang tạo giọng đọc TTS phân đoạn {i+1}/{total_segs}...")

    elapsed = time.perf_counter() - start_t
    _log(f"[Batch TTS Success] Đã tạo giọng đọc cho {len(results)} phân đoạn trong {elapsed:.2f}s")

    if job_id:
        update_job(job_id, 100, f"Đã tạo giọng đọc thành công cho {len(results)} phân đoạn!", status="completed", result=results)

    return results

def run_compute_timing_plan(segments: List[Dict[str, Any]], total_duration: float = 0.0,
                            global_voice_rate: float = 1.0, suggest_rate: bool = True,
                            policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute timing borrowing, cluster slowing, and suggest optimal rate."""
    data = {
        "segments": segments,
        "totalDuration": total_duration,
        "globalVoiceRate": global_voice_rate,
        "suggestRate": suggest_rate,
        "policy": policy or node_helper.DEFAULT_POLICY
    }
    return node_helper.action_compute_timing_plan(data)

def run_export_video_pipeline(video_path: str, output_path: str, segments: List[Dict[str, Any]],
                              video_segments: Optional[List[Dict[str, Any]]] = None,
                              timing_plan: Optional[Dict[str, Any]] = None,
                              fit_mode: str = "natural_flow",
                              export_speed: float = 1.0,
                              aspect_ratio: str = "original",
                              resolution: str = "original",
                              color_filter: Optional[str] = None,
                              color_filter_intensity: float = 1.0,
                              subtitle_style: Optional[Dict[str, Any]] = None,
                              watermark: Optional[Dict[str, Any]] = None,
                              audio_bed_vol: float = 0.3,
                              tts_vol: float = 1.0,
                              encoder_preset: str = "veryfast",
                              crf: int = 22,
                              ffmpeg_bin: Optional[str] = None,
                              job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Complete video export pipeline:
    1. Probe media geometry
    2. Generate ASS subtitle file
    3. Build Color grading filter
    4. Mix TTS voice track and original background audio
    5. Render final MP4
    """
    start_t = time.perf_counter()
    _log(f"[Export Video Start] Video='{video_path}', Out='{output_path}', AspectRatio='{aspect_ratio}' (JobID: {job_id[:8] if job_id else 'sync'})")

    if job_id:
        update_job(job_id, 5, "Bắt đầu khởi tạo quy trình xuất bản video...")

    ffmpeg_bin = ffmpeg_bin or FFMPEG_PATH or "ffmpeg"
    media_info = get_media_info(video_path, ffmpeg_bin)
    orig_w, orig_h = media_info.get("width", 1920), media_info.get("height", 1080)
    out_w, out_h = node_helper.compute_output_frame(orig_w, orig_h, aspect_ratio, resolution)

    if job_id:
        update_job(job_id, 20, f"Đang tạo file phụ đề ASS và cấu hình kích thước ({out_w}x{out_h})...")

    # Generate ASS file
    ass_path = str(TEMP_DIR / f"sub_{uuid.uuid4().hex[:8]}.ass")
    ass_content = node_helper.generate_ass_file(segments, subtitle_style or {}, out_w, out_h)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    # Build video filter chain
    vf_filters = []
    
    # Scale & Pad for Aspect Ratio
    vf_filters.append(f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2")

    # Color grading
    if color_filter and color_filter_intensity > 0.0:
        c_filter = node_helper.build_color_filter_chain(color_filter, color_filter_intensity, out_w, out_h)
        if c_filter:
            vf_filters.append(c_filter)

    # Subtitle burn-in (escape path for FFmpeg filter)
    escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")
    vf_filters.append(f"subtitles='{escaped_ass}'")

    # Watermark
    if watermark and watermark.get("text"):
        wm_text = watermark["text"].replace("'", "")
        vf_filters.append(f"drawtext=text='{wm_text}':fontcolor=white@0.8:fontsize={watermark.get('fontSize', 24)}:x=(w-text_w)/2:y=h-text_h-20")

    vf_chain = ",".join(vf_filters)

    if job_id:
        update_job(job_id, 40, "Đang hòa trộn âm thanh và biên dịch FFmpeg...")

    # Build FFmpeg command
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", video_path,
        "-vf", vf_chain,
        "-c:v", "libx264",
        "-preset", encoder_preset or "veryfast",
        "-crf", str(crf or 22),
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    if job_id:
        update_job(job_id, 60, "Đang kết xuất video qua FFmpeg (Video encoding)...")

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            err_msg = proc.stderr[-800:] if proc.stderr else "Unknown error"
            _log(f"[Export Error] FFmpeg thất bại (code {proc.returncode}): {err_msg}")
            raise RuntimeError(f"FFmpeg error: {err_msg}")

        out_info = get_media_info(output_path, ffmpeg_bin)
        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        elapsed = time.perf_counter() - start_t

        result_data = {
            "success": True,
            "outputPath": os.path.abspath(output_path),
            "filename": os.path.basename(output_path),
            "duration": out_info.get("duration", 0.0),
            "width": out_w,
            "height": out_h,
            "size": os.path.getsize(output_path),
            "assPath": os.path.abspath(ass_path)
        }

        _log(f"[Export Success] Xuất video hoàn tất: '{output_path}' ({file_size_mb:.2f}MB, {out_info.get('duration', 0.0):.2f}s) trong {elapsed:.2f}s")

        if job_id:
            update_job(job_id, 100, "Xuất bản video thành công!", status="completed", result=result_data)

        return result_data

    except Exception as e:
        elapsed = time.perf_counter() - start_t
        _log(f"[Export Error] Lỗi kết xuất sau {elapsed:.2f}s: {e}")
        if job_id:
            update_job(job_id, 0, f"Lỗi kết xuất video: {str(e)}", status="failed", error=str(e))
        raise
