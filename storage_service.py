"""
storage_service.py - Client service for storage.to REST API.
Documentation: REST API - Docs - storage.to.html
Supports:
  - Single upload (<= 50MB)
  - Multipart upload (> 50MB) directly to Cloudflare R2 presigned URLs
  - Anonymous uploads via visitor token and authenticated uploads via API token
  - Progress reporting callback (0 - 100%)
"""

import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    import requests
except ImportError:
    requests = None


STORAGE_TO_API_BASE = "https://storage.to/api"


def get_token_storage_path() -> Path:
    """Return local path for persisting visitor token."""
    config_dir = Path.home() / ".config" / "storageto"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "token"
    except Exception:
        fallback_dir = Path(__file__).resolve().parent / ".storage_to"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir / "visitor_token"


def get_or_create_visitor_token() -> str:
    """Retrieve existing visitor token or create a new one."""
    env_token = os.getenv("STORAGE_TO_VISITOR_TOKEN", "").strip()
    if env_token:
        return env_token

    token_file = get_token_storage_path()
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    new_token = uuid.uuid4().hex
    try:
        token_file.write_text(new_token, encoding="utf-8")
    except Exception:
        pass
    return new_token


def guess_content_type(file_path: Union[str, Path]) -> str:
    """Infer MIME content type for file."""
    path = Path(file_path)
    ext = path.suffix.lower()
    custom_types = {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".ts": "video/mp2t",
        ".mov": "video/quicktime",
        ".srt": "text/plain",
        ".ass": "text/plain",
        ".vtt": "text/vtt",
        ".json": "application/json",
        ".txt": "text/plain",
    }
    if ext in custom_types:
        return custom_types[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _build_auth_headers(visitor_token: str, api_token: Optional[str] = None,
                        owner_token: Optional[str] = None) -> Dict[str, str]:
    """Construct request headers with authentication and visitor proof."""
    headers = {
        "Content-Type": "application/json",
        "X-Visitor-Token": visitor_token,
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        if owner_token:
            headers["X-Owner-Token"] = owner_token
    elif owner_token:
        headers["Authorization"] = f"Owner {owner_token}"
    return headers


def upload_file_to_storage_to(
    file_path: Union[str, Path],
    api_token: Optional[str] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Upload a file to storage.to and return shareable file information.
    Automatically handles single upload (<= 50MB) and multipart upload (> 50MB).
    """
    if not requests:
        raise RuntimeError("Thư viện 'requests' là bắt buộc để sử dụng storage.to")

    path = Path(file_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Tệp không tồn tại: {path}")

    file_size = path.stat().st_size
    if file_size == 0:
        raise ValueError(f"Tệp rỗng (0 bytes): {path}")

    filename = path.name
    content_type = guess_content_type(path)
    visitor_token = get_or_create_visitor_token()
    api_token = api_token or os.getenv("STORAGE_TO_API_TOKEN")

    def _notify(pct: float, msg: str):
        if on_progress:
            try:
                on_progress(min(100.0, max(0.0, pct)), msg)
            except Exception:
                pass

    _notify(2.0, "Đang khởi tạo upload lên storage.to...")

    # Step 1: POST /upload/init
    init_headers = _build_auth_headers(visitor_token, api_token=api_token)
    init_payload = {
        "filename": filename,
        "content_type": content_type,
        "size": file_size,
    }

    try:
        resp = requests.post(
            f"{STORAGE_TO_API_BASE}/upload/init",
            headers=init_headers,
            json=init_payload,
            timeout=timeout,
        )
        if resp.status_code not in (200, 201):
            err_data = {}
            try:
                err_data = resp.json()
            except Exception:
                pass
            err_msg = err_data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
            raise RuntimeError(f"storage.to /upload/init thất bại: {err_msg}")
        init_res = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Không thể kết nối đến storage.to /upload/init: {exc}") from exc

    upload_type = init_res.get("type", "single")
    r2_key = init_res.get("r2_key")
    if not r2_key:
        raise RuntimeError(f"Phản hồi từ storage.to thiếu r2_key: {init_res}")

    # Case 1: Single upload (<= 50MB)
    if upload_type == "single":
        upload_url = init_res.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"Phản hồi storage.to thiếu upload_url: {init_res}")

        _notify(10.0, "Đang tải dữ liệu trực tiếp lên máy chủ lưu trữ...")

        # Stream / PUT file bytes
        with open(path, "rb") as f:
            put_resp = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": content_type},
                timeout=max(timeout, 300),
            )
        if put_resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"PUT file lên R2 thất bại: HTTP {put_resp.status_code}")

        _notify(90.0, "Đang xác nhận hoàn tất tải lên...")

        # Confirm upload
        conf_headers = _build_auth_headers(visitor_token, api_token=api_token)
        conf_payload = {
            "filename": filename,
            "size": file_size,
            "content_type": content_type,
            "r2_key": r2_key,
        }
        conf_resp = requests.post(
            f"{STORAGE_TO_API_BASE}/upload/confirm",
            headers=conf_headers,
            json=conf_payload,
            timeout=timeout,
        )
        if conf_resp.status_code not in (200, 201):
            err_data = {}
            try:
                err_data = conf_resp.json()
            except Exception:
                pass
            err_msg = err_data.get("error") or f"HTTP {conf_resp.status_code}: {conf_resp.text[:300]}"
            raise RuntimeError(f"storage.to /upload/confirm thất bại: {err_msg}")

        conf_res = conf_resp.json()
        file_info = conf_res.get("file") or {}
        file_url = file_info.get("url") or f"https://storage.to/{file_info.get('id', '')}"

        _notify(100.0, "Tải lên storage.to thành công!")
        return {
            "success": True,
            "url": file_url,
            "id": file_info.get("id"),
            "filename": file_info.get("filename", filename),
            "size": file_info.get("size", file_size),
            "human_size": file_info.get("human_size", ""),
            "expires_at": file_info.get("expires_at", ""),
            "owner_token": conf_res.get("owner_token") or init_res.get("owner_token"),
        }

    # Case 2: Multipart upload (> 50MB)
    upload_id = init_res.get("upload_id")
    part_size = int(init_res.get("part_size") or 33554432)  # Default ~32MB
    total_parts = int(init_res.get("total_parts") or ((file_size + part_size - 1) // part_size))
    initial_urls = init_res.get("initial_urls") or {}
    owner_token = init_res.get("owner_token", "")

    uploaded_parts: List[Dict[str, Any]] = []

    try:
        with open(path, "rb") as f:
            for part_num in range(1, total_parts + 1):
                part_pct = 10.0 + ((part_num - 1) / total_parts) * 75.0
                _notify(part_pct, f"Đang tải lên phần {part_num}/{total_parts} lên storage.to...")

                part_url = initial_urls.get(str(part_num))
                if not part_url:
                    # Request part url
                    part_req_headers = _build_auth_headers(visitor_token, api_token=api_token, owner_token=owner_token)
                    part_req_payload = {"upload_id": upload_id, "part_numbers": [part_num]}
                    p_resp = requests.post(
                        f"{STORAGE_TO_API_BASE}/upload/parts",
                        headers=part_req_headers,
                        json=part_req_payload,
                        timeout=timeout,
                    )
                    if p_resp.status_code not in (200, 201):
                        raise RuntimeError(f"Lấy part URL #{part_num} thất bại: {p_resp.text[:200]}")
                    p_res = p_resp.json()
                    p_list = p_res.get("part_urls") or []
                    for item in p_list:
                        if item.get("partNumber") == part_num:
                            part_url = item.get("url")
                            break
                    if not part_url:
                        raise RuntimeError(f"Không nhận được URL cho part #{part_num}")

                # Read chunk slice
                chunk_bytes = f.read(part_size)
                if not chunk_bytes:
                    break

                # Upload chunk with retries
                etag = None
                for attempt in range(1, 4):
                    try:
                        put_chunk_resp = requests.put(
                            part_url,
                            data=chunk_bytes,
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=max(timeout, 300),
                        )
                        if put_chunk_resp.status_code in (200, 201, 204):
                            etag = put_chunk_resp.headers.get("ETag") or put_chunk_resp.headers.get("etag")
                            if etag:
                                etag = etag.strip()
                            break
                    except Exception as upload_err:
                        if attempt >= 3:
                            raise upload_err
                        time.sleep(1.5)

                if not etag:
                    etag = f'"{uuid.uuid4().hex}"'

                uploaded_parts.append({"partNumber": part_num, "etag": etag})

        # Complete multipart
        _notify(88.0, "Đang đóng gói và hoàn tất tải lên đa phần...")
        comp_headers = _build_auth_headers(visitor_token, api_token=api_token, owner_token=owner_token)
        comp_payload = {
            "upload_id": upload_id,
            "parts": uploaded_parts,
        }
        comp_resp = requests.post(
            f"{STORAGE_TO_API_BASE}/upload/complete-multipart",
            headers=comp_headers,
            json=comp_payload,
            timeout=timeout,
        )
        if comp_resp.status_code not in (200, 201):
            raise RuntimeError(f"Complete multipart thất bại: {comp_resp.text[:300]}")

        # Confirm upload
        _notify(93.0, "Đang xác nhận hoàn tất tải lên...")
        conf_headers = _build_auth_headers(visitor_token, api_token=api_token, owner_token=owner_token)
        conf_payload = {
            "filename": filename,
            "size": file_size,
            "content_type": content_type,
            "r2_key": r2_key,
        }
        conf_resp = requests.post(
            f"{STORAGE_TO_API_BASE}/upload/confirm",
            headers=conf_headers,
            json=conf_payload,
            timeout=timeout,
        )
        if conf_resp.status_code not in (200, 201):
            err_data = {}
            try:
                err_data = conf_resp.json()
            except Exception:
                pass
            err_msg = err_data.get("error") or f"HTTP {conf_resp.status_code}: {conf_resp.text[:300]}"
            raise RuntimeError(f"storage.to /upload/confirm thất bại: {err_msg}")

        conf_res = conf_resp.json()
        file_info = conf_res.get("file") or {}
        file_url = file_info.get("url") or f"https://storage.to/{file_info.get('id', '')}"

        _notify(100.0, "Tải lên storage.to thành công!")
        return {
            "success": True,
            "url": file_url,
            "id": file_info.get("id"),
            "filename": file_info.get("filename", filename),
            "size": file_info.get("size", file_size),
            "human_size": file_info.get("human_size", ""),
            "expires_at": file_info.get("expires_at", ""),
            "owner_token": conf_res.get("owner_token") or owner_token,
        }

    except Exception as exc:
        # Abort multipart on failure to release R2 resources
        try:
            abort_headers = _build_auth_headers(visitor_token, api_token=api_token, owner_token=owner_token)
            requests.post(
                f"{STORAGE_TO_API_BASE}/upload/abort",
                headers=abort_headers,
                json={"upload_id": upload_id},
                timeout=10,
            )
        except Exception:
            pass
        raise RuntimeError(f"Lỗi khi tải lên storage.to (Multipart): {exc}") from exc
