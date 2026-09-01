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
import json
import os
import re
import sys
import time
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
) -> Optional[Path]:
    """
    Download all episodes of a series with real-time tqdm progress bars,
    with optional instant video merging.
    """
    ensure_device_configured()

    series_id = extract_series_id(series_input)
    if not series_id:
        print("❌ Mã Series ID không hợp lệ!")
        return None

    save_base = Path(output_dir).resolve() if output_dir else get_default_download_dir()
    save_base.mkdir(parents=True, exist_ok=True)

    print(f"\n📋 Đang lấy thông tin chi tiết phim ID: \033[1;36m{series_id}\033[0m ...")
    try:
        detail = app_module.get_hongguo_detail(series_id)
    except Exception as exc:
        print(f"❌ Không thể lấy thông tin phim: {exc}")
        return None

    series_name = detail.get("series_name") or f"Phim_{series_id}"
    episodes = detail.get("episodes") or []
    total_eps = len(episodes)

    if total_eps == 0:
        print("❌ Bộ phim không có tập nào hoặc không tìm thấy danh sách video ID.")
        return None

    clean_name = re.sub(r'[\\/*?:"<>|]', '_', series_name).strip()
    series_folder = save_base / clean_name
    series_folder.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 65)
    print(f"🎬 TÊN PHIM     : \033[1;32m{series_name}\033[0m")
    print(f"🔢 TỔNG SỐ TẬP   : \033[1;33m{total_eps} tập\033[0m")
    print(f"📁 THƯ MỤC LƯU  : {series_folder}")
    if auto_merge:
        print(f"⚙️ TỰ ĐỘNG GHÉP : Bật (Cắt đuôi: {cut_end_seconds}s | Lật hình: {'Có' if mirror else 'Không'})")
    print("=" * 65 + "\n")

    # Step 1: Pre-resolve video models in batches for maximum speed
    vids = [ep.get("vid") for ep in episodes if ep.get("vid")]
    if vids:
        print("⚡ Đang tối ưu hóa kết nối giải mã hàng loạt...")
        try:
            parser_module.resolve_batch_video_models(vids, batch_size=30)
        except Exception:
            pass

    # Step 2: Download each episode with a progress bar
    downloaded_files: List[Path] = []
    success_count = 0
    fail_count = 0

    pbar = tqdm(
        episodes,
        desc="📥 Đang tải các tập",
        unit="tập",
        bar_format="{l_bar}\033[1;32m{bar}\033[0m| {n_fmt}/{total_fmt} tập [{elapsed}<{remaining}, {rate_fmt}] {postfix}",
        ncols=100,
    )

    for ep in pbar:
        vid = ep.get("vid")
        ep_num = ep.get("episode_num", 1)
        filename = f"{clean_name}_Tap_{ep_num:03d}.mp4"
        file_path = series_folder / filename

        pbar.set_postfix_str(f"Tập {ep_num:03d} (ID: {vid})")

        # Skip if file already exists and is non-empty
        if file_path.exists() and file_path.stat().st_size > 50000:
            downloaded_files.append(file_path)
            success_count += 1
            continue

        try:
            parser_module.handle_video_request(
                vid,
                series_id=series_id,
                episode=ep_num,
                filename=filename,
                save_dir=str(series_folder),
            )
            if file_path.exists() and file_path.stat().st_size > 0:
                downloaded_files.append(file_path)
                success_count += 1
            else:
                fail_count += 1
                pbar.write(f"⚠️ Tập {ep_num} ({vid}) tải về nhưng file rỗng.")
        except Exception as exc:
            fail_count += 1
            pbar.write(f"❌ Lỗi tải tập {ep_num} ({vid}): {exc}")

    pbar.close()

    print(f"\n✨ TẢI HOÀN TẤT: \033[1;32m{success_count}/{total_eps} tập thành công\033[0m (Thất bại: {fail_count})")
    print(f"📂 Thư mục chứa tập: {series_folder}\n")

    # Step 3: Optional Auto-merge
    if auto_merge and downloaded_files:
        merged_file = cli_merge_folder(
            folder_path=series_folder,
            output_name=f"{clean_name}_FULL.mp4",
            output_dir=save_base,
            cut_end_seconds=cut_end_seconds,
            mirror=mirror,
            codec=codec,
            gpu_pref=gpu_pref,
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
) -> Optional[Path]:
    """
    Merge all video files in a folder into one continuous video with live CLI progress bar.
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
    }

    merge_pbar = tqdm(
        total=100,
        desc="🎬 Đang ghép video",
        unit="%",
        bar_format="{l_bar}\033[1;36m{bar}\033[0m| {n_fmt}/100% [{elapsed}<{remaining}] {postfix}",
        ncols=100,
    )

    def _progress_cb(pct: float, speed: str, msg: str):
        target = min(100, int(round(pct)))
        if target > merge_pbar.n:
            merge_pbar.update(target - merge_pbar.n)
        postfix_parts = []
        if speed and speed != "-":
            postfix_parts.append(f"Tốc độ: {speed}")
        if msg:
            short_msg = msg if len(msg) < 35 else msg[:32] + "..."
            postfix_parts.append(short_msg)
        merge_pbar.set_postfix_str(" | ".join(postfix_parts))

    try:
        merged_path = video_service.merge_videos_sync(valid_files, options, progress_callback=_progress_cb)
        merge_pbar.n = 100
        merge_pbar.refresh()
        merge_pbar.close()

        file_size_str = video_service.format_size(merged_path.stat().st_size)
        print("\n" + "=" * 65)
        print("🎉 \033[1;32mGHÉP VIDEO THÀNH CÔNG RỰC RỠ!\033[0m")
        print(f"📁 Tệp hoàn chỉnh: \033[1;36m{merged_path}\033[0m")
        print(f"📦 Dung lượng    : {file_size_str}")
        print("=" * 65 + "\n")
        return merged_path

    except Exception as exc:
        merge_pbar.close()
        print(f"\n❌ \033[1;31mLỗi trong quá trình ghép video:\033[0m {exc}\n")
        return None


# ───────────────────────── Interactive Menu ─────────────────────────

def run_interactive_menu():
    """Interactive CLI menu with prompt-driven workflow."""
    ensure_device_configured()
    save_dir = get_default_download_dir()

    while True:
        print("\n" + "=" * 70)
        print("       🎬 TRÌNH TẢI & GHÉP PHIM NGẮN (SHORT DRAMA CLI TOOL)       ")
        print("=" * 70)
        print(f" 💾 Thư mục lưu hiện tại: \033[1;36m{save_dir}\033[0m")
        print("=" * 70)
        print(" [1] 🔍 Tìm kiếm phim theo tên / thể loại")
        print(" [2] ⚡ Tải phim theo Series ID (kèm tùy chọn tự động ghép)")
        print(" [3] 🎬 Ghép các file video có sẵn trong một thư mục")
        print(" [4] 📱 Kiểm tra & Đăng ký lại thiết bị (Device ID)")
        print(" [5] 📁 Đổi thư mục lưu trữ video")
        print(" [0] 🚪 Thoát")
        print("=" * 70)

        try:
            choice = input("👉 Nhập lựa chọn của bạn [0-5]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Tạm biệt!")
            break

        if choice == "1":
            kw = input("\n👉 Nhập từ khóa hoặc tên phim: ").strip()
            if kw:
                results = cli_search(kw)
                if results:
                    sel = input("👉 Nhập số thứ tự phim để tải (hoặc Enter để bỏ qua): ").strip()
                    if sel.isdigit() and 1 <= int(sel) <= len(results):
                        chosen = results[int(sel) - 1]
                        s_id = chosen.get("drama_id")
                        merge_ans = input("👉 Tự động ghép thành 1 video FULL sau khi tải xong? (y/N) [y]: ").strip().lower()
                        auto_merge = merge_ans in ["", "y", "yes", "1"]
                        cut_sec = 0.0
                        if auto_merge:
                            cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây nhạc kết, ví dụ 0): ").strip()
                            cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                        cli_download_series(s_id, output_dir=save_dir, auto_merge=auto_merge, cut_end_seconds=cut_sec)

        elif choice == "2":
            s_id = input("\n👉 Nhập Series ID hoặc Link phim (ví dụ 7369168922572164134): ").strip()
            if s_id:
                merge_ans = input("👉 Tự động ghép thành 1 video FULL sau khi tải xong? (y/N) [y]: ").strip().lower()
                auto_merge = merge_ans in ["", "y", "yes", "1"]
                cut_sec = 0.0
                mirror = False
                if auto_merge:
                    cut_str = input("👉 Cắt bỏ phần cuối mỗi tập (giây nhạc kết, ví dụ 0): ").strip()
                    cut_sec = float(cut_str) if cut_str.replace('.', '', 1).isdigit() else 0.0
                    m_ans = input("👉 Lật hình (Mirror video)? (y/N) [n]: ").strip().lower()
                    mirror = m_ans in ["y", "yes", "1"]
                cli_download_series(s_id, output_dir=save_dir, auto_merge=auto_merge, cut_end_seconds=cut_sec, mirror=mirror)

        elif choice == "3":
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

        elif choice == "4":
            print("\n🔄 Đang thực hiện đăng ký thiết bị mới...")
            try:
                from liushen.device_register import device_register
                res = device_register()
                print(f"✅ Đăng ký thành công: device_id={res.get('device_id')}, install_id={res.get('install_id')}")
            except Exception as exc:
                print(f"❌ Lỗi đăng ký: {exc}")

        elif choice == "5":
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

    # Command: download
    p_dl = subparsers.add_parser("download", help="Tải toàn bộ các tập của một bộ phim")
    p_dl.add_argument("series_id", type=str, help="Series ID hoặc Link phim (ví dụ: 7369168922572164134)")
    p_dl.add_argument("--merge", action="store_true", help="Tự động ghép tất cả các tập thành 1 video FULL")
    p_dl.add_argument("--cut-end", type=float, default=0.0, help="Số giây cắt bỏ ở cuối mỗi tập (mặc định: 0)")
    p_dl.add_argument("--mirror", action="store_true", help="Lật hình ngang (Mirror video)")
    p_dl.add_argument("--clean-parts", action="store_true", help="Xóa các tập lẻ sau khi ghép thành công")
    p_dl.add_argument("--save-dir", type=str, default="", help="Thư mục lưu video (mặc định: Google Drive hoặc ./src)")
    p_dl.add_argument("--codec", type=str, default="h264", choices=["h264", "hevc"], help="Định dạng codec video")
    p_dl.add_argument("--gpu", type=str, default="nvenc", choices=["nvenc", "cpu", "qsv", "amf"], help="Bộ mã hóa phần cứng")

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

    # Command: register
    subparsers.add_parser("register", help="Đăng ký thiết bị mới và lưu cấu hình")

    args = parser.parse_args()

    if not args.command:
        # If no arguments provided, launch the interactive menu
        run_interactive_menu()
        return

    if args.command == "search":
        cli_search(args.keyword, page=args.page)

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
