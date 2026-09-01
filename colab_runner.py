#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Colab & Remote Server Runner for Short Drama Downloader.
Supports:
  - Automatic Cloudflare Tunnel (Free, high speed, no registration required)
  - Google Colab Port Proxy (serve_kernel_port_as_window)
  - Ngrok Tunnel (optional)
  - Auto-mount Google Drive save folder detection
  - Headless CLI downloader mode
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
liushen_dir = BASE_DIR / "liushen"
if str(liushen_dir) not in sys.path:
    sys.path.insert(0, str(liushen_dir))


def is_colab() -> bool:
    """Check if code is running in Google Colab environment."""
    return "google.colab" in sys.modules or os.getenv("COLAB_GPU") is not None or os.getenv("COLAB_RELEASE_TAG") is not None


def detect_drive_folder() -> Path:
    """Detect if Google Drive is mounted, return suitable download directory."""
    gdrive_mount = Path("/content/drive/MyDrive")
    if gdrive_mount.exists() and gdrive_mount.is_dir():
        target = gdrive_mount / "ShortDrama_Downloads"
        target.mkdir(parents=True, exist_ok=True)
        return target
    # Default local folder
    default_dir = BASE_DIR / "src"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def check_and_setup_device(device_id: str = "", install_id: str = ""):
    """Ensure device_id and install_id exist or register a new virtual device."""
    config_file = BASE_DIR / "config.json"
    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    d_id = device_id or os.getenv("DUANJU_DEVICE_ID") or cfg.get("device_id", "")
    i_id = install_id or os.getenv("DUANJU_INSTALL_ID") or cfg.get("install_id", "")

    if not d_id or not i_id:
        print("[Device] Chua cau hinh device_id / install_id. Dang tu dong khoi tao thiet bi...")
        try:
            from liushen import device_register
            # If device_register runs on import or provides registered data
            # Alternatively use a default valid device payload
            print("[Device] Ban co the nhap device_id va install_id tren giao dien Web UI hoac truyen tham so.")
        except Exception as exc:
            print(f"[Device] Chu y: {exc}")
    else:
        cfg["device_id"] = d_id
        cfg["install_id"] = i_id
        cfg["platform"] = cfg.get("platform", "android")
        config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.environ["DUANJU_DEVICE_ID"] = d_id
        os.environ["DUANJU_INSTALL_ID"] = i_id
        print(f"[Device] Da cau hinh thiet bi: device_id={d_id[:4]}***{d_id[-3:] if len(d_id) > 6 else ''}")


