# -*- coding: utf-8 -*-
"""
Python script to fetch episodes & metadata for a short drama series (series_id).
Derived from:
  - config.example.json (device & session credentials)
  - hongguo.py (episodes data parsing & CLI design)
  - safeguards.py (caching, throttling, and anti-risk controls)
  - 1.py (Liushen signing & device management)

Usage:
  python 2.py <series_id>                  # List all episodes with metadata
  python 2.py <series_id> 1-10             # List episodes 1 through 10
  python 2.py <series_id> --json           # Output full series & episodes in JSON format
  python 2.py <series_id> --vids           # Output only video IDs (vids), one per line
  python 2.py <series_id> --no-cache       # Force fetch without using local cache

Example:
  python 2.py 7638207474180312089
  python 2.py 7638207474180312089 1-5
  python 2.py 7638207474180312089 --vids
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlsplit, parse_qsl, quote

import requests

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

# Ensure liushen package is in sys.path
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
except Exception as _e:
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
    """Load KEY=VALUE pairs from .env without extra dependencies."""
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

DEFAULT_API_HOST = "api5-normal-sinfonlineb.fqnovel.com"

VIDEO_DETAIL_URL_TEMPLATE = (
    "https://{api_host}/novel/player/multi_video_detail/v1/"
    "?iid={install_id}&device_id={device_id}&ac=wifi&channel=update_64&aid=8662"
    "&app_name=novelread&version_code=71332&version_name=7.1.3.32"
    "&device_platform=android&os=android&ssmix=a&device_type=25053RT47C"
    "&device_brand=Redmi&language=zh&os_api=36&os_version=16"
    "&manifest_version_code=71332&resolution=1280*2772&dpi=520"
    "&update_version_code=71332&host_abi=arm64-v8a&dragon_device_type=phone"
    "&pv_player=71332&compliance_status=0&need_personal_recommend=1"
    "&player_so_load=1&is_android_pad_screen=0"
)

IMAGE_SHRINK = (
    "W3siaW1hZ2VfdHlwZSI6MywiaW1hZ2Vfd2lkdGgiOjkwMCwic2hyaW5rX3R5cGUiOjN9LHsiaW1h\n"
    "Z2VfdHlwZSI6NCwiaW1hZ2Vfd2lkdGgiOjU0LCJzaHJpbmtfdHlwZSI6NH1d\n"
)


def load_local_config() -> Dict[str, Any]:
    """Load config.json if available, or fall back to empty dict."""
    config_path = BASE_DIR / "config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[config] failed_to_read_config={config_path} error={exc}")
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
        or str(config.get("api_host") or DEFAULT_API_HOST)
    ).strip() or DEFAULT_API_HOST

    return {
        "device_id": device_id,
        "install_id": install_id,
        "platform": platform,
        "api_host": api_host,
    }


def build_liushen_device(device_keys: Dict[str, str]) -> Dict[str, str]:
    """Build the device signature payload for liushen."""
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


def sign_json_request_with_liushen(
    url: str,
    body_obj: Dict[str, Any],
    device_keys: Dict[str, str],
) -> Tuple[str, Dict[str, str], bytes]:
    """Sign JSON request with Liushen algorithm (matching 1.py)."""
    body_text = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    body_data = json.loads(body_text)
    body_bytes = body_text.encode("utf-8")

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

    # Optional session_headers from config.json (like cookie / x-tt-token)
    cfg = load_local_config()
    session_headers = cfg.get("session_headers", {})
    if isinstance(session_headers, dict):
        for k, v in session_headers.items():
            if v and str(k).lower() in ("cookie", "x-tt-token"):
                base_headers[k] = str(v)

    url_parts = urlsplit(url)
    base_url = f"{url_parts.scheme}://{url_parts.netloc}{url_parts.path}"
    params = dict(parse_qsl(url_parts.query, keep_blank_values=True))
    sign_headers, sign_url = core_sixgod(
        surl=base_url,
        params=params,
        data=body_data,
        devices=build_liushen_device(device_keys),
        header=base_headers,
        log=False,
    )
    return sign_url, sign_headers, body_bytes


def curl_request(
    url: str,
    headers: Dict[str, str],
    post_body: Optional[bytes] = None,
    timeout: int = 30,
) -> bytes:
    """Execute HTTP request with requests library."""
    if post_body is not None:
        resp = requests.post(url, headers=headers, data=post_body, timeout=timeout)
    else:
        resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


# ─── Data Parsing & Helpers ──────────────────────────────────────────────────

def format_duration(seconds: Any) -> str:
    """Convert duration in seconds to MM:SS string."""
    try:
        total_sec = int(round(float(seconds or 0)))
        if total_sec <= 0:
            return "00:00"
        hrs = total_sec // 3600
        mins = (total_sec % 3600) // 60
        secs = total_sec % 60
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"
    except Exception:
        return "00:00"


def format_count(count: Any) -> str:
    """Format large numbers (e.g. play_cnt, digged_count) to human-readable string."""
    try:
        n = int(count or 0)
        if n >= 100_000_000:
            return f"{n / 100_000_000:.1f}亿"
        if n >= 10_000:
            return f"{n / 10_000:.1f}万"
        return str(n)
    except Exception:
        return str(count or 0)


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


def build_episodes_body(series_id: str) -> Dict[str, Any]:
    """Construct multi_video_detail request body payload."""
    return {
        "biz_param": {
            "detail_page_version": 0,
            "disable_digg_stat": False,
            "disable_video_relate_book": False,
            "image_shrink_datas_str": IMAGE_SHRINK,
            "need_all_video_definition": False,
            "need_mp4_align": False,
            "screen_width_px": "900",
            "source": 7,
            "use_os_player": False,
            "use_server_dns": False,
        },
        "series_id": str(series_id).strip(),
    }


def parse_episode_detail(sid: str, vd: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Parse raw video_data structure into clean (meta, episodes) pair."""
    vd = vd or {}
    raw_video_list = vd.get("video_list") or []

    eps = []
    for e in raw_video_list:
        if not isinstance(e, dict):
            continue
        vid_index = e.get("vid_index")
        vid_str = str(e.get("vid") or "").strip()
        duration_val = e.get("duration") or 0
        eps.append({
            "index": int(vid_index) if vid_index is not None else len(eps) + 1,
            "vid": vid_str,
            "title": str(e.get("title") or f"第{vid_index}集").strip(),
            "duration": duration_val,
            "duration_str": format_duration(duration_val),
            "cover": normalize_cover_url(e.get("episode_cover") or e.get("cover") or ""),
            "comment_count": int(e.get("comment_count") or 0),
            "digged_count": int(e.get("digged_count") or 0),
        })

    eps.sort(key=lambda x: x["index"] or 0)

    first_ep_cover = next((e.get("cover") for e in eps if e.get("cover")), "")
    series_cover = normalize_cover_url(vd.get("series_cover") or first_ep_cover or "")

    # Extract cast / celebrities
    celebs = []
    for c in (vd.get("celebrities") or []):
        if isinstance(c, dict):
            celebs.append({
                "name": str(c.get("nickname") or "").strip(),
                "role": str(c.get("role_name") or "").strip(),
                "avatar": str(c.get("avatar") or "").strip(),
                "intro": str(c.get("intro") or "").strip()[:100],
            })

    # Extract categories
    cat_schema = str(vd.get("category_schema") or "")
    categories = re.findall(r'"name":"([^"]+)"', cat_schema)

    # Status
    status_code = vd.get("series_status")
    status_str = "完结" if status_code == 1 else "连载中"
    status_vn = "Trọn bộ" if status_code == 1 else "Đang cập nhật"

    meta = {
        "series_id": str(sid),
        "title": str(vd.get("series_title") or sid).strip(),
        "intro": str(vd.get("series_intro") or "").strip(),
        "episode_cnt": int(vd.get("episode_cnt") or len(eps)),
        "status": status_str,
        "status_vn": status_vn,
        "play_cnt": int(vd.get("series_play_cnt") or 0),
        "play_cnt_str": format_count(vd.get("series_play_cnt") or 0),
        "followed_cnt": int(vd.get("followed_cnt") or 0),
        "create_time": int(vd.get("create_time") or 0),
        "cover": series_cover,
        "category": categories,
        "celebrities": celebs,
    }

    return meta, eps


