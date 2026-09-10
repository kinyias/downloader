#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Short Drama CLI - Command Line Interface for Searching, Downloading & Merging Short Dramas.
Features:
  - Interactive menu & direct command-line arguments
  - Concurrent multi-threaded downloading with real-time tqdm progress bars
  - High-performance FFmpeg video merging with live percentage and speed progress bar
  - Auto-detection of Google Drive & GPU hardware acceleration (NVIDIA NVENC / CPU)
  - Search engine for Hongguo dramas
  - Auto device registration
"""

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional
from tqdm import tqdm

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
liushen_dir = BASE_DIR / "liushen"
if str(liushen_dir) not in sys.path:
    sys.path.insert(0, str(liushen_dir))

import importlib
parser_module = importlib.import_module("1")
app_module = importlib.import_module("app")
video_service = importlib.import_module("video_service")


# ───────────────────────── Helper Functions ─────────────────────────

def get_default_download_dir() -> Path:
    """Detect Google Drive mount in Colab or default local directory."""
    gdrive_mount = Path("/content/drive/MyDrive")
    if gdrive_mount.exists() and gdrive_mount.is_dir():
        target = gdrive_mount / "ShortDrama_Downloads"
        target.mkdir(parents=True, exist_ok=True)
        return target

    custom_env = os.getenv("DOWNLOAD_DIR", "").strip()
    if custom_env:
        target = Path(custom_env).resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    default_dir = BASE_DIR / "src"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def ensure_device_configured() -> Dict[str, str]:
    """Ensure device_id & install_id exist or auto-register a new virtual device."""
    config_file = BASE_DIR / "config.json"
    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    d_id = os.getenv("DUANJU_DEVICE_ID") or str(cfg.get("device_id", ""))
    i_id = os.getenv("DUANJU_INSTALL_ID") or str(cfg.get("install_id", ""))

    if not d_id or not i_id:
        print("\n⚙️ [Thiết bị] Chưa có cấu hình thiết bị. Đang tự động đăng ký thiết bị mới...")
        try:
            from liushen.device_register import device_register
            res = device_register()
            d_id = str(res.get("device_id", ""))
            i_id = str(res.get("install_id", ""))
            if d_id and i_id:
                cfg["device_id"] = d_id
                cfg["install_id"] = i_id
                cfg["platform"] = "android"
                config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                os.environ["DUANJU_DEVICE_ID"] = d_id
                os.environ["DUANJU_INSTALL_ID"] = i_id
                print(f"✅ Đăng ký thiết bị thành công: device_id={d_id[:4]}***{d_id[-3:]}\n")
        except Exception as exc:
            print(f"⚠️ Không thể tự động đăng ký: {exc}\n")
    else:
        os.environ["DUANJU_DEVICE_ID"] = d_id
        os.environ["DUANJU_INSTALL_ID"] = i_id

    return {"device_id": d_id, "install_id": i_id}


def extract_series_id(raw_input: str) -> str:
    """Extract clean series_id from URL or plain string."""
    raw = str(raw_input or "").strip()
    if "series_id=" in raw:
        m = re.search(r"series_id=([0-9A-Za-z_-]+)", raw)
        if m:
            return m.group(1)
    if "detail/" in raw:
        m = re.search(r"detail/([0-9A-Za-z_-]+)", raw)
        if m:
            return m.group(1)
    m = re.search(r"\d{10,}", raw)
    if m:
        return m.group(0)
    return raw


# ───────────────────────── Search Feature ─────────────────────────

def cli_search(keyword: str, page: int = 1) -> List[Dict[str, Any]]:
    """Search short dramas by keyword and print styled table."""
    keyword = keyword.strip()
    if not keyword:
        print("❌ Vui lòng nhập từ khóa tìm kiếm!")
        return []

    print(f"\n🔍 Đang tìm kiếm phim với từ khóa: \033[1;36m{keyword}\033[0m (Trang {page})...")
    try:
        items = app_module.search_short_drama(keyword, page=page)
    except Exception as exc:
        print(f"❌ Lỗi tìm kiếm: {exc}")
        return []

    if not items:
        print("ℹ️ Không tìm thấy bộ phim nào phù hợp.")
        return []

    print("\n" + "=" * 90)
    print(f"{'STT':<4} | {'TÊN PHIM':<35} | {'SỐ TẬP':<10} | {'SERIES ID':<20} | {'THỂ LOẠI'}")
    print("-" * 90)
    for idx, item in enumerate(items, 1):
        title = (item.get("title") or "Không tên")[:33]
        episodes = item.get("episodes") or "N/A"
        s_id = item.get("drama_id") or "N/A"
        cat = (item.get("category") or "")[:20]
        print(f"{idx:<4} | {title:<35} | {episodes:<10} | {s_id:<20} | {cat}")
    print("=" * 90 + "\n")
    return items


def parse_episode_selection(selection_str: str, total_count: int) -> List[int]:
    """
    Parse episode selection string into a sorted list of 1-based episode indices.
    Examples:
      - "" or "all": [1, 2, ..., total_count]
      - "1-20": [1, 2, ..., 20]
      - "21-40": [21, 22, ..., 40]
      - "10-": [10, 11, ..., total_count]
      - "-15": [1, 2, ..., 15]
      - "1,3,5,10-15": [1, 3, 5, 10, 11, 12, 13, 14, 15]
      - "10": [10]
    """
    s = str(selection_str or "").strip().lower()
    if not s or s in ["all", "tat ca", "het", "*"]:
        return list(range(1, total_count + 1))

    selected = set()
    parts = re.split(r"[,;|\s]+", s)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            left = left.strip()
            right = right.strip()
            start = int(left) if left.isdigit() else 1
            end = int(right) if right.isdigit() else total_count
            start = max(1, min(start, total_count))
            end = max(1, min(end, total_count))
            if start <= end:
                selected.update(range(start, end + 1))
            else:
                selected.update(range(end, start + 1))
        elif part.isdigit():
            val = int(part)
            if 1 <= val <= total_count:
                selected.add(val)

    if not selected:
        return list(range(1, total_count + 1))
    return sorted(list(selected))


# ───────────────────────── Show Detail Feature ─────────────────────────

def cli_show_detail(
    series_input: str,
    episode_range: str = "",
    as_json: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Display complete metadata and episode list for a series using the new 2.py App API detail.
    """
    ensure_device_configured()
    series_id = extract_series_id(series_input)
    if not series_id:
        print("❌ Mã Series ID không hợp lệ!")
        return None

    print(f"\n📋 Đang lấy thông tin chi tiết phim ID: \033[1;36m{series_id}\033[0m ...")
    try:
        detail = app_module.get_hongguo_detail(series_id)
    except Exception as exc:
        print(f"❌ Không thể lấy thông tin chi tiết phim: {exc}")
        return None

    if as_json:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return detail

    series_name = detail.get("series_name") or detail.get("title") or f"Phim_{series_id}"
    episodes = detail.get("episodes") or []
    total_eps = len(episodes)
    status_str = detail.get("status_vn") or detail.get("status") or "Trọn bộ"
    play_str = detail.get("play_cnt_str") or str(detail.get("play_cnt") or 0)
    followed = detail.get("followed_cnt") or 0
    categories = " / ".join(detail.get("tags") or detail.get("category") or []) or "N/A"
    intro = detail.get("series_intro") or detail.get("intro") or ""
    celebrities = detail.get("celebrities") or []

    print("\n" + "=" * 75)
    print(f"  🎬 《\033[1;32m{series_name}\033[0m》 ({status_str} · {total_eps} tập)")
    print("=" * 75)
    print(f"  • Series ID    : \033[1;36m{series_id}\033[0m")
    print(f"  • Thể loại     : {categories}")
    print(f"  • Lượt xem     : {play_str}")
    if followed:
        follow_str = f"{followed / 10000:.1f}万" if followed >= 10000 else str(followed)
        print(f"  • Theo dõi     : {follow_str}")
    if detail.get("cover") or detail.get("series_cover"):
        print(f"  • Ảnh bìa      : {detail.get('cover') or detail.get('series_cover')}")
    if celebrities:
        cast_strs = [f"{c['name']}({c['role']})" if c.get('role') else c['name'] for c in celebrities[:6]]
        print(f"  • Diễn viên    : {' · '.join(cast_strs)}")
    if intro:
        intro_snippet = intro.replace("\n", " ").strip()
        if len(intro_snippet) > 100:
            intro_snippet = intro_snippet[:100] + "..."
        print(f"  • Giới thiệu   : {intro_snippet}")
    print("-" * 75)

    # Filter episodes if range provided
    shown_episodes = episodes
    if episode_range:
        selected_set = set(parse_episode_selection(episode_range, total_eps))
        shown_episodes = [ep for ep in episodes if int(ep.get("episode_num", 0)) in selected_set]
        print(f"  Danh sách tập ({len(shown_episodes)}/{total_eps} tập được chọn):")
    else:
        print(f"  Danh sách tập ({len(episodes)} tập):")

    print("-" * 75)
    print(f"  {'STT':>4}  {'Tập':<8}  {'Thời lượng':<10}  {'Lượt thích':<10}  {'Mã VID':<22}  {'Nội dung / Tiêu đề'}")
    print(f"  {'-'*4:>4}  {'-'*8:<8}  {'-'*10:<10}  {'-'*10:<10}  {'-'*22:<22}  {'-'*20}")

    for idx, ep in enumerate(shown_episodes, 1):
        ep_num = ep.get("episode_num", idx)
        dur = ep.get("duration_str") or "00:00"
        dig = ep.get("digged_count", 0)
        dig_str = f"👍 {dig/10000:.1f}万" if dig >= 10000 else (f"👍 {dig}" if dig else "-")
        vid = ep.get("vid") or ""
        title = (ep.get("title") or "").replace("\n", " ").strip()
        if title.startswith(f"Tập {ep_num}") or title == f"第{ep_num}集":
            title = ""
        if len(title) > 30:
            title = title[:30] + "..."
        print(f"  [{idx:>3}]  Tập {ep_num:<4}  {dur:<10}  {dig_str:<10}  {vid:<22}  {title}")

    print("=" * 75 + "\n")
    return detail


