"""
短剧下载工具 · 开源版

启动：python app.py
API：
  - GET/POST /api/search
  - GET/POST /hg?vid=VIDEO_ID
"""
import importlib
import json
import os
import re
import sys
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs

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

import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, send_file, Response
from config import DEFAULT_SETTINGS


APP_DIR = Path(__file__).resolve().parent
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}
ITEMS_PER_PAGE = 50


SOURCE_ALIASES = {
    "红果短剧": "hongguo",
    "红果短剧官网": "hongguo",
    "hongguo": "hongguo",
}

# 红果短剧官网分类参数，来自公开分类页。
HONGGUO_CATEGORY_MAP = {
    "现代": "background=cate_757",
    "都市": "background=cate_1",
    "古代": "background=cate_758",
    "乡村": "background=cate_11",
    "年代": "background=cate_79",
    "架空": "background=cate_452",
    "职场": "background=cate_127",
    "民国": "background=cate_390",
    "宫廷": "background=cate_1153",
    "校园": "background=cate_4",
    "现言": "topic=cate_1021",
    "女性成长": "topic=cate_1048",
    "脑洞": "topic=cate_262",
    "奇幻": "topic=cate_1020",
    "玄幻": "topic=cate_1019",
    "古言": "topic=cate_439",
    "战神": "topic=cate_1038",
    "宫斗": "topic=cate_246",
    "仙侠": "topic=cate_1013",
    "权谋": "topic=cate_1047",
    "悬疑": "topic=cate_165",
    "喜剧": "topic=cate_303",
    "科幻": "topic=cate_1092",
    "打脸虐渣": "setting=cate_1051",
    "大女主": "setting=cate_760",
    "大男主": "setting=cate_1207",
    "马甲": "setting=cate_266",
    "重生": "setting=cate_36",
    "穿越": "setting=cate_37",
    "系统": "setting=cate_19",
    "先婚后爱": "setting=cate_265",
    "神豪": "setting=cate_20",
    "破镜重圆": "setting=cate_475",
    "豪门": "setting=cate_936",
    "甜宠": "setting=cate_96",
    "娱乐圈": "setting=cate_43",
    "赘婿": "setting=cate_1044",
    "赘婿逆袭": "setting=cate_1044",
    "神医": "setting=cate_26",
    "男频": "gender=1",
    "女频": "gender=0",
    "最新": "sort_type=2",
    "最热": "sort_type=1",
}