# ─── Main Functions ──────────────────────────────────────────────────────────

def get_episodes(
    series_id: str,
    use_cache: bool = True,
    max_retries: int = 3,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Fetch all episodes and metadata for a given series_id using App API.
    
    Workflow:
      1. Check TTL cache (via safeguards.py).
      2. Call App API `/novel/player/multi_video_detail/v1/` signed with Liushen.
      3. Verify anti-risk checks (throttle, check_response).
      4. Store result in cache and return (meta, eps).
    """
    sid = str(series_id).strip()
    if "series_id=" in sid:
        m = re.search(r"series_id=([0-9A-Za-z_-]+)", sid)
        if m:
            sid = m.group(1)

    if not sid:
        raise ValueError("series_id is required")

    # 1. Check cache
    cache_key_str = ""
    if use_cache and _HAS_SAFEGUARDS and SG:
        cache_key_str = SG.cache_key("episodes", sid)
        cached = SG.cache_get(cache_key_str)
        if cached is not None:
            return cached

    # 2. Call App API
    device_keys = get_device_keys()
    if not device_keys.get("device_id") or not device_keys.get("install_id"):
        raise RuntimeError("Missing DUANJU_DEVICE_ID or DUANJU_INSTALL_ID in .env/config.json")

    base_url = VIDEO_DETAIL_URL_TEMPLATE.format(
        api_host=device_keys.get("api_host", DEFAULT_API_HOST),
        install_id=quote(device_keys["install_id"], safe=""),
        device_id=quote(device_keys["device_id"], safe=""),
    )

    body_payload = build_episodes_body(sid)
    last_err = None

    for attempt in range(max_retries):
        try:
            if _HAS_SAFEGUARDS and SG:
                SG.throttle.wait()

            signed_url, headers, post_body = sign_json_request_with_liushen(
                base_url, body_payload, device_keys
            )
            raw_resp = curl_request(signed_url, headers, post_body, timeout=30)
            data = json.loads(raw_resp)

            if _HAS_SAFEGUARDS and SG:
                SG.check_response(data)

            sid_entry = data.get("data", {}).get(sid, {})
            video_data = sid_entry.get("video_data") or {}

            if not video_data and isinstance(data.get("data"), dict):
                # In case key is numeric or slightly different
                for k, v in data["data"].items():
                    if isinstance(v, dict) and "video_data" in v:
                        video_data = v["video_data"]
                        break

            if not video_data:
                raise ValueError(f"No video_data returned for series_id={sid}")

            meta, eps = parse_episode_detail(sid, video_data)

            if use_cache and _HAS_SAFEGUARDS and SG and cache_key_str:
                SG.cache_set(cache_key_str, (meta, eps), ttl=21600)  # Cache 6 hours

            return meta, eps

        except RiskControlError as rce:
            last_err = rce
            wait_sec = 2 ** attempt + 1
            time.sleep(wait_sec)
        except Exception as ex:
            last_err = ex
            time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(f"Failed to fetch episodes for series_id={sid} after {max_retries} attempts: {last_err}")


def get_series_detail(series_id: str, use_cache: bool = True) -> Dict[str, Any]:
    """Return consolidated dictionary with meta & episode list."""
    meta, eps = get_episodes(series_id, use_cache=use_cache)
    return {
        "series_id": meta["series_id"],
        "title": meta["title"],
        "intro": meta["intro"],
        "episode_cnt": meta["episode_cnt"],
        "status": meta["status"],
        "status_vn": meta.get("status_vn", "Trọn bộ"),
        "play_cnt": meta["play_cnt"],
        "play_cnt_str": meta["play_cnt_str"],
        "followed_cnt": meta.get("followed_cnt", 0),
        "cover": meta["cover"],
        "category": meta["category"],
        "celebrities": meta["celebrities"],
        "episodes": eps,
    }


def get_episode_vids(series_id: str) -> List[str]:
    """Return only the ordered list of video_ids (vids) for the series."""
    _, eps = get_episodes(series_id)
    return [e["vid"] for e in eps if e.get("vid")]


def get_episodes_batch(
    series_ids: List[str],
    batch_size: int = 20,
) -> Dict[str, Any]:
    """
    Batch retrieve series details using multi_video_detail comma-separated series_id.
    """
    cleaned_ids = [str(s).strip() for s in series_ids if str(s).strip()]
    if not cleaned_ids:
        return {"success": {}, "failed": {}}

    batch_size = max(1, min(int(batch_size or 20), 20))
    results = {}
    failed = {}

    for i in range(0, len(cleaned_ids), batch_size):
        chunk = cleaned_ids[i : i + batch_size]
        try:
            device_keys = get_device_keys()
            base_url = VIDEO_DETAIL_URL_TEMPLATE.format(
                api_host=device_keys.get("api_host", DEFAULT_API_HOST),
                install_id=quote(device_keys["install_id"], safe=""),
                device_id=quote(device_keys["device_id"], safe=""),
            )
            body_payload = build_episodes_body(",".join(chunk))
            signed_url, headers, post_body = sign_json_request_with_liushen(
                base_url, body_payload, device_keys
            )
            raw_resp = curl_request(signed_url, headers, post_body, timeout=30)
            data = json.loads(raw_resp)

            if _HAS_SAFEGUARDS and SG:
                SG.check_response(data)

            data_map = data.get("data", {}) or {}
            for sid in chunk:
                vd = (data_map.get(sid) or {}).get("video_data") or {}
                if vd:
                    meta, eps = parse_episode_detail(sid, vd)
                    results[sid] = {"meta": meta, "episodes": eps}
                else:
                    failed[sid] = "No video_data returned"
        except Exception as exc:
            for sid in chunk:
                failed[sid] = str(exc)

    return {"success": results, "failed": failed}


# ─── CLI Handler ─────────────────────────────────────────────────────────────

def parse_range(range_str: str, max_count: int) -> List[int]:
    """Parse a range string like '1-10' or '1,3,5' into 1-based index set."""
    range_str = range_str.strip().lower()
    if range_str in ("all", "*", ""):
        return list(range(1, max_count + 1))

    indices = set()
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            parts = part.split("-", 1)
            try:
                start = int(parts[0])
                end = int(parts[1])
                for idx in range(start, end + 1):
                    if 1 <= idx <= max_count:
                        indices.add(idx)
            except ValueError:
                pass
        else:
            try:
                idx = int(part)
                if 1 <= idx <= max_count:
                    indices.add(idx)
            except ValueError:
                pass

    return sorted(list(indices))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    # Arguments parsing
    series_id = sys.argv[1].strip()
    if series_id in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    output_json = "--json" in sys.argv
    output_vids = "--vids" in sys.argv
    no_cache = "--no-cache" in sys.argv

    # Detect optional episode range parameter
    range_arg = None
    for arg in sys.argv[2:]:
        if not arg.startswith("--"):
            range_arg = arg
            break

    if not output_json and not output_vids:
        print(f"Fetching episodes for series_id={series_id} ...\n")

    try:
        meta, eps = get_episodes(series_id, use_cache=not no_cache)
    except Exception as e:
        if output_json:
            print(json.dumps({"error": str(e), "series_id": series_id}, ensure_ascii=False, indent=2))
        else:
            print(f"[Error] {e}")
        sys.exit(1)

    # Filter episodes if range provided
    total_eps = len(eps)
    if range_arg:
        allowed_indices = set(parse_range(range_arg, total_eps))
        eps = [e for e in eps if e["index"] in allowed_indices]

    # Mode 1: JSON output
    if output_json:
        result = {
            "meta": meta,
            "total_episodes": total_eps,
            "returned_episodes": len(eps),
            "episodes": eps,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # Mode 2: Only VIDs output (for script piping)
    if output_vids:
        for e in eps:
            if e.get("vid"):
                print(e["vid"])
        sys.exit(0)

    # Mode 3: Human-readable formatted console display
    cats = " / ".join(meta["category"]) if meta["category"] else "N/A"
    print("=" * 70)
    print(f"  🎬 《{meta['title']}》 ({meta['status']} · {meta['episode_cnt']} tập)")
    print("=" * 70)
    print(f"  • Series ID  : {meta['series_id']}")
    print(f"  • Thể loại   : {cats}")
    print(f"  • Lượt xem   : {meta['play_cnt_str']} ({meta['play_cnt']})")
    print(f"  • Theo dõi   : {format_count(meta['followed_cnt'])}")
    if meta.get("cover"):
        print(f"  • Ảnh bìa    : {meta['cover']}")

    if meta.get("celebrities"):
        cast_list = [f"{c['name']}({c['role']})" if c['role'] else c['name'] for c in meta["celebrities"][:5]]
        print(f"  • Diễn viên  : {' · '.join(cast_list)}")

    if meta.get("intro"):
        intro_clean = meta["intro"].replace("\n", " ").strip()
        if len(intro_clean) > 90:
            intro_clean = intro_clean[:90] + "..."
        print(f"  • Giới thiệu : {intro_clean}")

    print("-" * 70)
    print(f"  Danh sách tập ({len(eps)}/{total_eps} tập):")
    print("-" * 70)

    for e in eps:
        idx_str = f"[{e['index']:>3}]"
        vid_str = f"vid={e['vid']}"
        dur_str = f"({e['duration_str']})"
        likes_str = f"👍 {format_count(e['digged_count'])}" if e['digged_count'] else ""
        title_snippet = e['title'].replace("\n", " ").strip()
        if len(title_snippet) > 40:
            title_snippet = title_snippet[:40] + "..."
        print(f"  {idx_str}  {vid_str:<26}  {dur_str:<8}  {likes_str:<10}  {title_snippet}")

    print("=" * 70)
    if eps:
        first_vid = eps[0]['vid']
        print(f"\n💡 Gợi ý tải tập 1:")
        print(f"   python 1.py {first_vid}\n")


if __name__ == "__main__":
    main()