# ───────────────────────── Download Series Feature ─────────────────────────

def cli_download_series(
    series_input: str,
    output_dir: Optional[Path] = None,
    auto_merge: bool = False,
    cut_end_seconds: float = 0.0,
    mirror: bool = False,
    clean_parts: bool = False,
    codec: str = "h264",
    gpu_pref: str = "nvenc",
    episode_selection: str = "",
    start_ep: Optional[int] = None,
    end_ep: Optional[int] = None,
    limit: Optional[int] = None,
    threads: int = 5,
    upload_to_storage: bool = True,
    generate_subtitles: bool = True,
    translate_subtitles: bool = True,
    translate_prompt: str = "ai_tong_hop_thong_minh",
    custom_endpoint: Optional[str] = None,
    custom_api_key: Optional[str] = None,
    translate_model: Optional[str] = None,
    dubbing: bool = True,
    tts_voice: str = "Ngọc Huyền",
    tts_batch_size: int = 64,
    video_speed: float = 0.9,
) -> Optional[Path]:
    """
    Download selected episodes of a series with concurrent multi-threading
    and real-time tqdm progress bars, with optional instant video merging.
    """
    ensure_device_configured()

    series_id = extract_series_id(series_input)
    if not series_id:
        print("❌ Mã Series ID không hợp lệ!")
        return None

    threads = max(1, min(int(threads or 5), 16))
    save_base = Path(output_dir).resolve() if output_dir else get_default_download_dir()
    save_base.mkdir(parents=True, exist_ok=True)

    print(f"\n📋 Đang lấy thông tin chi tiết phim ID: \033[1;36m{series_id}\033[0m ...")
    try:
        detail = app_module.get_hongguo_detail(series_id)
    except Exception as exc:
        print(f"❌ Không thể lấy thông tin phim: {exc}")
        return None

    series_name = detail.get("series_name") or detail.get("title") or f"Phim_{series_id}"
    all_episodes = detail.get("episodes") or []
    total_eps = len(all_episodes)

    if total_eps == 0:
        print("❌ Bộ phim không có tập nào hoặc không tìm thấy danh sách video ID.")
        return None

    # Filter selected episodes
    if start_ep is not None or end_ep is not None or limit is not None:
        st = max(1, start_ep or 1)
        en = min(total_eps, end_ep or total_eps)
        if limit is not None and limit > 0:
            en = min(en, st + limit - 1)
        selected_indices = set(range(st, en + 1))
    elif episode_selection:
        selected_indices = set(parse_episode_selection(episode_selection, total_eps))
    else:
        selected_indices = set(range(1, total_eps + 1))

    episodes = [ep for ep in all_episodes if int(ep.get("episode_num", 0)) in selected_indices]

    if not episodes:
        print(f"❌ Không có tập nào phù hợp với lựa chọn '{episode_selection}' (Tổng số tập phim: {total_eps}).")
        return None

    clean_name = re.sub(r'[\\/*?:"<>|]', '_', series_name).strip()
    series_folder = save_base / clean_name
    series_folder.mkdir(parents=True, exist_ok=True)

    min_ep = min(int(ep["episode_num"]) for ep in episodes)
    max_ep = max(int(ep["episode_num"]) for ep in episodes)

    status_vn = detail.get("status_vn") or detail.get("status") or "Trọn bộ"
    tags_str = " / ".join(detail.get("tags") or detail.get("category") or [])
    play_cnt_str = detail.get("play_cnt_str") or ""
    celebs = detail.get("celebrities") or []

    print("\n" + "=" * 70)
    print(f"🎬 TÊN PHIM        : \033[1;32m{series_name}\033[0m ({status_vn})")
    if tags_str:
        print(f"🏷️  THỂ LOẠI       : {tags_str}")
    if play_cnt_str:
        print(f"🔥 LƯỢT XEM        : {play_cnt_str}")
    if celebs:
        cast_strs = [f"{c['name']}({c['role']})" if c.get('role') else c['name'] for c in celebs[:5]]
        print(f"🎭 DIỄN VIÊN       : {' · '.join(cast_strs)}")
    if len(episodes) == total_eps:
        print(f"🔢 TỔNG SỐ TẬP TẢI : \033[1;33m{total_eps} tập (Toàn bộ)\033[0m")
    else:
        print(f"🔢 SỐ TẬP ĐƯỢC CHỌN: \033[1;33m{len(episodes)} tập (Từ tập {min_ep} -> tập {max_ep} / Tổng: {total_eps} tập)\033[0m")
    print(f"⚡ SỐ LUỒNG TẢI    : \033[1;36m{threads} luồng đồng thời\033[0m")
    print(f"📁 THƯ MỤC LƯU     : {series_folder}")
    if auto_merge:
        print(f"⚙️  TỰ ĐỘNG GHÉP    : Bật (Cắt đuôi: {cut_end_seconds}s | Lật hình: {'Có' if mirror else 'Không'})")
    print("=" * 70 + "\n")

    # Step 1: Pre-resolve video models in batches for maximum speed
    vids = [ep.get("vid") for ep in episodes if ep.get("vid")]
    if vids:
        print("⚡ Đang tối ưu hóa kết nối giải mã hàng loạt...")
        try:
            parser_module.resolve_batch_video_models(vids, batch_size=30)
        except Exception:
            pass

    # Step 2: Download episodes concurrently with ThreadPoolExecutor & tqdm progress bar
    downloaded_map: Dict[int, Path] = {}
    pbar_lock = threading.Lock()
    success_count = 0
    fail_count = 0

    pbar = tqdm(
        total=len(episodes),
        desc=f"📥 Đang tải ({threads} luồng)",
        unit="tập",
        bar_format="{l_bar}\033[1;32m{bar}\033[0m| {n_fmt}/{total_fmt} tập [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
        ncols=100,
    )

    def _dl_worker(ep: Dict[str, Any]) -> None:
        nonlocal success_count, fail_count
        vid = ep.get("vid")
        ep_num = int(ep.get("episode_num") or ep.get("index") or 1)
        filename = f"{clean_name}_Tap_{ep_num:03d}.mp4"
        file_path = series_folder / filename
        dur_tag = f" [{ep.get('duration_str')}]" if ep.get("duration_str") else ""

        # Skip if file already exists and is non-empty (>50KB)
        if file_path.exists() and file_path.stat().st_size > 50000:
            with pbar_lock:
                downloaded_map[ep_num] = file_path
                success_count += 1
                pbar.update(1)
                pbar.set_postfix_str(f"Tập {ep_num:03d} (sẵn có){dur_tag}")
            return

        try:
            parser_module.handle_video_request(
                vid,
                series_id=series_id,
                episode=ep_num,
                filename=filename,
                save_dir=str(series_folder),
            )
            if file_path.exists() and file_path.stat().st_size > 0:
                with pbar_lock:
                    downloaded_map[ep_num] = file_path
                    success_count += 1
                    pbar.update(1)
                    pbar.set_postfix_str(f"Tập {ep_num:03d} (xong){dur_tag}")
            else:
                with pbar_lock:
                    fail_count += 1
                    pbar.update(1)
                    pbar.write(f"⚠️ Tập {ep_num} ({vid}) tải về nhưng file rỗng.")
        except Exception as exc:
            with pbar_lock:
                fail_count += 1
                pbar.update(1)
                pbar.write(f"❌ Lỗi tải tập {ep_num} ({vid}): {exc}")

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_dl_worker, ep) for ep in episodes]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass

    pbar.close()

    # Step 3: Sort downloaded files in strict chronological order 1..N
    downloaded_files: List[Path] = [
        downloaded_map[int(ep.get("episode_num") or ep.get("index") or 0)]
        for ep in episodes
        if int(ep.get("episode_num") or ep.get("index") or 0) in downloaded_map
    ]

    print(f"\n✨ TẢI HOÀN TẤT: \033[1;32m{success_count}/{len(episodes)} tập thành công\033[0m (Thất bại: {fail_count})")
    print(f"📂 Thư mục chứa tập: {series_folder}\n")

    # Step 3: Optional Auto-merge
    if auto_merge and downloaded_files:
        if len(episodes) == total_eps:
            merge_name = f"{clean_name}_FULL.mp4"
        else:
            merge_name = f"{clean_name}_Tap_{min_ep:03d}-{max_ep:03d}_FULL.mp4"

        merged_file = cli_merge_folder(
            folder_path=series_folder,
            output_name=merge_name,
            output_dir=save_base,
            cut_end_seconds=cut_end_seconds,
            mirror=mirror,
            codec=codec,
            gpu_pref=gpu_pref,
            upload_to_storage=upload_to_storage,
            generate_subtitles=generate_subtitles,
            translate_subtitles=translate_subtitles,
            translate_prompt=translate_prompt,
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
            translate_model=translate_model,
            dubbing=dubbing,
            tts_voice=tts_voice,
            tts_batch_size=tts_batch_size,
            video_speed=video_speed,
        )

        if merged_file and clean_parts:
            print("🧹 Đang dọn dẹp các file tập lẻ...")
            for f in downloaded_files:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                series_folder.rmdir()
            except Exception:
                pass
            print("✅ Đã dọn dẹp các tập lẻ!")

        return merged_file

    return series_folder


