#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Colab & Remote Server Runner for Short Drama Downloader.
Supports:
  - Multi-tunnel simultaneous launching (Cloudflare, Localtunnel, Pinggy, Localhost.run)
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
from typing import Dict, List


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

    d_id = device_id or os.getenv("DUANJU_DEVICE_ID") or str(cfg.get("device_id", ""))
    i_id = install_id or os.getenv("DUANJU_INSTALL_ID") or str(cfg.get("install_id", ""))

    if not d_id or not i_id:
        print("[Device] Chưa có device_id / install_id. Đang tự động đăng ký thiết bị mới...")
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
                print(f"[Device] Đăng ký thiết bị thành công: device_id={d_id[:4]}***{d_id[-3:] if len(d_id) > 6 else ''}")
        except Exception as exc:
            print(f"[Device] Chú ý: Không thể tự động đăng ký ({exc}). Bạn có thể nhập ID trên Web UI tại tab Cài đặt.")
    else:
        cfg["device_id"] = d_id
        cfg["install_id"] = i_id
        cfg["platform"] = cfg.get("platform", "android")
        config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.environ["DUANJU_DEVICE_ID"] = d_id
        os.environ["DUANJU_INSTALL_ID"] = i_id
        print(f"[Device] Đã cấu hình thiết bị: device_id={d_id[:4]}***{d_id[-3:] if len(d_id) > 6 else ''}")


def start_cloudflare_tunnel(port: int = 5000) -> str:
    """Download cloudflared binary and start a Cloudflare tunnel."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    binary_name = "cloudflared.exe" if system == "windows" else "cloudflared"
    bin_path = BASE_DIR / binary_name

    if not bin_path.exists() or not os.access(bin_path, os.X_OK):
        print("[Tunnel] Đang tải Cloudflare Tunnel (cloudflared)...")
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
        except Exception as exc:
            print(f"[Cloudflare Tunnel] Lỗi tải cloudflared: {exc}")
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
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line.strip())
                if m:
                    tunnel_url[0] = m.group(0)
                    url_found_event.set()
        except Exception:
            url_found_event.set()

    threading.Thread(target=_monitor, daemon=True).start()
    url_found_event.wait(timeout=12)
    return tunnel_url[0]


def start_localtunnel(port: int = 5000) -> str:
    """Start localtunnel via npx."""
    if not shutil.which("npx"):
        return ""

    cmd = ["npx", "-y", "localtunnel", "--port", str(port)]
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
                m = re.search(r"https://[a-zA-Z0-9-]+\.loca\.lt", line.strip())
                if m:
                    tunnel_url[0] = m.group(0)
                    url_found_event.set()
        except Exception:
            url_found_event.set()

    threading.Thread(target=_monitor, daemon=True).start()
    url_found_event.wait(timeout=10)
    return tunnel_url[0]


def start_pinggy_tunnel(port: int = 5000) -> str:
    """Start Pinggy SSH tunnel."""
    if not shutil.which("ssh"):
        return ""

    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-p", "443",
        f"-R0:localhost:{port}",
        "a.pinggy.io"
    ]
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
                m = re.search(r"https://[a-zA-Z0-9-]+\.a\.pinggy\.(?:link|io)", line.strip())
                if m:
                    tunnel_url[0] = m.group(0)
                    url_found_event.set()
        except Exception:
            url_found_event.set()

    threading.Thread(target=_monitor, daemon=True).start()
    url_found_event.wait(timeout=8)
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


def get_public_ip() -> str:
    """Get public IP for Localtunnel password bypass."""
    try:
        req = urllib.request.urlopen("https://ipv4.icanhazip.com", timeout=3)
        return req.read().decode("utf-8").strip()
    except Exception:
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
            parser_module.handle_video_request(
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


def print_banner(tunnels: Dict[str, str], port: int, save_dir: Path):
    """Print clean, formatted banner with multiple live tunnel links and instructions."""
    colab_env = is_colab()
    print("\n" + "=" * 70)
    print("        🎬 TRÌNH TẢI & GHÉP PHIM NGẮN (GOOGLE COLAB & SERVER)       ")
    print("=" * 70)
    print(f" 🌐 Web Server Local: http://127.0.0.1:{port}")
    print("\n 🚀 ĐƯỜNG DẪN TRUY CẬP CÔNG KHAI (CHỌN 1 TRONG CÁC LINK DƯỚI ĐÂY):")

    has_any = False
    for name, url in tunnels.items():
        if url:
            has_any = True
            print(f"   👉 [{name}]: \033[1;32m{url}\033[0m")

    if not has_any:
        print("   ⚠️ Đang kết nối tunnel... Nếu link chưa hiện, vui lòng thử lại sau vài giây.")

    public_ip = get_public_ip()
    if public_ip and any("loca.lt" in u for u in tunnels.values()):
        print(f"\n 🔑 Mật khẩu Localtunnel (Endpoint IP nếu được hỏi): \033[1;33m{public_ip}\033[0m")

    if colab_env:
        try:
            from google.colab import output
            output.serve_kernel_port_as_window(port)
            print(f" 📱 Colab Port Proxy đã được kích hoạt trực tiếp trong notebook!")
        except Exception:
            pass

    print(f"\n 💾 Thư mục lưu video: {save_dir}")
    print("=" * 70)
    print("  💡 Lưu ý: Nếu đường link Cloudflare báo lỗi 'This site can’t be reached'")
    print("     (do nhà mạng VN chặn DNS trycloudflare), hãy click link Localtunnel/Pinggy ở trên!")
    print("  * Nhấn Ctrl+C để dừng server.")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Google Colab Runner for Short Drama Downloader")
    parser.add_argument("--port", type=int, default=5000, help="Port to run Flask app (default: 5000)")
    parser.add_argument("--tunnel", type=str, default="auto", choices=["auto", "cloudflare", "localtunnel", "pinggy", "ngrok", "none"], help="Tunnel provider")
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

    # Start tunnels
    tunnels: Dict[str, str] = {}

    if args.tunnel in ["auto", "cloudflare"]:
        cf_url = start_cloudflare_tunnel(args.port)
        if cf_url:
            tunnels["Cloudflare Tunnel (Khuyên dùng)"] = cf_url

    if args.tunnel in ["auto", "localtunnel"]:
        lt_url = start_localtunnel(args.port)
        if lt_url:
            tunnels["Localtunnel (Dự phòng 1 - Ổn định ở VN)"] = lt_url

    if args.tunnel in ["auto", "pinggy"]:
        pg_url = start_pinggy_tunnel(args.port)
        if pg_url:
            tunnels["Pinggy (Dự phòng 2 - Tốc độ cao)"] = pg_url

    if args.tunnel == "ngrok" or args.ngrok_token:
        ngrok_url = start_ngrok_tunnel(args.port, args.ngrok_token)
        if ngrok_url:
            tunnels["Ngrok Tunnel"] = ngrok_url

    print_banner(tunnels, args.port, save_dir)

    # Launch Flask application
    from app import app
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