def load_dotenv_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from .env without adding a dependency."""
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


load_dotenv_file(APP_DIR / ".env")

if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR / "_internal"))
else:
    RESOURCE_DIR = APP_DIR

LIUSHEN_DIR = RESOURCE_DIR / "liushen"
STATIC_DIR = RESOURCE_DIR / "static"

for path in (RESOURCE_DIR, LIUSHEN_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

parser_module = importlib.import_module("1")
handle_video_request = parser_module.handle_video_request


# ───────────────────────── 配置 ─────────────────────────

def get_config_path() -> Path:
    return parser_module.get_runtime_base_dir() / "config.json"


def mask_value(value: str) -> str:
    value = str(value or "")
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def read_local_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = read_local_config()
    device_id = os.getenv("DUANJU_DEVICE_ID") or str(cfg.get("device_id", ""))
    install_id = os.getenv("DUANJU_INSTALL_ID") or str(cfg.get("install_id", ""))
    platform = os.getenv("DUANJU_PLATFORM") or str(cfg.get("platform", "android"))

    custom_endpoint = str(cfg.get("custom_endpoint") or os.getenv("CUSTOM_API_ENDPOINT") or DEFAULT_SETTINGS.get("customApiEndpoint", ""))
    raw_api_key = str(cfg.get("custom_api_key") or os.getenv("CUSTOM_API_KEY") or DEFAULT_SETTINGS.get("customApiKey", ""))
    custom_model = str(cfg.get("custom_model") or os.getenv("CUSTOM_MODEL") or DEFAULT_SETTINGS.get("customModel", "gemini-lite"))

    return jsonify({
        "configured": bool(device_id and install_id),
        "device_id_masked": mask_value(device_id),
        "install_id_masked": mask_value(install_id),
        "platform": platform or "android",
        "custom_endpoint": custom_endpoint,
        "custom_api_key_masked": mask_value(raw_api_key),
        "custom_api_key_set": bool(raw_api_key),
        "custom_model": custom_model,
        "config_path": str(get_config_path()),
    })


@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.get_json(silent=True) or {}
    cfg = read_local_config()

    device_id = str(data.get("device_id", "")).strip()
    install_id = str(data.get("install_id", "")).strip()
    platform = str(data.get("platform", "")).strip()

    if device_id:
        cfg["device_id"] = device_id
    if install_id:
        cfg["install_id"] = install_id
    if platform:
        cfg["platform"] = platform

    if "custom_endpoint" in data:
        cfg["custom_endpoint"] = str(data.get("custom_endpoint", "")).strip()
    if "custom_api_key" in data:
        new_key = str(data.get("custom_api_key", "")).strip()
        if new_key and not ("***" in new_key):
            cfg["custom_api_key"] = new_key
    if "custom_model" in data:
        cfg["custom_model"] = str(data.get("custom_model", "")).strip()

    path = get_config_path()
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return jsonify({
        "ok": True,
        "config_path": str(path),
        "custom_endpoint": cfg.get("custom_endpoint", ""),
        "custom_model": cfg.get("custom_model", ""),
        "custom_api_key_masked": mask_value(str(cfg.get("custom_api_key", ""))),
    })


# ───────────────────────── 搜索源 ─────────────────────────

def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def estimate_duration_from_episodes(episode_text: str) -> str:
    """Return a conservative series duration estimate when official duration is absent."""
    m = re.search(r"(\d+)", episode_text or "")
    if not m:
        return ""
    episodes = int(m.group(1))
    # Most short-drama episodes are about 1-2 minutes; use a neutral range.
    return f"\u7ea6 {episodes}-{episodes * 2} \u5206\u949f\uff08\u6309\u6bcf\u96c6 1-2 \u5206\u949f\u4f30\u7b97\uff09"


def public_unknown_time() -> str:
    return "\u5b98\u7f51\u672a\u516c\u5f00"


def fetch_text(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def dedupe_items(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = item.get("drama_id") or item.get("source_url") or item.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def matches_keywords(item: dict, keyword: str, category_filter: str) -> bool:
    keys = []
    if keyword:
        keys.extend([x.strip() for x in re.split(r"[,，\s]+", keyword) if x.strip()])
    if category_filter:
        keys.extend([x.strip() for x in re.split(r"[,，]+", category_filter) if x.strip()])
    if not keys:
        return True
    haystack = " ".join(str(item.get(k, "")) for k in ("title", "category", "author", "desc", "source"))
    return any(k in haystack for k in keys)


def apply_page(items: list[dict], page: int) -> list[dict]:
    page = max(int(page or 1), 1)
    start = (page - 1) * ITEMS_PER_PAGE
    return items[start:start + ITEMS_PER_PAGE]


def first_category_query(keyword: str, category_filter: str) -> str:
    text = f"{keyword},{category_filter}"
    for name, query in HONGGUO_CATEGORY_MAP.items():
        if name and name in text:
            return query
    return "sort_type=1"


def parse_hongguo_cards(html_text: str, base_url: str = "https://hongguoduanju.com") -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    for a in soup.select('a[href*="/detail?series_id="]'):
        href = a.get("href") or ""
        parsed = urlparse(urljoin(base_url, href))
        series_id = (parse_qs(parsed.query).get("series_id") or [""])[0]
        if not series_id:
            continue

        texts = [clean_text(x) for x in a.stripped_strings if clean_text(x)]
        text_join = " ".join(texts)
        episode = next((x for x in texts if re.search(r"全\d+集", x)), "")

        title = ""
        title_node = a.select_one('[class*="title"]')
        if title_node:
            title = clean_text(title_node.get_text(" "))
        if not title:
            # fallback: 去掉“全xx集”和标签后，取第一段不像标签的文本
            candidates = [x for x in texts if not re.fullmatch(r"全\d+集", x)]
            title = candidates[0] if candidates else text_join[:40]

        tag_texts = []
        for node in a.select('[class*="tag-text"], [class*="tag"] span'):
            t = clean_text(node.get_text(" "))
            if t and t not in tag_texts and len(t) <= 12:
                tag_texts.append(t)
        if not tag_texts:
            # fallback: anchor 文本中除标题/集数外的短词作为标签
            tag_texts = [x for x in texts if x not in {title, episode} and 1 <= len(x) <= 12][:5]

        # 提取封面图片 URL
        cover_node = a.select_one("img")
        source_node = a.select_one("source")
        cover = ""
        if cover_node:
            cover = cover_node.get("src") or cover_node.get("data-src") or ""
        if not cover and source_node:
            cover = source_node.get("srcset") or source_node.get("src") or ""
        if cover:
            cover = urljoin(base_url, cover)

        items.append({
            "author": "红果短剧",
            "title": title,
            "drama_id": series_id,
            "episodes": episode,
            "duration": estimate_duration_from_episodes(episode),
            "online_time": public_unknown_time(),
            "category": " / ".join(tag_texts),
            "cover": cover,
            "source": "红果短剧官网",
            "source_url": urljoin(base_url, href),
            "downloadable": True,
            "desc": text_join,
            "duration_source": "estimated_from_episode_count" if episode else "not_public",
            "online_time_source": "not_public",
        })
    return dedupe_items(items)


def search_hongguo(keyword: str, page: int, category_filter: str) -> list[dict]:
    urls = []
    query = first_category_query(keyword, category_filter)
    urls.append(f"https://hongguoduanju.com/category?{query}")
    urls.append("https://hongguoduanju.com/category?sort_type=1")
    urls.append("https://hongguoduanju.com/")

    items = []
    for url in urls:
        try:
            items.extend(parse_hongguo_cards(fetch_text(url), "https://hongguoduanju.com"))
        except Exception as exc:
            print(f"[search][hongguo] failed url={url} error={exc}")
    items = dedupe_items(items)
    filtered = [x for x in items if matches_keywords(x, keyword, category_filter)]
    return apply_page(filtered or items, page)


def search_short_drama(keyword: str, page: int, source: str = "红果短剧", category_filter: str = "") -> list[dict]:
    return search_hongguo(keyword, page, category_filter)


@app.route("/api/search", methods=["GET", "POST"])
def api_search():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        keyword = str(payload.get("keyword", "")).strip()
        page = int(payload.get("page") or 1)
        source = str(payload.get("source", "红果短剧")).strip()
        category_filter = str(payload.get("category_filter", "")).strip()
    else:
        keyword = request.args.get("keyword", "").strip()
        page = int(request.args.get("page") or 1)
        source = request.args.get("source", "红果短剧").strip()
        category_filter = request.args.get("category_filter", "").strip()

    try:
        items = search_short_drama(keyword, page, source, category_filter)
        message = f"搜索完成：红果短剧，第 {page} 页，返回 {len(items)} 条。"
        return jsonify({"items": items, "page": page, "source": "红果短剧", "message": message})
    except Exception as exc:
        return jsonify({"items": [], "page": page, "source": "红果短剧", "message": f"搜索失败：{exc}"}), 500


_episodes_module = None


def get_episodes_module():
    """Dynamically load 2.py module."""
    global _episodes_module
    if _episodes_module is not None:
        return _episodes_module

    script_path = APP_DIR / "2.py"
    if not script_path.exists():
        return None

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("episodes_service", script_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _episodes_module = mod
            return _episodes_module
    except Exception as exc:
        print(f"[app] Failed to load 2.py: {exc}")
    return None


def get_hongguo_detail(series_id: str, use_cache: bool = True) -> dict:
    """Fetch and parse series detail using 2.py (Hongguo App API with Liushen signing)."""
    series_id = str(series_id or "").strip()
    # If user provided a URL, extract series_id
    if "series_id=" in series_id:
        m = re.search(r"series_id=([0-9A-Za-z_-]+)", series_id)
        if m:
            series_id = m.group(1)

    if not series_id:
        raise ValueError("series_id is required")

    # 1. Primary method: Use 2.py
    mod = get_episodes_module()
    if mod and hasattr(mod, "get_episodes"):
        try:
            meta, eps = mod.get_episodes(series_id, use_cache=use_cache)
            vid_list = [str(e.get("vid") or "") for e in eps if e.get("vid")]
            episodes = []
            for ep in eps:
                idx = ep.get("index") or len(episodes) + 1
                episodes.append({
                    "episode_num": idx,
                    "index": idx,
                    "title": ep.get("title") or f"Tập {idx}",
                    "vid": str(ep.get("vid") or ""),
                    "series_id": str(meta.get("series_id") or series_id),
                    "series_name": meta.get("title") or "",
                    "duration": ep.get("duration") or 0,
                    "duration_str": ep.get("duration_str") or "00:00",
                    "cover": ep.get("cover") or "",
                    "digged_count": ep.get("digged_count") or 0,
                    "comment_count": ep.get("comment_count") or 0,
                })

            return {
                "series_id": str(meta.get("series_id") or series_id),
                "series_name": meta.get("title") or "",
                "title": meta.get("title") or "",
                "series_cover": meta.get("cover") or "",
                "cover": meta.get("cover") or "",
                "episode_cnt": meta.get("episode_cnt") or len(vid_list),
                "series_intro": meta.get("intro") or "",
                "intro": meta.get("intro") or "",
                "tags": meta.get("category") or [],
                "category": meta.get("category") or [],
                "status": meta.get("status") or "完结",
                "status_vn": meta.get("status_vn") or "Trọn bộ",
                "play_cnt": meta.get("play_cnt") or 0,
                "play_cnt_str": meta.get("play_cnt_str") or "0",
                "followed_cnt": meta.get("followed_cnt") or 0,
                "celebrities": meta.get("celebrities") or [],
                "vid_list": vid_list,
                "episodes": episodes,
                "source": "app_api_2.py",
                "source_url": f"https://hongguoduanju.com/detail?series_id={meta.get('series_id') or series_id}",
            }
        except Exception as exc:
            print(f"[app] Fetch detail via 2.py failed for series_id={series_id}: {exc}. Fallback to bs4 scraper.")

    # 2. Fallback method: web scraper (in case 2.py cannot run or device keys missing)
    url = f"https://hongguoduanju.com/detail?series_id={series_id}"
    html_text = fetch_text(url)
    soup = BeautifulSoup(html_text, "html.parser")

    script = soup.find("script", string=lambda x: x and "_ROUTER_DATA" in x)
    if not script or not script.string:
        raise ValueError("Cannot find _ROUTER_DATA in page")

    text = script.string
    start = text.find("_ROUTER_DATA =")
    if start == -1:
        raise ValueError("_ROUTER_DATA assignment not found")

    json_str = text[start + len("_ROUTER_DATA ="):].strip()
    data, _ = json.JSONDecoder().raw_decode(json_str)

    detail_page = data.get("loaderData", {}).get("detail_page", {})
    series = detail_page.get("seriesDetail")
    if not series:
        raise ValueError(f"Series detail not found for series_id={series_id}")

    vid_list = [str(v) for v in (series.get("vid_list") or [])]
    episodes = []
    for idx, vid in enumerate(vid_list, 1):
        episodes.append({
            "episode_num": idx,
            "index": idx,
            "title": f"Tập {idx}",
            "vid": str(vid),
            "series_id": str(series.get("series_id") or series_id),
            "series_name": series.get("series_name") or "",
            "duration": 0,
            "duration_str": "00:00",
            "cover": "",
            "digged_count": 0,
            "comment_count": 0,
        })

    return {
        "series_id": str(series.get("series_id") or series_id),
        "series_name": series.get("series_name") or "",
        "title": series.get("series_name") or "",
        "series_cover": series.get("series_cover") or "",
        "cover": series.get("series_cover") or "",
        "episode_cnt": series.get("episode_cnt") or len(vid_list),
        "series_intro": series.get("series_intro") or "",
        "intro": series.get("series_intro") or "",
        "tags": series.get("tags") or [],
        "category": series.get("tags") or [],
        "status": "完结",
        "status_vn": "Trọn bộ",
        "play_cnt": 0,
        "play_cnt_str": "",
        "followed_cnt": 0,
        "celebrities": [],
        "vid_list": vid_list,
        "episodes": episodes,
        "source": "web_scraper",
        "source_url": url,
    }


@app.route("/api/detail", methods=["GET", "POST"])
def api_detail():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        series_id = str(payload.get("series_id") or payload.get("id") or "").strip()
        no_cache = bool(payload.get("no_cache") or payload.get("refresh"))
    else:
        series_id = str(request.args.get("series_id") or request.args.get("id") or "").strip()
        no_cache = request.args.get("no_cache") == "1" or request.args.get("refresh") == "1"

    if not series_id:
        return jsonify({"error": "Missing series_id parameter"}), 400

    try:
        data = get_hongguo_detail(series_id, use_cache=not no_cache)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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


@app.route("/api/proxy_image", methods=["GET"])
def proxy_image():
    """Proxy image request to bypass browser CORS, referrer policy, or ISP restrictions."""
    img_url = request.args.get("url", "").strip()
    if not img_url:
        return Response(status=400)

    img_url = normalize_cover_url(img_url)
    try:
        resp = requests.get(img_url, headers=HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return Response(resp.content, content_type=content_type, headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ───────────────────────── Phim Mới Hôm Nay (4.py) ─────────────────────────

_latest_module = None


def get_latest_module():
    """Dynamically load 4.py module."""
    global _latest_module
    if _latest_module is not None:
        return _latest_module

    script_path = APP_DIR / "4.py"
    if not script_path.exists():
        return None

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("latest_service", script_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _latest_module = mod
            return _latest_module
    except Exception as exc:
        print(f"[app] Failed to load 4.py: {exc}")
    return None


@app.route("/api/latest", methods=["GET", "POST"])
def api_latest():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.args

    genre = str(payload.get("genre", "short_play")).strip() or "short_play"
    only_today_raw = payload.get("only_today")
    if only_today_raw is None:
        only_today = True if genre == "short_play" else False
    else:
        only_today = str(only_today_raw).lower() in ("1", "true", "yes", "on")

    limit = int(payload.get("limit") or 60)
    limit = max(1, min(limit, 200))
    page = max(1, int(payload.get("page") or 1))
    refresh = str(payload.get("refresh", "")).lower() in ("1", "true", "yes")

    # Filter parameters
    sort = str(payload.get("sort", "")).strip() or None
    gender = str(payload.get("gender", "")).strip() or None

    def _parse_list(val):
        if not val:
            return None
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return [x.strip() for x in str(val).split(",") if x.strip()]

    online_time = _parse_list(payload.get("online_time"))
    theme = _parse_list(payload.get("theme") or payload.get("category_dim_theme"))
    setting = _parse_list(payload.get("setting") or payload.get("role") or payload.get("category_dim_role"))
    background = _parse_list(payload.get("background") or payload.get("epoch") or payload.get("category_dim_epoch"))
    keyword = str(payload.get("keyword", "")).strip().lower()

    try:
        mod = get_latest_module()
        if not mod or not hasattr(mod, "latest"):
            return jsonify({"ok": False, "error": "Module 4.py not found or invalid", "items": []}), 500

        fetch_max = max(limit * page + 5, limit * 2)
        results = mod.latest(
            genre=genre,
            only_today=only_today,
            max_items=fetch_max,
            refresh=refresh,
            online_time=online_time,
            sort=sort,
            gender=gender,
            theme=theme,
            role=setting,
            epoch=background,
        )

        is_fallback = False
        # Fallback if only_today is True but server returned 0 items
        if not results and only_today and genre == "short_play" and not (theme or setting or background or online_time):
            results = mod.latest(
                genre=genre,
                only_today=False,
                max_items=fetch_max,
                refresh=refresh,
                sort=sort,
                gender=gender,
            )
            is_fallback = True

        # Optional keyword filtering
        if keyword and results:
            results = [
                it for it in results
                if keyword in it.get("title", "").lower()
                or keyword in it.get("category", "").lower()
                or keyword in it.get("category_vn", "").lower()
                or keyword in it.get("intro", "").lower()
                or keyword in it.get("series_id", "")
            ]

        # Pagination slicing
        start_idx = (page - 1) * limit
        if start_idx >= len(results):
            paged_items = []
        else:
            paged_items = results[start_idx:start_idx + limit]

        has_more = bool(len(results) > (start_idx + len(paged_items)) and len(paged_items) == limit)

        genre_name = getattr(mod, "GENRE_NAMES", {}).get(genre, genre)
        return jsonify({
            "ok": True,
            "genre": genre,
            "genre_name": genre_name,
            "only_today": only_today if not is_fallback else False,
            "is_fallback": is_fallback,
            "total": len(results),
            "page": page,
            "limit": limit,
            "count": len(paged_items),
            "has_more": has_more,
            "items": paged_items,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "items": []}), 500


@app.route("/api/latest/filters", methods=["GET"])
def api_latest_filters():
    genre = str(request.args.get("genre", "short_play")).strip() or "short_play"
    refresh = request.args.get("refresh", "").lower() in ("1", "true")
    try:
        mod = get_latest_module()
        if not mod or not hasattr(mod, "get_category_filters"):
            return jsonify({"ok": False, "error": "Module 4.py not found", "filters": []}), 500
        filter_rows = mod.get_category_filters(genre=genre, refresh=refresh)
        return jsonify({
            "ok": True,
            "genre": genre,
            "filters": filter_rows,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "filters": []}), 500


# ───────────────────────── 页面和下载 ─────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/src/<path:filename>")
def generated_video(filename):
    src_dir = parser_module.get_runtime_base_dir() / "src"
    return send_from_directory(str(src_dir), filename)


@app.route("/hg", methods=["GET", "POST"])
def hg():
    video_id = request.args.get("vid") or (request.form.get("vid") if request.method == "POST" else None)
    if not video_id:
        return jsonify({"error": "Missing vid parameter"}), 400

    series_id = request.args.get("series_id") or (request.form.get("series_id") if request.method == "POST" else None)
    episode = request.args.get("episode") or request.args.get("ep") or (request.form.get("episode") if request.method == "POST" else None)
    custom_filename = request.args.get("filename") or (request.form.get("filename") if request.method == "POST" else None)
    save_dir = request.args.get("save_dir") or (request.form.get("save_dir") if request.method == "POST" else None)

    try:
        result = handle_video_request(
            video_id.strip(),
            request,
            max_retries=3,
            series_id=series_id,
            episode=episode,
            filename=custom_filename,
            save_dir=save_dir,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/batch_resolve", methods=["POST"])
def api_batch_resolve():
    payload = request.get_json(silent=True) or {}
    video_ids = payload.get("video_ids") or []
    if not isinstance(video_ids, list) or not video_ids:
        return jsonify({"error": "video_ids list is required"}), 400

    batch_size = int(payload.get("batch_size") or 20)
    try:
        res = parser_module.resolve_batch_video_models(video_ids, batch_size=batch_size)
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/choose_directory", methods=["GET", "POST"])
def api_choose_directory():
    """Open native OS folder picker dialog to select download folder."""
    try:
        if sys.platform != "win32" and not os.getenv("DISPLAY"):
            return jsonify({
                "ok": False,
                "headless": True,
                "message": "Môi trường Linux/Google Colab không có giao diện cửa sổ. Bạn có thể nhập trực tiếp đường dẫn thư mục (ví dụ: /content/drive/MyDrive/ShortDrama hoặc ./src)."
            })
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Chọn thư mục lưu video tải về")
        root.destroy()
        if selected:
            norm_path = str(Path(selected).resolve())
            return jsonify({"ok": True, "path": norm_path})
        return jsonify({"ok": False, "cancelled": True})
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"Không thể mở hộp thoại chọn thư mục: {exc}. Bạn có thể nhập trực tiếp đường dẫn."
        })


@app.route("/api/open_directory", methods=["POST"])
def api_open_directory():
    """Open directory in OS file manager (Windows Explorer / Finder / Linux)."""
    payload = request.get_json(silent=True) or {}
    dir_path = str(payload.get("path", "")).strip()
    if not dir_path:
        dir_path = str(parser_module.get_download_base_dir())
    try:
        path_obj = Path(dir_path).resolve()
        path_obj.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path_obj))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path_obj)])
        else:
            if os.getenv("DISPLAY"):
                subprocess.run(["xdg-open", str(path_obj)], check=False)
        return jsonify({"ok": True, "path": str(path_obj)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/video_file", methods=["GET"])
def api_video_file():
    """Stream locally saved MP4 file for preview player."""
    file_path = request.args.get("path", "").strip()
    if not file_path:
        return jsonify({"error": "Missing path parameter"}), 400
    p = Path(file_path).resolve()
    if not p.exists() or not p.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(str(p), mimetype="video/mp4")


# ───────────────────────── Ghép Video (Video Merger API) ─────────────────────────

try:
    import video_service
except ImportError:
    video_service = importlib.import_module("video_service")


@app.route("/api/merge/list_videos", methods=["GET"])
def api_merge_list_videos():
    """List local downloaded video files with metadata for merge dialog."""
    dir_query = request.args.get("dir", "").strip()
    if dir_query:
        target_dir = Path(dir_query).resolve()
    else:
        target_dir = parser_module.get_download_base_dir()

    if not target_dir.exists() or not target_dir.is_dir():
        target_dir.mkdir(parents=True, exist_ok=True)

    videos = video_service.list_directory_videos(target_dir)
    return jsonify({
        "ok": True,
        "dir": str(target_dir),
        "videos": videos,
        "count": len(videos),
    })


@app.route("/api/merge/detect_gpu", methods=["GET"])
def api_merge_detect_gpu():
    """Detect and return available GPU hardware acceleration encoders."""
    info = video_service.detect_available_gpu_encoders()
    return jsonify({"ok": True, **info})


def _parse_merge_options(payload: dict, default_full_flow: bool = False) -> dict:
    """Parse merge & full-flow pipeline options with explicit mode detection."""
    full_flow_val = payload.get("full_flow")
    pipeline_mode = str(payload.get("pipeline_mode", "")).strip().lower()

    if full_flow_val is True or pipeline_mode == "full_flow" or default_full_flow:
        upload_to_storage = payload.get("upload_to_storage", True) in (True, "true", "True", 1, "1", "on")
        generate_subtitles = True
        translate_subtitles = True
        dubbing = True
    elif full_flow_val is False or pipeline_mode == "merge_only":
        upload_to_storage = payload.get("upload_to_storage", False) in (True, "true", "True", 1, "1", "on")
        generate_subtitles = False
        translate_subtitles = False
        dubbing = False
    else:
        upload_to_storage = payload.get("upload_to_storage", True) in (True, "true", "True", 1, "1", "on")
        generate_subtitles = payload.get("generate_subtitles", True) in (True, "true", "True", 1, "1", "on")
        translate_subtitles = payload.get("translate_subtitles", True) in (True, "true", "True", 1, "1", "on")
        dubbing = payload.get("dubbing", True) in (True, "true", "True", 1, "1", "on")

    cfg = read_local_config()
    default_endpoint = cfg.get("custom_endpoint") or os.getenv("CUSTOM_API_ENDPOINT") or DEFAULT_SETTINGS.get("customApiEndpoint", "")
    default_api_key = cfg.get("custom_api_key") or os.getenv("CUSTOM_API_KEY") or DEFAULT_SETTINGS.get("customApiKey", "")
    default_model = cfg.get("custom_model") or os.getenv("CUSTOM_MODEL") or DEFAULT_SETTINGS.get("customModel", "gemini-lite")

    req_endpoint = str(payload.get("custom_endpoint") or payload.get("endpoint") or "").strip()
    req_api_key = str(payload.get("custom_api_key") or payload.get("api_key") or payload.get("apikey") or "").strip()
    req_model = str(payload.get("translate_model") or payload.get("model") or "").strip()

    custom_endpoint = req_endpoint or default_endpoint
    custom_api_key = req_api_key or default_api_key
    translate_model = req_model or default_model

    return {
        "concurrency": int(payload.get("concurrency") or payload.get("threads") or payload.get("workers") or 5),
        "cut_end_seconds": float(payload.get("cut_end_seconds") or 0.0),
        "mirror": payload.get("mirror") in (True, "true", "True", "1", 1, "on"),
        "quality": str(payload.get("quality", "original")).strip(),
        "resolution": str(payload.get("resolution", "original")).strip(),
        "custom_resolution": str(payload.get("custom_resolution", "")).strip(),
        "fps": str(payload.get("fps", "original")).strip(),
        "bitrate": str(payload.get("bitrate", "auto")).strip(),
        "custom_bitrate": str(payload.get("custom_bitrate", "")).strip(),
        "codec": str(payload.get("codec", "h264")).strip(),
        "gpu": str(payload.get("gpu", "nvenc")).strip(),
        "color_filter": str(payload.get("color_filter", "none")).strip(),
        "custom_color_filter": str(payload.get("custom_color_filter", "")).strip(),
        "audio_effect": str(payload.get("audio_effect", "none")).strip(),
        "custom_audio_effect": str(payload.get("custom_audio_effect", "")).strip(),
        "format": str(payload.get("format", "mp4")).strip(),
        "output_dir": str(payload.get("output_dir", "")).strip(),
        "output_name": str(payload.get("output_name", "")).strip(),
        "upload_to_storage": upload_to_storage,
        "generate_subtitles": generate_subtitles,
        "translate_subtitles": translate_subtitles,
        "translate_prompt": str(payload.get("translate_prompt") or payload.get("prompt") or "ai_tong_hop_thong_minh").strip(),
        "custom_endpoint": custom_endpoint,
        "custom_api_key": custom_api_key,
        "translate_model": translate_model,
        "storage_token": str(payload.get("storage_token", "")).strip(),
        "capcut_tdid": str(payload.get("capcut_tdid", "")).strip(),
        "source_lang": str(payload.get("source_lang", "auto")).strip(),
        "dubbing": dubbing,
        "tts_voice": str(payload.get("tts_voice") or payload.get("voice") or "Ngọc Huyền").strip(),
        "tts_batch_size": int(payload.get("tts_batch_size") or payload.get("batch_size") or 64),
        "max_speedup": float(payload.get("max_speedup") or payload.get("tts_speedup") or 1.35),
        "tolerance": float(payload.get("tolerance") or payload.get("tts_tolerance") or 0.3),
        "video_speed": float(payload.get("video_speed") or payload.get("speed") or 0.9),
        "bg_volume": float(payload.get("bg_volume") if payload.get("bg_volume") is not None else payload.get("dub_bg_volume", 0.1)),
        "dub_volume": float(payload.get("dub_volume") if payload.get("dub_volume") is not None else payload.get("dubbing_volume", 3.0)),
        "dub_pitch_down_pct": float(payload.get("dub_pitch_down_pct") if payload.get("dub_pitch_down_pct") is not None else 5.0),
        "enable_periodic_mute": payload.get("enable_periodic_mute", True) in (True, "true", "True", 1, "1", "on"),
    }


@app.route("/api/merge/start", methods=["POST"])
def api_merge_start():
    """Start background video merge task."""
    payload = request.get_json(silent=True) or {}
    files = payload.get("files") or []
    if not isinstance(files, list) or not files:
        return jsonify({"error": "Danh sách tệp video 'files' không được để trống"}), 400

    options = _parse_merge_options(payload)
    try:
        task_id = video_service.start_merge_task(files, options)
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/merge/start_online", methods=["POST"])
def api_merge_start_online():
    """Start background online video merge task without local episode files."""
    payload = request.get_json(silent=True) or {}
    video_ids = payload.get("video_ids") or payload.get("vids") or []
    if not isinstance(video_ids, list) or not video_ids:
        return jsonify({"error": "Danh sách mã video 'video_ids' không được để trống"}), 400

    options = _parse_merge_options(payload)
    try:
        task_id = video_service.start_online_merge_task(video_ids, options)
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/merge/start_full_flow", methods=["POST"])
def api_merge_start_full_flow():
    """
    Dedicated endpoint for Full Flow pipeline:
    Ghép video -> CapCut ASR -> Dịch phụ đề -> Lồng tiếng VieNeu-TTS -> Render Video Dubbing -> Upload storage.to
    Supports either online video_ids or local video files.
    """
    payload = request.get_json(silent=True) or {}
    video_ids = payload.get("video_ids") or payload.get("vids") or []
    files = payload.get("files") or []

    if not video_ids and not files:
        return jsonify({"error": "Cần cung cấp danh sách mã video 'video_ids' hoặc tệp video 'files'"}), 400

    options = _parse_merge_options(payload, default_full_flow=True)
    try:
        if video_ids:
            task_id = video_service.start_online_merge_task(video_ids, options)
            mode = "online"
        else:
            task_id = video_service.start_merge_task(files, options)
            mode = "offline"
        return jsonify({"ok": True, "task_id": task_id, "mode": mode, "pipeline": "full_flow"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500



@app.route("/api/batch_download", methods=["POST"])
def api_batch_download():
    """Download multiple episodes concurrently to a folder."""
    payload = request.get_json(silent=True) or {}
    episodes = payload.get("episodes") or []
    series_id = str(payload.get("series_id", "")).strip()
    series_name = str(payload.get("series_name") or f"Phim_{series_id}").strip()
    save_dir = str(payload.get("save_dir", "")).strip()
    concurrency = int(payload.get("concurrency") or payload.get("threads") or payload.get("workers") or 5)

    if not episodes or not isinstance(episodes, list):
        return jsonify({"error": "Danh sách tập 'episodes' không được để trống"}), 400

    target_dir = Path(save_dir).resolve() if save_dir else parser_module.get_download_base_dir()
    clean_name = re.sub(r'[\\/*?:"<>|]', '_', series_name).strip()
    series_folder = target_dir / clean_name

    try:
        downloaded, success_cnt, fail_cnt = video_service.download_series_episodes_concurrent(
            episodes=episodes,
            series_id=series_id,
            save_folder=series_folder,
            clean_name=clean_name,
            concurrency=concurrency,
        )
        return jsonify({
            "ok": True,
            "success_count": success_cnt,
            "fail_count": fail_cnt,
            "downloaded_files": [str(f) for f in downloaded],
            "save_folder": str(series_folder),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500




@app.route("/api/merge/status", methods=["GET"])
def api_merge_status():
    """Get status of an ongoing or completed merge task."""
    task_id = request.args.get("task_id", "").strip()
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400

    status_data = video_service.get_task_status(task_id)
    if not status_data:
        return jsonify({"error": "Task not found"}), 404

    # Remove non-serializable objects (such as proc)
    clean_data = {k: v for k, v in status_data.items() if k != "proc"}
    return jsonify(clean_data)


@app.route("/api/merge/events", methods=["GET"])
def api_merge_events():
    """Stream real-time merge task progress via Server-Sent Events (SSE)."""
    task_id = request.args.get("task_id", "").strip()
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400

    def event_stream():
        last_progress = None
        last_message = None
        last_speed = None
        last_status = None

        while True:
            status_data = video_service.get_task_status(task_id)
            if not status_data:
                err_payload = json.dumps({
                    "status": "error",
                    "error": "Task not found",
                    "message": "Không tìm thấy tác vụ ghép video."
                }, ensure_ascii=False)
                yield f"data: {err_payload}\n\n"
                break

            clean_data = {k: v for k, v in status_data.items() if k != "proc"}
            cur_status = clean_data.get("status")
            cur_progress = clean_data.get("progress")
            cur_message = clean_data.get("message")
            cur_speed = clean_data.get("speed")

            # Push immediately if state changed or on the very first frame
            if (
                cur_status != last_status
                or cur_progress != last_progress
                or cur_message != last_message
                or cur_speed != last_speed
                or last_status is None
            ):
                last_status = cur_status
                last_progress = cur_progress
                last_message = cur_message
                last_speed = cur_speed
                yield f"data: {json.dumps(clean_data, ensure_ascii=False)}\n\n"

            if cur_status in {"done", "error", "cancelled"}:
                break

            time.sleep(0.1)

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(event_stream(), headers=headers)


@app.route("/api/merge/cancel", methods=["POST"])
def api_merge_cancel():
    """Cancel a running merge task."""
    payload = request.get_json(silent=True) or {}
    task_id = str(payload.get("task_id", "")).strip()
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400

    ok = video_service.cancel_merge_task(task_id)
    return jsonify({"ok": ok, "task_id": task_id})


@app.route("/api/storage_links", methods=["GET"])
def api_storage_links():
    """Return storage.to links log history. Supports json (default) or plain text format."""
    format_type = request.args.get("format", "json").strip().lower()
    raw_content = video_service.get_storage_links_file_content()
    logs = video_service.get_storage_upload_logs()

    if format_type in ("text", "txt", "plain"):
        return Response(
            raw_content or "Chưa có link storage.to nào được ghi nhận.\n",
            mimetype="text/plain; charset=utf-8",
        )

    return jsonify({
        "success": True,
        "count": len(logs),
        "links": logs,
        "raw_text": raw_content,
    })


def _is_colab_or_headless() -> bool:
    """Detect if running in Google Colab, Kaggle, or a headless server."""
    if "google.colab" in sys.modules or os.getenv("COLAB_GPU") is not None or os.getenv("COLAB_RELEASE_TAG") is not None:
        return True
    if sys.platform != "win32" and not os.getenv("DISPLAY"):
        return True
    return False


def _should_open_browser() -> bool:
    if _is_colab_or_headless():
        return False
    return os.getenv("OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no", "off"}


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0" if _is_colab_or_headless() else "127.0.0.1")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("APP_PORT", "5000"))
    url = f"http://127.0.0.1:{port}"

    if _should_open_browser():
        def open_browser():
            import time
            time.sleep(1)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=open_browser, daemon=True).start()

    print(f"短剧下载工具 开源版: http://127.0.0.1:{port} (host={host})")
    print(f"☁️ Xem link Storage.to: http://127.0.0.1:{port}/api/storage_links?format=text")
    app.run(host=host, port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