# ───────────────────────── Merge Videos Feature ─────────────────────────

def cli_merge_folder(
    folder_path: Path,
    output_name: Optional[str] = None,
    output_dir: Optional[Path] = None,
    cut_end_seconds: float = 0.0,
    mirror: bool = False,
    codec: str = "h264",
    gpu_pref: str = "nvenc",
    color_filter: str = "none",
    audio_effect: str = "none",
    upload_to_storage: bool = True,
    generate_subtitles: bool = True,
    translate_subtitles: bool = True,
    translate_prompt: str = "ai_tong_hop_thong_minh",
    custom_endpoint: Optional[str] = None,
    custom_api_key: Optional[str] = None,
    translate_model: Optional[str] = None,
    dubbing: bool = True,
    tts_voice: str = "Ngọc Huyền",
    tts_batch_size: int = 64,
    video_speed: float = 0.9,
) -> Optional[Path]:
    """
    Merge all video files in a folder into one continuous video with live CLI progress bar,
    then automatically upload to storage.to, extract SRT subtitles with CapCut ASR,
    translate subtitles to Vietnamese with LLM, and dub video with VieNeu-TTS.
    """
    folder = Path(folder_path).resolve()
    if not folder.exists() or not folder.is_dir():
        print(f"❌ Thư mục không tồn tại: {folder}")
        return None

    # Scan and natural-sort video files
    video_items = video_service.list_directory_videos(folder)
    if not video_items:
        print(f"❌ Không tìm thấy video hợp lệ nào (.mp4, .ts, .mkv) trong thư mục: {folder}")
        return None

    valid_files = [item["path"] for item in video_items if item.get("is_valid", True)]
    if not valid_files:
        print("❌ Không có video nào đủ điều kiện để ghép.")
        return None

    out_folder = Path(output_dir).resolve() if output_dir else folder.parent
    out_folder.mkdir(parents=True, exist_ok=True)

    if not output_name:
        output_name = f"{folder.name}_FULL.mp4"
    if not output_name.lower().endswith(".mp4"):
        output_name += ".mp4"

    final_output = out_folder / output_name

    gpu_info = video_service.detect_available_gpu_encoders()
    primary_gpu = gpu_info.get("primary_gpu", "CPU Software (x264)")

    print("\n" + "=" * 65)
    print("🎬 TIẾN TRÌNH GHÉP VIDEO TOÀN DIỆN")
    print("=" * 65)
    print(f"📁 Số lượng tập cần ghép: \033[1;33m{len(valid_files)} tập\033[0m")
    print(f"⚡ Bộ mã hóa dự kiến   : \033[1;32m{primary_gpu}\033[0m")
    print(f"✂️ Cắt đuôi mỗi tập      : {cut_end_seconds}s")
    print(f"🪞 Lật hình (Mirror)     : {'Bật' if mirror else 'Tắt'}")
    print(f"☁️ Upload storage.to    : {'Bật' if upload_to_storage else 'Tắt'}")
    print(f"🎙️ CapCut ASR (Phụ đề)  : {'Bật' if generate_subtitles else 'Tắt'}")
    if generate_subtitles:
        if translate_subtitles:
            p_lbl = translate_prompt or "ai_tong_hop_thong_minh"
            m_lbl = translate_model or "gemini-lite"
            print(f"🇻🇳 Dịch sang Tiếng Việt : \033[1;32mBật\033[0m (Prompt: \033[1;33m{p_lbl}\033[0m | Model: \033[1;36m{m_lbl}\033[0m)")
            if dubbing:
                print(f"🦜 Lồng tiếng VieNeu-TTS: \033[1;32mBật\033[0m (Giọng: \033[1;33m{tts_voice}\033[0m | Batch: \033[1;36m{tts_batch_size}\033[0m segments)")
            else:
                print("🦜 Lồng tiếng VieNeu-TTS: Tắt")
        else:
            print(f"🇻🇳 Dịch sang Tiếng Việt : Tắt")
    print(f"💾 File đầu ra           : \033[1;36m{final_output}\033[0m")
    print("=" * 65 + "\n")

    # Options for merge
    options = {
        "cut_end_seconds": float(cut_end_seconds or 0.0),
        "mirror": bool(mirror),
        "quality": "original",
        "resolution": "original",
        "fps": "original",
        "bitrate": "auto",
        "codec": codec,
        "gpu": gpu_pref,
        "color_filter": color_filter,
        "audio_effect": audio_effect,
        "format": "mp4",
        "output_dir": str(out_folder),
        "output_name": output_name,
        "upload_to_storage": upload_to_storage,
        "generate_subtitles": generate_subtitles,
        "translate_subtitles": translate_subtitles,
        "translate_prompt": translate_prompt,
        "custom_endpoint": custom_endpoint,
        "custom_api_key": custom_api_key,
        "translate_model": translate_model,
        "dubbing": bool(dubbing),
        "tts_voice": str(tts_voice or "Ngọc Huyền"),
        "tts_batch_size": int(tts_batch_size or 64),
        "video_speed": float(video_speed or 0.9),
    }

    merge_pbar = tqdm(
        total=100,
        desc="🎬 Đang xử lý video",
        unit="%",
        bar_format="{l_bar}\033[1;36m{bar}\033[0m| {n_fmt}/100% [{elapsed}<{remaining}] {postfix}",
        ncols=100,
    )

    seen_uploaded_urls = set()

    def _progress_cb(pct: float, speed: str, msg: str):
        target = min(100, int(round(pct)))
        if target > merge_pbar.n:
            merge_pbar.update(target - merge_pbar.n)
        postfix_parts = []
        if speed and speed != "-":
            postfix_parts.append(f"Tốc độ: {speed}")
        if msg:
            short_msg = msg if len(msg) < 40 else msg[:37] + "..."
            postfix_parts.append(short_msg)
        merge_pbar.set_postfix_str(" | ".join(postfix_parts))

        # Hiển thị ngay lập tức các link storage.to ngay khi vừa upload xong, tránh mất link nếu session bị ngắt
        for k, label, color in [
            ("video_url", "🎬 [Storage.to] Link Video FULL", "\033[1;35m"),
            ("srt_url", "📝 [Storage.to] Link Subtitle Gốc", "\033[1;35m"),
            ("translated_srt_url", "🇻🇳 [Storage.to] Link Subtitle Dịch", "\033[1;32m"),
            ("dubbed_audio_url", "🎙️ [Storage.to] Link Audio Dubbing", "\033[1;36m"),
            ("dubbed_video_url", "🎬 [Storage.to] Link Video Dubbing", "\033[1;32m"),
        ]:
            val = options.get(k)
            if val and val not in seen_uploaded_urls:
                seen_uploaded_urls.add(val)
                tqdm.write(f"\n⚡ {label}: {color}{val}\033[0m\n")

    try:
        merged_path = video_service.merge_videos_sync(valid_files, options, progress_callback=_progress_cb)
        merge_pbar.n = 100
        merge_pbar.refresh()
        merge_pbar.close()

        file_size_str = video_service.format_size(merged_path.stat().st_size)
        video_url = options.get("video_url")
        srt_url = options.get("srt_url")
        srt_path = options.get("srt_path")
        translated_srt_url = options.get("translated_srt_url")
        translated_srt_path = options.get("translated_srt_path")
        dubbed_audio_url = options.get("dubbed_audio_url")
        dubbed_audio_path = options.get("dubbed_audio_path")
        dubbed_video_url = options.get("dubbed_video_url")
        dubbed_video_path = options.get("dubbed_video_path")

        print("\n" + "=" * 70)
        print("🎉 \033[1;32mGHÉP VIDEO & XUẤT BẢN THÀNH CÔNG RỰC RỠ!\033[0m")
        print("=" * 70)
        print(f"📁 Video cục bộ          : \033[1;36m{merged_path}\033[0m")
        print(f"📦 Dung lượng            : {file_size_str}")
        if video_url:
            print(f"🌐 Link Video storage.to : \033[1;35m{video_url}\033[0m")
        if srt_path:
            print(f"📝 Phụ đề Gốc cục bộ     : \033[1;36m{srt_path}\033[0m")
        if srt_url:
            print(f"🌐 Link Sub Gốc storage.to : \033[1;35m{srt_url}\033[0m")
        if translated_srt_path:
            print(f"🇻🇳 Phụ đề Dịch Tiếng Việt: \033[1;32m{translated_srt_path}\033[0m")
        if translated_srt_url:
            print(f"🌐 Link Sub Dịch storage.to: \033[1;35m{translated_srt_url}\033[0m")
        if dubbed_audio_path:
            print(f"🎙️ Audio Dubbing cục bộ   : \033[1;36m{dubbed_audio_path}\033[0m")
        if dubbed_audio_url:
            print(f"🌐 Link Audio Dubbing    : \033[1;36m{dubbed_audio_url}\033[0m")
        if dubbed_video_path:
            print(f"🎬 Video Dubbed cục bộ    : \033[1;32m{dubbed_video_path}\033[0m")
        if dubbed_video_url:
            print(f"🌐 Link Video Dubbing    : \033[1;32m{dubbed_video_url}\033[0m")
        print("=" * 70 + "\n")
        return merged_path

    except Exception as exc:
        merge_pbar.close()
        print(f"\n❌ \033[1;31mLỗi trong quá trình xử lý:\033[0m {exc}\n")

        # In ngay các link đã upload thành công trước khi bị lỗi
        v_url = options.get("video_url")
        s_url = options.get("srt_url")
        t_url = options.get("translated_srt_url")
        da_url = options.get("dubbed_audio_url")
        dv_url = options.get("dubbed_video_url")
        if v_url or s_url or t_url or da_url or dv_url:
            print("=" * 65)
            print("⚠️ \033[1;33mCÁC LIÊN KẾT ĐÃ KỊP TẢI LÊN THÀNH CÔNG TRƯỚC ĐÓ:\033[0m")
            if v_url:
                print(f"🎬 Link Video storage.to   : \033[1;35m{v_url}\033[0m")
            if s_url:
                print(f"📝 Link Sub Gốc storage.to   : \033[1;35m{s_url}\033[0m")
            if t_url:
                print(f"🇻🇳 Link Sub Dịch storage.to : \033[1;32m{t_url}\033[0m")
            if da_url:
                print(f"🎙️ Link Audio Dubbing      : \033[1;36m{da_url}\033[0m")
            if dv_url:
                print(f"🎬 Link Video Dubbing      : \033[1;32m{dv_url}\033[0m")
            print("=" * 65 + "\n")
        return None


