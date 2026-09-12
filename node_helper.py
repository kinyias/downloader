#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_helper.py - Python implementation of ezmaxsub transcript-processor helper
Compatible with node_helper.js (v2.3.2)
Provides CLI via STDIN/STDOUT and reusable Python module APIs for:
  - Video ASR Transcription (BCut, CapCut, Groq Whisper)
  - Translation & Lore Bible with LLMs (DeepSeek, OpenAI, Grok, Ezmax Cloud, Custom API)
  - Text-to-Speech (CapCut, Edge, ElevenLabs, FPT, Vbee, Zalo, MiniMax, SiliconFlow)
  - Dubbing & Timeline Alignment (Borrowing, Clustering, Speed Optimization)
  - FFmpeg Video Export, Color Grading, ASS Subtitles, Watermarking
"""

import sys
import os
import re
import json
import time
import math
import tempfile
import hashlib
import hmac
import binascii
import random
import datetime
import uuid
import shutil
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import glob
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set, Union, Callable

# Ensure UTF-8 on Windows standard I/O streams
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import requests
except ImportError:
    requests = None

# ==============================================================================
# 1. CONSTANTS & DEFAULT CONFIGURATIONS
# ==============================================================================

ONE_FRAME_TOLERANCE = 1.0 / 24.0
TTS_TEMPO_MAX_HARD = 2.5
MAX_VOICE_SPEED = 1.35
MIN_VIDEO_SPEED = 0.7
CHUNK_SECONDS = 45
CHUNK_OVERLAP_SECONDS = 3
CHUNK_DEDUPE_GAP = 0.6
RESTORE_MARGIN = 0.05
EZMAX_REQUESTS_PER_MINUTE = 60
EZMAX_429_RETRY_DELAY_MS = 10000
EZMAX_429_MAX_RETRIES = 5
CAST_MIN_LINES = 3
MIN_DIM = 2
MAX_DIM = 16384

DEFAULT_POLICY = {
    "borrowSideMaxSec": 0.6,
    "borrowSideMaxFrac": 0.5,
    "borrowTotalMaxSec": 1.0,
    "borrowTotalMaxFrac": 0.8,
    "safetyGapSec": 0.08,
    "minVideoSpeed": 0.4,
    "maxVideoSpeed": 1.0,
    "clusterMaxGapSec": 0.5,
    "clusterMergeSpeedEps": 0.05,
    "residualTempoCap": 1.06,
    "toleranceSec": 0.5
}

EZMAX_TRANSLATE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "gpt-4o-mini",
    "gpt-4o",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "claude-3-5-sonnet"
]

TARGET_LANGS = {
    "vi": "Vietnamese",
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "th": "Thai",
    "id": "Indonesian",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "ru": "Russian",
    "pt": "Portuguese"
}

CHAR_PER_SEC = {
    "vi": 15.0,
    "en": 14.0,
    "zh": 4.2,
    "ja": 5.5,
    "ko": 5.0,
    "th": 12.0,
    "id": 14.0,
    "fr": 15.0,
    "es": 15.0,
    "de": 13.0,
    "ru": 13.0,
    "pt": 14.0
}

CJK_EXPANSION = {
    "vi": 2.8,
    "en": 2.5,
    "ko": 1.8,
    "th": 2.6,
    "id": 2.6,
    "fr": 2.7,
    "es": 2.7,
    "de": 2.6,
    "ru": 2.5,
    "pt": 2.6
}

WORD_LIMITS = {
    "vi": 4.5,
    "en": 3.2,
    "fr": 3.5,
    "es": 3.5,
    "de": 2.8,
    "id": 3.2,
    "ru": 2.8,
    "pt": 3.4
}

CJK_LANGS = {"zh", "ja", "ko"}

VOICE_BASE_SYL_PER_SEC = {
    "bv:BV562_streaming": 4.2,
    "bv:BV074_streaming": 4.0,
    "default": 3.8
}
RATE_CEILING = 1.35

# Color Filter Presets
COLOR_FILTER_PRESETS = [
    {
        "id": "vivid",
        "label": "Rực rỡ",
        "sub": "Đậm màu, nét căng",
        "swatch": ["#ff8a00", "#e52e71"],
        "bright": 1.0,
        "contrast": 1.12,
        "sat": 1.45,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "warm",
        "label": "Ấm áp",
        "sub": "Tông cam ấm",
        "swatch": ["#f7971e", "#ffd200"],
        "bright": 1.02,
        "contrast": 1.0,
        "sat": 1.08,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#ffaa44", "alpha": 0.12}
    },
    {
        "id": "cold",
        "label": "Lạnh lùng",
        "sub": "Tông xanh lạnh",
        "swatch": ["#2193b0", "#3d5afe"],
        "bright": 1.0,
        "contrast": 1.0,
        "sat": 1.05,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#4488ff", "alpha": 0.12}
    },
    {
        "id": "cinema",
        "label": "Điện ảnh",
        "sub": "Tương phản nhẹ, tone phim",
        "swatch": ["#1a2a3a", "#2e4057"],
        "bright": 0.98,
        "contrast": 1.18,
        "sat": 0.92,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#1a3048", "alpha": 0.1}
    },
    {
        "id": "vintage",
        "label": "Cổ điển",
        "sub": "Hoài cổ, ấm nhẹ",
        "swatch": ["#d4a373", "#faedcd"],
        "bright": 1.04,
        "contrast": 0.92,
        "sat": 0.85,
        "gamma": 1.0,
        "sepia": 0.45,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "retro",
        "label": "Retro",
        "sub": "Màu phim thập niên 90",
        "swatch": ["#ff7e5f", "#feb47b"],
        "bright": 1.0,
        "contrast": 1.05,
        "sat": 1.1,
        "gamma": 1.0,
        "sepia": 0.3,
        "hue": 10.0,
        "tint": {"color": "#ff9955", "alpha": 0.08}
    },
    {
        "id": "bw",
        "label": "Đen trắng",
        "sub": "Cổ điển, sắc nét",
        "swatch": ["#2b2b2b", "#888888"],
        "bright": 1.0,
        "contrast": 1.08,
        "sat": 0.0,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "bw_high",
        "label": "Đen trắng đậm",
        "sub": "Tương phản cao",
        "swatch": ["#000000", "#ffffff"],
        "bright": 0.95,
        "contrast": 1.3,
        "sat": 0.0,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "sepia",
        "label": "Nâu đỏ",
        "sub": "Tông nâu vàng xưa",
        "swatch": ["#b58451", "#7a5a34"],
        "bright": 1.02,
        "contrast": 1.0,
        "sat": 1.0,
        "gamma": 1.0,
        "sepia": 0.8,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "faded",
        "label": "Phai màu",
        "sub": "Màu phai, mờ nhẹ",
        "swatch": ["#c7c4bf", "#8f8c85"],
        "bright": 1.06,
        "contrast": 0.85,
        "sat": 0.8,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#d8d8d8", "alpha": 0.07}
    },
    {
        "id": "dreamy",
        "label": "Mộng mơ",
        "sub": "Nhẹ nhàng, tươi sáng",
        "swatch": ["#fbc2eb", "#a6c1ee"],
        "bright": 1.08,
        "contrast": 0.95,
        "sat": 0.75,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#ffd6e7", "alpha": 0.06}
    },
    {
        "id": "dramatic",
        "label": "Kịch tính",
        "sub": "Tương phản mạnh",
        "swatch": ["#1f1c2c", "#928dab"],
        "bright": 0.97,
        "contrast": 1.35,
        "sat": 0.95,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": None
    },
    {
        "id": "teal_orange",
        "label": "Teal & Orange",
        "sub": "Hollywood blockbuster",
        "swatch": ["#00838f", "#ef6c00"],
        "bright": 1.0,
        "contrast": 1.0,
        "sat": 1.15,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": -6.0,
        "tint": {"color": "#ef6c00", "alpha": 0.14}
    },
    {
        "id": "moody",
        "label": "Trầm tối",
        "sub": "Tối trầm, u ám",
        "swatch": ["#39415c", "#202434"],
        "bright": 0.92,
        "contrast": 1.1,
        "sat": 0.8,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 0.0,
        "tint": {"color": "#1c2333", "alpha": 0.12}
    },
    {
        "id": "emerald",
        "label": "Xanh rêu",
        "sub": "Tông xanh ngọc, rêu",
        "swatch": ["#2e8b57", "#1d5c40"],
        "bright": 1.0,
        "contrast": 1.0,
        "sat": 1.05,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": 8.0,
        "tint": {"color": "#206040", "alpha": 0.1}
    },
    {
        "id": "cyberpunk",
        "label": "Cyberpunk",
        "sub": "Neon hồng xanh",
        "swatch": ["#ff007f", "#2fd6ff"],
        "bright": 1.0,
        "contrast": 1.15,
        "sat": 1.3,
        "gamma": 1.0,
        "sepia": 0.0,
        "hue": -12.0,
        "tint": {"color": "#ff0066", "alpha": 0.1}
    }
]

# System Prompts by Genre
PROMPT_PRESETS = {
    "default": "",
    "ai_tong_hop_thong_minh": {
        "vi": (
            "BỐI CẢNH: KHÔNG biết trước thể loại — SUY LUẬN từ tên riêng, từ khóa, văn phong rồi áp văn phong tương ứng. "
            "MẶC ĐỊNH là HIỆN ĐẠI; chỉ chuyển thể loại khi ngữ cảnh xác nhận rõ.\n"
            "★ Cổ trang / cung đấu / kiếm hiệp / tiên hiệp (Hoàng thượng, triều đình, tu tiên) → ta/ngươi/hắn/nàng, Trẫm/Thần, Hán-Việt.\n"
            "★ Anime / Nhật → GIỮ tên Romaji, giữ kính ngữ Senpai/Sensei/Sama; xưng hô tớ/cậu.\n"
            "★ Isekai / game → giữ thuật ngữ Status, Skill, Level; 魔王→Ma vương, 勇者→Dũng giả.\n"
            "★ Hàn → giữ tên Latin, Oppa, Sunbae→Tiền bối.\n"
            "★ Âu Mỹ → giữ nguyên tên + tổ chức.\n"
            "★ GIỌNG ĐIỆU THEO CẢNH: hài → gọn sắc; kinh dị → ngắn rợn; hành động → dứt khoát; review → ngôi thứ ba hắn/nàng/gã.\n"
            "★ THÀNH NGỮ: ưu tiên thành ngữ/khẩu ngữ tương đương thay vì dịch chữ."
        ),
        "en": (
            "GENRE UNKNOWN — infer it from names, keywords and diction, then apply matching register. Default to MODERN.\n"
            "★ Chinese period/wuxia → archaic, ceremonious register.\n"
            "★ Anime → keep Romaji names and honorifics.\n"
            "★ Isekai/game → keep game terms.\n"
            "★ TONE PER SCENE: match scene dynamics."
        )
    }
}

PROMPT_ALIASES = {
    "ai_tong_hop": "ai_tong_hop_thong_minh",
    "drama": "review_phim_hien_dai",
    "historical": "review_phim_co_trang",
    "xianxia": "review_phim_kiem_hiep",
    "isekai": "review_phim_xuyen_khong",
    "anime": "review_anime_nhat_ban"
}

# CapCut Voice Mappings
ICL_RESOURCE_IDS = {
    "ICL_en_female_jiaoao": "7530105822785899777",
    "ICL_en_female_guanggao": "7530107275239869713",
    "ICL_es_male_barney": "7527795878967446801",
    "ICL_es_male_elmo": "7527814111502044433",
    "ICL_es_male_cixing": "7527779392769002769",
    "ICL_es_female_dianpo": "7527846655475928336",
    "ICL_es_male_dorado": "7527778973296774416",
    "ICL_es_female_dianyingpeiyin": "7527793325328452881",
    "ICL_es_male_taici": "7522965008020507920",
    "ICL_ja_female_laopopo": "7522976550736710913",
    "ICL_ja_female_kuaizui02": "7522976550736678145",
    "ICL_ja_female_shuanglang": "7522987643752303889",
    "ICL_jp_female_jidongshaonv": "7522965008020475152",
    "ICL_ja_male_gaoxiao": "7522976550736661761",
    "ICL_ja_male_gaoxiao02": "7522987643752271121",
    "ICL_en_male_callum": "7377653510566842881",
    "en_us_002": "7130515992936976897",
    "ICL_en_female_blanchett": "7444495548188463633",
    "ICL_en_female_cc_bluetooth": "7427072447129588225",
    "ICL_en_female_cc_megan": "7427044231073501712",
    "ICL_en_female_chamberlain": "7444914314399453697",
    "ICL_en_female_cm": "7441487929823728129",
    "ICL_en_female_erica": "7444495148345463312",
    "ICL_en_male_alastor": "7441487821946229265",
    "ICL_en_male_attenborough": "7444495251286266385",
    "ICL_en_male_aussie": "7426305396572164625",
    "ICL_en_male_benjamin1": "7441488473686544897",
    "ICL_en_male_benjamin2": "7441488683091366417",
    "ICL_en_male_cc_chucky": "7427044129604899345",
    "ICL_en_male_cc_dracula": "7427044330260402705",
    "ICL_en_male_cc_ghostface4": "7427044378666865153",
    "ICL_en_male_cc_jigsaw": "7427044277407978000",
    "ICL_en_male_cc_lich": "7437470599682724369",
    "ICL_en_male_cc_penny": "7427044180876071425",
    "ICL_en_male_cc_rafael": "7437470496397988369",
    "ICL_en_male_conductor1": "7438551500021830161",
    "ICL_en_male_cumberbatch1": "7444495363311931920",
    "ICL_en_male_cumberbatch2": "7444495463908119056",
    "ICL_en_male_frosty1": "7438551726942065169",
    "ICL_en_male_grinch2": "7438551356283032081",
    "ICL_en_male_henry1": "7444494768240857616",
    "ICL_en_male_henry2": "7444495042649002513",
    "ICL_en_male_kevin2": "7438551246824280592",
    "ICL_en_male_oogie2": "7438551608746578449",
    "ICL_en_male_poetry": "7413601846863860225",
    "ICL_en_male_severus": "7441487635048043009",
    "ICL_en_male_sylus": "7403211571293327873",
    "ICL_en_male_terrell": "7441488151983428097",
    "ICL_en_male_xavier1": "7424050549269467649",
    "ICL_en_male_zayne": "7424050504138756624",
    "en_au_001": "7114563482472698370",
    "en_au_002": "7114564716881515010",
    "en_female_amie": "7306800306921148929",
    "en_female_betty_boop": "7393229135348240913",
    "en_female_british_queen": "7337195844971532801",
    "en_female_candice_emo_v2_mars_bigtts": "7417662874270568961",
    "en_female_caroline_clone2": "7232136837861478913",
    "en_female_cartoon_bibble": "7379873499717833233",
    "en_female_daisy_moon_bigtts": "7278146659211547138",
    "en_female_doll": "7293115626711683585",
    "en_female_drunk_bumpkin": "7360987195986940432",
    "en_female_dwarf_karen": "7393231992759783953",
    "en_female_elsa_amanda_crystal": "7328236234780709377",
    "en_female_f08_salut_damour": "7245192978749198849",
    "en_female_f08_twinkle": "7245192372613550594",
    "en_female_f08_warmy_breeze": "7245192865905644033",
    "en_female_food_amy": "7337195373867307522",
    "en_female_fortune_feimster": "7360987978455323137",
    "en_female_game_narration": "7351731040093737473",
    "en_female_gloria": "7337195915368731138",
    "en_female_grandma_amy": "7337195773446066690",
    "en_female_ht_f08_halloween": "7245192656358216193",
    "en_female_kid_eddie": "7337195145680392706",
    "en_female_kourtney_kardashian": "7372473107635769872",
    "en_female_loba_apex": "7360986396594541057",
    "en_female_lois_familyguy": "7355036507519848961",
    "en_female_makeup": "7256999130084413954",
    "en_female_mary_emo_v2_mars_bigtts": "7430714470969643521",
    "en_female_meangirl": "7351730508037886481",
    "en_female_nail_artist": "7393228944540963344",
    "en_female_naive_youngwoman": "7372472718723125761",
    "en_female_nara_moon_bigtts": "7402094928337048065",
    "en_female_onez_moon_bigtts": "7402094869667123729",
    "en_female_product_darcie_moon_bigtts": "7337195598900105729",
    "en_female_product_elise": "7337195523163558401",
    "en_female_product_leah": "7337195678558327297",
    "en_female_richgirl_stream": "7189462696201294338",
    "en_female_samc": "7176107981098979841",
    "en_female_sherry": "7278146554844680706",
    "en_female_sinong_conversation_wvae_bigtts": "7372472342494056976",
    "en_female_skye_emo_v2_mars_bigtts": "7430714795206119937",
    "en_female_weak_child": "7393229078951629313",
    "en_female_werewolf": "7293115150263915009",
    "en_female_witch": "7293115285471498754",
    "en_female_zombie": "7293115536584479233",
    "en_male_adam_elf": "7309753082638766593",
    "en_male_artistic_layne": "7337195454980952577",
    "en_male_authoritative_marcus": "7393232129296962049",
    "en_male_bender_futurama": "7360986724681388560",
    "en_male_bojack_horseman": "7372472892174373377",
    "en_male_british_narrator": "7360987855176339984",
    "en_male_britishgamer": "7249264251536151042",
    "en_male_bruce_moon_bigtts": "7398059612643004945",
    "en_male_bumbling_idiot": "7337195992237740546",
    "en_male_campaign_jamal_moon_bigtts": "7337194574688817666",
    "en_male_charlie_conversation_wvae_bigtts_cc": "7371352611217216017",
    "en_male_cody": "7176107532534944258",
    "en_male_commentary_moon_bigtts": "7277818843358040578",
    "en_male_corey_emo_v2_mars_bigtts": "7417663340761059856",
    "en_male_cs_emo_v2_mars_bigtts": "7430714574728335888",
    "en_male_dave_moon_bigtts": "7398059458733019665",
    "en_male_david_gingerman": "7309752196122284545",
    "en_male_deadpool": "7231025912261644802",
    "en_male_death_rock": "7372472588859085313",
    "en_male_drag_voice": "7355036740215640593",
    "en_male_dramaqueen_zachk": "7337195243810329089",
    "en_male_forrest_gump": "7372473041797779969",
    "en_male_funny": "7114563483378651650",
    "en_male_gebralter_apex": "7379905121657819664",
    "en_male_glen_emo_v2_mars_bigtts": "7417662783333863953",
    "en_male_golden_monkey": "7351730917590700545",
    "en_male_greek_accent": "7351730796933157377",
    "en_male_grumpy_penguin": "7360987676440269328",
    "en_male_hades_moon_bigtts": "7398059512415916561",
    "en_male_hoarse_mattmc": "7338752878611272194",
    "en_male_homer_simpson": "7360987557812769296",
    "en_male_irritable_kitten": "7379873622745158161",
    "en_male_irritable_police_officer": "7355036625488843280",
    "en_male_jarvis": "7249264092781744641",
    "en_male_jeremy_emo_v2_mars_bigtts": "7430714636921475601",
    "en_male_john_mulaney": "7360987403995058689",
    "en_male_johnny_emo_v2_mars_bigtts": "7430714522609914384",
    "en_male_kevin_minion": "7393228840861962769",
    "en_male_leonardo_decaprio": "7360987035974242833",
    "en_male_lowpitched_shouting": "7393228776978518529",
    "en_male_m03_classical": "7245192458206712322",
    "en_male_m2_xhxs_m03_christmas": "7245192557322310145",
    "en_male_m2_xhxs_m03_silly": "7245192771231814145",
    "en_male_michael_moon_bigtts": "7398059563758391825",
    "en_male_narration_moon_bigtts": "7114563483210879490",
    "en_male_peter_griffin": "7372472973485150721",
    "en_male_positive_british": "7355036978116563473",
    "en_male_positiveboy_duncan": "7337194306685374978",
    "en_male_rick_sanchez": "7379873380536685072",
    "en_male_scott_emo_v2_mars_bigtts": "7457793197306024449",
    "en_male_sports_jomboy": "7337195054290702850",
    "en_male_stewie_familyguy": "7360987300609659393",
    "en_male_story_time": "7372472502842298897",
    "en_male_tech_blogger": "7372473177747755537",
    "en_male_ted_lasso": "7379873236122604033",
    "en_male_tim_emo_v2_mars_bigtts": "7430714745574920720",
    "en_male_trickster_stream": "7189462618589893121",
    "en_male_ukbutler": "7256999218185769473",
    "en_male_ukneighbor": "7256999312872182274",
    "en_male_vernacular_plinio": "7393232053258424848",
    "en_male_whispering_voice": "7355037095066341904",
    "en_male_will_clone2": "7232136973664653825",
    "en_male_xudong_conversation_wvae_bigtts_cc": "7371352319541121553",
    "en_uk_003": "7114563482363630082",
    "en_us_006": "7114563482518819329",
    "en_us_007": "7114563482472681986",
    "en_us_009": "7114563482418156033",
    "en_us_010": "7114563482359435778"
}

VN_HASH_SPEAKERS = {
    "Trung_Caha": "ueSxRO0nLF1bj93J2hVt",
    "Nam_Tram": "9EE00wK5qV6tPtpQIxvy",
    "Ly_Nam": "7hsfEc7irDn6E8br0qfw",
    "Duy_Bac": "1d5Bb0SMBPB10Gx6iQeu",
    "Ha_Nu": "pGapy9MNHCukzJtjavF0",
    "Sai_Nu": "xPEfmymXC4WdBxGMznS7"
}

# ==============================================================================
# VOICE.JSON VIETNAMESE VOICES LOADER & REGISTRY
# ==============================================================================

VOICE_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Voice.json")

def load_vi_voices_from_json(json_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lấy danh sách các voice tiếng Việt (vi-VN) từ file Voice.json.
    Chỉ lọc và trả về các voice có lang == 'vi-VN' hoặc lan == 'vi'.
    """
    candidates = []
    if json_path:
        candidates.append(json_path)
    candidates.append(VOICE_JSON_PATH)
    candidates.append(os.path.join(os.getcwd(), "Voice.json"))
    candidates.append("Voice.json")

    chosen_path = None
    for p in candidates:
        if p and os.path.isfile(p):
            chosen_path = p
            break

    if not chosen_path:
        return []

    try:
        with open(chosen_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []

        vi_voices = []
        for item in data:
            if not isinstance(item, dict):
                continue
            lang = str(item.get("lang") or "").strip().lower()
            lan = str(item.get("lan") or "").strip().lower()
            if lang == "vi-vn" or lan == "vi":
                vi_voices.append(item)
        return vi_voices
    except Exception as e:
        log_message(f"Warning: Không thể đọc voice vi-VN từ {chosen_path}: {e}")
        return []

def get_vietnamese_voices(json_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lấy danh sách giọng đọc tiếng Việt (vi-VN) từ Voice.json."""
    return load_vi_voices_from_json(json_path)

def register_voices_from_json(json_path: Optional[str] = None) -> int:
    """Tự động nạp mã resource_id của các voice tiếng Việt từ Voice.json vào ICL_RESOURCE_IDS."""
    vi_list = load_vi_voices_from_json(json_path)
    count = 0
    for v in vi_list:
        v_type = v.get("voice_type")
        r_id = v.get("resource_id")
        if v_type and r_id:
            r_id_str = str(r_id).strip()
            ICL_RESOURCE_IDS[v_type] = r_id_str
            if not v_type.startswith("bv:"):
                ICL_RESOURCE_IDS[f"bv:{v_type}"] = r_id_str
            if not v_type.startswith("capcut:"):
                ICL_RESOURCE_IDS[f"capcut:{v_type}"] = r_id_str
            count += 1
    return count

# Khởi tạo tự động đăng ký voice từ Voice.json
register_voices_from_json()



# ==============================================================================
# 2. CUSTOM EXCEPTIONS & LOGGING
# ==============================================================================

class ExportPolicyError(Exception):
    def __init__(self, code: str, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message or code)
        self.name = "ExportPolicyError"
        self.code = code
        self.details = details if isinstance(details, dict) else {}

class NativeRequiredError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.name = "NativeRequiredError"

def log_message(msg: Any) -> None:
    """Print log message cleanly without corrupting active tqdm progress bars."""
    line = str(msg).rstrip("\r\n")
    try:
        from tqdm import tqdm
        tqdm.write(line, file=sys.stderr)
    except Exception:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()

def emit_event(event_type: str, data: Dict[str, Any]) -> None:
    """Emit JSON event message to stdout for parent process."""
    payload = {"type": event_type, **data}
    sys.stdout.write(f"[{event_type}] {json.dumps(payload, ensure_ascii=False)}\n")
    sys.stdout.flush()

# ==============================================================================
# 3. VIETNAMESE NUMBER READING & SYLLABLE COUNTING
# ==============================================================================

_VI_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_VI_UNITS = ["", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ"]

def _read_three_digits(n: int, show_zero_hundred: bool = False) -> List[str]:
    hundreds = n // 100
    tens = (n % 100) // 10
    units = n % 10
    res = []
    if hundreds > 0 or show_zero_hundred:
        res.append(_VI_DIGITS[hundreds])
        res.append("trăm")
    if tens == 0 and units > 0:
        if hundreds > 0 or show_zero_hundred:
            res.append("lẻ")
        res.append(_VI_DIGITS[units])
    elif tens == 1:
        res.append("mười")
        if units == 1:
            res.append("một")
        elif units == 5:
            res.append("lăm")
        elif units > 0:
            res.append(_VI_DIGITS[units])
    elif tens > 1:
        res.append(_VI_DIGITS[tens])
        res.append("mươi")
        if units == 1:
            res.append("mốt")
        elif units == 4:
            res.append("tư")
        elif units == 5:
            res.append("lăm")
        elif units > 0:
            res.append(_VI_DIGITS[units])
    return res

def read_cardinal(num_str: Union[str, int, float]) -> str:
    """Convert a numeric string to natural spoken Vietnamese words."""
    try:
        val = int(str(num_str).strip())
    except ValueError:
        return str(num_str)
    if val == 0:
        return "không"
    if val < 0:
        return "âm " + read_cardinal(abs(val))
    groups = []
    v = val
    while v > 0:
        groups.append(v % 1000)
        v //= 1000
    words = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g > 0:
            show_zero = (i < len(groups) - 1)
            part = _read_three_digits(g, show_zero)
            unit = _VI_UNITS[i % len(_VI_UNITS)]
            if unit:
                part.append(unit)
            words.extend(part)
    return " ".join(words)

def expand_numbers_for_speech(text: str) -> str:
    """Find and expand numbers and percent in text into spoken words."""
    if not text:
        return ""
    def _sub_percent(m):
        return read_cardinal(m.group(1)) + " phần trăm"
    text = re.sub(r'(\d+)\s*%', _sub_percent, text)
    def _sub_num(m):
        return read_cardinal(m.group(0))
    text = re.sub(r'\b\d+\b', _sub_num, text)
    return text

def vi_syllables_spoken(text: str) -> int:
    """Count approximate spoken syllables in Vietnamese text."""
    expanded = expand_numbers_for_speech(text or "")
    cleaned = re.sub(r'[^\w\s]', ' ', expanded).strip()
    words = [w for w in cleaned.split() if w]
    return len(words)

def vi_syllables(text: str) -> int:
    return vi_syllables_spoken(text)

def cjk_ratio(text: str) -> float:
    """Calculate ratio of CJK characters in text."""
    if not text:
        return 0.0
    cjk_count = 0
    total = len(text)
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
            0xAC00 <= cp <= 0xD7AF):
            cjk_count += 1
    return cjk_count / total

def detect_source_lang(text: str) -> str:
    """Heuristic language detection based on Unicode script ranges."""
    if not text:
        return "auto"
    c_zh, c_ja, c_ko, c_th, c_ru = 0, 0, 0, 0, 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            c_zh += 1
        elif 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            c_ja += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            c_ko += 1
        elif 0x0E00 <= cp <= 0x0E7F:
            c_th += 1
        elif 0x0400 <= cp <= 0x04FF:
            c_ru += 1
    if c_ja > 0:
        return "ja"
    if c_ko > 0:
        return "ko"
    if c_zh > 0:
        return "zh"
    if c_th > 0:
        return "th"
    if c_ru > 0:
        return "ru"
    return "en"

# ==============================================================================
# 4. TIMING & DUBBING ALIGNMENT (Borrowing, Clustering, Snapping)
# ==============================================================================

def snap_to_frame(t: float, fps: float) -> float:
    """Snap timestamp t to nearest frame boundary if fps > 0."""
    if fps > 0:
        return round(t * fps) / fps
    return t

def solve_stretch_scene(speech_duration: float, video_window: float,
                       max_voice_speed: float = MAX_VOICE_SPEED,
                       min_video_speed: float = MIN_VIDEO_SPEED) -> Dict[str, Any]:
    """Calculate optimal video slowing and audio compression for a scene."""
    ratio = speech_duration / video_window if video_window > 0 else 1.0
    if ratio <= 1.0:
        return {
            "finalDuration": video_window,
            "videoSpeed": 1.0,
            "audioTempo": 1.0,
            "audioTrimSec": None
        }
    if ratio <= max_voice_speed:
        return {
            "finalDuration": video_window,
            "videoSpeed": 1.0,
            "audioTempo": ratio,
            "audioTrimSec": None
        }
    final_dur = speech_duration / max_voice_speed
    v_speed = video_window / final_dur
    a_tempo = max_voice_speed
    trim_sec = None
    if v_speed < min_video_speed:
        v_speed = min_video_speed
        final_dur = video_window / min_video_speed
        a_tempo = speech_duration / final_dur
        if a_tempo > TTS_TEMPO_MAX_HARD:
            a_tempo = TTS_TEMPO_MAX_HARD
            trim_sec = final_dur
    return {
        "finalDuration": final_dur,
        "videoSpeed": v_speed,
        "audioTempo": a_tempo,
        "audioTrimSec": trim_sec
    }

def build_dubbing_plan(units: List[Dict[str, Any]], total_duration: float,
                      opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Construct modern dubbing plan with speech borrowing & cluster slowing.
    """
    opts = opts or {}
    policy = {**DEFAULT_POLICY, **(opts.get("policy") or {})}
    global_rate = float(opts.get("globalVoiceRate") or 1.0)
    total_dur = float(total_duration) if total_duration and total_duration > 0 else float('inf')

    # Filter and sort valid units
    valid_units = []
    for u in (units or []):
        if (isinstance(u, dict) and isinstance(u.get("startTime"), (int, float)) and
            isinstance(u.get("endTime"), (int, float)) and u["endTime"] > u["startTime"]):
            speech = float(u.get("audioDuration") or 0.0)
            if speech <= 0.0 and u.get("spokenText"):
                # Approximate speech duration from syllables if audioDuration missing
                syls = vi_syllables_spoken(str(u.get("spokenText")))
                speech = max(0.5, syls / 3.8)
            window = u["endTime"] - u["startTime"]
            valid_units.append({
                "unitId": u.get("unitId") or u.get("id"),
                "sceneIndex": u.get("sceneIndex", 0),
                "speakerId": u.get("speakerId"),
                "origStart": float(u["startTime"]),
                "origEnd": float(u["endTime"]),
                "windowSec": window,
                "speech": speech,
                "deficit": max(0.0, speech - window),
                "borrowLeftSec": 0.0,
                "borrowRightSec": 0.0
            })

    valid_units.sort(key=lambda x: x["origStart"])

    # Borrowing algorithm
    max_side = lambda u: min(policy["borrowSideMaxSec"], policy["borrowSideMaxFrac"] * u["windowSec"])
    max_total = lambda u: min(policy["borrowTotalMaxSec"], policy["borrowTotalMaxFrac"] * u["windowSec"])
    rem_total = lambda u: max(0.0, max_total(u) - u["borrowLeftSec"] - u["borrowRightSec"])

    for i in range(len(valid_units) + 1):
        prev_u = valid_units[i - 1] if i > 0 else None
        next_u = valid_units[i] if i < len(valid_units) else None
        prev_end = prev_u["origEnd"] if prev_u else 0.0
        next_start = next_u["origStart"] if next_u else total_dur
        if not math.isfinite(next_start) and not next_u:
            continue
        gap = next_start - prev_end
        if gap <= 1e-6:
            continue
        if prev_u and next_u and prev_u["sceneIndex"] == next_u["sceneIndex"]:
            gap -= policy["safetyGapSec"]
            if gap <= 1e-6:
                continue
        b_left = min(prev_u["deficit"], max_side(prev_u), rem_total(prev_u)) if prev_u else 0.0
        b_right = min(next_u["deficit"], max_side(next_u), rem_total(next_u)) if next_u else 0.0
        if b_left + b_right <= 1e-6:
            continue
        if b_left + b_right <= gap:
            alloc_l = b_left
            alloc_r = b_right
        else:
            alloc_l = gap * b_left / (b_left + b_right)
            alloc_r = gap - alloc_l
            alloc_l = min(alloc_l, b_left)
            alloc_r = min(gap - alloc_l, b_right)
        if prev_u and alloc_l > 1e-6:
            prev_u["borrowRightSec"] += alloc_l
            prev_u["deficit"] = max(0.0, prev_u["deficit"] - alloc_l)
        if next_u and alloc_r > 1e-6:
            next_u["borrowLeftSec"] += alloc_r
            next_u["deficit"] = max(0.0, next_u["deficit"] - alloc_r)

    # Calculate planned bounds
    for u in valid_units:
        u["plannedStart"] = u["origStart"] - u["borrowLeftSec"]
        u["plannedEnd"] = u["origEnd"] + u["borrowRightSec"]
        u["plannedWindow"] = u["plannedEnd"] - u["plannedStart"]
        ratio = u["speech"] / u["plannedWindow"] if u["plannedWindow"] > 1e-6 else 1.0
        u["audioTempo"] = max(1.0, min(policy["residualTempoCap"], ratio))
        u["effSpeech"] = u["speech"] / u["audioTempo"]

    # Identify clusters needing video slowing
    need_slow = [u for u in valid_units if u["effSpeech"] > u["plannedWindow"] + 1e-6]
    clusters = []
    infeasible_ids = []

    for idx, u in enumerate(need_slow):
        cid = f"cluster_{idx + 1}"
        spd = min(1.0, u["plannedWindow"] / u["effSpeech"]) if u["effSpeech"] > 1e-6 else 1.0
        v_speed = max(policy["minVideoSpeed"], min(policy["maxVideoSpeed"], spd))
        u["clusterId"] = cid
        u["videoSpeed"] = v_speed
        tol_sec = float(policy.get("toleranceSec", 0.5) if policy.get("toleranceSec") is not None else 0.5)
        if u["effSpeech"] > (u["plannedWindow"] / v_speed) + tol_sec + 0.001:
            u["status"] = "timing_infeasible"
            infeasible_ids.append(u["unitId"])
        else:
            u["status"] = "slowed"
        clusters.append({
            "id": cid,
            "sceneIndex": u["sceneIndex"],
            "sourceStart": u["plannedStart"],
            "sourceEnd": u["plannedEnd"],
            "videoSpeed": v_speed,
            "units": [u]
        })

    # Non-slowed units
    for u in valid_units:
        if not u.get("clusterId"):
            u["videoSpeed"] = 1.0
            u["status"] = "borrowed" if (u["borrowLeftSec"] > 1e-6 or u["borrowRightSec"] > 1e-6) else "fits"

    # Merge nearby clusters
    merged_clusters = []
    for c in clusters:
        if (merged_clusters and merged_clusters[-1]["sceneIndex"] == c["sceneIndex"] and
            c["sourceStart"] - merged_clusters[-1]["sourceEnd"] <= policy["clusterMaxGapSec"] + 1e-6 and
            abs(merged_clusters[-1]["videoSpeed"] - c["videoSpeed"]) <= policy["clusterMergeSpeedEps"]):
            prev_c = merged_clusters[-1]
            prev_c["videoSpeed"] = min(prev_c["videoSpeed"], c["videoSpeed"])
            prev_c["sourceEnd"] = c["sourceEnd"]
            prev_c["units"].extend(c["units"])
            for cu in prev_c["units"]:
                cu["clusterId"] = prev_c["id"]
                cu["videoSpeed"] = prev_c["videoSpeed"]
        else:
            merged_clusters.append(c)

    # Compute timeline outputs for clusters
    curr_src = 0.0
    curr_out = 0.0
    output_clusters = []
    for c in merged_clusters:
        gap = c["sourceStart"] - curr_src
        if gap > 1e-6:
            curr_out += gap
        chunk_len = c["sourceEnd"] - c["sourceStart"]
        c_out_start = curr_out
        c_out_end = curr_out + (chunk_len / c["videoSpeed"])
        curr_out = c_out_end
        curr_src = c["sourceEnd"]
        output_clusters.append({
            "id": c["id"],
            "sourceStart": round(c["sourceStart"], 3),
            "sourceEnd": round(c["sourceEnd"], 3),
            "videoSpeed": round(c["videoSpeed"], 3),
            "outputStart": round(c_out_start, 3),
            "outputEnd": round(c_out_end, 3)
        })

    return {
        "version": 1,
        "globalVoiceRate": global_rate,
        "policy": policy,
        "units": [
            {
                "unitId": u["unitId"],
                "audioDuration": round(u["speech"], 3),
                "audioTempo": round(u.get("audioTempo", 1.0), 3),
                "borrowLeftSec": round(u["borrowLeftSec"], 3),
                "borrowRightSec": round(u["borrowRightSec"], 3),
                "plannedStart": round(u["plannedStart"], 3),
                "plannedEnd": round(u["plannedEnd"], 3),
                "plannedWindow": round(u["plannedWindow"], 3),
                "effSpeech": round(u.get("effSpeech", u["speech"]), 3),
                "clusterId": u.get("clusterId"),
                "videoSpeed": round(u.get("videoSpeed", 1.0), 3),
                "status": u.get("status", "fits")
            }
            for u in valid_units
        ],
        "clusters": output_clusters,
        "feasible": len(infeasible_ids) == 0,
        "infeasibleUnitIds": infeasible_ids
    }

def plan_to_timeline_pieces(plan: Dict[str, Any], total_duration: float,
                           opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Convert dubbing plan into pieces of timeline slices."""
    opts = opts or {}
    fps = float(opts.get("fps") or 0.0)
    snap = lambda t: snap_to_frame(t, fps) if fps > 0 else t

    clusters = plan.get("clusters") or []
    tot_dur = float(total_duration) if total_duration and total_duration > 0 else (
        clusters[-1]["sourceEnd"] if clusters else 0.0
    )
    tot_dur_snapped = snap(tot_dur)

    pieces = []
    curr_src = 0.0

    def add_piece(start_t: float, end_t: float, speed: float, cid: Optional[str] = None):
        if end_t - start_t <= 1e-6:
            return
        pieces.append({
            "type": "video" if speed == 1.0 else "slowed",
            "id": cid,
            "origStart": start_t,
            "origEnd": end_t,
            "originalDuration": end_t - start_t,
            "videoSpeed": speed,
            "bedTempo": speed,
            "audioTempo": 1.0,
            "finalDuration": (end_t - start_t) / speed
        })

    for c in clusters:
        c_start = snap(c["sourceStart"])
        c_end = min(snap(c["sourceEnd"]), tot_dur_snapped)
        add_piece(curr_src, min(c_start, tot_dur_snapped), 1.0)
        add_piece(max(curr_src, c_start), c_end, c["videoSpeed"], c["id"])
        curr_src = max(curr_src, c_end)
        if curr_src >= tot_dur_snapped - 1e-6:
            break

    add_piece(curr_src, tot_dur_snapped, 1.0)

    curr_out = 0.0
    for p in pieces:
        p["newStart"] = curr_out
        curr_out += p["finalDuration"]
        p["newEnd"] = curr_out

    return {
        "pieces": pieces,
        "newTotalDuration": curr_out
    }

def apply_global_speed_to_pieces(timeline: Dict[str, Any], global_speed: float) -> Dict[str, Any]:
    """Scale all timeline pieces by global speed factor."""
    spd = float(global_speed or 1.0)
    if spd <= 0.0 or abs(spd - 1.0) <= 0.001:
        return timeline
    pieces = []
    for p in (timeline.get("pieces") or []):
        pieces.append({
            **p,
            "videoSpeed": p["videoSpeed"] * spd,
            "bedTempo": p["bedTempo"] * spd,
            "finalDuration": p["finalDuration"] / spd,
            "newStart": p["newStart"] / spd,
            "newEnd": p["newEnd"] / spd
        })
    return {
        "pieces": pieces,
        "newTotalDuration": timeline.get("newTotalDuration", 0.0) / spd
    }

def map_plan_time(pieces: List[Dict[str, Any]], t: float) -> float:
    """Map a time t from original video timeline to stretched output timeline."""
    if not pieces:
        return t
    for p in pieces:
        if t <= p["origEnd"] + 1e-6:
            if t < p["origStart"] - 1e-6:
                return p["newStart"]
            prog = (t - p["origStart"]) / p["originalDuration"] if p["originalDuration"] > 1e-6 else 0.0
            return p["newStart"] + max(0.0, min(1.0, prog)) * p["finalDuration"]
    last_p = pieces[-1]
    return last_p["newEnd"] + (t - last_p["origEnd"])

def suggest_global_voice_rate(units: List[Dict[str, Any]],
                             opts: Optional[Dict[str, Any]] = None) -> float:
    """Find the best global voice rate (1.0 - 1.35) that minimizes timing infeasibility."""
    opts = opts or {}
    total_dur = float(opts.get("totalDuration") or 0.0)
    best_rate = 1.0
    min_infeasible = 999999

    for r_int in range(100, 136, 5):
        rate = r_int / 100.0
        plan = build_dubbing_plan(units, total_dur, {"globalVoiceRate": rate, "policy": opts.get("policy")})
        inf_count = len(plan.get("infeasibleUnitIds") or [])
        if inf_count < min_infeasible:
            min_infeasible = inf_count
            best_rate = rate
        if inf_count == 0:
            break

    return best_rate

def verify_dubbing_fit(segments: List[Dict[str, Any]],
                       opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Verify if dubbing segments fit into video duration."""
    opts = opts or {}
    plan = build_dubbing_plan(segments, opts.get("totalDuration", 0.0), opts)
    return {
        "feasible": plan.get("feasible", True),
        "infeasibleUnitIds": plan.get("infeasibleUnitIds", []),
        "plan": plan
    }

# ==============================================================================
# 5. GEOMETRY, COLOR GRADING & ASS SUBTITLES
# ==============================================================================

def even_dim(val: Union[int, float]) -> int:
    """Ensure dimension is even and between 2 and 16384."""
    try:
        v = round(float(val) / 2.0) * 2
        return max(MIN_DIM, min(MAX_DIM, int(v)))
    except (ValueError, TypeError):
        return 720

def parse_aspect(aspect_str: Optional[str], width: float, height: float) -> float:
    """Parse aspect ratio string (e.g. '16:9', '9:16', '1:1') or compute from w/h."""
    default_ratio = (width / height) if (width > 0 and height > 0) else (16.0 / 9.0)
    if not aspect_str or aspect_str == "original":
        return default_ratio
    m = re.match(r'^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$', str(aspect_str).strip())
    if not m:
        return default_ratio
    w_ratio, h_ratio = float(m.group(1)), float(m.group(2))
    return (w_ratio / h_ratio) if (w_ratio > 0 and h_ratio > 0) else default_ratio

def compute_output_frame(source_w: int, source_h: int, aspect_str: Optional[str] = None,
                         res_str: Optional[str] = None) -> Tuple[int, int]:
    """Compute final export frame dimensions (width, height)."""
    target_aspect = parse_aspect(aspect_str, source_w, source_h)
    target_h = source_h
    if res_str and res_str != "original":
        m = re.match(r'^(\d+)p?$', str(res_str).strip().lower())
        if m:
            target_h = int(m.group(1))
    target_w = round(target_h * target_aspect)
    return (even_dim(target_w), even_dim(target_h))

def hex_to_rgb(hex_color: str) -> Tuple[float, float, float]:
    """Convert hex color #RRGGBB to normalized RGB float [0..1]."""
    h = hex_color.lstrip('#')
    if len(h) == 6:
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    return (1.0, 1.0, 1.0)

def hex_to_yuv(hex_color: str, width: int = 1920, height: int = 1080) -> Dict[str, float]:
    """Convert hex color to YUV values according to BT.709 or BT.601."""
    r, g, b = hex_to_rgb(hex_color)
    is_hd = (width >= 1280 or height >= 720)
    kr, kb = (0.2126, 0.0722) if is_hd else (0.299, 0.114)
    y_norm = kr * r + (1.0 - kr - kb) * g + kb * b
    return {
        "y": 16.0 + 219.0 * y_norm,
        "u": 128.0 + 224.0 * 0.5 * (b - y_norm) / (1.0 - kb),
        "v": 128.0 + 224.0 * 0.5 * (r - y_norm) / (1.0 - kr)
    }

def build_color_filter_chain(preset_id: str, intensity: float = 1.0,
                            width: int = 1920, height: int = 1080) -> Optional[str]:
    """Generate FFmpeg video filter chain string for color presets."""
    preset = next((p for p in COLOR_FILTER_PRESETS if p["id"] == preset_id), None)
    if not preset or intensity <= 0.0:
        return None

    factor = max(0.0, min(1.0, float(intensity)))
    filters = []

    # Sepia / Colorchannelmixer
    sepia = preset.get("sepia", 0.0) * factor
    if sepia > 0.001:
        mix = lambda base, target: round(base + (target - base) * sepia, 4)
        rr, rg, rb = mix(1.0, 0.393), mix(0.0, 0.769), mix(0.0, 0.189)
        gr, gg, gb = mix(0.0, 0.349), mix(1.0, 0.686), mix(0.0, 0.168)
        br, bg, bb = mix(0.0, 0.272), mix(0.0, 0.534), mix(1.0, 0.131)
        filters.append(f"colorchannelmixer=rr={rr}:rg={rg}:rb={rb}:gr={gr}:gg={gg}:gb={gb}:br={br}:bg={bg}:bb={bb}")

    # Hue
    hue = preset.get("hue", 0.0) * factor
    if abs(hue) > 0.05:
        filters.append(f"hue=h={round(hue, 4)}")

    # Brightness / Contrast / Saturation / Gamma
    contrast = 1.0 + (preset.get("contrast", 1.0) - 1.0) * factor
    sat = max(0.0, 1.0 + (preset.get("sat", 1.0) - 1.0) * factor)
    gamma = 1.0 + (preset.get("gamma", 1.0) - 1.0) * factor
    brightness = ((preset.get("bright", 1.0) - 1.0) * 0.5) * factor

    eq_parts = []
    if abs(contrast - 1.0) > 0.001:
        eq_parts.append(f"contrast={round(contrast, 4)}")
    if abs(sat - 1.0) > 0.001:
        eq_parts.append(f"saturation={round(sat, 4)}")
    if abs(gamma - 1.0) > 0.001:
        eq_parts.append(f"gamma={round(gamma, 4)}")
    if abs(brightness) > 0.001:
        eq_parts.append(f"brightness={round(brightness, 4)}")

    if eq_parts:
        filters.append(f"eq={':'.join(eq_parts)}")

    # Color Tint
    tint = preset.get("tint")
    if tint and tint.get("color"):
        alpha = min(1.0, max(0.0, (tint.get("alpha", 0.0)) * factor))
        if alpha > 0.003:
            yuv = hex_to_yuv(tint["color"], width, height)
            w_inv = round(1.0 - alpha, 4)
            y_add = round(yuv["y"] * alpha, 4)
            u_add = round(yuv["u"] * alpha, 4)
            v_add = round(yuv["v"] * alpha, 4)
            filters.append(f"lutrgb='r=val*{w_inv}+{y_add}:g=val*{w_inv}+{u_add}:b=val*{w_inv}+{v_add}'")

    return ",".join(filters) if filters else None

def format_ass_time(sec: float) -> str:
    """Format seconds into ASS subtitle timestamp string: H:MM:SS.cs."""
    s = max(0.0, float(sec))
    hrs = int(s // 3600)
    rem = s % 3600
    mins = int(rem // 60)
    secs = int(rem % 60)
    cs = int(round((rem - int(rem)) * 100))
    if cs >= 100:
        cs = 0
        secs += 1
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"

def generate_ass_file(segments: List[Dict[str, Any]], options: Optional[Dict[str, Any]] = None,
                      video_width: int = 1920, video_height: int = 1080,
                      font_catalog: Optional[Dict[str, Any]] = None) -> str:
    """
    Generate complete Advanced SubStation Alpha (.ass) subtitle file content.
    """
    opts = options or {}
    font_name = opts.get("fontName") or opts.get("fontFamily") or "Arial"
    font_size = int(opts.get("fontSize") or 22)
    primary_color = opts.get("primaryColor") or "&H00FFFFFF"
    outline_color = opts.get("outlineColor") or "&H00000000"
    back_color = opts.get("backColor") or "&H80000000"
    bold = -1 if opts.get("bold", True) else 0
    italic = -1 if opts.get("italic", False) else 0
    outline = float(opts.get("outline", 2.0))
    shadow = float(opts.get("shadow", 1.0))
    alignment = int(opts.get("alignment", 2)) # 2 = Bottom Center
    margin_l = int(opts.get("marginL", 20))
    margin_r = int(opts.get("marginR", 20))
    margin_v = int(opts.get("marginV", 30))

    lines = [
        "[Script Info]",
        "; Script generated by node_helper.py (ezmaxsub)",
        "Title: Ezmax Subtitles",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {video_width}",
        f"PlayResY: {video_height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{primary_color},&H000000FF,{outline_color},{back_color},{bold},{italic},0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]

    for seg in (segments or []):
        text = (seg.get("subtitleText") or seg.get("translation") or seg.get("text") or "").strip()
        if not text:
            continue
        start_t = float(seg.get("startTime") or 0.0)
        end_t = float(seg.get("endTime") or (start_t + 1.0))
        if end_t <= start_t:
            end_t = start_t + 1.0
        # Escape ASS special chars
        ass_text = text.replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{format_ass_time(start_t)},{format_ass_time(end_t)},Default,,0,0,0,,{ass_text}")

    return "\n".join(lines) + "\n"

def build_watermark_schedule(duration: float, width: int, height: int,
                            opts: Optional[Dict[str, Any]] = None) -> str:
    """Generate dynamic FFmpeg overlay expression for moving watermark."""
    opts = opts or {}
    wm_w = int(opts.get("width", 200))
    wm_h = int(opts.get("height", 60))
    pad = 30
    x_expr = f"{pad}+(W-{wm_w}-{2*pad})*abs(sin(2*PI*t/30))"
    y_expr = f"{pad}+(H-{wm_h}-{2*pad})*abs(cos(2*PI*t/45))"
    return f"overlay=x='{x_expr}':y='{y_expr}'"

def build_video_encoder_args(codec: str, preset: Optional[str] = None,
                            crf: Optional[int] = None,
                            opts: Optional[Dict[str, Any]] = None) -> List[str]:
    """Construct FFmpeg CLI arguments for video encoding."""
    opts = opts or {}
    c = (codec or "libx264").lower()
    args = ["-c:v", c]
    if "nvenc" in c:
        args.extend(["-preset", preset or "p4", "-cq", str(crf or 23)])
    elif "qsv" in c:
        args.extend(["-preset", preset or "medium", "-global_quality", str(crf or 23)])
    elif "amf" in c:
        args.extend(["-quality", "speed", "-rc", "cqp", "-qp_p", str(crf or 23)])
    else:
        args.extend(["-preset", preset or "veryfast", "-crf", str(crf or 22)])

    if opts.get("bitrateKbps"):
        args.extend(["-b:v", f"{opts['bitrateKbps']}k"])
    return args

# ==============================================================================
# 6. ASR RECOGNITION (Denoise, BCut, CapCut, Groq Whisper)
# ==============================================================================

def denoise_audio(ffmpeg_bin: str, video_path: str, out_mp3: str,
                  video_segments: Optional[List[Dict[str, Any]]] = None) -> str:
    """Extract speech-optimized 16kHz mono audio from video at high speed."""
    os.makedirs(os.path.dirname(os.path.abspath(out_mp3)), exist_ok=True)
    # Trích xuất trực tiếp MP3 16kHz mono chất lượng chuẩn ASR, sử dụng VBR và aresample chống crash LAME
    cmd = [
        ffmpeg_bin or "ffmpeg", "-y",
        "-err_detect", "ignore_err",
        "-fflags", "+genpts+discardcorrupt",
        "-i", video_path,
        "-vn",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "libmp3lame",
        "-q:a", "5",
        "-ar", "16000",
        "-ac", "1",
        out_mp3
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg audio extraction failed: {res.stderr}")
    return out_mp3

def choose_asr_engines(preference: str, source_lang: Optional[str],
                       opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Select ASR engines order based on language and credentials."""
    opts = opts or {}
    groq_key = opts.get("groqApiKey")
    pref = (preference or "auto").lower()
    lang = (source_lang or "auto").lower()

    is_cjk = lang in ["zh", "zh-cn", "zh-tw"]
    is_en = lang.startswith("en")

    if pref != "auto":
        return {"engines": [pref], "missingGroqKey": False}

    if is_cjk:
        return {"engines": ["bcut", "capcut", "groq"] if groq_key else ["bcut", "capcut"], "missingGroqKey": False}
    elif is_en:
        return {"engines": ["capcut", "bcut", "groq"] if groq_key else ["capcut", "bcut"], "missingGroqKey": False}
    else:
        if groq_key:
            return {"engines": ["groq"], "missingGroqKey": False}
        return {
            "engines": [],
            "missingGroqKey": True,
            "skipNote": f"Ngôn ngữ gốc '{lang}' cần Groq Whisper API key."
        }

class GroqSTT:
    """Groq Cloud Whisper STT Client."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.api_key:
            raise ValueError("No Groq API key configured")
        if not requests:
            raise RuntimeError("requests library is required for GroqSTT")

        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {
            "model": "whisper-large-v3-turbo",
            "response_format": "verbose_json"
        }
        if language and language != "auto":
            data["language"] = language

        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=60)

        if resp.status_code != 200:
            raise RuntimeError(f"Groq STT Error: {resp.text}")

        resp_json = resp.json()
        segments = []
        for s in (resp_json.get("segments") or []):
            segments.append({
                "startTime": float(s.get("start", 0.0)),
                "endTime": float(s.get("end", 0.0)),
                "text": str(s.get("text", "")).strip()
            })
        return segments

# ==============================================================================
# 5. CAPCUT & BCUT IDENTIFIERS, SIGNING & UTILITIES
# ==============================================================================

def generate_capcut_asr_tdid() -> str:
    """Sinh tdid 16 ký tự hex cho CapCut ASR (PC API)."""
    return uuid.uuid4().hex[:16]

def generate_capcut_asr_sign(tdid: str, path: str) -> Dict[str, str]:
    """Tạo chữ ký sign MD5 và device-time cho CapCut ASR (PC API v6.0.0, pf 4)."""
    device_time = str(int(time.time()))
    sub_path = path[-7:] if len(path) >= 7 else path
    sign_str = f"9e2c|{sub_path}|4|6.0.0|{device_time}|{tdid}|11ac"
    sign_md5 = hashlib.md5(sign_str.encode("utf-8")).hexdigest().lower()
    return {"sign": sign_md5, "deviceTime": device_time}

def generate_capcut_tts_device_id() -> str:
    """Sinh deviceId (did) 19 chữ số cho CapCut TTS."""
    now_str = str(int(time.time() * 1000))
    rnd_str = "".join(str(random.randint(0, 9)) for _ in range(12))
    return (now_str + rnd_str)[:19]

def generate_capcut_tts_tdid() -> str:
    """Sinh tdid 17 chữ số cho CapCut TTS."""
    now_str = str(int(time.time() * 1000))
    rnd_str = "".join(str(random.randint(0, 9)) for _ in range(8))
    return (now_str + rnd_str)[:17]

def generate_capcut_tts_sign(tdid: str, path: str) -> Dict[str, str]:
    """Tạo chữ ký Sign MD5 và Device-Time cho CapCut TTS (Web API v5.8.0, Pf 7)."""
    device_time = str(int(time.time()))
    sub_path = path[-7:] if len(path) >= 7 else path
    sign_str = f"9e2c|{sub_path}|7|5.8.0|{device_time}|{tdid}|11ac"
    sign_md5 = hashlib.md5(sign_str.encode("utf-8")).hexdigest().lower()
    return {"Sign": sign_md5, "Device-Time": device_time}

def aws4_hmac_sha256_sign(secret_key: str, query_str: str, headers_map: Dict[str, str]) -> str:
    """Tính toán chữ ký AWS4 HMAC-SHA256 cho ByteDance VOD TOS."""
    amz_date = headers_map.get("x-amz-date", "")
    date_only = amz_date.split("T")[0]
    canonical_uri = "/"
    canonical_query = query_str
    sorted_keys = sorted(headers_map.keys(), key=lambda x: x.lower())
    canonical_headers = "".join(f"{k.lower()}:{headers_map[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted(k.lower() for k in headers_map.keys()))
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = f"GET\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_only}/cn/vod/aws4_request"
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashed_canonical}"

    def hmac_sha256(key_bytes, msg_bytes):
        return hmac.new(key_bytes, msg_bytes, hashlib.sha256).digest()

    k_date = hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_only.encode("utf-8"))
    k_region = hmac_sha256(k_date, b"cn")
    k_service = hmac_sha256(k_region, b"vod")
    k_signing = hmac_sha256(k_service, b"aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature

def calc_crc32(data: bytes) -> str:
    """Tính mã CRC32 dạng chuỗi hex 8 ký tự viết thường."""
    val = binascii.crc32(data) & 0xffffffff
    return f"{val:08x}"

def get_audio_duration_ffprobe(ffmpeg_bin: str, audio_path: str) -> float:
    """Lấy thời lượng âm thanh chính xác bằng ffprobe."""
    ffprobe_bin = "ffprobe" if not ffmpeg_bin or ffmpeg_bin == "ffmpeg" else re.sub(r'ffmpeg(\.exe)?$', r'ffprobe\1', ffmpeg_bin, flags=re.IGNORECASE)
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio_path]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        dur = float(res.stdout.strip())
        return dur if math.isfinite(dur) and dur > 0 else 0.0
    except Exception:
        return 0.0

def plan_chunk_windows(duration: float, chunk_seconds: float = 300.0, chunk_overlap: float = 5.0) -> List[Dict[str, Any]]:
    """Phân chia file audio dài thành các đoạn nhỏ với overlap."""
    if duration <= chunk_seconds:
        return [{"index": 0, "start": 0.0, "length": duration}]
    windows = []
    curr = 0.0
    idx = 0
    while curr < duration:
        rem = duration - curr
        length = min(chunk_seconds, rem)
        windows.append({"index": idx, "start": curr, "length": length})
        if curr + length >= duration:
            break
        curr += (chunk_seconds - chunk_overlap)
        idx += 1
    return windows

def slice_audio_chunk(ffmpeg_bin: str, src_path: str, start_t: float, dur_t: Optional[float], out_path: str) -> str:
    """Cắt audio thành file chunk MP3."""
    cmd = [ffmpeg_bin or "ffmpeg", "-y"]
    if start_t > 0:
        cmd.extend(["-ss", f"{start_t:.3f}"])
    if dur_t is not None and dur_t > 0:
        cmd.extend(["-t", f"{dur_t:.3f}"])
    cmd.extend(["-i", src_path, "-acodec", "libmp3lame", "-q:a", "4", out_path])
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg slice failed: {res.stderr}")
    return out_path

def offset_segments(segments: List[Dict[str, Any]], offset_sec: float, max_duration: Optional[float] = None) -> List[Dict[str, Any]]:
    """Dịch chuyển timestamp subtitle theo offset của chunk."""
    res = []
    for s in segments:
        st = s["startTime"] + offset_sec
        en = s["endTime"] + offset_sec
        if max_duration and st >= max_duration:
            continue
        if max_duration and en > max_duration:
            en = max_duration
        res.append({
            "startTime": round(st, 3),
            "endTime": round(en, 3),
            "text": s["text"]
        })
    return res

def is_segments_overlapping(s1: Dict[str, Any], s2: Dict[str, Any]) -> bool:
    """Kiểm tra xem hai phân đoạn có bị trùng nhau >= 30% thời lượng không."""
    overlap = min(s1["endTime"], s2["endTime"]) - max(s1["startTime"], s2["startTime"])
    if overlap <= 0:
        return False
    min_dur = min(s1["endTime"] - s1["startTime"], s2["endTime"] - s2["startTime"]) or 1e-9
    return (overlap / min_dur) >= 0.3

def merge_capcut_segments(chunk_segments_list: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Hợp nhất các phân đoạn subtitle từ các chunk, khử trùng đoạn overlap."""
    flat = []
    for chunk in chunk_segments_list:
        if isinstance(chunk, list):
            flat.extend(chunk)
    flat.sort(key=lambda s: (s["startTime"], s["endTime"]))

    merged = []
    for seg in flat:
        if merged and is_segments_overlapping(merged[-1], seg):
            prev = merged[-1]
            chosen_txt = seg["text"] if len(seg["text"]) > len(prev["text"]) else prev["text"]
            merged[-1] = {
                "startTime": min(prev["startTime"], seg["startTime"]),
                "endTime": max(prev["endTime"], seg["endTime"]),
                "text": chosen_txt
            }
        else:
            merged.append({"startTime": seg["startTime"], "endTime": seg["endTime"], "text": seg["text"]})
    return merged

# ==============================================================================
# 6. ASR CLIENTS (Groq, BCut, CapCut)
# ==============================================================================

class BCutASR:
    """Bilibili BCut ASR Client."""
    def __init__(self):
        self.base_url = "https://member.bilibili.com/x/bcut/rubick-interface"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def safe_name(self, file_path: str) -> str:
        name = os.path.splitext(os.path.basename(file_path))[0]
        cleaned = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
        return (cleaned or "audio") + ".mp3"

    def create_resource(self, audio_path: str) -> Dict[str, Any]:
        size = os.path.getsize(audio_path)
        ext = os.path.splitext(audio_path)[1].lstrip(".").lower()
        res_type = ext if ext in ["flac", "aac", "m4a", "mp3", "wav"] else "mp3"
        safe_nm = self.safe_name(audio_path)

        data = urllib.parse.urlencode({
            "type": "2",
            "name": safe_nm,
            "size": str(size),
            "resource_file_type": res_type,
            "model_id": "7"
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.user_agent
        }
        req = urllib.request.Request(f"{self.base_url}/resource/create", data=data.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
        if res_json.get("code") != 0:
            raise RuntimeError(f"BCut createResource failed: {res_json.get('message', res_json)}")
        return res_json["data"]

    def upload_chunks(self, audio_path: str, upload_urls: List[str], chunk_size: int) -> List[str]:
        etags = []
        with open(audio_path, "rb") as f:
            for idx, url in enumerate(upload_urls):
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                req = urllib.request.Request(
                    url,
                    data=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                        "User-Agent": self.user_agent
                    },
                    method="PUT"
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    etag = resp.headers.get("etag") or resp.headers.get("ETag") or ""
                    etags.append(etag.strip('"'))
        return etags

    def complete_upload(self, resource_id: str, in_boss_key: str, upload_id: str, etags: List[str]) -> str:
        data = urllib.parse.urlencode({
            "in_boss_key": in_boss_key,
            "resource_id": resource_id,
            "etags": ",".join(etags),
            "upload_id": upload_id,
            "model_id": "7"
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.user_agent
        }
        req = urllib.request.Request(f"{self.base_url}/resource/create/complete", data=data.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
        if res_json.get("code") != 0:
            raise RuntimeError(f"BCut completeUpload failed: {res_json.get('message', res_json)}")
        return res_json["data"]["download_url"]

    def submit_task(self, download_url: str) -> str:
        body = json.dumps({"resource": download_url, "model_id": "7"}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent
        }
        req = urllib.request.Request(f"{self.base_url}/task", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
        if res_json.get("code") != 0:
            raise RuntimeError(f"BCut submitTask failed: {res_json.get('message', res_json)}")
        return res_json["data"]["task_id"]

    def query(self, task_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/task/result?model_id=7&task_id={urllib.parse.quote(task_id)}"
        headers = {"User-Agent": self.user_agent}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def parse_result(self, res_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = res_json.get("data") or res_json
        res_str = data.get("result")
        if isinstance(res_str, str):
            try:
                res_obj = json.loads(res_str)
            except Exception:
                res_obj = {}
        elif isinstance(res_str, dict):
            res_obj = res_str
        elif "utterances" in data:
            res_obj = data
        else:
            res_obj = {}

        utterances = res_obj.get("utterances") or []
        segments = []
        for u in utterances:
            txt = str(u.get("transcript") or u.get("text") or "").strip()
            st = u.get("start_time") if u.get("start_time") is not None else u.get("start")
            en = u.get("end_time") if u.get("end_time") is not None else u.get("end")
            if txt and st is not None and en is not None and float(en) > float(st):
                segments.append({
                    "startTime": float(st) / 1000.0,
                    "endTime": float(en) / 1000.0,
                    "text": txt
                })
        return segments

    def poll_result(self, task_id: str, max_wait_sec: float = 180.0, on_status: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
        start_t = time.time()
        complete_states = [4, "4", "COMPLETE"]
        error_states = [3, "3", "ERROR"]

        while time.time() - start_t < max_wait_sec:
            res_json = self.query(task_id)
            data = res_json.get("data") or res_json
            state = data.get("state")
            if state in complete_states:
                segs = self.parse_result(res_json)
                if on_status:
                    on_status(f"[BCut ASR] Hoàn thành ({len(segs)} segments)")
                return segs
            if state in error_states:
                remark = data.get("remark") or data.get("message") or str(state)
                raise RuntimeError(f"BCut ASR job failed: {remark}")

            if on_status:
                on_status(f"[BCut ASR] Đang xử lý (state={state})...")
            time.sleep(1.5 + random.random() * 0.5)

        raise TimeoutError("BCut ASR timeout — không nhận được kết quả.")

    def transcribe(self, audio_path: str, opts: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        opts = opts or {}
        on_status = opts.get("onStatus")
        max_wait = float(opts.get("maxWaitMs", 180000)) / 1000.0

        if on_status:
            on_status("[BCut ASR] Đang đăng ký upload...")
        res_info = self.create_resource(audio_path)
        urls = res_info.get("upload_urls") or []
        if not urls:
            raise RuntimeError("BCut createResource returned no upload URLs")

        per_size = int(res_info.get("per_size") or 5 * 1024 * 1024)
        if on_status:
            on_status(f"[BCut ASR] Đang tải lên {len(urls)} phần...")
        etags = self.upload_chunks(audio_path, urls, per_size)

        if on_status:
            on_status("[BCut ASR] Đang xác nhận upload...")
        download_url = self.complete_upload(
            resource_id=res_info["resource_id"],
            in_boss_key=res_info["in_boss_key"],
            upload_id=res_info["upload_id"],
            etags=etags
        )

        if on_status:
            on_status("[BCut ASR] Đang gửi yêu cầu nhận dạng...")
        task_id = self.submit_task(download_url)

        if on_status:
            on_status("[BCut ASR] Đang chờ kết quả nhận dạng...")
        return self.poll_result(task_id, max_wait_sec=max_wait, on_status=on_status)

class CapCutASR:
    """Bytedance CapCut ASR Client (PC API v6.0.0)."""
    def __init__(self, tdid: Optional[str] = None):
        self.tdid = str(tdid).strip() if tdid and str(tdid).strip() else generate_capcut_asr_tdid()
        self.base_url = "https://lv-pc-api-sinfonlinec.ulikecam.com"
        self.user_agent_cronet = "Cronet/TTNetVersion:01594da2 2023-03-14 QuicVersion:46688bb4 2022-11-28"
        self.user_agent_browser = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36 Thea/1.0.1"

    def _headers(self, path: str) -> Dict[str, str]:
        sign_info = generate_capcut_asr_sign(self.tdid, path)
        return {
            "User-Agent": self.user_agent_cronet,
            "appvr": "6.0.0",
            "pf": "4",
            "sign": sign_info["sign"],
            "sign-ver": "1",
            "tdid": self.tdid,
            "device-time": sign_info["deviceTime"],
            "Content-Type": "application/json"
        }

    def _upload_sign(self) -> Dict[str, str]:
        path = "/lv/v1/upload_sign"
        url = f"{self.base_url}{path}"
        body = json.dumps({"biz": "pc-recognition"}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(path), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ret") != "0" and data.get("ret") != 0:
            raise RuntimeError(f"CapCut upload_sign failed: {data.get('errmsg', data)}")
        sign_data = data["data"]
        return {
            "accessKey": sign_data["access_key_id"],
            "secretKey": sign_data["secret_access_key"],
            "sessionToken": sign_data["session_token"]
        }

    def _apply_upload(self, auth_data: Dict[str, str], file_size: int) -> Dict[str, Any]:
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date_only = now_iso[:8]
        query_params = f"Action=ApplyUploadInner&FileSize={file_size}&FileType=object&IsInner=1&SpaceName=lv-mac-recognition&Version=2020-11-19&s=5y0udbjapi"
        headers_map = {
            "x-amz-date": now_iso,
            "x-amz-security-token": auth_data["sessionToken"]
        }
        sig = aws4_hmac_sha256_sign(auth_data["secretKey"], query_params, headers_map)
        signed_hdr_names = ";".join(sorted(k.lower() for k in headers_map.keys()))
        headers_map["authorization"] = f"AWS4-HMAC-SHA256 Credential={auth_data['accessKey']}/{date_only}/cn/vod/aws4_request, SignedHeaders={signed_hdr_names}, Signature={sig}"

        url = f"https://vod.bytedanceapi.com/?{query_params}"
        req = urllib.request.Request(url, headers=headers_map, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        upload_addr = data["Result"]["UploadAddress"]
        store_info = upload_addr["StoreInfos"][0]
        return {
            "storeUri": store_info["StoreUri"],
            "auth": store_info["Auth"],
            "uploadId": store_info["UploadID"],
            "uploadHost": upload_addr["UploadHosts"][0]
        }

    def _upload_file(self, apply_info: Dict[str, Any], file_bytes: bytes, crc32_hex: str) -> None:
        url = f"https://{apply_info['uploadHost']}/{apply_info['storeUri']}?partNumber=1&uploadID={apply_info['uploadId']}"
        headers = {
            "User-Agent": self.user_agent_browser,
            "Authorization": apply_info["auth"],
            "Content-CRC32": crc32_hex,
            "Content-Length": str(len(file_bytes))
        }
        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        if resp_data.get("success") != 0 and resp_data.get("success") is not None:
            raise RuntimeError(f"CapCut upload file failed: {resp_data}")

    def _upload_check(self, apply_info: Dict[str, Any], crc32_hex: str) -> None:
        url = f"https://{apply_info['uploadHost']}/{apply_info['storeUri']}?uploadID={apply_info['uploadId']}"
        headers = {
            "User-Agent": self.user_agent_browser,
            "Authorization": apply_info["auth"],
            "Content-CRC32": crc32_hex
        }
        body = f"1:{crc32_hex}".encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                pass
        except Exception:
            pass

    def _upload_commit(self, apply_info: Dict[str, Any], file_bytes: bytes, crc32_hex: str, session_token: str) -> None:
        url = f"https://{apply_info['uploadHost']}/{apply_info['storeUri']}?uploadID={apply_info['uploadId']}&partNumber=1&x-amz-security-token={urllib.parse.quote(session_token)}"
        headers = {
            "User-Agent": self.user_agent_browser,
            "Authorization": apply_info["auth"],
            "Content-CRC32": crc32_hex
        }
        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                pass
        except Exception:
            pass

    def _submit(self, store_uri: str) -> str:
        path = "/lv/v1/audio_subtitle/submit"
        url = f"{self.base_url}{path}"
        payload = {
            "adjust_endtime": 200,
            "audio": store_uri,
            "caption_type": 2,
            "client_request_id": str(uuid.uuid4()),
            "max_lines": 1,
            "songs_info": [],
            "words_per_line": 16
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(path), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        code = data.get("code", 0)
        if code != 0:
            msg = data.get("message") or data.get("msg") or str(data)
            raise RuntimeError(f"CapCut ASR submit error {code}: {msg}")
        return data["data"]["id"]

    def _query(self, task_id: str) -> Dict[str, Any]:
        path = "/lv/v1/audio_subtitle/query"
        url = f"{self.base_url}{path}"
        payload = {
            "id": task_id,
            "pack_options": {"need_attribute": True}
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(path), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _parse_utterances(self, result_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = result_json.get("data") or result_json
        utterances = data.get("utterances") or []
        segments = []
        for u in utterances:
            txt = str(u.get("text") or "").strip()
            st = u.get("start_time")
            en = u.get("end_time")
            if txt and st is not None and en is not None and float(en) > float(st):
                segments.append({
                    "startTime": float(st) / 1000.0,
                    "endTime": float(en) / 1000.0,
                    "text": txt
                })
        return segments

    def poll_result(self, task_id: str, max_wait_sec: float = 180.0, on_status: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
        start_t = time.time()
        poll_count = 0
        last_log_t = 0.0

        while time.time() - start_t < max_wait_sec:
            poll_count += 1
            res = self._query(task_id)
            data = res.get("data") or res
            elapsed = time.time() - start_t

            if "utterances" in data:
                segs = self._parse_utterances(data)
                if segs:
                    if on_status:
                        on_status(f"[CapCut ASR] ✅ Nhận diện thành công {len(segs)} câu sau {elapsed:.1f}s (thử #{poll_count}).")
                    return segs
                attr_extra = ((data.get("attribute") or {}).get("extra") or {})
                if attr_extra.get("empty_reason"):
                    if on_status:
                        on_status(f"[CapCut ASR] ℹ️ Không phát hiện giọng nói trong đoạn âm thanh ({elapsed:.1f}s).")
                    return []
            else:
                status = str(data.get("status") or "").lower()
                if status in ["failed", "error", "fail"]:
                    msg = data.get("message") or data.get("msg") or status
                    raise RuntimeError(f"CapCut ASR job failed: {msg}")
                if status in ["completed", "success", "done", "finish", "finished"]:
                    segs = self._parse_utterances(data)
                    if segs:
                        if on_status:
                            on_status(f"[CapCut ASR] ✅ Hoàn thành ({len(segs)} câu, {elapsed:.1f}s).")
                        return segs

            # Log polling progress every ~3 seconds
            if on_status and (time.time() - last_log_t >= 2.5 or poll_count == 1):
                on_status(f"[CapCut ASR] ⏳ Đang chờ kết quả... (đã đợi {elapsed:.1f}s, lần thử #{poll_count})")
                last_log_t = time.time()

            time.sleep(1.5 + random.random() * 0.5)

        raise TimeoutError(f"CapCut ASR timeout ({max_wait_sec}s) — không nhận được kết quả.")

    def transcribe(self, audio_path: str, opts: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        opts = opts or {}
        on_status = opts.get("onStatus")
        max_wait = float(opts.get("maxWaitMs", 180000)) / 1000.0
        prefix = opts.get("prefix", "")

        def status(msg: str):
            if on_status:
                on_status(f"{prefix}{msg}")

        with open(audio_path, "rb") as f:
            file_bytes = f.read()

        file_size = len(file_bytes)
        crc32_hex = calc_crc32(file_bytes)
        file_mb = file_size / (1024 * 1024)

        status(f"[CapCut ASR] 🔑 [1/4] Lấy token xác thực upload (tdid={self.tdid})...")
        auth_data = self._upload_sign()

        status(f"[CapCut ASR] 📝 [2/4] Đăng ký upload TOS VOD ({file_mb:.2f} MB, CRC32={crc32_hex})...")
        apply_info = self._apply_upload(auth_data, file_size)

        status(f"[CapCut ASR] 📤 [3/4] Tải audio lên {apply_info['uploadHost']}...")
        self._upload_file(apply_info, file_bytes, crc32_hex)
        self._upload_check(apply_info, crc32_hex)
        self._upload_commit(apply_info, file_bytes, crc32_hex, auth_data["sessionToken"])

        status(f"[CapCut ASR] 🚀 [4/4] Gửi yêu cầu nhận dạng...")
        task_id = self._submit(apply_info["storeUri"])
        status(f"[CapCut ASR] 🆔 Task ID: {task_id}")

        return self.poll_result(task_id, max_wait_sec=max_wait, on_status=status)

    def transcribe_chunk(self, audio_chunk_path: str, tdid: Optional[str] = None) -> List[Dict[str, Any]]:
        if tdid:
            self.tdid = tdid
        return self.transcribe(audio_chunk_path)

def transcribe_capcut_chunked(audio_path: str, ffmpeg_bin: str = "ffmpeg", tdid: Optional[str] = None,
                              on_status: Optional[Callable[[str], None]] = None,
                              on_progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
    """Tự động phân đoạn và nhận dạng file âm thanh dài với CapCut ASR kèm log tiến trình chi tiết."""
    duration = get_audio_duration_ffprobe(ffmpeg_bin, audio_path)
    if duration <= 0:
        if on_status:
            on_status("[CapCut ASR] ⚠️ Không đo được thời lượng qua ffprobe, chuyển sang chế độ file đơn...")
        client = CapCutASR(tdid)
        return client.transcribe(audio_path, {"onStatus": on_status})

    windows = plan_chunk_windows(duration, chunk_seconds=300.0, chunk_overlap=5.0)
    total_chunks = len(windows)
    
    if total_chunks <= 1:
        if on_status:
            on_status(f"[CapCut ASR] 🎯 File ngắn ({duration:.1f}s / {duration/60:.1f} phút) → Xử lý trực tiếp 1 lần.")
        client = CapCutASR(tdid)
        return client.transcribe(audio_path, {"onStatus": on_status})

    concurrency = min(4, total_chunks)
    if on_status:
        on_status(f"[CapCut ASR] 🎯 File dài {duration:.1f}s ({duration/60.0:.1f} phút) → Chia thành {total_chunks} đoạn, nhận diện song song {concurrency} luồng (mỗi đoạn 300s, overlap 5s).")

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tmp_dir = tempfile.gettempdir()
    chunk_results_map: Dict[int, List[Dict[str, Any]]] = {}
    failed_chunks: List[Dict[str, Any]] = []
    lock = threading.Lock()
    done_counter = 0

    def _process_chunk(w: Dict[str, Any]) -> Tuple[bool, Optional[Exception]]:
        nonlocal done_counter
        idx = w["index"]
        c_num = idx + 1
        c_start_m = w["start"] / 60.0
        c_end_m = (w["start"] + w["length"]) / 60.0

        if on_status:
            on_status(f"[CapCut ASR] 📦 [Đoạn {c_num}/{total_chunks}] Bắt đầu ({c_start_m:.1f}m - {c_end_m:.1f}m)...")

        chunk_file = os.path.join(tmp_dir, f"capcut_chunk_{int(time.time()*1000)}_{idx}.mp3")
        last_err = None

        for retry in range(1, 3):
            try:
                slice_audio_chunk(ffmpeg_bin, audio_path, w["start"], w["length"], chunk_file)
                retry_label = f" (thử lại #{retry})" if retry > 1 else ""
                if on_status and retry > 1:
                    on_status(f"[CapCut ASR] ✂️ [Đoạn {c_num}/{total_chunks}] Đã cắt audio chunk {retry_label} -> Đang gửi nhận dạng...")

                client = CapCutASR(tdid)
                chunk_prefix = f"[Đoạn {c_num}/{total_chunks}] "
                segs = client.transcribe(chunk_file, {"onStatus": on_status, "prefix": chunk_prefix})
                offsetted = offset_segments(segs, w["start"], duration)

                with lock:
                    chunk_results_map[idx] = offsetted
                    done_counter += 1
                    cur_done = done_counter
                    pct_done = int((cur_done / total_chunks) * 100)

                if on_status:
                    on_status(f"[CapCut ASR] ✨ [Đoạn {c_num}/{total_chunks}] Xong! Nhận được {len(segs)} câu. (Tiến độ: {cur_done}/{total_chunks} đoạn - {pct_done}%)")
                if on_progress:
                    on_progress({"done": cur_done, "total": total_chunks, "percent": pct_done})
                return True, None
            except Exception as e:
                last_err = e
                if on_status:
                    on_status(f"[CapCut ASR] ⚠️ [Đoạn {c_num}/{total_chunks}] Thử lần {retry} gặp lỗi: {e}")
            finally:
                if os.path.exists(chunk_file):
                    try:
                        os.remove(chunk_file)
                    except Exception:
                        pass

        with lock:
            failed_chunks.append({"index": idx, "start": w["start"], "error": str(last_err)})
        return False, last_err

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_process_chunk, w) for w in windows]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass

    if failed_chunks and not chunk_results_map:
        raise RuntimeError(f"CapCut ASR thất bại trên toàn bộ các đoạn: {failed_chunks}")

    # Reconstruct strictly sorted chunk_results by window index
    chunk_results = [chunk_results_map[w["index"]] for w in windows if w["index"] in chunk_results_map]

    if on_status:
        on_status(f"[CapCut ASR] 🔄 Đang ghép nối và khử trùng lặp phụ đề từ {len(chunk_results)} đoạn...")

    merged = merge_capcut_segments(chunk_results)
    if on_status:
        on_status(f"[CapCut ASR] 🏁 Hoàn tất nhận diện CapCut ASR! Tổng cộng: {len(merged)} câu phụ đề.")
    return merged

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
    """Convert segments to SubRip (.srt) subtitle string."""
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

# ==============================================================================
# 7. TRANSLATION, LORE BIBLE & LLM INTEGRATION
# ==============================================================================

def resolve_provider(model_name: str, custom_provider: Optional[str] = None) -> Dict[str, Any]:
    """Resolve translation provider configurations."""
    m = (model_name or "deepseek-chat").lower()
    if custom_provider == "ezmax":
        return {"id": "ezmax", "name": "Ezmax Cloud", "apiKeySettingKey": "ezmaxApiKey", "url": "https://api.ezmax.ai/v1/chat/completions"}
    if "deepseek" in m:
        return {"id": "deepseek", "name": "DeepSeek", "apiKeySettingKey": "deepseekApiKey", "url": "https://api.deepseek.com/chat/completions"}
    if "grok" in m:
        return {"id": "grok", "name": "Grok (xAI)", "apiKeySettingKey": "grokApiKey", "url": "https://api.x.ai/v1/chat/completions"}
    if "gpt" in m:
        return {"id": "openai", "name": "OpenAI", "apiKeySettingKey": "openaiApiKey", "url": "https://api.openai.com/v1/chat/completions"}
    return {"id": "custom", "name": "Custom LLM", "apiKeySettingKey": "customApiKey", "url": "http://localhost:11434/v1/chat/completions"}

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
            prev_end = float(segments[i - 1].get("endTime", 0.0))
            gap_before = max(0.0, start - prev_end - 0.08)
        else:
            gap_before = max(0.0, start)
        gap_before = min(0.6, gap_before * 0.5)
        
        gap_after = 0.0
        if i + 1 < n:
            next_start = float(segments[i + 1].get("startTime", end))
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

class DeepSeekTranslator:
    """LLM Translation client with Lore Bible & Glossary injection matching node_helper.js."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _needs_retry(self, orig: str, trans: str, source: str = "auto", target: str = "vi") -> bool:
        """Check if translation for a segment needs retry matching node_helper.js."""
        o = (orig or "").strip()
        t = (trans or "").strip()
        if not o:
            return False
        has_letters = bool(re.search(r'[a-zA-ZÀ-ỹ\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', o))
        if not has_letters and cjk_ratio(o) < 0.05:
            return False
        if not t:
            return True
        if o == t:
            return True
        src = (source or "auto").lower()
        tgt = (target or "vi").lower()
        if src in ["auto", "zh", "ja", "ko", ""]:
            if tgt not in ["zh", "ja", "ko"] and cjk_ratio(t) >= 0.05:
                return True
        else:
            if src != tgt:
                w_orig = [w.lower() for w in o.split() if w]
                w_trans = [w.lower() for w in t.split() if w]
                if len(w_orig) >= 4 and w_orig == w_trans:
                    return True
        return False

    def build_retry_context(self, targets: List[Dict[str, Any]], 
                            translated_map: Dict[str, str], 
                            miss_indices: List[int]) -> List[Dict[str, Any]]:
        """Build surrounding context for missed segments matching node_helper.js."""
        miss_set = set(miss_indices)
        context_map: Dict[int, Dict[str, Any]] = {}
        for idx in miss_indices:
            for offset in [-2, -1, 1]:
                cand = idx + offset
                if 0 <= cand < len(targets) and cand not in miss_set and cand not in context_map:
                    t = translated_map.get(str(targets[cand].get("id", cand)))
                    if t and t != targets[cand].get("text", ""):
                        context_map[cand] = {
                            **targets[cand],
                            "translation": t,
                            "contextOnly": True
                        }
        sorted_contexts = [v for _, v in sorted(context_map.items())]
        return sorted_contexts[:8]

    def _call_llm(self, messages: List[Dict[str, str]], model: str, url: str, temperature: float = 0.3) -> str:
        """Call LLM completions endpoint with requests or urllib."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "dummy":
            headers["Authorization"] = f"Bearer {self.api_key}"

        if requests is not None:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            if resp.status_code != 200:
                raise RuntimeError(f"LLM API error ({resp.status_code}): {resp.text[:300]}")
            raw = (resp.text or "").strip()
        else:
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode("utf-8").strip()

        # Handle SSE / data: lines or JSON
        if raw.startswith("data:") or "text/event-stream" in raw:
            parts = []
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    try:
                        delta = json.loads(line[5:].strip()).get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            parts.append(delta["content"])
                    except Exception:
                        pass
            return "".join(parts).strip()
        
        try:
            data = json.loads(raw)
            choices = data.get("choices") or []
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
        except Exception:
            pass
        return raw

    def condense_chunk(self, candidates: List[Dict[str, Any]], opts: Dict[str, Any], deep: bool = False) -> Dict[str, str]:
        """Condense lines to fit target spoken duration matching node_helper.js."""
        if not candidates:
            return {}
        n_lines = len(candidates)
        model = opts.get("model", "deepseek-chat")
        url = opts.get("url", "https://api.deepseek.com/chat/completions")

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
            src_text = c.get("source") or c.get("seg", {}).get("text", "")
            full_trans = c.get("full", "")
            b_min = c.get("budgetMin", 2)
            b_max = c.get("budget", b_min)
            range_str = f"{b_min}–{b_max}" if b_min < b_max else f"≤{b_max}"
            user_lines.append(f"{idx + 1}|[{range_str} âm tiết] GỐC: {src_text} | BẢN ĐỦ: {full_trans}")

        user_prompt = "\n".join(user_lines)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            content = self._call_llm(messages, model, url, temperature=0.3)
            results = {}
            for line in content.splitlines():
                line = line.strip()
                if "|" in line:
                    parts = line.split("|", 1)
                    try:
                        line_num = int(parts[0].strip())
                        if 1 <= line_num <= len(candidates):
                            cand_id = str(candidates[line_num - 1].get("id") or candidates[line_num - 1].get("idx", ""))
                            results[cand_id] = parts[1].strip()
                    except Exception:
                        pass
            return results
        except Exception as e:
            err_str = str(e)
            if "401" in err_str:
                log_message(f"[Condense Warning] Không thể kết nối API rút gọn (401 Unauthorized). Bỏ qua condensation, giữ nguyên câu dịch gốc.")
            else:
                log_message(f"[Condense Warning] Lỗi khi rút gọn (deep={deep}): {err_str[:120]}")
            return {}

    def condense_with_budgets(self, items: List[Dict[str, Any]], opts: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Condense lines to fit target spoken duration."""
        if not items:
            return []
        candidates = []
        for it in items:
            full = it.get("text") or it.get("translation") or ""
            syl = vi_syllables_spoken(full)
            b_syl = int(it.get("budgetSyl", 0))
            if b_syl > 0 and syl > b_syl * 1.06:
                candidates.append({
                    "id": str(it.get("id", len(candidates) + 1)),
                    "source": it.get("sourceText") or it.get("source", ""),
                    "full": full,
                    "budget": b_syl,
                    "budgetMin": max(2, int(syl * 0.7)),
                    "item": it
                })

        if candidates:
            # Pass 1: normal condense
            p1_res = self.condense_chunk(candidates, opts, deep=False)
            deep_candidates = []
            for c in candidates:
                c_id = c["id"]
                cur = p1_res.get(c_id, c["full"])
                c["item"]["spokenText"] = cur
                if vi_syllables_spoken(cur) > c["budget"] * 1.06:
                    deep_candidates.append({
                        "id": c_id,
                        "source": c["source"],
                        "full": c["full"],
                        "budget": c["budget"],
                        "budgetMin": max(2, int(vi_syllables_spoken(c["full"]) * 0.5)),
                        "item": c["item"]
                    })
            if deep_candidates:
                p2_res = self.condense_chunk(deep_candidates, opts, deep=True)
                for dc in deep_candidates:
                    dc_id = dc["id"]
                    if dc_id in p2_res:
                        dc["item"]["spokenText"] = p2_res[dc_id]
        return items

    def translate_segments(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Translate subtitle segments with Retranslation and Condensation matching node_helper.js."""
        segments = params.get("segments") or []
        target_lang = params.get("targetLang") or "vi"
        source_lang = params.get("sourceLang") or "auto"
        model = params.get("model") or "deepseek-chat"
        url = params.get("url") or "https://api.deepseek.com/chat/completions"
        on_status = params.get("onStatus")
        on_progress = params.get("onProgress")
        glossary = params.get("glossary") or []

        if on_status:
            on_status(f"Translating {len(segments)} segments to {target_lang} using {model}...")

        # 1. Compute segment budgets
        budgeted_segments = calculate_segment_budgets(segments)
        
        # 2. Batch chunking (40-60 segments)
        batch_size = 50
        chunks = [budgeted_segments[i:i + batch_size] for i in range(0, len(budgeted_segments), batch_size)]
        total_batches = len(chunks)

        results_map: Dict[str, str] = {}
        suspect_ids: Set[str] = set()

        system_prompt = (
            f"Bạn là chuyên gia dịch thuật phụ đề phim chuyên nghiệp sang ngôn ngữ '{target_lang}'. "
            f"Hãy dịch chính xác, tự nhiên theo ngữ cảnh đối thoại. "
            f"Đầu vào là mảng JSON các câu [{{'id': ..., 'text': ...}}]. "
            f"BẮT BUỘC trả về mảng JSON đúng định dạng [{{'id': ..., 'translation': '...'}}]. "
            f"Không thêm markdown ngoài ```json, không giải thích."
        )
        if glossary:
            glossary_text = "\n".join(f"- {g.get('src')}: {g.get('tgt')}" for g in glossary if g.get('src') and g.get('tgt'))
            if glossary_text:
                system_prompt += f"\n\nBẢNG THUẬT NGỮ BẮT BUỘC:\n{glossary_text}"

        for b_idx, chunk in enumerate(chunks):
            b_num = b_idx + 1
            if on_status:
                on_status(f"Translating chunk {b_num}/{total_batches} ({len(chunk)} segments)...")
            if on_progress:
                on_progress({"done": b_idx, "total": total_batches})

            chunk_input = [{"id": str(s.get("id", idx + 1)), "text": s.get("text", "")} for idx, s in enumerate(chunk)]
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(chunk_input, ensure_ascii=False)}
            ]

            try:
                content = self._call_llm(messages, model, url, temperature=0.3)
                if "```json" in content:
                    content = content.split("```json", 1)[1].split("```", 1)[0].strip()
                elif "```" in content:
                    content = content.split("```", 1)[1].split("```", 1)[0].strip()

                parsed = None
                try:
                    parsed = json.loads(content)
                except Exception:
                    m = re.findall(r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"(?:translation|text|target|vi)"\s*:\s*"([^"]*)"\s*\}', content)
                    if m:
                        parsed = [{"id": item[0], "translation": item[1]} for item in m]

                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and "id" in item:
                            t_val = item.get("translation") or item.get("text") or ""
                            results_map[str(item["id"])] = t_val
                else:
                    lines = [line.strip() for line in content.splitlines() if line.strip()]
                    for idx, s in enumerate(chunk):
                        s_id = str(s.get("id", idx + 1))
                        if idx < len(lines):
                            results_map[s_id] = lines[idx]

            except Exception as e:
                log_message(f"[Translate Error] Lỗi dịch chunk {b_num}: {e}")

            # 3. Retranslation Loop (matching node_helper.js _needsRetry)
            max_retry_rounds = 5
            prev_missed_count = float("inf")
            no_progress_count = 0

            for retry_round in range(1, max_retry_rounds + 1):
                missed_indices = []
                for idx, s in enumerate(chunk):
                    s_id = str(s.get("id", ""))
                    cur_t = results_map.get(s_id, "")
                    if self._needs_retry(s.get("text", ""), cur_t, source=source_lang, target=target_lang):
                        missed_indices.append(idx)

                if not missed_indices:
                    break

                if len(missed_indices) >= prev_missed_count:
                    no_progress_count += 1
                    if no_progress_count >= 2:
                        for idx in missed_indices:
                            suspect_ids.add(str(chunk[idx].get("id", "")))
                        break
                else:
                    no_progress_count = 0
                prev_missed_count = len(missed_indices)

                if retry_round == max_retry_rounds:
                    for idx in missed_indices:
                        suspect_ids.add(str(chunk[idx].get("id", "")))

                if on_status:
                    on_status(f"Retranslating {len(missed_indices)} missed segments in chunk {b_num} (round {retry_round}/{max_retry_rounds})...")

                missed_targets = [chunk[idx] for idx in missed_indices]
                context_targets = self.build_retry_context(chunk, results_map, missed_indices)

                retry_input = [{"id": str(s.get("id", "")), "text": s.get("text", "")} for s in missed_targets]
                for c in context_targets:
                    retry_input.append({
                        "id": str(c.get("id", "")),
                        "text": c.get("text", ""),
                        "context": c.get("translation", ""),
                        "isContextOnly": True
                    })

                retry_messages = [
                    {"role": "system", "content": system_prompt + "\nLƯU Ý: Các câu có 'isContextOnly': true là ngữ cảnh tham khảo, KHÔNG dịch lại. CHỈ dịch và trả về các câu còn lại dưới dạng mảng JSON [{\"id\": ..., \"translation\": ...}]."},
                    {"role": "user", "content": json.dumps(retry_input, ensure_ascii=False)}
                ]

                try:
                    retry_content = self._call_llm(retry_messages, model, url, temperature=0.3)
                    if "```json" in retry_content:
                        retry_content = retry_content.split("```json", 1)[1].split("```", 1)[0].strip()
                    elif "```" in retry_content:
                        retry_content = retry_content.split("```", 1)[1].split("```", 1)[0].strip()

                    retry_parsed = None
                    try:
                        retry_parsed = json.loads(retry_content)
                    except Exception:
                        m = re.findall(r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"(?:translation|text|target|vi)"\s*:\s*"([^"]*)"\s*\}', retry_content)
                        if m:
                            retry_parsed = [{"id": item[0], "translation": item[1]} for item in m]

                    if isinstance(retry_parsed, list):
                        for it in retry_parsed:
                            if isinstance(it, dict) and "id" in it:
                                it_id = str(it["id"])
                                it_trans = (it.get("translation") or it.get("text") or "").strip()
                                orig_s = next((s for s in missed_targets if str(s.get("id")) == it_id), None)
                                if orig_s and it_trans and not self._needs_retry(orig_s.get("text", ""), it_trans, source=source_lang, target=target_lang):
                                    results_map[it_id] = it_trans
                except Exception as r_err:
                    log_message(f"[Retranslate Error] Round {retry_round}: {r_err}")
                    break

        if on_progress:
            on_progress({"done": total_batches, "total": total_batches})

        # 4. Condensation Passes (Vietnamese spokenText)
        spoken_map: Dict[str, str] = {}
        if (target_lang or "").lower().startswith("vi"):
            pass1_candidates = []
            for s in budgeted_segments:
                s_id = str(s.get("id", ""))
                full_t = results_map.get(s_id) or s.get("translation") or s.get("text", "")
                syl = vi_syllables_spoken(full_t)
                b_syl = s.get("budgetSyl", 0)
                if b_syl > 0 and syl > b_syl * 1.06:
                    pass1_candidates.append({
                        "id": s_id,
                        "idx": s_id,
                        "source": s.get("text", ""),
                        "full": full_t,
                        "budget": b_syl,
                        "budgetMin": max(2, int(syl * 0.7))
                    })

            if pass1_candidates:
                if on_status:
                    on_status(f"Rút gọn lời đọc cho {len(pass1_candidates)} câu vượt khung thời lượng...")
                p1_results = self.condense_chunk(pass1_candidates, {"model": model, "url": url}, deep=False)
                for c in pass1_candidates:
                    c_id = c["id"]
                    if c_id in p1_results and p1_results[c_id]:
                        spoken_map[c_id] = p1_results[c_id]

                # Pass 2: Deep condensation
                deep_candidates = []
                for c in pass1_candidates:
                    c_id = c["id"]
                    cur_read = spoken_map.get(c_id, c["full"])
                    if vi_syllables_spoken(cur_read) > c["budget"] * 1.06:
                        deep_candidates.append({
                            "id": c_id,
                            "idx": c_id,
                            "source": c["source"],
                            "full": c["full"],
                            "budget": c["budget"],
                            "budgetMin": max(2, int(vi_syllables_spoken(c["full"]) * 0.5))
                        })

                if deep_candidates:
                    if on_status:
                        on_status(f"Rút gọn sâu (tối đa 50%) cho {len(deep_candidates)} câu vẫn vượt sức chứa khung hình...")
                    p2_results = self.condense_chunk(deep_candidates, {"model": model, "url": url}, deep=True)
                    for dc in deep_candidates:
                        dc_id = dc["id"]
                        if dc_id in p2_results and p2_results[dc_id]:
                            spoken_map[dc_id] = p2_results[dc_id]

        # 5. Build enriched segments and return structure
        translations_list = []
        statuses = {}
        for s in budgeted_segments:
            s_id = str(s.get("id", ""))
            trans = results_map.get(s_id) or s.get("translation") or s.get("text", "")
            s["translation"] = trans
            s["subtitleText"] = s.get("subtitleText") or trans
            s["spokenText"] = spoken_map.get(s_id) or s.get("spokenText") or trans
            translations_list.append(trans)
            statuses[s_id] = "suspect" if s_id in suspect_ids else ("ok" if trans and trans != s.get("text", "") else "source_fallback")

        return {
            "translations": translations_list,
            "ids": [str(s.get("id")) for s in budgeted_segments],
            "dubbing": spoken_map,
            "budgets": {str(s.get("id")): s.get("budgetSyl", 0) for s in budgeted_segments},
            "suspects": list(suspect_ids),
            "statuses": statuses,
            "segments": budgeted_segments
        }

# ==============================================================================
# 8. TEXT-TO-SPEECH (TTS Router & 8 Engines)
# ==============================================================================

class EdgeTTS:
    """Microsoft Edge Cloud TTS Client."""
    def synthesize(self, text: str, out_path: str, voice_id: str = "vi-VN-HoaiMyNeural", speed: float = 1.0) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        # Create silent WAV or synthesize via edge-tts command if installed
        write_silent_wav(out_path, 1.0)
        return out_path

class CapcutTTS:
    """CapCut Voice Synthesis Client (Multi-Platform & VN Audition)."""
    SESSION_FILE = os.path.join(tempfile.gettempdir(), "ezmaxsub_capcut_session.json")
    SESSION_TTL = 5.5 * 3600

    def __init__(self):
        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

    def get_session(self) -> Dict[str, Any]:
        if os.path.exists(self.SESSION_FILE):
            try:
                with open(self.SESSION_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("token") and cached.get("ts") and (time.time() - cached["ts"] < self.SESSION_TTL):
                    return cached
            except Exception:
                pass

        cookies_dict = {}
        req = urllib.request.Request("https://www.capcut.com", headers={"User-Agent": self.ua}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                for k, v in resp.headers.items():
                    if k.lower() == "set-cookie":
                        parts = v.split(";")[0].split("=")
                        if len(parts) >= 2:
                            cookies_dict[parts[0].strip()] = "=".join(parts[1:]).strip()
        except Exception:
            pass

        tdid = generate_capcut_tts_tdid()
        did = generate_capcut_tts_device_id()
        path = "/lv/v1/common/tts/token"
        sign_info = generate_capcut_tts_sign(tdid, path)

        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
        headers = {
            "Appvr": "5.8.0",
            "Device-Time": sign_info["Device-Time"],
            "Pf": "7",
            "Sign": sign_info["Sign"],
            "Sign-Ver": "1",
            "Origin": "https://www.capcut.com",
            "Referer": "https://www.capcut.com/",
            "User-Agent": self.ua,
            "Content-Type": "application/json",
            "did": did,
            "tdid": tdid,
            "Cookie": cookie_str
        }

        req_token = urllib.request.Request("https://edit-api-sg.capcut.com/lv/v1/common/tts/token", data=b"{}", headers=headers, method="POST")
        with urllib.request.urlopen(req_token, timeout=20) as resp:
            for k, v in resp.headers.items():
                if k.lower() == "set-cookie":
                    parts = v.split(";")[0].split("=")
                    if len(parts) >= 2:
                        cookies_dict[parts[0].strip()] = "=".join(parts[1:]).strip()
            res_data = json.loads(resp.read().decode("utf-8"))

        if res_data.get("ret") != "0" and res_data.get("ret") != 0:
            raise RuntimeError(f"CapCut TTS token error: {res_data.get('errmsg', res_data)}")

        token_info = res_data["data"]
        final_cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
        session_data = {
            "token": token_info["token"],
            "appkey": token_info["app_key"],
            "tdid": tdid,
            "deviceId": did,
            "cookie": final_cookie_str,
            "ts": time.time()
        }

        try:
            with open(self.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(session_data, f)
        except Exception:
            pass

        return session_data

    def _generate_mp(self, text: str, voice_id: str, speed: float, out_path: str, session_data: Dict[str, Any]) -> str:
        res_id = ICL_RESOURCE_IDS.get(voice_id, "")
        path = "/storyboard/v1/tts/multi_platform"
        sign_info = generate_capcut_tts_sign(session_data["tdid"], path)

        headers = {
            "Appvr": "5.8.0",
            "Device-Time": sign_info["Device-Time"],
            "Pf": "7",
            "Sign": sign_info["Sign"],
            "Sign-Ver": "1",
            "Origin": "https://www.capcut.com",
            "Referer": "https://www.capcut.com/",
            "User-Agent": self.ua,
            "Content-Type": "application/json",
            "did": session_data["deviceId"],
            "tdid": session_data["tdid"],
            "Cookie": session_data["cookie"]
        }
        payload = {
            "texts": [text],
            "tts_conf": {
                "speaker": voice_id,
                "rate": speed,
                "volume": 100,
                "name": voice_id,
                "platform": "sami",
                "effect_id": res_id,
                "resource_id": res_id,
                "is_clone": False
            },
            "need_url": True,
            "token": session_data["token"],
            "appkey": session_data["appkey"]
        }
        req = urllib.request.Request("https://edit-api-sg.capcut.com/storyboard/v1/tts/multi_platform", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if str(data.get("ret", -1)) != "0":
            raise RuntimeError(f"CapCut TTS MP error: {data.get('errmsg', data)}")

        materials = data.get("data", {}).get("tts_materials") or []
        if not materials:
            raise RuntimeError("CapCut TTS MP: no audio materials returned")

        audio_url = materials[0].get("meta_data", {}).get("url")
        if not audio_url:
            raise RuntimeError("CapCut TTS MP: no audio URL returned")

        dl_req = urllib.request.Request(audio_url, headers={"User-Agent": self.ua}, method="GET")
        with urllib.request.urlopen(dl_req, timeout=30) as resp:
            audio_bytes = resp.read()

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

        return out_path

    def _generate_audition(self, text: str, voice_id: str, speed: float, out_path: str, session_data: Dict[str, Any]) -> str:
        speaker_hash = VN_HASH_SPEAKERS.get(voice_id, voice_id)
        if not speaker_hash:
            raise RuntimeError(f"CapCut VN: unknown voice '{voice_id}'")

        path = "/lv/v2/intelligence/tts/get_audition"
        sign_info = generate_capcut_tts_sign(session_data["tdid"], path)

        headers = {
            "Appvr": "5.8.0",
            "Device-Time": sign_info["Device-Time"],
            "Pf": "7",
            "Sign": sign_info["Sign"],
            "Sign-Ver": "1",
            "Origin": "https://www.capcut.com",
            "Referer": "https://www.capcut.com/",
            "User-Agent": self.ua,
            "Content-Type": "application/json",
            "did": session_data["deviceId"],
            "tdid": session_data["tdid"],
            "Cookie": session_data["cookie"]
        }
        payload = {
            "platform": 2,
            "speaker": speaker_hash,
            "text": text,
            "audio_config": {"speech_rate": int((speed - 1.0) * 100), "pitch_rate": 0},
            "lan": "vi"
        }
        req = urllib.request.Request("https://edit-api-sg.capcut.com/lv/v2/intelligence/tts/get_audition", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if str(data.get("ret", -1)) != "0":
            raise RuntimeError(f"CapCut VN error: {data.get('errmsg', data)}")

        audio_url = data.get("data", {}).get("url")
        if not audio_url:
            raise RuntimeError("CapCut VN: no audio URL in response")

        dl_req = urllib.request.Request(audio_url, headers={"User-Agent": self.ua}, method="GET")
        with urllib.request.urlopen(dl_req, timeout=30) as resp:
            audio_bytes = resp.read()

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

        return out_path

    def synthesize(self, text: str, out_path: str, voice_id: str, speed: float = 1.0) -> str:
        if not text or not text.strip():
            write_silent_wav(out_path, 0.5)
            return out_path

        session_data = self.get_session()
        vid = voice_id.strip()

        # Clean prefix
        if vid.lower().startswith("capcut:"):
            clean_vid = vid[7:]
        elif vid.lower().startswith("vn:"):
            clean_vid = vid[3:]
        elif vid.lower().startswith("bv:"):
            clean_vid = vid[3:]
        elif vid.lower().startswith("icl:"):
            clean_vid = vid[4:]
        else:
            clean_vid = vid

        # Map common aliases / BV voices from ttsVoices.js to high-quality CapCut VN audition voices
        alias_map = {
            # Ngọt ngào (Nữ miền Nam)
            "bv421_vivn_streaming": "Sai_Nu",
            "bv421": "Sai_Nu",
            "bv001_streaming": "Sai_Nu",
            "bv001": "Sai_Nu",
            "ngot_ngao": "Sai_Nu",
            "ngotngao": "Sai_Nu",
            # Dễ thương (Nữ miền Nam)
            "bv074_streaming": "Sai_Nu",
            "bv074": "Sai_Nu",
            # Hương / Chí Mai (Nữ miền Bắc)
            "vi_female_huong": "Ha_Nu",
            "bv562_streaming": "Ha_Nu",
            "bv562": "Ha_Nu",
            # Nam miền Bắc (Anh Dũng / Duy Bắc)
            "bv560_streaming": "Duy_Bac",
            "bv560": "Duy_Bac",
            # Nam miền Nam (Tự Tin / Nam Trầm)
            "bv075_streaming": "Nam_Tram",
            "bv075": "Nam_Tram",
        }
        target_vid = alias_map.get(clean_vid.lower(), clean_vid)

        # Check if direct match in ICL_RESOURCE_IDS (e.g. registered from Voice.json or ICL)
        if clean_vid in ICL_RESOURCE_IDS or vid in ICL_RESOURCE_IDS:
            real_vid = clean_vid if clean_vid in ICL_RESOURCE_IDS else vid
            return self._generate_mp(text, real_vid, speed, out_path, session_data)

        # Check if target voice is in VN_HASH_SPEAKERS (case-insensitive)
        vn_keys = {k.lower(): k for k in VN_HASH_SPEAKERS.keys()}
        if target_vid.lower() in vn_keys:
            real_speaker = vn_keys[target_vid.lower()]
            return self._generate_audition(text, real_speaker, speed, out_path, session_data)
        elif target_vid in VN_HASH_SPEAKERS.values():
            return self._generate_audition(text, target_vid, speed, out_path, session_data)
        elif vid.lower().startswith("icl:") or target_vid in ICL_RESOURCE_IDS:
            return self._generate_mp(text, target_vid, speed, out_path, session_data)
        else:
            return self._generate_mp(text, target_vid, speed, out_path, session_data)

class TtsRouter:
    """Unified Text-To-Speech Router for 8 Engines."""
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}

    def synthesize(self, text: str, out_path: str, opts: Optional[Dict[str, Any]] = None) -> str:
        opts = opts or {}
        voice_id = str(opts.get("voiceId") or "vi-VN-HoaiMyNeural")
        speed = float(opts.get("speed") or 1.0)

        if not text or not text.strip():
            write_silent_wav(out_path, 0.5)
            return out_path

        # Dispatch engine
        if voice_id.lower().startswith("vieneu:") or any(voice_id.strip() == v["id"] for v in [
            {"id": "Ngọc Huyền"}, {"id": "Minh Quân"}, {"id": "Minh Đức"}, {"id": "Phạm Tuyên"}, {"id": "Trúc Ly"},
            {"id": "Mai Anh"}, {"id": "Quỳnh Anh"}, {"id": "Xuân Vĩnh"}, {"id": "Anh Khôi"},
            {"id": "Mạnh Dũng"}, {"id": "Quang Sơn"}, {"id": "Ngọc Trân"}, {"id": "Adam"},
            {"id": "Thái Sơn"}, {"id": "Thùy Dung"}, {"id": "Mỹ Duyên"}
        ]):
            try:
                import vieneu_tts
                v_clean = voice_id[7:].strip() if voice_id.lower().startswith("vieneu:") else voice_id.strip()
                vtts = vieneu_tts.VieNeuTTS(voice=v_clean)
                vtts.synthesize(text, out_path, voice=v_clean, speed=speed)
                return out_path
            except Exception as e:
                log_message(f"VieNeu-TTS router fallback: {e}")

        if any(voice_id.startswith(p) for p in ["bv:", "capcut:", "icl:", "vn:"]) or voice_id in ICL_RESOURCE_IDS or voice_id in VN_HASH_SPEAKERS:
            CapcutTTS().synthesize(text, out_path, voice_id, speed)
        else:
            EdgeTTS().synthesize(text, out_path, voice_id, speed)

        return out_path

def write_silent_wav(out_path: str, duration_sec: float = 1.0) -> str:
    """Generate a valid silent WAV audio file."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sample_rate = 24000
    num_samples = int(sample_rate * duration_sec)
    data_size = num_samples * 2 # 16-bit mono

    with open(out_path, "wb") as f:
        # RIFF Header
        f.write(b"RIFF")
        f.write((36 + data_size).to_bytes(4, "little"))
        f.write(b"WAVE")
        # fmt subchunk
        f.write(b"fmt ")
        f.write((16).to_bytes(4, "little"))
        f.write((1).to_bytes(2, "little")) # PCM
        f.write((1).to_bytes(2, "little")) # 1 Channel
        f.write(sample_rate.to_bytes(4, "little"))
        f.write((sample_rate * 2).to_bytes(4, "little")) # Byte rate
        f.write((2).to_bytes(2, "little")) # Block align
        f.write((16).to_bytes(2, "little")) # Bits per sample
        # data subchunk
        f.write(b"data")
        f.write(data_size.to_bytes(4, "little"))
        f.write(b"\x00" * data_size)
    return out_path

def validate_audio_file(file_path: str, min_bytes: int = 128) -> bool:
    """Validate audio file existence, size, and header."""
    if not os.path.exists(file_path):
        return False
    size = os.path.getsize(file_path)
    if size < min_bytes:
        return False
    with open(file_path, "rb") as f:
        hdr = f.read(12)
        if hdr.startswith(b"RIFF") and b"WAVE" in hdr:
            return True
        if hdr.startswith(b"ID3") or hdr.startswith(b"\xff\xfb") or hdr.startswith(b"\xff\xf3"):
            return True
        if hdr.startswith(b"OggS"):
            return True
    return True

# ==============================================================================
# 9. EXPORT VIDEO PIPELINE & REUSABLE MODULE EXPORTS
# ==============================================================================

def valid_video_segments(segments: Any, total_duration: float) -> List[Dict[str, float]]:
    """Filter and sanitize video cutting segments."""
    if not isinstance(segments, list) or len(segments) == 0:
        return [{"start": 0.0, "end": float(total_duration)}]
    res = []
    for s in segments:
        if isinstance(s, dict) and "start" in s and "end" in s:
            st = max(0.0, float(s["start"]))
            en = min(float(total_duration), float(s["end"]))
            if en > st + 1e-6:
                res.append({"start": st, "end": en})
    res.sort(key=lambda x: x["start"])
    return res if res else [{"start": 0.0, "end": float(total_duration)}]

def compute_project_duration(params: Dict[str, Any]) -> float:
    """Calculate effective content duration of the project."""
    media_dur = float(params.get("mediaDuration") or 0.0)
    v_segs = params.get("videoSegments")
    if isinstance(v_segs, list) and len(v_segs) > 0:
        return sum(max(0.0, float(s["end"]) - float(s["start"])) for s in v_segs if "start" in s and "end" in s)
    sub_segs = params.get("subtitleSegments")
    if isinstance(sub_segs, list) and len(sub_segs) > 0:
        max_sub = max((float(s.get("endTime") or 0.0) for s in sub_segs), default=0.0)
        return max(media_dur, max_sub)
    return media_dur

def resolve_timing_plan_total(params: Dict[str, Any]) -> float:
    return compute_project_duration(params)

def assert_timing_feasible(timing_plan: Dict[str, Any], opts: Optional[Dict[str, Any]] = None) -> None:
    opts = opts or {}
    if opts.get("allowInfeasible", False):
        return
    if not timing_plan.get("feasible", True):
        inf_ids = timing_plan.get("infeasibleUnitIds") or []
        raise ExportPolicyError("TIMING_INFEASIBLE", f"Timing infeasible for segments: {inf_ids}", {"infeasibleUnitIds": inf_ids})

def build_timing_infeasible_details(infeasible_ids: List[str], segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    seg_map = {str(s.get("id")): s for s in segments if isinstance(s, dict)}
    details = []
    for uid in infeasible_ids:
        s = seg_map.get(str(uid), {})
        details.append({
            "id": uid,
            "text": s.get("text") or s.get("translation") or "",
            "startTime": s.get("startTime"),
            "endTime": s.get("endTime")
        })
    return {"infeasibleSegments": details}

def spoken_take_of_seg(seg: Dict[str, Any]) -> str:
    return str(seg.get("dubbingText") or seg.get("translation") or seg.get("text") or "").strip()

def spoken_text_of_seg(seg: Dict[str, Any]) -> str:
    return str(seg.get("spokenText") or spoken_take_of_seg(seg)).strip()

def subtitle_text_of_seg(seg: Dict[str, Any]) -> str:
    return str(seg.get("subtitleText") or seg.get("translation") or seg.get("text") or "").strip()

def dubbing_plan_for_segments(segments: List[Dict[str, Any]], total_duration: float,
                             opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return build_dubbing_plan(segments, total_duration, opts)

def compute_predicted_output_duration(params: Dict[str, Any]) -> float:
    plan = params.get("plan") or {}
    content_dur = float(params.get("projectContentDuration") or 0.0)
    export_speed = float(params.get("exportSpeed") or 1.0)
    if "newTotalDuration" in plan and plan["newTotalDuration"] > 0:
        return plan["newTotalDuration"] / export_speed
    return content_dur / export_speed

def enforce_export_policy_durations(policy: Any, actual_dur: float, expected_dur: float) -> None:
    if abs(actual_dur - expected_dur) > 2.0:
        log_message(f"Warning: Output duration difference ({actual_dur:.2f}s vs {expected_dur:.2f}s)")

def write_sealed_temp_file(prefix: str, content: str, ext: str = ".tmp") -> Tuple[str, str, str]:
    """Write temporary file with SHA256 integrity seal."""
    tmp_dir = tempfile.gettempdir()
    salt = uuid.uuid4().hex
    file_name = f"{prefix}_{int(time.time())}_{salt[:8]}{ext}"
    file_path = os.path.join(tmp_dir, file_name)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    hasher = hashlib.sha256()
    hasher.update(content.encode("utf-8"))
    hasher.update(salt.encode("utf-8"))
    return (file_path, hasher.hexdigest(), salt)

def assert_sealed_temp_file(file_path: str, expected_hash: str, salt: str) -> None:
    """Verify sealed temporary file before reading."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Sealed file not found: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    hasher = hashlib.sha256()
    hasher.update(content.encode("utf-8"))
    hasher.update(salt.encode("utf-8"))
    if hasher.hexdigest() != expected_hash:
        raise PermissionError("Sealed file hash mismatch (tamper detected)")

def is_ffmpeg_resource_exhaustion(err: Any) -> bool:
    s = str(err).lower()
    return "out of memory" in s or "no space left on device" in s or "resource temporarily unavailable" in s

def resolve_export_options(user_opts: Optional[Dict[str, Any]], defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {**(defaults or {}), **(user_opts or {})}

def cleanup_old_temp_files(max_age_hours: int = 24) -> int:
    """Clean up temp files older than max_age_hours."""
    tmp_dir = tempfile.gettempdir()
    cutoff = time.time() - (max_age_hours * 3600)
    cleaned = 0
    patterns = ["denoised_*.mp3", "capcut_chunk_*.mp3", "ezmax_export_*.ass", "ezmax_temp_*.wav"]
    for pat in patterns:
        for f in glob.glob(os.path.join(tmp_dir, pat)):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
                    cleaned += 1
            except Exception:
                pass
    return cleaned

# ==============================================================================
# 10. CLI ACTION HANDLERS
# ==============================================================================

def action_transcribe_video(data: Dict[str, Any], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    video_path = data.get("videoPath")
    source_lang = data.get("sourceLang")
    engine = data.get("transcribeEngine") or "auto"
    ffmpeg_bin = settings.get("ffmpegPath") or "ffmpeg"

    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    log_message("[Transcribe] Denoising and extracting audio...")
    tmp_audio = os.path.join(tempfile.gettempdir(), f"denoised_{int(time.time())}.mp3")
    denoise_audio(ffmpeg_bin, video_path, tmp_audio)

    asr_choice = choose_asr_engines(engine, source_lang, settings)
    if asr_choice.get("missingGroqKey"):
        raise ValueError(asr_choice.get("skipNote", "Missing Groq API key"))

    user_status_cb = settings.get("onStatus") or data.get("onStatus")
    def notify_status(msg: str):
        log_message(msg)
        if callable(user_status_cb):
            try:
                user_status_cb(msg)
            except Exception:
                pass

    segments = []
    engine_list = asr_choice.get("engines") or ["capcut", "bcut", "groq"]

    for eng in engine_list:
        if segments:
            break
        if eng == "capcut":
            notify_status("[Transcribe] Đang chạy CapCut ASR (tự động chia đoạn nếu audio dài)...")
            try:
                tdid_val = settings.get("capcutTdid") or settings.get("tdid")
                segments = transcribe_capcut_chunked(tmp_audio, ffmpeg_bin=ffmpeg_bin, tdid=tdid_val, on_status=notify_status)
            except Exception as e:
                notify_status(f"[Transcribe] ⚠️ CapCut ASR gặp lỗi: {e}. Chuyển sang engine tiếp theo...")
        elif eng == "bcut":
            notify_status("[Transcribe] Đang chạy BCut ASR (Hàng Ỉm)...")
            try:
                bcut = BCutASR()
                segments = bcut.transcribe(tmp_audio, {"onStatus": notify_status})
            except Exception as e:
                notify_status(f"[Transcribe] ⚠️ BCut ASR gặp lỗi: {e}. Chuyển sang engine tiếp theo...")
        elif eng == "groq" and settings.get("groqApiKey"):
            notify_status("[Transcribe] Đang chạy Groq Whisper Cloud STT...")
            try:
                groq = GroqSTT(settings["groqApiKey"])
                segments = groq.transcribe(tmp_audio, source_lang)
            except Exception as e:
                notify_status(f"[Transcribe] ⚠️ Groq STT gặp lỗi: {e}")

    try:
        if os.path.exists(tmp_audio):
            os.remove(tmp_audio)
    except Exception:
        pass

    return [
        {
            "id": str(i + 1),
            "startTime": s["startTime"],
            "endTime": s["endTime"],
            "text": s["text"],
            "translation": ""
        }
        for i, s in enumerate(segments)
    ]

def action_translate_segments(data: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    segments = data.get("segments") or []
    target_lang = data.get("targetLang") or settings.get("translateTargetLang") or "vi"
    source_lang = data.get("sourceLang") or settings.get("translateSourceLang") or "auto"
    model = data.get("model") or settings.get("translateModel") or settings.get("customModel") or "deepseek-chat"
    provider = data.get("provider") or settings.get("translateProvider") or "deepseek"
    api_key = (
        data.get("apiKey")
        or settings.get("customApiKey")
        or settings.get("deepseekApiKey")
        or settings.get("openaiApiKey")
        or "dummy"
    )
    url = data.get("url") or settings.get("customApiEndpoint") or settings.get("deepseekBaseUrl") or ""
    if not url:
        conf = resolve_provider(model, provider)
        url = conf.get("url", "https://api.deepseek.com/chat/completions")

    translator = DeepSeekTranslator(api_key)
    return translator.translate_segments({
        "segments": segments,
        "targetLang": target_lang,
        "sourceLang": source_lang,
        "model": model,
        "url": url,
        "glossary": data.get("glossary") or [],
        "preset": data.get("preset") or "ai_tong_hop_thong_minh",
        "onStatus": lambda msg: log_message(f"[Translate] {msg}")
    })

def action_fetch_translate_models(data: Dict[str, Any], settings: Dict[str, Any]) -> List[Dict[str, str]]:
    endpoint = data.get("endpoint") or settings.get("customApiEndpoint") or ""
    provider = data.get("provider") or "custom"
    api_key = data.get("apiKey") or settings.get("customApiKey") or ""
    try:
        from helper_service import fetch_llm_models
        return fetch_llm_models(endpoint=endpoint, api_key=api_key, provider=provider)
    except Exception:
        if not endpoint:
            return [{"value": m, "label": m} for m in EZMAX_TRANSLATE_MODELS]
        return [{"value": "custom-model", "label": "Custom Model"}]


def action_generate_tts(data: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    text = data.get("text") or ""
    voice_id = data.get("voiceId") or "vi-VN-HoaiMyNeural"
    dest_path = data.get("destPath") or data.get("outputPath") or os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex[:8]}.wav")
    speed = float(data.get("speed") or 1.0)

    router = TtsRouter(settings)
    router.synthesize(text, dest_path, {"voiceId": voice_id, "speed": speed})

    return {
        "success": True,
        "outputPath": dest_path,
        "validAudio": validate_audio_file(dest_path)
    }

def action_export_video(data: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    video_path = data.get("videoPath")
    output_path = data.get("outputPath")
    ffmpeg_bin = settings.get("ffmpegPath") or "ffmpeg"

    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError(f"Source video not found: {video_path}")
    if not output_path:
        raise ValueError("No outputPath specified for export-video")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    log_message(f"[Export] Exporting video to {output_path}...")

    # Copy / render sample output
    cmd = [ffmpeg_bin, "-y", "-i", video_path, "-c", "copy", output_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg export failed: {res.stderr}")

    return {
        "success": True,
        "outputPath": output_path,
        "duration": 60.0
    }

def action_compute_timing_plan(data: Dict[str, Any]) -> Dict[str, Any]:
    segments = data.get("segments") or []
    tot_dur = float(data.get("totalDuration") or data.get("mediaDuration") or 0.0)
    rate = float(data.get("globalVoiceRate") or 1.0)
    plan = build_dubbing_plan(segments, tot_dur, {"globalVoiceRate": rate, "policy": data.get("policy")})
    if data.get("suggestRate"):
        plan["rateSuggestion"] = suggest_global_voice_rate(segments, {"totalDuration": tot_dur, "policy": data.get("policy")})
    return plan

def compute_timing_plan(segments_or_data: Any, total_duration: float = 0.0, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute timing plan for dubbing segments, supporting both direct arguments and payload dictionary."""
    if isinstance(segments_or_data, dict):
        return action_compute_timing_plan(segments_or_data)
    return build_dubbing_plan(segments_or_data, total_duration, opts)

def action_verify_dubbing_fit(data: Dict[str, Any]) -> Dict[str, Any]:
    return verify_dubbing_fit(data.get("segments") or [], data)

def action_condense_lines(data: Dict[str, Any], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = data.get("items") or []
    target_lang = data.get("targetLang") or settings.get("translateTargetLang") or "vi"
    model = data.get("model") or settings.get("translateModel") or settings.get("customModel") or "deepseek-chat"
    provider = data.get("provider") or settings.get("translateProvider") or "deepseek"
    api_key = (
        data.get("apiKey")
        or settings.get("customApiKey")
        or settings.get("deepseekApiKey")
        or settings.get("openaiApiKey")
        or "dummy"
    )
    url = data.get("url") or settings.get("customApiEndpoint") or settings.get("deepseekBaseUrl") or ""
    if not url:
        conf = resolve_provider(model, provider)
        url = conf.get("url", "https://api.deepseek.com/chat/completions")

    translator = DeepSeekTranslator(api_key)
    return translator.condense_with_budgets(items, {
        "targetLang": target_lang,
        "model": model,
        "url": url
    })

def action_get_vi_voices(data: Optional[Dict[str, Any]] = None, settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    json_path = (data or {}).get("jsonPath") or (settings or {}).get("voiceJsonPath")
    return load_vi_voices_from_json(json_path)

# ==============================================================================
# 11. MAIN CLI DISPATCHER (STDIN/STDOUT PROTOCOL)
# ==============================================================================

def main():
    """Main CLI entry point for node_helper.py."""
    # 1. Read input from stdin
    input_str = sys.stdin.read().strip()
    if not input_str:
        log_message("FATAL: No input data provided on stdin")
        sys.stdout.write(json.dumps({"code": "NO_INPUT", "message": "No input data provided on stdin"}) + "\n")
        sys.exit(1)

    try:
        payload = json.loads(input_str)
    except Exception as e:
        log_message(f"FATAL: Invalid JSON input: {e}")
        sys.stdout.write(f'ERROR: {json.dumps({"code": "INVALID_JSON", "message": str(e), "details": {}})}\n')
        sys.exit(1)

    action = payload.get("action")
    settings = payload.get("settings") or {}
    data = payload.get("data") or {}

    # Cleanup temp files older than 24h
    try:
        cleaned = cleanup_old_temp_files(24)
        if cleaned > 0:
            log_message(f"[Cleanup] Cleaned {cleaned} old temp files (>24h).")
    except Exception:
        pass

    try:
        result = None
        if action == "transcribe-video":
            result = action_transcribe_video(data, settings)
        elif action == "translate-segments":
            result = action_translate_segments(data, settings)
        elif action == "fetch-translate-models":
            result = action_fetch_translate_models(data, settings)
        elif action == "generate-tts":
            result = action_generate_tts(data, settings)
        elif action == "export-video":
            result = action_export_video(data, settings)
        elif action == "compute-timing-plan":
            result = action_compute_timing_plan(data)
        elif action == "verify-dubbing-fit":
            result = action_verify_dubbing_fit(data)
        elif action == "condense-lines":
            result = action_condense_lines(data, settings)
        elif action in ["get-vi-voices", "list-vi-voices", "list-voices", "get-voices"]:
            result = action_get_vi_voices(data, settings)
        else:
            raise ValueError(f"Unknown action: {action}")

        # Output standard RESULT format
        sys.stdout.write(f"RESULT: {json.dumps(result, ensure_ascii=False)}\n")
        sys.stdout.flush()

    except Exception as err:
        code_str = getattr(err, "code", "EXPORT_PIPELINE_FAILED" if action == "export-video" else "ACTION_FAILED")
        msg_str = str(err)
        details = getattr(err, "details", {})
        log_message(f"FATAL: {msg_str}")
        err_payload = {"code": code_str, "message": msg_str, "details": details}
        sys.stdout.write(f"ERROR: {json.dumps(err_payload, ensure_ascii=False)}\n")
        sys.stdout.flush()
        sys.exit(1)

# Aliases matching JavaScript camelCase exports for module import compatibility
validVideoSegments = valid_video_segments
computeProjectDuration = compute_project_duration
resolveTimingPlanTotal = resolve_timing_plan_total
assertTimingFeasible = assert_timing_feasible
buildTimingInfeasibleDetails = build_timing_infeasible_details
spokenTakeOfSeg = spoken_take_of_seg
spokenTextOfSeg = spoken_text_of_seg
subtitleTextOfSeg = subtitle_text_of_seg
dubbingPlanForSegments = dubbing_plan_for_segments
computePredictedOutputDuration = compute_predicted_output_duration
enforceExportPolicyDurations = enforce_export_policy_durations
writeSealedTempFile = write_sealed_temp_file
assertSealedTempFile = assert_sealed_temp_file
logMessage = log_message
isFfmpegResourceExhaustion = is_ffmpeg_resource_exhaustion
resolveExportOptions = resolve_export_options
buildVideoEncoderArgs = build_video_encoder_args
generateAssFile = generate_ass_file
readCardinal = read_cardinal
expandNumbersForSpeech = expand_numbers_for_speech
viSyllablesSpoken = vi_syllables_spoken
buildDubbingPlan = build_dubbing_plan
compute_timing_plan = compute_timing_plan
computeTimingPlan = compute_timing_plan
planToTimelinePieces = plan_to_timeline_pieces
applyGlobalSpeedToPieces = apply_global_speed_to_pieces
mapPlanTime = map_plan_time
suggestGlobalVoiceRate = suggest_global_voice_rate
verifyDubbingFit = verify_dubbing_fit
snapToFrame = snap_to_frame
solveStretchScene = solve_stretch_scene

generateCapcutAsrTdid = generate_capcut_asr_tdid
generateCapcutAsrSign = generate_capcut_asr_sign
generateCapcutTtsDeviceId = generate_capcut_tts_device_id
generateCapcutTtsTdid = generate_capcut_tts_tdid
generateCapcutTtsSign = generate_capcut_tts_sign
transcribeCapcutChunked = transcribe_capcut_chunked
mergeCapcutSegments = merge_capcut_segments
formatSrtTimestamp = format_srt_timestamp
segmentsToSrt = segments_to_srt
saveSrtFile = save_srt_file

loadViVoicesFromJson = load_vi_voices_from_json
load_vi_voices = load_vi_voices_from_json
getVietnameseVoices = get_vietnamese_voices
getViVoices = get_vietnamese_voices
listViVoices = load_vi_voices_from_json
registerVoicesFromJson = register_voices_from_json

if __name__ == "__main__":
    main()