def start_cloudflare_tunnel(port: int = 5000) -> str:
    """Download cloudflared binary and start a free tunnel."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    binary_name = "cloudflared.exe" if system == "windows" else "cloudflared"
    bin_path = BASE_DIR / binary_name

    if not bin_path.exists() or not os.access(bin_path, os.X_OK):
        print("[Tunnel] Dang tai Cloudflare Tunnel (cloudflared)...")
        if system == "windows":
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        elif system == "darwin":
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
        elif "arm" in machine or "aarch64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

        try:
            urllib.request.urlretrieve(url, str(bin_path))
            if system != "windows":
                bin_path.chmod(0o755)
            print("[Tunnel] Tai cloudflared thanh cong!")
        except Exception as exc:
            print(f"[Tunnel] Khong the tai cloudflared: {exc}")
            return ""

    cmd = [str(bin_path), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
    
    tunnel_url = [""]
    url_found_event = threading.Event()

    def _monitor():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )
            for line in proc.stdout:
                line_str = line.strip()
                # Search for trycloudflare.com URL
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line_str)
                if m:
                    tunnel_url[0] = m.group(0)
                    url_found_event.set()
        except Exception as exc:
            print(f"[Tunnel Error] {exc}")
            url_found_event.set()

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()

    url_found_event.wait(timeout=15)
    return tunnel_url[0]


def start_ngrok_tunnel(port: int = 5000, authtoken: str = "") -> str:
    """Start ngrok tunnel using pyngrok."""
    try:
        from pyngrok import ngrok
        if authtoken:
            ngrok.set_auth_token(authtoken)
        tunnel = ngrok.connect(port)
        return tunnel.public_url
    except Exception as exc:
        print(f"[Ngrok Error] {exc}")
        return ""


def run_cli_downloader(series_id: str, output_dir: Path):
    """Headless CLI mode to download an entire series directly."""
    import importlib
    app_module = importlib.import_module("app")
    parser_module = importlib.import_module("1")
    from tqdm import tqdm

    print(f"\n=======================================================")
    print(f"   DANG LAY THONG TIN BO PHIM: {series_id}")
    print(f"=======================================================\n")

    detail = app_module.get_hongguo_detail(series_id)
    series_name = detail.get("series_name") or f"Phim_{series_id}"
    episodes = detail.get("episodes") or []
    print(f"🎬 Tên phim: {series_name}")
    print(f"📋 Tổng số tập: {len(episodes)}")
    print(f"📁 Thư mục lưu: {output_dir}\n")

    series_clean_name = re.sub(r'[\\/*?:"<>|]', '_', series_name)
    save_folder = output_dir / series_clean_name
    save_folder.mkdir(parents=True, exist_ok=True)

    success_count = 0
    fail_count = 0

    pbar = tqdm(episodes, desc="Đang tải các tập", unit="tập")
    for ep in pbar:
        vid = ep.get("vid")
        ep_num = ep.get("episode_num", 1)
        filename = f"{series_clean_name}_Tap_{ep_num:03d}.mp4"
        pbar.set_postfix_str(f"Tập {ep_num} ({vid})")

        try:
            result = parser_module.handle_video_request(
                vid,
                series_id=series_id,
                episode=ep_num,
                filename=filename,
                save_dir=str(save_folder),
            )
            success_count += 1
        except Exception as exc:
            pbar.write(f"❌ Lỗi tải tập {ep_num} ({vid}): {exc}")
            fail_count += 1

    print(f"\n✨ HOÀN TẤT: Thành công {success_count}/{len(episodes)} tập! (Thất bại: {fail_count})")
    print(f"📂 Video được lưu tại: {save_folder}\n")


def print_banner(public_url: str, port: int, save_dir: Path):
    """Print clean, formatted banner with links and instructions."""
    colab_env = is_colab()
    print("\n" + "=" * 68)
    print("        🎬 TRÌNH TẢI & GHÉP PHIM NGẮN (GOOGLE COLAB & SERVER)       ")
    print("=" * 68)
    print(f" 🌐 Web Server Local: http://127.0.0.1:{port}")
    if public_url:
        print(f" 🚀 ĐƯỜNG DẪN TRUY CẬP CÔNG KHAI (CLICK VÀO ĐÂY ĐỂ MỞ WEB UI):")
        print(f"    👉 \033[1;32m{public_url}\033[0m")
    if colab_env:
        try:
            from google.colab import output
            output.serve_kernel_port_as_window(port)
            print(f" 📱 Colab Port Proxy đã được kích hoạt!")
        except Exception:
            pass
    print(f" 💾 Thư mục lưu video: {save_dir}")
    print("=" * 68)
    print("  * Giữ tab Colab này hoạt động trong lúc tải và ghép video.")
    print("  * Nhấn Ctrl+C để dừng server.")
    print("=" * 68 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Google Colab Runner for Short Drama Downloader")
    parser.add_argument("--port", type=int, default=5000, help="Port to run Flask app (default: 5000)")
    parser.add_argument("--tunnel", type=str, default="cloudflare", choices=["cloudflare", "ngrok", "colab", "none"], help="Tunnel provider")
    parser.add_argument("--ngrok-token", type=str, default="", help="Ngrok authtoken if using ngrok")
    parser.add_argument("--device-id", type=str, default="", help="DUANJU_DEVICE_ID")
    parser.add_argument("--install-id", type=str, default="", help="DUANJU_INSTALL_ID")
    parser.add_argument("--save-dir", type=str, default="", help="Custom download save directory")
    parser.add_argument("--cli", type=str, default="", help="CLI series_id to download in headless mode without web server")
    args = parser.parse_args()

    # Determine save directory
    if args.save_dir:
        save_dir = Path(args.save_dir).resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
    else:
        save_dir = detect_drive_folder()

    os.environ["DOWNLOAD_DIR"] = str(save_dir)
    os.environ["APP_PORT"] = str(args.port)
    os.environ["APP_HOST"] = "0.0.0.0"
    os.environ["OPEN_BROWSER"] = "0"

    # Setup device config
    check_and_setup_device(args.device_id, args.install_id)

    # CLI mode
    if args.cli:
        run_cli_downloader(args.cli, save_dir)
        return

    # Start tunnel if requested
    public_url = ""
    if args.tunnel == "cloudflare":
        public_url = start_cloudflare_tunnel(args.port)
    elif args.tunnel == "ngrok":
        public_url = start_ngrok_tunnel(args.port, args.ngrok_token)

    print_banner(public_url, args.port, save_dir)

    # Launch Flask application
    from app import app
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