# ───────────────────────── Interactive Menu ─────────────────────────

def cli_check_gpu():
    """Inspect and diagnose GPU hardware status and FFmpeg NVENC acceleration."""
    print("\n" + "=" * 70)
    print("       🔍 KIỂM TRA PHẦN CỨNG GPU & BỘ MÃ HÓA NVIDIA NVENC       ")
    print("=" * 70)

    smi_present = bool(shutil.which("nvidia-smi"))
    gpu_name = video_service.get_nvidia_gpu_model_name()
    print(f"1. Lệnh nvidia-smi    : {'✅ Khả dụng' if smi_present else '❌ Không tìm thấy'}")
    print(f"2. Tên card GPU       : \033[1;32m{gpu_name or 'Không nhận diện được'}\033[0m")

    ffmpeg_bin = video_service.get_ffmpeg_binary()
    print(f"3. Đường dẫn FFmpeg   : {ffmpeg_bin}")

    video_service._DETECTED_GPU_ENCODERS = None  # Force re-detection
    gpu_info = video_service.detect_available_gpu_encoders()
    print(f"4. Nhận diện GPU      : {'✅ CÓ GPU' if gpu_info.get('has_gpu') else '⚠️ CPU ONLY'}")
    print(f"5. Bộ mã hóa chính    : \033[1;36m{gpu_info.get('primary_gpu')}\033[0m")
    print(f"6. Hỗ trợ h264_nvenc  : {'✅ Có' if gpu_info.get('has_nvenc') else '❌ Không'}")

    test_cmd = [
        ffmpeg_bin, "-y", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
        "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-f", "null", "-"
    ]
    try:
        t_res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if t_res.returncode == 0:
            print("7. Thử nghiệm mã hóa  : \033[1;32m✅ THÀNH CÔNG (NVIDIA NVENC hoạt động hoàn hảo!)\033[0m")
        else:
            print(f"7. Thử nghiệm mã hóa  : \033[1;31m❌ THẤT BẠI (Mã lỗi {t_res.returncode})\033[0m")
            print(f"   Chi tiết lỗi FFmpeg: {t_res.stderr.strip()[:300]}")
    except Exception as exc:
        print(f"7. Thử nghiệm mã hóa  : ❌ Lỗi ngoại lệ: {exc}")

    print("=" * 70 + "\n")


