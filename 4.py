# -*- coding: utf-8 -*-
"""
Python script to display latest short drama releases (Hôm nay lên sóng / 今日上新 / 最新上架).
Derived from:
  - config.example.json (device & session credentials)
  - hongguo.py (landpage category endpoint, genre settings, latest() function, CLI design)
  - safeguards.py (caching, throttling, and anti-risk controls)
  - 1.py, 2.py, 3.py (Liushen signing, device management, helper utilities, CLI style)

Usage:
  python 4.py                             # Hiển thị phim ngắn mới hôm nay (short_play, 今日上新)
  python 4.py --all                       # Hiển thị tất cả phim mới lên sóng gần đây (bỏ lọc 今日上新)
  python 4.py -g comic_series             # Hiển thị mạn kịch / hoạt hình mới nhất (7 ngày qua)
  python 4.py -g ai_series                # Hiển thị phim ngắn AI mới nhất
  python 4.py --limit 30                  # Giới hạn số lượng hiển thị (mặc định: 60)
  python 4.py --json                      # Xuất kết quả đầy đủ dưới dạng JSON
  python 4.py --ids                       # Chỉ xuất danh sách series_id (tiện pipe sang 2.py)
  python 4.py --no-cache                  # Bỏ qua bộ nhớ đệm cache (làm mới dữ liệu)
  python 4.py --watch [giây]              # Chế độ theo dõi phim mới tự động định kỳ

Example:
  python 4.py
  python 4.py --all --limit 20
  python 4.py -g comic_series --limit 10
  python 4.py --ids
  python 4.py --json
"""

import os
import re
import sys
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Set
from urllib.parse import urlsplit, parse_qsl

import urllib3
import requests

urllib3.disable_warnings()

# Reconfigure stdout/stderr encoding for UTF-8 compatibility (especially on Windows)
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