# ───────────────────────── Interactive Menu ─────────────────────────

def run_interactive_menu():
    """Interactive CLI menu with prompt-driven workflow."""
    ensure_device_configured()
    save_dir = get_default_download_dir()
    gpu_info = video_service.detect_available_gpu_encoders()
    gpu_name = gpu_info.get("primary_gpu", "CPU Software (x264)")
    gpu_badge = f"\033[1;32m{gpu_name} (Đang bật - Tốc độ cao)\033[0m" if gpu_info.get("has_gpu") else f"\033[1;33m{gpu_name} (Chưa bật GPU Colab)\033[0m"

    while True:
        print("\n" + "=" * 70)
        print("       🎬 TRÌNH TẢI & GHÉP PHIM NGẮN (SHORT DRAMA CLI TOOL)       ")
        print("=" * 70)
        print(f" ⚡ Tăng tốc phần cứng  : {gpu_badge}")
        print(f" 💾 Thư mục lưu hiện tại: \033[1;36m{save_dir}\033[0m")
        print("=" * 70)
        print(" [1] 🔍 Tìm kiếm phim theo tên / thể loại")
        print(" [2] ℹ️  Xem chi tiết thông tin phim & danh sách tập")
        print(" [3] ⚡ Tải phim theo Series ID (kèm tùy chọn tự động ghép)")
        print(" [4] 🎬 Ghép các file video có sẵn trong một thư mục")
        print(" [5] 🔍 Kiểm tra chi tiết GPU & Thử nghiệm NVENC")
        print(" [6] 📱 Kiểm tra & Đăng ký lại thiết bị (Device ID)")
        print(" [7] 📁 Đổi thư mục lưu trữ video")
        print(" [0] 🚪 Thoát")
        print("=" * 70)

        try:
            choice = input("👉 Nhập lựa chọn của bạn [0-7]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Tạm biệt!")
            break

        if choice == "1":
            kw = input("\n👉 Nhập từ khóa hoặc tên phim: ").strip()
            if kw:
                results = cli_search(kw)
                if results:
                    sel = input("👉 Nhập STT để tải, hoặc 'd <STT>' để xem chi tiết (Enter bỏ qua): ").strip()
                    if sel.lower().startswith(("d ", "i ", "xem ")):
                        parts = sel.split()
                        if len(parts) > 1 and parts[1].isdigit():
                            idx = int(parts[1])
                            if 1 <= idx <= len(results):
                                chosen = results[idx - 1]
                                s_id = chosen.get("drama_id")
                                cli_show_detail(s_id)
                    elif sel.isdigit() and 1 <= int(sel) <= len(results):
                        chosen = results[int(sel) - 1]
                        s_id = chosen.get("drama_id")
                        ep_sel = input("👉 Chọn tập cần tải (Enter để tải hết, hoặc nhập ví dụ: 1-20, 21-40, 1,3,5): ").strip()
                        threads_str = input("👉 Số luồng tải đồng thời (mặc định 5, 1-16) [5]: ").strip()
                        threads = int(threads_str) if threads_str.isdigit() else 5
                        merge_ans = input("👉 Tự động ghép thành 1 video FULL sau khi tải xong? (y/N) [y]: ").strip().lower()
                        auto_merge = merge_ans in ["", "y", "yes", "1"]
                        cut_sec = 0.0
                        if auto_merge:
                            cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây nhạc kết, ví dụ 0): ").strip()
                            cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                        cli_download_series(
                            s_id,
                            output_dir=save_dir,
                            auto_merge=auto_merge,
                            cut_end_seconds=cut_sec,
                            episode_selection=ep_sel,
                            threads=threads,
                        )

        elif choice == "2":
            s_id = input("\n👉 Nhập Series ID hoặc Link phim (ví dụ 7673742481694919704): ").strip()
            if s_id:
                ep_range = input("👉 Nhập khoảng tập muốn xem (Enter để xem toàn bộ, ví dụ 1-20): ").strip()
                detail = cli_show_detail(s_id, episode_range=ep_range)
                if detail:
                    dl_ans = input("👉 Bạn có muốn tải phim này ngay bây giờ không? (y/N) [y]: ").strip().lower()
                    if dl_ans in ["", "y", "yes", "1"]:
                        ep_sel = input("👉 Chọn tập cần tải (Enter để tải hết, hoặc nhập ví dụ: 1-20, 21-40, 1,3,5): ").strip()
                        threads_str = input("👉 Số luồng tải đồng thời (mặc định 5, 1-16) [5]: ").strip()
                        threads = int(threads_str) if threads_str.isdigit() else 5
                        merge_ans = input("👉 Tự động ghép thành 1 video FULL sau khi tải xong? (y/N) [y]: ").strip().lower()
                        auto_merge = merge_ans in ["", "y", "yes", "1"]
                        cut_sec = 0.0
                        mirror = False
                        if auto_merge:
                            cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây nhạc kết, ví dụ 0): ").strip()
                            cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                            m_ans = input("👉 Lật hình (Mirror video)? (y/N) [n]: ").strip().lower()
                            mirror = m_ans in ["y", "yes", "1"]
                        cli_download_series(
                            s_id,
                            output_dir=save_dir,
                            auto_merge=auto_merge,
                            cut_end_seconds=cut_sec,
                            mirror=mirror,
                            episode_selection=ep_sel,
                            threads=threads,
                        )

        elif choice == "3":
            s_id = input("\n👉 Nhập Series ID hoặc Link phim (ví dụ 7369168922572164134): ").strip()
            if s_id:
                ep_sel = input("👉 Chọn tập cần tải (Enter để tải hết, hoặc nhập ví dụ: 1-20, 21-40, 1,3,5): ").strip()
                threads_str = input("👉 Số luồng tải đồng thời (mặc định 5, 1-16) [5]: ").strip()
                threads = int(threads_str) if threads_str.isdigit() else 5
                merge_ans = input("👉 Tự động ghép thành 1 video FULL sau khi tải xong? (y/N) [y]: ").strip().lower()
                auto_merge = merge_ans in ["", "y", "yes", "1"]
                cut_sec = 0.0
                mirror = False
                if auto_merge:
                    cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây nhạc kết, ví dụ 0): ").strip()
                    cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                    m_ans = input("👉 Lật hình (Mirror video)? (y/N) [n]: ").strip().lower()
                    mirror = m_ans in ["y", "yes", "1"]
                cli_download_series(
                    s_id,
                    output_dir=save_dir,
                    auto_merge=auto_merge,
                    cut_end_seconds=cut_sec,
                    mirror=mirror,
                    episode_selection=ep_sel,
                    threads=threads,
                )

        elif choice == "4":
            f_path = input("\n👉 Nhập đường dẫn thư mục chứa các tập video (Enter để dùng thư mục con trong save_dir): ").strip()
            if not f_path:
                print(f"Các thư mục có sẵn trong {save_dir}:")
                subdirs = [d for d in save_dir.iterdir() if d.is_dir()]
                for i, d in enumerate(subdirs, 1):
                    print(f"  [{i}] {d.name}")
                if subdirs:
                    s_idx = input("👉 Chọn số thứ tự thư mục: ").strip()
                    if s_idx.isdigit() and 1 <= int(s_idx) <= len(subdirs):
                        f_path = str(subdirs[int(s_idx) - 1])
            if f_path:
                cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây, ví dụ 0): ").strip()
                cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                m_ans = input("👉 Lật hình (Mirror video)? (y/N) [n]: ").strip().lower()
                mirror = m_ans in ["y", "yes", "1"]
                cli_merge_folder(Path(f_path), cut_end_seconds=cut_sec, mirror=mirror)

        elif choice == "5":
            cli_check_gpu()

        elif choice == "6":
            print("\n🔄 Đang thực hiện đăng ký thiết bị mới...")
            try:
                from liushen.device_register import device_register
                res = device_register()
                print(f"✅ Đăng ký thành công: device_id={res.get('device_id')}, install_id={res.get('install_id')}")
            except Exception as exc:
                print(f"❌ Lỗi đăng ký: {exc}")

        elif choice == "7":
            new_p = input(f"\n👉 Nhập đường dẫn thư mục mới (Hiện tại: {save_dir}): ").strip()
            if new_p:
                p_obj = Path(new_p).resolve()
                p_obj.mkdir(parents=True, exist_ok=True)
                save_dir = p_obj
                os.environ["DOWNLOAD_DIR"] = str(save_dir)
                print(f"✅ Đã đổi thư mục lưu sang: {save_dir}")

        elif choice in ["0", "q", "exit"]:
            print("\n👋 Cảm ơn bạn đã sử dụng Short Drama CLI! Chúc bạn xem phim vui vẻ!")
            break
        else:
            print("⚠️ Lựa chọn không hợp lệ, vui lòng thử lại.")


# ───────────────────────── Command-Line Parser ─────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🎬 Short Drama CLI - Download and Merge Short Dramas with Progress Bars",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Lệnh chức năng")

    # Command: detail / info
    p_dt = subparsers.add_parser("detail", aliases=["info"], help="Xem thông tin chi tiết phim và danh sách tập")
    p_dt.add_argument("series_id", type=str, help="Series ID hoặc Link phim (ví dụ: 7673742481694919704)")
    p_dt.add_argument("-e", "--episodes", "--range", type=str, default="", help="Khoảng tập cần xem (ví dụ: 1-20, 21-40)")
    p_dt.add_argument("--json", action="store_true", help="Xuất dữ liệu chi tiết định dạng JSON")

    # Command: download
    p_dl = subparsers.add_parser("download", help="Tải các tập của một bộ phim")
    p_dl.add_argument("series_id", type=str, help="Series ID hoặc Link phim (ví dụ: 7369168922572164134)")
    p_dl.add_argument("-e", "--episodes", "--range", type=str, default="", help="Khoảng tập cần tải (ví dụ: 1-20, 21-40, 1,3,5-10, hoặc để trống tải hết)")
    p_dl.add_argument("-w", "--workers", "--threads", type=int, default=5, help="Số luồng tải đồng thời (mặc định: 5, tối đa: 16)")
    p_dl.add_argument("--start", type=int, default=None, help="Tập bắt đầu tải (ví dụ: 1)")
    p_dl.add_argument("--end", type=int, default=None, help="Tập kết thúc tải (ví dụ: 20)")
    p_dl.add_argument("--limit", type=int, default=None, help="Số lượng tập tối đa cần tải (ví dụ: 10)")
    p_dl.add_argument("--merge", action="store_true", help="Tự động ghép tất cả các tập đã chọn thành 1 video FULL")
    p_dl.add_argument("--cut-end", type=float, default=0.0, help="Số giây cắt bỏ ở cuối mỗi tập (mặc định: 0)")
    p_dl.add_argument("--mirror", action="store_true", help="Lật hình ngang (Mirror video)")
    p_dl.add_argument("--clean-parts", action="store_true", help="Xóa các tập lẻ sau khi ghép thành công")
    p_dl.add_argument("--save-dir", type=str, default="", help="Thư mục lưu video (mặc định: Google Drive hoặc ./src)")
    p_dl.add_argument("--codec", type=str, default="h264", choices=["h264", "hevc"], help="Định dạng codec video")
    p_dl.add_argument("--gpu", type=str, default="nvenc", choices=["nvenc", "cpu", "qsv", "amf"], help="Bộ mã hóa phần cứng")
    p_dl.add_argument("--no-upload", action="store_true", help="Không tự động tải lên storage.to sau khi ghép")
    p_dl.add_argument("--no-asr", action="store_true", help="Không tự động trích xuất phụ đề CapCut ASR")
    p_dl.add_argument("--no-translate", action="store_true", help="Không tự động dịch phụ đề sang tiếng Việt khi ghép")
    p_dl.add_argument("--prompt", "--translate-prompt", dest="translate_prompt", type=str, default="ai_tong_hop_thong_minh", help="Chọn prompt dịch thuật (preset key trong prompts.json hoặc custom prompt)")
    p_dl.add_argument("--custom-endpoint", "--endpoint", dest="custom_endpoint", type=str, default="", help="Custom LLM API endpoint (OpenAI-compatible)")
    p_dl.add_argument("--apikey", "--api-key", dest="custom_api_key", type=str, default="", help="Custom API key")
    p_dl.add_argument("--model", "--translate-model", dest="translate_model", type=str, default="", help="Model dịch thuật (vd: gemini-lite, deepseek-chat)")
    p_dl.add_argument("--no-dubbing", action="store_true", help="Không tự động lồng tiếng video với VieNeu-TTS")
    p_dl.add_argument("--voice", "--tts-voice", dest="tts_voice", type=str, default="Ngọc Huyền", help="Giọng đọc VieNeu-TTS (mặc định: Ngọc Huyền)")
    p_dl.add_argument("--batch-size", "--tts-batch-size", dest="tts_batch_size", type=int, default=64, help="Kích thước batch cho VieNeu-TTS infer_batch (mặc định: 64)")
    p_dl.add_argument("--speed", "--video-speed", dest="video_speed", type=float, default=0.9, help="Tốc độ phát của video sau khi ghép (mặc định: 0.9x)")

    # Command: search
    p_sc = subparsers.add_parser("search", help="Tìm kiếm phim theo từ khóa")
    p_sc.add_argument("keyword", type=str, help="Tên phim hoặc từ khóa tìm kiếm")
    p_sc.add_argument("--page", type=int, default=1, help="Số trang kết quả (mặc định: 1)")

    # Command: merge
    p_mg = subparsers.add_parser("merge", help="Ghép các video có sẵn trong thư mục")
    p_mg.add_argument("folder", type=str, help="Đường dẫn thư mục chứa các tập video")
    p_mg.add_argument("--output-name", type=str, default="", help="Tên file video sau khi ghép (ví dụ: movie_full.mp4)")
    p_mg.add_argument("--cut-end", type=float, default=0.0, help="Số giây cắt bỏ ở cuối mỗi tập (mặc định: 0)")
    p_mg.add_argument("--mirror", action="store_true", help="Lật hình ngang (Mirror video)")
    p_mg.add_argument("--output-dir", type=str, default="", help="Thư mục xuất file sau khi ghép")
    p_mg.add_argument("--codec", type=str, default="h264", choices=["h264", "hevc"], help="Định dạng codec video")
    p_mg.add_argument("--gpu", type=str, default="nvenc", choices=["nvenc", "cpu", "qsv", "amf"], help="Bộ mã hóa phần cứng")
    p_mg.add_argument("--no-upload", action="store_true", help="Không tự động tải lên storage.to sau khi ghép")
    p_mg.add_argument("--no-asr", action="store_true", help="Không tự động trích xuất phụ đề CapCut ASR")
    p_mg.add_argument("--no-translate", action="store_true", help="Không tự động dịch phụ đề sang tiếng Việt")
    p_mg.add_argument("--prompt", "--translate-prompt", dest="translate_prompt", type=str, default="ai_tong_hop_thong_minh", help="Chọn prompt dịch thuật (preset key trong prompts.json hoặc custom prompt)")
    p_mg.add_argument("--custom-endpoint", "--endpoint", dest="custom_endpoint", type=str, default="", help="Custom LLM API endpoint (OpenAI-compatible)")
    p_mg.add_argument("--apikey", "--api-key", dest="custom_api_key", type=str, default="", help="Custom API key")
    p_mg.add_argument("--model", "--translate-model", dest="translate_model", type=str, default="", help="Model dịch thuật (vd: gemini-lite, deepseek-chat)")
    p_mg.add_argument("--no-dubbing", action="store_true", help="Không tự động lồng tiếng video với VieNeu-TTS")
    p_mg.add_argument("--voice", "--tts-voice", dest="tts_voice", type=str, default="Ngọc Huyền", help="Giọng đọc VieNeu-TTS (mặc định: Ngọc Huyền)")
    p_mg.add_argument("--batch-size", "--tts-batch-size", dest="tts_batch_size", type=int, default=64, help="Kích thước batch cho VieNeu-TTS infer_batch (mặc định: 64)")
    p_mg.add_argument("--speed", "--video-speed", dest="video_speed", type=float, default=0.9, help="Tốc độ phát của video sau khi ghép (mặc định: 0.9x)")

    # Command: check-gpu
    subparsers.add_parser("check-gpu", help="Kiểm tra chi tiết GPU & bộ mã hóa NVIDIA NVENC")

    # Command: register
    subparsers.add_parser("register", help="Đăng ký thiết bị mới và lưu cấu hình")

    args = parser.parse_args()

    if not args.command:
        # If no arguments provided, launch the interactive menu
        run_interactive_menu()
        return

    if args.command in ["detail", "info"]:
        cli_show_detail(args.series_id, episode_range=args.episodes, as_json=args.json)

    elif args.command == "search":
        cli_search(args.keyword, page=args.page)

    elif args.command == "check-gpu":
        cli_check_gpu()

    elif args.command == "download":
        out_d = Path(args.save_dir).resolve() if args.save_dir else None
        cli_download_series(
            series_input=args.series_id,
            output_dir=out_d,
            auto_merge=args.merge,
            cut_end_seconds=args.cut_end,
            mirror=args.mirror,
            clean_parts=args.clean_parts,
            codec=args.codec,
            gpu_pref=args.gpu,
            episode_selection=args.episodes,
            start_ep=args.start,
            end_ep=args.end,
            limit=args.limit,
            threads=args.workers,
            upload_to_storage=not args.no_upload,
            generate_subtitles=not args.no_asr,
            translate_subtitles=not args.no_translate,
            translate_prompt=args.translate_prompt,
            custom_endpoint=args.custom_endpoint,
            custom_api_key=args.custom_api_key,
            translate_model=args.translate_model,
            dubbing=not args.no_dubbing,
            tts_voice=args.tts_voice,
            tts_batch_size=args.tts_batch_size,
            video_speed=getattr(args, "video_speed", 0.9),
        )

    elif args.command == "merge":
        out_d = Path(args.output_dir).resolve() if args.output_dir else None
        cli_merge_folder(
            folder_path=Path(args.folder),
            output_name=args.output_name,
            output_dir=out_d,
            cut_end_seconds=args.cut_end,
            mirror=args.mirror,
            codec=args.codec,
            gpu_pref=args.gpu,
            upload_to_storage=not args.no_upload,
            generate_subtitles=not args.no_asr,
            translate_subtitles=not args.no_translate,
            translate_prompt=args.translate_prompt,
            custom_endpoint=args.custom_endpoint,
            custom_api_key=args.custom_api_key,
            translate_model=args.translate_model,
            dubbing=not args.no_dubbing,
            tts_voice=args.tts_voice,
            tts_batch_size=args.tts_batch_size,
            video_speed=getattr(args, "video_speed", 0.9),
        )

    elif args.command == "register":
        print("\n🔄 Đang thực hiện đăng ký thiết bị mới...")
        try:
            from liushen.device_register import device_register
            res = device_register()
            print(f"✅ Đăng ký thành công: device_id={res.get('device_id')}, install_id={res.get('install_id')}\n")
        except Exception as exc:
            print(f"❌ Lỗi đăng ký: {exc}\n")


if __name__ == "__main__":
    main()