# Ensure liushen package and base directory are in sys.path
BASE_DIR = Path(__file__).resolve().parent
LIUSHEN_DIR = BASE_DIR / "liushen"
if str(LIUSHEN_DIR) not in sys.path:
    sys.path.insert(0, str(LIUSHEN_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Import Liushen core signer
try:
    from liushen.flurl.core import core_sixgod
    _HAS_LIUSHEN = True
except Exception:
    _HAS_LIUSHEN = False

# Import safeguards (cache, throttle, risk detection)
try:
    import safeguards as SG
    from safeguards import RiskControlError, AuthExpiredError
    _HAS_SAFEGUARDS = True
except Exception:
    _HAS_SAFEGUARDS = False
    SG = None
    RiskControlError = Exception
    AuthExpiredError = Exception


# ─── Environment & Configuration ──────────────────────────────────────────────

def load_dotenv_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from .env without extra dependencies."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv_file(BASE_DIR / ".env")

USER_AGENT = (
    "com.phoenix.read/71332 (Linux; U; Android 16; zh_CN; 25053RT47C; "
    "Build/BP2A.250605.031.A3; Cronet/TTNetVersion:04657795 2026-01-23 "
    "QuicVersion:c67e9834 2025-09-08)"
)

DEFAULT_API_HOSTS = [
    "api5-normal-sinfonlinea.fqnovel.com",
    "api5-normal-sinfonlineb.fqnovel.com",
]

HONGGUO_PROXY = (
    os.environ.get("HONGGUO_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("HTTPS_PROXY")
    or ""
).strip()

# Danh sách API landpage không yêu cầu ký chữ ký nặng (Sixgod). Bật HG_SIGN_LIST=1 nếu muốn ép ký.
SIGN_LIST = (os.environ.get("HG_SIGN_LIST") or "").strip().lower() in ("1", "true", "yes", "on")


def get_proxies() -> Optional[Dict[str, str]]:
    """Return proxy configuration for external requests if configured."""
    if HONGGUO_PROXY:
        return {"http": HONGGUO_PROXY, "https": HONGGUO_PROXY}
    return None


def load_local_config() -> Dict[str, Any]:
    """Load config.json if available. Never fall back to config.example.json to avoid template placeholders."""
    config_path = BASE_DIR / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_device_keys() -> Dict[str, str]:
    """Resolve device credentials from environment variables or local config."""
    config = load_local_config()
    base_query = config.get("base_query", {}) if isinstance(config.get("base_query"), dict) else {}

    device_id = (
        os.getenv("DUANJU_DEVICE_ID")
        or str(config.get("device_id") or base_query.get("device_id") or "")
    ).strip()

    install_id = (
        os.getenv("DUANJU_INSTALL_ID")
        or str(config.get("install_id") or base_query.get("iid") or config.get("DUANJU_INSTALL_ID") or "")
    ).strip()

    platform = (
        os.getenv("DUANJU_PLATFORM")
        or str(config.get("platform") or base_query.get("device_platform") or "android")
    ).strip() or "android"

    api_host = (
        os.getenv("DUANJU_API_HOST")
        or str(config.get("api_host") or DEFAULT_API_HOSTS[0])
    ).strip() or DEFAULT_API_HOSTS[0]

    return {
        "device_id": device_id,
        "install_id": install_id,
        "platform": platform,
        "api_host": api_host,
    }


def build_liushen_device(device_keys: Dict[str, str]) -> Dict[str, str]:
    """Build the device signature payload required by Liushen signing."""
    return {
        "device_id": device_keys.get("device_id", ""),
        "iid": device_keys.get("install_id", ""),
        "install_id": device_keys.get("install_id", ""),
        "device_brand": "Redmi",
        "device_model": "25053RT47C",
        "device_type": "25053RT47C",
        "device_manufacturer": "Xiaomi",
        "os_version": "16",
        "version_name": "7.1.3.32",
        "ua": USER_AGENT,
    }


# ─── URL & Request Signing ───────────────────────────────────────────────────

def build_base_query(device_keys: Dict[str, str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Build unified base query parameters."""
    config = load_local_config()
    cfg_base = dict(config.get("base_query", {})) if isinstance(config.get("base_query"), dict) else {}

    q = {
        "iid": device_keys.get("install_id", ""),
        "device_id": device_keys.get("device_id", ""),
        "ac": "wifi",
        "channel": cfg_base.get("channel", "update_64"),
        "aid": cfg_base.get("aid", "8662"),
        "app_name": cfg_base.get("app_name", "novelread"),
        "version_code": cfg_base.get("version_code", "71332"),
        "version_name": cfg_base.get("version_name", "7.1.3.32"),
        "device_platform": device_keys.get("platform", "android"),
        "os": "android",
        "ssmix": "a",
        "device_type": cfg_base.get("device_type", "25053RT47C"),
        "device_brand": "Redmi",
        "language": "zh",
        "os_api": "36",
        "os_version": cfg_base.get("os_version", "16"),
        "manifest_version_code": cfg_base.get("version_code", "71332"),
        "resolution": "1280*2772",
        "dpi": "520",
        "update_version_code": cfg_base.get("update_version_code", "71332"),
        "host_abi": "arm64-v8a",
        "dragon_device_type": "phone",
        "pv_player": "71332",
        "compliance_status": "0",
        "need_personal_recommend": "1",
        "player_so_load": "1",
        "is_android_pad_screen": "0",
    }

    # Clean template placeholders
    for k, v in list(q.items()):
        val_str = str(v).strip()
        if val_str.startswith("<") and val_str.endswith(">"):
            if k == "channel":
                q[k] = "update_64"
            elif k == "device_type":
                q[k] = "25053RT47C"
            else:
                del q[k]

    for k in ("cdid", "klink_egdi"):
        if k in cfg_base and cfg_base[k]:
            v = str(cfg_base[k]).strip()
            if not (v.startswith("<") and v.endswith(">")):
                q[k] = v

    if extra:
        for k, v in extra.items():
            if v is not None:
                q[k] = str(v)

    q["_rticket"] = str(int(time.time() * 1000))
    return q


def build_url(path: str, extra_query: Optional[Dict[str, Any]] = None, host: Optional[str] = None) -> str:
    """Build target URL with base query parameters."""
    device_keys = get_device_keys()
    api_host = host or device_keys.get("api_host", DEFAULT_API_HOSTS[0])
    q = build_base_query(device_keys, extra_query)
    qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in q.items())
    return f"https://{api_host}{path}?{qs}"


def sign_request(
    url: str,
    method: str = "GET",
    body_data: Any = None,
    device_keys: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str], Optional[bytes]]:
    """Sign GET or POST request with Liushen algorithm."""
    if device_keys is None:
        device_keys = get_device_keys()

    ts = str(int(time.time() * 1000))
    base_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json; charset=utf-8,application/x-protobuf",
        "Content-Type": "application/json; charset=UTF-8",
        "x-xs-from-web": "0",
        "x-ss-req-ticket": ts,
        "x-tt-request-tag": "t=0;n=0",
        "sdk-version": "2",
        "passport-sdk-version": "50561",
        "x-vc-bdturing-sdk-version": "3.7.2.cn",
    }

    cfg = load_local_config()
    session_headers = cfg.get("session_headers", {})
    if isinstance(session_headers, dict):
        for k, v in session_headers.items():
            kl = str(k).lower()
            val = str(v).strip("[]")
            if val and not (val.startswith("<") and val.endswith(">")) and kl in (
                "cookie", "x-tt-token", "x-tt-store-region", "x-tt-store-region-src"
            ):
                try:
                    val.encode("latin-1")
                    base_headers[k] = val
                except UnicodeEncodeError:
                    pass

    url_parts = urlsplit(url)
    base_url = f"{url_parts.scheme}://{url_parts.netloc}{url_parts.path}"
    params = dict(parse_qsl(url_parts.query, keep_blank_values=True))

    post_bytes = None
    data_dict: Dict[str, Any] = {}

    if method.upper() == "POST" and body_data is not None:
        if isinstance(body_data, dict):
            data_dict = body_data
            body_text = json.dumps(body_data, ensure_ascii=False, separators=(",", ":"))
            post_bytes = body_text.encode("utf-8")
        elif isinstance(body_data, bytes):
            post_bytes = body_data
            try:
                data_dict = json.loads(body_data.decode("utf-8"))
            except Exception:
                data_dict = {}
        if post_bytes:
            base_headers["x-ss-stub"] = hashlib.md5(post_bytes).hexdigest().upper()

    if _HAS_LIUSHEN:
        sign_headers, sign_url = core_sixgod(
            surl=base_url,
            params=params,
            data=data_dict,
            devices=build_liushen_device(device_keys),
            header=base_headers,
            log=False,
        )
    else:
        qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
        sign_url = f"{base_url}?{qs}"
        sign_headers = base_headers

    sign_headers.pop("accept-encoding", None)
    return sign_url, sign_headers, post_bytes


def _api_once(
    method: str,
    path: str,
    body: Any = None,
    extra_query: Optional[Dict[str, Any]] = None,
    signed: bool = False,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute single HTTP request with safeguards & error handling."""
    device_keys = get_device_keys()
    url = build_url(path, extra_query=extra_query, host=host)

    if signed:
        sign_url, headers, data_bytes = sign_request(url, method=method, body_data=body, device_keys=device_keys)
    else:
        sign_url = url
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json; charset=utf-8,application/x-protobuf",
            "Content-Type": "application/json; charset=UTF-8",
        }
        data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None

    # Anti-risk throttle
    if _HAS_SAFEGUARDS and SG:
        SG.throttle.wait()

    proxies = get_proxies()
    if method.upper() == "POST":
        resp = requests.post(sign_url, data=data_bytes, headers=headers, timeout=30, verify=False, proxies=proxies)
    else:
        resp = requests.get(sign_url, headers=headers, timeout=30, verify=False, proxies=proxies)

    if not resp.content:
        raise ValueError(f"Empty response body received from {path} (HTTP {resp.status_code})")

    j = resp.json()

    # Safeguards validation
    if _HAS_SAFEGUARDS and SG:
        SG.check_response(j)

    return j


def api(
    method: str,
    path: str,
    body: Any = None,
    extra_query: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    signed: bool = False,
) -> Dict[str, Any]:
    """Execute API call with retry backoff and host failover."""
    device_keys = get_device_keys()
    candidate_hosts = [device_keys.get("api_host", DEFAULT_API_HOSTS[0])]
    for h in DEFAULT_API_HOSTS:
        if h not in candidate_hosts:
            candidate_hosts.append(h)

    last_err = None
    for attempt in range(max_retries):
        for host in candidate_hosts:
            try:
                return _api_once(method, path, body=body, extra_query=extra_query, signed=signed, host=host)
            except AuthExpiredError as aee:
                last_err = aee
                time.sleep(1)
            except RiskControlError as rce:
                last_err = rce
                time.sleep(2 ** attempt + 1)
            except Exception as exc:
                last_err = exc
                time.sleep(0.5 * (attempt + 1))

    raise last_err if last_err else RuntimeError(f"API call failed for {path}")


# ─── Genre Configuration ─────────────────────────────────────────────────────

# 体裁 -> (req_scene, genre)
GENRES = {
    "short_play": ("default", "short_play"),              # 短剧 (Phim ngắn người đóng)
    "comic_series": ("comic_series", "comic_series"),      # 漫剧 (Mạn kịch / Manga animation)
    "ai_series": ("ai_series", "ai_series"),               # AI短剧 (Phim ngắn do AI sáng tạo)
}

GENRE_NAMES = {
    "short_play": "Phim Ngắn (短剧)",
    "comic_series": "Mạn Kịch (漫剧)",
    "ai_series": "Phim Ngắn AI (AI短剧)",
}

GENRE_ALIASES = {
    "short": "short_play",
    "short_play": "short_play",
    "duanju": "short_play",
    "phim": "short_play",
    "comic": "comic_series",
    "comic_series": "comic_series",
    "manhua": "comic_series",
    "ai": "ai_series",
    "ai_series": "ai_series",
}


# ─── Format Helpers ──────────────────────────────────────────────────────────

def format_count(count: Any) -> str:
    """Format numbers into human-readable strings (e.g. 1028, 2.5万, 30.6万)."""
    try:
        val = int(count)
    except (ValueError, TypeError):
        return str(count) if count else "0"

    if val < 10000:
        return str(val)
    elif val < 100000000:
        return f"{val / 10000:.1f}万".replace(".0万", "万")
    else:
        return f"{val / 100000000:.1f}亿".replace(".0亿", "亿")


def format_duration(seconds: Any) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    try:
        sec = int(seconds)
        if sec <= 0:
            return ""
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    except Exception:
        return ""


def normalize_cover_url(url: Any) -> str:
    """Normalize cover URL to standard web JPEG format on ByteDance public CDN."""
    url_str = str(url or "").strip()
    if not url_str:
        return ""
    m = re.search(r"novel-pic/([a-f0-9]+)", url_str)
    if m:
        img_id = m.group(1)
        return f"https://p3-novel.byteimg.com/novel-pic/{img_id}~tplv-shrink:640:0.image"
    return url_str


def parse_release_time(sid: str) -> Dict[str, Any]:
    """Trích xuất thời gian phát hành chính xác từ 64-bit Snowflake ID của ByteDance.

    Cơ chế ByteDance ID:
      32 bit cao nhất (High 32 bits = int(sid) >> 32) chính là Unix Timestamp (giây)
      khi bản ghi phim/tập được tạo và phát hành trên hệ thống cụm máy chủ ByteDance.
    """
    try:
        val = int(sid)
        ts = val >> 32
        # Kiểm tra khoảng timestamp hợp lệ (năm 2020 - 2035)
        if ts < 1577836800 or ts > 2051222400:
            return {"timestamp": 0, "time_vn": "", "time_bj": "", "relative": ""}

        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_vn = dt_utc.astimezone(timezone(timedelta(hours=7)))
        dt_bj = dt_utc.astimezone(timezone(timedelta(hours=8)))
        now_utc = datetime.now(timezone.utc)
        diff_sec = int((now_utc - dt_utc).total_seconds())

        if diff_sec < 60:
            rel = "Vừa xong"
        elif diff_sec < 3600:
            rel = f"{diff_sec // 60} phút trước"
        elif diff_sec < 86400:
            rel = f"{diff_sec // 3600} giờ trước"
        elif diff_sec < 86400 * 30:
            rel = f"{diff_sec // 86400} ngày trước"
        else:
            rel = f"{diff_sec // (86400 * 30)} tháng trước"

        return {
            "timestamp": ts,
            "time_vn": dt_vn.strftime("%Y-%m-%d %H:%M:%S"),
            "time_bj": dt_bj.strftime("%Y-%m-%d %H:%M:%S"),
            "relative": rel,
        }
    except Exception:
        return {"timestamp": 0, "time_vn": "", "time_bj": "", "relative": ""}


# ─── Vietnamese Translation Mappings ─────────────────────────────────────────

TAG_VN_MAP = {
    # Nhóm bộ lọc
    "genre": "Thể Loại Lớn",
    "category_dim_theme": "Chủ Đề",
    "category_dim_role": "Thiết Lập / Vai Trò",
    "category_dim_epoch": "Bối Cảnh / Thời Đại",
    "sort": "Sắp Xếp",
    "gender": "Đối Tượng Khán Giả",
    "online_time": "Thời Gian Lên Sóng",
    # Sắp xếp
    "online_time": "Mới Lên Sóng",
    "hot_score": "Thịnh Hành Nhất",
    "hot_collect": "Sưu Tầm Nhiều Nhất",
    "score": "Điểm Cao Nhất",
    "comprehensive": "Tổng Hợp",
    # Thời gian
    "days_1": "1 ngày qua",
    "days_3": "3 ngày qua",
    "days_7": "7 ngày qua",
    "days_14": "14 ngày qua",
    "days_30": "30 ngày qua",
    "days_90": "90 ngày qua",
    # Đối tượng
    "1": "Nam Tần (Dành cho Nam)",
    "0": "Nữ Tần (Dành cho Nữ)",
    # Thể loại lớn
    "short_play": "Phim Ngắn (短剧)",
    "comic_series": "Mạn Kịch (漫剧)",
    "ai_series": "Phim Ngắn AI (AI短剧)",
    "真人剧": "Phim Ngắn Người Đóng",
    "漫剧": "Mạn Kịch Hoạt Hình",
    "AI剧": "Phim Hoạt Hình AI",
    # Chủ đề
    "现言": "Hiện Ngôn (Hiện Đại)",
    "女性成长": "Nữ Quyền / Trưởng Thành",
    "脑洞": "Não Động / Đột Phá",
    "奇幻": "Kỳ Huyễn",
    "玄幻": "Huyền Huyễn",
    "古言": "Cổ Ngôn (Cổ Đại)",
    "战神": "Chiến Thần",
    "宫斗": "Cung Đấu",
    "仙侠": "Tiên Hiệp",
    "权谋": "Quyền Mưu",
    "悬疑": "Hồi Hộp / Ly Kỳ",
    "喜剧": "Hài Hước",
    "科幻": "Khoa Học Viễn Tưởng",
    # Thiết lập / Vai trò
    "打脸虐渣": "Vả Mặt / Trừng Trị Tra Nam",
    "大男主": "Nam Chính Mạnh",
    "大女主": "Nữ Cường",
    "马甲": "Ẩn Danh / Thân Phận Kép",
    "重生": "Trọng Sinh",
    "穿越": "Xuyên Không",
    "系统": "Hệ Thống",
    "先婚后爱": "Cưới Trước Yêu Sau",
    "神豪": "Thần Hào / Tỷ Phú",
    "破镜重圆": "Gương Vỡ Lại Lành",
    "豪门": "Hào Môn / Tổng Tài",
    "甜宠": "Ngọt Sủng",
    "娱乐圈": "Giới Giải Trí",
    "赘婿": "Ở Rể / Rể Quý",
    "神医": "Thần Y",
    # Bối cảnh
    "现代": "Hiện Đại",
    "都市": "Đô Thị",
    "古代": "Cổ Đại",
    "乡村": "Nông Thôn",
    "年代": "Niên Đại Thập Niên",
    "架空": "Giả Tưởng / Giá Không",
    "职场": "Công Sở",
    "民国": "Dân Quốc",
    "宫廷": "Cung Đình",
    "校园": "Học Đường",
}


# ─── Category Filter Panel ───────────────────────────────────────────────────

_FILTER_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def get_category_filters(genre: str = "short_play", refresh: bool = False) -> List[Dict[str, Any]]:
    """Lấy bảng các danh mục lọc thời gian thực từ Hongguo API kèm bản dịch tiếng Việt."""
    genre = GENRE_ALIASES.get(genre.lower(), genre)
    if genre not in GENRES:
        genre = "short_play"

    now = time.time()
    if not refresh and genre in _FILTER_CACHE:
        cache_time, cached_data = _FILTER_CACHE[genre]
        if now - cache_time < 3600:  # Cache 1 hour
            return cached_data

    scene, g = GENRES[genre]
    body = {
        "filter_ids": "",
        "req_scene": scene,
        "offset": 0,
        "limit": 1,
        "need_selector_panel": True,
        "req_type": "default",
        "client_req_type": 3,
        "select_items": {
            "category_dim_epoch": [],
            "online_time": [],
            "gender": [],
            "category_dim_role": [],
            "genre": [g],
            "sort": [],
            "category_dim_theme": [],
        },
        "session_id": "",
    }

    try:
        resp = api("POST", "/reading/distribution/category/landpage/v", body=body, signed=SIGN_LIST)
        raw_rows = resp.get("data", {}).get("selector_rows", [])
    except Exception:
        raw_rows = []

    out = []
    for r in raw_rows:
        rtype = str(r.get("type") or "")
        rname = str(r.get("row_name") or "")
        rname_vn = "Thời Gian Lên Sóng" if rtype == "online_time" else (TAG_VN_MAP.get(rtype) or rname)
        items = []
        for it in r.get("items", []):
            iid = str(it.get("selector_item_id") or "")
            name = str(it.get("show_name") or "")
            vn_name = TAG_VN_MAP.get(iid) or TAG_VN_MAP.get(name) or name
            items.append({
                "id": iid,
                "name": name,
                "name_vn": vn_name,
            })
        out.append({
            "type": rtype,
            "row_name": rname,
            "row_name_vn": rname_vn,
            "items": items,
        })

    if out:
        _FILTER_CACHE[genre] = (now, out)

    return out


# ─── Latest Function Core ────────────────────────────────────────────────────

def latest(
    genre: str = "short_play",
    only_today: bool = True,
    max_items: int = 120,
    stop_ids: Optional[Set[str]] = None,
    refresh: bool = False,
    online_time: Optional[List[str]] = None,
    sort: Optional[str] = None,
    gender: Optional[str] = None,
    theme: Optional[List[str]] = None,
    role: Optional[List[str]] = None,
    epoch: Optional[List[str]] = None,
    offset_start: int = 0,
) -> List[Dict[str, Any]]:
    """最新上架。
    - 短剧(short_play): 官方有'今日上新'标签。only_today=True 精确返回今日上新(扫描多页,
      整页无今日才停,处理交错); False 返回最新上架全部。
    - 漫剧/AI(comic_series/ai_series): 官方无'今日'粒度(最细7天)且不暴露上线时间,
      统一返回'7天内上新·最新上架'(days_7筛选+上线时间降序,顶部最新)。only_today 不影响结果,
      item.today 字段对这两类恒为 False(无法判定)。
    - Hỗ trợ đầy đủ bộ lọc: online_time, sort, gender, theme (chủ đề), role (thiết lập), epoch (bối cảnh).
    """
    genre = GENRE_ALIASES.get(genre.lower(), genre)
    if genre not in GENRES:
        raise ValueError(f"genrephải là một trong {list(GENRES)}")

    # Check safeguards cache if not forced refresh and not monitoring mode and no custom complex filters
    has_custom_filters = bool(theme or role or epoch or gender or (sort and sort != "online_time") or (online_time and online_time != ["days_7"]))
    ck = ""
    if not refresh and not stop_ids and not has_custom_filters and _HAS_SAFEGUARDS and SG:
        ck = SG.cache_key("latest", genre, only_today, max_items)
        cached = SG.cache_get(ck)
        if cached is not None:
            return cached

    scene, g = GENRES[genre]
    tag_today = (genre == "short_play")             # Chỉ短剧 có nhãn '今日上新'
    
    # Resolve online_time
    if online_time is not None:
        ot_param = [str(x) for x in online_time if str(x).strip()]
    elif tag_today:
        ot_param = []
    else:
        ot_param = ["days_7"]

    want_today = tag_today and only_today and not (online_time or theme or role or epoch)
    out, shown = [], []
    offset = max(0, int(offset_start or 0))
    pages, done = 0, False

    sort_param = [str(sort).strip()] if sort else ["online_time"]
    gender_param = [str(gender).strip()] if gender and str(gender).strip() in ("0", "1") else []
    theme_param = [str(x).strip() for x in (theme or []) if str(x).strip()]
    role_param = [str(x).strip() for x in (role or []) if str(x).strip()]
    epoch_param = [str(x).strip() for x in (epoch or []) if str(x).strip()]

    while len(out) < max_items and pages < 20:
        body = {
            "filter_ids": ",".join(shown),
            "req_scene": scene,
            "offset": offset,
            "need_selector_panel": False,
            "limit": 18,
            "select_items": {
                "category_dim_epoch": epoch_param,
                "online_time": ot_param,
                "gender": gender_param,
                "category_dim_role": role_param,
                "genre": [g],
                "sort": sort_param,
                "category_dim_theme": theme_param,
            },
            "session_id": "",
            "req_type": "only_content",
            "client_req_type": 3,
        }

        try:
            j = api("POST", "/reading/distribution/category/landpage/v", body=body, signed=SIGN_LIST)
        except Exception as exc:
            if not out:
                raise exc
            break

        items = j.get("data", {}).get("video_data", [])
        if not items:
            break

        page_today = 0
        for it in items:
            sid = str(it.get("series_id") or it.get("book_id") or "")
            if not sid:
                continue

            if stop_ids and sid in stop_ids:
                done = True  # Trúng phim đã giám sát ở lượt trước
                break

            shown.append(sid)
            subs = [str(s.get("content") or "") for s in (it.get("sub_title_list") or [])]
            tag_text = str((it.get("tag_info") or {}).get("text") or "").strip()

            is_today = ("今日上新" in subs) or ("今日" in subs) or (tag_text == "今日上新")
            if is_today:
                page_today += 1

            # Phân loại: Danh mục từ category_schema & sub_title_list
            cats = []
            for nm in re.findall(r'"name":"([^"]+)"', it.get("category_schema", "")):
                if nm and nm not in cats:
                    cats.append(nm)
            if not cats:
                for s in subs:
                    if (not s or s == "今日上新" or re.match(r"^[\d.]+万", s)
                            or "播放" in s or "热度" in s or re.match(r"^\d+集$", s)):
                        continue
                    if s not in cats:
                        cats.append(s)

            if want_today and not is_today:
                continue  # Bỏ qua phim không có nhãn 今日上新 khi đang ở chế độ Hôm Nay chuẩn

            play_cnt = int(it.get("play_cnt") or 0)
            duration = int(it.get("duration") or 0)
            rel_info = parse_release_time(sid)

            raw_cover = str(it.get("cover") or "").strip()
            norm_cover = normalize_cover_url(raw_cover)
            raw_horiz = str(it.get("horiz_cover") or "").strip()
            norm_horiz = normalize_cover_url(raw_horiz)

            # Map Vietnamese category tags
            vn_tags = [TAG_VN_MAP.get(c, c) for c in cats]

            out.append({
                "series_id": sid,
                "title": str(it.get("title") or "").strip(),
                "episode_cnt": int(it.get("episode_cnt") or 0),
                "score": str(it.get("score") or "").strip(),
                "play_cnt": play_cnt,
                "play_cnt_str": format_count(play_cnt),
                "cover": norm_cover or raw_cover,
                "cover_raw": raw_cover,
                "horiz_cover": norm_horiz or raw_horiz,
                "horiz_cover_raw": raw_horiz,
                "category": " / ".join(cats),
                "category_vn": " / ".join(vn_tags),
                "cover_tags": cats[:3],
                "cover_tags_vn": vn_tags[:3],
                "today": is_today,
                "copyright": str(it.get("copyright") or "").strip(),
                "premiere": tag_text,
                "intro": (str(it.get("video_desc") or "").replace("\n", " ").strip())[:120],
                "vid": str(it.get("vid") or "").strip(),
                "duration": duration,
                "duration_str": format_duration(duration),
                "comment_count": int(it.get("comment_count") or 0),
                "release_time": rel_info["time_vn"],
                "release_time_bj": rel_info["time_bj"],
                "release_ts": rel_info["timestamp"],
                "release_relative": rel_info["relative"],
            })

            if len(out) >= max_items:
                break

        pages += 1
        if done:
            break
        if want_today and page_today == 0:
            break
        if not j.get("data", {}).get("has_more", True):
            break
        offset += len(items)

    # Save to safeguards cache (10 minutes) if default call
    if not refresh and not stop_ids and not has_custom_filters and _HAS_SAFEGUARDS and SG and ck and out:
        SG.cache_set(ck, out, ttl=600)

    return out


# ─── Watch / Monitor Mode ────────────────────────────────────────────────────

def monitor_updates(genre: str = "short_play", interval: int = 60) -> None:
    """Giám sát cập nhật phim mới theo thời gian thực (sử dụng stop_ids để lấy gia tăng)."""
    genre = GENRE_ALIASES.get(genre.lower(), genre)
    genre_name = GENRE_NAMES.get(genre, genre)
    print("=" * 75)
    print(f"  📡 ĐANG KHỞI ĐỘNG CHẾ ĐỘ GIÁM SÁT PHIM MỚI: {genre_name}")
    print(f"  ⏱ Chu kỳ kiểm tra: {interval} giây/lần | Nhấn Ctrl+C để dừng")
    print("=" * 75)

    try:
        initial = latest(genre=genre, only_today=False, max_items=60, refresh=True)
        known_ids = {it["series_id"] for it in initial}
        print(f"  [Ban đầu] Đã ghi nhận {len(known_ids)} phim đang có trên hệ thống.\n")
    except Exception as e:
        print(f"  [Lỗi khởi tạo]: {e}")
        known_ids = set()

    round_idx = 1
    while True:
        try:
            time.sleep(interval)
            print(f"[{time.strftime('%H:%M:%S')}] Lần quét #{round_idx}...", end="", flush=True)
            news = latest(genre=genre, only_today=False, max_items=30, stop_ids=known_ids, refresh=True)
            if news:
                print(f" 🚨 PHÁT HIỆN {len(news)} PHIM MỚI LÊN SÓNG!")
                for it in news:
                    sid = it["series_id"]
                    known_ids.add(sid)
                    today_badge = " [HÔM NAY]" if it.get("today") else ""
                    rel_str = f" ({it['release_relative']})" if it.get("release_relative") else ""
                    print(f"    ✨ 《{it['title']}》 ({it['episode_cnt']} tập){today_badge}")
                    print(f"       • ID: {sid} | Điểm: {it['score'] or 'N/A'} | Ra mắt: {it.get('release_time', '')}{rel_str}")
                    print(f"       • Thể loại: {it['category']}")
                    if it.get("vid"):
                        print(f"       • Vid tập 1: {it['vid']} -> Tải ngay: python 1.py {it['vid']}")
                    print(f"       • Xem chi tiết: python 2.py {sid}\n")
            else:
                print(" Không có phim mới.")
            round_idx += 1
        except KeyboardInterrupt:
            print("\n  [Đã dừng chế độ giám sát]")
            break
        except Exception as e:
            print(f" [Lỗi]: {e}")
            round_idx += 1


# ─── CLI Handler ─────────────────────────────────────────────────────────────

def main() -> None:
    # CLI argument parsing
    genre = "short_play"
    only_today = True
    limit = 60
    output_json = "--json" in sys.argv
    output_ids = "--ids" in sys.argv
    no_cache = "--no-cache" in sys.argv or "--refresh" in sys.argv
    is_watch = "--watch" in sys.argv or "--monitor" in sys.argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    if "--all" in sys.argv:
        only_today = False

    # Extract --genre / -g
    for idx, arg in enumerate(sys.argv):
        if arg in ("--genre", "-g") and idx + 1 < len(sys.argv):
            genre = sys.argv[idx + 1]
            break
        elif arg.startswith("--genre="):
            genre = arg.split("=", 1)[1]
            break

    genre = GENRE_ALIASES.get(genre.lower(), genre)
    if genre not in GENRES:
        print(f"[Error] Thể loại không hợp lệ: '{genre}'. Chỉ hỗ trợ: {list(GENRES.keys())}")
        sys.exit(1)

    # Extract --limit / -n
    for idx, arg in enumerate(sys.argv):
        if arg in ("--limit", "-n") and idx + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[idx + 1])
            except ValueError:
                pass
            break
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                pass
            break

    # Watch mode handler
    if is_watch:
        interval = 60
        for idx, arg in enumerate(sys.argv):
            if arg in ("--watch", "--monitor") and idx + 1 < len(sys.argv):
                try:
                    interval = int(sys.argv[idx + 1])
                except ValueError:
                    pass
                break
        monitor_updates(genre=genre, interval=interval)
        sys.exit(0)

    genre_name = GENRE_NAMES.get(genre, genre)
    filter_label = "Hôm nay lên sóng (今日上新)" if (only_today and genre == "short_play") else "Mới nhất gần đây (最新上架)"

    if not output_json and not output_ids:
        print(f"🔍 Đang tải danh sách: {genre_name} · {filter_label} (tối đa {limit} phim) ...\n")

    try:
        results = latest(genre=genre, only_today=only_today, max_items=limit, refresh=no_cache)
    except Exception as e:
        if output_json:
            print(json.dumps({"error": str(e), "genre": genre}, ensure_ascii=False, indent=2))
        else:
            print(f"[Error] Lấy danh sách phim mới thất bại: {e}")
        sys.exit(1)

    # Smart fallback: Nếu tìm chế độ 'Hôm nay' (only_today=True) nhưng máy chủ chưa đánh dấu tag '今日上新',
    # tự động fallback sang lấy danh sách mới nhất gần đây để người dùng không bị rỗng kết quả.
    is_fallback = False
    if not results and only_today and genre == "short_play":
        if not output_json and not output_ids:
            sys.stderr.write(
                "💡 Lưu ý: Máy chủ hiện chưa cập nhật nhãn '今日上新' (Hôm nay lên sóng).\n"
                "➡️ Đang tự động chuyển sang lấy danh sách phim mới nhất vừa lên kệ (最新上架)...\n\n"
            )
            sys.stderr.flush()
        results = latest(genre=genre, only_today=False, max_items=limit, refresh=no_cache)
        is_fallback = True

    # Mode 1: JSON output
    if output_json:
        payload = {
            "genre": genre,
            "genre_name": genre_name,
            "only_today": only_today if not is_fallback else False,
            "total": len(results),
            "items": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(0)

    # Mode 2: Only series IDs output (for shell piping to 2.py)
    if output_ids:
        for r in results:
            print(r["series_id"])
        sys.exit(0)

    # Mode 3: Human-readable formatted console display
    count = len(results)
    title_banner = f"🎬 PHIM MỚI LÊN SÓNG: {genre_name.upper()}"
    if is_fallback:
        title_banner += " [MỚI NHẤT VỪA LÊN KỆ]"
    elif only_today and genre == "short_play":
        title_banner += " [HÔM NAY LÊN SÓNG]"
    else:
        title_banner += " [7 NGÀY GẦN NHẤT / MỚI NHẤT]"

    print("=" * 75)
    print(f"  {title_banner} (Tìm thấy {count} phim)")
    print("=" * 75)

    if not results:
        print(f"  Hiện tại không có phim mới nào phù hợp trong danh mục {genre_name}.")
        print("  💡 Bạn có thể thử lại với tham số: python 4.py --all")
        print("=" * 75)
        sys.exit(0)

    for idx, item in enumerate(results, start=1):
        idx_str = f"[{idx:>2}]"
        title = item["title"]
        ep_str = f"({item['episode_cnt']} tập)"
        badge = " [Hôm Nay]" if item.get("today") else (" [Mới]" if item.get("premiere") else "")
        print(f"  {idx_str} 🎬 《{title}》 {ep_str}{badge}")
        print(f"       • Series ID : {item['series_id']}")

        if item.get("release_time"):
            rel_str = f" ({item['release_relative']})" if item.get("release_relative") else ""
            print(f"       • Ra mắt    : {item['release_time']}{rel_str} [Giờ Bắc Kinh: {item.get('release_time_bj', '')}]")

        meta_parts = []
        if item.get("play_cnt"):
            meta_parts.append(f"Lượt xem: {item['play_cnt_str']}")
        if item.get("score"):
            meta_parts.append(f"Điểm: {item['score']}")
        if item.get("category"):
            meta_parts.append(f"Thể loại: {item['category']}")
        if item.get("premiere"):
            meta_parts.append(f"Trạng thái: {item['premiere']}")
        if meta_parts:
            print(f"       • Thông tin : {' | '.join(meta_parts)}")

        if item.get("copyright"):
            print(f"       • Bản quyền : {item['copyright']}")

        if item.get("vid"):
            dur = f" ({item['duration_str']})" if item.get("duration_str") else ""
            print(f"       • Tập 1 vid : {item['vid']}{dur}")

        if item.get("intro"):
            clean_intro = item["intro"].replace("\n", " ").strip()
            if len(clean_intro) > 85:
                clean_intro = clean_intro[:85] + "..."
            print(f"       • Tóm tắt   : {clean_intro}")

        if idx < count:
            print("  " + "-" * 71)

    print("=" * 75)
    first_sid = results[0]["series_id"]
    first_vid = results[0].get("vid")
    first_title = results[0].get("title", "")
    print("\n💡 Gợi ý tiếp theo:")
    print(f"   • Xem chi tiết và toàn bộ tập : python 2.py {first_sid}")
    if first_vid:
        print(f"   • Tải video tập 1 ngay         : python 1.py {first_vid}")
    if first_title:
        print(f"   • Tìm phim tương tự           : python 3.py \"{first_title[:10]}\"")
    print(f"   • Tự động theo dõi phim mới   : python 4.py --watch 60")
    print()


if __name__ == "__main__":
    main()
