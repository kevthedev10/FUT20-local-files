from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import logging
import random
import re
import secrets
import html
import socket
import socketserver
import sqlite3
import struct
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from config_loader import PROJECT_ROOT, load_config
from sbc_catalog import CHALLENGE_BY_ID, SBC_CATALOG, SET_BY_ID
import objectives as SCMP
import player_pick_state as PICK_STATE

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except Exception as _crypto_import_error:
    padding = Cipher = algorithms = modes = None
else:
    _crypto_import_error = None

try:
    from PIL import Image
except Exception as _pillow_import_error:
    Image = None
else:
    _pillow_import_error = None

APP_NAME = "FIFA20 Local FUT FIX22-RETAIL-SYSTEMS-COMPLETE"
VERSION = "1.0.32-LIVE-OBJECTIVES-MAPPED-SOURCE"
ROOT = Path(__file__).resolve().parent
CONTENT_ROOT = ROOT / "contentfifa"
TOTW_ARCHIVE_PATH = ROOT / "totw_archive.json"
with TOTW_ARCHIVE_PATH.open("r", encoding="utf-8") as _totw_archive_file:
    _TOTW_ARCHIVE = json.load(_totw_archive_file)
_TOTW_SQUADS = tuple(_TOTW_ARCHIVE.get("squads") or ())
_TOTW_BY_ID = {int(entry["id"]): entry for entry in _TOTW_SQUADS}
_TOTW_LATEST_ID = max(_TOTW_BY_ID) if _TOTW_BY_ID else 1
_TOTW_FIRST_WEEK = int(_TOTW_ARCHIVE.get("firstRealWeek") or (min(_TOTW_BY_ID) if _TOTW_BY_ID else 1))
_TOTW_LAST_WEEK = int(_TOTW_ARCHIVE.get("lastRealWeek") or _TOTW_LATEST_ID)
_TOTW_GAP_WEEKS = tuple(sorted(
    int(week) for week in (_TOTW_ARCHIVE.get("missingRegularWeeks") or ())
    if _TOTW_FIRST_WEEK <= int(week) <= _TOTW_LAST_WEEK
))
# The retail FIFA 20 selector adds two live/current featured-squad cards on
# top of the numeric history array.  Returning the latest two weeks in history
# as well therefore duplicates the current squad and produces WEEK 46/47 and
# WEEK 47/47.  Keep those two published weeks out of history and let the native
# live slots represent them instead.
_TOTW_NATIVE_LIVE_SLOTS = 2
_TOTW_HISTORY_LAST_WEEK = max(_TOTW_FIRST_WEEK, _TOTW_LAST_WEEK - _TOTW_NATIVE_LIVE_SLOTS)
_TOTW_HISTORY_IDS = tuple(range(_TOTW_FIRST_WEEK, _TOTW_HISTORY_LAST_WEEK + 1))
_TOTW_ZERO_ALIAS_ID = max(_TOTW_FIRST_WEEK, _TOTW_LATEST_ID - 1)
CFG = load_config(PROJECT_ROOT)
# Keep runtime state outside the FIFA installation directory. FIFA is commonly
# installed under Program Files, where a normal user cannot create SQLite/log
# files. The root launcher-settings.json file is the primary configuration
# source; localfut20/config.json remains a legacy fallback for standalone use.
RUNTIME_ROOT = Path(str(CFG["runtime_root"]))
DATA = RUNTIME_ROOT / "data"
LOGS = RUNTIME_ROOT / "logs"
CONTENT_CACHE = RUNTIME_ROOT / "content-cache"
DB_PATH = Path(str(CFG["database_path"]))
CONFIG_PATH = Path(str(CFG.get("_config_path") or (PROJECT_ROOT / "launcher-settings.json")))

DATA.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
CONTENT_CACHE.mkdir(parents=True, exist_ok=True)


def mock_account_age() -> int:
    """Return the age of the local test account from its test-only DOB."""
    dob = _dt.date.fromisoformat(str(CFG["date_of_birth"]))
    today = _dt.date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def mock_account_is_under_age() -> bool:
    return mock_account_age() < int(CFG["minimum_age"])


PACK_SETTINGS_PATH = ROOT / "pack_settings.json"

# Rarity weights are applied per promo family, never per card.  The previous
# list omitted FUT Birthday (30) and Shapeshifters (97) entirely and gave the
# one-card Record Breaker family enough weight to repeat Aguero constantly.
# These pools make every major FIFA 20 promo reachable while keeping MOTM and
# singleton families uncommon. Plain Icons (12) and iMOTM (15) stay excluded;
# Icon Moments (84) are the supported icon promo.
DEFAULT_SPECIAL_WEIGHTS = {
    "3":34, "4":2, "5":1, "6":0.15, "8":3, "21":3, "22":3,
    "24":2, "25":1, "28":0.5, "30":8, "32":2, "45":2, "46":3,
    "48":4, "49":1, "50":4, "51":3, "58":3, "60":3, "61":4,
    "64":2, "65":3, "66":10, "68":1, "70":2, "71":4, "72":3,
    "73":0.35, "81":3, "84":1, "90":3, "95":4, "97":8,
}
PROMO_SPECIAL_WEIGHTS = {
    "3":14, "4":2, "5":3, "6":0.10, "8":2, "21":4, "22":4,
    "24":3, "25":2, "28":0.5, "30":13, "32":3, "45":3, "46":5,
    "48":5, "49":2, "50":6, "51":5, "58":5, "60":4, "61":6,
    "64":3, "65":4, "66":13, "68":2, "70":3, "71":5, "72":4,
    "73":0.25, "81":4, "84":2.5, "90":4, "95":6, "97":13,
}

# Reported catalog defects. Keep existing owned cards intact, but exclude
# every Ansu Fati and Franck Ribery version from new packs and picks.
PACK_EXCLUDED_RESOURCE_IDS = {
    253_004, 50_584_652, 67_361_868,
    156_616, 50_488_264, 84_042_696,
}
PACK_EXCLUDED_ASSET_IDS = {253_004, 156_616}

DEFAULT_PACK_SETTINGS = {
    "next_pack_id": 28,
    "packs": {
        "1": {"enabled": True, "name":"Bronze Pack","bio":"Twelve Bronze players with one rare.","price":400,"size":12,"gold_weight":0,"silver_weight":0,"bronze_weight":100,"rare_slots":1,"special_chance":0.0,"store_group":"bronze","special_weights":{}},
        "2": {"enabled": True, "name":"Silver Pack","bio":"Twelve Silver players with one rare.","price":2500,"size":12,"gold_weight":0,"silver_weight":100,"bronze_weight":0,"rare_slots":1,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "3": {"enabled": True, "name":"Gold Pack","bio":"Twelve Gold players with one rare.","price":5000,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":1,"special_chance":0.03,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        # Keep the low-to-high promo ladder, without grade prefixes in the UI.
        "4": {"enabled": True, "name":"Breakthrough Pack",
              "bio":"Six Rare Gold players rated 81 or higher, with a balanced chance at every major promo.",
              "price":15000,"size":6,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
              "rare_slots":6,"min_rating":81,"guaranteed_specials":0,"special_chance":0.14,
              "store_group":"special","special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "5": {"enabled": True, "name":"Birthday Shift Pack",
              "bio":"Eight Rare Gold players rated 83 or higher with 2 guaranteed promos and boosted FUT Birthday and Shapeshifters odds.",
              "price":40000,"size":8,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
              "rare_slots":8,"min_rating":83,"guaranteed_specials":2,"special_chance":0.26,
              "store_group":"special","special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "6": {"enabled": True, "name":"Promo Constellation Pack",
              "bio":"Twelve Rare Gold players rated 85 or higher, including at least 5 varied promo players.",
              "price":90000,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
              "rare_slots":12,"min_rating":85,"guaranteed_specials":5,"special_chance":0.42,
              "store_group":"special","special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "7": {"enabled": True, "name":"Endgame Pantheon Pack",
              "bio":"The ultimate pack: fifteen players rated 88 or higher with at least 11 promos, including live chances for TOTY, TOTS and Icon Moments.",
              "price":200000,"size":15,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
              "rare_slots":15,"min_rating":88,"guaranteed_specials":11,"special_chance":0.72,
              # Pack article 7 has no entry in FIFA 20's native packList. The
              # missing base presentation suppresses the TOTY floor runner and
              # side banners even though rarity 5 is selected correctly. Keep
              # the unique purchase id, but use the native special pack shell.
              "store_group":"special","pack_asset_id":4,
              "special_weights":dict(PROMO_SPECIAL_WEIGHTS)},

        # A fuller permanent Store range. These deliberately use the native
        # Bronze/Silver/Gold pack art IDs while keeping unique purchase IDs.
        "8": {"enabled": True, "built_in_store": True, "name":"Premium Bronze Pack",
              "bio":"Twelve Bronze players with three rares.", "price":750,"size":12,
              "gold_weight":0,"silver_weight":0,"bronze_weight":100,"rare_slots":3,
              "special_chance":0.0,"store_group":"bronze","pack_asset_id":1,"special_weights":{}},
        "9": {"enabled": True, "built_in_store": True, "name":"Jumbo Premium Bronze Pack",
              "bio":"Twenty-four Bronze players with seven rares.", "price":1500,"size":24,
              "gold_weight":0,"silver_weight":0,"bronze_weight":100,"rare_slots":7,
              "special_chance":0.0,"store_group":"bronze","pack_asset_id":1,"special_weights":{}},
        "10": {"enabled": True, "built_in_store": True, "name":"Premium Silver Pack",
               "bio":"Twelve Silver players with three rares.", "price":3750,"size":12,
               "gold_weight":0,"silver_weight":100,"bronze_weight":0,"rare_slots":3,
               "special_chance":0.0,"store_group":"silver","pack_asset_id":2,"special_weights":{}},
        "11": {"enabled": True, "built_in_store": True, "name":"Jumbo Premium Silver Pack",
               "bio":"Twenty-four Silver players with seven rares.", "price":7500,"size":24,
               "gold_weight":0,"silver_weight":100,"bronze_weight":0,"rare_slots":7,
               "special_chance":0.0,"store_group":"silver","pack_asset_id":2,"special_weights":{}},
        "12": {"enabled": True, "built_in_store": True, "name":"Premium Gold Pack",
               "bio":"Twelve Gold players with three rares.", "price":7500,"size":12,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":3,
               "special_chance":0.05,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "13": {"enabled": True, "built_in_store": True, "name":"Jumbo Premium Gold Pack",
               "bio":"Twenty-four Gold players with seven rares.", "price":15000,"size":24,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":7,
               "special_chance":0.06,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "14": {"enabled": True, "built_in_store": True, "name":"Gold Players Pack",
               "bio":"Twelve Gold players with one rare.", "price":12500,"size":12,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":1,"min_rating":75,
               "special_chance":0.04,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "15": {"enabled": True, "built_in_store": True, "name":"Premium Gold Players Pack",
               "bio":"Twelve Gold players with three rares.", "price":25000,"size":12,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":3,"min_rating":75,
               "special_chance":0.06,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "16": {"enabled": True, "built_in_store": True, "name":"Prime Gold Players Pack",
               "bio":"Twelve Gold players with six rares.", "price":45000,"size":12,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":6,"min_rating":75,
               "special_chance":0.08,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "17": {"enabled": True, "built_in_store": True, "name":"Rare Players Pack",
               "bio":"Twelve Rare Gold players.", "price":50000,"size":12,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":12,"min_rating":75,
               "special_chance":0.10,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "18": {"enabled": True, "built_in_store": True, "name":"Jumbo Rare Players Pack",
               "bio":"Twenty-four Rare Gold players.", "price":100000,"size":24,
               "gold_weight":100,"silver_weight":0,"bronze_weight":0,"rare_slots":24,"min_rating":75,
               "special_chance":0.12,"store_group":"gold","pack_asset_id":3,"special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "19": {"enabled": True, "built_in_store": True, "name":"Gold Consumables Pack",
               "bio":"Ten mixed FUT consumables: contracts, fitness, healing, training, chemistry styles and manager leagues.",
               "price":5000,"size":10,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":10,"special_chance":0.0,"store_group":"gold","pack_asset_id":3,
               "content_type":"consumables","special_weights":{}},
        "20": {"enabled": True, "built_in_store": True, "name":"Gold Club Items Pack",
               "bio":"A Home Kit, Away Kit, Badge, Stadium and Ball for club customization.",
               "price":5000,"size":5,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":3,"special_chance":0.0,"store_group":"gold","pack_asset_id":3,
               "content_type":"club_items","special_weights":{}},
        "22": {"enabled": True, "built_in_store": True, "name":"Gold Position Modifiers Pack",
               "bio":"Twelve usable Gold position modifiers for permanent player position changes.",
               "price":5000,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":12,"special_chance":0.0,"store_group":"gold","pack_asset_id":3,
               "content_type":"position_modifiers","special_weights":{}},
        "23": {"enabled": True, "built_in_store": True, "name":"Wez Pack",
               "bio":"Twelve players rated 86 or higher, all Rare, with at least seven varied promo cards.",
               "price":125000,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":12,"min_rating":86,"guaranteed_specials":7,"special_chance":0.58,
               "store_group":"special","pack_asset_id":4,
               "special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "24": {"enabled": True, "built_in_store": True, "name":"Miz Special Pack",
               "bio":"Fifteen players rated 89 or higher, all Rare, with at least twelve varied promo cards.",
               "price":275000,"size":15,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":15,"min_rating":89,"guaranteed_specials":12,"special_chance":0.82,
               "store_group":"special","pack_asset_id":4,
               "special_weights":dict(PROMO_SPECIAL_WEIGHTS)},

        # 1.0.29 tester-only Store Player Picks. The Store still uses FIFA's
        # native pack article shell to make the entries selectable, but buying
        # one grants the real miscPlayerPick token (resource 5004241). FIFA then
        # redeems that token through /item/nontargeted and opens the native
        # temporary Player Pick selector implemented in player_pick_state.py.
        "25": {"enabled": True, "built_in_store": True, "name":"1 of 3 Promo Player Pick",
               "bio":"Choose one of three special FIFA 20 promo players.",
               "price":5000,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":1,"special_chance":0.0,"store_group":"special","pack_asset_id":4,
               "content_type":"player_pick","player_pick_pool":"promo","player_pick_options":3,
               "player_pick_min_rating":84,"special_weights":{}},
        "26": {"enabled": True, "built_in_store": True, "name":"1 of 3 TOTS Player Pick",
               "bio":"Choose one of three Team of the Season So Far players rated 88 or higher.",
               "price":7500,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":1,"special_chance":0.0,"store_group":"special","pack_asset_id":4,
               "content_type":"player_pick","player_pick_pool":"tots","player_pick_options":3,
               "player_pick_min_rating":88,"special_weights":{}},
        "27": {"enabled": True, "built_in_store": True, "name":"1 of 3 Icon Moments Player Pick",
               "bio":"Choose one of three Icon Moments players rated 90 or higher.",
               "price":10000,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
               "rare_slots":1,"special_chance":0.0,"store_group":"special","pack_asset_id":4,
               "content_type":"player_pick","player_pick_pool":"icon_moments","player_pick_options":3,
               "player_pick_min_rating":90,"special_weights":{}},

        # Reward-only packs used by the permanent FIFA 20 SBC catalog. They
        # are hidden from the coin store until one has been earned, at which
        # point the native Store/My Packs screen exposes the saved quantity.
        "1001": {"enabled": False, "reward_only": True, "name":"Bronze Reward Pack",
                 "bio":"A small pack of Bronze players earned from a Squad Building Challenge.",
                 "price":0,"size":4,"gold_weight":0,"silver_weight":0,"bronze_weight":100,
                 "rare_slots":1,"special_chance":0.0,"store_group":"bronze","special_weights":{}},
        "1002": {"enabled": False, "reward_only": True, "name":"Silver Players Pack",
                 "bio":"A pack of Silver players earned from a Squad Building Challenge.",
                 "price":0,"size":12,"gold_weight":0,"silver_weight":100,"bronze_weight":0,
                 "rare_slots":1,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "1003": {"enabled": False, "reward_only": True, "name":"Gold Players Pack",
                 "bio":"A pack of Gold players earned from a Squad Building Challenge.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1004": {"enabled": False, "reward_only": True, "name":"Mixed Players Pack",
                 "bio":"A mixture of Bronze, Silver and Gold players earned from an SBC.",
                 "price":0,"size":12,"gold_weight":34,"silver_weight":33,"bronze_weight":33,
                 "rare_slots":4,"special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1005": {"enabled": False, "reward_only": True, "name":"Premium Gold Pack",
                 "bio":"A Premium Gold Pack earned from a Squad Building Challenge.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":3,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1006": {"enabled": False, "reward_only": True, "name":"Prime Gold Players Pack",
                 "bio":"Twelve Gold players with six rares, earned from an SBC.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":6,"special_chance":0.08,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1007": {"enabled": False, "reward_only": True, "name":"Rare Players Pack",
                 "bio":"Twelve Rare Gold players earned from an SBC.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":12,"special_chance":0.10,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1008": {"enabled": False, "reward_only": True, "name":"Mega Pack",
                 "bio":"A large Gold player pack earned from a Squad Building Challenge.",
                 "price":0,"size":30,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":18,"special_chance":0.10,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1009": {"enabled": False, "reward_only": True, "name":"Rare Mega Pack",
                 "bio":"A Rare Mega Pack earned by completing an Advanced SBC group.",
                 "price":0,"size":30,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":30,"special_chance":0.12,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1010": {"enabled": False, "reward_only": True, "name":"Two Rare Gold Players Pack",
                 "bio":"Two Rare Gold players earned from a Squad Building Challenge.",
                 "price":0,"size":2,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":2,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1011": {"enabled": False, "reward_only": True, "name":"Three Common Gold Players Pack",
                 "bio":"Three Common Gold players earned from the Silver Upgrade SBC.",
                 "price":0,"size":3,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":0,"special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1012": {"enabled": False, "reward_only": True, "name":"Two Silver Players Pack",
                 "bio":"Two Silver players earned from the Bronze Upgrade SBC.",
                 "price":0,"size":2,"gold_weight":0,"silver_weight":100,"bronze_weight":0,
                 "rare_slots":0,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "1013": {"enabled": False, "reward_only": True, "name":"Premium Gold Players Pack",
                 "bio":"Twelve Gold players with three rares, earned from an SBC.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":3,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1014": {"enabled": False, "reward_only": True, "name":"Premium Silver Players Pack",
                 "bio":"Twelve Silver players with three rares, earned from an SBC.",
                 "price":0,"size":12,"gold_weight":0,"silver_weight":100,"bronze_weight":0,
                 "rare_slots":3,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "1015": {"enabled": False, "reward_only": True, "name":"Jumbo Gold Pack",
                 "bio":"A 24-item Gold reward pack.",
                 "price":0,"size":24,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":3,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1016": {"enabled": False, "reward_only": True, "name":"Jumbo Premium Gold Pack",
                 "bio":"A 24-item Premium Gold reward pack with seven rares.",
                 "price":0,"size":24,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":7,"special_chance":0.07,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1017": {"enabled": False, "reward_only": True, "name":"Rare Gold Pack",
                 "bio":"Twelve Rare Gold players earned from an SBC.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":12,"special_chance":0.08,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1018": {"enabled": False, "reward_only": True, "name":"Premium Bronze Pack",
                 "bio":"A Premium Bronze reward pack with three rares.",
                 "price":0,"size":12,"gold_weight":0,"silver_weight":0,"bronze_weight":100,
                 "rare_slots":3,"special_chance":0.0,"store_group":"bronze","special_weights":{}},
        "1019": {"enabled": False, "reward_only": True, "name":"Premium Loan Player Reward Pack",
                 "bio":"A local replacement for the original Premium Loan Player reward.",
                 "price":0,"size":4,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":4,"special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1020": {"enabled": False, "reward_only": True, "name":"Gold CB Reward (Max 80 OVR)",
                 "bio":"A Gold centre-back reward replacing the retired FIFA 20 player-pick service.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":0,"min_rating":75,"max_rating":80,"position":"CB","special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1021": {"enabled": False, "reward_only": True, "name":"Gold CM Reward (Max 80 OVR)",
                 "bio":"A Gold central-midfielder reward replacing the retired FIFA 20 player-pick service.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":0,"min_rating":75,"max_rating":80,"position":"CM","special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1022": {"enabled": False, "reward_only": True, "name":"Gold ST Reward (Max 80 OVR)",
                 "bio":"A Gold striker reward replacing the retired FIFA 20 player-pick service.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":0,"min_rating":75,"max_rating":80,"position":"ST","special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1023": {"enabled": False, "reward_only": True, "name":"Gold GK Reward (Max 80 OVR)",
                 "bio":"A Gold goalkeeper reward replacing the retired FIFA 20 player-pick service.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":0,"min_rating":75,"max_rating":80,"position":"GK","special_chance":0.0,"store_group":"gold","special_weights":{}},
        "1024": {"enabled": False, "reward_only": True, "name":"Gold Pack",
                 "bio":"A standard Gold Pack earned as a mode reward.",
                 "price":0,"size":12,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"special_chance":0.03,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1025": {"enabled": False, "reward_only": True, "name":"Prime Mixed Players Pack",
                 "bio":"Twelve players: four Gold, four Silver and four Bronze, with six rares.",
                 "price":0,"size":12,"gold_weight":34,"silver_weight":33,"bronze_weight":33,
                 "rare_slots":6,"special_chance":0.04,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1026": {"enabled": False, "reward_only": True, "name":"Jumbo Premium Silver Pack",
                 "bio":"Twenty-four Silver players with seven rares, earned as a Draft reward.",
                 "price":0,"size":24,"gold_weight":0,"silver_weight":100,"bronze_weight":0,
                 "rare_slots":7,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "1027": {"enabled": False, "reward_only": True, "name":"Silver Pack",
                 "bio":"A standard Silver Pack earned as a Draft reward.",
                 "price":0,"size":12,"gold_weight":0,"silver_weight":100,"bronze_weight":0,
                 "rare_slots":1,"special_chance":0.0,"store_group":"silver","special_weights":{}},
        "1028": {"enabled": False, "reward_only": True, "name":"FUT Birthday Vault Pack",
                 "bio":"Six FUT Birthday players rated 84 or higher, earned from an SBC.",
                 "price":0,"size":6,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":6,"min_rating":84,"guaranteed_specials":6,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"30":100}},
        "1029": {"enabled": False, "reward_only": True, "name":"Shapeshifters Vault Pack",
                 "bio":"Six Shapeshifters players rated 84 or higher, earned from an SBC.",
                 "price":0,"size":6,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":6,"min_rating":84,"guaranteed_specials":6,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"97":100}},
        "1030": {"enabled": False, "reward_only": True, "name":"TOTS Vanguard Pack",
                 "bio":"Five Team of the Season So Far players rated 88 or higher.",
                 "price":0,"size":5,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":5,"min_rating":88,"guaranteed_specials":5,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"66":100}},
        "1031": {"enabled": False, "reward_only": True, "name":"Festival of FUT Pack",
                 "bio":"Ten players rated 85 or higher with at least 7 promos from across FIFA 20.",
                 "price":0,"size":10,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":10,"min_rating":85,"guaranteed_specials":7,"special_chance":0.70,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1032": {"enabled": False, "reward_only": True, "name":"SBC Endgame Crown Pack",
                 "bio":"The master reward: fifteen players rated 89 or higher with at least 12 promo players.",
                 "price":0,"size":15,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":15,"min_rating":89,"guaranteed_specials":12,"special_chance":0.80,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1033": {"enabled": False, "reward_only": True, "name":"FUT Birthday Star Pack",
                 "bio":"One guaranteed FUT Birthday player rated 84 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":84,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"30":100}},
        "1034": {"enabled": False, "reward_only": True, "name":"Shapeshifters Star Pack",
                 "bio":"One guaranteed Shapeshifters player rated 84 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":84,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"97":100}},
        "1035": {"enabled": False, "reward_only": True, "name":"Icon Moments Star Pack",
                 "bio":"One guaranteed Icon Moments player rated 90 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":90,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"84":100}},
        "1036": {"enabled": False, "reward_only": True, "name":"Promo Star Pack",
                 "bio":"One guaranteed promo player rated 84 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":84,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1037": {"enabled": False, "reward_only": True, "name":"Elite Promo Star Pack",
                 "bio":"One guaranteed promo player rated 87 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":87,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1038": {"enabled": False, "reward_only": True, "name":"Supreme Promo Star Pack",
                 "bio":"One guaranteed promo player rated 88 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":88,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1039": {"enabled": False, "reward_only": True, "name":"Ultimate Promo Star Pack",
                 "bio":"One guaranteed promo player rated 90 or higher.",
                 "price":0,"size":1,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":1,"min_rating":90,"guaranteed_specials":1,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":dict(PROMO_SPECIAL_WEIGHTS)},
        "1040": {"enabled": False, "reward_only": True, "name":"Icon Moments Trinity Pack",
                 "bio":"Three different Icon Moments players rated 90 or higher.",
                 "price":0,"size":3,"gold_weight":100,"silver_weight":0,"bronze_weight":0,
                 "rare_slots":3,"min_rating":90,"guaranteed_specials":3,"special_chance":1.0,
                 "store_group":"special","pack_asset_id":4,"special_weights":{"84":100}},
        "1041": {"enabled": False, "reward_only": True, "name":"Premium Mixed Players Pack",
                 "bio":"Twelve players: four Gold, four Silver and four Bronze, with three rares.",
                 "price":0,"size":12,"gold_weight":34,"silver_weight":33,"bronze_weight":33,
                 "rare_slots":3,"special_chance":0.03,"store_group":"gold",
                 "special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1042": {"enabled": False, "reward_only": True, "name":"Rare Electrum Players Pack",
                 "bio":"Twelve Rare players: six Gold and six Silver.",
                 "price":0,"size":12,"gold_weight":50,"silver_weight":50,"bronze_weight":0,
                 "rare_slots":12,"special_chance":0.06,"store_group":"gold",
                 "special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1043": {"enabled": False, "reward_only": True, "name":"Prime Electrum Players Pack",
                 "bio":"Twelve players: six Gold and six Silver, with six rares.",
                 "price":0,"size":12,"gold_weight":50,"silver_weight":50,"bronze_weight":0,
                 "rare_slots":6,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1044": {"enabled": False, "reward_only": True, "name":"Rare Mixed Players Pack",
                 "bio":"Twelve Rare players: four Gold, four Silver and four Bronze.",
                 "price":0,"size":12,"gold_weight":34,"silver_weight":33,"bronze_weight":33,
                 "rare_slots":12,"special_chance":0.05,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1045": {"enabled": False, "reward_only": True, "name":"Premium Electrum Players Pack",
                 "bio":"Twelve players: six Gold and six Silver, with three rares.",
                 "price":0,"size":12,"gold_weight":50,"silver_weight":50,"bronze_weight":0,
                 "rare_slots":3,"special_chance":0.04,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1046": {"enabled": False, "reward_only": True, "name":"Three Silver Players Pack",
                 "bio":"Three Silver players.","price":0,"size":3,"gold_weight":0,
                 "silver_weight":100,"bronze_weight":0,"rare_slots":0,"special_chance":0.0,
                 "store_group":"silver","special_weights":{}},
        "1047": {"enabled": False, "reward_only": True, "name":"Four Gold Common Players Pack",
                 "bio":"Four Common Gold players.","price":0,"size":4,"gold_weight":100,
                 "silver_weight":0,"bronze_weight":0,"rare_slots":0,"special_chance":0.0,
                 "store_group":"gold","special_weights":{}},
        "1048": {"enabled": False, "reward_only": True, "name":"Small Prime Gold Players Pack",
                 "bio":"Six Gold players with three rares.","price":0,"size":6,"gold_weight":100,
                 "silver_weight":0,"bronze_weight":0,"rare_slots":3,"special_chance":0.05,
                 "store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1049": {"enabled": False, "reward_only": True, "name":"Small Prime Electrum Players Pack",
                 "bio":"Six Gold and Silver players with three rares.","price":0,"size":6,
                 "gold_weight":50,"silver_weight":50,"bronze_weight":0,"rare_slots":3,
                 "special_chance":0.04,"store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1050": {"enabled": False, "reward_only": True, "name":"Small Rare Gold Players Pack",
                 "bio":"Six Rare Gold players.","price":0,"size":6,"gold_weight":100,
                 "silver_weight":0,"bronze_weight":0,"rare_slots":6,"special_chance":0.07,
                 "store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
        "1051": {"enabled": False, "reward_only": True, "name":"Two Players Pack",
                 "bio":"Two players with one rare.","price":0,"size":2,"gold_weight":70,
                 "silver_weight":25,"bronze_weight":5,"rare_slots":1,"special_chance":0.03,
                 "store_group":"gold","special_weights":dict(DEFAULT_SPECIAL_WEIGHTS)},
    },
}

# Three early tutorial SBCs used visible coin-store pack IDs in the first
# permanent catalog revision. Store articles and earned My Packs entities are
# different native contracts: reusing IDs 1/3 made FIFA show a currency selector
# (and sometimes an undefined label) for a reward the user already owned.
_LEGACY_SBC_REWARD_PACK_IDS = {
    1: 1001,  # Bronze Pack -> hidden Bronze Reward Pack
    3: 1005,  # Gold Pack -> hidden Premium Gold Pack
}

# These three player-pack articles remain valid reward definitions, but the
# tester explicitly removed them from the coin Store. Earned copies must still
# appear normally under My Packs.
_HIDDEN_COIN_STORE_PACK_IDS = frozenset((14, 15, 16))

_LEGACY_NOVELTY_PACK_NAMES = {
    "4": {"Elite Ten 82+ Pack", "82+ x10 Rare Gold Players Pack", "Prospect Pulse Pack"},
    "5": {"Inde Nav's Uncharted Pack", "81+ x2 Rare Gold Players Pack", "Spotlight Selection Pack"},
    "6": {"Gattouz Warrior Squad", "83+ x5 Rare Gold Players Pack", "Champions Vault Pack"},
    "7": {"Mia Khalifa Jaw-Dropper", "85+ x10 Rare Gold Players Pack", "Ultimate Legacy XI Pack"},
}

_PREVIOUS_GRADED_PACK_NAMES = {
    "4": "GRADE I - Breakthrough Pack",
    "5": "GRADE II - Birthday Shift Pack",
    "6": "GRADE III - Promo Constellation Pack",
    "7": "GRADE IV - Endgame Pantheon Pack",
}


def _sbc_reward_pack_id(pack_id: Any) -> int:
    try:
        value = int(pack_id or 0)
    except (TypeError, ValueError):
        value = 0
    return int(_LEGACY_SBC_REWARD_PACK_IDS.get(value, value))

def _merge_pack_settings(raw):
    out=json.loads(json.dumps(DEFAULT_PACK_SETTINGS))
    if not isinstance(raw,dict):
        return out

    # v25 migration support: older Pack Studio versions stored one global
    # special_weights object. Use it only as a fallback for packs that do not
    # yet contain their own weights. Legends (12) and iMOTM (15) stay disabled.
    legacy_weights=raw.get("special_weights") if isinstance(raw.get("special_weights"),dict) else DEFAULT_SPECIAL_WEIGHTS
    legacy_weights={str(k):v for k,v in legacy_weights.items() if str(k) not in ("12","15")}

    if isinstance(raw.get("packs"),dict):
        out["packs"]={}
        for pid,cfg in raw["packs"].items():
            if isinstance(cfg,dict):
                base={"enabled":True,"name":f"Custom Pack {pid}","bio":"A custom FIFA 20 Ultimate Team pack.","price":7500,"size":12,
                      "gold_weight":100,"silver_weight":0,"bronze_weight":0,
                      "rare_slots":3,"special_chance":0.10,"special_weights":dict(legacy_weights)}
                base.update(cfg)
                # Migrate the exact built-in novelty packs from older builds
                # without overwriting genuine user-created Pack Studio entries.
                legacy_names = _LEGACY_NOVELTY_PACK_NAMES.get(str(pid), set())
                if str(base.get("name", "")) in legacy_names:
                    base = json.loads(json.dumps(DEFAULT_PACK_SETTINGS["packs"][str(pid)]))
                elif str(base.get("name", "")) == _PREVIOUS_GRADED_PACK_NAMES.get(str(pid)):
                    base["name"] = DEFAULT_PACK_SETTINGS["packs"][str(pid)]["name"]
                # Backfill the native promo presentation shell only for the
                # built-in top pack. A genuinely custom ID 7 remains untouched.
                if str(pid) == "7" and str(base.get("name", "")) == DEFAULT_PACK_SETTINGS["packs"]["7"]["name"]:
                    base.setdefault("pack_asset_id", 4)
                weights=base.get("special_weights") if isinstance(base.get("special_weights"),dict) else legacy_weights
                weights={str(k):v for k,v in weights.items() if str(k) not in ("12","15")}
                for rf in DEFAULT_SPECIAL_WEIGHTS:
                    weights.setdefault(rf,0)
                base["special_weights"]=weights
                out["packs"][str(pid)]=base
        # Pack Studio files from older builds do not know about newly added
        # standard packs or hidden SBC rewards. Merge both kinds back without
        # overwriting a genuine user-created Pack Studio entry at the same ID.
        for pid, default_cfg in DEFAULT_PACK_SETTINGS["packs"].items():
            if bool(default_cfg.get("reward_only", False)) or bool(default_cfg.get("built_in_store", False)):
                out["packs"].setdefault(str(pid), json.loads(json.dumps(default_cfg)))
    # The retired OtaGoat article must not be resurrected by a tester's
    # persisted Pack Studio settings from an older build. ID 22 remains the
    # separate, unchanged x12 Position Modifiers Pack.
    out["packs"].pop("21", None)
    visible_pack_ids = [
        int(pid) for pid, cfg in out["packs"].items()
        if not bool(cfg.get("reward_only", False))
    ]
    first_free_pack_id = max(visible_pack_ids or [3]) + 1
    try:
        # Older Pack Studio files commonly say 8 here. Do not let the next
        # user-created pack overwrite one of the new built-in IDs 8-24.
        out["next_pack_id"] = max(int(raw.get("next_pack_id", first_free_pack_id)), first_free_pack_id)
    except Exception:
        out["next_pack_id"] = first_free_pack_id
    return out

def load_pack_settings():
    try:
        if PACK_SETTINGS_PATH.exists():
            return _merge_pack_settings(json.loads(PACK_SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("Could not load pack_settings.json: %s", exc)
    return _merge_pack_settings({})

def get_pack_cfg(pack_id:int):
    settings=load_pack_settings()
    stored=settings.get("packs",{}).get(str(int(pack_id)))
    if not isinstance(stored,dict) or not stored:
        return {}
    cfg=dict(stored)
    cfg["_special_weights"]=dict(cfg.get("special_weights",{}))
    return cfg

# FIFA 20 base-card metadata for the starter pool.
# Face stats are [PAC, SHO, PAS, DRI, DEF, PHY] (GK: [DIV, HAN, KIC, REF, SPE, POS]).
PLAYER_DB = {
    20801:  {"name":"Cristiano Ronaldo","rating":92,"preferredPosition":"LW","leagueId":53,"teamid":243,"nation":38,"face":[93,93,81,91,32,79]},
    158023: {"name":"Lionel Messi","rating":93,"preferredPosition":"CF","leagueId":53,"teamid":241,"nation":52,"face":[93,89,86,96,27,62]},
    190871: {"name":"Neymar Jr","rating":86,"preferredPosition":"LW","leagueId":53,"teamid":241,"nation":54,"face":[90,80,72,92,30,58]},
    176580: {"name":"Luis Suarez","rating":89,"preferredPosition":"ST","leagueId":53,"teamid":241,"nation":60,"face":[83,87,79,88,42,79]},
    173731: {"name":"Gareth Bale","rating":87,"preferredPosition":"RM","leagueId":53,"teamid":243,"nation":50,"face":[94,83,83,84,63,81]},
    188545: {"name":"Robert Lewandowski","rating":87,"preferredPosition":"ST","leagueId":19,"teamid":21,"nation":37,"face":[81,84,74,85,39,78]},
    153079: {"name":"Sergio Aguero","rating":86,"preferredPosition":"ST","leagueId":13,"teamid":10,"nation":52,"face":[88,86,77,88,28,66]},
    183277: {"name":"Eden Hazard","rating":88,"preferredPosition":"LM","leagueId":13,"teamid":5,"nation":7,"face":[89,82,84,91,32,64]},
    167495: {"name":"Manuel Neuer","rating":90,"preferredPosition":"GK","leagueId":19,"teamid":21,"nation":21,"face":[87,85,92,86,58,90]},
    155862: {"name":"Sergio Ramos","rating":87,"preferredPosition":"CB","leagueId":53,"teamid":243,"nation":45,"face":[79,60,71,66,87,82]},
    164240: {"name":"Thiago Silva","rating":87,"preferredPosition":"CB","leagueId":16,"teamid":73,"nation":54,"face":[78,57,72,72,90,80]},
    177003: {"name":"Luka Modric","rating":87,"preferredPosition":"CM","leagueId":53,"teamid":243,"nation":10,"face":[76,74,85,89,71,70]},
    182521: {"name":"Toni Kroos","rating":85,"preferredPosition":"CM","leagueId":53,"teamid":243,"nation":21,"face":[58,81,89,84,58,69]},
    195864: {"name":"Paul Pogba","rating":83,"preferredPosition":"CM","leagueId":31,"teamid":45,"nation":18,"face":[76,78,79,83,73,88]},
}

# Optional external player database. This keeps the server logic small and lets
# the pack pool expand without hardcoding every player in this file.
PLAYER_DB_PATH = ROOT / "players.json"
if PLAYER_DB_PATH.exists():
    try:
        raw_player_db = json.loads(PLAYER_DB_PATH.read_text(encoding="utf-8"))
        if isinstance(raw_player_db, dict):
            for rid, meta in raw_player_db.items():
                if isinstance(meta, dict):
                    PLAYER_DB[int(rid)] = meta
        log_player_count = len(PLAYER_DB)
    except Exception:
        log_player_count = len(PLAYER_DB)
else:
    log_player_count = len(PLAYER_DB)

# Six base cards in the imported catalog inherited the Adidas exhibition-team
# placeholder (112190/Rest of World). Correct the complete set at load time so
# new and already-owned items receive their actual FIFA 20 club and league.
_ADIDAS_BASE_TEAM_FIXES = {
    158023: (241, "FC Barcelona", 53, "LaLiga Santander"),  # Messi
    168651: (241, "FC Barcelona", 53, "LaLiga Santander"),  # Rakitić
    176676: (243, "Real Madrid", 53, "LaLiga Santander"),   # Marcelo
    201942: (9, "Liverpool", 13, "Premier League"),         # Roberto Firmino
    212218: (10, "Manchester City", 13, "Premier League"), # Laporte
    230666: (10, "Manchester City", 13, "Premier League"), # Gabriel Jesus
}
for _resource_id, (_team_id, _team_name, _league_id, _league_name) in _ADIDAS_BASE_TEAM_FIXES.items():
    _base_card = PLAYER_DB.get(_resource_id)
    if isinstance(_base_card, dict) and int(_base_card.get("teamid", 0) or 0) == 112190:
        _base_card.update({
            "teamid": _team_id,
            "teamName": _team_name,
            "leagueId": _league_id,
            "leagueName": _league_name,
        })

# FIFA's exhibition roster duplicates these players under MLS All Stars. FUT
# base cards must instead use each player's real club so packs never show the
# exhibition badge or Rest of World league.
_MLS_ALL_STARS_TEAM_FIXES = {
    16_097: (111065, "Real Salt Lake"),
    41_236: (697, "LA Galaxy"),
    54_050: (688, "D.C. United"),
    121_944: (693, "Chicago Fire Soccer Club"),
    139_068: (112606, "Orlando City SC"),
    164_505: (112885, "Atlanta United FC"),
    164_559: (111928, "San Jose Earthquakes"),
    169_416: (112996, "Los Angeles FC"),
    183_895: (112828, "New York City FC"),
    190_557: (696, "Sporting Kansas City"),
    197_225: (111144, "Seattle Sounders FC"),
    199_669: (112885, "Atlanta United FC"),
    199_781: (111138, "Minnesota United FC"),
    203_067: (111140, "Portland Timbers"),
    203_895: (111651, "Toronto FC"),
    206_662: (695, "FC Dallas"),
    216_939: (112134, "Philadelphia Union"),
    220_209: (689, "New York Red Bulls"),
    226_377: (112885, "Atlanta United FC"),
    236_007: (112885, "Atlanta United FC"),
    238_049: (695, "FC Dallas"),
    238_922: (112996, "Los Angeles FC"),
    241_907: (112996, "Los Angeles FC"),
}
for _asset_id, (_team_id, _team_name) in _MLS_ALL_STARS_TEAM_FIXES.items():
    for _player_definition in PLAYER_DB.values():
        if (
            int(_player_definition.get("assetId", 0) or 0) == _asset_id
            and int(_player_definition.get("teamid", 0) or 0) == 111596
        ):
            _player_definition.update({
                "teamid": _team_id,
                "teamName": _team_name,
                "leagueId": 39,
                "leagueName": "Major League Soccer",
            })

# Correct base-card club metadata that was imported from exhibition squads.
# These overrides are deliberately applied before item construction so they
# also repair existing owned cards when the normal item-enrichment pass runs.
_BASE_CARD_TEAM_FIXES = {
    41_236: (697, "LA Galaxy", 39, "Major League Soccer"),  # Ibrahimovic
}
for _resource_id, (_team_id, _team_name, _league_id, _league_name) in _BASE_CARD_TEAM_FIXES.items():
    _base_card = PLAYER_DB.get(_resource_id)
    if isinstance(_base_card, dict):
        _base_card.update({
            "teamid": _team_id,
            "teamName": _team_name,
            "leagueId": _league_id,
            "leagueName": _league_name,
        })

# Late FUT additions can collide with an older installed FIFA player database.
# Carry every supported identity spelling so the item DTO wins over that stale
# local lookup (Ansu Fati was otherwise rendered as Ryan Allsop).
for _player_definition in PLAYER_DB.values():
    if int(_player_definition.get("assetId", 0) or 0) == 253_004:
        _player_definition.update({
            "name": "Ansu Fati",
            "firstName": "Anssumane",
            "lastName": "Fati",
            "commonName": "Ansu Fati",
            "commonname": "Ansu Fati",
        })

# Ansu Fati was added after the native database shipped with some FIFA 20 PC
# installations. Those builds resolve asset 253004 to Ryan Allsop, which makes
# the card nameless and labels its detail panel with goalkeeper attributes.
# Keep Ansu's FUT resource/card/portrait, but use a native Barcelona outfielder
# as the client-side schema anchor. Explicit identity, club and stats still
# come from Ansu's FUT definition.
_NATIVE_PLAYER_ASSET_OVERRIDES = {
    253_004: 240_654,  # Carles Perez: native Barcelona RM/outfield schema
}


def _native_player_asset_id(meta: dict[str, Any], fallback: int = 0) -> int:
    asset_id = int(meta.get("assetId", fallback) or fallback)
    return int(_NATIVE_PLAYER_ASSET_OVERRIDES.get(asset_id, asset_id))


def _player_is_pack_safe(meta: dict[str, Any]) -> bool:
    """Reject definitions which cannot render a stable native player identity."""
    name = str(
        meta.get("name") or meta.get("commonName") or meta.get("commonname")
        or meta.get("lastName") or ""
    ).strip()
    if not name:
        return False
    rating = int(meta.get("rating", 0) or 0)
    # Base silver definitions absent from the bundled native player table are
    # the cards testers reported with an empty name.
    if 65 <= rating <= 74 and not meta.get("special") and not meta.get("nativeDatabase"):
        return False
    return True

PLAYER_RESOURCE_ALIASES: dict[int, int] = {}
for _canonical_rid, _player_definition in PLAYER_DB.items():
    _legacy_ids = _player_definition.get("legacyResourceIds", [])
    if not isinstance(_legacy_ids, list):
        _legacy_ids = [_legacy_ids]
    for _legacy_rid in _legacy_ids:
        try:
            _legacy_value = int(_legacy_rid)
        except (TypeError, ValueError):
            continue
        if _legacy_value > 0 and _legacy_value != int(_canonical_rid):
            PLAYER_RESOURCE_ALIASES[_legacy_value] = int(_canonical_rid)

# Some Icon Moments and promo definitions request their separate dynamic-image
# id rather than their FUT item resource id. Resolve only unique mappings; a
# reused dynamic id is deliberately left alone instead of guessing a player.
_dynamic_resource_candidates: dict[int, list[int]] = {}
for _canonical_rid, _player_definition in PLAYER_DB.items():
    _dynamic_rid = int(_player_definition.get("dynamicImageResourceId", 0) or 0)
    if _dynamic_rid > 0:
        _dynamic_resource_candidates.setdefault(_dynamic_rid, []).append(int(_canonical_rid))
PLAYER_DYNAMIC_RESOURCE_ALIASES = {
    dynamic_id: resource_ids[0]
    for dynamic_id, resource_ids in _dynamic_resource_candidates.items()
    if len(resource_ids) == 1
}


def canonical_player_resource_id(resource_id: int) -> int:
    """Map IDs emitted by older local catalogs to their native FUT card ID."""
    value = int(resource_id)
    return int(PLAYER_RESOURCE_ALIASES.get(value, value))


def player_meta(resource_id: int) -> dict[str, Any]:
    return PLAYER_DB.get(canonical_player_resource_id(resource_id), {})


def _player_is_excluded_from_packs(resource_id: int, meta: dict[str, Any]) -> bool:
    """Block a whole player family, including newly added card resource IDs."""
    if canonical_player_resource_id(resource_id) in PACK_EXCLUDED_RESOURCE_IDS:
        return True
    if _int_or(meta.get("assetId"), 0) in PACK_EXCLUDED_ASSET_IDS:
        return True
    names = " ".join(str(meta.get(key, "") or "") for key in
                     ("name", "commonName", "commonname", "firstName", "lastName"))
    plain = unicodedata.normalize("NFKD", names).encode("ascii", "ignore").decode("ascii").casefold()
    return "ansu fati" in plain or "ribery" in plain


def _player_identity_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Return optional identity fields for cards absent from FIFA's base DB.

    Most FUT cards resolve these values from assetId. Late FIFA 20 cards such
    as Ansu Fati and unlicensed players such as Ribery do not always exist in
    the installed database, so the native UI otherwise renders a blank name
    and default bio values. Extra JSON fields are ignored for normal cards.
    """
    display_name = str(meta.get("name", "") or "").strip()
    first_name = str(meta.get("firstName", "") or "").strip()
    last_name = str(meta.get("lastName", "") or "").strip()
    common_name = str(meta.get("commonName", meta.get("commonname", "")) or "").strip()
    if not display_name:
        display_name = common_name or last_name or first_name
    if not display_name:
        display_name = f"Player {int(meta.get('assetId', 0) or 0)}"
    if not common_name:
        common_name = display_name
    if not last_name:
        last_name = display_name
    return {
        "name": display_name,
        "displayName": display_name,
        "playerName": display_name,
        "shortName": common_name,
        "firstName": first_name,
        "firstname": first_name,
        "lastName": last_name,
        "lastname": last_name,
        "surname": last_name,
        "commonName": common_name,
        "commonname": common_name,
        "playerCommonName": common_name,
        "knownAs": common_name,
        "knownas": common_name,
        "dateOfBirth": str(meta.get("dateOfBirth", "") or ""),
        "height": int(meta.get("height", 0) or 0),
        "weight": int(meta.get("weight", 0) or 0),
        "preferredFoot": str(meta.get("preferredFoot", "") or ""),
        "weakFoot": int(meta.get("weakFoot", 0) or 0),
        "skillMoves": int(meta.get("skillMoves", 0) or 0),
        "workRateAttack": str(meta.get("workRateAttack", "") or ""),
        "workRateDefense": str(meta.get("workRateDefense", "") or ""),
    }

def player_attribute_list(resource_id: int) -> list[dict[str, int]]:
    face = player_meta(resource_id).get("face", [])
    return [{"index": i, "value": int(v)} for i, v in enumerate(face)]


# Real FUT manager-card definitions (ported from the FIFA 21 local build; the
# FUT manager resource-id range and card shape are unchanged between 20 and 21).
MANAGER_DB_PATH = ROOT / "managers.json"
MANAGER_DB: dict[int, dict[str, Any]] = {}
if MANAGER_DB_PATH.exists():
    try:
        raw_manager_db = json.loads(MANAGER_DB_PATH.read_text(encoding="utf-8"))
        if isinstance(raw_manager_db, dict):
            for rid, meta in raw_manager_db.items():
                if isinstance(meta, dict):
                    MANAGER_DB[int(rid)] = meta
    except Exception:
        MANAGER_DB = {}


def _use_native_fifa20_managers(catalog: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Keep only managers the installed FIFA 20 database can render.

    The original catalog came from FIFA 21, which left every club at zero and
    included people whose portraits do not exist in FIFA 20. Join it against
    FIFA 20's native manager and league tables so cards have real portraits,
    club badges, nations and leagues.
    """
    native_root = (
        PROJECT_ROOT / "runtime" / "mined" / "native-fifa20" / "tables"
        / "fifa_ng_db"
    )
    try:
        native_rows = json.loads((native_root / "manager.json").read_text(encoding="utf-8"))
        league_rows = json.loads((native_root / "leagueteamlinks.json").read_text(encoding="utf-8"))
    except Exception:
        return catalog
    leagues_by_team = {
        int(row.get("teamid", 0) or 0): int(row.get("leagueid", 0) or 0)
        for row in league_rows if isinstance(row, dict)
    }
    native_by_id = {
        int(row.get("managerid", 0) or 0): row
        for row in native_rows
        if isinstance(row, dict) and int(row.get("managerid", 0) or 0) > 0
    }
    corrected: dict[int, dict[str, Any]] = {}
    for resource_id, raw_meta in catalog.items():
        manager_id = int(resource_id) - 1_000_000
        native = native_by_id.get(manager_id)
        if not native:
            continue
        native_identity = "".join(str(native.get(key, "") or "").strip()
                                  for key in ("firstname", "surname", "commonname"))
        if not native_identity:
            # Placeholder manager rows have a generic head and no localized
            # identity; exposing their imported name produces blank artwork.
            continue
        meta = dict(raw_meta)
        team_id = int(native.get("teamid", 0) or 0)
        meta.update({
            "assetId": manager_id,
            "teamid": team_id,
            "leagueId": leagues_by_team.get(team_id, 0),
            "nation": int(native.get("nationality", meta.get("nation", 0)) or 0),
            "firstName": str(native.get("firstname", meta.get("firstName", "")) or ""),
            "lastName": str(native.get("surname", meta.get("lastName", "")) or ""),
            "commonname": str(native.get("commonname", meta.get("commonname", "")) or ""),
        })
        corrected[int(resource_id)] = meta
    return corrected or catalog


MANAGER_DB = _use_native_fifa20_managers(MANAGER_DB)

def manager_meta(resource_id: int) -> dict[str, Any]:
    return MANAGER_DB.get(int(resource_id), {})


# A deliberately varied set of FIFA 20 gold managers. These are granted to
# both new and existing saves so manager-card compatibility can be tested in
# game without relying on pack luck. Unai Emery is first because the supplied
# runtime capture confirms that his native card resolves successfully.
_STARTER_MANAGER_RESOURCE_IDS = (
    1_237_389,  # Unai Emery
    1_169_894,  # Pep Guardiola
    1_232_302,  # Jurgen Klopp
    1_005_571,  # Diego Simeone
    1_005_471,  # Frank Lampard
    1_249_063,  # Brendan Rodgers
    1_001_845,  # Ole Gunnar Solskjaer
    1_249_119,  # Chris Wilder
    1_250_187,  # Nuno
    1_156_320,  # Graham Potter
    1_220_467,  # Roy Hodgson
    1_051_404,  # Sean Dyche
    1_113_348,  # Gaizka Garitano
)


# Native FUT development cards used by the Apply Consumable picker. FIFA's
# item factory expects a dynamic item nested inside a stack wrapper; keeping
# the catalog here also lets stats and application use one authoritative map.
CONSUMABLE_DB: dict[int, dict[str, Any]] = {
    5_001_003: {
        "cardassetid": 7, "cardsubtypeid": 201, "rating": 80,
        "rareflag": 0, "amount": 13, "bronze": 15, "silver": 11, "gold": 13,
        "discardValue": 80, "sourceMember": "consumablesContractPlayer",
    },
    5_002_003: {
        "cardassetid": 10, "cardsubtypeid": 219, "rating": 80,
        "rareflag": 0, "amount": 60, "bronze": 60, "silver": 60, "gold": 60,
        "discardValue": 80, "sourceMember": "consumablesFitnessPlayer",
    },
    5_002_006: {
        "cardassetid": 10, "cardsubtypeid": 220, "rating": 80,
        "rareflag": 1, "amount": 30, "bronze": 30, "silver": 30, "gold": 30,
        "discardValue": 160, "sourceMember": "consumablesFitnessTeam",
    },
}

# The native FUT position-card table is stable across these legacy PC clients:
# card asset 34 and subtypes 91-110 are the twenty reversible position paths.
POSITION_MODIFIER_EFFECTS: dict[int, tuple[str, str]] = {
    5_003_059: ("LWB", "LB"), 5_003_060: ("LB", "LWB"),
    5_003_061: ("RWB", "RB"), 5_003_062: ("RB", "RWB"),
    5_003_063: ("LM", "LW"), 5_003_064: ("RM", "RW"),
    5_003_065: ("LW", "LM"), 5_003_066: ("RW", "RM"),
    5_003_067: ("LW", "LF"), 5_003_068: ("RW", "RF"),
    5_003_069: ("LF", "LW"), 5_003_070: ("RF", "RW"),
    5_003_071: ("CM", "CAM"), 5_003_072: ("CAM", "CM"),
    5_003_073: ("CDM", "CM"), 5_003_074: ("CM", "CDM"),
    5_003_075: ("CAM", "CF"), 5_003_076: ("CF", "CAM"),
    5_003_077: ("CF", "ST"), 5_003_078: ("ST", "CF"),
}
for _modifier_index, (_modifier_resource, (_from_position, _to_position)) in enumerate(
        POSITION_MODIFIER_EFFECTS.items()):
    CONSUMABLE_DB[_modifier_resource] = {
        "cardassetid": 34,
        "cardsubtypeid": 91 + _modifier_index,
        "rating": 90 if _modifier_resource in {
            5_003_060, 5_003_062, 5_003_067, 5_003_069, 5_003_071, 5_003_074,
        } else 95,
        "rareflag": 1, "amount": 0, "bronze": 0, "silver": 0, "gold": 0,
        "discardValue": 160, "sourceMember": "consumablesPosition",
        "fromPosition": _from_position, "toPosition": _to_position,
    }


def _add_consumable_definition(
    resource_id: int,
    cardassetid: int,
    cardsubtypeid: int,
    rating: int,
    rareflag: int,
    amount: int,
    source_member: str,
    bronze: int | None = None,
    silver: int | None = None,
    gold: int | None = None,
) -> None:
    CONSUMABLE_DB.setdefault(int(resource_id), {
        "cardassetid": int(cardassetid),
        "cardsubtypeid": int(cardsubtypeid),
        "rating": int(rating),
        "rareflag": int(rareflag),
        "amount": int(amount),
        "bronze": int(amount if bronze is None else bronze),
        "silver": int(amount if silver is None else silver),
        "gold": int(amount if gold is None else gold),
        "discardValue": 160 if int(rareflag) else 80,
        "sourceMember": str(source_member),
    })


# Complete legacy FUT development-card catalog used by FIFA 20's native
# consumable picker. Existing proven rows above remain authoritative.
_player_contracts = {
    5_001_001: (50, 0, 8, 2, 1), 5_001_002: (65, 0, 10, 10, 8),
    5_001_003: (80, 0, 15, 11, 13), 5_001_004: (60, 1, 15, 6, 3),
    5_001_005: (70, 1, 20, 24, 18), 5_001_006: (90, 1, 28, 24, 28),
}
for _rid, (_rating, _rare, _bronze, _silver, _gold) in _player_contracts.items():
    _add_consumable_definition(
        _rid, 7, 201, _rating, _rare, _gold, "consumablesContractPlayer",
        _bronze, _silver, _gold,
    )

_manager_contracts = {
    5_001_007: (50, 0, 8, 2, 1), 5_001_008: (65, 0, 8, 10, 8),
    5_001_009: (80, 0, 11, 11, 13), 5_001_010: (60, 1, 15, 6, 3),
    5_001_011: (70, 1, 18, 24, 18), 5_001_012: (90, 1, 24, 24, 28),
}
for _rid, (_rating, _rare, _bronze, _silver, _gold) in _manager_contracts.items():
    _add_consumable_definition(
        _rid, 8, 202, _rating, _rare, _gold, "consumablesContractManager",
        _bronze, _silver, _gold,
    )

for _rid, _rating, _amount in (
    (5_002_001, 55, 20), (5_002_002, 70, 40), (5_002_003, 80, 60),
):
    _add_consumable_definition(_rid, 10, 219, _rating, 0, _amount, "consumablesFitnessPlayer")
for _rid, _rating, _amount in (
    (5_002_004, 55, 10), (5_002_005, 70, 20), (5_002_006, 80, 30),
):
    _add_consumable_definition(_rid, 10, 220, _rating, 1, _amount, "consumablesFitnessTeam")

for _base, _subtype in (
    (5_002_007, 211), (5_002_010, 212), (5_002_013, 213),
    (5_002_019, 215), (5_002_022, 216), (5_002_025, 217),
):
    for _offset, (_rating, _amount) in enumerate(((55, 1), (70, 2), (80, 5))):
        _add_consumable_definition(
            _base + _offset, 9, _subtype, _rating, 0, _amount, "consumablesHealing"
        )
for _offset, (_rating, _amount) in enumerate(((60, 1), (74, 2), (85, 4))):
    _add_consumable_definition(
        5_002_028 + _offset, 9, 218, _rating, 1, _amount, "consumablesHealing"
    )

for _group in range(7):
    _subtype = 51 + _group
    _base = 5_003_001 + (_group * 3)
    _values = ((55, 5, 0), (65, 10, 0), (85, 15, 0)) if _group < 6 else (
        (64, 3, 1), (74, 6, 1), (95, 10, 1)
    )
    for _offset, (_rating, _amount, _rare) in enumerate(_values):
        _add_consumable_definition(
            _base + _offset, 3, _subtype, _rating, _rare, _amount, "consumablesTrainingGk"
        )

for _group in range(7):
    _subtype = 61 + _group
    _base = 5_003_022 + (_group * 3)
    _values = ((55, 5, 0), (65, 10, 0), (85, 15, 0)) if _group < 6 else (
        (64, 3, 1), (74, 6, 1), (95, 10, 1)
    )
    for _offset, (_rating, _amount, _rare) in enumerate(_values):
        _add_consumable_definition(
            _base + _offset, 1, _subtype, _rating, _rare, _amount, "consumablesTrainingPlayer"
        )

for _offset in range(19):
    _subtype = 250 + _offset
    _add_consumable_definition(
        5_003_095 + _offset, 50, _subtype,
        75 if _offset == 0 else 95, 0,
        0 if _offset == 0 else min(4, 1 + ((_offset - 1) // 5)),
        "consumablesTrainingPlayerPlayStyle",
    )
for _offset in range(5):
    _subtype = 269 + _offset
    _add_consumable_definition(
        5_003_114 + _offset, 51, _subtype,
        75 if _offset == 4 else 95, 0, 5,
        "consumablesTrainingGkPlayStyle",
    )

_manager_leagues = (
    (5_003_119, 300, 1, 0), (5_003_120, 301, 4, 0),
    (5_003_122, 303, 10, 0), (5_003_123, 304, 13, 1),
    (5_003_124, 305, 14, 0), (5_003_125, 306, 16, 1),
    (5_003_126, 307, 17, 0), (5_003_127, 308, 19, 1),
    (5_003_128, 309, 20, 0), (5_003_129, 310, 31, 1),
    (5_003_130, 311, 32, 0), (5_003_131, 312, 39, 1),
    (5_003_132, 313, 41, 0), (5_003_133, 314, 50, 0),
    (5_003_134, 315, 53, 1), (5_003_135, 316, 54, 0),
    (5_003_136, 317, 56, 0), (5_003_137, 318, 60, 0),
    (5_003_138, 319, 61, 0), (5_003_139, 320, 63, 0),
    (5_003_140, 321, 65, 0), (5_003_141, 322, 66, 0),
    (5_003_142, 323, 67, 0), (5_003_143, 324, 68, 0),
    (5_003_144, 325, 80, 0), (5_003_145, 326, 83, 0),
    (5_003_146, 327, 189, 0), (5_003_147, 328, 308, 0),
    (5_003_148, 329, 332, 0), (5_003_149, 330, 335, 0),
    (5_003_150, 331, 336, 0), (5_003_151, 332, 341, 0),
    (5_003_152, 333, 347, 0), (5_003_153, 334, 350, 0),
    (5_003_154, 335, 351, 0), (5_003_155, 336, 353, 0),
    (5_003_156, 337, 2118, 1), (5_003_157, 338, 322, 0),
    (5_003_158, 339, 349, 0),
)
for _rid, _subtype, _league_id, _rare in _manager_leagues:
    _add_consumable_definition(
        _rid, 32, _subtype, 95, _rare, _league_id,
        "consumablesTrainingManagerLeagueModifier",
    )


def _consumable_pack_groups() -> list[list[int]]:
    groups: dict[str, list[int]] = {
        "contracts": [], "fitness": [], "healing": [], "training_player": [],
        "training_gk": [], "playstyle": [], "manager_league": [],
    }
    for _rid, _meta in CONSUMABLE_DB.items():
        _source = str(_meta.get("sourceMember", ""))
        if "Contract" in _source:
            groups["contracts"].append(_rid)
        elif "Fitness" in _source:
            groups["fitness"].append(_rid)
        elif _source == "consumablesHealing":
            groups["healing"].append(_rid)
        elif _source == "consumablesTrainingPlayer":
            groups["training_player"].append(_rid)
        elif _source == "consumablesTrainingGk":
            groups["training_gk"].append(_rid)
        elif "PlayStyle" in _source:
            groups["playstyle"].append(_rid)
        elif _source == "consumablesTrainingManagerLeagueModifier":
            groups["manager_league"].append(_rid)
    return [rows for rows in groups.values() if rows]


# These five rows are already proven by the supplied FIFA 20 runtime captures.
# They are now real owned items rather than ephemeral bootstrap decorations.
CLUB_ITEM_DB: dict[int, dict[str, Any]] = {
    6_200_058: {
        "family": "stadiums", "table": "fcc_stadium", "assetId": 261,
        "cardassetid": 36, "itemType": "stadium", "cardsubtypeid": 10,
        "itemState": "activeStadium", "rating": 64, "rareflag": 0,
        "value": 64, "weightrare": 0, "category": 4, "stadiumid": 261,
        "capacity": 35000, "boost": 0,
    },
    6_300_074: {
        "family": "kits", "table": "fcc_kitcards", "assetId": 14,
        "cardassetid": 35, "itemType": "kit", "cardsubtypeid": 9,
        "itemState": "activeHomeKit", "rating": 83, "rareflag": 1,
        "value": 83, "weightrare": 10, "category": 2, "teamid": 1, "year": 0,
    },
    6_400_057: {
        "family": "kits", "table": "fcc_kitcards", "assetId": 15,
        "cardassetid": 35, "itemType": "kit", "cardsubtypeid": 9,
        "itemState": "activeAwayKit", "rating": 83, "rareflag": 1,
        "value": 83, "weightrare": 10, "category": 3, "teamid": 1, "year": 0,
    },
    6_000_057: {
        "family": "badges", "table": "fcc_badgecards", "assetId": 1,
        "cardassetid": 39, "itemType": "custom", "cardsubtypeid": 11,
        "itemState": "activeBadge", "rating": 83, "rareflag": 1,
        "value": 83, "weightrare": 10, "category": 1, "teamid": 1,
    },
    8_120_212: {
        # The captured legacy row pointed at ball 191, which is not a FIFA 20
        # store ball and renders as *BallName_191. Preserve the resource id for
        # saved clubs while migrating it to native, localized ball 0.
        "family": "balls", "table": "fcc_balls", "assetId": 0,
        "cardassetid": 37, "itemType": "ball", "cardsubtypeid": 30,
        "itemState": "activeBall", "rating": 74, "rareflag": 0,
        "value": 74, "weightrare": 0, "category": 1, "ballid": 0,
    },
}


def _load_native_kit_and_badge_catalog() -> tuple[list[int], list[int], list[int]]:
    home_resources: list[int] = []
    away_resources: list[int] = []
    badge_resources: list[int] = []
    table_path = (
        PROJECT_ROOT / "runtime" / "mined" / "native-fifa20" / "tables"
        / "fifa_ng_db" / "teamkits.json"
    )
    try:
        rows = json.loads(table_path.read_text(encoding="utf-8"))
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []

    def as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    team_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        team_id = as_int(row.get("teamtechid"), 0)
        kit_id = as_int(row.get("teamkitid"), 0)
        raw_type = row.get("teamkittypetechid", -1)
        try:
            kit_type = int(raw_type)
        except (TypeError, ValueError):
            kit_type = -1
        if team_id <= 0 or kit_id <= 0 or kit_type not in (0, 1):
            continue
        team_ids.add(team_id)
        if kit_type == 0:
            resource_id = 6_300_000 + kit_id
            category = 2
            asset_id = 14
            home_resources.append(resource_id)
        else:
            resource_id = 6_400_000 + kit_id
            category = 3
            asset_id = 15
            away_resources.append(resource_id)
        CLUB_ITEM_DB.setdefault(resource_id, {
            "family": "kits", "table": "fcc_kitcards", "assetId": asset_id,
            "cardassetid": 35, "itemType": "kit", "cardsubtypeid": 9,
            "itemState": "free", "rating": 83, "rareflag": 1,
            "value": 83, "weightrare": 10, "category": category,
            "teamid": team_id, "year": as_int(row.get("year"), 0),
        })

    for team_id in sorted(team_ids):
        resource_id = 6_050_000 + team_id
        CLUB_ITEM_DB.setdefault(resource_id, {
            "family": "badges", "table": "fcc_badgecards", "assetId": 1,
            "cardassetid": 39, "itemType": "custom", "cardsubtypeid": 11,
            "itemState": "free", "rating": 83, "rareflag": 1,
            "value": 83, "weightrare": 10, "category": 1, "teamid": team_id,
        })
        badge_resources.append(resource_id)

    return home_resources, away_resources, badge_resources


_NATIVE_HOME_KITS, _NATIVE_AWAY_KITS, _NATIVE_BADGES = _load_native_kit_and_badge_catalog()


def _load_native_stadium_and_ball_catalog() -> tuple[list[int], list[int]]:
    """Expose the stadiums and balls shipped in FIFA 20 as persistent FUT cards."""
    table_root = (
        PROJECT_ROOT / "runtime" / "mined" / "native-fifa20" / "tables"
        / "fifa_ng_db"
    )

    def read_rows(name: str) -> list[dict[str, Any]]:
        try:
            rows = json.loads((table_root / name).read_text(encoding="utf-8"))
        except Exception:
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    stadium_resources: list[int] = []
    for row in read_rows("stadiums.json"):
        stadium_id = as_int(row.get("stadiumid"), -1)
        if (
            stadium_id < 0
            or as_int(row.get("isstadiumavailable"), 0) != 1
            or as_int(row.get("gmavailability"), 0) != 1
            or as_int(row.get("issmallsided"), 0) != 0
            or not str(row.get("name", "")).strip(" _")
        ):
            continue
        resource_id = 6_201_000 + stadium_id
        licensed = as_int(row.get("islicensed"), 0) == 1
        CLUB_ITEM_DB.setdefault(resource_id, {
            "family": "stadiums", "table": "fcc_stadium", "assetId": stadium_id,
            "cardassetid": 36, "itemType": "stadium", "cardsubtypeid": 10,
            "itemState": "free", "rating": 75 if licensed else 64,
            "rareflag": 1 if licensed else 0, "value": 75 if licensed else 64,
            "weightrare": 1 if licensed else 0, "category": 4,
            "stadiumid": stadium_id,
            "capacity": max(0, as_int(row.get("capacity"), 0)), "boost": 0,
            "name": str(row.get("name", "")).lstrip("_"),
        })
        stadium_resources.append(resource_id)

    ball_resources: list[int] = []
    for row in read_rows("teamballs.json"):
        ball_id = as_int(row.get("ballid"), -1)
        if (
            ball_id < 0
            or as_int(row.get("isavailableinstore"), 0) != 1
            or as_int(row.get("isembargoed"), 0) != 0
        ):
            continue
        resource_id = 8_120_000 + ball_id
        licensed = as_int(row.get("islicensed"), 0) == 1
        CLUB_ITEM_DB.setdefault(resource_id, {
            "family": "balls", "table": "fcc_balls", "assetId": ball_id,
            "cardassetid": 37, "itemType": "ball", "cardsubtypeid": 30,
            "itemState": "free", "rating": 75 if licensed else 64,
            "rareflag": 1 if licensed else 0, "value": 75 if licensed else 64,
            "weightrare": 1 if licensed else 0, "category": 1,
            "ballid": ball_id,
        })
        ball_resources.append(resource_id)

    return stadium_resources, ball_resources


_NATIVE_STADIUMS, _NATIVE_BALLS = _load_native_stadium_and_ball_catalog()

_STARTER_CONSUMABLE_STACKS = {
    5_001_003: 20,
    5_002_003: 10,
    5_002_006: 5,
}


logging.basicConfig(
    level=getattr(logging, str(CFG.get("logging_level", "INFO")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOGS / "localfut20.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("localfut20")
log.info("[Config] Loaded config from %s", CONFIG_PATH)
log.info("[DB] SQLite path %s", DB_PATH)
log.info("[FUT] Loaded %d FIFA 20 player definitions", log_player_count)
log.info("[FUT] Loaded %d FUT manager definitions", len(MANAGER_DB))

try:
    _special_pool_counts={}
    for _rid,_meta in PLAYER_DB.items():
        _rf=int(_meta.get("rareflag",0) or 0)
        if _rf>1:
            _special_pool_counts[_rf]=_special_pool_counts.get(_rf,0)+1
    log.info("[FUT] Special rarity pools: %s", dict(sorted(_special_pool_counts.items())))
except Exception:
    pass

FLOW_STATES = [
    "START",
    "LOCAL_SERVICES_READY",
    "REDIRECTOR_READY",
    "TLS_CONNECTED",
    "SERVER_INSTANCE",
    "BLAZE_CONNECTED",
    "BLAZE_PREAUTH",
    "CONFIG",
    "AUTH",
    "SESSION",
    "ENTITLEMENT",
    "EASW_ROSTER",
    "EASW_ROUTING",
    "ACCOUNT",
    "NUCLEUS",
    "FUT_AUTH",
    "FUT_INIT",
    "CLUB",
    "SQUAD",
    "STORE",
    "PACKS",
    "FUT_HOME",
]
FLOW_ORDER = {name: idx for idx, name in enumerate(FLOW_STATES)}


class RuntimeFlow:
    def __init__(self, status_path: Path):
        self.status_path = status_path
        self.lock = threading.RLock()
        self.current_state = "START"
        self.milestones: dict[str, dict[str, Any]] = {}
        self.mark("START", "FIFA20", "runtime imported")

    def _timestamp(self) -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()

    def _status_name(self) -> str:
        if self.current_state == "FUT_HOME":
            return "FUT READY"
        if self.current_state in ("LOCAL_SERVICES_READY", "REDIRECTOR_READY"):
            return self.current_state
        return f"{self.current_state} OBSERVED"

    def mark(self, state: str, component: str, detail: str = "", session: str = "") -> None:
        if state not in FLOW_ORDER:
            state = "START"
        with self.lock:
            prior = self.current_state
            if FLOW_ORDER[state] >= FLOW_ORDER.get(self.current_state, 0):
                self.current_state = state
            event = {
                "at": self._timestamp(),
                "component": component,
                "detail": detail,
                "session": session,
            }
            self.milestones[state] = event
            log.info(
                "[FIFA20] State: %s component=%s session=%s detail=%s",
                state, component, session or "-", detail or "-",
            )
            if self.current_state != prior or state == "START":
                self.write_status_locked()

    def write_status_locked(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        current_index = FLOW_ORDER.get(self.current_state, 0)
        status = {
            "status": self._status_name(),
            "currentState": self.current_state,
            "verified": [name for name in FLOW_STATES if name in self.milestones],
            "notYetVerified": [name for name in FLOW_STATES[current_index + 1:] if name not in self.milestones],
            "milestones": self.milestones,
            "updatedAt": self._timestamp(),
        }
        self.status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    def log_http(
        self,
        component: str,
        method: str,
        path: str,
        request_size: int,
        response_size: int,
        status: int,
        session: str = "",
    ) -> None:
        log.info(
            "[%s] HTTP method=%s path=%s requestBytes=%d responseBytes=%d status=%d session=%s state=%s",
            component, method, path, request_size, response_size, status, session or "-", self.current_state,
        )
        if status >= 400:
            log.warning(
                "[%s] HTTP failure method=%s path=%s requestBytes=%d responseBytes=%d status=%d session=%s state=%s",
                component, method, path, request_size, response_size, status, session or "-", self.current_state,
            )

    def log_blaze(self, packet: "Fire2Packet", label: str, response_size: int, session: str = "") -> None:
        log.info(
            "[Blaze] component=0x%04X command=0x%04X seq=%d requestBytes=%d responseBytes=%d result=%s session=%s state=%s",
            packet.component, packet.command, packet.msg_num, len(packet.payload), response_size,
            label, session or "-", self.current_state,
        )
        if label == "unknown-empty":
            log.warning(
                "[Blaze] UNKNOWN component=0x%04X command=0x%04X seq=%d len=%d session=%s state=%s",
                packet.component, packet.command, packet.msg_num, len(packet.payload), session or "-", self.current_state,
            )


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def safe_json(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {}


# Presentation/configuration files requested during the native FUT bootstrap.
# These are bundled local copies from the working FIFA 21 runtime so startup
# never depends on the retired EA CDN and never receives a malformed JSON `{}`
# response for a binary or structured file.
_FUT_STARTUP_CONTENT = {
    "/fut/items/images/backgrounds/itembgs/futitemraritytunables.json": "application/json; charset=utf-8",
    "/fut/packs/packopening/packopeningsetting.json": "application/json; charset=utf-8",
    "/fut/packs/packopening/packopeningconfig.json": "application/json; charset=utf-8",
    "/fut/playerheads/g4/fut2dheads.big": "application/octet-stream",
}

_EA_FUT20_CONTENT_BASE = (
    "https://fifa20.content.easports.com/fifa/fltOnlineAssets/"
    "20C1B296-B15C-4F72-AF0F-882F187EC2C9/2020"
)
_CONTENT_FETCH_LOCK = threading.RLock()
_CONTENT_FILE_LOCKS: dict[str, threading.Lock] = {}
_PLAYER_HEAD_PRIME_LOCK = threading.RLock()
_PLAYER_HEAD_PRIME_ACTIVE: set[int] = set()
_PLAYER_HEAD_PRIME_SLOTS = threading.BoundedSemaphore(12)
_CARD_BG_RE = re.compile(
    r"^/fut/items/images/backgrounds/itembgs/[0-9a-f-]{36}/"
    r"cards_bg_[cesj]_\d+_\d+_\d+\.dds$",
    re.IGNORECASE,
)
_PLAYER_HEAD_RE = re.compile(r"^/fut/playerheads/g4/single/p(\d+)\.dds$", re.IGNORECASE)

# The retired FIFA 20 CDN no longer has the custom/local SBC art referenced by
# this server. A valid transparent PNG keeps the native client from retrying
# each missing image in a tight loop while still allowing the menu to render.
_SBC_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)


def _bundled_fut_startup_content(path: str) -> tuple[bytes, str] | None:
    content_type = _FUT_STARTUP_CONTENT.get(path)
    if content_type is None:
        return None
    candidate = (CONTENT_ROOT / path.lstrip("/")).resolve()
    try:
        candidate.relative_to(CONTENT_ROOT.resolve())
        body = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    if not body:
        return None
    # Refuse to serve a copied error page as one of the JSON startup files.
    if content_type.startswith("application/json"):
        try:
            json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("Ignoring invalid bundled FUT startup JSON: %s", candidate)
            return None
    return body, content_type


def _download_fut20_dds(remote_path: str, cache_key: str) -> bytes | None:
    """Fetch one allow-listed FIFA 20 presentation asset and cache it locally."""
    cache_file = (CONTENT_CACHE / cache_key.lstrip("/")).resolve()
    try:
        cache_file.relative_to(CONTENT_CACHE.resolve())
    except ValueError:
        return None
    # A single global lock made a 25-card pack download every portrait in
    # series. While a requested texture was waiting, FIFA reused the previous
    # card's texture. Serialize only writers for the same cache file and allow
    # unrelated player heads in parallel.
    normalized_key = str(cache_file).lower()
    with _CONTENT_FETCH_LOCK:
        file_lock = _CONTENT_FILE_LOCKS.setdefault(normalized_key, threading.Lock())
    with file_lock:
        try:
            cached = cache_file.read_bytes()
            if cached.startswith(b"DDS ") and len(cached) >= 128:
                return cached
        except OSError:
            pass
        url = _EA_FUT20_CONTENT_BASE + remote_path
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FIFA/20 PC"})
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(8 * 1024 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("FIFA20 content fetch failed url=%s error=%s", url, exc)
            return None
        if len(data) > 8 * 1024 * 1024 or not data.startswith(b"DDS ") or len(data) < 128:
            log.warning("Rejected invalid FIFA20 DDS url=%s bytes=%d", url, len(data))
            return None
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(cache_file.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(cache_file)
        return data


def _native_player_head_dds(body: bytes) -> bytes | None:
    """Normalize Pillow's BC3 output to FIFA 20's native DXT5 container."""
    if len(body) < 128 or not body.startswith(b"DDS ") or body[84:88] != b"DXT5":
        return None
    height, width = struct.unpack_from("<II", body, 12)
    if (width, height) != (220, 256):
        return None
    payload_size = ((width + 3) // 4) * ((height + 3) // 4) * 16
    if len(body) not in {128 + payload_size, 128 + payload_size + 8}:
        return None
    normalized = bytearray(body[:128 + payload_size])
    # FIFA's own player-head DDS files use the complete BC3 linear size, a
    # zero RGB bit count for FOURCC formats, TEXTURE|COMPLEX caps, and an
    # eight-byte zero trailer. Pillow's writer uses a row-size value, reports
    # 32 bits, omits COMPLEX, and omits the trailer; FIFA's D3D12 loader turns
    # that malformed description into CreateCommittedResource(E_INVALIDARG).
    struct.pack_into("<I", normalized, 20, payload_size)
    struct.pack_into("<I", normalized, 88, 0)
    struct.pack_into("<I", normalized, 108, 0x1002)
    normalized.extend(b"\0" * 8)
    return bytes(normalized)


def _read_cached_dds(cache_file: Path) -> bytes | None:
    try:
        body = cache_file.read_bytes()
    except OSError:
        return None
    native = _native_player_head_dds(body)
    # Never pass a merely Pillow-readable DDS to the game. Only the exact
    # container contract observed in FIFA 20's accepted CDN files is safe.
    if native != body:
        return None
    return body


def _download_bytes(url: str, *, referer: str = "", limit: int = 4 * 1024 * 1024) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 FIFA20-LocalFUT/0.9",
        "Accept": "image/avif,image/webp,image/apng,image/png,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(limit + 1)
    if not body or len(body) > limit:
        raise ValueError(f"unexpected download size: {len(body)}")
    return body


def _catalog_portrait_png_url(resource_id: int, meta: dict[str, Any]) -> str | None:
    """Resolve the portrait belonging to this exact WeFUT card definition."""
    card = meta.get("wefutCard") or {}
    source = str(card.get("resourceIdSource", ""))
    dynamic_id = _int_or(meta.get("dynamicImageResourceId"), 0)
    if dynamic_id > 0:
        avatar_id = dynamic_id
        is_custom = True
    elif source == "wefut-custom-avatar":
        avatar_id = int(resource_id)
        is_custom = True
    else:
        # Some catalog rows were imported while their detail page was
        # temporarily unavailable and therefore have a local revision ID.
        # Resolve the image from the exact card page instead of guessing from
        # the revision bits; that guess is what put another player's portrait
        # on Walker, Verdi, and other promo cards.
        page_url = str(card.get("url", ""))
        if not page_url:
            return None
        html_body = _download_bytes(page_url, limit=2 * 1024 * 1024).decode("utf-8", "ignore")
        custom = re.search(r"fut20/playeravatars/custom/(\d+)\.png", html_body, re.IGNORECASE)
        if custom:
            avatar_id = int(custom.group(1))
            is_custom = True
        else:
            native = re.search(r"fut20/playeravatars/(\d+)\.png", html_body, re.IGNORECASE)
            if not native:
                return None
            avatar_id = int(native.group(1))
            is_custom = False
    folder = "custom/" if is_custom else ""
    return f"https://static.wefut.com/assets/images/fut20/playeravatars/{folder}{avatar_id}.png"


def _compose_catalog_portrait(rgba: Any) -> Any:
    """Accept only a prepared transparent dynamic-player canvas.

    WeFUT's /custom/ namespace contains both 485x567 prepared canvases and
    ordinary square player photos. Square replacements cover FIFA's initially
    correct native portrait and are the source of the oversized face/white
    rectangle seen on many MOTM, IF and UCL cards. Rejecting them lets the
    dynamic endpoint continue to the installed EA DDS or native atlas fallback.
    """
    width, height = rgba.size
    prepared_ratio = 485.0 / 567.0
    is_prepared_canvas = (
        width >= 320
        and height >= 375
        and abs((float(width) / float(height)) - prepared_ratio) <= 0.06
    )
    if is_prepared_canvas:
        return rgba.resize((220, 256), Image.Resampling.LANCZOS)
    return None


def _cached_catalog_player_head(resource_id: int) -> bytes | None:
    """Create a native-size DXT5 head from the exact card's transparent art."""
    if not bool(CFG.get("prefer_catalog_player_images", True)):
        return None
    # All Icon Moments portraits plus the reported promo regressions ship with
    # the release. This keeps those cards correct even before the first online
    # cache fill and avoids making the pack reveal wait on an image host.
    bundled = _read_cached_dds(ROOT / "playerheads" / f"p{resource_id}.dds")
    if bundled:
        return bundled
    if Image is None:
        log.warning("Pillow unavailable; cannot build player portrait: %s", _pillow_import_error)
        return None
    meta = player_meta(resource_id)
    if not meta or not bool(meta.get("special")):
        return None
    source = str((meta.get("wefutCard") or {}).get("resourceIdSource", ""))
    if not meta.get("dynamicImageResourceId") and source not in {
        "wefut-custom-avatar", "local-revision-fallback", "wefut-native-avatar",
    }:
        return None

    # v12 never turns a square catalog photo into a late-loading dynamic DDS.
    # Ignoring v11 is essential because it can contain those generated images.
    cache_file = CONTENT_CACHE / "catalog-playerheads-v12" / f"p{resource_id}.dds"
    cached = _read_cached_dds(cache_file)
    if cached:
        return cached
    normalized_key = str(cache_file.resolve()).lower()
    with _CONTENT_FETCH_LOCK:
        file_lock = _CONTENT_FILE_LOCKS.setdefault(normalized_key, threading.Lock())
    with file_lock:
        cached = _read_cached_dds(cache_file)
        if cached:
            return cached
        portrait_url = ""
        try:
            portrait_url = _catalog_portrait_png_url(resource_id, meta) or ""
            if not portrait_url:
                return None
            png = _download_bytes(portrait_url, referer=str((meta.get("wefutCard") or {}).get("url", "https://wefut.com/")))
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("portrait response is not PNG")
            with Image.open(io.BytesIO(png)) as source_image:
                rgba = source_image.convert("RGBA")
                if not rgba.getchannel("A").getbbox():
                    raise ValueError("portrait is fully transparent")
                portrait = _compose_catalog_portrait(rgba)
                if portrait is None:
                    log.info(
                        "Rejected unprepared catalog portrait resource=%d size=%dx%d; using native fallback",
                        resource_id, rgba.width, rgba.height,
                    )
                    return None
                output = io.BytesIO()
                portrait.save(output, format="DDS", pixel_format="DXT5")
                body = _native_player_head_dds(output.getvalue())
            if body is None:
                raise ValueError("generated portrait does not match FIFA's native DDS contract")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_file.with_suffix(".dds.tmp")
            temporary.write_bytes(body)
            temporary.replace(cache_file)
            log.info(
                "Cached exact catalog portrait resource=%d player=%s source=%s",
                resource_id, meta.get("name", ""), portrait_url,
            )
            return body
        except Exception as exc:
            log.warning(
                "Exact catalog portrait unavailable resource=%d player=%s source=%s error=%s",
                resource_id, meta.get("name", ""), portrait_url, exc,
            )
            return None


def _prime_pack_player_heads(items: list[dict[str, Any]]) -> None:
    """Begin portrait preparation without blocking the pack purchase reply."""
    if not bool(CFG.get("prefetch_pack_player_images", True)):
        return
    resource_ids = sorted({
        canonical_player_resource_id(_int_or(item.get("resourceId"), 0))
        for item in items
        if (
            isinstance(item, dict)
            and str(item.get("itemType", "player") or "player").lower() == "player"
            and _int_or(item.get("resourceId"), 0) > 0
        )
    })
    if not resource_ids:
        return

    queued = 0

    def fetch(resource_id: int) -> None:
        started = time.monotonic()
        try:
            with _PLAYER_HEAD_PRIME_SLOTS:
                resolved = bool(_cached_catalog_player_head(resource_id))
            log.info(
                "Background player-head preparation resource=%d resolved=%s elapsed=%.2fs",
                resource_id, resolved, time.monotonic() - started,
            )
        except Exception:
            log.exception("Background player-head preparation failed resource=%d", resource_id)
        finally:
            with _PLAYER_HEAD_PRIME_LOCK:
                _PLAYER_HEAD_PRIME_ACTIVE.discard(resource_id)

    # The 0.9.17 implementation waited for every future here. The supplied
    # trace shows that kept a 25-special pack request open for 46.52 seconds
    # and even caused the client to retry the purchase. Daemon workers warm
    # exact catalog textures concurrently while this request returns at once.
    for resource_id in resource_ids:
        with _PLAYER_HEAD_PRIME_LOCK:
            if resource_id in _PLAYER_HEAD_PRIME_ACTIVE:
                continue
            _PLAYER_HEAD_PRIME_ACTIVE.add(resource_id)
        try:
            threading.Thread(
                target=fetch,
                args=(resource_id,),
                name=f"fut-head-{resource_id}",
                daemon=True,
            ).start()
            queued += 1
        except Exception:
            with _PLAYER_HEAD_PRIME_LOCK:
                _PLAYER_HEAD_PRIME_ACTIVE.discard(resource_id)
            log.exception("Could not schedule player-head preparation resource=%d", resource_id)
    log.info("Queued %d/%d pack player heads without blocking pack response", queued, len(resource_ids))


def _dynamic_fut20_content(path: str) -> tuple[int, bytes, str] | None:
    """Resolve card backgrounds and per-player heads requested by the PC client."""
    normalized = path.lower()
    if _CARD_BG_RE.fullmatch(normalized):
        # The CDN spelling is case-sensitive for the itemBGs directory.
        remote_path = re.sub(r"/itembgs/", "/itemBGs/", path, flags=re.IGNORECASE)
        body = _download_fut20_dds(remote_path, normalized)
        return (200, body, "application/octet-stream") if body else (404, b"", "text/plain")

    head_match = _PLAYER_HEAD_RE.fullmatch(normalized)
    if head_match:
        raw_requested_id = int(head_match.group(1))
        requested_id = canonical_player_resource_id(
            PLAYER_DYNAMIC_RESOURCE_ALIASES.get(raw_requested_id, raw_requested_id)
        )
        # Prefer the card-specific catalog artwork. A native revision may map
        # to a different atlas entry, while a 404 leaves FIFA free to reuse a
        # previously loaded texture. Returning an explicit, cached DDS keeps
        # both identity and placement deterministic.
        catalog_body = _cached_catalog_player_head(requested_id)
        if catalog_body:
            return 200, catalog_body, "application/octet-stream"
        # Build every id this head could really live under. Special/promo cards
        # ask for p<resourceId>.dds; when that id is a locally-assigned revision
        # (no such asset on EA's CDN) fall back through the card's real base
        # asset, its explicit dynamic-image id, and the base id embedded in the
        # low 24 bits of the revision id.
        meta = player_meta(requested_id)
        base_from_bits = requested_id & 0x00FFFFFF
        candidate_ids: list[int] = []
        source = str((meta.get("wefutCard") or {}).get("resourceIdSource", ""))
        candidates = [int(meta.get("assetId", 0) or 0)]
        # A custom-avatar/local Icon Moments resource id does not belong to
        # EA's CDN. A numerically valid response for that guessed URL may be a
        # different player's portrait, so use the verified base asset instead.
        if source not in {"wefut-custom-avatar", "fifa-icon-moments-revision"}:
            candidates.insert(0, requested_id)
        candidates.extend((
            base_from_bits,
            int(player_meta(base_from_bits).get("assetId", 0) or 0),
        ))
        for candidate in candidates:
            if candidate and candidate not in candidate_ids:
                candidate_ids.append(candidate)
        for candidate_id in candidate_ids:
            remote_path = f"/fut/playerheads/g4/single/p{candidate_id}.dds"
            body = _download_fut20_dds(remote_path, remote_path.lower())
            if body:
                return 200, body, "application/octet-stream"
        # Nothing resolved: a real 404 lets the client keep the head it already
        # has from the bundled 2D atlas. Never a 200 here -- a stand-in texture
        # overwrites that atlas head and a `{}` body is decoded as garbage.
        return 404, b"", "text/plain"
    return None


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _int_or(value: Any, default: int = 0) -> int:
    try:
        if value is None or isinstance(value, bool):
            return int(default)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _clamp_int(value: Any, minimum: int, maximum: int, default: int = 0) -> int:
    return max(int(minimum), min(int(maximum), _int_or(value, default)))


class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self.lock:
            c = self.conn.cursor()
            c.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(
                    id INTEGER PRIMARY KEY,
                    resource_id INTEGER NOT NULL,
                    pile TEXT NOT NULL DEFAULT 'club',
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS squads(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS unopened_packs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id INTEGER NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_runs(
                    id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sbc_squads(
                    challenge_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sbc_completions(
                    challenge_id INTEGER PRIMARY KEY,
                    times_completed INTEGER NOT NULL DEFAULT 0,
                    completed_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sbc_set_completions(
                    set_id INTEGER PRIMARY KEY,
                    times_completed INTEGER NOT NULL DEFAULT 0,
                    completed_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS auctions(
                    trade_id INTEGER PRIMARY KEY,
                    item_id INTEGER NOT NULL DEFAULT 0,
                    resource_id INTEGER NOT NULL,
                    seller_type TEXT NOT NULL,
                    seller_name TEXT NOT NULL,
                    starting_bid INTEGER NOT NULL,
                    current_bid INTEGER NOT NULL DEFAULT 0,
                    buy_now_price INTEGER NOT NULL,
                    highest_bidder TEXT NOT NULL DEFAULT '',
                    expires_at INTEGER NOT NULL,
                    duration INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    item_data TEXT NOT NULL,
                    coins_claimed INTEGER NOT NULL DEFAULT 0,
                    item_delivered INTEGER NOT NULL DEFAULT 0,
                    watched INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS auctions_status_seller
                    ON auctions(status, seller_type);
                CREATE TABLE IF NOT EXISTS player_pick_tokens(
                    token_id INTEGER PRIMARY KEY,
                    pool_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    option_count INTEGER NOT NULL,
                    token_json TEXT NOT NULL,
                    pool_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    created_at INTEGER NOT NULL,
                    redeemed_at INTEGER,
                    resolved_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS player_pick_sessions(
                    token_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER,
                    selected_candidate_id INTEGER,
                    selected_resource_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS player_pick_candidates(
                    candidate_id INTEGER PRIMARY KEY,
                    token_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    resource_id INTEGER NOT NULL,
                    owned INTEGER NOT NULL DEFAULT 0,
                    item_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS player_pick_candidates_token
                    ON player_pick_candidates(token_id, ordinal);
                CREATE TABLE IF NOT EXISTS player_pick_receipts(
                    token_id INTEGER PRIMARY KEY,
                    candidate_id INTEGER NOT NULL,
                    resource_id INTEGER NOT NULL,
                    item_json TEXT NOT NULL,
                    selected_at INTEGER NOT NULL
                );
                """
            )
            self.conn.commit()
            self.set_default("credits", int(CFG["credits"]))
            self.set_default("next_item_id", 900000000000)
            self.set_default("next_trade_id", 700000000)
            self.set_default("sid", "LOCAL20-" + sha1_text(str(time.time()))[:20].upper())
            self.set_default("token", "LOCAL20TOKEN-" + sha1_text(str(time.time() + 1))[:24].upper())
            self.set_default("established", now_s())
            self.set_default("account_id", str(CFG.get("test_account_id", "local-test-account-001")))
            self.set_default("nucleus_id", int(CFG["nucleus_id"]))
            self.set_default("persona_id", int(CFG["persona_id"]))
            self.set_default("club_id", int(CFG["club_id"]))
            self.set_default("club_name", str(CFG["club_name"]))
            self.set_default("club_abbr", str(CFG["club_abbr"]))
            self.set_default("global_match_record", {"wins": 0, "draws": 0, "losses": 0})
            self.set_default("global_match_record_keys", [])
            self.set_default("entitlements", [
                {"id": 1, "grantName": str(CFG["game_sku"]), "tag": "ONLINE_ACCESS", "status": "ACTIVE"},
                {"id": 2, "grantName": str(CFG["fut_sku"]), "tag": "FUT_ACCESS", "status": "ACTIVE"},
            ])
            if not self._has_any_items():
                self._seed_items()
            self._enrich_existing_player_items()
            self._migrate_embedded_player_resources()
            self._enrich_existing_manager_items()
            self._ensure_initial_club_manager()
            self._ensure_initial_consumables()
            if not self._has_any_squads():
                self._seed_squad()
            self._repair_misclassified_mixed_concept_squads()

    def _ensure_initial_club_manager(self) -> dict[str, Any] | None:
        """Give every existing or new club a varied FIFA 20 manager pool."""
        if not MANAGER_DB:
            return None
        managers = [
            item for item in self.list_items()
            if str(item.get("itemType", "")).lower() == "manager"
        ]
        owned_resource_ids = {
            int(item.get("resourceId", 0) or 0) for item in managers
        }

        seeded = 0
        for resource_id in _STARTER_MANAGER_RESOURCE_IDS:
            if resource_id not in MANAGER_DB or resource_id in owned_resource_ids:
                continue
            manager = self.make_manager_item(resource_id, "club")
            managers.append(manager)
            owned_resource_ids.add(resource_id)
            seeded += 1

        # Retain the old fallback if a custom manager catalog contains none of
        # the curated definitions.
        if not managers:
            resource_id = min(
                MANAGER_DB,
                key=lambda rid: (-int(MANAGER_DB[rid].get("rating", 0) or 0), int(rid)),
            )
            managers.append(self.make_manager_item(int(resource_id), "club"))
            seeded += 1

        managers.sort(key=lambda item: (
            -int(item.get("rating", 0) or 0), int(item.get("id", 0) or 0)
        ))
        manager = managers[0]
        if str(manager.get("pile", "club")) not in ("club", "squad"):
            self.update_item_pile(int(manager["id"]), "club")
            manager = self.get_item(int(manager["id"])) or manager
        if seeded:
            log.info(
                "Added %d FIFA 20 starter manager(s); club now owns %d manager card(s)",
                seeded, len(managers),
            )
        return manager

    def _ensure_initial_consumables(self) -> None:
        """Grant a small native consumable collection once per local save."""
        marker = "starter_consumables_v094_seeded"
        if bool(self.get(marker, False)):
            return
        seeded = 0
        for resource_id, stack_count in _STARTER_CONSUMABLE_STACKS.items():
            self.make_consumable_item(resource_id, "club", stack_count)
            seeded += 1
        self.set(marker, True)
        log.info("Added %d FIFA 20 starter consumable stack(s)", seeded)

    def set_default(self, key: str, value: Any) -> None:
        row = self.conn.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))
            self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row[0])
            except Exception:
                return row[0]

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    def credits(self) -> int:
        return int(self.get("credits", 0))

    def add_credits(self, amount: int) -> int:
        v = max(0, self.credits() + int(amount))
        self.set("credits", v)
        return v

    def global_match_record(self) -> dict[str, int]:
        raw = self.get("global_match_record", {})
        if not isinstance(raw, dict):
            raw = {}
        wins = max(0, _int_or(raw.get("wins"), 0))
        draws = max(0, _int_or(raw.get("draws"), 0))
        losses = max(0, _int_or(raw.get("losses"), 0))
        return {
            "wins": wins, "draws": draws, "losses": losses,
            "matchesPlayed": wins + draws + losses,
        }

    def record_global_match(self, result: str, event_key: str,
                            xp_eligible: bool = True) -> bool:
        """Persist one FUT-wide W/D/L result exactly once."""
        normalized = str(result or "LOSS").upper()
        field = "wins" if normalized == "WIN" else "draws" if normalized == "DRAW" else "losses"
        key = str(event_key or "").strip()
        if not key:
            return False
        with self.lock:
            # Freeze the season's baseline before the first settled result, so
            # playing without opening Objectives never loses that match's XP.
            season = _scmp_state()
            seen_raw = self.conn.execute(
                "SELECT value FROM kv WHERE key='global_match_record_keys'"
            ).fetchone()
            try:
                seen = json.loads(seen_raw[0]) if seen_raw else []
            except Exception:
                seen = []
            if not isinstance(seen, list):
                seen = []
            if key in seen:
                return False
            record_raw = self.conn.execute(
                "SELECT value FROM kv WHERE key='global_match_record'"
            ).fetchone()
            try:
                record = json.loads(record_raw[0]) if record_raw else {}
            except Exception:
                record = {}
            if not isinstance(record, dict):
                record = {}
            record = {
                "wins": max(0, _int_or(record.get("wins"), 0)),
                "draws": max(0, _int_or(record.get("draws"), 0)),
                "losses": max(0, _int_or(record.get("losses"), 0)),
            }
            record[field] += 1
            seen = [*seen[-511:], key]
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES('global_match_record',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(record, separators=(",", ":")),),
            )
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES('global_match_record_keys',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(seen, separators=(",", ":")),),
            )
            # Objectives need mode-specific settled results, not a guess from
            # the current menu. This shares the same idempotency gate as the
            # club-wide record, so report retries never advance Objectives.
            mode = key.split(":", 1)[0]
            if mode in ("squad_battles", "draft", "rivals", "champion", "generic"):
                mode_row = self.conn.execute(
                    "SELECT value FROM kv WHERE key='objectives_mode_record'"
                ).fetchone()
                try:
                    mode_record = json.loads(mode_row[0]) if mode_row else {}
                except Exception:
                    mode_record = {}
                if not isinstance(mode_record, dict):
                    mode_record = {}
                entry = mode_record.get(mode, {})
                if not isinstance(entry, dict):
                    entry = {}
                entry = {
                    "matches": max(0, _int_or(entry.get("matches"), 0)) + 1,
                    "wins": max(0, _int_or(entry.get("wins"), 0)) + (1 if normalized == "WIN" else 0),
                }
                mode_record[mode] = entry
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('objectives_mode_record',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(mode_record, separators=(",", ":")),),
                )
            gained_xp = SCMP.settled_match_xp(mode, normalized) if xp_eligible else 0
            if gained_xp:
                season["xp"] = max(0, _int_or(season.get("xp"), 0)) + gained_xp
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('scmp_campaign_v1',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(season, separators=(",", ":")),),
                )
            revision_row = self.conn.execute(
                "SELECT value FROM kv WHERE key='scmp_revision'"
            ).fetchone()
            try:
                revision = _int_or(json.loads(revision_row[0]), 0) if revision_row else 0
            except Exception:
                revision = 0
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES('scmp_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(max(0, revision) + 1),),
            )
            self.conn.commit()
        log.info("GLOBAL RECORD result=%s key=%s record=%s seasonXp=%s",
                 normalized, key, record, gained_xp)
        # FIFA 20 keeps the Season Objectives/SCMP model alive for the whole
        # FUT session.  HTTP no-cache headers and fresh /scmp/details responses
        # are not enough to invalidate that native model.  Nudge the existing
        # Blaze UserSessions subscription after the match transition, matching
        # the same notification family retail Blaze uses for live user/session
        # updates.  Send twice because the first notification can arrive while
        # the client is still leaving the match presentation.
        new_revision = max(0, revision) + 1
        _mark_scmp_campaign_stale(new_revision)
        # The 1.0.26 trace proved that plain ExtendedData/UserUpdated pulses
        # reach FIFA but do not invalidate the long-lived SCMP campaign model.
        # Queue a stronger soft-session refresh: the first Blaze pulse also
        # re-announces the existing LoginSession (same identity/token, no
        # logout), followed by the normal extended-data/user-updated pulses.
        # This gives the client a chance to rebuild FUT's authenticated
        # per-session services and re-fetch /scmp/campaign/active without
        # forcing the player to leave Ultimate Team.
        _schedule_scmp_live_refresh(new_revision)
        return True

    def sid(self) -> str:
        return str(self.get("sid"))

    def token(self) -> str:
        return str(self.get("token"))

    def draft_run(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM draft_runs WHERE id=1").fetchone()
            if not row:
                return None
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError, json.JSONDecodeError):
                log.warning("Ignoring malformed saved Draft run")
                return None
            return data if isinstance(data, dict) else None

    def save_draft_run(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO draft_runs(id,data) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json.dumps(data),),
            )
            self.conn.commit()

    def clear_draft_run(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM draft_runs WHERE id=1")
            self.conn.commit()

    def _has_any_items(self) -> bool:
        return self.conn.execute("SELECT 1 FROM items LIMIT 1").fetchone() is not None

    def _has_any_squads(self) -> bool:
        return self.conn.execute("SELECT 1 FROM squads LIMIT 1").fetchone() is not None

    def next_item_id(self) -> int:
        with self.lock:
            v = int(self.get("next_item_id", 900000000000)) + 1
            self.set("next_item_id", v)
            return v

    def next_trade_id(self) -> int:
        with self.lock:
            value = int(self.get("next_trade_id", 700000000)) + 1
            self.set("next_trade_id", value)
            return value

    @staticmethod
    def _auction_from_row(row: sqlite3.Row) -> dict[str, Any]:
        model = dict(row)
        try:
            model["item_data"] = json.loads(model.get("item_data") or "{}")
        except Exception:
            model["item_data"] = {}
        return model

    def create_auction(
        self,
        *,
        resource_id: int,
        item_data: dict[str, Any],
        seller_type: str,
        seller_name: str,
        starting_bid: int,
        buy_now_price: int,
        duration: int,
        item_id: int = 0,
    ) -> dict[str, Any]:
        trade_id = self.next_trade_id()
        created_at = now_s()
        duration = max(60, int(duration))
        model = {
            "trade_id": trade_id,
            "item_id": int(item_id),
            "resource_id": int(resource_id),
            "seller_type": str(seller_type),
            "seller_name": str(seller_name),
            "starting_bid": int(starting_bid),
            "current_bid": 0,
            "buy_now_price": int(buy_now_price),
            "highest_bidder": "",
            "expires_at": created_at + duration,
            "duration": duration,
            "status": "active",
            "created_at": created_at,
            "item_data": dict(item_data),
            "coins_claimed": 0,
            "item_delivered": 0,
            "watched": 0,
        }
        with self.lock:
            self.conn.execute(
                "INSERT INTO auctions(trade_id,item_id,resource_id,seller_type,seller_name,"
                "starting_bid,current_bid,buy_now_price,highest_bidder,expires_at,duration,status,"
                "created_at,item_data,coins_claimed,item_delivered,watched) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    model["trade_id"], model["item_id"], model["resource_id"],
                    model["seller_type"], model["seller_name"], model["starting_bid"],
                    model["current_bid"], model["buy_now_price"], model["highest_bidder"],
                    model["expires_at"], model["duration"], model["status"],
                    model["created_at"], json.dumps(model["item_data"], separators=(",", ":")),
                    model["coins_claimed"], model["item_delivered"], model["watched"],
                ),
            )
            self.conn.commit()
        return model

    def list_auctions(self, seller_type: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if seller_type is None:
                rows = self.conn.execute("SELECT * FROM auctions ORDER BY trade_id").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM auctions WHERE seller_type=? ORDER BY trade_id", (str(seller_type),)
                ).fetchall()
        return [self._auction_from_row(row) for row in rows]

    def get_auction(self, trade_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM auctions WHERE trade_id=?", (int(trade_id),)
            ).fetchone()
        return self._auction_from_row(row) if row else None

    def update_auction(self, trade_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "current_bid", "highest_bidder", "expires_at", "status", "coins_claimed",
            "item_delivered", "watched", "item_data", "starting_bid", "buy_now_price",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return self.get_auction(trade_id)
        if "item_data" in clean:
            clean["item_data"] = json.dumps(clean["item_data"], separators=(",", ":"))
        assignments = ",".join(f"{key}=?" for key in clean)
        with self.lock:
            self.conn.execute(
                f"UPDATE auctions SET {assignments} WHERE trade_id=?",
                (*clean.values(), int(trade_id)),
            )
            self.conn.commit()
        return self.get_auction(trade_id)

    def delete_auction(self, trade_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM auctions WHERE trade_id=?", (int(trade_id),))
            self.conn.commit()
            return bool(cur.rowcount)

    def detach_item_from_squads(self, item_id: int) -> int:
        """Remove a listed card from every saved squad without disturbing slots."""
        changed = 0
        with self.lock:
            rows = self.conn.execute("SELECT id,data FROM squads").fetchall()
            for row in rows:
                try:
                    squad = json.loads(row["data"])
                except Exception:
                    continue
                players = squad.get("players", []) if isinstance(squad, dict) else []
                if not isinstance(players, list):
                    continue
                kept = []
                removed = False
                for slot in players:
                    if not isinstance(slot, dict):
                        kept.append(slot)
                        continue
                    embedded = slot.get("itemData") if isinstance(slot.get("itemData"), dict) else {}
                    slot_item_id = _int_or(slot.get("itemId") or embedded.get("id") or slot.get("id"), 0)
                    if slot_item_id == int(item_id):
                        removed = True
                    else:
                        kept.append(slot)
                if removed:
                    squad["players"] = kept
                    self.conn.execute(
                        "UPDATE squads SET data=? WHERE id=?",
                        (json.dumps(squad, separators=(",", ":")), int(row["id"])),
                    )
                    changed += 1
            if changed:
                self.conn.commit()
        return changed

    def player_discard_value(self, resource_id: int) -> int:
        """Local FUT quick-sell values.

        User-selected economy:
          common/base non-rare = 360
          base rare            = 700
          special              = 10,000-17,000 based on card rarity/type
        """
        resource_id = canonical_player_resource_id(resource_id)
        meta = player_meta(resource_id)
        try:
            rareflag = int(meta.get("rareflag", 0) or 0)
        except Exception:
            rareflag = 0

        is_special = bool(meta.get("special", False)) or rareflag > 1

        if not is_special:
            return 700 if rareflag == 1 else 360

        # FIFA 20 special shells. Values deliberately stay in the requested
        # 10k-17k range and increase roughly with prestige of the card type.
        special_values = {
            3: 10000,   # IF / TOTW
            4: 11500,   # Hero / Purple
            5: 17000,   # TOTY
            6: 13500,   # Record Breaker
            7: 11000,   # St Patrick / Green
            8: 12500,   # MOTM / Orange
            9: 12000,   # FUTTIES / Pink
            10: 12000,  # Teal / other special
            11: 16000,  # TOTS
            12: 15000,  # Legend
            13: 13500,
            14: 13500,
        }
        return int(special_values.get(rareflag, 12000))

    def build_player_item(
        self,
        resource_id: int,
        item_id: int,
        pile: str = "club",
        loan_games: int = 0,
    ) -> dict[str, Any]:
        """Build a FIFA 20 player-item dict without persisting it.

        Shared by make_player_item (club/pack items are stored) and the FUT
        Draft flow (choice cards are transient and live only inside the saved
        draft run).
        """
        meta = player_meta(resource_id)
        loan_games = max(0, int(loan_games))
        return {
            "id": int(item_id),
            "timestamp": now_s(),
            "resourceId": int(resource_id),
            "assetId": _native_player_asset_id(meta, resource_id),
            "rating": int(meta.get("rating", 0)),
            "itemState": "free",
            "rareflag": int(meta.get("rareflag", CFG.get("rare_flag", 1))),
            "formation": "f442",
            "leagueId": int(meta.get("leagueId", 0)),
            "injuryType": "none",
            "injuryGames": 0,
            "lastSalePrice": 0,
            "fitness": 99,
            "training": 0,
            "suspension": 0,
            "contract": 0 if loan_games else 7,
            "contracts": 0 if loan_games else 7,
            "preferredPosition": str(meta.get("preferredPosition", "")),
            "playStyle": 0,
            "discardValue": 0 if loan_games else self.player_discard_value(resource_id),
            "itemType": "player",
            "cardsubtypeid": int(meta.get("cardsubtypeid", 0) or 0),
            "owners": 1,
            "untradeable": bool(loan_games),
            "morale": 50,
            "statsList": [],
            "lifetimeStats": [],
            "attributeList": player_attribute_list(resource_id),
            "teamid": int(meta.get("teamid", 0)),
            "assists": 0,
            "lifetimeAssists": 0,
            "loans": loan_games,
            "loyaltyBonus": 1,
            "pile": pile,
            "nation": int(meta.get("nation", 0)),
            "resourceGameYear": 2020,
            "marketDataMinPrice": 150,
            "marketDataMaxPrice": 15000000,
            **_player_identity_fields(meta),
        }

    def build_manager_item(self, resource_id: int, item_id: int, pile: str = "club") -> dict[str, Any]:
        """Build a FIFA 20 manager-item dict from a real manager definition."""
        meta = manager_meta(resource_id)
        if not meta:
            raise ValueError(f"unknown FIFA 20 manager definition {resource_id}")
        rating = int(meta.get("rating", 75) or 75)
        rareflag = int(meta.get("rareflag", 1 if rating >= 80 else 0) or 0)
        asset_id = int(meta.get("assetId", 0) or 0)
        # FUT manager resource cards occupy the 1,000,000 family while the
        # native manager database uses the underlying manager id.  Supplying
        # the resource-card id as assetId renders a card, but FIFA's local
        # pre-match validator cannot resolve it and reports that no manager is
        # assigned.  Older generated catalogs copied the resource id here, so
        # also repair those values without requiring a catalog rewrite.
        if asset_id <= 0 or asset_id == int(resource_id):
            candidate = int(resource_id) - 1_000_000
            if 0 < candidate < 1_000_000:
                asset_id = candidate
        return {
            "id": int(item_id),
            "timestamp": now_s(),
            "resourceId": int(resource_id),
            "assetId": asset_id,
            "carddbid": int(meta.get("carddbid", resource_id) or resource_id),
            "stackCount": 1,
            "untradeableCount": 0,
            "rating": rating,
            "itemState": "free",
            "rareflag": rareflag,
            "leagueId": int(meta.get("leagueId", 0) or 0),
            "nation": int(meta.get("nation", 0) or 0),
            "teamid": int(meta.get("teamid", 0) or 0),
            "contract": 7,
            "contracts": 7,
            "negotiation": int(meta.get("negotiation", 0) or 0),
            "discardValue": 300 if rareflag else 150,
            "lastSalePrice": 0,
            "itemType": "manager",
            "cardsubtypeid": 4,
            "category": 0,
            "owners": 1,
            "untradeable": False,
            "pile": str(pile),
            "resourceGameYear": 2020,
            "marketDataMinPrice": 150,
            "marketDataMaxPrice": 15000000,
            "firstName": str(meta.get("firstName", "") or ""),
            "lastName": str(meta.get("lastName", "") or ""),
            "commonname": str(meta.get("commonname", meta.get("commonName", "")) or ""),
        }

    def _enrich_existing_manager_items(self) -> None:
        """Repair manager asset IDs created by the original 0.8.1 hotfix."""
        with self.lock:
            rows = self.conn.execute("SELECT id, resource_id, data FROM items").fetchall()
            changed = 0
            for row in rows:
                try:
                    data = json.loads(row["data"])
                except Exception:
                    continue
                if not isinstance(data, dict) or str(data.get("itemType", "")).lower() != "manager":
                    continue
                resource_id = int(row["resource_id"] or data.get("resourceId", 0) or 0)
                meta = manager_meta(resource_id)
                if not meta:
                    # Migrate FIFA 21-only manager cards already present in a
                    # tester save to deterministic, renderable FIFA 20 cards.
                    valid_ids = sorted(MANAGER_DB)
                    if not valid_ids:
                        continue
                    resource_id = valid_ids[int(row["id"]) % len(valid_ids)]
                    meta = manager_meta(resource_id)
                native = self.build_manager_item(
                    resource_id, int(row["id"]), str(data.get("pile", "club") or "club")
                )
                # Preserve mutable ownership values while repairing the native
                # definition fields used by card lookup and match validation.
                native.update({
                    "timestamp": int(data.get("timestamp", native["timestamp"]) or native["timestamp"]),
                    "stackCount": max(1, int(data.get("stackCount", 1) or 1)),
                    "untradeableCount": max(0, int(data.get("untradeableCount", 0) or 0)),
                    "itemState": str(data.get("itemState", "free") or "free"),
                    "contract": max(1, int(data.get("contract", data.get("contracts", 7)) or 7)),
                    "contracts": max(1, int(data.get("contracts", data.get("contract", 7)) or 7)),
                    "owners": max(1, int(data.get("owners", 1) or 1)),
                    "untradeable": bool(data.get("untradeable", False)),
                })
                if native != data:
                    self.conn.execute(
                        "UPDATE items SET resource_id=?, pile=?, data=? WHERE id=?",
                        (resource_id, native["pile"], json.dumps(native), int(row["id"])),
                    )
                    changed += 1
            if changed:
                self.conn.commit()
                log.info("Repaired %d existing FUT manager item(s) with native FIFA 20 definitions", changed)

    def make_player_item(self, resource_id: int, pile: str = "club") -> dict[str, Any]:
        item_id = self.next_item_id()
        # Keep the instance fields native, but also include the FIFA 20 base-card
        # metadata needed by the client during pack reveals and club rendering.
        item = self.build_player_item(resource_id, item_id, pile)
        with self.lock:
            self.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (item_id, int(resource_id), pile, json.dumps(item)),
            )
            self.conn.commit()
        return item

    def make_manager_item(self, resource_id: int, pile: str = "club") -> dict[str, Any]:
        item_id = self.next_item_id()
        item = self.build_manager_item(resource_id, item_id, pile)
        with self.lock:
            self.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (item_id, int(resource_id), str(pile), json.dumps(item)),
            )
            self.conn.commit()
        return item

    def build_consumable_item(
        self,
        resource_id: int,
        item_id: int,
        pile: str = "club",
        stack_count: int = 1,
    ) -> dict[str, Any]:
        """Build the nested dynamic row used by FIFA 20 consumable stacks."""
        meta = CONSUMABLE_DB.get(int(resource_id))
        if not meta:
            raise ValueError(f"unknown FIFA 20 consumable definition {resource_id}")
        source_member = str(meta["sourceMember"])
        item = {
            "id": int(item_id),
            "timestamp": now_s(),
            "resourceId": int(resource_id),
            "assetId": int(resource_id),
            "cardassetid": int(meta["cardassetid"]),
            "rating": int(meta["rating"]),
            "itemState": "free",
            "rareflag": int(meta["rareflag"]),
            "rareFlag": int(meta["rareflag"]),
            "itemType": "development",
            "cardsubtypeid": int(meta["cardsubtypeid"]),
            "pile": str(pile),
            "resourceGameYear": 2020,
            "discardValue": int(meta["discardValue"]),
            "lastSalePrice": 0,
            "owners": 1,
            "untradeable": False,
            "stackCount": max(1, int(stack_count)),
            "untradeableCount": 0,
            "amount": int(meta["amount"]),
            "bronze": int(meta["bronze"]),
            "silver": int(meta["silver"]),
            "gold": int(meta["gold"]),
            "weightrare": int(meta["rareflag"]),
            "consumables": 1,
            source_member: 1,
        }
        return item

    def make_consumable_item(
        self, resource_id: int, pile: str = "club", stack_count: int = 1
    ) -> dict[str, Any]:
        item_id = self.next_item_id()
        item = self.build_consumable_item(resource_id, item_id, pile, stack_count)
        with self.lock:
            self.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (item_id, int(resource_id), str(pile), json.dumps(item)),
            )
            self.conn.commit()
        return item

    def build_club_item(
        self,
        resource_id: int,
        item_id: int,
        pile: str = "club",
        item_state: str | None = None,
    ) -> dict[str, Any]:
        meta = CLUB_ITEM_DB.get(int(resource_id))
        if not meta:
            raise ValueError(f"unknown FIFA 20 club-item definition {resource_id}")
        item = {
            "id": int(item_id), "timestamp": now_s(),
            "resourceId": int(resource_id), "carddbid": int(resource_id),
            "itemState": str(item_state or meta.get("itemState", "free")),
            "owners": 1, "untradeable": False, "discardValue": 10,
            "lastSalePrice": 0, "resourceGameYear": 2020,
            "pile": str(pile),
            **{key: value for key, value in meta.items() if key != "itemState"},
        }
        return item

    def make_club_item(
        self,
        resource_id: int,
        pile: str = "club",
        item_state: str | None = None,
    ) -> dict[str, Any]:
        item_id = self.next_item_id()
        item = self.build_club_item(resource_id, item_id, pile, item_state)
        with self.lock:
            self.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (item_id, int(resource_id), str(pile), json.dumps(item)),
            )
            self.conn.commit()
        return item

    def activate_club_item(
        self, item_id: int, requested_state: str = "", activate_slot: int = -1
    ) -> dict[str, Any] | None:
        """Equip one owned club item and persist one active item per slot."""
        item = self.get_item(item_id)
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("itemType", "")).lower()
        slot_states = {
            0: "activeHomeKit", 1: "activeAwayKit", 2: "activeBadge",
            3: "activeBadge", 4: "activeBall", 5: "activeStadium",
            101: "activeHomeKit", 102: "activeAwayKit",
        }
        state = str(requested_state or "")
        if state.lower() in ("", "active", "free"):
            state = slot_states.get(int(activate_slot), "")
        if not state.startswith("active"):
            if item_type == "kit":
                state = "activeHomeKit" if _int_or(item.get("category"), 2) == 2 else "activeAwayKit"
            elif item_type == "custom":
                state = "activeBadge"
            elif item_type == "ball":
                state = "activeBall"
            elif item_type == "stadium":
                state = "activeStadium"
        valid_states = set(slot_states.values())
        if state not in valid_states:
            return None
        with self.lock:
            rows = self.conn.execute("SELECT id,data FROM items").fetchall()
            for row in rows:
                try:
                    other = json.loads(row["data"])
                except Exception:
                    continue
                if not isinstance(other, dict) or str(other.get("itemState", "")) != state:
                    continue
                other["itemState"] = "free"
                self.conn.execute(
                    "UPDATE items SET data=? WHERE id=?",
                    (json.dumps(other), int(row["id"])),
                )
            item["itemState"] = state
            item["pile"] = "club"
            self.conn.execute(
                "UPDATE items SET pile='club',data=? WHERE id=?",
                (json.dumps(item), int(item_id)),
            )
            self.conn.commit()
        return item

    def _enrich_existing_player_items(self) -> None:
        """Backfill metadata and migrate pre-0.9.15 non-native Icon IDs."""
        with self.lock:
            rows = self.conn.execute("SELECT id, resource_id, data FROM items").fetchall()
            changed = 0
            for row in rows:
                legacy_rid = int(row["resource_id"])
                rid = canonical_player_resource_id(legacy_rid)
                meta = player_meta(rid)
                if not meta:
                    continue
                data = json.loads(row["data"])
                data["resourceId"] = rid
                data["assetId"] = _native_player_asset_id(meta, rid)
                data["rating"] = int(meta.get("rating", data.get("rating", 0)))
                # Position modifiers are permanent owned-item mutations.  Only
                # backfill a missing/invalid value; never replace a saved one
                # with the player's base-card position during startup repair.
                saved_position = str(data.get("preferredPosition", "") or "").upper()
                valid_positions = {
                    "GK", "LB", "LWB", "CB", "RB", "RWB", "LM", "LW",
                    "CDM", "CM", "CAM", "RM", "RW", "LF", "CF", "RF", "ST",
                }
                data["preferredPosition"] = (
                    saved_position if saved_position in valid_positions
                    else str(meta.get("preferredPosition", "") or "").upper()
                )
                data["leagueId"] = int(meta.get("leagueId", data.get("leagueId", 0)))
                data["teamid"] = int(meta.get("teamid", data.get("teamid", 0)))
                data["nation"] = int(meta.get("nation", data.get("nation", 0)))
                data["rareflag"] = int(meta.get("rareflag", data.get("rareflag", CFG.get("rare_flag", 1))) or 0)
                data["cardsubtypeid"] = int(meta.get("cardsubtypeid", 0) or 0)
                data["discardValue"] = self.player_discard_value(rid)
                data["attributeList"] = player_attribute_list(rid)
                data.update(_player_identity_fields(meta))
                self.conn.execute(
                    "UPDATE items SET resource_id=?,data=? WHERE id=?",
                    (rid, json.dumps(data), int(row["id"])),
                )
                changed += 1
            if changed:
                self.conn.commit()
                log.info(
                    "Enriched %d existing FUT player items and migrated legacy Icon IDs",
                    changed,
                )

    def _migrate_embedded_player_resources(self) -> None:
        """Repair legacy Moments IDs inside saved squads, drafts, and auctions."""
        if not PLAYER_RESOURCE_ALIASES:
            return

        def rewrite(value: Any) -> bool:
            changed = False
            if isinstance(value, dict):
                raw_resource_id = _int_or(value.get("resourceId"), 0)
                resource_id = canonical_player_resource_id(raw_resource_id)
                if raw_resource_id > 0 and resource_id != raw_resource_id:
                    value["resourceId"] = resource_id
                    meta = player_meta(resource_id)
                    if meta:
                        value["assetId"] = _int_or(meta.get("assetId"), resource_id)
                    changed = True
                for child in value.values():
                    changed = rewrite(child) or changed
            elif isinstance(value, list):
                for child in value:
                    changed = rewrite(child) or changed
            return changed

        migrated = 0
        with self.lock:
            for table, key_column in (
                ("squads", "id"), ("draft_runs", "id"), ("sbc_squads", "challenge_id"),
            ):
                for row in self.conn.execute(
                    f"SELECT {key_column},data FROM {table}"
                ).fetchall():
                    try:
                        data = json.loads(row["data"])
                    except Exception:
                        continue
                    if rewrite(data):
                        self.conn.execute(
                            f"UPDATE {table} SET data=? WHERE {key_column}=?",
                            (json.dumps(data, separators=(",", ":")), int(row[key_column])),
                        )
                        migrated += 1

            for row in self.conn.execute(
                "SELECT trade_id,resource_id,item_data FROM auctions"
            ).fetchall():
                old_resource_id = int(row["resource_id"])
                resource_id = canonical_player_resource_id(old_resource_id)
                try:
                    item_data = json.loads(row["item_data"])
                except Exception:
                    item_data = {}
                changed = rewrite(item_data)
                if resource_id != old_resource_id or changed:
                    self.conn.execute(
                        "UPDATE auctions SET resource_id=?,item_data=? WHERE trade_id=?",
                        (
                            resource_id,
                            json.dumps(item_data, separators=(",", ":")),
                            int(row["trade_id"]),
                        ),
                    )
                    migrated += 1
            if migrated:
                self.conn.commit()
                log.info("Migrated legacy Icon IDs in %d saved container(s)", migrated)

    def _seed_items(self) -> None:
        roster = list(CFG.get("starter_players", [])) + list(CFG.get("reserve_players", []))
        for rid in roster:
            self.make_player_item(int(rid), "club")

    def list_items(self, pile: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if pile:
                rows = self.conn.execute("SELECT data FROM items WHERE pile=? ORDER BY id", (pile,)).fetchall()
            else:
                rows = self.conn.execute("SELECT data FROM items ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_item_pile(self, item_id: int, pile: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT data FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            data["pile"] = pile
            self.conn.execute("UPDATE items SET pile=?, data=? WHERE id=?", (pile, json.dumps(data), int(item_id)))
            self.conn.commit()
            return True

    def update_item_fields(self, item_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                return None
            try:
                data = json.loads(row[0])
            except Exception:
                return None
            if not isinstance(data, dict):
                return None
            data.update(fields)
            if "pile" in fields:
                self.conn.execute(
                    "UPDATE items SET pile=?,data=? WHERE id=?",
                    (str(data.get("pile", "club")), json.dumps(data), int(item_id)),
                )
            else:
                self.conn.execute("UPDATE items SET data=? WHERE id=?", (json.dumps(data), int(item_id)))
            self.conn.commit()
            return data

    def active_squad_item_ids(self) -> list[int]:
        with self.lock:
            row = self.conn.execute("SELECT data FROM squads WHERE active=1 ORDER BY id LIMIT 1").fetchone()
            if not row:
                row = self.conn.execute("SELECT data FROM squads ORDER BY id LIMIT 1").fetchone()
        if not row:
            return []
        try:
            squad = json.loads(row[0])
        except Exception:
            return []
        raw_players = squad.get("players", []) if isinstance(squad, dict) else []
        ids: list[int] = []
        if not isinstance(raw_players, list):
            return ids
        for slot in raw_players[:23]:
            if not isinstance(slot, dict):
                continue
            embedded = slot.get("itemData") if isinstance(slot.get("itemData"), dict) else {}
            try:
                iid = int(slot.get("itemId") or embedded.get("id") or slot.get("id") or 0)
            except Exception:
                iid = 0
            if iid > 0 and iid not in ids:
                ids.append(iid)
        return ids

    def consume_resource_instance(self, resource_id: int, target_ids: set[int] | None = None) -> bool:
        target_ids = target_ids or set()
        with self.lock:
            rows = self.conn.execute(
                "SELECT id,data FROM items WHERE resource_id=? ORDER BY id",
                (int(resource_id),),
            ).fetchall()
            for row in rows:
                iid = int(row["id"])
                if iid in target_ids:
                    continue
                try:
                    data = json.loads(row["data"])
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if str(data.get("itemType", "")).lower() in ("player", "manager"):
                    continue
                stack = max(1, _int_or(data.get("stackCount"), 1))
                if stack > 1:
                    data["stackCount"] = stack - 1
                    self.conn.execute("UPDATE items SET data=? WHERE id=?", (json.dumps(data), iid))
                else:
                    self.conn.execute("DELETE FROM items WHERE id=?", (iid,))
                self.conn.commit()
                return True
        return False

    def delete_item(self, item_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM items WHERE id=?", (int(item_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def discard_items(self, item_ids: list[int]) -> tuple[int, list[int]]:
        """Delete owned item instances and credit their stored server-side values.

        Never trusts a discard value supplied by the FIFA client. Each item is
        looked up in SQLite and its expected value is recalculated from players.json.
        """
        clean_ids = []
        seen = set()
        for raw in item_ids:
            try:
                iid = int(raw)
            except Exception:
                continue
            if iid > 0 and iid not in seen:
                seen.add(iid)
                clean_ids.append(iid)

        if not clean_ids:
            return self.credits(), []

        payout = 0
        deleted = []

        with self.lock:
            for iid in clean_ids:
                row = self.conn.execute(
                    "SELECT resource_id, data FROM items WHERE id=?",
                    (iid,),
                ).fetchone()
                if not row:
                    continue

                try:
                    data = json.loads(row["data"] if hasattr(row, "keys") else row[1])
                except Exception:
                    data = {}

                # Do not allow squad-bound cards to disappear through an odd client call.
                pile = str(data.get("pile", ""))
                if pile == "squad":
                    log.warning("Refused discard of squad-bound item %d", iid)
                    continue

                try:
                    rid = int(data.get("resourceId", row["resource_id"] if hasattr(row, "keys") else row[0]) or 0)
                except Exception:
                    rid = int(row["resource_id"] if hasattr(row, "keys") else row[0])

                value = self.player_discard_value(rid)
                payout += max(0, int(value))

                cur = self.conn.execute("DELETE FROM items WHERE id=?", (iid,))
                if cur.rowcount:
                    deleted.append(iid)

            if deleted and payout:
                current = int(self.get("credits", 0))
                new_total = max(0, current + payout)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("credits", json.dumps(new_total)),
                )
            self.conn.commit()

        log.warning(
            "QUICKSELL items=%s payout=%d newCredits=%d",
            deleted, payout, self.credits()
        )
        return self.credits(), deleted

    def clear_pile(self, pile: str) -> int:
        """Remove all transient items from a pile and return the number removed."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM items WHERE pile=?", (str(pile),))
            self.conn.commit()
            return int(cur.rowcount or 0)

    def owned_player_resource_ids(self) -> set[int]:
        """Return player definitions already owned in the club or a squad.

        Unassigned pack items are intentionally excluded: FIFA's duplicate marker
        should compare a newly opened card against cards the user already owns,
        not against the rest of the current pack reveal.
        """
        owned: set[int] = set()
        for pile in ("club", "squad"):
            for item in self.list_items(pile):
                if str(item.get("itemType", "player")) != "player":
                    continue
                try:
                    rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
                except Exception:
                    rid = 0
                if rid:
                    owned.add(rid)
        return owned

    def owned_player_item_by_resource_id(self, resource_id: int) -> dict[str, Any] | None:
        """Return an already-owned club/squad item for a player definition."""
        rid = int(resource_id or 0)
        if not rid:
            return None
        for pile in ("club", "squad"):
            for item in self.list_items(pile):
                if str(item.get("itemType", "player")) != "player":
                    continue
                try:
                    item_rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
                except Exception:
                    continue
                if item_rid == rid:
                    return item
        return None

    def duplicate_item_ids(self, items: list[dict[str, Any]] | None = None) -> list[int]:
        """Return item-instance IDs whose player definition is already owned."""
        owned = self.owned_player_resource_ids()
        check = items if items is not None else self.list_items("unassigned")
        dupes: list[int] = []
        for item in check:
            try:
                rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
                iid = int(item.get("id", 0) or 0)
            except Exception:
                continue
            if rid in owned and iid:
                dupes.append(iid)
        return dupes

    def mark_item_duplicate(self, item_id: int, is_duplicate: bool = True) -> bool:
        """Persist only a conservative duplicate marker/pile state."""
        with self.lock:
            row = self.conn.execute("SELECT data FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                return False
            try:
                data = json.loads(row[0])
            except Exception:
                return False
            # Do not inject UI-only symbols into the native FUT item schema.
            for key in ("IS_DUPLICATE", "isItemAlreadyInClub", "DUPLICATE_CARD_REF", "duplicateCardRef"):
                data.pop(key, None)
            data["duplicate"] = bool(is_duplicate)
            data["isDuplicate"] = bool(is_duplicate)
            if is_duplicate:
                data["pile"] = "unassigned"
            self.conn.execute("UPDATE items SET pile=?, data=? WHERE id=?",
                              (str(data.get("pile", "unassigned")), json.dumps(data), int(item_id)))
            self.conn.commit()
            return True

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        """Return one stored FUT item by item-instance id."""
        with self.lock:
            row = self.conn.execute("SELECT data FROM items WHERE id=?", (int(item_id),)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def would_duplicate_owned_player(self, item_id: int) -> bool:
        """True when moving this player into club/squad would duplicate a definition."""
        item = self.get_item(item_id)
        if not item or str(item.get("itemType", "player")) != "player":
            return False
        try:
            rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
        except Exception:
            return False
        if not rid:
            return False
        # Compare against cards already owned in club/squad. The item being moved
        # is normally still unassigned, so it is naturally excluded here.
        return rid in self.owned_player_resource_ids()

    def _seed_squad(self) -> None:
        players = [
            item for item in self.list_items("club")
            if str(item.get("itemType", "player")).lower() == "player"
        ][:11]
        lineup = []
        for idx, item in enumerate(players):
            lineup.append({"index": idx, "itemId": item["id"], "loyaltyBonus": 1})
        squad_id = int(CFG.get("squad_id", 1))
        squad_name = str(CFG.get("squad_name", "Local Test XI"))
        squad = {
            "id": squad_id,
            "squadId": squad_id,
            "name": str(CFG.get("squad_name", "Local Test XI")),
            "squadName": str(CFG.get("squad_name", "Local Test XI")),
            "formation": "f442",
            "rating": 0,
            "chemistry": 0,
            "active": True,
            "players": lineup,
            "kicktakers": [],
        }
        with self.lock:
            self.conn.execute(
                "INSERT INTO squads(id,name,active,data) VALUES(?,?,?,?)",
                (squad_id, squad_name, 1, json.dumps(squad)),
            )
            self.conn.commit()

    def list_squads(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT data FROM squads ORDER BY active DESC,id").fetchall()
        squads: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                log.warning("Ignoring malformed saved squad row")
                continue
            if isinstance(data, dict):
                squads.append(data)
        return squads

    def get_squad(self, sid: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM squads WHERE id=?", (int(sid),)).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def next_squad_id(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COALESCE(MAX(id),0) + 1 FROM squads").fetchone()
        return max(1, _int_or(row[0] if row else 1, 1))

    def set_active_squad(self, sid: int) -> bool:
        sid = int(sid)
        with self.lock:
            exists = self.conn.execute("SELECT 1 FROM squads WHERE id=?", (sid,)).fetchone()
            if not exists:
                return False
            rows = self.conn.execute("SELECT id,data FROM squads").fetchall()
            for row in rows:
                try:
                    data = json.loads(row["data"])
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                active = int(row["id"]) == sid
                data["active"] = active
                self.conn.execute(
                    "UPDATE squads SET active=?,data=? WHERE id=?",
                    (1 if active else 0, json.dumps(data, separators=(",", ":")), int(row["id"])),
                )
            self.conn.commit()
        return True

    def _repair_misclassified_mixed_concept_squads(self) -> None:
        """Repair squads promoted to DREAM_SQUAD by the pre-1.0.8 bug.

        Older builds converted every owned card to a concept reference as soon
        as one concept was added.  A genuine concept squad is left untouched;
        the conservative repair applies only to a non-concept-named squad where
        almost every saved concept resource has an exact owned item.
        """
        marker = "mixed_concept_squad_repair_v108"
        if bool(self.get(marker, False)):
            return
        owned_by_resource: dict[int, list[int]] = {}
        for item in self.list_items():
            if str(item.get("itemType", "player")).lower() != "player":
                continue
            if str(item.get("pile", "club")).lower() not in ("club", "squad"):
                continue
            resource_id = _int_or(item.get("resourceId"), 0)
            item_id = _int_or(item.get("id"), 0)
            if resource_id > 0 and item_id > 0:
                owned_by_resource.setdefault(resource_id, []).append(item_id)

        repaired = 0
        with self.lock:
            rows = self.conn.execute("SELECT id,data FROM squads").fetchall()
            for row in rows:
                try:
                    squad = json.loads(row["data"])
                except Exception:
                    continue
                if not isinstance(squad, dict) or str(squad.get("squadType", "")).upper() != "DREAM_SQUAD":
                    continue
                name = str(squad.get("squadName") or squad.get("name") or "").casefold()
                if "concept" in name or "dream" in name:
                    continue
                players = squad.get("players", [])
                if not isinstance(players, list):
                    continue
                concept_slots = [
                    slot for slot in players if isinstance(slot, dict)
                    and _int_or(slot.get("conceptResourceId"), 0) > 0
                ]
                if len(concept_slots) < 3:
                    continue
                owned_matches = sum(
                    1 for slot in concept_slots
                    if owned_by_resource.get(_int_or(slot.get("conceptResourceId"), 0))
                )
                unmatched = len(concept_slots) - owned_matches
                if owned_matches < 2 or unmatched < 1 or unmatched > 2:
                    continue

                used_items: set[int] = set()
                rebuilt: list[dict[str, Any]] = []
                for slot in players:
                    if not isinstance(slot, dict):
                        continue
                    resource_id = _int_or(slot.get("conceptResourceId"), 0)
                    candidates = owned_by_resource.get(resource_id, [])
                    item_id = next((value for value in candidates if value not in used_items), 0)
                    if item_id > 0:
                        used_items.add(item_id)
                        restored = dict(slot)
                        restored["itemId"] = item_id
                        restored.pop("conceptResourceId", None)
                        restored.pop("dream", None)
                        restored.pop("concept", None)
                        restored["loyaltyBonus"] = 1
                        rebuilt.append(restored)
                    else:
                        rebuilt.append(dict(slot))
                squad["players"] = rebuilt
                squad["squadType"] = "REGULAR_SQUAD"
                squad["dreamSquad"] = False
                self.conn.execute(
                    "UPDATE squads SET data=? WHERE id=?",
                    (json.dumps(squad, separators=(",", ":")), int(row["id"])),
                )
                repaired += 1
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (marker, json.dumps(True)),
            )
            self.conn.commit()
        if repaired:
            log.warning("Repaired %d mixed regular/concept squad(s) from older saves", repaired)

    @staticmethod
    def _concept_resource_id(raw_slot: dict[str, Any], raw_item: dict[str, Any]) -> int:
        candidates = (
            raw_item.get("resourceId"), raw_item.get("definitionId"),
            raw_item.get("defId"), raw_slot.get("resourceId"),
            raw_slot.get("definitionId"), raw_slot.get("defId"),
            raw_item.get("id"), raw_slot.get("itemId"), raw_slot.get("id"),
        )
        for candidate in candidates:
            value = _int_or(candidate, 0)
            if value in PLAYER_DB:
                return value
            if value <= 0:
                continue
            variants = [
                int(resource_id) for resource_id, meta in PLAYER_DB.items()
                if _int_or(meta.get("assetId", resource_id), resource_id) == value
            ]
            if variants:
                return max(variants, key=lambda resource_id: _int_or(
                    PLAYER_DB.get(resource_id, {}).get("rating"), 0
                ))
        return 0

    def save_squad(self, payload: Any, *, create: bool = False) -> dict[str, Any]:
        raw = payload if isinstance(payload, dict) else {}
        if isinstance(raw.get("squad"), dict):
            raw = raw["squad"]
        src = dict(raw)
        incoming = dict(src)

        requested_sid = _int_or(src.get("id") or src.get("squadId"), 0)
        # SquadCreateDelegate posts the currently selected squad id as part of
        # its new-squad template. It is not an update key. Honouring that id
        # made every subsequent create overwrite the active squad (normally
        # id 1), which also left the Squad tab holding stale native references.
        sid = self.next_squad_id() if create else requested_sid
        if sid <= 0:
            sid = 1
        existing = self.get_squad(sid)
        if existing and not create:
            # Rename, activate, and other light native updates carry only the
            # changed fields.  Merge them so those calls never erase a lineup.
            src = {**existing, **src, "id": sid, "squadId": sid}
            # FIFA uses both aliases depending on which squad screen issued
            # the request.  Make the explicitly supplied alias authoritative
            # instead of letting the stale stored alias win after the merge.
            if "squadName" in incoming and "name" not in incoming:
                src["name"] = incoming["squadName"]
            elif "name" in incoming and "squadName" not in incoming:
                src["squadName"] = incoming["name"]
        name = str(src.get("name") or src.get("squadName") or f"Squad {sid}").strip() or f"Squad {sid}"
        formation = str(src.get("formation") or "f442").strip().lower()
        if not re.fullmatch(r"f[0-9a-z_]{2,16}", formation):
            formation = "f442"

        squad_type = str(src.get("squadType") or "REGULAR_SQUAD").upper()
        raw_players_for_type = src.get("players", [])
        has_concept_players = isinstance(raw_players_for_type, list) and any(
            isinstance(slot, dict) and (
                bool(slot.get("dream", False))
                or bool(slot.get("concept", False))
                or (
                    isinstance(slot.get("itemData"), dict)
                    and (
                        bool(slot["itemData"].get("dream", False))
                        or bool(slot["itemData"].get("concept", False))
                    )
                )
            )
            for slot in raw_players_for_type
        )
        has_regular_players = isinstance(raw_players_for_type, list) and any(
            isinstance(slot, dict)
            and not bool(slot.get("dream", False))
            and not bool(slot.get("concept", False))
            and not (
                isinstance(slot.get("itemData"), dict)
                and (
                    bool(slot["itemData"].get("dream", False))
                    or bool(slot["itemData"].get("concept", False))
                )
            )
            and _int_or(
                slot.get("itemId")
                or (slot.get("itemData", {}).get("id") if isinstance(slot.get("itemData"), dict) else 0)
                or slot.get("id"),
                0,
            ) > 0
            for slot in raw_players_for_type
        )
        # A concept card inside My Squads makes that lineup non-playable, but
        # it does not move the whole squad into the Concept Squads collection.
        # Infer DREAM_SQUAD only when every supplied card is a concept; an
        # explicit conversion or Create Concept request remains authoritative.
        if (squad_type == "DREAM_SQUAD"
                or (has_concept_players and not has_regular_players)):
            squad_type = "DREAM_SQUAD"
        if squad_type not in ("REGULAR_SQUAD", "DREAM_SQUAD", "DRAFT_SQUAD"):
            squad_type = "REGULAR_SQUAD"

        item_by_id = {
            _int_or(item.get("id"), 0): item
            for item in self.list_items()
            if isinstance(item, dict) and _int_or(item.get("id"), 0) > 0
        }
        slots: dict[int, dict[str, Any]] = {}
        seen_item_ids: set[int] = set()
        players = src.get("players", [])
        if isinstance(players, list):
            for order, raw_slot in enumerate(players[:64]):
                if not isinstance(raw_slot, dict):
                    continue
                index = _int_or(raw_slot.get("index"), order)
                if index < 0 or index > 22 or index in slots:
                    continue
                raw_item = raw_slot.get("itemData") if isinstance(raw_slot.get("itemData"), dict) else {}
                item_id = _int_or(raw_slot.get("itemId") or raw_item.get("id") or raw_slot.get("id"), 0)
                item = item_by_id.get(item_id)
                is_concept = bool(
                    raw_slot.get("dream", False) or raw_slot.get("concept", False)
                    or raw_item.get("dream", False) or raw_item.get("concept", False)
                    or squad_type == "DREAM_SQUAD"
                )
                if is_concept:
                    resource_id = _int_or(item.get("resourceId"), 0) if item else 0
                    if resource_id <= 0:
                        resource_id = self._concept_resource_id(raw_slot, raw_item)
                    if resource_id <= 0:
                        continue
                    default_kit = index + 1 if index < 11 else 0
                    slots[index] = {
                        "index": index,
                        "itemId": 0,
                        "conceptResourceId": resource_id,
                        "dream": True,
                        "kitNumber": _clamp_int(raw_slot.get("kitNumber"), 0, 99, default_kit),
                        "loyaltyBonus": _clamp_int(raw_slot.get("loyaltyBonus"), 0, 1, 0),
                    }
                    continue
                if item_id <= 0 or item_id in seen_item_ids or not item:
                    continue
                if str(item.get("itemType", "player") or "player").lower() != "player":
                    continue
                seen_item_ids.add(item_id)
                default_kit = index + 1 if index < 11 else 0
                slots[index] = {
                    "index": index,
                    "itemId": item_id,
                    "kitNumber": _clamp_int(raw_slot.get("kitNumber"), 0, 99, default_kit),
                    "loyaltyBonus": _clamp_int(raw_slot.get("loyaltyBonus"), 0, 1, 1),
                }

        canonical_players = [slots[index] for index in sorted(slots)]
        starter_ids = [
            slot["itemId"] for slot in canonical_players
            if slot["index"] < 11 and _int_or(slot.get("itemId"), 0) > 0
        ]
        captain = _int_or(src.get("captain"), 0)
        if captain not in starter_ids:
            captain = starter_ids[0] if starter_ids else 0

        manager: list[dict[str, Any]] = []
        raw_manager = src.get("manager", [])
        if isinstance(raw_manager, dict):
            raw_manager = [raw_manager]
        if isinstance(raw_manager, list):
            for entry in raw_manager[:1]:
                if not isinstance(entry, dict):
                    continue
                entry_data = entry.get("itemData") if isinstance(entry.get("itemData"), dict) else {}
                manager_id = _int_or(entry.get("id") or entry_data.get("id"), 0)
                owned_manager = item_by_id.get(manager_id)
                if (manager_id > 0 and isinstance(owned_manager, dict)
                        and str(owned_manager.get("itemType", "")).lower() == "manager"):
                    manager.append({"id": manager_id, "dream": bool(entry.get("dream", False))})
        if not manager and squad_type != "DREAM_SQUAD":
            owned_managers = [
                item for item in item_by_id.values()
                if str(item.get("itemType", "")).lower() == "manager"
                and str(item.get("pile", "club")).lower() in ("club", "squad")
            ]
            if not owned_managers:
                created_manager = self._ensure_initial_club_manager()
                if isinstance(created_manager, dict):
                    item_by_id[_int_or(created_manager.get("id"), 0)] = created_manager
                    owned_managers = [created_manager]
            if owned_managers:
                owned_managers.sort(key=lambda item: (
                    -_int_or(item.get("rating"), 0), _int_or(item.get("id"), 0)
                ))
                manager.append({"id": _int_or(owned_managers[0].get("id"), 0),
                                "dream": False})

        rating = _clamp_int(src.get("rating", src.get("starRating", 0)), 0, 99, 0)
        star_rating = _clamp_int(src.get("starRating", rating), 0, 99, rating)
        chemistry = _clamp_int(src.get("chemistry", 0), 0, 100, 0)
        # A directly created squad becomes active unless a clone/conversion
        # explicitly asks to remain inactive.
        active = 1 if bool(src.get("active", create or sid == 1)) else 0

        canonical = {
            "id": sid,
            "squadId": sid,
            "name": name,
            "squadName": name,
            "formation": formation,
            "rating": rating,
            "starRating": star_rating,
            "chemistry": chemistry,
            "active": bool(active),
            "squadType": squad_type,
            "captain": captain,
            "players": canonical_players,
            "manager": manager,
            "kicktakers": src.get("kicktakers", []) if isinstance(src.get("kicktakers", []), list) else [],
            "custom": str(src.get("custom") or "[0,0,0,0,0,0,0,0,0,0,0]"),
        }

        with self.lock:
            if active:
                self.conn.execute("UPDATE squads SET active=0")
            self.conn.execute(
                "INSERT INTO squads(id,name,active,data) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=excluded.active,data=excluded.data",
                (sid, name, active, json.dumps(canonical, separators=(",", ":"))),
            )
            self.conn.commit()
        if active:
            self.set_active_squad(sid)
        saved_manager_id = _int_or(manager[0].get("id"), 0) if manager else 0
        saved_manager = item_by_id.get(saved_manager_id, {})
        saved_manager_native = _native_manager_item(saved_manager) if saved_manager else {}
        log.info(
            "SQUAD SAVE id=%d slots=%d starters=%d formation=%s chemistry=%d rating=%d "
            "managerItem=%d managerResource=%d managerAsset=%d managerContracts=%d",
            sid, len(canonical_players), len(starter_ids), formation, chemistry, rating,
            saved_manager_id,
            _int_or(saved_manager_native.get("resourceId"), 0),
            _int_or(saved_manager_native.get("assetId"), 0),
            _int_or(saved_manager_native.get("contracts"), 0),
        )
        return canonical

    def delete_squad(self, sid: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM squads WHERE id=?", (int(sid),))
            if cur.rowcount > 0:
                active = self.conn.execute(
                    "SELECT id FROM squads WHERE active=1 ORDER BY id LIMIT 1"
                ).fetchone()
                if not active:
                    fallback = self.conn.execute("SELECT id FROM squads ORDER BY id LIMIT 1").fetchone()
                    if fallback:
                        fallback_id = int(fallback[0])
                        row = self.conn.execute(
                            "SELECT data FROM squads WHERE id=?", (fallback_id,)
                        ).fetchone()
                        try:
                            data = json.loads(row[0]) if row else {}
                        except Exception:
                            data = {}
                        if not isinstance(data, dict):
                            data = {}
                        data["active"] = True
                        self.conn.execute(
                            "UPDATE squads SET active=1,data=? WHERE id=?",
                            (json.dumps(data, separators=(",", ":")), fallback_id),
                        )
            self.conn.commit()
            return cur.rowcount > 0

    def create_pack(self, pack_id: int = 1) -> dict[str, Any]:
        # Never discard existing Unassigned items. Market purchases deliberately
        # remain there when the player chooses Continue Searching, and FIFA will
        # route them through the normal new-items screen later.

        pcfg=get_pack_cfg(pack_id)
        if not pcfg:
            raise RuntimeError(f"Unknown pack id {pack_id}")

        pack_size=max(1,min(30,int(pcfg.get("size",12))))
        content_type = str(pcfg.get("content_type", "players") or "players").lower()
        if content_type in ("position_modifiers", "consumables", "club_items"):
            if content_type != "club_items":
                if content_type == "position_modifiers":
                    full_pool = list(POSITION_MODIFIER_EFFECTS)
                    selected_resources = []
                else:
                    groups = _consumable_pack_groups()
                    selected_resources = []
                    random.shuffle(groups)
                    for group in groups:
                        if len(selected_resources) >= pack_size:
                            break
                        selected_resources.append(random.choice(group))
                    # Position modifiers have their own Gold pack. Keep the
                    # mixed consumables pack focused on every other development
                    # family, including chemistry styles.
                    full_pool = [
                        resource_id for resource_id, meta in CONSUMABLE_DB.items()
                        if str(meta.get("sourceMember", "")) != "consumablesPosition"
                    ]
                while len(selected_resources) < pack_size and full_pool:
                    candidate = random.choice(full_pool)
                    if candidate not in selected_resources or len(full_pool) < pack_size:
                        selected_resources.append(candidate)
                random.shuffle(selected_resources)
                items = []
                for resource_id in selected_resources[:pack_size]:
                    # Development cards are stacks in retail FUT, not normal
                    # player inventory rows. Persist them in the club at pack
                    # creation so a client which skips/loses Store All can still
                    # immediately use position modifiers and chemistry styles.
                    stored = self.make_consumable_item(resource_id, "club", 1)
                    revealed = dict(stored)
                    revealed["pile"] = "unassigned"
                    items.append(revealed)
            else:
                owned_items = [
                    item for item in (*self.list_items("club"), *self.list_items("squad"), *self.list_items("unassigned"))
                    if isinstance(item, dict)
                ]
                owned_resources = {_int_or(item.get("resourceId"), 0) for item in owned_items}
                owned_signatures = {
                    (
                        str(item.get("itemType", "")).lower(),
                        _int_or(item.get("category"), 0),
                        _int_or(item.get("teamid"), 0),
                        _int_or(item.get("year"), 0),
                        _int_or(item.get("stadiumid"), -1),
                        _int_or(item.get("ballid"), -1),
                    )
                    for item in owned_items
                }

                def choose_resource(pool: list[int]) -> int:
                    def is_owned(rid: int) -> bool:
                        meta = CLUB_ITEM_DB.get(rid, {})
                        signature = (
                            str(meta.get("itemType", "")).lower(),
                            _int_or(meta.get("category"), 0),
                            _int_or(meta.get("teamid"), 0),
                            _int_or(meta.get("year"), 0),
                            _int_or(meta.get("stadiumid"), -1),
                            _int_or(meta.get("ballid"), -1),
                        )
                        return rid in owned_resources or signature in owned_signatures

                    available = [rid for rid in pool if not is_owned(rid)]
                    source = available or pool
                    return random.choice(source)

                home_pool = list(dict.fromkeys(_NATIVE_HOME_KITS)) or [6_300_074]
                away_pool = list(dict.fromkeys(_NATIVE_AWAY_KITS)) or [6_400_057]
                badge_pool = list(dict.fromkeys(_NATIVE_BADGES)) or [6_000_057]
                stadium_pool = list(dict.fromkeys(_NATIVE_STADIUMS)) or [6_200_058]
                ball_pool = list(dict.fromkeys(_NATIVE_BALLS)) or [8_120_212]
                # Keep a fixed native-friendly reveal order. A tester crash was
                # captured after a random ball-first response; FIFA 20's mixed
                # item reveal is most reliable when the badge establishes the
                # customization renderer before stadium, kits and ball.
                selected_resources = [
                    choose_resource(badge_pool), choose_resource(stadium_pool),
                    choose_resource(home_pool), choose_resource(away_pool),
                    choose_resource(ball_pool),
                ]
                if pack_size < len(selected_resources):
                    selected_resources = selected_resources[:pack_size]
                while len(selected_resources) < pack_size:
                    selected_resources.append(choose_resource(home_pool + away_pool + badge_pool))
                items = [
                    self.make_club_item(resource_id, "unassigned", "free")
                    for resource_id in selected_resources
                ]
            return {
                "packId": int(pack_id), "purchasedPackId": int(pack_id),
                "packType": "CUSTOM_PACK", "itemList": items,
                "itemData": items, "items": items, "newcards": len(items),
                "numberItems": len(items), "duplicateItemIdList": [],
                "credits": self.credits(),
            }
        rare_slots=max(0,min(pack_size,int(pcfg.get("rare_slots",3))))
        min_rating=max(0,min(99,int(pcfg.get("min_rating",0) or 0)))
        max_rating=max(min_rating,min(99,int(pcfg.get("max_rating",99) or 99)))
        required_position=str(pcfg.get("position","") or "").strip().upper()
        guaranteed_specials=max(0,min(rare_slots,int(pcfg.get("guaranteed_specials",0) or 0)))
        special_chance=max(0.0,min(1.0,float(pcfg.get("special_chance",0.10))))
        tier_weights={
            "gold":max(0.0,float(pcfg.get("gold_weight",100) or 0)),
            "silver":max(0.0,float(pcfg.get("silver_weight",0) or 0)),
            "bronze":max(0.0,float(pcfg.get("bronze_weight",0) or 0)),
        }
        if sum(tier_weights.values())<=0:
            tier_weights={"gold":100.0,"silver":0.0,"bronze":0.0}

        special_weights={}
        for k,v in pcfg.get("_special_weights",{}).items():
            try:
                rf=int(k)
                if rf in (12, 15):
                    continue
                special_weights[rf]=max(0.0,float(v))
            except Exception: pass

        def tier_for(meta):
            t=str(meta.get("tier","") or "").lower()
            if t in ("gold","silver","bronze"): return t
            r=int(meta.get("rating",0) or 0)
            return "gold" if r>=75 else "silver" if r>=65 else "bronze"

        recent_special_assets = [
            _int_or(value, 0) for value in self.get("pack_recent_special_assets", [])
            if _int_or(value, 0) > 0
        ][:40]
        recent_special_set = set(recent_special_assets)
        pools={t:{"common":[],"rare":[],"special":[]} for t in ("gold","silver","bronze")}
        for rid0 in PLAYER_DB.keys():
            rid=int(rid0); meta=player_meta(rid); t=tier_for(meta)
            if _player_is_excluded_from_packs(rid, meta):
                continue
            if not _player_is_pack_safe(meta):
                continue
            rating=int(meta.get("rating",0) or 0)
            if rating < min_rating or rating > max_rating:
                continue
            if required_position and str(meta.get("preferredPosition","") or "").upper() != required_position:
                continue
            rf=int(meta.get("rareflag",0) or 0)
            issp=bool(meta.get("special",False)) or rf>1
            pools[t]["special" if issp else "rare" if rf==1 else "common"].append(rid)
        for t in pools:
            for k in pools[t]: random.shuffle(pools[t][k])

        selected=[]; used=set(); used_assets=set()

        def weighted_choice(opts):
            arr=[(v,float(w)) for v,w in opts if float(w)>0]
            if not arr: return None
            total=sum(w for _,w in arr); r=random.random()*total; acc=0
            for v,w in arr:
                acc+=w
                if r<=acc: return v
            return arr[-1][0]

        def choose_tier():
            return weighted_choice(list(tier_weights.items())) or "gold"

        def pop_unique(pool):
            while pool:
                rid=pool.pop()
                meta=player_meta(rid)
                asset_id=_native_player_asset_id(meta,rid)
                if rid not in used and asset_id not in used_assets:
                    used.add(rid); used_assets.add(asset_id); return rid
            return None

        def choose_special(tier):
            # special_weights are rarity/type weights, not per-card weights.
            # Choose the rareflag first, then a card within that rareflag.
            # Otherwise a type with hundreds of cards (e.g. IF/TOTW) overwhelms
            # a type with only a handful of cards even when both sliders are equal.
            def candidates_by_type(allow_recent: bool) -> dict[int, list[int]]:
                grouped: dict[int, list[int]] = {}
                for rid in pools[tier]["special"]:
                    meta=player_meta(rid)
                    asset_id=_native_player_asset_id(meta,rid)
                    if rid in used or asset_id in used_assets:
                        continue
                    if not allow_recent and asset_id in recent_special_set:
                        continue
                    rf=int(meta.get("rareflag",0) or 0)
                    if float(special_weights.get(rf,0))<=0:
                        continue
                    grouped.setdefault(rf,[]).append(rid)
                return grouped

            # Prefer footballers not headlining any of the last few packs.
            # This is especially important for one-card families such as the
            # Record Breaker bucket, which otherwise repeats the same Aguero.
            by_type=candidates_by_type(False)
            if not by_type:
                by_type=candidates_by_type(True)
            if not by_type:
                return None
            rf=weighted_choice([(rf,float(special_weights.get(rf,0))) for rf in by_type])
            if rf is None:
                return None
            candidates=by_type.get(int(rf),[])
            if not candidates:
                return None
            rid=random.choice(candidates)
            used.add(rid)
            used_assets.add(_native_player_asset_id(player_meta(rid),rid))
            try: pools[tier]["special"].remove(rid)
            except ValueError: pass
            return rid

        # guaranteed rare slots
        for slot_index in range(rare_slots):
            tier=choose_tier()
            must_be_special=slot_index < guaranteed_specials
            rid=choose_special(tier) if must_be_special or random.random()<special_chance else None
            if rid is None and must_be_special:
                for alt in (t for t,w in tier_weights.items() if w>0 and t!=tier):
                    rid=choose_special(alt)
                    if rid is not None: break
            if rid is None and must_be_special:
                raise RuntimeError("Configured guaranteed special pool is empty")
            if rid is None: rid=pop_unique(pools[tier]["rare"])
            if rid is None: rid=choose_special(tier)
            if rid is None:
                # only fall back to another ENABLED tier, not hidden tiers
                enabled=[t for t,w in tier_weights.items() if w>0 and t!=tier]
                for alt in enabled:
                    rid=pop_unique(pools[alt]["rare"]) or choose_special(alt)
                    if rid is not None: break
            if rid is None:
                raise RuntimeError("Configured rare/special pool is empty")
            selected.append(rid)

        # common slots
        for _ in range(pack_size-rare_slots):
            tier=choose_tier()
            rid=pop_unique(pools[tier]["common"])
            if rid is None:
                enabled=[t for t,w in tier_weights.items() if w>0 and t!=tier]
                for alt in enabled:
                    rid=pop_unique(pools[alt]["common"])
                    if rid is not None: break
            if rid is None:
                raise RuntimeError("Configured common pool is empty")
            selected.append(rid)

        # Pack reveal order: SPECIAL cards first, regardless of OVR; then highest OVR first.
        # Within each group, rareflag/resourceId are deterministic tie-breakers.
        selected=sorted(selected,key=lambda rid:(
            0 if (bool(player_meta(rid).get("special",False)) or int(player_meta(rid).get("rareflag",0) or 0)>1) else 1,
            -int(player_meta(rid).get("rating",0) or 0),
             -int(player_meta(rid).get("rareflag",0) or 0),
             -int(rid)))
        pulled_special_assets = [
            _native_player_asset_id(player_meta(rid), rid) for rid in selected
            if bool(player_meta(rid).get("special", False))
            or _int_or(player_meta(rid).get("rareflag"), 0) > 1
        ]
        if pulled_special_assets:
            rotated = list(dict.fromkeys([
                *pulled_special_assets,
                *(asset for asset in recent_special_assets if asset not in pulled_special_assets),
            ]))[:40]
            self.set("pack_recent_special_assets", rotated)
        owned_before=self.owned_player_resource_ids()

        # IMPORTANT: FIFA 20 PC does not reliably preserve the JSON array order on
        # the New Items screen. It can rebuild the unassigned collection using the
        # item-instance metadata. Encode the desired reveal order into the actual
        # item IDs/timestamps as well as the response arrays:
        #   - create worst -> best so the best/special card receives the HIGHEST id
        #   - newest timestamp = best/special card
        # This makes descending ID/timestamp client-side ordering resolve to exactly
        # the same order we want to display.
        created_reversed=[self.make_player_item(rid,"unassigned") for rid in reversed(selected)]
        items=list(reversed(created_reversed))
        base_ts=now_s()
        for rank,item in enumerate(items):
            item["timestamp"]=base_ts-rank
            with self.lock:
                self.conn.execute("UPDATE items SET data=? WHERE id=?", (json.dumps(item), int(item.get("id",0))))
        with self.lock:
            self.conn.commit()

        dup_ids=[int(x["id"]) for x in items if int(x.get("resourceId",x.get("assetId",0)) or 0) in owned_before]
        dup_list=[]
        for x in items:
            iid=int(x.get("id",0) or 0); rid=int(x.get("resourceId",x.get("assetId",0)) or 0)
            if iid not in dup_ids: continue
            own=self.owned_player_item_by_resource_id(rid)
            ownid=int(own.get("id",0) or 0) if isinstance(own,dict) else 0
            dup_list.append({"itemId":iid,"duplicateItemId":ownid} if ownid else {"itemId":iid})
        for iid in dup_ids: self.mark_item_duplicate(iid,True)
        if dup_ids:
            refreshed={int(x.get("id",0)):x for x in self.list_items("unassigned")}
            items=[refreshed.get(int(x.get("id",0)),x) for x in items]

        items=sorted(items,key=lambda x:(
            0 if (bool(x.get("special",False)) or int(x.get("rareflag",0) or 0)>1) else 1,
            -int(x.get("rating",0) or 0),
            -int(x.get("rareflag",0) or 0),
            -int(x.get("resourceId",x.get("assetId",0)) or 0)))
        log.warning("CUSTOM PACK id=%s name=%s size=%d rareSlots=%d minRating=%d guaranteedSpecials=%d specialChance=%.1f%%",
                    pack_id,pcfg.get("name"),pack_size,rare_slots,min_rating,
                    guaranteed_specials,special_chance*100)
        if items:
            log.warning("PACK HEADLINE CANDIDATE id=%s resource=%s rating=%s rareflag=%s discardValue=%s",
                        items[0].get("id"),items[0].get("resourceId"),items[0].get("rating"),
                        items[0].get("rareflag"),items[0].get("discardValue"))
        return {"packId":int(pack_id),"purchasedPackId":int(pack_id),"packType":"CUSTOM_PACK",
                "itemList":items,"itemData":items,"items":items,
                "newcards":len(items)-len(dup_ids),"numberItems":len(items),
                "duplicateItemIdList":dup_list,"credits":self.credits()}

    def unopened_pack_counts(self) -> dict[int, int]:
        with self.lock:
            # Repair rewards granted by 0.9.18 and earlier in place. Normal
            # store purchases open immediately and never enter this table, so
            # legacy rows here are unambiguously earned rewards.
            for legacy_id, reward_id in _LEGACY_SBC_REWARD_PACK_IDS.items():
                self.conn.execute(
                    "UPDATE unopened_packs SET pack_id=? WHERE pack_id=?",
                    (int(reward_id), int(legacy_id)),
                )
            self.conn.commit()
            rows = self.conn.execute(
                "SELECT pack_id,COUNT(*) AS quantity FROM unopened_packs GROUP BY pack_id ORDER BY pack_id"
            ).fetchall()
        return {int(row["pack_id"]): int(row["quantity"]) for row in rows}

    def consume_unopened_pack(self, pack_id: int) -> bool:
        """Consume one earned pack token before opening its contents."""
        with self.lock:
            row = self.conn.execute(
                "SELECT id FROM unopened_packs WHERE pack_id=? ORDER BY id LIMIT 1",
                (int(pack_id),),
            ).fetchone()
            if not row:
                return False
            self.conn.execute("DELETE FROM unopened_packs WHERE id=?", (int(row["id"]),))
            self.conn.commit()
            return True

    def grant_unopened_pack(self, pack_id: int, data: str = "{}") -> None:
        """Grant one unopened pack token (also used to roll back failed opens)."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                (int(pack_id), str(data or "{}")),
            )
            self.conn.commit()

    def pending_player_picks(self) -> list[dict[str, Any]]:
        raw = self.get("pending_player_picks", [])
        return [dict(entry) for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []

    def claim_player_pick(self, pick_id: int, choice_index: int = 0,
                          resource_id: int = 0) -> dict[str, Any] | None:
        # Keep the read, grant and removal under the same lock. Two native
        # retries arriving together must never redeem the same choice twice.
        with self.lock:
            picks = self.pending_player_picks()
            selected_pick = next(
                (entry for entry in picks if _int_or(entry.get("id"), 0) == int(pick_id)), None
            )
            if not selected_pick:
                return None
            resources = [
                _int_or(value, 0) for value in selected_pick.get("resources", [])
                if _int_or(value, 0) in PLAYER_DB
            ]
            if not resources:
                return None
            if resource_id not in resources:
                resource_id = resources[max(0, min(int(choice_index), len(resources) - 1))]
            owned_before = self.owned_player_item_by_resource_id(resource_id)
            item = self.make_player_item(resource_id, "unassigned")
            item_id = _int_or(item.get("id"), 0)
            item = self.update_item_fields(item_id, {"untradeable": True}) or item
            if owned_before:
                self.mark_item_duplicate(item_id, True)
                item = self.get_item(item_id) or item
            self.set(
                "pending_player_picks",
                [entry for entry in picks if _int_or(entry.get("id"), 0) != int(pick_id)],
            )
            return item

    def sbc_completion_counts(self) -> tuple[dict[int, int], dict[int, int]]:
        with self.lock:
            challenges = self.conn.execute(
                "SELECT challenge_id,times_completed FROM sbc_completions"
            ).fetchall()
            sets = self.conn.execute(
                "SELECT set_id,times_completed FROM sbc_set_completions"
            ).fetchall()
        return (
            {int(row["challenge_id"]): int(row["times_completed"]) for row in challenges},
            {int(row["set_id"]): int(row["times_completed"]) for row in sets},
        )

    def sbc_squad(self, challenge_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT data FROM sbc_squads WHERE challenge_id=?", (int(challenge_id),)
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data"])
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def save_sbc_squad(self, challenge_id: int, payload: Any, squad_size: int) -> dict[str, Any]:
        """Save owned player-instance IDs in an SBC's eleven on-pitch slots."""
        raw = payload if isinstance(payload, dict) else {}
        raw_players = raw.get("players", [])
        if isinstance(raw.get("squad"), dict):
            raw = raw["squad"]
            raw_players = raw.get("players", [])
        if not isinstance(raw_players, list):
            raw_players = []

        owned = {
            _int_or(item.get("id"), 0): item
            for item in self.list_items()
            if isinstance(item, dict)
            and _int_or(item.get("id"), 0) > 0
            and str(item.get("itemType", "player")).lower() == "player"
            and str(item.get("pile", "club")).lower() in ("club", "squad")
        }
        slots: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        for order, entry in enumerate(raw_players[:64]):
            if not isinstance(entry, dict):
                continue
            index = _int_or(entry.get("index"), order)
            embedded = entry.get("itemData") if isinstance(entry.get("itemData"), dict) else {}
            item_id = _int_or(entry.get("itemId") or embedded.get("id") or entry.get("id"), 0)
            if index < 0 or index >= int(squad_size) or item_id <= 0 or item_id in seen:
                continue
            if item_id not in owned:
                continue
            seen.add(item_id)
            slots[index] = {"index": index, "itemData": {"id": item_id, "dream": False}}

        chemistry_value = _int_or(raw.get("chemistry"), -1)
        if chemistry_value >= 0:
            chemistry_value = max(0, min(100, chemistry_value))
        canonical = {
            "challengeId": int(challenge_id),
            "formation": str(raw.get("formation") or "f442"),
            "chemistry": chemistry_value,
            "players": [slots[index] for index in sorted(slots)],
        }
        with self.lock:
            self.conn.execute(
                "INSERT INTO sbc_squads(challenge_id,data,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(challenge_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                (int(challenge_id), json.dumps(canonical, separators=(",", ":")), now_s()),
            )
            self.conn.commit()
        return canonical

    def complete_sbc(
        self,
        challenge_id: int,
        set_id: int,
        item_ids: list[int],
        challenge_awards: list[dict[str, Any]],
        set_challenge_ids: list[int],
        set_awards: list[dict[str, Any]],
        repeatable: bool,
    ) -> dict[str, Any]:
        """Consume a valid SBC squad and grant all rewards in one transaction."""
        clean_ids = [int(value) for value in item_ids if int(value) > 0]
        if len(clean_ids) != len(set(clean_ids)):
            raise ValueError("the submitted squad contains duplicate item instances")

        def grant_awards(cursor: sqlite3.Cursor, awards: list[dict[str, Any]], source: str) -> int:
            coin_total = 0
            for award in awards:
                award_type = str(award.get("type", "")).upper()
                count = max(1, _int_or(award.get("count"), 1))
                if award_type == "COINS":
                    coin_total += max(0, _int_or(award.get("value"), 0)) * count
                elif award_type == "PACK":
                    pack_id = _sbc_reward_pack_id(
                        award.get("pack_id") or award.get("packId") or award.get("value")
                    )
                    if pack_id <= 0 or not get_pack_cfg(pack_id):
                        raise ValueError(f"unknown SBC reward pack {pack_id}")
                    data = json.dumps({"source": source, "awardedAt": now_s()}, separators=(",", ":"))
                    for _ in range(count):
                        cursor.execute(
                            "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)", (pack_id, data)
                        )
                elif award_type == "PLAYER_PICK":
                    for _ in range(count):
                        option_count = max(2, min(5, _int_or(award.get("options"), 3)))
                        resources = _generate_player_pick_resources(
                            str(award.get("pool", "promo")),
                            option_count,
                            _int_or(award.get("min_rating"), 0),
                        )
                        if not resources:
                            raise ValueError("configured SBC player-pick pool is empty")
                        sequence_row = cursor.execute(
                            "SELECT value FROM kv WHERE key='next_item_id'"
                        ).fetchone()
                        try:
                            token_id = int(json.loads(sequence_row[0])) if sequence_row else 900000000000
                        except Exception:
                            token_id = 900000000000
                        token_id += 1
                        cursor.execute(
                            "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(token_id),),
                        )
                        title = str(
                            award.get("title")
                            or f"1 of {option_count} FUT Champions Players"
                        )
                        token = PICK_STATE.token_item(token_id, title)
                        cursor.execute(
                            "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                            (
                                token_id, PICK_STATE.REAL_PICK_RESOURCE_ID, "unassigned",
                                json.dumps(token, separators=(",", ":")),
                            ),
                        )
                        PICK_STATE.register_cursor(
                            cursor, token, str(award.get("pool", "promo")),
                            title, option_count, resources,
                        )
                elif award_type == "PLAYER":
                    resource_id = canonical_player_resource_id(
                        _int_or(award.get("resource_id") or award.get("resourceId")
                                or award.get("value"), 0)
                    )
                    if resource_id <= 0 or not player_meta(resource_id):
                        raise ValueError(f"unknown SBC player reward {resource_id}")
                    loan_games = max(
                        0, _int_or(award.get("loan_games") or award.get("loan"), 0)
                    )
                    for _ in range(count):
                        sequence_row = cursor.execute(
                            "SELECT value FROM kv WHERE key='next_item_id'"
                        ).fetchone()
                        try:
                            item_id = int(json.loads(sequence_row[0])) if sequence_row else 900000000000
                        except Exception:
                            item_id = 900000000000
                        item_id += 1
                        cursor.execute(
                            "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(item_id),),
                        )
                        item = self.build_player_item(
                            resource_id, item_id, "unassigned", loan_games=loan_games
                        )
                        # Every SBC player is untradeable, including permanent
                        # cards.  Loans additionally carry their remaining games.
                        item["untradeable"] = True
                        item["discardValue"] = 0
                        cursor.execute(
                            "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                            (item_id, resource_id, "unassigned", json.dumps(item)),
                        )
            return coin_total

        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                current_row = cursor.execute(
                    "SELECT times_completed FROM sbc_completions WHERE challenge_id=?",
                    (int(challenge_id),),
                ).fetchone()
                current_count = int(current_row[0]) if current_row else 0
                if current_count and not repeatable:
                    raise ValueError("this challenge has already been completed")

                placeholders = ",".join("?" for _ in clean_ids)
                rows = cursor.execute(
                    f"SELECT id,pile,data FROM items WHERE id IN ({placeholders})", clean_ids
                ).fetchall() if clean_ids else []
                if len(rows) != len(clean_ids):
                    raise ValueError("one or more submitted players are no longer owned")
                for row in rows:
                    data = json.loads(row["data"])
                    if str(data.get("itemType", "player")).lower() != "player":
                        raise ValueError("SBC squads can contain player items only")
                    if str(row["pile"]).lower() not in ("club", "squad"):
                        raise ValueError("submitted players must be in the Club")

                cursor.execute(f"DELETE FROM items WHERE id IN ({placeholders})", clean_ids)

                # Remove submitted cards from any saved regular squad as well.
                removed = set(clean_ids)
                for squad_row in cursor.execute("SELECT id,data FROM squads").fetchall():
                    try:
                        squad = json.loads(squad_row["data"])
                    except Exception:
                        continue
                    players = squad.get("players", []) if isinstance(squad, dict) else []
                    kept = []
                    for slot in players if isinstance(players, list) else []:
                        embedded = slot.get("itemData") if isinstance(slot, dict) and isinstance(slot.get("itemData"), dict) else {}
                        item_id = _int_or(slot.get("itemId") or embedded.get("id") or slot.get("id"), 0) if isinstance(slot, dict) else 0
                        if item_id not in removed:
                            kept.append(slot)
                    if isinstance(squad, dict) and len(kept) != len(players):
                        squad["players"] = kept
                        cursor.execute("UPDATE squads SET data=? WHERE id=?", (json.dumps(squad), int(squad_row["id"])))

                completed_at = now_s()
                next_count = current_count + 1
                cursor.execute(
                    "INSERT INTO sbc_completions(challenge_id,times_completed,completed_at) VALUES(?,?,?) "
                    "ON CONFLICT(challenge_id) DO UPDATE SET times_completed=excluded.times_completed,completed_at=excluded.completed_at",
                    (int(challenge_id), next_count, completed_at),
                )
                cursor.execute("DELETE FROM sbc_squads WHERE challenge_id=?", (int(challenge_id),))

                granted = list(challenge_awards)
                coin_total = grant_awards(cursor, challenge_awards, f"SBC challenge {challenge_id}")

                set_completed = False
                if set_challenge_ids:
                    set_counts = []
                    for cid in set_challenge_ids:
                        row = cursor.execute(
                            "SELECT times_completed FROM sbc_completions WHERE challenge_id=?", (int(cid),)
                        ).fetchone()
                        set_counts.append(int(row[0]) if row else 0)
                    completed_cycles = min(set_counts) if set_counts else 0
                    prior_set_row = cursor.execute(
                        "SELECT times_completed FROM sbc_set_completions WHERE set_id=?", (int(set_id),)
                    ).fetchone()
                    prior_cycles = int(prior_set_row[0]) if prior_set_row else 0
                    if completed_cycles > prior_cycles:
                        set_completed = True
                        cursor.execute(
                            "INSERT INTO sbc_set_completions(set_id,times_completed,completed_at) VALUES(?,?,?) "
                            "ON CONFLICT(set_id) DO UPDATE SET times_completed=excluded.times_completed,completed_at=excluded.completed_at",
                            (int(set_id), completed_cycles, completed_at),
                        )
                        coin_total += grant_awards(cursor, set_awards, f"SBC set {set_id}")
                        granted.extend(set_awards)

                if coin_total:
                    row = cursor.execute("SELECT value FROM kv WHERE key='credits'").fetchone()
                    try:
                        current_credits = int(json.loads(row[0])) if row else 0
                    except Exception:
                        current_credits = 0
                    new_credits = max(0, current_credits + coin_total)
                    cursor.execute(
                        "INSERT INTO kv(key,value) VALUES('credits',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(new_credits),),
                    )

                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        return {
            "challengeId": int(challenge_id),
            "setId": int(set_id),
            "timesCompleted": next_count,
            "setCompleted": set_completed,
            "awards": granted,
            "consumedItemIds": clean_ids,
            "credits": self.credits(),
            "unopenedPacks": sum(self.unopened_pack_counts().values()),
        }


def _generate_player_pick_resources(pool: str, options: int, min_rating: int = 0) -> list[int]:
    pool_name = str(pool or "promo").lower()
    rarity_pools = {
        "icon_moments": {84},
        "fut_birthday": {30},
        "shapeshifters": {97},
        "tots": {65, 66},
        "promo": set(int(key) for key in PROMO_SPECIAL_WEIGHTS),
    }
    accepted = rarity_pools.get(pool_name, rarity_pools["promo"])
    candidates = [
        int(resource_id) for resource_id, meta in PLAYER_DB.items()
        if not _player_is_excluded_from_packs(int(resource_id), meta)
        and _player_is_pack_safe(meta)
        and _int_or(meta.get("rareflag"), 0) in accepted
        and _int_or(meta.get("rating"), 0) >= max(0, int(min_rating))
    ]
    # One footballer may have many special versions; a pick must never display
    # two versions of the same player.
    by_asset: dict[int, list[int]] = {}
    for resource_id in candidates:
        meta = player_meta(resource_id)
        by_asset.setdefault(_native_player_asset_id(meta, resource_id), []).append(resource_id)
    assets = list(by_asset)
    random.shuffle(assets)
    selected: list[int] = []
    for asset_id in assets[:max(1, int(options))]:
        variants = by_asset[asset_id]
        selected.append(max(
            variants,
            key=lambda resource_id: (
                _int_or(player_meta(resource_id).get("rating"), 0), int(resource_id)
            ),
        ))
    return selected


def _purchase_store_player_pick(pack_id: int, cfg: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Buy one tester Store Player Pick and persist its native misc token.

    The Store transaction itself stays pack-shaped because FIFA 20's Store
    provider only knows card-pack purchase articles. The returned item is not a
    fake pack, though: it is the real miscPlayerPick token that the client later
    redeems through /item/nontargeted.
    """
    option_count = max(2, min(5, _int_or(cfg.get("player_pick_options"), 3)))
    pool_key = str(cfg.get("player_pick_pool", "promo") or "promo")
    min_rating = max(0, _int_or(cfg.get("player_pick_min_rating"), 0))
    title = str(cfg.get("name") or f"1 of {option_count} Player Pick")
    price = max(0, _int_or(cfg.get("price"), 0))

    resources = _generate_player_pick_resources(pool_key, option_count, min_rating)
    if len(resources) < option_count:
        log.warning(
            "PLAYER PICK STORE blocked packId=%d pool=%s options=%d generated=%d",
            int(pack_id), pool_key, option_count, len(resources),
        )
        return 409, {"success": False, "reason": "PLAYER_PICK_POOL_EMPTY", **_credits_payload()}

    token_id = STATE.next_item_id()
    token = PICK_STATE.token_item(token_id, title)
    token.update({
        "source": "store",
        "sourcePackId": int(pack_id),
        "playerPickPool": pool_key,
        "playerPickOptions": option_count,
        "playerPickMinRating": min_rating,
    })

    with STATE.lock:
        try:
            row = STATE.conn.execute("SELECT value FROM kv WHERE key='credits'").fetchone()
            try:
                credits = int(json.loads(row[0])) if row else 0
            except Exception:
                credits = 0
            if credits < price:
                return 402, {"success": False, "reason": "INSUFFICIENT_CREDITS", "credits": credits}

            STATE.conn.execute("BEGIN IMMEDIATE")
            new_credits = credits - price
            STATE.conn.execute(
                "INSERT INTO kv(key,value) VALUES('credits',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(new_credits),),
            )
            STATE.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (
                    token_id, PICK_STATE.REAL_PICK_RESOURCE_ID, "unassigned",
                    json.dumps(token, separators=(",", ":")),
                ),
            )
            PICK_STATE.register_cursor(
                STATE.conn, token, pool_key, title, option_count, resources,
            )
            STATE.conn.commit()
        except Exception:
            STATE.conn.rollback()
            raise

    log.info(
        "PLAYER PICK STORE purchased packId=%d token=%d pool=%s options=%d minRating=%d resources=%s",
        int(pack_id), token_id, pool_key, option_count, min_rating, resources,
    )
    native_token = _native_pack_item(token)
    return 200, {
        "success": True,
        "numberItems": 1,
        "purchasedPackId": int(pack_id),
        "packId": int(pack_id),
        "duplicateItemIdList": [],
        "itemList": [native_token],
        "itemData": [native_token],
        "items": [native_token],
        "playerPickItemId": token_id,
        "playerPickId": token_id,
        "isPlayerPick": True,
        **_credits_payload(),
    }


def _native_player_pick_payload(active: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the actual FIFA 20 temporary-storage Player Pick envelope."""
    current = active if isinstance(active, dict) else PICK_STATE.active(STATE)
    if not current:
        return {
            "success": True, "playerPicks": [], "picks": [], "items": [],
            "itemData": [], "itemList": [], "numberItems": 0,
            "availablePicks": 0, "pickSize": 0, "selectionSize": 0, "count": 0,
        }
    items = [dict(item) for item in current.get("items", []) if isinstance(item, dict)]
    ownership = [bool(value) for value in current.get("ownership", [])]
    token_id = _int_or(current.get("token_id"), 0)
    response = {"items": items, "availablePicks": 1, "ownership": ownership}
    return {
        "success": True,
        "playerPickItemId": token_id,
        "playerPickId": token_id,
        "pickId": token_id,
        "data": {"playerPicks": items, **response},
        "response": response,
        "playerPicks": items,
        "picks": items,
        "itemData": items,
        "itemList": items,
        "items": items,
        "numberItems": len(items),
        "availablePicks": 1,
        "pickSize": len(items),
        "selectionSize": len(items),
        "ownership": ownership,
        "duplicateItemIdList": [],
        "count": 1,
        **_credits_payload(),
    }


def _open_native_player_pick(token_id: int) -> tuple[int, dict[str, Any]]:
    token = PICK_STATE.token(STATE, token_id)
    existing = PICK_STATE.active(STATE)
    if existing:
        return 200, _native_player_pick_payload(existing)
    if token is None:
        return 404, {"success": False, "reason": "PLAYER_PICK_TOKEN_NOT_FOUND"}

    resources = [
        canonical_player_resource_id(_int_or(value, 0))
        for value in token.get("pool", [])
        if canonical_player_resource_id(_int_or(value, 0)) in PLAYER_DB
    ]
    option_count = max(2, min(5, _int_or(token.get("option_count"), 3)))
    if len(resources) < option_count:
        resources = _generate_player_pick_resources(
            str(token.get("pool_key", "promo")), option_count, 0,
        )
    resources = resources[:option_count]
    if len(resources) < 2:
        return 409, {"success": False, "reason": "PLAYER_PICK_POOL_EMPTY"}

    owned = STATE.owned_player_resource_ids()
    items: list[dict[str, Any]] = []
    ownership: list[bool] = []
    for resource_id in resources:
        candidate_id = STATE.next_item_id()
        candidate = _native_pack_item(
            STATE.build_player_item(resource_id, candidate_id, "none")
        )
        # Temporary pick candidates are not owned pile items until one is
        # selected. These fields make FIFA display them as choices instead of
        # trying to move all of them into New Items.
        for key in ("pile", "definitionId", "_definitionId"):
            candidate.pop(key, None)
        candidate.update({"untradeable": True, "discardValue": 0})
        items.append(candidate)
        ownership.append(resource_id in owned)

    current, _created = PICK_STATE.begin(STATE, token_id, items, ownership)
    if current is None:
        return 409, {"success": False, "reason": "PLAYER_PICK_ALREADY_REDEEMED"}
    log.info(
        "PLAYER PICK opened token=%d candidates=%s",
        token_id, [item.get("resourceId") for item in current.get("items", [])],
    )
    return 200, _native_player_pick_payload(current)


def _native_pack_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return the item shape used by native FUT pack/unassigned responses.

    Keep the real resourceId (the client uses it to resolve the player/card), but
    omit local-only/derived keys that are not part of the original pack item
    payload.  In particular, do not send assetId alongside resourceId here: the
    native client derives the base asset from resourceId.
    """
    out = dict(item)
    if str(out.get("itemType", "player")).lower() == "player":
        resource_id = canonical_player_resource_id(
            _int_or(out.get("resourceId", out.get("assetId", 0)), 0)
        )
        meta = player_meta(resource_id)
        out["resourceId"] = resource_id
        if meta:
            out["assetId"] = _native_player_asset_id(meta, resource_id)
    # Keep assetId on FIFA 20 PC. Removing it can make some base cards render
    # as NOT FOUND even when resourceId is valid. Only strip server-only fields.
    for key in ("pile", "special", "tier", "wefutCard"):
        out.pop(key, None)
    return out


STATE = State(DB_PATH)
FLOW = RuntimeFlow(RUNTIME_ROOT / "status.json")


_SBC_REQUIREMENT_KEYS = {
    # FIFA 20's native SBC eligibility enum. These are deliberately not the
    # newer Web App kvPairs keys (notably, chemistry is key 1 in FIFA 20).
    "chemistry": ("TEAM_CHEMISTRY", 1),
    "player_count": ("PLAYER_COUNT", 2),
    "quality": ("PLAYER_QUALITY", 3),
    "same_nation": ("SAME_NATION_COUNT", 4),
    "same_league": ("SAME_LEAGUE_COUNT", 5),
    "same_club": ("SAME_CLUB_COUNT", 6),
    "nation_count": ("NATION_COUNT", 7),
    "league_count": ("LEAGUE_COUNT", 8),
    "club_count": ("CLUB_COUNT", 9),
    "specific_nation": ("PLAYER_NATION", 10),
    "specific_league": ("PLAYER_LEAGUE", 11),
    "specific_club": ("PLAYER_CLUB", 12),
    "card_type_count": ("PLAYER_RARITY", 18),
    "rare_count": ("PLAYER_RARITY", 18),
    "team_rating": ("TEAM_RATING", 19),
}
_SBC_SCOPE_IDS = {"min": 0, "max": 1, "exact": 2}


def _sbc_awards_payload(awards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for raw in awards:
        award_type = str(raw.get("type", "")).upper()
        count = max(1, _int_or(raw.get("count"), 1))
        if award_type == "PACK":
            pack_id = _sbc_reward_pack_id(
                raw.get("pack_id") or raw.get("packId") or raw.get("value")
            )
            cfg = get_pack_cfg(pack_id)
            store_group = str(cfg.get("store_group", "gold")).lower()
            hal_id = _int_or(
                cfg.get("pack_asset_id"),
                1 if store_group == "bronze" else 2 if store_group == "silver" else 3,
            )
            pack_name = str(
                raw.get("display_name") or raw.get("displayName")
                or raw.get("name") or cfg.get("name") or f"FUT Pack {pack_id}"
            )
            result.append({
                "value": pack_id,
                "awardValue": pack_id,
                "type": "pack",
                "awardType": "pack",
                "halId": hal_id,
                "count": count,
                "awardCount": count,
                "isUntradeable": True,
                "untradeable": True,
                "loan": 0,
                "name": pack_name,
                "displayName": pack_name,
                "packName": pack_name,
                "packId": pack_id,
                "packAssetId": hal_id,
            })
        elif award_type == "COINS":
            amount = max(0, _int_or(raw.get("value"), 0))
            result.append({
                "value": amount,
                "awardValue": amount,
                "type": "coin",
                "awardType": "coin",
                "halId": 0,
                "count": count,
                "awardCount": count,
                "isUntradeable": False,
                "loan": 0,
            })
        elif award_type == "PLAYER_PICK":
            options = max(2, min(5, _int_or(raw.get("options"), 3)))
            title = str(
                raw.get("display_name") or raw.get("displayName") or raw.get("title")
                or f"1 of {options} FUT Champions Players"
            )
            result.append({
                # The carousel preview is the same static miscPlayerPick item
                # that is granted on completion. A synthetic playerPick type
                # falls through to FIFA's blank pack placeholder.
                "value": PICK_STATE.REAL_PICK_RESOURCE_ID,
                "awardValue": PICK_STATE.REAL_PICK_RESOURCE_ID,
                "type": "misc",
                "awardType": "playerPick",
                "halId": PICK_STATE.REAL_PICK_CARD_ASSET_ID,
                "count": count,
                "awardCount": count,
                "isUntradeable": True,
                "untradeable": True,
                "loan": 0,
                "resourceId": PICK_STATE.REAL_PICK_RESOURCE_ID,
                "assetId": PICK_STATE.REAL_PICK_RESOURCE_ID,
                "cardassetid": PICK_STATE.REAL_PICK_CARD_ASSET_ID,
                "cardsubtypeid": 237,
                "amount": 4012,
                "name": "PlayerPickItemName113",
                "displayName": title,
                "description": title,
                "playerPickPool": str(raw.get("pool", "promo")),
                "playerPickOptions": options,
            })
        elif award_type == "PLAYER":
            resource_id = canonical_player_resource_id(
                _int_or(raw.get("resource_id") or raw.get("resourceId")
                        or raw.get("value"), 0)
            )
            meta = player_meta(resource_id)
            loan_games = max(0, _int_or(raw.get("loan_games") or raw.get("loan"), 0))
            if meta:
                asset_id = _native_player_asset_id(meta, resource_id)
                preview_id = 980_000_000_000 + resource_id
                preview = _native_player_item(
                    STATE.build_player_item(resource_id, preview_id, "none")
                )
                preview["untradeable"] = True
                player_name = str(
                    meta.get("commonName") or meta.get("name")
                    or meta.get("lastName") or "Player Reward"
                )
                result.append({
                    "value": resource_id,
                    "awardValue": resource_id,
                    "type": "player",
                    "awardType": "player",
                    "halId": _int_or(meta.get("cardassetid"), _int_or(meta.get("rareflag"), 0)),
                    "count": count,
                    "awardCount": count,
                    "isUntradeable": True,
                    "untradeable": True,
                    "loan": loan_games,
                    "loanGames": loan_games,
                    "resourceId": resource_id,
                    "assetId": asset_id,
                    "name": player_name,
                    "displayName": player_name,
                    "playerName": player_name,
                    "rating": _int_or(meta.get("rating"), 0),
                    "preferredPosition": str(meta.get("preferredPosition", "any")),
                    "itemData": preview,
                    "item": preview,
                })
    return result


def _sbc_elg_entry(rule_type: str, slot: int, key: int, value: int) -> dict[str, Any]:
    payload = {
        "type": str(rule_type),
        "eligibilitySlot": int(slot),
        "eligibilityKey": int(key),
        "eligibilityValue": int(value),
    }
    return payload


def _sbc_requirement_payload(raw: dict[str, Any], eligibility_slot: int) -> list[dict[str, Any]]:
    """Encode one catalog rule using FIFA 20's flattened elgReq wire format."""
    kind = str(raw.get("kind", ""))
    scope = str(raw.get("scope", "min"))
    value = _int_or(raw.get("value"), 0)
    count = _int_or(raw.get("count"), -1)
    scope_value = _SBC_SCOPE_IDS.get(scope, 0)

    # Counted quality/rarity rules are represented by three records sharing a
    # slot: the player count, its scope, then the player property to match.
    if kind == "quality" and count >= 0:
        return [
            _sbc_elg_entry("PLAYER_COUNT", eligibility_slot, 2, count),
            _sbc_elg_entry("SCOPE", eligibility_slot, 13, scope_value),
            _sbc_elg_entry("PLAYER_LEVEL", eligibility_slot, 17, value),
        ]
    if kind == "rare_count":
        return [
            _sbc_elg_entry("PLAYER_COUNT", eligibility_slot, 2, value),
            _sbc_elg_entry("SCOPE", eligibility_slot, 13, scope_value),
            _sbc_elg_entry("PLAYER_RARITY", eligibility_slot, 18, 1),
        ]
    if kind in ("specific_nation", "specific_league", "specific_club", "card_type_count"):
        rule_type, key = _SBC_REQUIREMENT_KEYS[kind]
        return [
            _sbc_elg_entry("PLAYER_COUNT", eligibility_slot, 2, max(0, count)),
            _sbc_elg_entry("SCOPE", eligibility_slot, 13, scope_value),
            _sbc_elg_entry(rule_type, eligibility_slot, key, value),
        ]

    rule_type, key = _SBC_REQUIREMENT_KEYS.get(kind, (kind.upper(), 0))
    return [
        _sbc_elg_entry(rule_type, eligibility_slot, key, value),
        _sbc_elg_entry("SCOPE", eligibility_slot, 13, scope_value),
    ]


def _sbc_slot_requirements(squad_size: int) -> list[dict[str, Any]]:
    open_slots = max(0, min(11, int(squad_size)))
    return [
        {"index": index, "playerType": "DEFAULT" if index < open_slots else "BRICK"}
        for index in range(11)
    ]


def _sbc_empty_item(item_type: str) -> dict[str, Any]:
    item = {
        "id": 0,
        "timestamp": 0,
        "formation": "any",
        "untradeable": False,
        "assetId": 0,
        "rating": 0,
        "itemType": str(item_type),
        "resourceId": 0,
        "owners": 0,
        "discardValue": 0,
        "itemState": "invalid",
        "cardsubtypeid": 0,
        "lastSalePrice": 0,
        "morale": 0,
        "fitness": 0,
        "injuryGames": 0,
        "preferredPosition": "any",
        "statsList": [],
        "lifetimeStats": [],
        "training": 0,
        "contract": 0,
        "suspension": 0,
        "attributeList": [],
        "teamid": 0,
        "rareflag": 0,
        "pile": 0,
        "nation": 0,
        "resourceGameYear": 2020,
    }
    if item_type == "player":
        item["loyaltyBonus"] = 1
    return item


def _sbc_set_summary(set_entry: dict[str, Any], challenge_counts: dict[int, int], set_counts: dict[int, int]) -> dict[str, Any]:
    challenge_entries = list(set_entry.get("challenges", []))
    completed = sum(1 for entry in challenge_entries if challenge_counts.get(int(entry["challenge_id"]), 0) > 0)
    set_id = int(set_entry["set_id"])
    times_completed = int(set_counts.get(set_id, 0))
    repeatable = bool(set_entry.get("repeatable", False))
    # FIFA 20 treats challengesCompletedCount == challengesCount as a closed
    # set before it considers the repeatable flag.  Reporting the lifetime
    # completion here therefore leaves an Upgrade tile visible but makes the
    # X/Select action a no-op.  A repeatable cycle is always presented as open;
    # its lifetime count remains available in timesCompleted.
    visible_completed = 0 if repeatable else completed
    preview_awards = list(set_entry.get("awards", []))
    # A large part of the retail catalogue consists of one-squad SBCs whose
    # only reward belongs to that challenge rather than to the containing set.
    # FIFA still previews it in the group-reward panel.  Mirror it for display
    # only; the completion path continues to grant the challenge award once.
    if not preview_awards and len(challenge_entries) == 1:
        preview_awards = list(challenge_entries[0].get("awards", []))
    return {
        "setId": set_id,
        "name": str(set_entry["name"]),
        "priority": int(set_entry.get("priority", 0)),
        "categoryId": int(set_entry["category_id"]),
        "description": str(set_entry["description"]),
        "challengesCount": len(challenge_entries),
        "hidden": False,
        "tagged": 0,
        "endTime": 0,
        "repeatable": repeatable,
        "challengesCompletedCount": visible_completed,
        "awards": _sbc_awards_payload(preview_awards),
        # The original service used this bit to gate every later set until the
        # onboarding group was completed. The local catalog is all unlocked.
        "tutorial": False,
        "timesCompleted": times_completed,
        "taggedByProduction": False,
        "taggedByUser": False,
        # Version the URL so clients discard the old square synthetic badge.
        "setImageId": f"{1000000 + set_id}-localfut20-sbc-v9",
    }


def _sbc_sets_payload() -> dict[str, Any]:
    challenge_counts, set_counts = STATE.sbc_completion_counts()
    categories = []
    category_order = (
        (5, "LIVE"), (1, "BASIC"), (2, "ADVANCED"),
        (3, "UPGRADES"), (4, "PLAYERS"),
    )
    for priority, (category_id, name) in enumerate(category_order, 1):
        sets = [
            _sbc_set_summary(entry, challenge_counts, set_counts)
            for entry in SBC_CATALOG
            if int(entry["category_id"]) == category_id
        ]
        categories.append({
            "categoryId": category_id,
            "id": category_id,
            "name": name,
            "priority": priority,
            "sets": sets,
        })
    return {"categories": categories}


def _sbc_challenge_payload(
    set_entry: dict[str, Any],
    challenge_entry: dict[str, Any],
    completion_count: int,
    priority: int = 0,
) -> dict[str, Any]:
    challenge_id = int(challenge_entry["challenge_id"])
    set_id = int(set_entry["set_id"])
    repeatable = bool(set_entry.get("repeatable", False))
    # The number of occupied SBC slots is already represented by the
    # playerRequirements array in the squad document.  Sending PLAYER_COUNT
    # again in elgReq makes FIFA 20 render a second, permanently-red condition
    # (for example the red "Players: Exactly 3" beside the green native count).
    # Keep the rule in the server-side evaluator, but omit that duplicate wire
    # rule and number the remaining eligibility slots contiguously.
    wire_rules = [
        rule for rule in challenge_entry.get("rules", [])
        if str(rule.get("kind", "")) != "player_count"
        and bool(rule.get("supported", True))
    ]
    requirements: list[dict[str, Any]] = []
    for eligibility_slot, rule in enumerate(wire_rules, 1):
        requirements.extend(_sbc_requirement_payload(rule, eligibility_slot))
    squad_size = int(challenge_entry.get("players", 11))
    return {
        "name": str(challenge_entry["name"]),
        "priority": int(priority),
        "status": "COMPLETED" if completion_count and not repeatable else "NOT_STARTED",
        "setId": set_id,
        "description": str(challenge_entry["description"]),
        "challengeId": challenge_id,
        "endTime": 0,
        "repeatable": repeatable,
        "formation": str(challenge_entry.get("formation", "f442")),
        "timesCompleted": int(completion_count),
        "elgReq": requirements,
        "elgOperation": "AND",
        "tutorial": 0,
        "type": "OPEN_CHALLENGE" if squad_size == 11 else "BRICK_CHALLENGE",
        "challengeImageId": f"{100000 + challenge_id}-localfut20-sbc",
        # A one-challenge Upgrade earns its reward at set completion. Mirror
        # that same pack in the challenge preview so both retail-client views
        # show the correct reward, without granting it twice on submission.
        "awards": _sbc_awards_payload(list(
            challenge_entry.get("awards", [])
            or (set_entry.get("awards", []) if len(set_entry.get("challenges", [])) == 1 else [])
        )),
        # Preserve the exact archive wording even when the retail client's
        # finite eligibility enum cannot render a newer multi-card-type rule.
        "sourceRequirements": list(challenge_entry.get("source_requirements", [])),
        "requirementsText": list(challenge_entry.get("source_requirements", [])),
        "sourceRewards": list(challenge_entry.get("source_rewards", [])),
    }


def _sbc_challenges_payload(set_id: int) -> dict[str, Any] | None:
    set_entry = SET_BY_ID.get(int(set_id))
    if not set_entry:
        return None
    challenge_counts, _ = STATE.sbc_completion_counts()
    challenges = [
        _sbc_challenge_payload(
            set_entry,
            entry,
            challenge_counts.get(int(entry["challenge_id"]), 0),
            priority,
        )
        for priority, entry in enumerate(set_entry.get("challenges", []))
    ]
    return {"challenges": challenges}


def _sbc_saved_item_ids(saved: dict[str, Any] | None) -> list[int]:
    result: list[int] = []
    players = saved.get("players", []) if isinstance(saved, dict) else []
    for entry in players if isinstance(players, list) else []:
        embedded = entry.get("itemData") if isinstance(entry, dict) and isinstance(entry.get("itemData"), dict) else {}
        item_id = _int_or(entry.get("itemId") or embedded.get("id") or entry.get("id"), 0) if isinstance(entry, dict) else 0
        if item_id > 0 and item_id not in result:
            result.append(item_id)
    return result


def _sbc_squad_response(set_entry: dict[str, Any], challenge_entry: dict[str, Any]) -> dict[str, Any]:
    challenge_id = int(challenge_entry["challenge_id"])
    squad_size = int(challenge_entry.get("players", 11))
    saved = STATE.sbc_squad(challenge_id) or {}
    saved_by_index = {
        _int_or(entry.get("index"), -1): entry
        for entry in saved.get("players", [])
        if isinstance(entry, dict)
    }
    players = []
    ratings: list[int] = []
    for index in range(23):
        item_id = 0
        if index < 11 and index < squad_size and index in saved_by_index:
            embedded = saved_by_index[index].get("itemData", {})
            item_id = _int_or(embedded.get("id"), 0) if isinstance(embedded, dict) else 0
        item = STATE.get_item(item_id) if item_id else None
        if isinstance(item, dict):
            item_data = _native_player_item(item)
            item_data.setdefault("formation", "any")
            item_data.setdefault("loyaltyBonus", 1)
            item_data.setdefault("resourceGameYear", 2020)
            ratings.append(_int_or(item_data.get("rating"), 0))
        else:
            item_data = _sbc_empty_item("player")
        players.append({"index": index, "itemData": item_data})

    reported_chemistry = _int_or(saved.get("chemistry"), 0)
    squad = {
        "id": 1,
        "formation": str(challenge_entry.get("formation", "f442")),
        "rating": int(round(sum(ratings) / len(ratings))) if ratings else 0,
        "chemistry": max(0, min(100, reported_chemistry)),
        "manager": [_sbc_empty_item("manager")],
        "players": players,
    }
    return {
        "challengeId": challenge_id,
        "playerRequirements": _sbc_slot_requirements(squad_size),
        "squad": squad,
    }


def _sbc_quality(item: dict[str, Any]) -> int:
    rating = _int_or(item.get("rating"), 0)
    return 3 if rating >= 75 else 2 if rating >= 65 else 1


def _sbc_evaluate(challenge_entry: dict[str, Any], saved: dict[str, Any] | None) -> tuple[bool, list[dict[str, Any]], list[int]]:
    item_ids = _sbc_saved_item_ids(saved)
    items = [STATE.get_item(item_id) for item_id in item_ids]
    items = [item for item in items if isinstance(item, dict) and str(item.get("itemType", "player")).lower() == "player"]
    ratings = [_int_or(item.get("rating"), 0) for item in items]
    team_rating = int(round(sum(ratings) / len(ratings))) if ratings else 0
    # FIFA computes chemistry client-side. Some native builds do not echo it in
    # the squad PUT, so absence means "client validated" rather than zero.
    reported_chemistry = _int_or(saved.get("chemistry"), -1) if isinstance(saved, dict) else -1
    chemistry = reported_chemistry if reported_chemistry >= 0 else 100

    nations = [_int_or(item.get("nation"), 0) for item in items]
    leagues = [_int_or(item.get("leagueId"), 0) for item in items]
    clubs = [_int_or(item.get("teamid"), 0) for item in items]

    def distinct(values: list[int]) -> int:
        return len({value for value in values if value > 0})

    def largest_group(values: list[int]) -> int:
        counts: dict[int, int] = {}
        for value in values:
            if value > 0:
                counts[value] = counts.get(value, 0) + 1
        return max(counts.values(), default=0)

    actuals = {
        "player_count": len(items),
        "quality": [_sbc_quality(item) for item in items],
        "rare_count": sum(1 for item in items if _int_or(item.get("rareflag"), 0) > 0),
        "nation_count": distinct(nations),
        "league_count": distinct(leagues),
        "club_count": distinct(clubs),
        "same_nation": largest_group(nations),
        "same_league": largest_group(leagues),
        "same_club": largest_group(clubs),
        "team_rating": team_rating,
        "chemistry": chemistry,
        "specific_nation": nations,
        "specific_league": leagues,
        "specific_club": clubs,
        "card_type_count": [_int_or(item.get("rareflag"), 0) for item in items],
        "special_count": sum(1 for item in items if _int_or(item.get("rareflag"), 0) > 1),
    }

    checks = []
    all_valid = True
    for raw in challenge_entry.get("rules", []):
        if not bool(raw.get("supported", True)):
            continue
        kind = str(raw.get("kind", ""))
        target = _int_or(raw.get("value"), 0)
        scope = str(raw.get("scope", "min"))
        count = _int_or(raw.get("count"), -1)
        actual: Any = actuals.get(kind, 0)
        if kind == "quality":
            qualities = actual if isinstance(actual, list) else []
            if count < 0:
                valid = bool(qualities) and all(value == target for value in qualities) if scope == "exact" else (
                    bool(qualities) and all(value >= target for value in qualities) if scope == "min" else
                    bool(qualities) and all(value <= target for value in qualities)
                )
                actual_value = min(qualities, default=0) if scope == "min" else max(qualities, default=0)
            else:
                matching = sum(1 for value in qualities if value >= target) if scope == "min" else sum(1 for value in qualities if value == target)
                valid = matching >= count
                actual_value = matching
        elif kind in ("specific_nation", "specific_league", "specific_club", "card_type_count"):
            values = actual if isinstance(actual, list) else []
            actual_value = sum(1 for item_value in values if item_value == target)
            required = max(0, count)
            valid = actual_value >= required if scope == "min" else (
                actual_value <= required if scope == "max" else actual_value == required
            )
        else:
            actual_value = _int_or(actual, 0)
            valid = actual_value >= target if scope == "min" else actual_value <= target if scope == "max" else actual_value == target
        checks.append({
            "type": kind,
            "description": str(raw.get("text", kind)),
            "actual": actual_value,
            "target": count if (
                count >= 0 and kind in (
                    "quality", "specific_nation", "specific_league",
                    "specific_club", "card_type_count",
                )
            ) else target,
            "scope": scope,
            "valid": bool(valid),
        })
        all_valid = all_valid and bool(valid)
    return all_valid, checks, item_ids


def local_user_info() -> dict[str, Any]:
    return {
        "personaId": int(CFG["persona_id"]),
        "personaName": CFG["display_name"],
        "nucleusId": int(CFG["nucleus_id"]),
        "clubId": int(CFG["club_id"]),
        "clubName": CFG["club_name"],
        "clubAbbr": CFG["club_abbr"],
        "credits": STATE.credits(),
    }


def account_info() -> dict[str, Any]:
    """Return the pre-auth account-discovery document used by FUT 20.

    This is deliberately *not* a session response.  Retail FUT first reads a
    persona and a club SKU from this document, obtains a Nucleus auth code,
    and only then POSTs /ut/auth.  Supplying sid/token fields here made the
    native client treat an unauthenticated account lookup as a malformed
    session and stop before its Nucleus hand-off.
    """
    stamp = now_s()
    club = {
        "year": "2020",
        "assetId": int(CFG["club_id"]),
        "teamId": int(CFG["club_id"]),
        "lastAccessTime": stamp,
        "platform": "pc",
        "clubName": str(CFG["club_name"]),
        "clubAbbr": str(CFG["club_abbr"]),
        "established": int(STATE.get("established", stamp)),
        "divisionOnline": 10,
        "badgeId": 0,
        # FIFA 20 expects its single-SKU discovery value as a scalar string.
        "skuAccessList": str(CFG["game_sku"]),
    }
    persona = {
        "personaId": int(CFG["persona_id"]),
        "personaName": str(CFG["display_name"]),
        # FIFA 20's JSON reader expects numeric 0/1 here (rather than a JSON
        # boolean) and explicitly reads userState before walking the club list.
        "returningUser": 1,
        "userState": None,
        "userClubList": [club],
    }
    return {
        "userAccountInfo": {
            "dateOfBirth": str(CFG["date_of_birth"]),
            "personas": [persona],
        },
    }


def _completed_onboarding_client_data() -> dict[str, Any]:
    equipped = {
        str(item.get("itemState", "")): int(item.get("resourceId", 0) or 0)
        for item in _active_club_items()
    }
    return {"entries": [
        # Completion acknowledgement plus the three selection markers that
        # drive the native resume state all the way to normal FUT navigation.
        {"key": 0, "value": 3},
        {"key": 1, "value": equipped.get("activeHomeKit", 6300074)},
        {"key": 2, "value": equipped.get("activeAwayKit", 6400057)},
        {"key": 3, "value": equipped.get("activeBadge", 6000057)},
    ]}


def _completed_loan_client_data() -> dict[str, Any]:
    # This value is a 32-bit player definition/resource id, never the much
    # larger owned-item id.  Any valid saved squad player proves that the local
    # club is already past the starter-loan selection stage.
    for slot in _native_squad().get("players", []):
        item = slot.get("itemData") if isinstance(slot, dict) else None
        if not isinstance(item, dict):
            continue
        value = int(item.get("assetId") or item.get("resourceId") or 0) & 0xFFFFFF
        if value:
            return {"entries": [{"key": 0, "value": value}]}
    return {"entries": [{"key": 0, "value": 1}]}


def user_mass_info() -> dict[str, Any]:
    # FIFA's startup decoder feeds these sections into the same native readers
    # used by /user, /squad/active, and /loan/players.  Keep the current SCMP
    # campaign at the *top level* too.  Retail FIFA refreshes userMassInfo as a
    # shared model and the objectives UI can use this revision to invalidate a
    # campaign snapshot without forcing a FUT re-authentication.
    campaign = _scmp_campaign()
    return {
        "userInfo": _native_user(),
        "squad": _native_squad(),
        "loanPlayers": {"loans": []},
        "loanPlayerClientData": _completed_loan_client_data(),
        "onboardingClientData": _completed_onboarding_client_data(),
        "credits": STATE.credits(),
        "enablePlayerPicks": 1,
        "isPlayerPicksTemporaryStorageNotEmpty": bool(PICK_STATE.pending_flag(STATE)),
        **_scmp_live_fields(campaign),
    }


def club_info() -> dict[str, Any]:
    items = STATE.list_items("club")
    record = STATE.global_match_record()
    return {
        "clubInfo": {
            "clubId": int(CFG["club_id"]),
            "clubName": CFG["club_name"],
            "clubAbbr": CFG["club_abbr"],
            "established": 2014,
            "credits": STATE.credits(),
            **record,
        },
        "clubId": int(CFG["club_id"]),
        "clubName": CFG["club_name"],
        "clubAbbr": CFG["club_abbr"],
        "clubCount": len(items),
        "itemData": items,
        "credits": STATE.credits(),
        **record,
    }


def store_info() -> dict[str, Any]:
    pack = {
        "id": 1,
        "packId": 1,
        "packType": "LOCAL_GOLD",
        "packTypeId": 1,
        "name": "Local Gold Pack",
        "description": "12 local player items",
        "coins": int(CFG.get("pack_cost", 7500)),
        "points": 0,
        "numberItems": int(CFG.get("pack_size", 12)),
        "numItems": int(CFG.get("pack_size", 12)),
        "rare": 3,
        "isAvailableInStore": True,
        "isavailableinstore": True,
        "categoryId": 1,
        "packContentInfo": {
            "gold": int(CFG.get("pack_size", 12)),
            "rare": 3,
            "items": int(CFG.get("pack_size", 12)),
        },
    }
    return {
        "storeEnabled": True,
        "cardPackStoreEnabled": True,
        "packList": [pack],
        "packTypes": [pack],
        "credits": STATE.credits(),
    }


def purchased_info() -> dict[str, Any]:
    return {
        "unopenedPacks": 0,
        "packList": [],
        "purchasedItems": [],
        "credits": STATE.credits(),
    }


def _pow_sid() -> str:
    return "pow." + hashlib.sha256(("POW-" + STATE.token()).encode("utf-8")).hexdigest()[:32]


def _control_session() -> dict[str, Any] | None:
    session_path = RUNTIME_ROOT / "session.json"
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
        if int(data.get("expiresAt", 0)) < now_s():
            return None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _ut_sid() -> str:
    session = _control_session()
    seed = str(session.get("sessionId")) if session else STATE.sid()
    return "ut." + hashlib.sha256(("UT-" + seed).encode("utf-8")).hexdigest()[:32]


def _credits_payload() -> dict[str, Any]:
    credits = STATE.credits()
    unopened_count = sum(STATE.unopened_pack_counts().values())
    return {
        "currencies": [
            {"name": "coins", "funds": credits, "finalFunds": credits},
            {"name": "points", "funds": 0, "finalFunds": 0},
        ],
        "unopenedPacks": {"preOrderPacks": 0, "recoveredPacks": unopened_count, "total": unopened_count},
        "unopenedPackCount": unopened_count,
    }


_CLUB_ACTIVE_STATES = (
    "activeStadium", "activeHomeKit", "activeAwayKit", "activeBadge", "activeBall",
)


def _ensure_persistent_club_items() -> None:
    """Migrate the old bootstrap-only customization set into SQLite once."""
    owned = [
        item for item in (*STATE.list_items("club"), *STATE.list_items("squad"))
        if isinstance(item, dict)
    ]
    # Repair identity fields on club items created by older builds. In
    # particular this replaces the non-localized ball 191 without discarding
    # the user's equipped selection or creating a duplicate active item.
    for item in owned:
        resource_id = _int_or(item.get("resourceId"), 0)
        meta = CLUB_ITEM_DB.get(resource_id)
        if not isinstance(meta, dict) or str(meta.get("itemType", "")) not in ("stadium", "ball"):
            continue
        identity_fields = {
            key: meta[key]
            for key in ("assetId", "stadiumid", "ballid", "capacity", "name")
            if key in meta
        }
        if any(item.get(key) != value for key, value in identity_fields.items()):
            changed = STATE.update_item_fields(_int_or(item.get("id"), 0), identity_fields)
            if isinstance(changed, dict):
                item.update(changed)
    active_states = {
        str(item.get("itemState", "")) for item in owned
        if str(item.get("itemState", "")) in _CLUB_ACTIVE_STATES
    }
    for resource_id, meta in CLUB_ITEM_DB.items():
        state = str(meta.get("itemState", "free"))
        if state not in _CLUB_ACTIVE_STATES:
            continue
        if state not in active_states:
            STATE.make_club_item(resource_id, "club", state)
            active_states.add(state)


def _active_club_items() -> list[dict[str, Any]]:
    """Return the persisted Home/Away/Badge/Stadium/Ball selections."""
    _ensure_persistent_club_items()
    active_by_state: dict[str, dict[str, Any]] = {}
    for item in (*STATE.list_items("club"), *STATE.list_items("squad")):
        if not isinstance(item, dict):
            continue
        state = str(item.get("itemState", ""))
        if state in _CLUB_ACTIVE_STATES:
            active_by_state[state] = dict(item)
    result = []
    for state in _CLUB_ACTIVE_STATES:
        item = active_by_state.get(state)
        if item:
            item.pop("pile", None)
            result.append(item)
    return result


def _native_player_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize locally stored items to FIFA 20's native player-item shape."""
    resource_id = canonical_player_resource_id(
        int(item.get("resourceId", item.get("assetId", 0)) or 0)
    )
    meta = player_meta(resource_id)
    minimum_price, maximum_price = _market_price_bounds(resource_id, "player")
    return {
        "assetId": _native_player_asset_id(meta, resource_id),
        "attributeList": item.get("attributeList", []) if isinstance(item.get("attributeList", []), list) else [],
        "cardsubtypeid": 0,
        "contract": int(item.get("contract", item.get("contracts", 7)) or 0),
        "discardValue": int(item.get("discardValue", 600) or 0),
        "fitness": int(item.get("fitness", 99) or 0),
        "formation": str(item.get("formation", "any") or "any"),
        "injuryGames": int(item.get("injuryGames", 0) or 0),
        "injuryType": 0,
        "itemState": str(item.get("itemState", "free") or "free"),
        "itemType": "player",
        "lastSalePrice": int(item.get("lastSalePrice", 0) or 0),
        "leagueId": int(item.get("leagueId", 0) or 0),
        "loyaltyBonus": int(item.get("loyaltyBonus", 1) or 0),
        "loans": int(item.get("loans", 0) or 0),
        "morale": int(item.get("morale", 50) or 0),
        "nation": int(item.get("nation", 0) or 0),
        "owners": int(item.get("owners", 1) or 1),
        "preferredPosition": str(item.get("preferredPosition", "") or ""),
        "rareflag": int(item.get("rareflag", CFG.get("rare_flag", 1)) or 0),
        "rating": int(item.get("rating", 0) or 0),
        "resourceId": resource_id,
        # FIFA's Compare Price request reads definitionId from the selected
        # auction item.  Keep it equal to resourceId so special cards compare
        # against that exact version rather than the base player's assetId.
        "definitionId": resource_id,
        "resourceGameYear": int(item.get("resourceGameYear", 2020) or 2020),
        "suspension": int(item.get("suspension", 0) or 0),
        "teamid": int(item.get("teamid", 0) or 0),
        "timestamp": int(item.get("timestamp", STATE.get("established", now_s())) or 0),
        "training": int(item.get("training", 0) or 0),
        "untradeable": bool(item.get("untradeable", False)),
        "playStyle": int(item.get("playStyle", 250) or 0),
        "posMods": item.get("posMods", []) if isinstance(item.get("posMods", []), list) else [],
        "id": int(item.get("id", 0) or 0),
        "statsList": item.get("statsList", []) if isinstance(item.get("statsList", []), list) else [],
        "lifetimeStats": item.get("lifetimeStats", []) if isinstance(item.get("lifetimeStats", []), list) else [],
        "assists": int(item.get("assists", 0) or 0),
        "lifetimeAssists": int(item.get("lifetimeAssists", 0) or 0),
        # FIFA's item factory attaches these bounds directly to the card.  The
        # native listing screen reads them before enabling its price/duration
        # controls, so do not rely only on the later pricelimits request.
        "marketDataMinPrice": minimum_price,
        "marketDataMaxPrice": maximum_price,
        "pile": str(item.get("pile", "club") or "club"),
        **_player_identity_fields(meta),
    }


_MARKET_TARGET_LISTINGS = 200
_MARKET_RESTOCK_SECONDS = 45
_MARKET_GOLD_POOL: list[int] | None = None
_MARKET_SPECIAL_POOL: list[int] | None = None
_MARKET_SILVER_POOL: list[int] | None = None
_MARKET_BRONZE_POOL: list[int] | None = None
_MARKET_MANAGER_POOL: list[int] | None = None
_MARKET_SELLER_NAMES = (
    "London FC", "Merseyside", "Manchester Blue", "Madrid CF", "Catalunya",
    "Turin Calcio", "Milano Rosso", "Paris XI", "Munich Stars", "Dortmund",
    "Amsterdam AFC", "Lisbon Eagles", "Local Traders", "FUT Founder",
)


def _market_round_price(value: int) -> int:
    value = max(150, min(15_000_000, int(value)))
    if value < 1_000:
        step = 50
    elif value < 10_000:
        step = 100
    elif value < 50_000:
        step = 250
    elif value < 100_000:
        step = 500
    else:
        step = 1_000
    return max(150, int(round(value / step)) * step)


def _market_bid_increment(price: int) -> int:
    price = max(0, int(price))
    if price < 1_000:
        return 50
    if price < 10_000:
        return 100
    if price < 50_000:
        return 250
    if price < 100_000:
        return 500
    return 1_000


def _market_recommended_price(resource_id: int, item_type: str = "player") -> int:
    """Return a stable, card-aware local valuation on FIFA's price ladder."""
    if str(item_type).lower() in ("manager", "staff"):
        meta = manager_meta(resource_id)
        rating = max(0, _int_or(meta.get("rating"), 0))
        base = {
            55: 250, 56: 250, 57: 300, 58: 300, 59: 350,
            60: 400, 61: 450, 62: 500, 63: 550, 64: 600,
            65: 650, 66: 700, 67: 750, 68: 800, 69: 850,
            70: 900, 71: 1_000, 72: 1_100, 73: 1_200, 74: 1_300,
            75: 1_500, 76: 1_700, 77: 1_900, 78: 2_100, 79: 2_400,
            80: 2_800, 81: 3_200, 82: 3_800, 83: 4_500, 84: 5_500,
        }.get(rating, 700)
        nudge = 0.92 + ((int(resource_id) * 29) % 17) / 100.0
        return _market_round_price(int(base * nudge))

    meta = player_meta(resource_id)
    rating = max(0, _int_or(meta.get("rating"), 0))
    anchors = {
        75: 700, 76: 750, 77: 800, 78: 900, 79: 1_000,
        80: 1_200, 81: 1_500, 82: 2_000, 83: 3_000, 84: 5_000,
        85: 9_000, 86: 15_000, 87: 28_000, 88: 48_000, 89: 80_000,
        90: 125_000, 91: 200_000, 92: 300_000, 93: 450_000,
        94: 650_000, 95: 900_000, 96: 1_200_000, 97: 1_700_000,
        98: 2_500_000, 99: 4_000_000,
    }
    if rating < 65:
        base = max(150, 200 + max(0, rating - 45) * 20)
    elif rating < 75:
        base = 350 + (rating - 65) * 65
    else:
        base = anchors.get(rating, 4_000_000)
    rareflag = _int_or(meta.get("rareflag"), 0)
    special = bool(meta.get("special", False)) or rareflag > 1
    if special:
        # Rating already captures most of a special card's value. Card type and
        # on-card quality provide the smaller premium that differentiates cards
        # with the same OVR.
        type_multiplier = {3: 1.22, 4: 1.28, 5: 1.75, 6: 1.35, 8: 1.32,
                           9: 1.38, 10: 1.35, 11: 1.55, 12: 1.65}.get(rareflag, 1.30)
        base = int(base * type_multiplier)
    face = meta.get("face", [])
    if isinstance(face, list) and face:
        useful = sorted((_int_or(value, 0) for value in face), reverse=True)[:3]
        quality = sum(useful) / max(1, len(useful))
        base = int(base * max(0.88, min(1.18, 0.88 + quality / 300.0)))
    # A deterministic per-card nudge keeps equally rated cards from all having
    # an identical price while keeping every listing near its recommendation.
    nudge = 0.92 + ((int(resource_id) * 37) % 17) / 100.0
    return _market_round_price(int(base * nudge))


def _market_price_bounds(resource_id: int, item_type: str = "player") -> tuple[int, int]:
    """Return the card's native listing range on FIFA's transfer-price ladder."""
    recommended = _market_recommended_price(int(resource_id), item_type)
    minimum = max(150, _market_round_price(int(recommended * 0.45)))
    maximum = min(15_000_000, max(5_000, _market_round_price(int(recommended * 2.5))))
    return minimum, maximum


def _market_pools() -> tuple[list[int], list[int]]:
    """Compatibility accessor for callers that need gold and special players."""
    pools = _market_all_pools()
    return pools["gold"], pools["special"]


def _market_all_pools() -> dict[str, list[int]]:
    global _MARKET_GOLD_POOL, _MARKET_SPECIAL_POOL
    global _MARKET_SILVER_POOL, _MARKET_BRONZE_POOL, _MARKET_MANAGER_POOL
    if any(pool is None for pool in (
            _MARKET_GOLD_POOL, _MARKET_SPECIAL_POOL, _MARKET_SILVER_POOL,
            _MARKET_BRONZE_POOL, _MARKET_MANAGER_POOL)):
        gold: list[int] = []
        special: list[int] = []
        silver: list[int] = []
        bronze: list[int] = []
        for resource_id, meta in PLAYER_DB.items():
            rating = _int_or(meta.get("rating"), 0)
            rareflag = _int_or(meta.get("rareflag"), 0)
            is_special = bool(meta.get("special", False)) or rareflag > 1
            if is_special:
                special.append(int(resource_id))
            elif rating >= 75:
                gold.append(int(resource_id))
            elif rating >= 65:
                silver.append(int(resource_id))
            else:
                bronze.append(int(resource_id))
        _MARKET_GOLD_POOL, _MARKET_SPECIAL_POOL = gold, special
        _MARKET_SILVER_POOL, _MARKET_BRONZE_POOL = silver, bronze
        _MARKET_MANAGER_POOL = [int(resource_id) for resource_id in MANAGER_DB]
    return {
        "gold": _MARKET_GOLD_POOL or [],
        "special": _MARKET_SPECIAL_POOL or [],
        "silver": _MARKET_SILVER_POOL or [],
        "bronze": _MARKET_BRONZE_POOL or [],
        "manager": _MARKET_MANAGER_POOL or [],
    }


def _market_item_kind(item: dict[str, Any]) -> str:
    return "manager" if str(item.get("itemType", "")).lower() in ("manager", "staff") else "player"


def _market_native_item(item: dict[str, Any]) -> dict[str, Any]:
    return _native_manager_item(item) if _market_item_kind(item) == "manager" else _native_player_item(item)


def _market_bucket(item: dict[str, Any]) -> str:
    if _market_item_kind(item) == "manager":
        return "manager"
    rating = _int_or(item.get("rating"), 0)
    resource_id = _int_or(item.get("resourceId", item.get("assetId", 0)), 0)
    meta = player_meta(resource_id)
    rareflag = _int_or(item.get("rareflag"), 0)
    if bool(meta.get("special", False)) or rareflag > 1:
        return "special"
    if rating >= 75:
        return "gold"
    if rating >= 65:
        return "silver"
    return "bronze"


def _market_cleanup_compare_seed_artifacts() -> int:
    """Remove exact-card batches manufactured by the old Compare Price path.

    Versions 0.9.2/0.9.3 created at least four CPU copies of one resource in
    the same second. A few cards in a batch may since have sold, so creation
    time plus resource is the durable signature. Ordinary stock is replenished
    after cleanup, while watched or bid-on cards are always preserved.
    """
    marker = "market_compare_seed_cleanup_v094"
    if bool(STATE.get(marker, False)):
        return 0
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for auction in STATE.list_auctions("cpu"):
        if str(auction.get("status")) != "active":
            continue
        key = (
            _int_or(auction.get("resource_id"), 0),
            _int_or(auction.get("created_at"), 0),
        )
        groups.setdefault(key, []).append(auction)

    removed = 0
    for (resource_id, _), rows in groups.items():
        rows.sort(key=lambda row: _int_or(row.get("trade_id"), 0))
        if len(rows) < 4:
            continue
        for row in rows:
            engaged = (
                bool(_int_or(row.get("watched"), 0))
                or bool(str(row.get("highest_bidder", "")))
                or _int_or(row.get("current_bid"), 0) > 0
            )
            if not engaged and STATE.delete_auction(_int_or(row.get("trade_id"), 0)):
                removed += 1
    STATE.set(marker, True)
    if removed:
        log.info("TRANSFER MARKET removed %d legacy Compare Price duplicate listing(s)", removed)
    return removed


def _market_restore_proven_population(
    active_cpu: list[dict[str, Any]], targets: dict[str, int]
) -> list[dict[str, Any]]:
    """Shrink an existing oversized CPU market without touching user activity.

    The short-lived 400-card implementation wrote those listings to the
    persistent database.  Merely lowering the seed target would therefore
    leave upgraded saves on the slow population forever.  Keep every watched
    or bid-on listing, retain the oldest ordinary stock up to the proven
    per-bucket targets, and remove only surplus, untouched CPU listings.
    """
    marker = "market_proven_population_restore_v1031"
    if bool(STATE.get(marker, False)):
        return active_cpu

    by_bucket: dict[str, list[dict[str, Any]]] = {name: [] for name in targets}
    for auction in active_cpu:
        item = auction.get("item_data", {}) if isinstance(auction.get("item_data"), dict) else {}
        bucket = _market_bucket(item)
        if bucket in by_bucket:
            by_bucket[bucket].append(auction)

    removed = 0
    for bucket, rows in by_bucket.items():
        rows.sort(key=lambda row: _int_or(row.get("trade_id"), 0))
        engaged = [
            row for row in rows
            if bool(_int_or(row.get("watched"), 0))
            or bool(str(row.get("highest_bidder", "")))
            or _int_or(row.get("current_bid"), 0) > 0
        ]
        ordinary = [row for row in rows if row not in engaged]
        ordinary_slots = max(0, targets[bucket] - len(engaged))
        for row in ordinary[ordinary_slots:]:
            if STATE.delete_auction(_int_or(row.get("trade_id"), 0)):
                removed += 1

    STATE.set(marker, True)
    if removed:
        log.info("TRANSFER MARKET restored proven population; removed %d surplus CPU listing(s)", removed)
        current_time = now_s()
        return [
            auction for auction in STATE.list_auctions("cpu")
            if str(auction.get("status")) == "active"
            and _int_or(auction.get("expires_at"), 0) > current_time
        ]
    return active_cpu


def _market_seed() -> None:
    """Maintain a persistent, varied set of local CPU auctions."""
    _market_cleanup_compare_seed_artifacts()
    current_time = now_s()
    active_cpu = [
        auction for auction in STATE.list_auctions("cpu")
        if str(auction.get("status")) == "active"
        and _int_or(auction.get("expires_at"), 0) > current_time
    ]
    pools = _market_all_pools()
    # Replenish by category so an existing 0.9.0 database containing 120 gold
    # cards is upgraded immediately with silver, bronze and manager listings.
    # Proven pre-virtual-market population from 1.0.27.  This keeps the first
    # search fast while retaining a useful mix of every quality and managers.
    targets = {"gold": 70, "special": 45, "silver": 40, "bronze": 30, "manager": 15}
    active_cpu = _market_restore_proven_population(active_cpu, targets)
    counts = {name: 0 for name in targets}
    for auction in active_cpu:
        item = auction.get("item_data", {}) if isinstance(auction.get("item_data"), dict) else {}
        bucket = _market_bucket(item)
        if bucket in counts:
            counts[bucket] += 1

    # A sold card must remain absent long enough for FIFA's hub counter and
    # search result total to visibly update.  The local traders still restock
    # the market automatically, but do so on a short timer instead of inside
    # the same request that completed the purchase.
    missing = sum(max(0, target - counts[bucket]) for bucket, target in targets.items())
    next_restock_at = _int_or(STATE.get("market_next_restock_at", 0), 0)
    if active_cpu and missing:
        if next_restock_at <= 0:
            STATE.set("market_next_restock_at", current_time + _MARKET_RESTOCK_SECONDS)
            return
        if current_time < next_restock_at:
            return
    rng = random.SystemRandom()
    added = 0
    for bucket, target in targets.items():
        pool = pools.get(bucket, [])
        if not pool:
            continue
        for _ in range(max(0, target - counts[bucket])):
            resource_id = int(rng.choice(pool))
            item_kind = "manager" if bucket == "manager" else "player"
            recommended = _market_recommended_price(resource_id, item_kind)
            buy_now = _market_round_price(int(recommended * rng.uniform(0.92, 1.10)))
            starting = _market_round_price(int(buy_now * rng.uniform(0.82, 0.94)))
            if starting >= buy_now:
                starting = max(150, buy_now - _market_bid_increment(buy_now))
            duration = rng.choice((600, 900, 1800, 3600, 7200, 21600))
            virtual_item_id = STATE.next_item_id()
            if item_kind == "manager":
                item = STATE.build_manager_item(resource_id, virtual_item_id, "trade")
            else:
                item = STATE.build_player_item(resource_id, virtual_item_id, "trade")
            item["itemState"] = "forSale"
            item["marketDataMinPrice"] = 150
            item["marketDataMaxPrice"] = min(
                15_000_000, max(5_000, _market_round_price(int(recommended * 2.5)))
            )
            STATE.create_auction(
                resource_id=resource_id,
                item_data=item,
                seller_type="cpu",
                seller_name=rng.choice(_MARKET_SELLER_NAMES),
                starting_bid=starting,
                buy_now_price=buy_now,
                duration=duration,
            )
            added += 1
    if added:
        log.info("TRANSFER MARKET replenished %d mixed CPU listings", added)
    STATE.set("market_next_restock_at", current_time + _MARKET_RESTOCK_SECONDS)


def _market_delay_restock() -> None:
    """Start a fresh restock window after a user completes a purchase."""
    STATE.set("market_next_restock_at", now_s() + _MARKET_RESTOCK_SECONDS)


def _market_deliver(auction: dict[str, Any], sale_price: int) -> dict[str, Any] | None:
    if _int_or(auction.get("item_delivered"), 0):
        data = auction.get("item_data", {})
        delivered_id = _int_or(data.get("_deliveredItemId"), 0) if isinstance(data, dict) else 0
        return STATE.get_item(delivered_id) if delivered_id else None
    auction_data = dict(auction.get("item_data", {}))
    resource_id = _int_or(auction.get("resource_id"), 0)

    # The in-squad Transfer Market keeps the selected auction card alive while
    # showing its Buyout Successful actions. It sends that card's instance id
    # to /item instead of refreshing /purchased/items first. CPU listings
    # already reserve unique item ids, so persist the purchase with that id.
    # Creating a second id here made both post-buy actions address a card the
    # user did not own, after which FIFA tore down the FUT session.
    auction_item_id = _int_or(auction_data.get("id"), 0)
    if auction_item_id > 0 and STATE.get_item(auction_item_id) is None:
        if _market_item_kind(auction_data) == "manager":
            item = STATE.build_manager_item(resource_id, auction_item_id, "unassigned")
        else:
            item = STATE.build_player_item(resource_id, auction_item_id, "unassigned")
        with STATE.lock:
            STATE.conn.execute(
                "INSERT INTO items(id,resource_id,pile,data) VALUES(?,?,?,?)",
                (auction_item_id, resource_id, "unassigned", json.dumps(item)),
            )
            STATE.conn.commit()
    elif _market_item_kind(auction_data) == "manager":
        item = STATE.make_manager_item(resource_id, "unassigned")
    else:
        item = STATE.make_player_item(resource_id, "unassigned")
    item = STATE.update_item_fields(int(item["id"]), {
        "lastSalePrice": int(sale_price), "owners": 2, "itemState": "free", "pile": "unassigned",
    }) or item
    auction_data["_deliveredItemId"] = int(item["id"])
    auction_data["lastSalePrice"] = int(sale_price)
    STATE.update_auction(
        int(auction["trade_id"]), item_delivered=1, item_data=auction_data,
    )
    return item


def _market_finalize() -> None:
    current_time = now_s()
    for auction in STATE.list_auctions():
        if str(auction.get("status")) != "active":
            continue
        seller_type = str(auction.get("seller_type", "cpu"))
        item_data = auction.get("item_data", {}) if isinstance(auction.get("item_data"), dict) else {}
        cpu_buy_at = _int_or(item_data.get("_cpuBuyAt"), 0)
        if seller_type == "user" and cpu_buy_at and current_time >= cpu_buy_at:
            sold = STATE.update_auction(
                int(auction["trade_id"]), status="sold", highest_bidder="cpu",
                current_bid=int(auction["buy_now_price"]),
            )
            _market_credit_user_sale(sold or auction)
            log.info("TRANSFER MARKET bot bought trade=%s buyNow=%s",
                     auction["trade_id"], auction["buy_now_price"])
            continue
        cpu_action_at = _int_or(item_data.get("_cpuActionAt"), 0)
        if seller_type == "user" and cpu_action_at and current_time >= cpu_action_at:
            max_bid = _int_or(item_data.get("_cpuMaxBid"), 0)
            current_bid = _int_or(auction.get("current_bid"), 0)
            next_bid = (_int_or(auction.get("starting_bid"), 150) if current_bid <= 0
                        else current_bid + _market_bid_increment(current_bid))
            if next_bid <= max_bid:
                item_data["_cpuBidCount"] = _int_or(item_data.get("_cpuBidCount"), 0) + 1
                following = next_bid + _market_bid_increment(next_bid)
                if following <= max_bid:
                    item_data["_cpuActionAt"] = current_time + 18 + (int(auction["trade_id"]) % 29)
                else:
                    item_data.pop("_cpuActionAt", None)
                STATE.update_auction(
                    int(auction["trade_id"]), current_bid=next_bid,
                    highest_bidder="cpu", item_data=item_data,
                )
                log.info("TRANSFER MARKET bot bid trade=%s amount=%s ceiling=%s",
                         auction["trade_id"], next_bid, max_bid)
                auction = STATE.get_auction(int(auction["trade_id"])) or auction
        if _int_or(auction.get("expires_at"), 0) > current_time:
            continue
        if seller_type == "cpu" and str(auction.get("highest_bidder")) == "user":
            price = max(_int_or(auction.get("current_bid"), 0), _int_or(auction.get("starting_bid"), 0))
            delivered = _market_deliver(auction, price)
            STATE.update_auction(int(auction["trade_id"]), status="won")
            log.info("TRANSFER MARKET won bid trade=%s price=%s item=%s",
                     auction["trade_id"], price, _int_or((delivered or {}).get("id"), 0))
        elif seller_type == "user" and str(auction.get("highest_bidder")) == "cpu":
            sold = STATE.update_auction(int(auction["trade_id"]), status="sold")
            _market_credit_user_sale(sold or auction)
            log.info("TRANSFER MARKET bot won user listing trade=%s price=%s",
                     auction["trade_id"], auction.get("current_bid"))
        else:
            STATE.update_auction(int(auction["trade_id"]), status="expired")
            if seller_type == "cpu" and not _int_or(auction.get("watched"), 0):
                STATE.delete_auction(int(auction["trade_id"]))


def _market_credit_user_sale(auction: dict[str, Any]) -> int:
    """Credit one completed user sale immediately and exactly once."""
    trade_id = _int_or(auction.get("trade_id", auction.get("tradeId")), 0)
    if trade_id <= 0:
        return 0
    with STATE.lock:
        current = STATE.get_auction(trade_id)
        if not current:
            return 0
        if (str(current.get("seller_type", "")) != "user"
                or str(current.get("status", "")) != "sold"
                or _int_or(current.get("coins_claimed"), 0)):
            return 0
        gross = max(
            _int_or(current.get("current_bid"), 0),
            _int_or(current.get("starting_bid"), 0),
        )
        # FIFA Ultimate Team charges a five-percent transfer-market tax.
        net = max(0, gross - (gross * 5 // 100))
        STATE.update_auction(trade_id, coins_claimed=1)
        balance = STATE.add_credits(net)
    log.info(
        "TRANSFER MARKET credited sale trade=%s gross=%s tax=%s net=%s balance=%s",
        trade_id, gross, gross - net, net, balance,
    )
    return net


def _market_auction_info(auction: dict[str, Any]) -> dict[str, Any]:
    item = dict(auction.get("item_data", {}))
    delivered_id = _int_or(item.get("_deliveredItemId"), 0)
    if delivered_id:
        item = STATE.get_item(delivered_id) or item
    elif str(auction.get("seller_type")) == "user" and _int_or(auction.get("item_id"), 0):
        item = STATE.get_item(_int_or(auction.get("item_id"), 0)) or item
    native_item = _market_native_item(item)
    status = str(auction.get("status", "active"))
    highest = str(auction.get("highest_bidder", ""))
    return {
        "buyNowPrice": _int_or(auction.get("buy_now_price"), 0),
        "currentBid": _int_or(auction.get("current_bid"), 0),
        "expires": max(0, _int_or(auction.get("expires_at"), 0) - now_s()),
        "itemData": native_item,
        "offers": 1 if _int_or(auction.get("current_bid"), 0) > 0 else 0,
        "sellerEstablished": _int_or(STATE.get("established", now_s()), now_s()),
        "sellerId": int(CFG["persona_id"]) if str(auction.get("seller_type")) == "user" else 0,
        "sellerName": str(auction.get("seller_name", "FUT Trader")),
        "startingBid": _int_or(auction.get("starting_bid"), 150),
        "tradeId": _int_or(auction.get("trade_id"), 0),
        "tradeState": "active" if status == "active" else "closed",
        "bidState": "highest" if highest == "user" else "none",
        "watched": bool(_int_or(auction.get("watched"), 0)),
        "tradeOwner": str(auction.get("seller_type")) == "user",
    }


def _market_first(query: dict[str, list[str]], *names: str, default: str = "") -> str:
    lowered = {str(key).lower(): values for key, values in query.items()}
    for name in names:
        values = lowered.get(name.lower(), [])
        if values:
            return str(values[0])
    return default


def _market_search(query: dict[str, list[str]]) -> dict[str, Any]:
    _market_finalize()
    _market_seed()
    requested_type = _market_first(query, "type", default="player").lower()
    if requested_type in ("manager", "staff", "2"):
        wanted_kind = "manager"
    elif requested_type in ("", "player", "1"):
        wanted_kind = "player"
    else:
        return {"auctionInfo": [], "total": 0, "count": 0, "credits": STATE.credits()}

    position = _market_first(query, "pos", "position").upper()
    level = _market_first(query, "lev", "level").lower()
    # Compare Price uses definitionId and means the exact card version.  A
    # normal named-player search uses maskedDefId and means the base footballer.
    definition_id = _int_or(
        _market_first(query, "definitionId", "resourceId", "selectedResourceId"), 0
    )
    masked_def_id = _int_or(_market_first(query, "maskedDefId", "defId", "assetId"), 0)
    nation = _int_or(_market_first(query, "nat", "nation"), 0)
    league = _int_or(_market_first(query, "leag", "league"), 0)
    team = _int_or(_market_first(query, "team", "club"), 0)
    min_bid = _int_or(_market_first(query, "minb", "minimumBid"), 0)
    max_bid = _int_or(_market_first(query, "maxb", "maximumBid"), 0)
    min_buy = _int_or(_market_first(query, "micr", "minBuy", "minimumBuyNowPrice"), 0)
    max_buy = _int_or(_market_first(query, "macr", "maxBuy", "maximumBuyNowPrice"), 0)
    min_rating = _int_or(_market_first(query, "minRating", "minr"), 0)
    max_rating = _int_or(_market_first(query, "maxRating", "maxr"), 0)
    rarity = _market_first(query, "rarity", "rare").lower()

    matches: list[dict[str, Any]] = []
    for auction in STATE.list_auctions():
        # The public Transfer Market never returns the current user's own
        # listings. Those belong exclusively to the Transfer List endpoint.
        if str(auction.get("seller_type", "cpu")) == "user":
            continue
        if str(auction.get("status")) != "active":
            continue
        item = auction.get("item_data", {}) if isinstance(auction.get("item_data"), dict) else {}
        if _market_item_kind(item) != wanted_kind:
            continue
        rating = _int_or(item.get("rating"), 0)
        rareflag = _int_or(item.get("rareflag"), 0)
        meta = (manager_meta(_int_or(auction.get("resource_id"), 0))
                if wanted_kind == "manager"
                else player_meta(_int_or(auction.get("resource_id"), 0)))
        is_special = wanted_kind == "player" and (bool(meta.get("special", False)) or rareflag > 1)
        if min_rating and rating < min_rating:
            continue
        if max_rating and rating > max_rating:
            continue
        if rarity in ("special", "sp", "true") and not is_special:
            continue
        if rarity in ("rare", "1") and rareflag != 1:
            continue
        if rarity in ("common", "nonrare", "0") and rareflag != 0:
            continue
        if rarity.isdigit() and rarity not in ("0", "1") and rareflag != int(rarity):
            continue
        if wanted_kind == "player" and position and str(item.get("preferredPosition", "")).upper() != position:
            continue
        if level == "gold" and (rating < 75 or is_special):
            continue
        if level == "special" and not is_special:
            continue
        if level == "silver" and not (65 <= rating <= 74):
            continue
        if level == "bronze" and rating > 64:
            continue
        if definition_id and definition_id not in (
                _int_or(item.get("definitionId"), 0),
                _int_or(item.get("resourceId"), 0)):
            continue
        if masked_def_id and masked_def_id not in (
                _int_or(item.get("assetId"), 0), _int_or(item.get("resourceId"), 0)):
            continue
        if nation and _int_or(item.get("nation"), 0) != nation:
            continue
        if league and _int_or(item.get("leagueId"), 0) != league:
            continue
        if team and _int_or(item.get("teamid"), 0) != team:
            continue
        starting = _int_or(auction.get("starting_bid"), 0)
        buy_now = _int_or(auction.get("buy_now_price"), 0)
        if min_bid and starting < min_bid:
            continue
        if max_bid and starting > max_bid:
            continue
        if min_buy and buy_now < min_buy:
            continue
        if max_buy and buy_now > max_buy:
            continue
        matches.append(auction)

    matches.sort(key=lambda row: (
        _int_or(row.get("buy_now_price"), 0), _int_or(row.get("expires_at"), 0),
        _int_or(row.get("trade_id"), 0),
    ))
    total = len(matches)
    start = max(0, _int_or(_market_first(query, "start"), 0))
    count = max(1, min(50, _int_or(_market_first(query, "num", "count"), 16)))
    page = matches[start:start + count]
    return {
        "auctionInfo": [_market_auction_info(row) for row in page],
        "total": total,
        "count": len(page),
        "credits": STATE.credits(),
    }


def _market_price_limits(def_id: int) -> list[dict[str, Any]]:
    """Return the raw array consumed by FIFA's MarketDataDelegate."""
    resource_id = int(def_id)
    item_type = "manager" if resource_id in MANAGER_DB else "player"
    if item_type == "player" and resource_id not in PLAYER_DB:
        # maskedDefId is usually the base asset id. Prefer the matching base
        # resource where possible, but safe defaults still let the dialog open.
        candidates = [rid for rid, meta in PLAYER_DB.items()
                      if _int_or(meta.get("assetId"), 0) == resource_id]
        if candidates:
            resource_id = int(candidates[0])
    minimum, maximum = _market_price_bounds(resource_id, item_type)
    row = {
        "itemId": int(def_id),
        "defId": int(def_id),
        "minPrice": minimum,
        "maxPrice": maximum,
    }
    return [row]


def _market_item_price_limits(item_ids: list[int]) -> list[dict[str, Any]]:
    """Price-range form used when FIFA asks about owned item instance IDs."""
    rows: list[dict[str, Any]] = []
    for item_id in item_ids:
        item = STATE.get_item(int(item_id))
        if not item:
            continue
        resource_id = _int_or(item.get("resourceId", item.get("assetId", 0)), 0)
        minimum, maximum = _market_price_bounds(resource_id, _market_item_kind(item))
        rows.append({"itemId": int(item_id), "minPrice": minimum, "maxPrice": maximum})
    return rows


def _market_inactive_trade_info(item: dict[str, Any]) -> dict[str, Any]:
    """Wrap an unlisted transfer-pile card in FIFA's native auction envelope."""
    return {
        "buyNowPrice": 0,
        "currentBid": 0,
        "expires": 0,
        "itemData": _market_native_item(item),
        "offers": 0,
        "sellerEstablished": _int_or(STATE.get("established", now_s()), now_s()),
        "sellerId": int(CFG["persona_id"]),
        "sellerName": str(CFG["club_name"]),
        "startingBid": 0,
        # Zero is intentional: the client treats this as an owned item that has
        # not yet become an auction and therefore enables List on Transfer Market.
        "tradeId": 0,
        "tradeState": "inactive",
        "bidState": "none",
        "watched": False,
        "tradeOwner": True,
    }


def _market_tradepile() -> dict[str, Any]:
    _market_finalize()
    auctions = STATE.list_auctions("user")
    info = [_market_auction_info(row) for row in auctions]
    auction_item_ids = {
        _int_or(row.get("item_id"), 0) for row in auctions
        if _int_or(row.get("item_id"), 0) > 0
    }
    pending = [
        item for item in STATE.list_items("trade")
        if _int_or(item.get("id"), 0) not in auction_item_ids
    ]
    pending_info = [_market_inactive_trade_info(item) for item in pending]
    selling = sum(1 for row in auctions if str(row.get("status")) == "active")
    sold = sum(1 for row in auctions if str(row.get("status")) == "sold")
    info.extend(pending_info)
    total = len(info)
    return {
        "auctionInfo": info, "credits": STATE.credits(), "total": total,
        "count": total, "maxAuctionsAllowed": 100, "offered": 0,
        "selling": selling, "sold": sold, "itemData": [],
    }


def _market_watchlist() -> dict[str, Any]:
    _market_finalize()
    auctions = [
        row for row in STATE.list_auctions("cpu")
        if _int_or(row.get("watched"), 0) or str(row.get("highest_bidder")) == "user"
    ]
    info = [_market_auction_info(row) for row in auctions]
    return {
        "auctionInfo": info, "credits": STATE.credits(), "total": len(info),
        "count": len(info), "maxWatchListSize": 50,
    }


def _market_trade_status(trade_ids: list[int], include_bank_info: bool = False) -> dict[str, Any]:
    """Refresh selected auctions without making FIFA leave Ultimate Team."""
    _market_finalize()
    info: list[dict[str, Any]] = []
    for trade_id in trade_ids:
        auction = STATE.get_auction(int(trade_id))
        if auction:
            info.append(_market_auction_info(auction))
    response: dict[str, Any] = {"auctionInfo": info}
    if include_bank_info:
        response.update(_credits_payload())
        response["credits"] = STATE.credits()
    return response


def _market_payload_item_id(payload: dict[str, Any]) -> int:
    item_data = payload.get("itemData", {}) if isinstance(payload, dict) else {}
    if isinstance(item_data, list):
        item_data = item_data[0] if item_data else {}
    return _int_or(
        (item_data.get("id") or item_data.get("itemId")) if isinstance(item_data, dict) else 0,
        _int_or(payload.get("itemId") or payload.get("id"), 0) if isinstance(payload, dict) else 0,
    )


def _market_create_listing(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    item_id = _market_payload_item_id(payload)
    item = STATE.get_item(item_id)
    if not item or _market_item_kind(item) not in ("player", "manager"):
        return 404, {"reason": "ITEM_NOT_FOUND", "credits": STATE.credits()}
    if bool(item.get("untradeable", False)):
        return 409, {"reason": "ITEM_UNTRADEABLE", "credits": STATE.credits()}
    if any(_int_or(row.get("item_id"), 0) == item_id and str(row.get("status")) == "active"
           for row in STATE.list_auctions("user")):
        return 409, {"reason": "ITEM_ALREADY_LISTED", "credits": STATE.credits()}

    starting_bid = _market_round_price(_int_or(payload.get("startingBid"), 150))
    buy_now = _market_round_price(_int_or(payload.get("buyNowPrice"), starting_bid + 50))
    if buy_now <= starting_bid:
        buy_now = _market_round_price(starting_bid + _market_bid_increment(starting_bid))
    duration = _int_or(payload.get("duration"), 3600)
    if duration not in (3600, 10800, 21600, 43200, 86400, 259200):
        duration = max(60, min(259200, duration))

    resource_id = _int_or(item.get("resourceId", item.get("assetId", 0)), 0)
    recommended = _market_recommended_price(resource_id, _market_item_kind(item))
    listed_item = dict(item)
    listed_item.update({
        "pile": "trade", "itemState": "forSale",
        "marketDataMinPrice": max(150, _market_round_price(int(recommended * 0.45))),
        "marketDataMaxPrice": min(15_000_000, _market_round_price(int(recommended * 2.5))),
    })
    # Local traders use a stable card-specific budget. Bargain Buy Now cards
    # sell quickly; normally priced cards collect visible bot bids and sell at
    # expiry; overpriced cards can expire unsold, just like the original game.
    bot_budget = _market_round_price(int(recommended * (0.96 + (item_id % 10) / 100.0)))
    action_at = now_s() + 12 + (item_id % 29)
    if buy_now <= bot_budget:
        listed_item["_cpuBuyAt"] = action_at
    elif starting_bid <= bot_budget:
        listed_item["_cpuActionAt"] = action_at
        listed_item["_cpuMaxBid"] = bot_budget
    STATE.detach_item_from_squads(item_id)
    STATE.update_item_fields(item_id, {"pile": "trade", "itemState": "forSale"})
    auction = STATE.create_auction(
        resource_id=resource_id,
        item_data=listed_item,
        seller_type="user",
        seller_name=str(CFG["club_name"]),
        starting_bid=starting_bid,
        buy_now_price=buy_now,
        duration=duration,
        item_id=item_id,
    )
    log.info("TRANSFER MARKET listed item=%s resource=%s trade=%s start=%s buyNow=%s duration=%s",
             item_id, resource_id, auction["trade_id"], starting_bid, buy_now, duration)
    return 200, {
        "auctionInfo": [_market_auction_info(auction)],
        "credits": STATE.credits(), "id": int(auction["trade_id"]),
    }


def _market_place_bid(trade_id: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    _market_finalize()
    auction = STATE.get_auction(trade_id)
    if not auction:
        return 404, {"reason": "AUCTION_NOT_FOUND", "credits": STATE.credits()}
    if str(auction.get("seller_type")) == "user":
        return 409, {"reason": "CANNOT_BID_ON_OWN_AUCTION", "credits": STATE.credits()}
    if str(auction.get("status")) != "active" or _int_or(auction.get("expires_at"), 0) <= now_s():
        return 409, {"reason": "AUCTION_EXPIRED", "credits": STATE.credits()}

    offered = _int_or(payload.get("bid") or payload.get("amount") or payload.get("currentBid"), 0)
    current_bid = _int_or(auction.get("current_bid"), 0)
    starting_bid = _int_or(auction.get("starting_bid"), 150)
    buy_now = _int_or(auction.get("buy_now_price"), 0)
    minimum = starting_bid if current_bid <= 0 else current_bid + _market_bid_increment(current_bid)
    is_buy_now = buy_now > 0 and offered >= buy_now
    target = buy_now if is_buy_now else offered
    if target < minimum:
        return 409, {
            "reason": "BID_TOO_LOW", "minimumBid": minimum, "credits": STATE.credits(),
            "auctionInfo": [_market_auction_info(auction)],
        }
    previous_escrow = current_bid if str(auction.get("highest_bidder")) == "user" else 0
    debit = max(0, target - previous_escrow)
    if STATE.credits() < debit:
        return 409, {
            "reason": "INSUFFICIENT_CREDITS", "credits": STATE.credits(),
            "auctionInfo": [_market_auction_info(auction)],
        }
    STATE.add_credits(-debit)
    if is_buy_now:
        STATE.update_auction(
            trade_id, current_bid=buy_now, highest_bidder="user", status="bought", watched=1,
        )
        auction = STATE.get_auction(trade_id) or auction
        delivered = _market_deliver(auction, buy_now)
        auction = STATE.get_auction(trade_id) or auction
        _market_delay_restock()
        log.info("TRANSFER MARKET buy-now trade=%s price=%s deliveredItem=%s balance=%s",
                 trade_id, buy_now, _int_or((delivered or {}).get("id"), 0), STATE.credits())
    else:
        STATE.update_auction(
            trade_id, current_bid=target, highest_bidder="user", watched=1,
        )
        auction = STATE.get_auction(trade_id) or auction
        log.info("TRANSFER MARKET bid trade=%s amount=%s balance=%s", trade_id, target, STATE.credits())
    return 200, {
        "auctionInfo": [_market_auction_info(auction)],
        "credits": STATE.credits(),
        "currencies": [{"name": "coins", "funds": STATE.credits(), "finalFunds": STATE.credits()}],
    }


def _market_clear_trade(trade_id: int) -> tuple[int, dict[str, Any]]:
    _market_finalize()
    auction = STATE.get_auction(trade_id)
    if not auction:
        # FIFA 20's bulk "Clear Sold" request sometimes sends the owned item
        # id instead of the trade id. Resolve that alternate identity before
        # acknowledging the deletion, otherwise the auction row resurrects on
        # the next Transfer List refresh.
        auction = next((
            row for row in STATE.list_auctions("user")
            if _int_or(row.get("item_id"), 0) == int(trade_id)
            and str(row.get("status", "active")) in ("sold", "expired")
        ), None)
    if not auction:
        return 200, {"credits": STATE.credits(), "success": True}
    trade_id = _int_or(auction.get("trade_id"), trade_id)
    status = str(auction.get("status", "active"))
    seller_type = str(auction.get("seller_type", "cpu"))
    if status == "active":
        return 409, {"reason": "AUCTION_STILL_ACTIVE", "credits": STATE.credits()}
    if seller_type == "user":
        item_id = _int_or(auction.get("item_id"), 0)
        if status == "sold":
            # Backward-compatible claim for sales created by older builds.
            _market_credit_user_sale(auction)
            STATE.delete_item(item_id)
        else:
            STATE.update_item_pile(item_id, "club")
            STATE.update_item_fields(item_id, {"itemState": "free", "pile": "club"})
    STATE.delete_auction(trade_id)
    return 200, {
        "credits": STATE.credits(), "success": True,
        "currencies": [{"name": "coins", "funds": STATE.credits(), "finalFunds": STATE.credits()}],
    }


def _club_player_search(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return FIFA 20's native /club player or manager search envelope."""
    def first(name: str, default: str = "") -> str:
        values = query.get(name, [])
        return str(values[0]) if values else default

    position_groups = {
        "DEFENSE": {"LB", "LWB", "CB", "RB", "RWB"},
        "DEFENDER": {"LB", "LWB", "CB", "RB", "RWB"},
        "MIDFIELD": {"LM", "CDM", "CM", "CAM", "RM"},
        "MIDFIELDER": {"LM", "CDM", "CM", "CAM", "RM"},
        "ATTACK": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "ATTACKER": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "FORWARD": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "GOALKEEPER": {"GK"},
    }
    type_position_groups = {
        "playerdefender": position_groups["DEFENDER"],
        "playermidfielder": position_groups["MIDFIELDER"],
        "playerforward": position_groups["FORWARD"],
        "playerattacker": position_groups["ATTACKER"],
        "playergoalkeeper": position_groups["GOALKEEPER"],
    }

    requested_type = first("type", "player").lower()
    implicit_positions = type_position_groups.get(requested_type)
    club_type_aliases = {
        "kit": {"kit"}, "kits": {"kit"},
        "badge": {"custom"}, "custom": {"custom"}, "clubinfo": {"custom"},
        "stadium": {"stadium"}, "stadiums": {"stadium"},
        "ball": {"ball"}, "balls": {"ball"},
        "3": {"kit", "custom", "stadium", "ball"},
    }
    club_item_types = club_type_aliases.get(requested_type)
    if requested_type in ("manager", "staff", "2"):
        item_kind = "manager"
    elif requested_type in ("player", "1", "") or implicit_positions is not None:
        item_kind = "player"
    elif club_item_types is not None:
        _ensure_persistent_club_items()
        owned_by_id: dict[int, dict[str, Any]] = {}
        for raw in (*STATE.list_items("club"), *STATE.list_items("squad")):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("itemType", "")).lower() not in club_item_types:
                continue
            item_id = _int_or(raw.get("id"), 0)
            if item_id > 0:
                owned_by_id[item_id] = raw
        items = sorted(
            owned_by_id.values(),
            key=lambda item: (_int_or(item.get("rating"), 0), _int_or(item.get("id"), 0)),
            reverse=True,
        )
        start = max(0, _int_or(first("start"), 0))
        count = max(1, min(100, _int_or(first("count"), 50)))
        page = [dict(item) for item in items[start:start + count]]
        return {"itemData": page, "itemCount": len(items)}
    else:
        return {"itemData": [], "itemCount": 0}

    # Active-squad references still belong to the user's club. Include both
    # local inventory piles and de-duplicate by item-instance ID.
    owned_by_id: dict[int, dict[str, Any]] = {}
    for raw in [*STATE.list_items("club"), *STATE.list_items("squad")]:
        if not isinstance(raw, dict) or str(raw.get("itemType", "player")).lower() != item_kind:
            continue
        item_id = _int_or(raw.get("id"), 0)
        if item_id > 0:
            owned_by_id[item_id] = raw
    items = list(owned_by_id.values())

    position = first("position", "any").upper()
    if item_kind == "player" and position not in ("", "ANY", "0"):
        accepted_positions = position_groups.get(position, {position})
        items = [
            item for item in items
            if str(item.get("preferredPosition", "")).upper() in accepted_positions
        ]
    elif item_kind == "player" and implicit_positions is not None:
        items = [
            item for item in items
            if str(item.get("preferredPosition", "")).upper() in implicit_positions
        ]

    level = first("level", "any").lower()
    if level in ("bronze", "1"):
        items = [
            item for item in items
            if _int_or(item.get("rating"), 0) <= 64
            and _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) <= 1
        ]
    elif level in ("silver", "2"):
        items = [
            item for item in items
            if 65 <= _int_or(item.get("rating"), 0) <= 74
            and _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) <= 1
        ]
    elif level in ("gold", "3"):
        items = [
            item for item in items
            if _int_or(item.get("rating"), 0) >= 75
            and _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) <= 1
        ]

    numeric_filters = (
        (("league", "leagueId"), "leagueId"),
        (("club", "team", "teamid"), "teamid"),
        (("nation", "nationality"), "nation"),
    )
    for aliases, field in numeric_filters:
        requested = 0
        for alias in aliases:
            requested = _int_or(first(alias), 0)
            if requested > 0:
                break
        if requested > 0:
            items = [item for item in items if _int_or(item.get(field), 0) == requested]

    definition_id = _int_or(first("defId") or first("assetId"), 0)
    if definition_id:
        items = [
            item for item in items
            if _int_or(item.get("assetId", item.get("resourceId")), 0) == definition_id
        ]

    minimum_rating = _int_or(first("min-ovr") or first("minRating"), 0)
    maximum_rating = _int_or(first("max-ovr") or first("maxRating"), 0)
    if minimum_rating:
        items = [item for item in items if _int_or(item.get("rating"), 0) >= minimum_rating]
    if maximum_rating:
        items = [item for item in items if _int_or(item.get("rating"), 0) <= maximum_rating]

    rare = first("rare", "").lower()
    if rare in ("sp", "special"):
        items = [
            item for item in items
            if _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) > 1
        ]
    elif rare in ("1", "true", "rare"):
        items = [
            item for item in items
            if _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) == 1
        ]
    elif rare in ("0", "false", "common", "nonrare"):
        items = [
            item for item in items
            if _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) == 0
        ]

    loan = first("loan", "").lower()
    if loan in ("1", "true"):
        items = [item for item in items if _int_or(item.get("loans"), 0) > 0]
    elif loan in ("0", "false"):
        items = [item for item in items if _int_or(item.get("loans"), 0) == 0]

    excluded_definitions: set[int] = set()
    for encoded in query.get("excldef", []):
        for value in str(encoded).split(","):
            parsed = _int_or(value, 0)
            if parsed > 0:
                excluded_definitions.add(parsed)
    if excluded_definitions:
        items = [
            item for item in items
            if not {
                _int_or(item.get("assetId"), 0),
                _int_or(item.get("resourceId"), 0),
            }.intersection(excluded_definitions)
        ]

    excluded_instances: set[int] = set()
    for encoded in [*query.get("exclid", []), *query.get("exclude", [])]:
        for value in str(encoded).split(","):
            parsed = _int_or(value, 0)
            if parsed > 0:
                excluded_instances.add(parsed)
    if excluded_instances:
        items = [item for item in items if _int_or(item.get("id"), 0) not in excluded_instances]

    descending = first("sort", "desc").lower() != "asc"
    sort_by = first("sortBy", "ovr").lower()
    if sort_by in ("ovr", "rating"):
        items.sort(
            key=lambda item: (_int_or(item.get("rating"), 0), _int_or(item.get("id"), 0)),
            reverse=descending,
        )
    else:
        items.sort(key=lambda item: _int_or(item.get("id"), 0), reverse=descending)

    total = len(items)
    start = max(0, _int_or(first("start"), 0))
    count = max(1, min(100, _int_or(first("count"), 50)))
    normalizer = _native_manager_item if item_kind == "manager" else _native_player_item
    page = [normalizer(item) for item in items[start:start + count]]
    log.info(
        "CLUB ITEM SEARCH type=%s position=%s level=%s start=%d count=%d matched=%d returned=%d",
        item_kind, position or "ANY", level or "any", start, count, total, len(page),
    )
    return {"itemData": page, "itemCount": total}


def _dream_player_search(query: dict[str, list[str]]) -> dict[str, Any]:
    """Search the definition catalog used by FIFA's Concept Player picker."""
    def first(*names: str, default: str = "") -> str:
        for name in names:
            values = query.get(name, [])
            if values:
                return str(values[0])
        return default

    resources = [
        resource_id for resource_id, meta in PLAYER_DB.items()
        if _player_is_pack_safe(meta)
    ]
    definition_id = _int_or(first("defId", "definitionId", "maskedDefId"), 0)
    if definition_id > 0:
        resources = [
            resource_id for resource_id in resources
            if resource_id == definition_id
            or _int_or(PLAYER_DB[resource_id].get("assetId", resource_id), resource_id) == definition_id
        ]

    position_groups = {
        "DEFENSE": {"LB", "LWB", "CB", "RB", "RWB"},
        "DEFENDER": {"LB", "LWB", "CB", "RB", "RWB"},
        "MIDFIELD": {"LM", "CDM", "CM", "CAM", "RM"},
        "MIDFIELDER": {"LM", "CDM", "CM", "CAM", "RM"},
        "ATTACK": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "ATTACKER": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "FORWARD": {"LW", "LF", "CF", "ST", "RF", "RW"},
        "GOALKEEPER": {"GK"},
    }
    position = first("position", "pos", "zone", default="any").upper()
    if position not in ("", "ANY", "0", "-1"):
        accepted = position_groups.get(position, {position})
        resources = [
            resource_id for resource_id in resources
            if str(PLAYER_DB[resource_id].get("preferredPosition", "")).upper() in accepted
        ]

    level = first("level", "lev", default="any").lower()
    if level in ("bronze", "1"):
        resources = [resource_id for resource_id in resources
                     if _int_or(PLAYER_DB[resource_id].get("rating"), 0) <= 64]
    elif level in ("silver", "2"):
        resources = [resource_id for resource_id in resources
                     if 65 <= _int_or(PLAYER_DB[resource_id].get("rating"), 0) <= 74]
    elif level in ("gold", "3"):
        resources = [resource_id for resource_id in resources
                     if _int_or(PLAYER_DB[resource_id].get("rating"), 0) >= 75]

    for names, field in (
        (("nation", "nat"), "nation"),
        (("league", "leag"), "leagueId"),
        (("club", "team"), "teamid"),
    ):
        requested = _int_or(first(*names), 0)
        if requested > 0:
            resources = [resource_id for resource_id in resources
                         if _int_or(PLAYER_DB[resource_id].get(field), 0) == requested]

    rare = first("rare", default="").lower()
    if rare in ("sp", "special"):
        resources = [resource_id for resource_id in resources
                     if _int_or(PLAYER_DB[resource_id].get("rareflag"), 0) > 1]

    resources.sort(
        key=lambda resource_id: (
            _int_or(PLAYER_DB[resource_id].get("rating"), 0), int(resource_id)
        ),
        reverse=True,
    )
    start = max(0, _int_or(first("start"), 0))
    count = max(1, min(100, _int_or(first("count", "num"), 50)))
    items: list[dict[str, Any]] = []
    for resource_id in resources[start:start + count]:
        # A zero instance id makes FIFA's collection model treat every card on
        # the page as the same object and repaint the grid with one face.
        # These stable IDs are not owned and stay below the real 900bn sequence.
        concept_id = 800_000_000_000 + int(resource_id)
        item = _native_player_item(
            STATE.build_player_item(resource_id, concept_id, "none")
        )
        item["dream"] = True
        item["concept"] = True
        item["conceptResourceId"] = int(resource_id)
        item["untradeable"] = True
        items.append(item)
    log.info(
        "CONCEPT SEARCH definition=%d position=%s level=%s matched=%d returned=%d",
        definition_id, position, level, len(resources), len(items),
    )
    return {"itemData": items}


def _native_squad_summary(saved: dict[str, Any]) -> dict[str, Any]:
    squad_type = str(saved.get("squadType") or "REGULAR_SQUAD").upper()
    if squad_type not in ("REGULAR_SQUAD", "DREAM_SQUAD", "DRAFT_SQUAD"):
        squad_type = "REGULAR_SQUAD"
    sid = max(1, _int_or(saved.get("id") or saved.get("squadId"), 1))
    return {
        "id": sid,
        "squadName": str(saved.get("squadName") or saved.get("name") or f"Squad {sid}"),
        "rating": _clamp_int(saved.get("rating", saved.get("starRating", 0)), 0, 99, 0),
        "chemistry": _clamp_int(saved.get("chemistry", 0), 0, 100, 0),
        "formation": str(saved.get("formation") or "f442"),
        "squadType": squad_type,
    }


def _native_squad(squad_id: int | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    squads = STATE.list_squads()
    if isinstance(source, dict):
        src = dict(source)
    elif squad_id is not None:
        found = STATE.get_squad(int(squad_id))
        src = dict(found) if isinstance(found, dict) else {
            "id": int(squad_id), "squadName": f"Squad {int(squad_id)}",
            "formation": "f442", "players": [],
        }
    else:
        src = dict(squads[0]) if squads else {
            "id": 1, "squadName": "Local XI", "formation": "f442", "players": [],
        }
    item_by_id = {_int_or(x.get("id"), 0): x for x in STATE.list_items() if isinstance(x, dict)}

    positions: dict[int, dict[str, Any]] = {}
    seen_item_ids: set[int] = set()
    raw_players = src.get("players", []) if isinstance(src.get("players"), list) else []
    for order, pos in enumerate(raw_players[:64]):
        if not isinstance(pos, dict):
            continue
        raw_item = pos.get("itemData") if isinstance(pos.get("itemData"), dict) else {}
        item_id = _int_or(pos.get("itemId") or raw_item.get("id") or pos.get("id"), 0)
        index = _int_or(pos.get("index"), order)
        if index < 0 or index > 22 or index in positions:
            continue
        item = item_by_id.get(item_id)
        is_dream = bool(
            pos.get("dream", False) or raw_item.get("dream", False)
            or _int_or(pos.get("conceptResourceId"), 0) > 0
        )
        if item and str(item.get("itemType", "player") or "player").lower() == "player":
            if item_id <= 0 or item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            native_item = _native_player_item(item)
        elif is_dream:
            resource_id = _int_or(pos.get("conceptResourceId"), 0)
            if resource_id <= 0:
                resource_id = STATE._concept_resource_id(pos, raw_item)
            if resource_id <= 0:
                continue
            native_item = _native_player_item(
                STATE.build_player_item(resource_id, 0, "none")
            )
            native_item["dream"] = True
        else:
            continue
        default_kit = index + 1 if index < 11 else 0
        positions[index] = {
            "index": index,
            "kitNumber": _clamp_int(pos.get("kitNumber"), 0, 99, default_kit),
            "itemData": native_item,
        }

    # FIFA does not represent a vacant position by omitting ``itemData``. Its
    # own Squad-tab save request sends a concrete zero-ID item for every one
    # of the 23 positions. CardsDLL creates one native slot object per entry
    # and later walks the first 11 objects while the Squad screen is torn
    # down. Omitting itemData leaves a null object in that array; removing a
    # player or creating an empty squad therefore crashes on the next screen
    # transition. Echo the native placeholder shape verbatim.
    players: list[dict[str, Any]] = [
        positions.get(index, {
            "index": index,
            "kitNumber": 0,
            "itemData": {"id": 0, "dream": False},
        })
        for index in range(23)
    ]

    # Empty placeholders are structural slots, not selected players. Keep
    # rating/validity/captain calculations based only on actual or concept
    # cards after making every native slot non-null.
    starters = [
        slot["itemData"] for slot in players[:11]
        if isinstance(slot.get("itemData"), dict)
        and (
            _int_or(slot["itemData"].get("id"), 0) > 0
            or (
                bool(slot["itemData"].get("dream", False))
                and _int_or(slot["itemData"].get("resourceId"), 0) > 0
            )
        )
    ]
    sid = _int_or(src.get("id") or src.get("squadId"), 1)
    if sid <= 0:
        sid = 1
    name = str(src.get("squadName") or src.get("name") or "Local XI")
    captain = _int_or(src.get("captain"), 0)
    starter_ids = [_int_or(item.get("id"), 0) for item in starters]
    starter_ids = [item_id for item_id in starter_ids if item_id > 0]
    if captain not in starter_ids:
        captain = starter_ids[0] if starter_ids else 0

    kick_ids = starter_ids[:5]
    while len(kick_ids) < 5:
        kick_ids.append(kick_ids[-1] if kick_ids else 0)
    kicktakers = [{"index": index, "id": item_id, "dream": False} for index, item_id in enumerate(kick_ids)]

    # FIFA's match validator needs the complete manager item, not merely the
    # saved {id, dream} squad reference. Resolve it from owned inventory and
    # fall back to the best club manager for older manager-less saves.
    manager: list[dict[str, Any]] = []
    raw_manager = src.get("manager", [])
    if isinstance(raw_manager, dict):
        raw_manager = [raw_manager]
    if isinstance(raw_manager, list):
        for entry in raw_manager[:1]:
            if not isinstance(entry, dict):
                continue
            entry_data = entry.get("itemData") if isinstance(entry.get("itemData"), dict) else {}
            embedded = entry_data if entry_data else entry
            if (str(embedded.get("itemType", "")).lower() == "manager"
                    and _int_or(embedded.get("resourceId"), 0) > 0):
                manager.append(_native_manager_item(embedded))
                continue
            manager_id = _int_or(entry.get("id") or entry_data.get("id"), 0)
            owned_manager = item_by_id.get(manager_id)
            if (manager_id > 0 and isinstance(owned_manager, dict)
                    and str(owned_manager.get("itemType", "")).lower() == "manager"):
                manager.append(_native_manager_item(owned_manager))
    if not manager and str(src.get("squadType") or "REGULAR_SQUAD").upper() != "DREAM_SQUAD":
        default_manager = _native_default_manager()
        if default_manager:
            manager = [default_manager]

    formation = str(src.get("formation") or "f442").lower()
    if not re.fullmatch(r"f[0-9a-z_]{2,16}", formation):
        formation = "f442"
    rating = _clamp_int(src.get("rating", src.get("starRating", 0)), 0, 99, 0)
    star_rating = _clamp_int(src.get("starRating", rating), 0, 99, rating)
    chemistry = _clamp_int(src.get("chemistry", 100), 0, 100, 100)
    squad_type = str(src.get("squadType") or "REGULAR_SQUAD")
    if squad_type not in ("REGULAR_SQUAD", "DREAM_SQUAD", "DRAFT_SQUAD"):
        squad_type = "REGULAR_SQUAD"

    actives = _active_club_items()
    return {
        "id": sid,
        "squadId": sid,
        "personaId": int(CFG["persona_id"]),
        "name": name,
        "squadName": name,
        "formation": formation,
        "rating": rating,
        "starRating": star_rating,
        "squadType": squad_type,
        "dreamSquad": squad_type == "DREAM_SQUAD",
        "active": bool(src.get("active", squad_id is None)),
        "valid": len(starters) == 11,
        "newSquad": 0,
        "newsquad": 0,
        "captain": captain,
        "chemistry": chemistry,
        "changed": 0,
        "custom": str(src.get("custom") or "[0,0,0,0,0,0,0,0,0,0,0]"),
        "players": players,
        "actives": actives,
        "manager": manager,
        "club": actives,
        "kicktakers": kicktakers,
    }


def _native_squad_list() -> dict[str, Any]:
    """Build the complete FutSquadListServerResponse expected by CardsDLL.

    FIFA does not treat entries in ``squad`` as lightweight summaries. It
    sends every row through the same squad factory as a direct squad load;
    that factory immediately reads all 23 player slots. Returning only name,
    formation, rating, and chemistry therefore crashes before the create-
    squad request can be sent.
    """
    saved_squads = STATE.list_squads()
    active_id = next(
        (
            _int_or(saved.get("id") or saved.get("squadId"), 0)
            for saved in saved_squads
            if bool(saved.get("active", False))
        ),
        0,
    )
    if active_id <= 0 and saved_squads:
        active_id = _int_or(
            saved_squads[0].get("id") or saved_squads[0].get("squadId"), 0
        )
    return {
        "activeSquadId": max(0, active_id),
        "squad": [_native_squad(source=saved) for saved in saved_squads],
    }


def _native_hub_squad() -> dict[str, Any]:
    """Return a formation-safe squad for the FUT Hub response.

    CardsDLL builds the Hub formation immediately and assumes that all eleven
    starter item objects are real cards.  Empty or partly edited squads are
    valid in the Squad screen, but sending one as the Hub's active squad makes
    the native formation builder dereference player zero.  Keep the user's
    saved/active state intact and use the first complete saved squad only for
    this presentation payload.
    """
    selected = _native_squad()
    if bool(selected.get("valid", False)):
        return selected

    selected_id = _int_or(selected.get("id") or selected.get("squadId"), 0)
    for saved in STATE.list_squads():
        candidate = _native_squad(source=saved)
        if not bool(candidate.get("valid", False)):
            continue
        candidate["active"] = True
        log.warning(
            "SQUAD HUB FALLBACK activeId=%d fallbackId=%d reason=incomplete-active",
            selected_id,
            _int_or(candidate.get("id") or candidate.get("squadId"), 0),
        )
        return candidate

    # An SBC may legitimately consume cards referenced by every regular squad.
    # The saved squads must keep those positions vacant, but FIFA 20's Hub tile
    # cannot construct its native formation when any of players[0..10] is the
    # zero-ID placeholder. Complete only this response with other owned cards;
    # never write the presentation lineup back to the user's saved squad.
    players = selected.get("players", [])
    if not isinstance(players, list):
        players = []
    while len(players) < 23:
        index = len(players)
        players.append({"index": index, "kitNumber": 0, "itemData": {"id": 0, "dream": False}})
    selected["players"] = players[:23]

    def usable_slot(slot: Any) -> bool:
        if not isinstance(slot, dict) or not isinstance(slot.get("itemData"), dict):
            return False
        item = slot["itemData"]
        return _int_or(item.get("id"), 0) > 0 or (
            bool(item.get("dream", False)) and _int_or(item.get("resourceId"), 0) > 0
        )

    used_item_ids = {
        _int_or(slot.get("itemData", {}).get("id"), 0)
        for slot in selected["players"]
        if isinstance(slot, dict) and isinstance(slot.get("itemData"), dict)
    }
    used_item_ids.discard(0)
    owned_fillers = [
        item for item in STATE.list_items()
        if isinstance(item, dict)
        and str(item.get("itemType", "player")).lower() == "player"
        and str(item.get("pile", "club")).lower() in ("club", "squad")
        and _int_or(item.get("id"), 0) > 0
        and _int_or(item.get("id"), 0) not in used_item_ids
    ]
    owned_fillers.sort(
        key=lambda item: (_int_or(item.get("rating"), 0), _int_or(item.get("id"), 0)),
        reverse=True,
    )

    missing_indexes = [index for index in range(11) if not usable_slot(selected["players"][index])]
    owned_fill_count = min(len(missing_indexes), len(owned_fillers))
    for index, item in zip(missing_indexes, owned_fillers):
        selected["players"][index] = {
            "index": index,
            "kitNumber": index + 1,
            "itemData": _native_player_item(item),
        }

    # A nearly empty club still needs a structurally safe Hub response. These
    # non-persisted catalog cards exist only in the Hub DTO and cannot be sold,
    # submitted, or saved. Normal clubs never enter this branch.
    missing_indexes = [index for index in range(11) if not usable_slot(selected["players"][index])]
    catalog_fill_count = 0
    if missing_indexes:
        used_resources = {
            _int_or(slot.get("itemData", {}).get("resourceId"), 0)
            for slot in selected["players"]
            if isinstance(slot, dict) and isinstance(slot.get("itemData"), dict)
        }
        resources = sorted(
            PLAYER_DB,
            key=lambda resource_id: (
                _int_or(PLAYER_DB[resource_id].get("rating"), 0), int(resource_id)
            ),
            reverse=True,
        )
        resource_iter = (resource_id for resource_id in resources if resource_id not in used_resources)
        for index in missing_indexes:
            resource_id = next(resource_iter, 0)
            if resource_id <= 0:
                break
            item = STATE.build_player_item(resource_id, 980000000000 + index, "none")
            item["untradeable"] = True
            selected["players"][index] = {
                "index": index,
                "kitNumber": index + 1,
                "itemData": _native_player_item(item),
            }
            catalog_fill_count += 1

    starters = [slot["itemData"] for slot in selected["players"][:11] if usable_slot(slot)]
    if len(starters) == 11:
        starter_ids = [_int_or(item.get("id"), 0) for item in starters]
        selected["valid"] = True
        selected["active"] = True
        selected["captain"] = starter_ids[0]
        selected["kicktakers"] = [
            {"index": index, "id": item_id, "dream": False}
            for index, item_id in enumerate(starter_ids[:5])
        ]
        selected["rating"] = round(sum(_int_or(item.get("rating"), 0) for item in starters) / 11)
        selected["starRating"] = selected["rating"]
        log.warning(
            "SQUAD HUB PRESENTATION_FILL activeId=%d ownedFill=%d catalogFill=%d reason=sbc-depleted-squads",
            selected_id, owned_fill_count, catalog_fill_count,
        )
        return selected

    log.error(
        "SQUAD HUB UNSAFE activeId=%d starters=%d reason=no-presentation-fill-available",
        selected_id, len(starters),
    )
    return selected


# ---- FUT Draft (ported from the FIFA 21 local build) --------------------------

_DRAFT_STADIUM_ID = 261

_DRAFT_STATES = {
    "INVALID", "PICK_DIFFICULTY", "FORMATION_DRAFT", "CAPTAIN_DRAFT",
    "PLAYER_DRAFT", "MANAGER_DRAFT", "READY_FOR_MATCH", "READY_FOR_REWARDS",
}
_DRAFT_DIFFICULTIES = (
    # The Draft picker starts at AMATEUR; the match engine's difficulty enum
    # also has BEGINNER at zero. Keep the menu index and gameplay ID separate.
    "AMATEUR", "SEMIPRO", "PRO", "WORLDCLASS", "LEGENDARY", "ULTIMATE",
)
# The Draft picker and the roundsInfo wire contract use different names for
# the same third choice. CardsDLL's native roundsInfo parser only recognizes
# PROFESSIONAL; sending the picker token PRO makes it discard the field.
_DRAFT_ROUND_DIFFICULTIES = (
    "AMATEUR", "SEMIPRO", "PROFESSIONAL", "WORLDCLASS", "LEGENDARY", "ULTIMATE",
)
_DRAFT_COIN_REWARDS = (750, 1500, 3000, 5000, 9000)
_DRAFT_DIFFICULTY_COIN_MULTIPLIERS = (0.75, 1.0, 1.1, 1.25, 1.5, 1.75)
# FIFA 20 Draft paid out one of several reward bundles for a given finish.
# Keep that variation deterministic per run, with authentic representative
# Online and Single Player combinations. Reward-only IDs avoid exposing an
# earned pack as a zero-price Store article.
_DRAFT_REWARD_POOLS = {
    "SINGLE_PLAYER": {
        0: ((0, (1024,)),),
        1: ((0, (1027, 1024, 1024)),),
        2: ((0, (1016,)), (0, (1024, 1024, 1026)), (0, (1024, 1024, 1024))),
        3: ((0, (1005, 1016)), (0, (1005, 1005, 1005)), (0, (1016, 1013))),
        4: ((0, (1016, 1008)), (0, (1016, 1013)), (0, (1007,))),
    },
    "ONLINE": {
        0: ((0, (1024, 1024)),),
        1: ((0, (1024, 1005, 1005)), (0, (1024, 1024, 1013))),
        2: ((0, (1024, 1016)), (0, (1005, 1016)),
            (0, (1024, 1024, 1005, 1005)), (0, (1005, 1007)),
            (0, (1016, 1013))),
        3: ((20_000, (1024, 1013)), (0, (1007,)), (0, (1016, 1017))),
        4: ((25_000, (1005, 1017)), (30_000, (1016, 1017)),
            (45_000, (1016, 1007)), (0, (1016, 1016))),
    },
}


def _draft_reward_for_run(run: dict[str, Any], wins: int) -> tuple[int, tuple[int, ...]]:
    mode = "ONLINE" if str(run.get("mode", "SINGLE_PLAYER")).upper() == "ONLINE" else "SINGLE_PLAYER"
    finish = max(0, min(4, int(wins)))
    choices = _DRAFT_REWARD_POOLS[mode][finish]
    seed = int(run.get("seed", 1) or 1)
    rng = random.Random(f"fut20-draft-reward:{mode}:{finish}:{seed}")
    coins, packs = choices[rng.randrange(len(choices))]
    if mode == "SINGLE_PLAYER":
        choice = max(0, min(_int_or(run.get("difficulty"), 0), len(_DRAFT_DIFFICULTIES) - 1))
        base_coins = _DRAFT_COIN_REWARDS[finish]
        coins = max(500, int(round(base_coins * _DRAFT_DIFFICULTY_COIN_MULTIPLIERS[choice] / 50)) * 50)
    elif coins <= 0:
        # Zero-coin Draft bundles render as "1 Coins" in the retail reward
        # view because the first pack's quantity leaks into the coin slot.
        coins = _DRAFT_COIN_REWARDS[finish]
    return int(coins), tuple(int(pack_id) for pack_id in packs)


def _draft_game_difficulty(run: dict[str, Any]) -> int:
    """Map Draft's Amateur=0 picker to FIFA's Beginner=0 gameplay enum."""
    return max(0, min(_int_or(run.get("difficulty"), 0), len(_DRAFT_DIFFICULTIES) - 1)) + 1
DRAFT_ELITE_99_CHANCE = 0.92
_DRAFT_FORMATIONS = (
    "f3412", "f3421", "f343", "f352", "f41212", "f4141", "f4222", "f4231",
    "f4312", "f4321", "f433", "f4411", "f442", "f451", "f5212", "f5221",
    "f532",
)
_DRAFT_FORMATION_POSITIONS = {
    "f3412": ("GK", "CB", "CB", "CB", "RM", "CM", "CM", "LM", "CAM", "ST", "ST"),
    "f3421": ("GK", "CB", "CB", "CB", "RM", "CM", "CM", "LM", "RF", "ST", "LF"),
    "f343": ("GK", "CB", "CB", "CB", "RM", "CM", "CM", "LM", "RW", "ST", "LW"),
    "f352": ("GK", "CB", "CB", "CB", "RM", "CDM", "CDM", "LM", "CAM", "ST", "ST"),
    "f41212": ("GK", "RB", "CB", "CB", "LB", "CDM", "CM", "CM", "CAM", "ST", "ST"),
    "f4141": ("GK", "RB", "CB", "CB", "LB", "CDM", "RM", "CM", "CM", "LM", "ST"),
    "f4222": ("GK", "RB", "CB", "CB", "LB", "CDM", "CDM", "CAM", "CAM", "ST", "ST"),
    "f4231": ("GK", "RB", "CB", "CB", "LB", "CDM", "CDM", "CAM", "CAM", "CAM", "ST"),
    "f4312": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "CAM", "ST", "ST"),
    "f4321": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "RF", "LF", "ST"),
    "f433": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "RW", "ST", "LW"),
    "f4411": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "CF", "ST"),
    "f442": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "ST", "ST"),
    "f451": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "LM", "CM", "CAM", "ST"),
    "f5212": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "CAM", "ST", "ST"),
    "f5221": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "RW", "ST", "LW"),
    "f532": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "CM", "ST", "ST"),
    "f541": ("GK", "RWB", "CB", "CB", "CB", "LWB", "RM", "CM", "CM", "LM", "ST"),
}
_draft_lock = threading.RLock()
_DRAFT_99_POSITIONS = frozenset(
    str(meta.get("preferredPosition", "") or "").upper()
    for meta in PLAYER_DB.values()
    if int(meta.get("rating", 0) or 0) == 99
)

_DRAFT_PRESETS = (
    (80, 82),   # round 1 opponent: target rating, chemistry
    (83, 88),   # round 2
    (86, 94),   # round 3
    (89, 100),  # round 4
)
_DRAFT_OPPONENT_CLUBS = (
    (243, "Real Madrid", "RMA"),
    (9, "Liverpool", "LIV"),
    (21, "FC Bayern", "FCB"),
    (241, "FC Barcelona", "BAR"),
)


def _native_manager_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored manager to the native FUT manager subtype."""
    meta = manager_meta(int(item.get("resourceId", item.get("assetId", 0)) or 0))
    resource_id = int(item.get("resourceId", item.get("carddbid", 0)) or 0)
    asset_id = int(item.get("assetId", meta.get("assetId", 0)) or 0)
    if asset_id <= 0 or asset_id == resource_id:
        candidate = resource_id - 1_000_000
        if 0 < candidate < 1_000_000:
            asset_id = candidate
    contract = max(1, int(item.get("contract", item.get("contracts", 7)) or 7))
    minimum_price, maximum_price = _market_price_bounds(resource_id, "manager")
    return {
        "id": int(item.get("id", 0) or 0),
        "assetId": asset_id,
        "resourceId": resource_id,
        "carddbid": int(item.get("carddbid", resource_id) or resource_id),
        "managerId": asset_id,
        "stackCount": max(1, int(item.get("stackCount", 1) or 1)),
        "untradeableCount": max(0, int(item.get("untradeableCount", 0) or 0)),
        "resourceGameYear": 2020,
        "itemType": "manager",
        "itemState": str(item.get("itemState", "free") or "free"),
        "cardsubtypeid": 4,
        "category": int(item.get("category", 0) or 0),
        "contract": contract,
        "contracts": contract,
        "negotiation": int(item.get("negotiation", 0) or 0),
        "discardValue": int(item.get("discardValue", 150) or 0),
        "lastSalePrice": int(item.get("lastSalePrice", 0) or 0),
        "leagueId": int(item.get("leagueId", 0) or 0),
        "nation": int(item.get("nation", 0) or 0),
        "teamid": int(item.get("teamid", 0) or 0),
        "owners": int(item.get("owners", 1) or 1),
        "rareflag": int(item.get("rareflag", 0) or 0),
        "rating": int(item.get("rating", 0) or 0),
        "timestamp": int(item.get("timestamp", STATE.get("established", now_s())) or 0),
        "untradeable": bool(item.get("untradeable", False)),
        "pile": str(item.get("pile", "club") or "club"),
        "firstName": str(item.get("firstName", meta.get("firstName", "")) or ""),
        "lastName": str(item.get("lastName", meta.get("lastName", "")) or ""),
        "commonname": str(item.get("commonname", meta.get("commonname", meta.get("commonName", ""))) or ""),
        "marketDataMinPrice": minimum_price,
        "marketDataMaxPrice": maximum_price,
    }


def _native_default_manager() -> dict[str, Any]:
    """Return a real FUT manager for opponent/native squads."""
    managers = [item for item in STATE.list_items()
                if str(item.get("itemType", "")) == "manager"]
    if managers:
        managers.sort(key=lambda item: (-int(item.get("rating", 0) or 0), int(item.get("id", 0) or 0)))
        return _native_manager_item(managers[0])
    if not MANAGER_DB:
        return {}
    resource_id = min(MANAGER_DB, key=lambda rid: (-int(MANAGER_DB[rid].get("rating", 0) or 0), int(rid)))
    return _native_manager_item(STATE.build_manager_item(resource_id, 0, "none"))


def _draft_valid_run(run: Any) -> bool:
    return isinstance(run, dict) and str(run.get("state", "")) in _DRAFT_STATES


def _draft_new_run(mode: str = "SINGLE_PLAYER") -> dict[str, Any]:
    return {
        "mode": "ONLINE" if str(mode).upper() == "ONLINE" else "SINGLE_PLAYER",
        "state": "PICK_DIFFICULTY" if str(mode).upper() != "ONLINE" else "FORMATION_DRAFT",
        "difficulty": -1 if str(mode).upper() != "ONLINE" else 2,
        "formation": "",
        "formationChoices": [],
        "picked": {},
        "manager": None,
        "currentChoices": [],
        "currentChoiceKind": "",
        "currentPositionId": 0,
        "seenAssets": [],
        "choiceSerial": 0,
        "seed": random.SystemRandom().randrange(1, 2_000_000_000),
        "nextItemId": 910_000_000_000,
        "captain": 0,
        "wins": 0,
        "losses": 0,
        "draftsCompleted": 0,
        "pendingCoinReward": 0,
        "pendingPackRewards": [],
        "currentMatchGid": 0,
        "lastSettledGid": 0,
        "lastMatchEndResponse": {},
    }


def _draft_elite_99_pick_ordinals(run: dict[str, Any]) -> tuple[int, ...]:
    """Return the pick rounds boosted to contain a 99/98/97-rated card.

    Deterministic for a run's seed so reopening a choice does not reroll the
    chance. Tuned for an "insane" Draft: almost every run fires, and when it
    does it boosts the large majority of rounds whose slot can actually take
    a top-end card.
    """
    seed = int(run.get("seed", 1) or 1)
    rng = random.Random(f"fut20-draft-elite99:{seed}")
    if rng.random() >= DRAFT_ELITE_99_CHANCE:
        return ()
    eligible_ordinals = []
    for ordinal in range(23):
        if ordinal == 0:
            eligible_ordinals.append(ordinal)
            continue
        position_id = ordinal - 1 if ordinal <= 10 else ordinal
        allowed = _draft_allowed_positions(run, position_id)
        if allowed is None or _DRAFT_99_POSITIONS.intersection(allowed):
            eligible_ordinals.append(ordinal)
    if not eligible_ordinals:
        return ()
    count = min(len(eligible_ordinals), rng.randint(16, 22))
    return tuple(sorted(rng.sample(eligible_ordinals, count)))


def _draft_bad_pick_ordinals(run: dict[str, Any]) -> tuple[int, ...]:
    """No deliberately weak rounds -- the Draft is tuned to stay stacked."""
    return ()


def _draft_run() -> dict[str, Any] | None:
    run = STATE.draft_run()
    if not _draft_valid_run(run):
        return None
    return run


def _draft_save(run: dict[str, Any]) -> None:
    STATE.save_draft_run(run)


def _draft_allowed_positions(run: dict[str, Any], position_id: int) -> set[str] | None:
    if position_id == 11:
        return {"GK"}
    if position_id in (12, 13):
        return {"RWB", "RB", "CB", "LB", "LWB"}
    if position_id in (14, 15):
        return {"CDM", "RM", "CM", "LM", "CAM"}
    if position_id == 16:
        return {"CDM", "RM", "CM", "LM", "CAM", "RF", "CF", "LF", "RW", "ST", "LW"}
    if position_id == 17:
        return {"RF", "CF", "LF", "RW", "ST", "LW"}
    if 18 <= position_id <= 22:
        return None
    positions = _DRAFT_FORMATION_POSITIONS.get(str(run.get("formation") or "f442"), ())
    if position_id < 0 or position_id >= len(positions):
        return None
    target = positions[position_id]
    if target == "RF":
        return {"RF", "RW"}
    if target == "LF":
        return {"LF", "LW"}
    if target == "RWB":
        return {"RWB", "RB"}
    if target == "LWB":
        return {"LWB", "LB"}
    return {target}


def _draft_player_choices(run: dict[str, Any], position_id: int, captain: bool = False) -> list[dict[str, Any]]:
    seen_assets = {int(value) for value in run.get("seenAssets", [])}
    allowed = None if captain else _draft_allowed_positions(run, position_id)
    candidates = []
    for resource_id, meta in PLAYER_DB.items():
        rating = int(meta.get("rating", 0) or 0)
        asset_id = _native_player_asset_id(meta, resource_id)
        position = str(meta.get("preferredPosition", "") or "").upper()
        if rating < 75 or asset_id in seen_assets or not position:
            continue
        if allowed is not None and position not in allowed:
            continue
        candidates.append((int(resource_id), rating, asset_id))
    rng = random.Random(int(run.get("seed", 1)) + int(run.get("choiceSerial", 0)) * 1009 + position_id * 31)
    chosen = []
    used_assets = set()
    pick_ordinal = min(22, len(run.get("picked", {})))
    elite_99_pick = pick_ordinal in _draft_elite_99_pick_ordinals(run)
    bad_pick = False

    # Elite round: guarantee one of the very best cards the slot can take,
    # walking down 99 -> 98 -> 97 (-> 96 for the captain) until a rating tier
    # has a real option at this position.
    if elite_99_pick:
        for elite_rating in (99, 98, 97) + ((96,) if captain else ()):
            elite_candidates = [
                row for row in candidates
                if row[1] == elite_rating and (
                    allowed is None or
                    str(PLAYER_DB[row[0]].get("preferredPosition", "")).upper() in allowed)
            ]
            if elite_candidates:
                resource_id, _rating, asset_id = rng.choice(elite_candidates)
                item_id = int(run.get("nextItemId", 910_000_000_000))
                run["nextItemId"] = item_id + 1
                chosen.append(_native_player_item(
                    STATE.build_player_item(resource_id, item_id, "none")
                ))
                used_assets.add(asset_id)
                seen_assets.add(asset_id)
                candidates = [row for row in candidates if row[2] != asset_id]
                break

    # Rating floor: aim very high, then step down only as far as the position
    # actually allows so the screen never ends up short of five options.
    needed = 5 - len(chosen)
    target_floor = 96 if captain else 93
    for rating_floor in range(target_floor, 84, -1):
        strong_candidates = [row for row in candidates if row[1] >= rating_floor]
        if len(strong_candidates) >= needed:
            candidates = strong_candidates
            break

    while candidates and len(chosen) < 5:
        weights = []
        for resource_id, rating, _asset_id in candidates:
            rarity = int(PLAYER_DB[resource_id].get("rareflag", 0) or 0)
            exponent = 7.5 if captain else 6.5
            high_rating_bias = max(1.0, (rating - 69) ** exponent)
            special_bias = 20.0 if rarity > 1 else 1.0
            # Premium promos (TOTY 5, TOTS 11/53, TOTSSF 66, Icons 12/84, ...)
            # dominate hard.
            if rarity in (5, 11, 12, 51, 52, 53, 66, 84, 85, 86, 97):
                special_bias *= 3.0
            if rating >= 95:
                high_rating_bias *= 10.0
            weights.append(high_rating_bias * special_bias)
        resource_id, _rating, asset_id = rng.choices(candidates, weights=weights, k=1)[0]
        candidates = [row for row in candidates if row[2] != asset_id]
        if asset_id in used_assets:
            continue
        item_id = int(run.get("nextItemId", 910_000_000_000))
        run["nextItemId"] = item_id + 1
        item = _native_player_item(STATE.build_player_item(resource_id, item_id, "none"))
        chosen.append(item)
        used_assets.add(asset_id)
        seen_assets.add(asset_id)
    run["seenAssets"] = sorted(seen_assets)
    run["choiceSerial"] = int(run.get("choiceSerial", 0)) + 1
    log.info(
        "DRAFT WEIGHT pick=%s guaranteed99=%s badPick=%s ratings=%s rarities=%s",
        pick_ordinal, elite_99_pick, bad_pick,
        [int(item.get("rating", 0) or 0) for item in chosen],
        [int(item.get("rareflag", 0) or 0) for item in chosen],
    )
    return chosen


def _draft_manager_choices(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows = sorted(MANAGER_DB, key=lambda rid: (-int(MANAGER_DB[rid].get("rating", 0) or 0), int(rid)))
    rng = random.Random(int(run.get("seed", 1)) + 97_531)
    pool = rows[:min(25, len(rows))]
    rng.shuffle(pool)
    result = []
    for resource_id in pool[:5]:
        item_id = int(run.get("nextItemId", 910_000_000_000))
        run["nextItemId"] = item_id + 1
        result.append(_native_manager_item(STATE.build_manager_item(resource_id, item_id, "none")))
    return result


def _draft_choices(run: dict[str, Any], kind: str, position_id: int = 0) -> dict[str, Any]:
    kind = kind.lower()
    if kind == "difficulty":
        # The tile index is Draft-local (Amateur starts at zero), but the
        # difficulty value is FIFA's native gameplay/profile enum (Beginner
        # is zero, so Amateur starts at one).  Returning the tile index in
        # both fields made Semi-Pro cache as Amateur before match creation.
        choices = [{"choiceIndex": index, "difficulty": index + 1, "difficultyName": name}
                   for index, name in enumerate(_DRAFT_DIFFICULTIES)]
        return {"choices": choices}
    if kind == "formation":
        saved_choices = run.get("formationChoices", [])
        if (not isinstance(saved_choices, list) or len(saved_choices) != 5 or
                any(choice not in _DRAFT_FORMATIONS for choice in saved_choices)):
            rng = random.Random(int(run.get("seed", 1)) + 41)
            run["formationChoices"] = rng.sample(list(_DRAFT_FORMATIONS), 5)
        _draft_save(run)
        return {"choices": [{"formation": formation, "index": index}
                            for index, formation in enumerate(run["formationChoices"])],
                "positionid": 0, "tier": 0}
    if kind == "captain":
        items = _draft_player_choices(run, 0, captain=True)
    elif kind == "player":
        items = _draft_player_choices(run, position_id, captain=False)
    elif kind == "manager":
        items = _draft_manager_choices(run)
    else:
        items = []
    run["currentChoices"] = items
    run["currentChoiceKind"] = kind
    run["currentPositionId"] = position_id
    _draft_save(run)
    return {"choices": [{"index": index, "itemData": item}
                        for index, item in enumerate(items)],
            "positionid": position_id, "tier": 0}


def _draft_choose(run: dict[str, Any], choice_index: int, position_id: int) -> None:
    state = str(run.get("state", ""))
    if state == "FORMATION_DRAFT":
        formations = run.get("formationChoices", [])
        run["formation"] = formations[choice_index] if 0 <= choice_index < len(formations) else (formations[0] if formations else "f442")
        run["state"] = "CAPTAIN_DRAFT"
    elif state in ("CAPTAIN_DRAFT", "PLAYER_DRAFT"):
        choices = run.get("currentChoices", [])
        if choices:
            selected = choices[choice_index] if 0 <= choice_index < len(choices) else choices[0]
            slot = max(0, min(22, int(position_id)))
            run.setdefault("picked", {})[str(slot)] = selected
            if state == "CAPTAIN_DRAFT":
                run["captain"] = int(selected.get("id", 0) or 0)
                run["state"] = "PLAYER_DRAFT"
            elif len(run["picked"]) >= 23:
                run["state"] = "MANAGER_DRAFT"
    elif state == "MANAGER_DRAFT":
        choices = run.get("currentChoices", [])
        if choices:
            run["manager"] = choices[choice_index] if 0 <= choice_index < len(choices) else choices[0]
        run["state"] = "READY_FOR_MATCH"
    run["currentChoices"] = []
    run["currentChoiceKind"] = ""
    run["currentPositionId"] = int(position_id)
    _draft_save(run)


def _draft_apply_slot_order(run: dict[str, Any], raw_resource_ids: Any) -> bool:
    """Persist the current Draft XI/bench order sent with a later pick.

    The chosen card for this request is not in ``swapPlayerDefIds`` yet, so the
    caller applies the order before adding that selection. Item instances stay
    intact while moving by resourceId.
    """
    if not isinstance(raw_resource_ids, list):
        return False
    picked = run.get("picked")
    if not isinstance(picked, dict) or not picked:
        return False

    current: list[tuple[int, dict[str, Any]]] = []
    by_resource: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for raw_slot, item in picked.items():
        if not isinstance(item, dict):
            continue
        slot = max(0, min(22, _int_or(raw_slot, 0)))
        resource_id = _int_or(item.get("resourceId") or item.get("carddbid"), 0)
        current.append((slot, item))
        if resource_id > 0:
            by_resource.setdefault(resource_id, []).append((slot, item))

    reordered: dict[str, dict[str, Any]] = {}
    used_items: set[int] = set()
    for target_slot, raw_resource_id in enumerate(raw_resource_ids[:23]):
        resource_id = _int_or(raw_resource_id, 0)
        if resource_id <= 0:
            continue
        candidates = by_resource.get(resource_id, [])
        match = next((entry for entry in candidates if id(entry[1]) not in used_items), None)
        if match is None:
            continue
        reordered[str(target_slot)] = match[1]
        used_items.add(id(match[1]))

    if not used_items:
        return False

    # A partial array must never drop a pick. Keep unmatched cards in the old
    # slot where possible, otherwise move them to the first available slot.
    free_slots = [slot for slot in range(23) if str(slot) not in reordered]
    for old_slot, item in sorted(current, key=lambda entry: entry[0]):
        if id(item) in used_items:
            continue
        target_slot = old_slot if str(old_slot) not in reordered else free_slots[0]
        reordered[str(target_slot)] = item
        if target_slot in free_slots:
            free_slots.remove(target_slot)

    run["picked"] = reordered
    return True


def _draft_squad(run: dict[str, Any]) -> dict[str, Any]:
    picked = run.get("picked", {}) if isinstance(run.get("picked"), dict) else {}
    players = []
    for index in range(23):
        item = picked.get(str(index))
        if isinstance(item, dict):
            players.append({"index": index, "kitNumber": index + 1, "itemData": item})
        else:
            players.append({"index": index, "kitNumber": 0})
    manager = run.get("manager")
    manager_list = [manager] if isinstance(manager, dict) else []
    populated = [slot["itemData"] for slot in players if isinstance(slot.get("itemData"), dict)]
    starter_items = [
        slot["itemData"] for slot in players[:11]
        if isinstance(slot.get("itemData"), dict)
    ]
    starter_ratings = [int(item.get("rating", 0) or 0) for item in starter_items]
    rating = round(sum(starter_ratings) / len(starter_ratings)) if starter_ratings else 0
    captain = int(run.get("captain", 0) or (populated[0].get("id", 0) if populated else 0))
    actives = _active_club_items()
    return {
        "id": 900001,
        "personaId": int(CFG["persona_id"]),
        "squadName": "Draft Squad",
        "squadType": "DRAFT_SQUAD",
        "active": False,
        "changed": 0,
        "formation": str(run.get("formation") or "f442"),
        "captain": captain,
        "chemistry": 100 if len(starter_items) == 11 else 0,
        "starRating": rating,
        "rating": rating,
        "players": players,
        "manager": manager_list,
        "actives": actives,
        "club": [dict(item) for item in actives],
        "stadiumAssetId": _DRAFT_STADIUM_ID,
        "stadiumId": _DRAFT_STADIUM_ID,
    }


def _draft_autofill_response(run: dict[str, Any]) -> dict[str, Any]:
    """Return the object-shaped result used by native Draft autocomplete.

    ``FutGetDraftCurrentStateServerResponse`` is list-shaped, but FIFA binds
    the autocomplete command to the distinct operation-result contract.
    Returning the current-state row at the response root exposes its
    ``READY_FOR_MATCH`` state to the global navigation handler and sends FIFA
    back to the FUT Hub even though every pick was saved.  Return the native
    operation envelope instead: success/mode plus a nested state and the
    completed squad/player aliases consumed by the Draft screen.
    """
    state = _draft_state_payload(run)[0]
    squad = state["squad"]
    return {
        "success": True,
        "mode": "SINGLE_PLAYER",
        "draftState": state,
        "squad": squad,
        "players": squad.get("players", []),
    }


def _draft_native_match_squad(run: dict[str, Any]) -> dict[str, Any]:
    """Project the Draft editor squad into FIFA's playable match contract."""
    squad = _draft_squad(run)
    formation = str(squad.get("formation") or "f442")
    players: list[dict[str, Any]] = []
    for index, raw_slot in enumerate(squad.get("players", [])[:23]):
        slot = dict(raw_slot) if isinstance(raw_slot, dict) else {"index": index}
        slot["index"] = int(slot.get("index", index) or index)
        slot["kitNumber"] = int(slot.get("kitNumber", index + 1) or 0)
        raw_item = slot.get("itemData")
        if isinstance(raw_item, dict):
            item = dict(raw_item)
            item["itemState"] = "free"
            item["contract"] = max(1, int(item.get("contract", 7) or 7))
            item["fitness"] = max(1, int(item.get("fitness", 99) or 99))
            item["formation"] = formation
            item["concept"] = False
            item["untradeable"] = True
            slot["itemData"] = item
        players.append(slot)
    while len(players) < 23:
        players.append({"index": len(players), "kitNumber": 0})

    starters = [
        slot["itemData"] for slot in players[:11]
        if isinstance(slot.get("itemData"), dict)
    ]
    captain = int(squad.get("captain", 0) or 0)
    starter_ids = [int(item.get("id", 0) or 0) for item in starters]
    if captain not in starter_ids and starter_ids:
        captain = starter_ids[0]
    kick_ids = [item_id for item_id in starter_ids if item_id > 0][:5]
    while kick_ids and len(kick_ids) < 5:
        kick_ids.append(kick_ids[-1])

    manager = []
    for raw_manager in squad.get("manager", []):
        if not isinstance(raw_manager, dict):
            continue
        item = dict(raw_manager.get("itemData", raw_manager))
        item["itemState"] = "free"
        item["concept"] = False
        item["untradeable"] = True
        manager.append(item)
    if not manager:
        manager = [_native_default_manager()]

    ratings = [int(item.get("rating", 0) or 0) for item in starters]
    rating = round(sum(ratings) / len(ratings)) if ratings else 75
    actives = _active_club_items()
    squad.update({
        "squadId": 900001,
        "name": "Draft Squad",
        "active": True,
        "valid": True,
        "newSquad": 0,
        "newsquad": 0,
        "formation": formation,
        "captain": captain,
        "chemistry": 100,
        "starRating": rating,
        "rating": rating,
        "custom": "[0,0,0,0,0,0,0,0,0,0,0]",
        "players": players,
        "manager": manager,
        "kicktakers": [
            {"index": index, "id": item_id, "dream": False}
            for index, item_id in enumerate(kick_ids)
        ],
        "actives": actives,
        "club": [dict(item) for item in actives],
        "stadiumAssetId": _DRAFT_STADIUM_ID,
        "stadiumId": _DRAFT_STADIUM_ID,
    })
    return squad


def _draft_state_payload(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    if run is None:
        return [{
            "entranceCriteria": {"COINS": 15000, "DRAFT_TOKEN": 1, "POINTS": 300},
            "gamesWonCurrentMatch": 0,
            "roundsInfo": [],
            "squadState": "INVALID",
            "stateParam1": "INVALID",
            "stateParam2": "0",
        }]
    state = str(run.get("state", "INVALID"))
    position_id = int(run.get("currentPositionId", 0) or 0)
    raw_difficulty = _int_or(run.get("difficulty"), -1)
    draft_difficulty = max(0, min(raw_difficulty, len(_DRAFT_DIFFICULTIES) - 1))
    rounds_info: list[dict[str, Any]] = []
    if raw_difficulty >= 0:
        # CardsDLL's retail round-record parser (0x180257130) dispatches JSON
        # property id 0x116: `difficulty`. Its enum converter accepts the
        # canonical gameplay names, including PROFESSIONAL rather than the
        # Draft picker's PRO token. The previous `difficultyName` member was
        # therefore ignored and both preview/settings fell back to unrelated
        # profile values (Ultimate and Beginner respectively).
        round_index = min(3, max(0, _int_or(run.get("wins"), 0)))
        opponent_id = int(_DRAFT_OPPONENT_CLUBS[round_index][0])
        opponent_rating = int(_DRAFT_PRESETS[round_index][0])
        rounds_info.append({
            "difficulty": _DRAFT_ROUND_DIFFICULTIES[draft_difficulty],
            "opponentId": opponent_id,
            "opponentPenaltyScore": 0,
            "opponentRating": opponent_rating,
            "penaltyScore": 0,
            "round": round_index + 1,
            "score": 0,
        })
    payload = {
        "entranceCriteria": {"COINS": 15000, "DRAFT_TOKEN": 1, "POINTS": 300},
        "gamesWonCurrentMatch": int(run.get("wins", 0) or 0),
        "roundsInfo": rounds_info,
        "squadState": state,
        "stateParam1": "PLAYER" if state == "PLAYER_DRAFT" else "INVALID",
        "stateParam2": "23" if state in ("MANAGER_DRAFT", "READY_FOR_MATCH", "READY_FOR_REWARDS") else str(position_id),
        "squad": _draft_squad(run),
    }
    return [payload]


def _draft_opponent_player_ids(
    target_rating: int, round_index: int, seed_key: str | None = None,
    shortlist_size: int = 160, closeness_exponent: float = 1.7,
    team_id: int | None = None,
) -> list[int]:
    """Seeded, positionally valid 23-man CPU squad around a target rating.

    Squad Battles uses a tighter shortlist and stronger weighting so its high
    tiers really field stronger teams. Draft keeps the original defaults.
    """
    slot_positions = (
        ("GK",), ("LB", "LWB"), ("CB",), ("CB",), ("RB", "RWB"),
        ("LM", "LW"), ("CM", "CDM", "CAM"), ("CM", "CAM", "CDM"),
        ("RM", "RW"), ("ST", "CF"), ("ST", "CF"),
        ("GK",), ("LB", "LWB"), ("CB",), ("RB", "RWB"),
        ("LM", "LW"), ("CM", "CAM", "CDM"), ("RM", "RW"),
        ("CAM", "CM", "CF"), ("CDM", "CM"), ("LW", "LM"),
        ("RW", "RM"), ("ST", "CF"),
    )
    used_resources: set[int] = set()
    used_assets: set[int] = set()
    selected: list[int] = []
    rng = random.Random(seed_key or f"fut20-draft-opponent:{round_index}:{int(target_rating)}")
    required_team_id = max(0, int(team_id or 0))
    all_positions = tuple({
        str(meta.get("preferredPosition", "") or "").upper()
        for meta in PLAYER_DB.values()
    })

    def candidates(positions: tuple[str, ...]) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        for raw_resource_id, meta in PLAYER_DB.items():
            resource_id = int(raw_resource_id)
            position = str(meta.get("preferredPosition", "") or "").upper()
            rating = int(meta.get("rating", 0) or 0)
            rareflag = int(meta.get("rareflag", 1) or 0)
            asset_id = _native_player_asset_id(meta, resource_id)
            if required_team_id and int(meta.get("teamid", 0) or 0) != required_team_id:
                continue
            if position not in positions or rating < 55:
                continue
            if rareflag not in (0, 1) or resource_id > 0x00FFFFFF:
                continue
            if resource_id in used_resources or asset_id in used_assets:
                continue
            result.append((abs(rating - target_rating), -rating, resource_id))
        return sorted(result)

    for positions in slot_positions:
        pool = candidates(positions) or candidates(all_positions)
        if not pool:
            raise RuntimeError("FIFA 20 player catalog cannot fill a Draft opponent squad")
        shortlist = pool[:min(shortlist_size, len(pool))]
        weights = [1.0 / ((1.0 + row[0]) ** closeness_exponent) for row in shortlist]
        resource_id = int(rng.choices(shortlist, weights=weights, k=1)[0][2])
        selected.append(resource_id)
        used_resources.add(resource_id)
        meta = player_meta(resource_id)
        used_assets.add(_native_player_asset_id(meta, resource_id))
    return selected


def _draft_opponent_club_items(
    team_id: int,
    round_index: int,
    stadium_id: int = _DRAFT_STADIUM_ID,
    ball_id: int = 0,
) -> list[dict[str, Any]]:
    """Build CPU-only customization items for the selected real club.

    These are deliberately not read from the user's active club items.  The
    old implementation copied the user's equipped badge and both kits into
    every CPU squad, even though the opponent's name/teamId was different.
    """
    team_id = int(team_id)

    def team_resource(pool: list[int]) -> int:
        return next((
            int(resource_id) for resource_id in pool
            if int(CLUB_ITEM_DB.get(int(resource_id), {}).get("teamid", 0) or 0) == team_id
        ), 0)

    home_resource = team_resource(_NATIVE_HOME_KITS)
    away_resource = team_resource(_NATIVE_AWAY_KITS)
    badge_resource = 6_050_000 + team_id
    if badge_resource not in CLUB_ITEM_DB:
        badge_resource = next((
            int(resource_id) for resource_id in _NATIVE_BADGES
            if int(CLUB_ITEM_DB.get(int(resource_id), {}).get("teamid", 0) or 0) == team_id
        ), 0)

    stadium_resource = 6_201_000 + max(0, int(stadium_id))
    if stadium_resource not in CLUB_ITEM_DB:
        stadium_resource = 6_200_058
    ball_resource = 8_120_000 + max(0, int(ball_id))
    if ball_resource not in CLUB_ITEM_DB:
        ball_resource = 8_120_212

    definitions = (
        (home_resource, "activeHomeKit"),
        (away_resource, "activeAwayKit"),
        (badge_resource, "activeBadge"),
        (stadium_resource, "activeStadium"),
        (ball_resource, "activeBall"),
    )
    result: list[dict[str, Any]] = []
    for slot, (resource_id, state) in enumerate(definitions):
        if resource_id not in CLUB_ITEM_DB:
            continue
        item_id = 8_800_000 + max(0, int(round_index)) * 10 + slot
        item = STATE.build_club_item(resource_id, item_id, "none", state)
        item.pop("pile", None)
        result.append(item)
    return result


def _draft_opponent_squad(round_index: int) -> dict[str, Any]:
    """One complete playable CPU opponent squad for a Draft round."""
    round_index = max(0, min(int(round_index), len(_DRAFT_PRESETS) - 1))
    target_rating, chemistry = _DRAFT_PRESETS[round_index]
    team_id, club_name, club_abbr = _DRAFT_OPPONENT_CLUBS[round_index]
    players: list[dict[str, Any]] = []
    for index, resource_id in enumerate(_draft_opponent_player_ids(
        target_rating, round_index, team_id=team_id,
    )):
        item_id = 920000 + round_index * 100 + index
        item = STATE.build_player_item(resource_id, item_id, "none")
        players.append({
            "index": index,
            "kitNumber": index + 1,
            "itemData": _native_player_item(item),
        })
    starter_ids = [int(player["itemData"]["id"]) for player in players[:11]]
    kicktakers = [
        {"index": index, "id": item_id, "dream": False}
        for index, item_id in enumerate(starter_ids[1:6])
    ]
    actives = _draft_opponent_club_items(team_id, round_index)
    return {
        "id": team_id,
        "squadId": team_id,
        "personaId": int(CFG["persona_id"]),
        "squadName": club_name,
        "name": club_name,
        "clubName": club_name,
        "clubAbbr": club_abbr,
        "teamId": team_id,
        "badgeAssetId": team_id,
        "formation": "f442",
        "captain": starter_ids[1] if len(starter_ids) > 1 else (starter_ids[0] if starter_ids else 0),
        "chemistry": chemistry,
        "changed": 0,
        "starRating": target_rating,
        "rating": target_rating,
        "custom": "[0,0,0,0,0,0,0,0,0,0,0]",
        "players": players,
        "actives": actives,
        "club": [dict(item) for item in actives],
        "stadiumAssetId": _DRAFT_STADIUM_ID,
        "stadiumId": _DRAFT_STADIUM_ID,
        "isHomeTeam": True,
        "manager": [_native_default_manager()],
        "kicktakers": kicktakers,
    }


def _native_draft_match_response(run: dict[str, Any]) -> dict[str, Any]:
    squad = _draft_native_match_squad(run)
    squad["home"] = False
    squad["isHomeTeam"] = False
    round_index = min(3, max(0, int(run.get("wins", 0) or 0)))
    opponent = _draft_opponent_squad(round_index)
    opponent_team_id = int(_DRAFT_OPPONENT_CLUBS[round_index][0])
    opponent["id"] = opponent_team_id
    opponent["squadId"] = opponent_team_id
    opponent["squadType"] = "DRAFT_SQUAD"
    opponent["active"] = True
    opponent["valid"] = True
    choice_difficulty = max(0, min(
        int(run.get("difficulty", 0) or 0), len(_DRAFT_DIFFICULTIES) - 1,
    ))
    difficulty = _draft_game_difficulty(run)
    difficulty_name = _DRAFT_DIFFICULTIES[choice_difficulty]
    # Different native match-preview/gameplay consumers read the setting from
    # different levels of the match document. Keep the engine's 1..6 enum
    # on every squad alias as well as at the response root.
    for match_squad in (squad, opponent):
        match_squad.update({
            "matchDifficulty": difficulty,
            "difficulty": difficulty,
            "difficultyId": difficulty,
            "gameDifficulty": difficulty,
            "sqbtMatchDifficulty": difficulty,
            "difficultyName": difficulty_name,
        })
    return {
        "squad": squad,
        "opponentTeamId": opponent_team_id,
        "opponentSquad": opponent,
        "awaySquad": opponent,
        "opponent": opponent,
        # The picker uses 0..5 while the match engine uses 1..6. Publish both
        # matchDifficulty and the FUT-specific sqbtMatchDifficulty so neither
        # preview nor gameplay has to fall back to a stale global setting.
        "matchDifficulty": difficulty,
        "difficulty": difficulty,
        "difficultyId": difficulty,
        "gameDifficulty": difficulty,
        "sqbtMatchDifficulty": difficulty,
        "draftDifficulty": choice_difficulty,
        "difficultyName": difficulty_name,
        "startDateTime": now_s(),
        "stadiumAssetId": _DRAFT_STADIUM_ID,
        "stadiumId": _DRAFT_STADIUM_ID,
        "home": False,
        "isHomeTeam": False,
    }


# ---- Squad Battles native contract (ported from unknown20) ----------------

_SQUAD_BATTLE_PRESETS = (
    # FUT club name, abbreviation, equipped badge/team id, target rating,
    # chemistry, difficulty.  These are user-style FUT squads, not the real
    # licensed teams represented by their chosen badges.
    ("North London XI",  "NLX", 1,   82, 85,  "SEMIPRO"),
    ("Sky Blues United", "SBU", 10,  85, 90,  "PROFESSIONAL"),
    ("Yellow Wall FC",   "YWF", 22,  88, 95,  "WORLD CLASS"),
    ("Royal Champions",  "ROY", 243, 92, 100, "LEGENDARY"),
)

_SQUAD_BATTLE_CLUB_META = {
    # ``established`` is a FUT account creation timestamp.  Sending a football
    # club's founding year (for example 1880) is outside the native timestamp
    # range and makes this screen fall back to the literal "FUT Founder".
    1: {"nation": 14, "foundationYear": 2019, "established": 1567296000,
        "stadiumId": 156, "stadiumName": "Emirates Stadium", "ballId": 37,
        "location": "ENGLAND", "ownerName": "GunnerAce"},
    10: {"nation": 14, "foundationYear": 2019, "established": 1569974400,
         "stadiumId": 246, "stadiumName": "Etihad Stadium", "ballId": 37,
         "location": "ENGLAND", "ownerName": "BlueMoon20"},
    22: {"nation": 21, "foundationYear": 2019, "established": 1572652800,
         "stadiumId": 9, "stadiumName": "Signal Iduna Park", "ballId": 23,
         "location": "GERMANY", "ownerName": "YellowWall"},
    243: {"nation": 45, "foundationYear": 2019, "established": 1575158400,
          "stadiumId": 2, "stadiumName": "Estadio Santiago Bernabeu", "ballId": 57,
          "location": "SPAIN", "ownerName": "RoyalFUT"},
    5: {"nation": 14, "foundationYear": 2019, "established": 1569888000,
        "stadiumId": 58, "stadiumName": "Stamford Bridge", "ballId": 37,
        "location": "ENGLAND", "ownerName": "BlueLondon"},
    9: {"nation": 14, "foundationYear": 2019, "established": 1569801600,
        "stadiumId": 206, "stadiumName": "Anfield", "ballId": 37,
        "location": "ENGLAND", "ownerName": "MerseyRed"},
    11: {"nation": 14, "foundationYear": 2019, "established": 1569715200,
         "stadiumId": 178, "stadiumName": "Old Trafford", "ballId": 37,
         "location": "ENGLAND", "ownerName": "RedDevils20"},
    18: {"nation": 14, "foundationYear": 2019, "established": 1569628800,
         "stadiumId": 330, "stadiumName": "Tottenham Hotspur Stadium", "ballId": 37,
         "location": "ENGLAND", "ownerName": "NorthLondonWhite"},
    21: {"nation": 21, "foundationYear": 2019, "established": 1569542400,
         "stadiumId": 137, "stadiumName": "Allianz Arena", "ballId": 23,
         "location": "GERMANY", "ownerName": "BavarianFUT"},
    44: {"nation": 27, "foundationYear": 2019, "established": 1569456000,
         "stadiumId": 5, "stadiumName": "San Siro", "ballId": 12,
         "location": "ITALY", "ownerName": "NerazzurriXI"},
    47: {"nation": 27, "foundationYear": 2019, "established": 1569369600,
         "stadiumId": 5, "stadiumName": "San Siro", "ballId": 12,
         "location": "ITALY", "ownerName": "RossoneriXI"},
    73: {"nation": 18, "foundationYear": 2019, "established": 1569283200,
         "stadiumId": 155, "stadiumName": "Parc des Princes", "ballId": 22,
         "location": "FRANCE", "ownerName": "ParisElite"},
    95: {"nation": 14, "foundationYear": 2019, "established": 1569196800,
         "stadiumId": 261, "stadiumName": "King Power Stadium", "ballId": 37,
         "location": "ENGLAND", "ownerName": "FoxesFUT"},
    240: {"nation": 45, "foundationYear": 2019, "established": 1569110400,
          "stadiumId": 298, "stadiumName": "Wanda Metropolitano", "ballId": 57,
          "location": "SPAIN", "ownerName": "AtletiFUT"},
    241: {"nation": 45, "foundationYear": 2019, "established": 1569024000,
          "stadiumId": 2, "stadiumName": "Camp Nou", "ballId": 57,
          "location": "SPAIN", "ownerName": "CatalunyaXI"},
}


def _squad_battle_presentation(team_id: int, slot: int) -> dict[str, Any]:
    """FUT club metadata and scalar customization IDs used by the SB detail UI."""
    profile = dict(_SQUAD_BATTLE_CLUB_META.get(int(team_id), {}))
    stadium_id = _int_or(profile.get("stadiumId"), _DRAFT_STADIUM_ID)
    ball_id = _int_or(profile.get("ballId"), 0)
    actives = _draft_opponent_club_items(team_id, slot, stadium_id, ball_id)
    by_state = {str(item.get("itemState", "")): item for item in actives}
    home = by_state.get("activeHomeKit", {})
    away = by_state.get("activeAwayKit", {})
    badge = by_state.get("activeBadge", {})
    established = _int_or(profile.get("established"), 1567296000)
    home_resource = _int_or(home.get("resourceId"), 0)
    away_resource = _int_or(away.get("resourceId"), 0)
    home_team = _int_or(home.get("teamid"), team_id)
    away_team = _int_or(away.get("teamid"), team_id)
    return {
        **profile,
        "est": established,
        "established": established,
        "establishedTime": established,
        "clubEstablished": established,
        "homeKitId": home_resource,
        "awayKitId": away_resource,
        "homeKitResourceId": home_resource,
        "awayKitResourceId": away_resource,
        # The detail panel's thumbnail renderer resolves these asset aliases as
        # team IDs, while the full squad/customization readers use resource IDs.
        "homeKitAssetId": home_team,
        "awayKitAssetId": away_team,
        "homeKitTeamId": home_team,
        "awayKitTeamId": away_team,
        "badgeId": _int_or(badge.get("teamid"), team_id),
        "homeKit": home,
        "awayKit": away,
        "actives": actives,
        "club": [dict(item) for item in actives],
    }


def _squad_battle_cycle_id() -> int:
    """Stable daily key so a day's hub and opponent squads stay consistent."""
    return now_s() // (24 * 60 * 60)


def _squad_battle_opponent_ids(
        target_rating: int, slot: int, refresh_id: int = 0) -> list[int]:
    # The daily cycle keeps a round stable across requests.  refresh_id makes
    # the next four-opponent round a genuinely new set instead of merely
    # assigning new ids to the same four squads.
    base_ids = _draft_opponent_player_ids(
        target_rating, slot,
        seed_key=(
            f"fut20-squad-battles:{_squad_battle_cycle_id()}:"
            f"{max(0, int(refresh_id))}:{int(slot)}:{int(target_rating)}"
        ),
        shortlist_size=40, closeness_exponent=3.2,
    )
    return _squad_battle_promote_specials(
        base_ids, target_rating, slot, refresh_id
    )


_SB_SPECIAL_VARIANTS_BY_ASSET: dict[int, list[int]] | None = None


def _squad_battle_promote_specials(
        resource_ids: list[int], target_rating: int, slot: int,
        refresh_id: int) -> list[int]:
    """Replace several base cards with real special versions of those players."""
    global _SB_SPECIAL_VARIANTS_BY_ASSET
    if _SB_SPECIAL_VARIANTS_BY_ASSET is None:
        variants: dict[int, list[int]] = {}
        for raw_resource_id, meta in PLAYER_DB.items():
            resource_id = int(raw_resource_id)
            asset_id = _native_player_asset_id(meta, resource_id)
            if asset_id <= 0 or resource_id == asset_id:
                continue
            if not bool(meta.get("special", False)) and _int_or(meta.get("rareflag"), 0) <= 1:
                continue
            variants.setdefault(asset_id, []).append(resource_id)
        for values in variants.values():
            values.sort(key=lambda rid: (_int_or(player_meta(rid).get("rating"), 0), rid))
        _SB_SPECIAL_VARIANTS_BY_ASSET = variants

    promoted = list(resource_ids)
    desired = min(4, max(1, int(slot) + 1))
    rng = random.Random(
        f"fut20-sb-specials:{_squad_battle_cycle_id()}:{refresh_id}:{slot}"
    )
    starter_indices = list(range(min(11, len(promoted))))
    rng.shuffle(starter_indices)
    replacements = 0
    for index in starter_indices:
        base_id = promoted[index]
        base_meta = player_meta(base_id)
        asset_id = _native_player_asset_id(base_meta, base_id)
        candidates = list((_SB_SPECIAL_VARIANTS_BY_ASSET or {}).get(asset_id, []))
        if not candidates:
            continue
        sensible = [
            rid for rid in candidates
            if _int_or(player_meta(rid).get("rating"), 0) <= target_rating + 5
        ] or candidates
        sensible.sort(key=lambda rid: (
            abs(_int_or(player_meta(rid).get("rating"), 0) - target_rating), rid
        ))
        promoted[index] = sensible[rng.randrange(min(3, len(sensible)))]
        replacements += 1
        if replacements >= desired:
            break
    return promoted


def _native_squad_battle_event() -> dict[str, Any]:
    current = now_s()
    start = current - 24 * 60 * 60
    end = current + 30 * 24 * 60 * 60
    event_id = current // (7 * 24 * 60 * 60)
    return {
        "id": event_id, "eventId": event_id, "competitionId": event_id,
        "startTime": start, "startTimeStamp": start, "eventStartTime": start,
        "endTime": end, "endTimeStamp": end, "eventEndTime": end,
        "currentTime": current, "crtTime": current, "serverCrtTime": current,
        "serverTimeInSeconds": current,
        "secondsUntilStart": 0, "secondsUntilEnd": end - current,
        "timeUntilStart": 0, "timeUntilEnd": end - current,
        "untilStartSeconds": 0, "untilEndSeconds": end - current,
        "nextEventStartTime": end + 1,
        "gamesPlayed": 0, "numberOfMatchesPlayed": 0, "matchesPlayed": 0,
        "gamesRemaining": 40, "remainingMatches": 40, "squadsRemaining": 4,
        "totalGames": 40, "maxMatches": 40, "maxNumberOfMatches": 40,
        "maxRegularGames": 40, "userPoints": 0, "pointsWon": 0,
        "squadBattleEnabled": True, "weeklyRewardsAutoClaimed": False,
        "active": True, "isActive": True, "enabled": True, "available": True,
        "status": "ACTIVE",
    }


def _native_squad_battle_opponent(
        slot: int, refresh_id: int = 0,
        team_profile: tuple[int, str, str] | None = None) -> dict[str, Any]:
    slot = max(0, min(int(slot), len(_SQUAD_BATTLE_PRESETS) - 1))
    club_name, club_abbr, badge_id, target_rating, chemistry, difficulty = _SQUAD_BATTLE_PRESETS[slot]
    if team_profile is not None:
        badge_id, club_name, club_abbr = team_profile
    players: list[dict[str, Any]] = []
    for index, resource_id in enumerate(
            _squad_battle_opponent_ids(target_rating, slot, refresh_id)):
        item_id = 940000 + slot * 100 + index
        item = STATE.build_player_item(resource_id, item_id, "none")
        players.append({
            "index": index,
            "kitNumber": index + 1,
            "itemData": _native_player_item(item),
        })
    starter_ids = [int(p["itemData"]["id"]) for p in players[:11]]
    kicktakers = [
        {"index": i, "id": iid, "dream": False}
        for i, iid in enumerate(starter_ids[1:6])
    ]
    presentation = _squad_battle_presentation(badge_id, slot)
    actives = presentation["actives"]
    actual_rating = int(round(sum(
        _int_or(player["itemData"].get("rating"), 0) for player in players[:11]
    ) / 11.0)) if players else target_rating
    return {
        "id": slot, "squadId": slot,
        "personaId": int(CFG["persona_id"]),
        "squadName": club_name, "name": club_name,
        "clubName": club_name, "clubAbbr": club_abbr,
        "displayName": club_name, "teamName": club_name,
        "ownerName": presentation["ownerName"],
        "personaName": presentation["ownerName"],
        "badgeAssetId": str(badge_id), "teamId": badge_id, "teamid": badge_id,
        "squadType": "REGULAR_SQUAD", "formation": "f442",
        "captain": starter_ids[1] if len(starter_ids) > 1 else (starter_ids[0] if starter_ids else 0),
        "chemistry": chemistry, "changed": 0,
        "active": True, "valid": True,
        "starRating": actual_rating,
        "rating": actual_rating,
        "difficulty": difficulty, "difficultyName": difficulty,
        "custom": "[0,0,0,0,0,0,0,0,0,0,0]",
        "players": players,
        "actives": actives, "club": [dict(item) for item in actives],
        "manager": [_native_default_manager()], "kicktakers": kicktakers,
        "nation": presentation["nation"],
        "location": presentation["location"],
        "foundationYear": presentation["foundationYear"],
        "est": presentation["est"], "established": presentation["established"],
        "homeKitId": presentation["homeKitId"],
        "awayKitId": presentation["awayKitId"],
        "homeKitResourceId": presentation["homeKitResourceId"],
        "awayKitResourceId": presentation["awayKitResourceId"],
        "homeKitAssetId": presentation["homeKitAssetId"],
        "awayKitAssetId": presentation["awayKitAssetId"],
        "homeKitTeamId": presentation["homeKitTeamId"],
        "awayKitTeamId": presentation["awayKitTeamId"],
        "homeKit": presentation["homeKit"], "awayKit": presentation["awayKit"],
        "clubInfo": {
            "clubName": club_name, "clubAbbr": club_abbr,
            "displayName": club_name, "ownerName": presentation["ownerName"],
            "teamId": badge_id, "badgeId": badge_id,
            "established": presentation["established"],
            "foundationYear": presentation["foundationYear"],
            "nation": presentation["nation"], "location": presentation["location"],
        },
        "stadiumAssetId": presentation["stadiumId"],
        "stadiumId": presentation["stadiumId"],
        "stadiumName": presentation["stadiumName"],
        "isHomeTeam": True,
    }


# FIFA 20's client expects the featured-squad history to contain only numeric
# squad IDs.  Returning a nested squad there makes the client flatten player
# ratings and item fields into bogus "weeks" (the former 124-week bug).  Squad
# details are supplied separately by /featuredsquad/{id}.
def _totw_gap_definition(week: int) -> dict[str, Any]:
    # FIFA's native carousel numbers cards by their position in the history
    # array, not by metadata returned by /featuredsquad/{id}.  Keeping explicit
    # slots for the five suspended weeks prevents resumed TOTWs from being
    # renumbered on the OUTSIDE selector card (for example TOTW 40 becoming
    # WEEK 35).  These are navigation placeholders only: no team or players are
    # fabricated for weeks in which EA published no FIFA 20 TOTW squad.
    return {
        "id": int(week),
        "archive_index": int(week),
        "published_index": None,
        "actual_week": int(week),
        "real_totw_number": int(week),
        "release_type": "no_release",
        "moments_number": None,
        "display_label": f"WEEK {int(week)}",
        "name": f"FIFA 20 Team of the Week {int(week)} - No Release",
        "shortName": "No Team of the Week Released",
        "formation": "f442",
        "resourceIds": [],
        "startingResourceIds": [],
        "selector_placeholder": True,
    }


def _totw_definition(squad_id: int) -> dict[str, Any]:
    requested = int(squad_id)
    if requested == 0:
        # FIFA's archive contributes one native live slot through the zero-id
        # path and another through the current featuredSquadId.  Use the
        # penultimate published week here so the two live slots become the real
        # Weeks 44 and 45 instead of two extra copies of Week 45.
        requested = _TOTW_ZERO_ALIAS_ID
    if requested in _TOTW_BY_ID:
        return _TOTW_BY_ID[requested]
    if requested in _TOTW_GAP_WEEKS:
        return _totw_gap_definition(requested)
    # Unknown positive IDs must never alias the latest real squad.  A stale
    # client asking for the old 46/47 cache entries should receive an explicit
    # unavailable placeholder rather than another Week 45 duplicate.
    if requested > 0:
        return _totw_gap_definition(requested)
    return _TOTW_BY_ID[_TOTW_LATEST_ID]


def _totw_lineup_resource_ids(definition: dict[str, Any]) -> list[int]:
    # The archive explicitly identifies the published XI. Never promote
    # high-rated reserves into it by picking eleven cards greedily.
    return [int(resource_id) for resource_id in definition["resourceIds"]]


def _native_totw_squad(squad_id: int) -> dict[str, Any]:
    definition = _totw_definition(squad_id)
    real_id = int(definition["id"])
    squad = _native_squad_battle_opponent(0)
    if bool(definition.get("selector_placeholder")):
        # Keep the placeholder structurally safe for the retail client while
        # making it explicitly unavailable.  Most importantly, do not alias a
        # missing week to another published TOTW and do not invent players.
        name = str(definition["name"])
        short_name = str(definition["shortName"])
        squad.update({
            "id": real_id, "squadId": real_id, "featuredSquadId": real_id,
            "archiveIndex": real_id, "publishedIndex": None,
            "actualWeek": real_id, "realTotwNumber": real_id,
            "releaseType": "no_release", "momentsNumber": None,
            "displayLabel": str(definition["display_label"]),
            "featureConsumerId": "sqbttotw",
            "squadName": name, "name": name,
            "clubName": short_name, "displayName": short_name,
            "teamName": short_name, "clubAbbr": "TOTW",
            "ownerName": "EA SPORTS", "personaName": "EA SPORTS",
            "rating": 0, "starRating": 0, "chemistry": 0,
            "players": [], "formation": "f442", "captain": 0,
            "kicktakers": [], "active": False, "valid": False,
            "available": False, "featuredSquadAvailable": False,
            "selectorPlaceholder": True, "noTeamOfTheWeekRelease": True,
        })
        squad["clubInfo"].update({
            "clubName": short_name, "clubAbbr": "TOTW",
            "displayName": short_name, "ownerName": "EA SPORTS",
        })
        return squad
    players = []
    for index, resource_id in enumerate(_totw_lineup_resource_ids(definition)):
        item_id = 970_000_000 + real_id * 100 + index
        item = STATE.build_player_item(resource_id, item_id, "none")
        players.append({
            "index": index, "kitNumber": index + 1,
            "itemData": _native_player_item(item),
        })
    starter_ids = [int(entry["itemData"]["id"]) for entry in players[:11]]
    actual_rating = int(round(sum(
        _int_or(entry["itemData"].get("rating"), 0) for entry in players[:11]
    ) / 11.0)) if players else 0
    name = str(definition["name"])
    short_name = str(definition["shortName"])
    squad.update({
        "id": real_id, "squadId": real_id, "featuredSquadId": real_id,
        "archiveIndex": _int_or(definition.get("archive_index"), real_id),
        "publishedIndex": definition.get("published_index"),
        "actualWeek": _int_or(definition.get("actual_week"), real_id),
        "realTotwNumber": _int_or(definition.get("real_totw_number"), real_id),
        "releaseType": str(definition.get("release_type", "totw")),
        "momentsNumber": definition.get("moments_number"),
        "displayLabel": str(definition.get("display_label", f"WEEK {real_id}")),
        "featureConsumerId": "sqbttotw",
        "squadName": name, "name": name,
        "clubName": short_name, "displayName": short_name,
        "teamName": short_name, "clubAbbr": "TOTW",
        "ownerName": "EA SPORTS", "personaName": "EA SPORTS",
        "rating": actual_rating, "starRating": actual_rating,
        "chemistry": 100, "players": players,
        "formation": str(definition["formation"]),
        "captain": starter_ids[-1] if starter_ids else 0,
        "kicktakers": [
            {"index": index, "id": item_id, "dream": False}
            for index, item_id in enumerate(starter_ids[1:6])
        ],
        "active": True, "valid": True,
    })
    squad["clubInfo"].update({
        "clubName": short_name, "clubAbbr": "TOTW",
        "displayName": short_name, "ownerName": "EA SPORTS",
    })
    return squad


def _totw_featured_history() -> dict[str, Any]:
    # The native FIFA 20 carousel appends two live/current featured-squad cards
    # after this history array.  Therefore history must stop at Week 43: the
    # client's two native slots represent Weeks 44 and 45.  Returning 1..45
    # here created two excess cards (46/47 and 47/47), both resolving to Week
    # 45.  Weeks 33-37 remain explicit no-release slots so the resumed archive
    # still keeps its real historical numbering.
    return {"squads": [int(week) for week in reversed(_TOTW_HISTORY_IDS)]}


def _sb_native_summary_with_state(
        native: dict[str, Any], opponent: dict[str, Any]) -> dict[str, Any]:
    """Overlay round state without changing the proven native summary schema."""
    result = json.loads(json.dumps(native))
    squad = opponent.get("squad", {}) if isinstance(opponent.get("squad"), dict) else {}
    slot = max(0, min(_int_or(opponent.get("id"), 0), len(_SQUAD_BATTLE_PRESETS) - 1))
    club_name = str(opponent.get("clubName", squad.get("clubName", result.get("clubName", "CPU Squad"))))
    club_abbr = str(opponent.get("clubAbbr", squad.get("clubAbbr", result.get("clubAbbr", "CPU"))))
    team_id = _int_or(opponent.get("teamId"), _int_or(squad.get("teamId"), 1))
    result.update({
        "id": slot,
        "squadId": slot,
        "clubName": club_name,
        "clubAbbr": club_abbr,
        "displayName": club_name,
        "teamName": club_name,
        "badgeAssetId": str(team_id),
        "teamId": team_id,
        "teamid": team_id,
        "rating": _int_or(opponent.get("rating"), _int_or(squad.get("rating"), 80)),
        "chemistry": _int_or(opponent.get("chemistry"), _int_or(squad.get("chemistry"), 100)),
        "difficulty": str(squad.get("difficulty", result.get("difficulty", "PROFESSIONAL"))),
        "difficultyName": str(squad.get("difficultyName", result.get("difficultyName", "PROFESSIONAL"))),
        "nation": _int_or(squad.get("nation"), _int_or(result.get("nation"), 14)),
        "location": str(squad.get("location", result.get("location", "INTERNATIONAL"))),
        "ownerName": str(squad.get("ownerName", result.get("ownerName", "FUT Challenger"))),
        "personaName": str(squad.get("personaName", result.get("personaName", "FUT Challenger"))),
        "played": bool(opponent.get("played")),
        "pointsWon": _int_or(opponent.get("pointsWon"), -1),
        "score": _int_or(opponent.get("score"), -1),
        "oppScore": _int_or(opponent.get("oppScore"), -1),
        "penaltyScore": _int_or(opponent.get("penaltyScore"), -1),
        "oppPenaltyScore": _int_or(opponent.get("oppPenaltyScore"), -1),
    })
    for field in (
        "foundationYear", "est", "established", "homeKitId", "awayKitId",
        "homeKitAssetId", "awayKitAssetId", "homeKitTeamId", "awayKitTeamId",
        "homeKitResourceId", "awayKitResourceId", "stadiumAssetId", "stadiumId",
        "stadiumName", "homeKit", "awayKit", "actives", "club",
    ):
        if field in squad:
            result[field] = json.loads(json.dumps(squad[field]))

    starters = [
        row.get("itemData") for row in squad.get("players", [])[:11]
        if isinstance(row, dict) and isinstance(row.get("itemData"), dict)
    ] if isinstance(squad.get("players"), list) else []
    top_players = [
        {"itemData": json.loads(json.dumps(item))}
        for item in sorted(
            starters, key=lambda row: _int_or(row.get("rating"), 0), reverse=True
        )[:3]
    ]
    detail = dict(result.get("sqbt2data", {}))
    detail.update({
        "squadId": slot,
        "teamId": team_id,
        "badgeId": team_id,
        "badgeAssetId": team_id,
        "clubName": club_name,
        "clubAbbr": club_abbr,
        "displayName": club_name,
        "teamName": club_name,
        "ownerName": result["ownerName"],
        "personaName": result["personaName"],
        "nation": result["nation"],
        "location": result["location"],
        "homeKit": _int_or(squad.get("homeKitId"), _int_or(result.get("homeKitId"), 0)),
        "awayKit": _int_or(squad.get("awayKitId"), _int_or(result.get("awayKitId"), 0)),
        "homeKitId": _int_or(squad.get("homeKitId"), _int_or(result.get("homeKitId"), 0)),
        "awayKitId": _int_or(squad.get("awayKitId"), _int_or(result.get("awayKitId"), 0)),
        "homeKitItem": json.loads(json.dumps(squad.get("homeKit", {}))),
        "awayKitItem": json.loads(json.dumps(squad.get("awayKit", {}))),
        "actives": json.loads(json.dumps(squad.get("actives", []))),
        "topPlayers": top_players,
    })
    result["sqbt2data"] = detail
    return result


def _native_squad_battle_hub() -> dict[str, Any]:
    event = _native_squad_battle_event()
    refresh_at = now_s() + 24 * 60 * 60
    opponents: list[dict[str, Any]] = []
    for slot, (club_name, club_abbr, badge_id, target_rating, chemistry, difficulty) in enumerate(_SQUAD_BATTLE_PRESETS):
        ids = _squad_battle_opponent_ids(target_rating, slot)
        ratings = [int(player_meta(rid).get("rating", 0) or 0) for rid in ids]
        presentation = _squad_battle_presentation(badge_id, slot)
        top_idx = sorted(range(min(11, len(ids))), key=lambda i: ratings[i], reverse=True)[:3]
        top_players = [
            {"itemData": _native_player_item(
                STATE.build_player_item(ids[i], 940000 + slot * 100 + i, "none"))}
            for i in top_idx
        ]
        opponents.append({
            "id": slot, "squadId": slot,
            "clubName": club_name, "clubAbbr": club_abbr,
            "displayName": club_name, "teamName": club_name,
            "ownerName": presentation["ownerName"],
            "personaName": presentation["ownerName"],
            "badgeAssetId": str(badge_id), "teamId": badge_id, "teamid": badge_id,
            "rating": int(round(sum(ratings[:11]) / 11.0)) if ratings else target_rating,
            "chemistry": chemistry, "formation": "f442",
            "difficulty": difficulty, "difficultyName": difficulty,
            "nation": presentation["nation"], "location": presentation["location"],
            "foundationYear": presentation["foundationYear"],
            "est": presentation["est"], "established": presentation["established"],
            "homeKitId": presentation["homeKitId"], "awayKitId": presentation["awayKitId"],
            "homeKitAssetId": presentation["homeKitAssetId"],
            "awayKitAssetId": presentation["awayKitAssetId"],
            "homeKit": presentation["homeKit"], "awayKit": presentation["awayKit"],
            "actives": presentation["actives"], "club": presentation["club"],
            "isHomeTeam": True,
            "basePointsWin": 400 + slot * 150, "basePointsLoss": 100,
            "sqbt2data": {
                "squadId": slot, "teamId": badge_id, "badgeId": badge_id,
                "badgeAssetId": badge_id,
                "stadiumAssetId": presentation["stadiumId"],
                "stadiumId": presentation["stadiumId"],
                "stadiumName": presentation["stadiumName"],
                "clubName": club_name, "clubAbbr": club_abbr,
                "displayName": club_name, "teamName": club_name,
                "ownerName": presentation["ownerName"],
                "personaName": presentation["ownerName"],
                "nation": presentation["nation"],
                "location": presentation["location"],
                "foundationYear": presentation["foundationYear"],
                "est": presentation["established"],
                "established": presentation["established"],
                "establishedTime": presentation["established"],
                "clubEstablished": presentation["established"],
                # This native summary screen expects scalar kit resources.
                # Full item documents remain available under *KitItem.
                "homeKit": presentation["homeKitId"],
                "awayKit": presentation["awayKitId"],
                "homeKitId": presentation["homeKitId"],
                "awayKitId": presentation["awayKitId"],
                "homeKitResourceId": presentation["homeKitResourceId"],
                "awayKitResourceId": presentation["awayKitResourceId"],
                "homeKitAssetId": presentation["homeKitAssetId"],
                "awayKitAssetId": presentation["awayKitAssetId"],
                "homeKitTeamId": presentation["homeKitTeamId"],
                "awayKitTeamId": presentation["awayKitTeamId"],
                "homeKitItem": presentation["homeKit"],
                "awayKitItem": presentation["awayKit"],
                "actives": presentation["actives"],
                "isHomeTeam": True,
                "topPlayers": top_players,
            },
        })
    payload = {
        "currentTime": event["currentTime"],
        "startTime": event["startTime"], "endTime": event["endTime"],
        "serverCrtTime": event["serverTimeInSeconds"], "crtTime": event["crtTime"],
        "eventStartTime": event["eventStartTime"], "eventEndTime": event["eventEndTime"],
        "nextEventStartTime": event["nextEventStartTime"],
        "secondsUntilStart": event["secondsUntilStart"], "secondsUntilEnd": event["secondsUntilEnd"],
        "timeUntilStart": event["timeUntilStart"], "timeUntilEnd": event["timeUntilEnd"],
        "untilStartSeconds": event["untilStartSeconds"], "untilEndSeconds": event["untilEndSeconds"],
        "gamesPlayed": event["gamesPlayed"], "gamesRemaining": event["gamesRemaining"],
        "matchesPlayed": event["matchesPlayed"], "remainingMatches": event["remainingMatches"],
        "numberOfMatchesPlayed": event["numberOfMatchesPlayed"],
        "squadsRemaining": event["squadsRemaining"], "totalGames": event["totalGames"],
        "maxMatches": event["maxMatches"], "maxNumberOfMatches": event["maxNumberOfMatches"],
        "maxRegularGames": event["maxRegularGames"],
        "sqbtEventId": event["eventId"], "sqbtMatchDifficulty": 0,
        "sqbtOpponentSquadId": -1, "sqbtOppid": -1,
        "squadBattleEnabled": True, "squadBattleCouchPlayEnabled": True,
        "active": True, "isActive": True, "enabled": True, "available": True,
        "status": "ACTIVE", "squadBattlesScore": 0,
        "sqbtEvent": event, "sqbt2data": event, "squadBattle": event,
        "sqbtOppSquadList": {
            "nextSquadRefreshTimeStamp": refresh_at,
            "sqbtOppSquads": opponents,
        },
        "squads": [_native_squad_battle_opponent(s) for s in range(len(_SQUAD_BATTLE_PRESETS))],
    }
    # Keep the proven native document, but overlay the local weekly progress
    # and reward availability maintained by the newer implementation.
    state = _sb_state()
    points = _int_or(state.get("battlePoints"), 0)
    regular_played = _int_or(state.get("pointMatchesPlayed"), 0)
    tier_name, tier_level = _sb_tier(points)
    state_opponents = [
        entry for entry in state.get("opponents", []) if isinstance(entry, dict)
    ]
    safe_summaries: list[dict[str, Any]] = []
    native_squads: list[dict[str, Any]] = []
    for slot, summary in enumerate(opponents):
        entry = state_opponents[slot] if slot < len(state_opponents) else {}
        safe_summaries.append(_sb_native_summary_with_state(summary, entry))
        squad = entry.get("squad", {}) if isinstance(entry.get("squad"), dict) else {}
        native_squads.append(
            json.loads(json.dumps(squad)) if squad
            else _native_squad_battle_opponent(slot)
        )
    payload["sqbtOppSquadList"] = {
        "nextSquadRefreshTimeStamp": _int_or(state.get("nextRefresh"), refresh_at),
        "sqbtOppSquads": safe_summaries,
    }
    payload["squads"] = native_squads
    payload.update({
        "gamesPlayed": regular_played,
        "numberOfMatchesPlayed": regular_played,
        "matchesPlayed": regular_played,
        "gamesRemaining": max(0, _SB_POINT_MATCH_LIMIT - regular_played),
        "remainingMatches": max(0, _SB_POINT_MATCH_LIMIT - regular_played),
        "squadBattlesScore": points, "score": points,
        "rank": _sb_rank(points), "userTierLevel": tier_level,
        "tierName": tier_name,
        "isPrizeAvailable": bool(state.get("pendingPrize")),
        "maxRegularGames": _SB_POINT_MATCH_LIMIT,
        # Primitive feature identifiers are part of the proven FIFA 20 hub
        # contract. The earlier crash came from replacing regular opponent
        # summaries with a different nested schema, not from these flags.
        "featuredSquadAvailable": not bool(state.get("customFeaturedPlayed")),
        "featuredSquadId": _SB_FEATURED_CUSTOM_ID,
        "featureConsumerId": "sqbt",
        "totwFeaturedSquadAvailable": not bool(state.get("featuredPlayed")),
        "totwFeaturedSquadId": _TOTW_LATEST_ID,
    })
    return payload


def _native_squad_battle_match_response(opponent_slot: int) -> dict[str, Any]:
    squad = _native_squad()
    actives = _active_club_items()
    squad["actives"] = actives
    squad["club"] = [dict(item) for item in actives]
    squad["stadiumAssetId"] = _DRAFT_STADIUM_ID
    squad["stadiumId"] = _DRAFT_STADIUM_ID
    squad["home"] = True
    squad["isHomeTeam"] = True
    opponent = _native_squad_battle_opponent(opponent_slot)
    opponent["isHomeTeam"] = False
    opponent["home"] = False
    return {
        "squad": squad, "homeSquad": squad,
        "opponentSquad": opponent, "awaySquad": opponent, "opponent": opponent,
        "opponentTeamId": int(opponent_slot),
        "isHomeTeam": True, "home": True, "startDateTime": now_s(),
        "stadiumAssetId": _DRAFT_STADIUM_ID, "stadiumId": _DRAFT_STADIUM_ID,
    }


# ---- FIFA 20 Squad Battles -------------------------------------------------
# This is a local, persistent simulation of the original weekly mode: four CPU
# opponents per refresh, selectable difficulty, battle points/ranks, Top 100,
# match settlement, and a claimable reward after the seven-day competition.
_SB_STATE_KEY = "squad_battles_v1"
_SB_WEEK_SECONDS = 7 * 24 * 60 * 60
_SB_POINT_MATCH_LIMIT = 40
_SB_FEATURED_CUSTOM_ID = 9000
_SB_DIFFICULTIES = (
    ("BEGINNER", 0.5, 0.20),
    ("AMATEUR", 0.6, 0.25),
    ("SEMI_PRO", 0.7, 0.40),
    ("PROFESSIONAL", 1.0, 0.50),
    ("WORLD_CLASS", 1.6, 0.60),
    ("LEGENDARY", 2.1, 0.70),
    ("ULTIMATE", 2.6, 0.70),
)
_SB_TIERS = (
    (40000, "ELITE 1", 11), (25000, "ELITE 2", 10),
    (15000, "ELITE 3", 9), (9000, "GOLD 1", 8),
    (6000, "GOLD 2", 7), (3500, "GOLD 3", 6),
    (2500, "SILVER 1", 5), (1500, "SILVER 2", 4),
    (750, "SILVER 3", 3), (350, "BRONZE 1", 2),
    (100, "BRONZE 2", 1), (0, "BRONZE 3", 0),
)
_SB_REWARDS = {
    # Bronze 3 through Elite 1, matching FIFA 20's weekly coin and pack table.
    0: (0, [1019]),
    1: (0, [1024]),
    2: (800, [1024, 1024]),
    3: (1500, [1024, 1005]),
    4: (4000, [1024, 1005, 1005]),
    5: (7000, [1016, 1016]),
    6: (10000, [1016, 1025]),
    7: (11000, [1025, 1013]),
    8: (11000, [1013, 1016]),
    9: (12000, [1013, 1013, 1008]),
    10: (15000, [1008, 1008, 1013]),
    11: (30000, [1009, 1009]),
}
_SB_CLUB_ROUNDS = (
    ((1, "North London XI", "NLX"), (10, "Sky Blues United", "SBU"),
     (22, "Yellow Wall FC", "YWF"), (243, "Royal Champions", "ROY")),
    ((9, "Merseyside Reds", "LIV"), (21, "Bavarian Stars", "FCB"),
     (241, "Catalunya XI", "BAR"), (73, "Paris Elite", "PSG")),
    ((5, "West London Blues", "CHE"), (11, "Manchester Reds", "MUN"),
     (18, "North London White", "TOT"), (240, "Madrid Rojiblancos", "ATM")),
    ((44, "Milano Blue", "INT"), (47, "Milano Red", "MIL"),
     (95, "Midlands Foxes", "LEI"), (9, "Anfield Founders", "LFC")),
)
_sb_lock = threading.RLock()


def _sb_tier(points: Any) -> tuple[str, int]:
    value = max(0, _int_or(points, 0))
    for threshold, name, level in _SB_TIERS:
        if value >= threshold:
            return name, level
    return "BRONZE 3", 0


def _sb_new_state(pending_prize: dict[str, Any] | None = None) -> dict[str, Any]:
    current = now_s()
    return {
        "schema": 1,
        "competitionId": current,
        "competitionStart": current,
        "competitionEnd": current + _SB_WEEK_SECONDS,
        "refreshId": 0,
        "refreshesUsed": 0,
        "generatedAt": 0,
        "nextRefresh": current,
        "opponents": [],
        "selectedOpponentId": 0,
        "selectedDifficulty": 3,
        "matchInProgress": False,
        "lastMatchStart": 0,
        "lastSettledMatchStart": 0,
        "lastMatchResponse": {},
        "battlePoints": 0,
        "matchesPlayed": 0,
        "pointMatchesPlayed": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "featuredPlayed": False,
        "featuredPoints": 0,
        "featuredResult": "",
        "customFeaturedPlayed": False,
        "customFeaturedPoints": 0,
        "customFeaturedResult": "",
        "selectedFeatured": False,
        "selectedFeaturedType": "",
        "pendingPrize": pending_prize or {},
    }


def _sb_save(state: dict[str, Any]) -> None:
    STATE.set(_SB_STATE_KEY, state)


def _sb_generate_opponents(state: dict[str, Any], refreshed: bool = False) -> None:
    serial = max(1, _int_or(state.get("refreshId"), 0) + 1)
    opponents: list[dict[str, Any]] = []
    club_round = _SB_CLUB_ROUNDS[(serial - 1) % len(_SB_CLUB_ROUNDS)]
    for index, (team_id, club_name, club_abbr) in enumerate(club_round):
        # FIFA 20's native opponent carousel uses the fixed slot ids 0..3.
        # Feeding it large persistence ids changes the item shape used by the
        # proven retail decoder and caused the supplied build to terminate on
        # /sqbt/user/hub before any opponent detail request was made.
        squad_id = index
        squad = _native_squad_battle_opponent(
            index, serial, (team_id, club_name, club_abbr)
        )
        squad.update({
            "id": squad_id,
            "squadId": squad_id,
            "personaId": 1_100_000_000 + serial * 10 + index,
            "name": club_name,
            "squadName": club_name,
            "clubName": club_name,
            "clubAbbr": club_abbr,
            "displayName": club_name,
            "teamName": club_name,
            "squadType": "REGULAR_SQUAD",
            "active": True,
            "valid": True,
            "home": True,
            "isHomeTeam": True,
        })
        opponents.append({
            "id": squad_id,
            "squadId": squad_id,
            "teamId": team_id,
            "clubName": club_name,
            "clubAbbr": club_abbr,
            "badgeAssetId": team_id,
            "nation": int(squad.get("nation", 14) or 14),
            "rating": _int_or(squad.get("rating"), int(_DRAFT_PRESETS[index][0])),
            "chemistry": _int_or(squad.get("chemistry"), int(_DRAFT_PRESETS[index][1])),
            "roundSerial": serial,
            "played": False,
            "pointsWon": -1,
            "score": -1,
            "oppScore": -1,
            "penaltyScore": -1,
            "oppPenaltyScore": -1,
            "squad": squad,
        })
    state["refreshId"] = serial
    if refreshed:
        state["refreshesUsed"] = _int_or(state.get("refreshesUsed"), 0) + 1
    state["generatedAt"] = now_s()
    state["nextRefresh"] = now_s() + 300
    state["opponents"] = opponents
    state["selectedOpponentId"] = opponents[0]["id"] if opponents else 0


def _sb_state() -> dict[str, Any]:
    with _sb_lock:
        raw = STATE.get(_SB_STATE_KEY, {})
        state = dict(raw) if isinstance(raw, dict) else _sb_new_state()
        changed = not isinstance(raw, dict) or not state
        if not state:
            state = _sb_new_state()
            changed = True
        if _int_or(state.get("competitionEnd"), 0) <= now_s():
            pending: dict[str, Any] = {}
            if _int_or(state.get("matchesPlayed"), 0) > 0:
                tier_name, tier_level = _sb_tier(state.get("battlePoints", 0))
                coins, packs = _SB_REWARDS[tier_level]
                pending = {
                    "tierName": tier_name,
                    "tierLevel": tier_level,
                    "coins": coins,
                    "packs": packs,
                    "competitionId": _int_or(state.get("competitionId"), 0),
                }
            state = _sb_new_state(pending)
            changed = True
        opponents = state.get("opponents")
        legacy_ids = (
            isinstance(opponents, list)
            and bool(opponents)
            and any(
                _int_or(entry.get("id"), -1) != index
                for index, entry in enumerate(opponents[:4])
                if isinstance(entry, dict)
            )
        )
        if not isinstance(opponents, list) or not opponents or legacy_ids:
            _sb_generate_opponents(state)
            changed = True
            if legacy_ids:
                log.info(
                    "SQUAD-BATTLES migrated opponent ids to native slots; "
                    "weekly points and match totals preserved"
                )
        for key, default in (
            ("featuredPlayed", False), ("featuredPoints", 0),
            ("featuredResult", ""), ("customFeaturedPlayed", False),
            ("customFeaturedPoints", 0), ("customFeaturedResult", ""),
            ("selectedFeatured", False), ("selectedFeaturedType", ""),
        ):
            if key not in state:
                state[key] = default
                changed = True
        if changed:
            _sb_save(state)
        return state


def _sb_opponent_descriptor(opponent: dict[str, Any]) -> dict[str, Any]:
    squad = opponent.get("squad", {}) if isinstance(opponent.get("squad"), dict) else {}
    club_name = str(opponent.get("clubName", "CPU Squad"))
    club_abbr = str(opponent.get("clubAbbr", "CPU"))
    owner_name = str(squad.get("ownerName", "FUT Challenger"))
    home_resource = _int_or(squad.get("homeKitResourceId", squad.get("homeKitId")), 0)
    away_resource = _int_or(squad.get("awayKitResourceId", squad.get("awayKitId")), 0)
    home_team = _int_or(squad.get("homeKitTeamId", squad.get("teamId")), 0)
    away_team = _int_or(squad.get("awayKitTeamId", squad.get("teamId")), 0)
    established = _int_or(squad.get("established"), 1567296000)
    starters = [
        slot.get("itemData") for slot in squad.get("players", [])[:11]
        if isinstance(slot, dict) and isinstance(slot.get("itemData"), dict)
    ] if isinstance(squad.get("players"), list) else []
    top_players = [
        {"itemData": dict(item)}
        for item in sorted(
            starters,
            key=lambda row: _int_or(row.get("rating"), 0),
            reverse=True,
        )[:3]
    ]
    descriptor = {
        "id": _int_or(opponent.get("id"), 0),
        "squadId": _int_or(opponent.get("squadId"), 0),
        "teamId": _int_or(opponent.get("teamId"), home_team),
        "clubName": club_name,
        "clubAbbr": club_abbr,
        "displayName": club_name,
        "teamName": club_name,
        "ownerName": owner_name,
        "personaName": owner_name,
        # The retail hub summary serialises badges as strings even though the
        # nested team/kit ids remain numbers.
        "badgeAssetId": str(_int_or(opponent.get("badgeAssetId"), 1)),
        "chemistry": _int_or(opponent.get("chemistry"), 100),
        "rating": _int_or(opponent.get("rating"), 80),
        "nation": _int_or(opponent.get("nation"), 14),
        "location": str(squad.get("location", "INTERNATIONAL")),
        "foundationYear": _int_or(squad.get("foundationYear"), 2020),
        "est": _int_or(squad.get("est"), 2020),
        "established": established,
        "establishedTime": established,
        "clubEstablished": established,
        "stadiumAssetId": _int_or(squad.get("stadiumAssetId"), _DRAFT_STADIUM_ID),
        "stadiumId": _int_or(squad.get("stadiumId"), _DRAFT_STADIUM_ID),
        "stadiumName": str(squad.get("stadiumName", "Sanderson Park")),
        "homeKitId": _int_or(squad.get("homeKitId"), 0),
        "awayKitId": _int_or(squad.get("awayKitId"), 0),
        "homeKit": dict(squad.get("homeKit", {})) if isinstance(squad.get("homeKit"), dict) else {},
        "awayKit": dict(squad.get("awayKit", {})) if isinstance(squad.get("awayKit"), dict) else {},
        "actives": list(squad.get("actives", [])) if isinstance(squad.get("actives"), list) else [],
        "basePointsWin": 200,
        "basePointsLoss": 50,
        "pointsWon": _int_or(opponent.get("pointsWon"), -1),
        "score": _int_or(opponent.get("score"), -1),
        "oppScore": _int_or(opponent.get("oppScore"), -1),
        "penaltyScore": _int_or(opponent.get("penaltyScore"), -1),
        "oppPenaltyScore": _int_or(opponent.get("oppPenaltyScore"), -1),
    }
    descriptor["sqbt2data"] = {
        "squadId": descriptor["squadId"],
        "teamId": _int_or(opponent.get("teamId"), home_team),
        "badgeId": descriptor["badgeAssetId"],
        "badgeAssetId": descriptor["badgeAssetId"],
        "clubName": club_name, "clubAbbr": club_abbr,
        "displayName": club_name, "teamName": club_name,
        "ownerName": owner_name, "personaName": owner_name,
        "nation": descriptor["nation"], "location": descriptor["location"],
        "foundationYear": descriptor["foundationYear"],
        "est": established, "established": established,
        "establishedTime": established, "clubEstablished": established,
        "stadiumAssetId": descriptor["stadiumAssetId"],
        "stadiumId": descriptor["stadiumId"],
        "stadiumName": descriptor["stadiumName"],
        "homeKit": home_resource, "awayKit": away_resource,
        "homeKitId": home_resource, "awayKitId": away_resource,
        "homeKitResourceId": home_resource, "awayKitResourceId": away_resource,
        "homeKitAssetId": home_team, "awayKitAssetId": away_team,
        "homeKitTeamId": home_team, "awayKitTeamId": away_team,
        "homeKitItem": descriptor["homeKit"], "awayKitItem": descriptor["awayKit"],
        "actives": descriptor["actives"], "isHomeTeam": True,
        "topPlayers": top_players,
    }
    return descriptor


def _sb_opponent_list(state: dict[str, Any]) -> dict[str, Any]:
    rows = [_sb_opponent_descriptor(entry) for entry in state.get("opponents", [])]
    return {
        "nextSquadRefreshTimeStamp": _int_or(state.get("nextRefresh"), now_s()),
        "sqbtOppSquads": rows,
    }


def _sb_rank(points: int) -> int:
    # Stable local percentile/rank curve used by the rank and Top 100 screens.
    return max(1, 250_000 - max(0, int(points)) * 6)


def _sb_prize_tiers_payload() -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = []
    upper = 2_147_483_647
    for threshold, name, level in _SB_TIERS:
        coins, packs = _SB_REWARDS[level]
        awards = ([{"type": "coins", "value": coins, "count": 1}] if coins else []) + [
            {
                "type": "pack", "value": pack_id, "halId": pack_id,
                "count": 1, "isUntradeable": True, "loan": 0,
            }
            for pack_id in packs
        ]
        tiers.append({
            "tierEnd": upper,
            "tierEndValue": upper,
            "tierLevel": level,
            "tierStart": threshold,
            "tierStartValue": threshold,
            "tierType": name.replace(" ", "_"),
            "awards": awards,
        })
        upper = max(0, threshold - 1)
    return tiers


def _sb_hub_payload(state: dict[str, Any]) -> dict[str, Any]:
    # All hub aliases must use the one verified FIFA 20 document.  Maintaining
    # a second "new" hub shape here is what allowed the crashing descriptor
    # container to return through alternate /sqbt/v2 paths.
    return _native_squad_battle_hub()


def _sb_feature_stats_payload(
        state: dict[str, Any], consumer: str = "sqbttotw") -> dict[str, Any]:
    consumer = "sqbt" if str(consumer).lower() == "sqbt" else "sqbttotw"
    custom = consumer == "sqbt"
    played = bool(state.get("customFeaturedPlayed" if custom else "featuredPlayed"))
    result = str(state.get(
        "customFeaturedResult" if custom else "featuredResult", ""
    )).upper()
    points = _int_or(state.get(
        "customFeaturedPoints" if custom else "featuredPoints"
    ), 0)
    featured_id = _SB_FEATURED_CUSTOM_ID if custom else _TOTW_LATEST_ID
    return {
        "wins": 1 if result == "WIN" else 0,
        "losses": 1 if result == "LOSS" else 0,
        "draws": 1 if result == "DRAW" else 0,
        "gamesPlayed": 1 if played else 0,
        "matchesPlayed": 1 if played else 0,
        "score": points, "points": points,
        "nextFeatureSquadTimeStamp": _int_or(state.get("competitionEnd"), 0),
        "featuredSquadAvailable": not bool(state.get("featuredPlayed")),
        "available": not bool(state.get("featuredPlayed")),
        "featureConsumerId": consumer,
        "featuredSquadId": featured_id,
    }


def _sb_featured_opponent() -> dict[str, Any]:
    squad = _native_totw_squad(_TOTW_LATEST_ID)
    return {
        "id": _TOTW_LATEST_ID,
        "squadId": _TOTW_LATEST_ID,
        "teamId": _TOTW_LATEST_ID,
        "clubName": str(squad.get("clubName", "FIFA 20 TOTW")),
        "clubAbbr": str(squad.get("clubAbbr", "TOTW")),
        "badgeAssetId": _int_or(squad.get("badgeAssetId"), 0),
        "nation": _int_or(squad.get("nation"), 14),
        "rating": _int_or(squad.get("rating"), 85),
        "chemistry": _int_or(squad.get("chemistry"), 100),
        "played": False,
        "featured": True,
        "pointsWon": -1,
        "score": -1,
        "oppScore": -1,
        "penaltyScore": -1,
        "oppPenaltyScore": -1,
        "squad": squad,
    }


def _sb_custom_featured_opponent(state: dict[str, Any] | None = None) -> dict[str, Any]:
    current = state if isinstance(state, dict) else _sb_state()
    refresh_id = max(1, _int_or(current.get("competitionId"), 1))
    squad = _native_squad_battle_opponent(
        3, refresh_id, (9, "Featured All-Stars", "FAS")
    )
    squad.update({
        "id": _SB_FEATURED_CUSTOM_ID,
        "squadId": _SB_FEATURED_CUSTOM_ID,
        "featuredSquadId": _SB_FEATURED_CUSTOM_ID,
        "featureConsumerId": "sqbt",
        "squadName": "Featured All-Stars",
        "name": "Featured All-Stars",
        "clubName": "Featured All-Stars",
        "clubAbbr": "FAS",
        "displayName": "Featured All-Stars",
        "teamName": "Featured All-Stars",
    })
    if isinstance(squad.get("clubInfo"), dict):
        squad["clubInfo"].update({
            "clubName": "Featured All-Stars",
            "clubAbbr": "FAS",
            "displayName": "Featured All-Stars",
        })
    return {
        "id": _SB_FEATURED_CUSTOM_ID,
        "squadId": _SB_FEATURED_CUSTOM_ID,
        "teamId": 9,
        "clubName": "Featured All-Stars",
        "clubAbbr": "FAS",
        "badgeAssetId": 9,
        "nation": _int_or(squad.get("nation"), 14),
        "rating": _int_or(squad.get("rating"), 90),
        "chemistry": _int_or(squad.get("chemistry"), 100),
        "played": bool(current.get("customFeaturedPlayed")),
        "featured": True,
        "featureConsumerId": "sqbt",
        "pointsWon": _int_or(current.get("customFeaturedPoints"), -1),
        "score": -1,
        "oppScore": -1,
        "penaltyScore": -1,
        "oppPenaltyScore": -1,
        "squad": squad,
    }


def _sb_find_int(payload: Any, names: tuple[str, ...], default: int = 0) -> int:
    if isinstance(payload, dict):
        for name in names:
            if name in payload:
                return _int_or(payload.get(name), default)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _sb_find_int(value, names, -2_147_483_648)
                if found != -2_147_483_648:
                    return found
    elif isinstance(payload, list):
        for value in payload:
            found = _sb_find_int(value, names, -2_147_483_648)
            if found != -2_147_483_648:
                return found
    return default


def _sb_requested_opponent(payload: Any, query: dict[str, list[str]] | None = None) -> int:
    names = (
        "sqbtOpponentSquadId", "sqbtOppid", "opponentId",
        "selectedOpponentId", "opponentSquadId", "squadId",
    )
    result = _sb_find_int(payload, names, 0)
    if result <= 0 and query:
        for name in names:
            values = query.get(name) or query.get(name.lower())
            if values:
                result = _int_or(values[0], 0)
                if result > 0:
                    break
    return result


def _sb_requested_difficulty(payload: Any, default: int = 3) -> int:
    value = _sb_find_int(
        payload,
        ("sqbtMatchDifficulty", "difficultyId", "gameDifficulty", "difficulty"),
        default,
    )
    return max(0, min(value, len(_SB_DIFFICULTIES) - 1))


def _sb_is_match_request(payload: Any, query: dict[str, list[str]] | None = None) -> bool:
    markers = (
        "sqbtEventId", "sqbtOpponentSquadId", "sqbtOppid",
        "sqbtMatchDifficulty", "selectedOpponentId", "opponentSquadId",
    )
    if isinstance(payload, dict):
        if any(marker in payload for marker in markers):
            return True
        return any(
            _sb_is_match_request(value)
            for value in payload.values()
            if isinstance(value, (dict, list))
        )
    if isinstance(payload, list):
        return any(_sb_is_match_request(value) for value in payload)
    return bool(query and any(marker in query for marker in markers))


def _sb_select(state: dict[str, Any], payload: Any, query: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    requested = _sb_requested_opponent(payload, query)
    serialized = json.dumps(payload, sort_keys=True).lower() if payload is not None else ""
    query_consumer = _market_first(query or {}, "featureConsumerId").lower()
    totw_requested = (
        requested == _TOTW_LATEST_ID
        or "sqbttotw" in serialized
        or query_consumer == "sqbttotw"
    )
    custom_requested = (
        requested == _SB_FEATURED_CUSTOM_ID
        or query_consumer == "sqbt"
        or '"featureconsumerid": "sqbt"' in serialized
    )
    if totw_requested:
        if bool(state.get("featuredPlayed")):
            return None
        state["selectedOpponentId"] = _TOTW_LATEST_ID
        state["selectedFeatured"] = True
        state["selectedFeaturedType"] = "sqbttotw"
        state["selectedDifficulty"] = _sb_requested_difficulty(
            payload, _int_or(state.get("selectedDifficulty"), 3)
        )
        _sb_save(state)
        return _sb_featured_opponent()
    if custom_requested:
        if bool(state.get("customFeaturedPlayed")):
            return None
        state["selectedOpponentId"] = _SB_FEATURED_CUSTOM_ID
        state["selectedFeatured"] = True
        state["selectedFeaturedType"] = "sqbt"
        state["selectedDifficulty"] = _sb_requested_difficulty(
            payload, _int_or(state.get("selectedDifficulty"), 3)
        )
        _sb_save(state)
        return _sb_custom_featured_opponent(state)
    opponents = state.get("opponents", [])
    selected = next(
        (entry for entry in opponents if _int_or(entry.get("id"), 0) == requested and not entry.get("played")),
        None,
    )
    if selected is None:
        selected = next((entry for entry in opponents if not entry.get("played")), None)
    if selected is None:
        return None
    state["selectedOpponentId"] = _int_or(selected.get("id"), 0)
    state["selectedFeatured"] = False
    state["selectedFeaturedType"] = ""
    state["selectedDifficulty"] = _sb_requested_difficulty(payload, _int_or(state.get("selectedDifficulty"), 3))
    _sb_save(state)
    return selected


def _sb_selected(state: dict[str, Any]) -> dict[str, Any] | None:
    if bool(state.get("selectedFeatured")):
        feature_type = str(state.get("selectedFeaturedType", "sqbttotw"))
        if feature_type == "sqbt" and not bool(state.get("customFeaturedPlayed")):
            return _sb_custom_featured_opponent(state)
        if feature_type == "sqbttotw" and not bool(state.get("featuredPlayed")):
            return _sb_featured_opponent()
    selected_id = _int_or(state.get("selectedOpponentId"), 0)
    return next(
        (entry for entry in state.get("opponents", []) if _int_or(entry.get("id"), 0) == selected_id),
        None,
    )


def _sb_match_response(state: dict[str, Any], opponent: dict[str, Any]) -> dict[str, Any]:
    own = _native_squad()
    own["home"] = False
    own["isHomeTeam"] = False
    cpu = json.loads(json.dumps(opponent.get("squad", {})))
    cpu["home"] = True
    cpu["isHomeTeam"] = True
    return {
        "squad": own,
        # FIFA's second-stage game-session handshake still reads the legacy
        # homeSquad alias even though the primary payload uses `squad` for the
        # user's selected team.  Keep both keys pointing at the same complete
        # document so manager, kit and stadium data survive the launch.
        "homeSquad": own,
        "opponentTeamId": _int_or(opponent.get("teamId"), 1),
        "opponentSquad": cpu,
        "awaySquad": cpu,
        "opponent": cpu,
        "sqbtEventId": _int_or(state.get("competitionId"), 1),
        "sqbtOpponentSquadId": _int_or(opponent.get("id"), 0),
        "matchDifficulty": _int_or(state.get("selectedDifficulty"), 3),
        "startDateTime": _int_or(state.get("lastMatchStart"), now_s()),
        "stadiumAssetId": _DRAFT_STADIUM_ID,
        "stadiumId": _DRAFT_STADIUM_ID,
        "home": False,
        "isHomeTeam": False,
    }


def _sb_start_match(payload: Any, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    with _sb_lock:
        state = _sb_state()
        opponent = _sb_select(state, payload, query)
        if opponent is None:
            return {"reason": "NO_SQUAD_BATTLE_OPPONENTS", "opponents": []}
        state["matchInProgress"] = True
        state["lastMatchStart"] = max(now_s(), _int_or(state.get("lastMatchStart"), 0) + 1)
        _sb_save(state)
        return _sb_match_response(state, opponent)


def _sb_end_match(payload: Any) -> dict[str, Any]:
    with _sb_lock:
        state = _sb_state()
        match_start = _int_or(state.get("lastMatchStart"), 0)
        if match_start and match_start == _int_or(state.get("lastSettledMatchStart"), 0):
            cached = state.get("lastMatchResponse", {})
            return _with_scmp_live(cached) if isinstance(cached, dict) else _scmp_live_fields()
        if not state.get("matchInProgress"):
            return {"reason": "NO_SQUAD_BATTLE_MATCH", **_credits_payload(),
                    **_scmp_live_fields()}

        goals_for = max(0, _sb_find_int(payload, ("goalsFor", "homeGoals", "goals", "score"), 0))
        goals_against = max(0, _sb_find_int(payload, ("goalsAgainst", "awayGoals", "opponentScore", "oppScore"), 0))
        raw_reason = str(payload.get("endReason", payload.get("result", "")) if isinstance(payload, dict) else "").upper()
        dnf = raw_reason in ("QUIT", "DNF", "DISCONNECT", "ABANDONED")
        if dnf:
            result = "LOSS"
        elif raw_reason in ("WIN", "WON", "VICTORY"):
            result = "WIN"
        elif raw_reason in ("DRAW", "TIE"):
            result = "DRAW"
        elif raw_reason in ("LOSS", "LOST", "DEFEAT"):
            result = "LOSS"
        else:
            result = "WIN" if goals_for > goals_against else ("DRAW" if goals_for == goals_against else "LOSS")

        difficulty = max(0, min(_int_or(state.get("selectedDifficulty"), 3), len(_SB_DIFFICULTIES) - 1))
        multiplier = _SB_DIFFICULTIES[difficulty][1 if result in ("WIN", "DRAW") else 2]
        result_points = 200 if result == "WIN" else (75 if result == "DRAW" else 50)
        performance = max(0, 200 + goals_for * 40 - goals_against * 20)
        opponent = _sb_selected(state)
        is_featured = bool(state.get("selectedFeatured")) or bool(
            opponent and opponent.get("featured")
        )
        point_eligible = is_featured or _int_or(state.get("pointMatchesPlayed"), 0) < _SB_POINT_MATCH_LIMIT
        points = 0 if dnf or not point_eligible else int(round((result_points + performance) * multiplier))
        coins_earned = 0 if dnf else max(150, 325 + goals_for * 40 - goals_against * 10)

        if opponent is not None:
            opponent.update({
                "played": True,
                "pointsWon": points,
                "score": goals_for,
                "oppScore": goals_against,
                "penaltyScore": -1,
                "oppPenaltyScore": -1,
            })
        state["battlePoints"] = _int_or(state.get("battlePoints"), 0) + points
        state["matchesPlayed"] = _int_or(state.get("matchesPlayed"), 0) + 1
        if point_eligible and not is_featured:
            state["pointMatchesPlayed"] = _int_or(state.get("pointMatchesPlayed"), 0) + 1
        if is_featured:
            if str(state.get("selectedFeaturedType", "sqbttotw")) == "sqbt":
                state["customFeaturedPlayed"] = True
                state["customFeaturedPoints"] = points
                state["customFeaturedResult"] = result
            else:
                state["featuredPlayed"] = True
                state["featuredPoints"] = points
                state["featuredResult"] = result
        if result == "WIN":
            state["wins"] = _int_or(state.get("wins"), 0) + 1
        elif result == "DRAW":
            state["draws"] = _int_or(state.get("draws"), 0) + 1
        else:
            state["losses"] = _int_or(state.get("losses"), 0) + 1
        state["matchInProgress"] = False
        state["selectedFeatured"] = False
        state["selectedFeaturedType"] = ""
        state["lastSettledMatchStart"] = match_start
        STATE.record_global_match(
            result,
            _match_report_key(payload, "squad_battles", match_start),
            xp_eligible=not dnf,
        )
        balance = STATE.add_credits(coins_earned) if coins_earned else STATE.credits()
        tier_name, tier_level = _sb_tier(state["battlePoints"])
        response = {
            "endReason": result,
            "secondsPlayed": _sb_find_int(payload, ("secondsPlayed",), 0),
            "matchDifficulty": difficulty,
            "opponentTeamId": _int_or(opponent.get("teamId"), 1) if opponent else 1,
            "completionAward": coins_earned,
            "rewardCoins": coins_earned,
            "totalCoins": coins_earned,
            "credits": balance,
            "coins": balance,
            "squadBattlePoints": points,
            "battlePoints": _int_or(state.get("battlePoints"), 0),
            "score": _int_or(state.get("battlePoints"), 0),
            "rank": _sb_rank(_int_or(state.get("battlePoints"), 0)),
            "userTierLevel": tier_level,
            "tierName": tier_name,
            "won": 1 if result == "WIN" else 0,
            "draw": 1 if result == "DRAW" else 0,
            "loss": 1 if result == "LOSS" else 0,
            "currencies": [
                {"name": "coins", "funds": balance, "finalFunds": balance},
                {"name": "points", "funds": 0, "finalFunds": 0},
            ],
            **_scmp_live_fields(),
        }
        # Retail Squad Battles presents four regular opponents per round.  Once
        # all four are complete, the next visit gets a fresh quartet until the
        # 40 point-bearing matches have been played.
        if (
            state.get("opponents")
            and all(bool(entry.get("played")) for entry in state["opponents"])
            and not is_featured
            and _int_or(state.get("pointMatchesPlayed"), 0) < _SB_POINT_MATCH_LIMIT
        ):
            _sb_generate_opponents(state, refreshed=True)
            response["opponentsRefreshed"] = True
        state["lastMatchResponse"] = response
        _sb_save(state)
        log.warning(
            "SQUAD-BATTLES MATCH_END result=%s difficulty=%s points=%s coins=%s total=%s",
            result, difficulty, points, coins_earned, state["battlePoints"],
        )
        return response


def _sb_leaderboard(state: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(f"fut20-squad-battles-top100:{state.get('competitionId', 1)}")
    entries: list[dict[str, Any]] = []
    # Build a complete CPU Top 100 first.  The local player is added only when
    # their score genuinely displaces one of these entries after sorting.
    for index in range(100):
        score = max(1, 112_000 - index * 710 + rng.randrange(-250, 251))
        entries.append({
            "badge": (1, 10, 22, 243)[index % 4],
            "clubName": f"CPU Elite {index + 1}",
            "est": 2020,
            "insetUrl": "",
            "persona": f"LocalCPU{index + 1:03d}",
            "scoreValue": score,
            "wins": max(1, 40 - index // 4),
        })
    entries.append({
        "badge": 1,
        "clubName": str(CFG["club_name"]),
        "est": 2020,
        "insetUrl": "",
        "persona": str(CFG["display_name"]),
        "scoreValue": _int_or(state.get("battlePoints"), 0),
        "wins": _int_or(state.get("wins"), 0),
    })
    entries.sort(key=lambda entry: (-entry["scoreValue"], entry["persona"]))
    wire = []
    for rank, entry in enumerate(entries[:100], 1):
        wire.append({
            "badge": entry["badge"], "clubName": entry["clubName"],
            "est": entry["est"], "insetUrl": entry["insetUrl"],
            "persona": entry["persona"], "rank": rank,
            "score": {"icon": "", "value": entry["scoreValue"]},
            "tiebreak": {"icon": "", "value": entry["wins"]},
        })
    return {"entries": wire, "leaderboard": wire, "total": len(wire)}


def _sb_prize(state: dict[str, Any], claim: bool) -> dict[str, Any]:
    pending = state.get("pendingPrize", {})
    if not isinstance(pending, dict) or not pending:
        return {"isPrizeAvailable": False, "awards": [], **_credits_payload()}
    packs = [_int_or(pack_id, 0) for pack_id in pending.get("packs", []) if _int_or(pack_id, 0) > 0]
    coins = max(0, _int_or(pending.get("coins"), 0))
    awards = ([{"type": "coins", "value": coins, "count": 1}] if coins else []) + [
        {"type": "pack", "value": pack_id, "halId": pack_id, "count": 1, "isUntradeable": True, "loan": 0}
        for pack_id in packs
    ]
    if claim:
        if coins:
            STATE.add_credits(coins)
        for pack_id in packs:
            STATE.grant_unopened_pack(pack_id, json.dumps({"source": "squad_battles"}))
        state["pendingPrize"] = {}
        _sb_save(state)
    return {
        "isPrizeAvailable": not claim,
        "claimed": claim,
        "tierName": str(pending.get("tierName", "BRONZE 3")),
        "userTierLevel": _int_or(pending.get("tierLevel"), 0),
        "awards": awards,
        "rewards": awards,
        **_credits_payload(),
    }


def _sb_route(low: str, method: str, query: dict[str, list[str]], payload: Any) -> dict[str, Any]:
    with _sb_lock:
        state = _sb_state()
        if low.endswith("/history"):
            consumer = _market_first(query, "featureConsumerId", default="sqbttotw").lower()
            stats = _sb_feature_stats_payload(state, consumer)
            return {"entries": [{
                "featureConsumerId": stats["featureConsumerId"],
                "featuredSquadId": stats["featuredSquadId"],
                "played": stats["gamesPlayed"] > 0,
                "points": stats["points"],
            }]}
        if "featuredsquad" in low:
            consumer = _market_first(query, "featureConsumerId", default="sqbttotw").lower()
            if low.endswith("/stats"):
                return _sb_feature_stats_payload(state, consumer)
            featured = (
                _sb_custom_featured_opponent(state)["squad"]
                if consumer == "sqbt" else _native_totw_squad(_TOTW_LATEST_ID)
            )
            return {
                "featuredSquad": featured,
                **_sb_feature_stats_payload(state, consumer),
            }
        if "prize" in low or "reward" in low:
            return _sb_prize(state, method in ("POST", "PUT"))
        if "leaderboard" in low or "top100" in low or low.endswith("/standing"):
            return _sb_leaderboard(state)
        if low.endswith("/rank"):
            points = _int_or(state.get("battlePoints"), 0)
            tier_name, tier_level = _sb_tier(points)
            return {"rank": _sb_rank(points), "score": points, "userTierLevel": tier_level, "tierName": tier_name}
        opponent_squad_match = re.search(r"/(?:opponentsquad|opponent/squad|squad)/(\d+)$", low)
        if opponent_squad_match:
            requested = _int_or(opponent_squad_match.group(1), 0)
            opponent = next((entry for entry in state.get("opponents", []) if _int_or(entry.get("id"), 0) == requested), None)
            return json.loads(json.dumps(opponent.get("squad", {}))) if opponent else {}
        if low.endswith("/refresh"):
            _sb_generate_opponents(state, refreshed=True)
            _sb_save(state)
            safe_list = _native_squad_battle_hub()["sqbtOppSquadList"]
            return {"sqbtOppSquadList": safe_list, **safe_list}
        if "opponent" in low:
            if method in ("POST", "PUT"):
                selected = _sb_select(state, payload, query)
                safe_list = _native_squad_battle_hub()["sqbtOppSquadList"]
                safe_rows = safe_list.get("sqbtOppSquads", [])
                selected_id = _int_or(state.get("selectedOpponentId"), 0)
                return {
                    "selected": next(
                        (entry for entry in safe_rows
                         if _int_or(entry.get("id"), -1) == selected_id),
                        {},
                    ) if selected else {},
                    "selectedOpponentId": selected_id,
                    "matchDifficulty": _int_or(state.get("selectedDifficulty"), 3),
                    **safe_list,
                }
            safe_list = _native_squad_battle_hub()["sqbtOppSquadList"]
            return {"sqbtOppSquadList": safe_list, **safe_list}
        return _sb_hub_payload(state)


# ---- Division Rivals + FUT Champions native seasonal contract ------------

_SEASONAL_WEEK_SECONDS = 7 * 24 * 60 * 60
_RIVALS_WEEKLY_MATCHES = 30
_CHAMPION_FINALS_MATCHES = 30
_RIVALS_WEEKEND_LEAGUE_THRESHOLD = 2000
_RIVALS_WEEKEND_LEAGUE_POINTS_PER_WIN = 200
_RIVALS_SKILL_PER_WIN = 45
_RIVALS_SKILL_PER_LOSS = -20
_CHAMPION_EVENT_ID = 1
_RIVALS_WEEKLY_RANKS = [
    ("Rank 5", 0, 15_000, 3, 83),
    ("Rank 4", 2_000, 25_000, 4, 84),
    ("Rank 3", 4_000, 40_000, 5, 86),
    ("Rank 2", 6_000, 55_000, 6, 87),
    ("Rank 1", 8_000, 75_000, 7, 89),
    ("Elite", 11_000, 100_000, 8, 90),
]
_CHAMPION_REWARD_BANDS = [
    (1, 27, 30, 65_000, 15, 90), (2, 23, 26, 50_000, 12, 89),
    (3, 20, 22, 43_000, 10, 88), (4, 17, 19, 36_000, 9, 88),
    (5, 14, 16, 30_000, 8, 87), (6, 11, 13, 25_000, 7, 86),
    (7, 8, 10, 20_000, 6, 85), (8, 5, 7, 15_000, 5, 85),
    (9, 4, 4, 12_000, 4, 84), (10, 0, 3, 7_500, 3, 84),
]
_CHAMPION_RANK_NAMES = {
    1: "Rank I", 2: "Rank II", 3: "Rank III", 4: "Rank IV",
    5: "Gold 1", 6: "Gold 2", 7: "Gold 3", 8: "Silver 1",
    9: "Silver 2", 10: "Silver 3",
}


def _seasonal_window(now: int | None = None) -> dict[str, int]:
    current = int(now if now is not None else now_s())
    week_index = current // _SEASONAL_WEEK_SECONDS
    start = week_index * _SEASONAL_WEEK_SECONDS
    end = start + _SEASONAL_WEEK_SECONDS
    return {"weekIndex": week_index, "start": start, "end": end,
            "current": current, "untilStart": 0, "untilEnd": end - current}


def _rivals_progress() -> dict[str, Any]:
    raw = STATE.get("rivals_progress", {}) or {}
    week = _seasonal_window()["weekIndex"]
    if int(raw.get("weekIndex", week)) != week:
        raw = {"division": int(raw.get("division", 10) or 10),
               "rank": int(raw.get("rank", 5) or 5),
               "skillRating": int(raw.get("skillRating", 1000) or 1000),
               "wins": int(raw.get("wins", 0) or 0),
               "draws": int(raw.get("draws", 0) or 0),
               "losses": int(raw.get("losses", 0) or 0),
               "titleWins": int(raw.get("titleWins", 0) or 0)}
    return {
        "division": int(raw.get("division", 10) or 10),
        "rank": int(raw.get("rank", 5) or 5),
        "skillRating": int(raw.get("skillRating", 1000) or 1000),
        "wins": int(raw.get("wins", 0) or 0),
        "draws": int(raw.get("draws", 0) or 0),
        "losses": int(raw.get("losses", 0) or 0),
        "titleWins": int(raw.get("titleWins", 0) or 0),
        "weeklyWins": int(raw.get("weeklyWins", 0) or 0),
        "weeklyGames": int(raw.get("weeklyGames", 0) or 0),
        "weeklyPoints": int(raw.get("weeklyPoints", 0) or 0),
        "weekIndex": week,
        "rewardsClaimedWeek": int(raw.get("rewardsClaimedWeek", -1)),
    }


def _champion_progress() -> dict[str, Any]:
    raw = STATE.get("champion_progress", {}) or {}
    week = _seasonal_window()["weekIndex"]
    if int(raw.get("weekIndex", week)) != week:
        raw = {}
    return {"wins": int(raw.get("wins", 0) or 0),
            "losses": int(raw.get("losses", 0) or 0),
            "games": int(raw.get("games", 0) or 0), "weekIndex": week,
            "rewardsClaimedWeek": int(raw.get("rewardsClaimedWeek", -1))}


def _seasonal_mode() -> str:
    return str(STATE.get("seasonal_mode", "") or "")


def _seasonal_set_mode(mode: str) -> None:
    STATE.set("seasonal_mode", mode)


def _match_report_key(payload: Any, mode: str, fallback: Any = "") -> str:
    body = payload if isinstance(payload, dict) else {}
    identifier = _sb_find_int(body, ("gid", "gameId", "matchReportId", "reportId"), 0)
    if identifier > 0:
        return f"{mode}:{identifier}"
    stable = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return f"{mode}:{fallback}:{sha1_text(stable)}"


def _seasonal_record_match_end(reason: str, report_key: str = "") -> dict[str, Any] | None:
    mode = _seasonal_mode()
    result = str(reason).upper()
    won = result == "WIN"
    drawn = result == "DRAW"
    if report_key and not STATE.record_global_match(
        result, report_key, xp_eligible=result in ("WIN", "DRAW", "LOSS")
    ):
        if mode == "rivals":
            p = _rivals_progress()
            return {"mode": "rivals", "weeklyWins": p["weeklyWins"],
                    "weeklyGames": p["weeklyGames"], "division": p["division"],
                    "rivalsScore": p["skillRating"],
                    "weekendLeaguePoints": p["weeklyPoints"]}
        if mode == "champion":
            p = _champion_progress()
            return {"mode": "champion", "gamesWon": p["wins"], "gamesPlayed": p["games"]}
    if mode == "rivals":
        p = _rivals_progress()
        p["weeklyGames"] += 1
        if won:
            p["weeklyWins"] += 1
            p["wins"] += 1
            p["weeklyPoints"] += _RIVALS_WEEKEND_LEAGUE_POINTS_PER_WIN
            p["skillRating"] += _RIVALS_SKILL_PER_WIN
            if p["skillRating"] >= 1250 and p["division"] > 1:
                p["division"] -= 1
                p["skillRating"] = 1000
        elif drawn:
            p["draws"] += 1
        else:
            p["losses"] += 1
            p["skillRating"] = max(0, p["skillRating"] + _RIVALS_SKILL_PER_LOSS)
        STATE.set("rivals_progress", p)
        return {"mode": "rivals", "weeklyWins": p["weeklyWins"],
                "weeklyGames": p["weeklyGames"], "division": p["division"],
                "rivalsScore": p["skillRating"],
                "weekendLeaguePoints": p["weeklyPoints"]}
    if mode == "champion":
        p = _champion_progress()
        p["games"] += 1
        p["wins" if won else "losses"] += 1
        STATE.set("champion_progress", p)
        return {"mode": "champion", "gamesWon": p["wins"], "gamesPlayed": p["games"]}
    return None


def _rivals_hub_json() -> dict[str, Any]:
    w, p = _seasonal_window(), _rivals_progress()
    games_left = max(0, _RIVALS_WEEKLY_MATCHES - p["weeklyGames"])
    return {
        "rivalsEventId": w["weekIndex"], "seasonId": 0,
        "crtRivalDivision": p["division"], "crtDivision": p["division"],
        "currentDivision": p["division"], "divisionId": p["division"],
        "division": p["division"],
        "rivalsScore": p["skillRating"], "skillPoints": p["skillRating"],
        "skillRatingScore": p["skillRating"], "relegationBuffer": 3,
        "eloThresholdUp": 250, "eloThresholdDown": -100,
        "currentTime": w["current"], "serverTime": w["current"],
        "serverCrtTime": w["current"], "serverTimeInSeconds": w["current"],
        "crtTime": w["current"], "startTime": w["start"], "endTime": w["end"],
        "startTimeStamp": w["start"], "endTimeStamp": w["end"],
        "secondsUntilStart": 0, "secondsUntilEnd": w["untilEnd"],
        "untilStartSeconds": 0, "untilEndSeconds": w["untilEnd"],
        "nextReset": w["end"], "nextResetTime": w["end"], "weeklyResetTime": w["end"],
        "state": "ACTIVE", "status": "ACTIVE", "active": True,
        "isActive": True, "enabled": True, "available": True,
        "competitionClosed": False,
        "gamesPlayed": p["weeklyGames"], "rivalGamesPlayed": p["weeklyGames"],
        "numberOfMatchesPlayed": p["weeklyGames"], "matchesPlayed": p["weeklyGames"],
        "weeklyGamesPlayed": p["weeklyGames"], "weeklyWins": p["weeklyWins"],
        "weeklyDraws": 0, "weeklyLosses": max(0, p["weeklyGames"] - p["weeklyWins"]),
        "gamesWon": p["weeklyWins"], "gamesLost": max(0, p["weeklyGames"] - p["weeklyWins"]),
        "gamesDraw": 0, "gamesRemaining": games_left,
        "weeklyGamesRemaining": games_left, "remainingMatches": games_left,
        "maxMatches": _RIVALS_WEEKLY_MATCHES, "maxWeeklyGames": _RIVALS_WEEKLY_MATCHES,
        "maxRegularGames": _RIVALS_WEEKLY_MATCHES,
        "maxNumberOfMatches": _RIVALS_WEEKLY_MATCHES,
        "wins": p["wins"], "draws": p["draws"], "losses": p["losses"],
        "titleWins": p["titleWins"],
        "inPlacement": False, "inPlacementMatches": False, "placementComplete": True,
        "placementMatchesRequired": 5, "placementMatchesPlayed": 5,
        "gamesPlayedPlacement": 5, "rivalsNumberOfSqbtPlacementMatches": 5,
        "rivalsPlacementSqbtGamesPlayed": 5,
        "rivalsSqbtPlacementMatchesDisplayThreshold": 5,
        "rivalsEventMatchesSoftCap": _RIVALS_WEEKLY_MATCHES,
        "rivalsEventMatchesSoftCapMax": _RIVALS_WEEKLY_MATCHES,
        "rivalsThresholdBeforeSoftCap": _RIVALS_WEEKLY_MATCHES,
        "rivalsSkillPointsCapValue": 5000,
        "weekendLeagueEnabled": True, "weekendLeaguePoints": p["weeklyPoints"],
        "weekendLeaguePointsForWin": _RIVALS_WEEKEND_LEAGUE_POINTS_PER_WIN,
        "weekendLeagueThreshold": _RIVALS_WEEKEND_LEAGUE_THRESHOLD,
        "weekendLeagueGamesPlayed": p["weeklyGames"],
        "qualifiedInWeekendLeague": p["weeklyPoints"] >= _RIVALS_WEEKEND_LEAGUE_THRESHOLD,
        "prizes": {"prizeTiers": []}, "prizesInError": [],
        "prizesRivalPoints": p["weeklyPoints"], "unclaimedPrizeWaiting": False,
        "weeklyRewardsAutoClaimed": True, "hasRewardsToClaim": False,
        "needsAutoClaim": False, "needsMilestonesAutoClaim": False,
        "milestones": [], "unclaimedRewards": [], "pendingRewards": [],
        "rewardSummary": {"unclaimed": 0, "claimed": 0},
        "winForm": 1 if p["weeklyWins"] else 0, "userForm": [],
        "wasRefreshed": False, "opponent": None, "matchInProgress": False,
        "prizeSet": [], "divisionsList": [],
        "rivalPoints": p["weeklyPoints"],
    }


def _champion_tier_for_wins(wins: int) -> int:
    for level, lo, hi, _coins, _count, _rating in _CHAMPION_REWARD_BANDS:
        if lo <= wins <= hi:
            return level
    return 10


def _champion_prize_tiers() -> list[dict[str, Any]]:
    tiers = []
    for level, lo, hi, coins, count, rating in _CHAMPION_REWARD_BANDS:
        name = _CHAMPION_RANK_NAMES[level]
        tiers.append({
            "tierType": "NUMBER_OF_WINS_TYPE", "tierLevel": level,
            "tierStart": lo, "tierEnd": hi,
            "name": name, "rankName": name, "title": name,
            "coins": coins, "coinsReward": coins,
            "awards": [
                {"halId": 0, "assetId": 0, "awardType": "coin",
                 "awardValue": coins, "awardCount": 1},
                {"halId": 0, "assetId": 0, "awardType": "players",
                 "awardValue": rating, "awardCount": count},
            ],
        })
    return tiers


def _champion_hub_json() -> dict[str, Any]:
    w, p = _seasonal_window(), _champion_progress()
    played, won = p["games"], p["wins"]
    remaining = max(0, _CHAMPION_FINALS_MATCHES - played)
    remaining_games = {
        "remainingMatches": remaining, "matchesRemaining": remaining,
        "gamesRemaining": remaining, "remainingGames": remaining,
        "numGames": remaining, "numGamesRemaining": remaining,
        "regularGamesRemaining": remaining, "regularMatchesRemaining": remaining,
        "weekendLeagueGamesRemaining": remaining, "gamesLeft": remaining,
        "numberOfGamesRemaining": remaining, "numberOfMatchesRemaining": remaining,
    }
    max_games = {
        "maxMatches": _CHAMPION_FINALS_MATCHES,
        "maxNumberOfMatches": _CHAMPION_FINALS_MATCHES,
        "maxGames": _CHAMPION_FINALS_MATCHES, "numMaxGames": _CHAMPION_FINALS_MATCHES,
        "totalGames": _CHAMPION_FINALS_MATCHES, "totalMatches": _CHAMPION_FINALS_MATCHES,
        "numberOfGames": _CHAMPION_FINALS_MATCHES,
        "regularGames": _CHAMPION_FINALS_MATCHES,
        "numRegularGames": _CHAMPION_FINALS_MATCHES,
    }
    league = {
        "id": _CHAMPION_EVENT_ID, "championEventId": _CHAMPION_EVENT_ID,
        "championEventType": 0, "localizedName": "FUT_CHAMPIONS_WKND_LG",
        "currentTime": w["current"], "startTime": w["start"], "endTime": w["end"],
        "serverCrtTime": w["current"], "secondsUntilStart": 0,
        "secondsUntilEnd": w["untilEnd"], "untilStartSeconds": 0,
        "untilEndSeconds": w["untilEnd"], "nextEventStartTime": w["end"] + 1,
        "registrationCloseTime": w["end"], "minMatchesToRank": 1,
        "gamesPlayed": played, "gamesWon": won, "matchesPlayed": played,
        "eligibilities": [], "elgOperation": "AND",
        "prizeTiers": _champion_prize_tiers(), "qualifierTournaments": [],
        **max_games, **remaining_games,
    }
    user_stats = {
        "championEventId": _CHAMPION_EVENT_ID,
        "qualifiedChampionEventId": _CHAMPION_EVENT_ID,
        "gamesPlayed": played, "gamesWon": won,
        "gamesLost": max(0, played - won), "matchesPlayed": played,
        "rank": 0, "ranking": 0, "score": won,
        "tierLevel": 0, "userTierLevel": 0,
        "expectedTierLevel": _champion_tier_for_wins(won),
        "predictedTierLevel": _champion_tier_for_wins(won),
        "winForm": 1 if won else 0, **max_games, **remaining_games,
    }
    return {
        "activeChampionLeagues": [league], "previousChampionEvents": [],
        "upcomingChampionEvents": [], "unclaimedPrizesChampionEvents": [],
        "qualifiedChampionLeagueIds": [_CHAMPION_EVENT_ID],
        "qualifiedChampionEventId": _CHAMPION_EVENT_ID,
        "qualifiedInWeekendLeague": True, "hasEntry": True, "entryTokens": 1,
        "registered": True, "isRegistered": True,
        "currentChampionEvent": {
            "id": _CHAMPION_EVENT_ID, "championEventId": _CHAMPION_EVENT_ID,
            "championEventType": 0, "type": 0, "state": "ACTIVE",
            "startTime": w["start"], "endTime": w["end"],
            "currentTime": w["current"], "gamesPlayed": played,
            "gamesWon": won, **max_games, **remaining_games,
        },
        "userRegistration": {"competitionCountryCode": "EG",
                             "competitionRegion": "ME", "registered": True,
                             **max_games, **remaining_games},
        "userStats": [user_stats], "championEventId": _CHAMPION_EVENT_ID,
        "currentTime": w["current"], "startTime": w["start"], "endTime": w["end"],
        "secondsUntilEnd": w["untilEnd"], "untilEndSeconds": w["untilEnd"],
        "state": "ACTIVE", "status": "ACTIVE", "active": True,
        "competitionClosed": False, "gamesPlayed": played,
        "gamesWon": won, "matchesPlayed": played,
        "prizeTiers": _champion_prize_tiers(), "unclaimedRewards": [],
        "hasRewardsToClaim": False, **max_games, **remaining_games,
    }


def _champion_stats_json() -> dict[str, Any]:
    p = _champion_progress()
    remaining = max(0, _CHAMPION_FINALS_MATCHES - p["games"])
    return {
        "championEventId": _CHAMPION_EVENT_ID,
        "qualifiedChampionEventId": _CHAMPION_EVENT_ID,
        "period": "curr", "type": "base",
        "gamesPlayed": p["games"], "gamesWon": p["wins"],
        "gamesLost": max(0, p["games"] - p["wins"]), "gamesDraw": 0,
        "score": p["wins"], "points": p["wins"], "matchesPlayed": p["games"],
        "rank": 0, "ranking": 0, "tierLevel": 0, "userTierLevel": 0,
        "expectedTierLevel": _champion_tier_for_wins(p["wins"]),
        "predictedTierLevel": _champion_tier_for_wins(p["wins"]),
        "goalsFor": 0, "goalsAgainst": 0, "winStreak": 0, "bestWinStreak": 0,
        "remainingMatches": remaining, "matchesRemaining": remaining,
        "gamesRemaining": remaining, "remainingGames": remaining,
        "numGames": remaining, "numGamesRemaining": remaining,
        "regularGamesRemaining": remaining, "regularMatchesRemaining": remaining,
        "weekendLeagueGamesRemaining": remaining, "gamesLeft": remaining,
        "numberOfGamesRemaining": remaining, "numberOfMatchesRemaining": remaining,
        "maxMatches": _CHAMPION_FINALS_MATCHES, "maxGames": _CHAMPION_FINALS_MATCHES,
        "maxNumberOfMatches": _CHAMPION_FINALS_MATCHES,
        "totalGames": _CHAMPION_FINALS_MATCHES,
        "numberOfGames": _CHAMPION_FINALS_MATCHES,
        "regularGames": _CHAMPION_FINALS_MATCHES,
    }


def _champion_leaderboard_json() -> dict[str, Any]:
    p = _champion_progress()
    row = {"name": str(CFG.get("club_name", "Local FC")),
           "personaId": int(CFG["persona_id"]), "rank": 1, "ranking": 1,
           "score": p["wins"], "points": p["wins"], "gamesPlayed": p["games"],
           "gamesWon": p["wins"], "gamesLost": max(0, p["games"] - p["wins"]),
           "tierLevel": 0, "userTierLevel": 0,
           "championEventId": _CHAMPION_EVENT_ID}
    return {"leaderboard": [dict(row)], "ranking": [dict(row)],
            "entries": [dict(row)], "userEntry": dict(row), "totalCount": 1,
            "prizeTiers": _champion_prize_tiers()}


def _seasonal_config_json() -> dict[str, Any]:
    w = _seasonal_window()
    schedule = [{"weekId": w["weekIndex"], "startTime": w["start"],
                 "endTime": w["end"], "rewardsTime": w["end"] + 3600,
                 "active": True}]
    return {
        "version": 1, "generatedAt": w["current"],
        "serverTime": w["current"], "currentTime": w["current"],
        "divisionRivals": {"enabled": True, "active": True, "seasonId": 0,
                            "startTime": w["start"], "endTime": w["end"],
                            "weeklyResetTime": w["end"],
                            "maxWeeklyGames": _RIVALS_WEEKLY_MATCHES,
                            "placementMatches": 5, "schedule": schedule},
        "futChampions": {"enabled": True, "active": True,
                         "startTime": w["start"], "endTime": w["end"],
                         "registrationCloseTime": w["end"],
                         "playoffMaxGames": _CHAMPION_FINALS_MATCHES,
                         "finalsMaxGames": _CHAMPION_FINALS_MATCHES,
                         "qualificationPointsRequired": 2000,
                         "schedule": schedule,
                         "playoffScheduleViewPeriodInMinutes": 10080,
                         "finalsScheduleViewPeriodInMinutes": 10080},
    }


def _seasonal_player_reward_ids(count: int, min_rating: int) -> list[int]:
    pool = [int(rid) for rid, meta in PLAYER_DB.items()
            if int(meta.get("rating", 0) or 0) >= min_rating]
    if len(pool) < count:
        pool = [int(rid) for rid, meta in PLAYER_DB.items()
                if int(meta.get("rating", 0) or 0) >= max(75, min_rating - 6)]
    rng = random.Random(f"fut20-seasonal-reward:{now_s()}:{count}:{min_rating}")
    rng.shuffle(pool)
    return pool[:max(0, count)]


def _seasonal_grant_reward(coins: int, player_count: int, min_rating: int) -> dict[str, Any]:
    if coins:
        STATE.add_credits(int(coins))
    granted = []
    base_ts = now_s()
    for rank, resource_id in enumerate(_seasonal_player_reward_ids(player_count, min_rating)):
        item = STATE.make_player_item(resource_id, "unassigned")
        item["timestamp"] = base_ts - rank
        with STATE.lock:
            STATE.conn.execute("UPDATE items SET data=? WHERE id=?",
                               (json.dumps(item), int(item.get("id", 0))))
            STATE.conn.commit()
        granted.append(_native_pack_item(item))
    return {"coins": int(coins), "credits": STATE.credits(),
            "grantedItems": granted, "itemList": granted,
            "numberItems": len(granted)}


def _rivals_claim_rewards() -> dict[str, Any]:
    w, p = _seasonal_window(), _rivals_progress()
    if p["weeklyGames"] <= 0 or p["rewardsClaimedWeek"] == w["weekIndex"]:
        return {"alreadyClaimed": True, "awards": [], "grantedItems": [],
                "credits": STATE.credits()}
    tier = max(i for i, row in enumerate(_RIVALS_WEEKLY_RANKS) if p["weeklyPoints"] >= row[1])
    name, _threshold, coins, count, rating = _RIVALS_WEEKLY_RANKS[tier]
    coins += 2_000 * p["weeklyWins"]
    count += p["weeklyWins"] // 4
    result = _seasonal_grant_reward(coins, count, rating)
    stored = STATE.get("rivals_progress", {}) or {}
    stored.update({"rewardsClaimedWeek": w["weekIndex"], "weeklyWins": 0,
                   "weeklyGames": 0, "weeklyPoints": 0,
                   "weekIndex": w["weekIndex"]})
    STATE.set("rivals_progress", stored)
    awards = [{"awardType": "coin", "awardValue": coins, "awardCount": 1},
              {"awardType": "players", "awardValue": rating,
               "awardCount": count}]
    log.warning("RIVALS-REWARD rank=%s div=%s coins=%s players=%sx%s",
                name, p["division"], coins, count, rating)
    return {"alreadyClaimed": False, "awards": awards, "awardsList": awards,
            "division": p["division"], **result}


def _champion_claim_rewards() -> dict[str, Any]:
    w, p = _seasonal_window(), _champion_progress()
    if p["games"] <= 0 or p["rewardsClaimedWeek"] == w["weekIndex"]:
        return {"alreadyClaimed": True, "awardedPrizes": [],
                "prizesInError": [], "grantedItems": [],
                "credits": STATE.credits()}
    tier = _champion_tier_for_wins(p["wins"])
    _level, _lo, _hi, coins, count, rating = next(
        band for band in _CHAMPION_REWARD_BANDS if band[0] == tier)
    result = _seasonal_grant_reward(coins, count, rating)
    stored = STATE.get("champion_progress", {}) or {}
    stored.update({"rewardsClaimedWeek": w["weekIndex"], "wins": 0,
                   "losses": 0, "games": 0, "weekIndex": w["weekIndex"]})
    STATE.set("champion_progress", stored)
    awards = [{"awardType": "coin", "awardValue": coins, "awardCount": 1},
              {"awardType": "players", "awardValue": rating,
               "awardCount": count}]
    log.warning("CHAMPION-REWARD tier=%s wins=%s coins=%s players=%sx%s",
                tier, p["wins"], coins, count, rating)
    return {"alreadyClaimed": False, "tierLevel": tier, "wins": p["wins"],
            "awardedPrizes": [{"eventId": _CHAMPION_EVENT_ID,
                               "awards": awards}],
            "awards": awards, "prizesInError": [], **result}


def _native_user() -> dict[str, Any]:
    # Match the *types* and nesting of the successful FIFA 20 response.  The
    # client stops issuing requests if fields such as bidTokens/reliability/
    # squadList have the wrong JSON type, even when the HTTP status is 200.
    club_player_items = [x for x in STATE.list_items("club") if str(x.get("itemType", "player")) == "player"]
    squad = _native_squad()
    squad_players = sum(1 for x in squad.get("players", []) if isinstance(x.get("itemData"), dict))
    total_players = len(club_player_items)
    loose_players = max(0, total_players - squad_players)
    actives = _active_club_items()
    owned_inventory = STATE.list_items("club")
    consumable_count = sum(
        max(1, _int_or(item.get("stackCount"), 1))
        for item in owned_inventory
        if str(item.get("itemType", "")).lower() == "development"
    )
    club_item_counts = {
        item_type: sum(
            1 for item in owned_inventory
            if str(item.get("itemType", "")).lower() == item_type
        )
        for item_type in ("kit", "custom", "stadium", "ball")
    }
    owned_club_item_count = sum(club_item_counts.values())
    # The startup user decoder uses this summary collection to decide whether
    # an existing club has a usable squad.  An empty collection forces the
    # starter-squad wizard even when userMassInfo also carries a full squad.
    squad_summaries = [_native_squad_summary(saved) for saved in STATE.list_squads()]
    record = STATE.global_match_record()
    campaign = _scmp_campaign()
    return {
        "pileClubPlayers": loose_players,
        "pileSquadPlayers": squad_players,
        "pileConsumables": consumable_count,
        "pileManagers": 0,
        "pileClubItems": owned_club_item_count,
        "pileClubTotal": total_players + owned_club_item_count,
        "pilePack": len(STATE.list_items("unassigned")),
        "kitCount": club_item_counts["kit"],
        "badgeCount": club_item_counts["custom"],
        "stadiumCount": club_item_counts["stadium"],
        "ballCount": club_item_counts["ball"],
        "clubPlayers": total_players,
        "clubItemCount": owned_club_item_count,
        "clubCount": total_players + owned_club_item_count,
        "accountCreatedPlatformName": "pc",
        "personaId": int(CFG["persona_id"]),
        "clubName": str(CFG["club_name"]),
        "clubAbbr": str(CFG["club_abbr"]),
        "clubNameChangeAllowed": 0,
        "established": str(int(STATE.get("established", now_s()))),
        **_credits_payload(),
        "bidTokens": {"count": 0, "updateTime": 0},
        "divisionOffline": 1,
        "divisionOnline": 1,
        "won": record["wins"],
        "draw": record["draws"],
        "loss": record["losses"],
        "wins": record["wins"],
        "draws": record["draws"],
        "losses": record["losses"],
        "gamesPlayed": record["matchesPlayed"],
        "matchesPlayed": record["matchesPlayed"],
        "trophies": 0,
        "purchased": False,
        "fifaPointsFromLastYear": 0,
        "fifaPointsTransferredStatus": 0,
        "enablePlayerPicks": 1,
        "isPlayerPicksTemporaryStorageNotEmpty": bool(PICK_STATE.pending_flag(STATE)),
        "reliability": {"reliability": 100, "matchUnfinishedTime": 0},
        "actives": actives,
        "squadList": {"squad": squad_summaries},
        # FIFA refreshes /user immediately after a settled match, but does
        # not issue another SCMP GET until FUT is re-entered.  Carry the live
        # campaign snapshot on that already-established refresh path so the
        # Objectives model can invalidate its menu cache without a restart.
        **_scmp_live_fields(campaign),
    }


_STICKER_BOOK_CONTEXTS = {
    "club": 0,
    "year": 1,
    "country": 2,
    "league": 3,
    "newcards": 4,
    "consumables": 5,
}


def _sticker_book_stats(category: str, values: list[tuple[str, int]]) -> dict[str, Any]:
    """Build the exact FutStickerBookStats2ServerResponse wire shape.

    The retail Cards DLL accepts only ``category`` and ``stat`` at the top
    level.  ``category`` is a required string enum; omitting it leaves the
    native destination index at -1 and crashes at CardsDLL+0x28DB7C.
    """
    normalized = str(category or "club").lower()
    if normalized not in _STICKER_BOOK_CONTEXTS:
        normalized = "club"
    context_id = _STICKER_BOOK_CONTEXTS[normalized]
    return {
        "category": normalized,
        "stat": [
            {
                "contextId": context_id,
                "contextValue": 0,
                "type": str(name),
                "typeValue": max(0, int(value)),
            }
            for name, value in values
        ],
    }


def _club_stats(category: str = "year") -> dict[str, Any]:
    _ensure_persistent_club_items()
    club_items = STATE.list_items("club")
    player_items = [
        item for item in club_items
        if str(item.get("itemType", "player")).lower() == "player"
    ]
    managers = sum(
        1 for item in club_items
        if str(item.get("itemType", "")).lower() == "manager"
    )
    quality = {"bronze": 0, "silver": 0, "gold": 0}
    rare_players = 0
    for item in player_items:
        rating = _int_or(item.get("rating"), 0)
        quality["gold" if rating >= 75 else "silver" if rating >= 65 else "bronze"] += 1
        if _int_or(item.get("rareflag", item.get("rareFlag", 0)), 0) > 0:
            rare_players += 1
    club_type_counts = {
        item_type: sum(
            1 for item in club_items
            if str(item.get("itemType", "")).lower() == item_type
        )
        for item_type in ("kit", "custom", "stadium", "ball")
    }
    values = [
        ("players", len(player_items)),
        ("playersBronze", quality["bronze"]),
        ("playersSilver", quality["silver"]),
        ("playersGold", quality["gold"]),
        ("rarePlayers", rare_players),
        ("staff", managers),
        ("staffManager", managers),
        ("staffHeadCoach", 0),
        ("staffFitnessCoach", 0),
        ("staffGKCoach", 0),
        ("staffPhysio", 0),
        ("kits", club_type_counts["kit"]),
        ("kitsHome", sum(1 for item in club_items if str(item.get("itemType", "")).lower() == "kit" and _int_or(item.get("category"), 0) == 2)),
        ("kitsAway", sum(1 for item in club_items if str(item.get("itemType", "")).lower() == "kit" and _int_or(item.get("category"), 0) == 3)),
        ("badges", club_type_counts["custom"]),
        ("stadia", club_type_counts["stadium"]),
        ("balls", club_type_counts["ball"]),
        ("trophies", 0),
        ("trophiesOffline", 0),
        ("trophiesOnline", 0),
        ("trophiesFeaturedOffline", 0),
        ("trophiesFeaturedOnline", 0),
        ("trophiesSeasonOffline", 0),
        ("trophiesSeasonOnline", 0),
    ]
    return _sticker_book_stats(category, values)


def _consumable_stats() -> dict[str, Any]:
    """Return consumable totals in the native sticker-book response."""
    owned = [
        item for item in STATE.list_items("club")
        if isinstance(item, dict)
        and str(item.get("itemType", "")).lower() == "development"
        and _int_or(item.get("resourceId"), 0) in CONSUMABLE_DB
    ]
    totals = {
        "consumablesContract": 0,
        "consumablesPosition": 0,
        "consumablesTrainingGkPlayStyle": 0,
        "consumablesTrainingPlayerPlayStyle": 0,
        "consumablesHealing": 0,
        "consumablesTrainingGk": 0,
        "consumablesTrainingManager": 0,
        "consumablesTrainingPlayer": 0,
        "consumablesContractManager": 0,
        "consumablesContractPlayer": 0,
        "consumablesFitness": 0,
        "consumablesTraining": 0,
        "consumablesTrainingManagerLeagueModifier": 0,
        "consumablesFormationManager": 0,
        "consumablesFitnessTeam": 0,
        "consumablesFitnessPlayer": 0,
    }
    for item in owned:
        resource_id = _int_or(item.get("resourceId"), 0)
        meta = CONSUMABLE_DB[resource_id]
        stack_count = max(0, _int_or(item.get("stackCount"), 0))
        source_member = str(meta["sourceMember"])
        if source_member in totals:
            totals[source_member] += stack_count
        if "Contract" in source_member:
            totals["consumablesContract"] += stack_count
        if "Fitness" in source_member:
            totals["consumablesFitness"] += stack_count
        if "Training" in source_member:
            totals["consumablesTraining"] += stack_count
    return _sticker_book_stats("consumables", list(totals.items()))


def _club_consumable_search(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return owned development cards in FIFA's native stack envelope."""
    owned_rows = [
        item for item in STATE.list_items("club")
        if isinstance(item, dict)
        and str(item.get("itemType", "")).lower() == "development"
        and _int_or(item.get("resourceId"), 0) in CONSUMABLE_DB
    ]

    # A consumable pack historically persisted one SQLite item for every card.
    # FIFA's picker asks for at most 100 rows and then partitions that page into
    # tabs locally. After enough packs, valid chemistry styles and position
    # modifiers therefore sat beyond the first page: their sticker-book counts
    # were correct, but their tabs looked empty. Retail FUT exposes one stack per
    # resource definition, so aggregate the physical rows before pagination.
    # Keep the oldest instance as the representative id; application consumes by
    # resourceId and can continue to drain any of the backing rows safely.
    stacks: dict[int, dict[str, Any]] = {}
    for item in sorted(owned_rows, key=lambda row: _int_or(row.get("id"), 0)):
        resource_id = _int_or(item.get("resourceId"), 0)
        stack_count = max(1, _int_or(item.get("stackCount"), 1))
        untradeable_count = max(0, _int_or(item.get("untradeableCount"), 0))
        current = stacks.get(resource_id)
        if current is None:
            current = dict(item)
            current["stackCount"] = 0
            current["untradeableCount"] = 0
            stacks[resource_id] = current
        current["stackCount"] += stack_count
        current["untradeableCount"] += untradeable_count

    owned = list(stacks.values())
    owned.sort(key=lambda item: (
        _int_or(item.get("cardassetid"), 0),
        _int_or(item.get("cardsubtypeid"), 0),
        _int_or(item.get("resourceId"), 0),
    ))
    start = max(0, _int_or(_market_first(query, "start"), 0))
    requested_count = max(1, _int_or(_market_first(query, "count", "num"), 100))
    # The catalog has more than 100 distinct development definitions. FIFA 20
    # does not issue a second picker request; it filters the single response
    # into tabs. Over-deliver the complete stacked catalog on its first page so
    # no category can disappear behind an unreachable server-side page.
    count = max(requested_count, len(owned)) if start == 0 else requested_count
    wrapped: list[dict[str, Any]] = []
    for item in owned[start:start + count]:
        resource_id = _int_or(item.get("resourceId"), 0)
        nested = dict(item)
        wrapped.append({
            "count": max(1, _int_or(item.get("stackCount"), 1)),
            "untradeableCount": max(0, _int_or(item.get("untradeableCount"), 0)),
            "resourceId": resource_id,
            "discardValue": _int_or(item.get("discardValue"), 0),
            "item": nested,
        })
    return {
        "itemData": wrapped,
        "itemCount": len(wrapped),
        "total": len(owned),
        "count": len(wrapped),
        "start": start,
    }


def _store_purchasegroups() -> dict[str, Any]:
    credits=STATE.credits()
    settings=load_pack_settings()
    unopened=STATE.unopened_pack_counts()
    packs=[]
    visible_store_names: set[tuple[str, str]] = set()
    for pid_s,cfg0 in sorted(settings.get("packs",{}).items(),key=lambda kv:int(kv[0])):
        pid=int(pid_s); cfg=dict(cfg0)
        reward_quantity=max(0,int(unopened.get(pid,0)))
        if not bool(cfg.get("enabled",True)) and reward_quantity<=0: continue
        if pid in _HIDDEN_COIN_STORE_PACK_IDS and reward_quantity <= 0:
            continue
        name=str(cfg.get("name",f"Custom Pack {pid}"))
        price=0 if reward_quantity else max(0,int(cfg.get("price",0)))
        qty=max(1,min(30,int(cfg.get("size",12))))
        rare=max(0,min(qty,int(cfg.get("rare_slots",0))))
        gw=max(0.0,float(cfg.get("gold_weight",0) or 0))
        sw=max(0.0,float(cfg.get("silver_weight",0) or 0))
        bw=max(0.0,float(cfg.get("bronze_weight",0) or 0))
        total=gw+sw+bw
        if total<=0: gw,sw,bw,total=100.0,0.0,0.0,100.0
        gold=int(round(qty*gw/total)); silver=int(round(qty*sw/total)); bronze=max(0,qty-gold-silver)
        dominant=max((("gold",gw),("silver",sw),("bronze",bw)),key=lambda x:x[1])[0]
        requested_group=str(cfg.get("store_group",dominant) or dominant).lower()
        store_group={"promo":"special"}.get(requested_group,requested_group)
        if store_group not in {"bronze","silver","gold","special"}:
            store_group=dominant
        category_id={"bronze":1,"silver":2,"gold":3,"special":4}[store_group]
        # FIFA's native opening presentation uses assets 1/2/3 for the three
        # standard tiers. Pack ownership/purchase IDs remain unique while art
        # and animation can safely reuse those native assets.
        native_pack_asset_id = {"bronze": 1, "silver": 2, "gold": 3, "special": 4}[store_group]
        configured_pack_asset_id = max(1, int(cfg.get("pack_asset_id", pid) or pid))
        bio = str(cfg.get("bio", "") or "")
        is_my_pack = reward_quantity > 0
        display_group = "mypacks" if is_my_pack else store_group
        # Old launcher-settings files can retain a custom article with the
        # same label as a newly built-in Gold pack. Keep earned My Packs intact,
        # but show only one coin-store article per normalized name/category.
        if not is_my_pack:
            name_key = (store_group, re.sub(r"\s+", " ", name.strip().casefold()))
            if name_key in visible_store_names:
                log.info("STORE skipped duplicate %s pack name=%s id=%s", store_group, name, pid)
                continue
            visible_store_names.add(name_key)
        article = {
            "id":pid,
            "assetId":native_pack_asset_id if is_my_pack else configured_pack_asset_id,
            # Native article identifiers used by the My Packs selection path.
            # Without these FIFA falls back to a store-price dialog even
            # though the earned pack tile is visible.
            "packId":pid,
            "packAssetId":native_pack_asset_id if is_my_pack else configured_pack_asset_id,
            "quantity":reward_quantity if reward_quantity > 0 else 1,
            "unopenedQuantity":reward_quantity,
            "unopened":is_my_pack,
            "isReward":bool(reward_quantity),
            # Native Store code splits purchase entities into Store articles
            # and My Packs with this flag.  A zero coin price alone leaves an
            # earned SBC pack mixed into the normal catalog.
            "isMyPack":is_my_pack,
            "tradable":False if is_my_pack else True,
            "tradeable":False if is_my_pack else True,

            # FIFA 20 store presentation fields. Different PC builds reference
            # slightly different keys, so provide the same content under the
            # common aliases used by the store tile/header/detail panel.
            "name": name,
            "packName": name,
            "displayName": name,
            "title": name,

            # FIFA 20 uses this field for the visible label above the pack tile.
            "description": name,

            # Keep the longer text separate so it cannot blank/replace the title.
            "packDescription": bio,
            "longDescription": bio,
            "shortDescription": bio,
            "bio": bio,

            "label": name,
            "displayLabel": name,
            "storePackName": name,

            "finalPrice":price,"originalPrice":price,
            # The native store provider reads direct currency values before
            # consulting the currencies collection. Supplying both prevents
            # an undefined price on builds that use either representation.
            "coins":price,
            "points":0,
            "currencies":[{"name":"coins","funds":price,"finalFunds":price},
                          {"name":"points","funds":0,"finalFunds":0}],
            "categoryList":{"categoryId":category_id,"orderInCategory":pid},
            "displayGroup":{"value":display_group,"priority":0 if is_my_pack else category_id},
            "purchaseGroup":display_group,
            "group":display_group,
            "state":"active","packType":dominant.upper(),"sortPriority":pid,
            "packContentInfo":{"itemQuantity":qty,"goldQuantity":gold,"silverQuantity":silver,
                               "bronzeQuantity":bronze,"rareQuantity":rare},
            "visible":True,"isPurchaseable":not is_my_pack and credits>=price,
            "isCoinPurchasable":not is_my_pack,
            "isFifaPointPurchasable":False,
            "isPromo":store_group == "special" and not is_my_pack,
            "isAvailableInStore":not is_my_pack,
        }
        if str(cfg.get("content_type", "")).lower() == "player_pick" and not is_my_pack:
            article.update({
                "contentType": "playerPick",
                "purchaseType": "playerPick",
                "isPlayerPick": True,
                "playerPickPool": str(cfg.get("player_pick_pool", "promo")),
                "playerPickOptions": max(2, min(5, _int_or(cfg.get("player_pick_options"), 3))),
                "playerPickMinRating": max(0, _int_or(cfg.get("player_pick_min_rating"), 0)),
            })
        packs.append(article)
    return {"purchase":packs,"timestamp":now_s(),"unopenedPacks":sum(unopened.values())}


def _contract_gain_for_player(resource_id: int, rating: int) -> int:
    gains = {
        5001001: (8, 2, 1),
        5001002: (10, 10, 8),
        5001003: (15, 11, 13),
        5001004: (15, 6, 3),
        5001005: (20, 24, 18),
        5001006: (28, 24, 28),
        5001013: (99, 99, 99),
    }
    bronze, silver, gold = gains.get(int(resource_id), (0, 0, 0))
    if rating < 65:
        return bronze
    if rating < 75:
        return silver
    return gold


def _apply_consumable_resource(resource_id: int, target_ids: list[int]) -> list[dict[str, Any]]:
    rid = int(resource_id)
    unique_targets: list[int] = []
    for raw in target_ids:
        iid = _int_or(raw, 0)
        if iid > 0 and iid not in unique_targets:
            unique_targets.append(iid)

    meta = CONSUMABLE_DB.get(rid)
    if not meta:
        log.warning("Consumable resource %s acknowledged without mutation targets=%s", rid, unique_targets)
        return []

    updated: list[dict[str, Any]] = []
    application_succeeded = False
    card_asset = _int_or(meta.get("cardassetid"), 0)
    source_member = str(meta.get("sourceMember", ""))

    if source_member == "consumablesFitnessTeam":
        gain = max(0, _int_or(meta.get("amount"), 0))
        for iid in STATE.active_squad_item_ids():
            item = STATE.get_item(iid)
            if not isinstance(item, dict) or str(item.get("itemType", "player")).lower() != "player":
                continue
            fitness = min(99, max(0, _int_or(item.get("fitness"), 99)) + gain)
            changed = STATE.update_item_fields(iid, {"fitness": fitness})
            if isinstance(changed, dict) and iid in unique_targets:
                updated.append(changed)
            if isinstance(changed, dict):
                application_succeeded = True
    else:
        for iid in unique_targets:
            item = STATE.get_item(iid)
            if not isinstance(item, dict):
                log.warning("Consumable target item not found item=%s resource=%s", iid, rid)
                continue
            item_type = str(item.get("itemType", "player") or "player").lower()
            fields: dict[str, Any] = {}

            if source_member == "consumablesContractPlayer" and item_type == "player":
                gain = _contract_gain_for_player(rid, _int_or(item.get("rating"), 0))
                current = max(0, _int_or(item.get("contract", item.get("contracts", 0)), 0))
                value = min(99, current + gain)
                fields = {"contract": value, "contracts": value}
            elif source_member == "consumablesContractManager" and item_type == "manager":
                gains = {
                    5001007: (8, 2, 1), 5001008: (8, 10, 8),
                    5001009: (11, 11, 13), 5001010: (15, 6, 3),
                    5001011: (18, 24, 18), 5001012: (24, 24, 28),
                }
                bronze, silver, gold = gains.get(rid, (0, 0, 0))
                rating = _int_or(item.get("rating"), 75)
                gain = bronze if rating < 65 else silver if rating < 75 else gold
                current = max(0, _int_or(item.get("contract", item.get("contracts", 0)), 0))
                value = min(99, current + gain)
                fields = {"contract": value, "contracts": value}
            elif source_member == "consumablesFitnessPlayer" and item_type == "player":
                gain = max(0, _int_or(meta.get("amount"), 0))
                fields = {"fitness": min(99, max(0, _int_or(item.get("fitness"), 99)) + gain)}
            elif source_member == "consumablesHealing" and item_type == "player":
                amount = max(1, _int_or(meta.get("amount"), 1))
                remaining = max(0, _int_or(item.get("injuryGames"), 0) - amount)
                fields = {"injuryGames": remaining}
                if remaining == 0:
                    fields["injuryType"] = "none"
            elif source_member == "consumablesPosition" and item_type == "player":
                from_position, to_position = POSITION_MODIFIER_EFFECTS.get(rid, ("", ""))
                current_position = str(item.get("preferredPosition", "") or "").upper()
                if from_position and current_position == from_position:
                    fields = {"preferredPosition": to_position}
                else:
                    log.info(
                        "Rejected position modifier resource=%s item=%s expected=%s actual=%s",
                        rid, iid, from_position, current_position,
                    )
            elif source_member == "consumablesTrainingPlayer" and item_type == "player":
                if str(item.get("preferredPosition", "")).upper() != "GK":
                    fields = {"training": _int_or(meta.get("cardsubtypeid"), 0)}
            elif source_member == "consumablesTrainingGk" and item_type == "player":
                if str(item.get("preferredPosition", "")).upper() == "GK":
                    fields = {"training": _int_or(meta.get("cardsubtypeid"), 0)}
            elif source_member == "consumablesTrainingPlayerPlayStyle" and item_type == "player":
                if str(item.get("preferredPosition", "")).upper() != "GK":
                    # FIFA 20's dynamic player contract uses the native style
                    # ids 250..268 (BASIC through the outfield styles).
                    fields = {"playStyle": _int_or(meta.get("cardsubtypeid"), 250)}
            elif source_member == "consumablesTrainingGkPlayStyle" and item_type == "player":
                if str(item.get("preferredPosition", "")).upper() == "GK":
                    fields = {"playStyle": _int_or(meta.get("cardsubtypeid"), 269)}
            elif source_member == "consumablesTrainingManagerLeagueModifier" and item_type == "manager":
                fields = {"leagueId": max(0, _int_or(meta.get("amount"), 0))}

            if fields:
                changed = STATE.update_item_fields(iid, fields)
                if isinstance(changed, dict):
                    updated.append(changed)
                    application_succeeded = True

    if application_succeeded:
        STATE.consume_resource_instance(rid, set(unique_targets))
    else:
        log.info(
            "Consumable resource %s (%s/%s) was valid but no eligible target changed",
            rid, source_member, card_asset,
        )

    return updated


def _scmp_state() -> dict[str, Any]:
    """Create one durable local season without retroactively claiming matches."""
    with STATE.lock:
        saved = STATE.get("scmp_campaign_v1")
        if isinstance(saved, dict) and _int_or(saved.get("startTime"), 0) > 0:
            # 1.0.31 separates ordinary FUT play from Draft and Squad Battles.
            # Existing seasons need a one-time FUT baseline so historical mode
            # results do not suddenly complete the corrected objectives.
            baseline = saved.get("baseline", {})
            if not isinstance(baseline, dict):
                baseline = {}
            if "fut" not in baseline:
                mode_record = STATE.get("objectives_mode_record", {})
                if not isinstance(mode_record, dict):
                    mode_record = {}
                fut_entries = [
                    mode_record.get(mode, {})
                    for mode in ("generic", "rivals", "champion")
                ]
                baseline["fut"] = {
                    field: sum(
                        max(0, _int_or(entry.get(field), 0))
                        for entry in fut_entries if isinstance(entry, dict)
                    )
                    for field in ("matches", "wins")
                }
                saved["baseline"] = baseline
                STATE.set("scmp_campaign_v1", saved)
            return saved
        global_record = STATE.global_match_record()
        mode_record = STATE.get("objectives_mode_record", {})
        if not isinstance(mode_record, dict):
            mode_record = {}
        baseline = {
            "all": {"matches": global_record["matchesPlayed"], "wins": global_record["wins"]},
        }
        for mode in ("squad_battles", "draft", "rivals", "champion", "generic"):
            entry = mode_record.get(mode, {})
            if not isinstance(entry, dict):
                entry = {}
            baseline[mode] = {
                "matches": max(0, _int_or(entry.get("matches"), 0)),
                "wins": max(0, _int_or(entry.get("wins"), 0)),
            }
        baseline["fut"] = {
            field: sum(baseline[mode][field] for mode in ("generic", "rivals", "champion"))
            for field in ("matches", "wins")
        }
        saved = {
            "startTime": now_s(), "baseline": baseline, "xp": 0,
            "claims": {"objectives": {}, "groups": {}, "levels": {}},
        }
        STATE.set("scmp_campaign_v1", saved)
        return saved


def _scmp_metrics(saved: dict[str, Any]) -> dict[str, dict[str, int]]:
    global_record = STATE.global_match_record()
    mode_record = STATE.get("objectives_mode_record", {})
    if not isinstance(mode_record, dict):
        mode_record = {}
    current: dict[str, dict[str, int]] = {
        "all": {"matches": global_record["matchesPlayed"], "wins": global_record["wins"]},
    }
    for mode in ("squad_battles", "draft", "rivals", "champion", "generic"):
        entry = mode_record.get(mode, {})
        if not isinstance(entry, dict):
            entry = {}
        current[mode] = {
            "matches": max(0, _int_or(entry.get("matches"), 0)),
            "wins": max(0, _int_or(entry.get("wins"), 0)),
        }
    current["fut"] = {
        field: sum(current[mode][field] for mode in ("generic", "rivals", "champion"))
        for field in ("matches", "wins")
    }
    baseline = saved.get("baseline", {})
    if not isinstance(baseline, dict):
        baseline = {}
    return {
        mode: {
            field: max(0, value - _int_or((baseline.get(mode) or {}).get(field), 0))
            for field, value in values.items()
        }
        for mode, values in current.items()
    }


# The native FIFA 20 Objectives frontend keeps a long-lived active-campaign
# object for the authenticated FUT session.  The latest tester trace proves
# that a fresh category/details HTTP response alone does not replace that
# object: the screen keeps the old progress until /scmp/campaign/active is
# fetched during FUT authentication.  Track the revision the client has seen
# through the active-campaign route so a later details request can carry a full
# campaign envelope rather than only a child group fragment.
_scmp_campaign_refresh_lock = threading.RLock()
_scmp_stale_revision = 0
_scmp_last_active_fetch_revision = 0


def _mark_scmp_campaign_stale(revision: int) -> None:
    global _scmp_stale_revision
    with _scmp_campaign_refresh_lock:
        _scmp_stale_revision = max(_scmp_stale_revision, max(0, int(revision)))
    log.info("[SCMP-LIVE] active campaign marked stale revision=%d", int(revision))


def _note_scmp_active_fetch(revision: int) -> None:
    global _scmp_last_active_fetch_revision
    with _scmp_campaign_refresh_lock:
        _scmp_last_active_fetch_revision = max(
            _scmp_last_active_fetch_revision, max(0, int(revision))
        )


def _scmp_requires_full_campaign_envelope(revision: int) -> bool:
    with _scmp_campaign_refresh_lock:
        return (
            max(0, int(revision)) > _scmp_last_active_fetch_revision
            and max(0, int(revision)) <= _scmp_stale_revision
        )


def _scmp_objective_status(campaign: dict[str, Any], objective_id: int) -> tuple[int, int, str]:
    for category in campaign.get("categoriesList", []):
        if not isinstance(category, dict):
            continue
        for group in category.get("groupsList", []):
            if not isinstance(group, dict):
                continue
            for objective in group.get("objectives", []):
                if not isinstance(objective, dict):
                    continue
                if _int_or(objective.get("objectiveId"), 0) == int(objective_id):
                    return (
                        max(0, _int_or(objective.get("currentProgress"), 0)),
                        max(0, _int_or(objective.get("progressCount"), 0)),
                        str(objective.get("state", "")),
                    )
    return 0, 0, "MISSING"


def _scmp_response_headers(revision: int) -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        # A revision-dependent validator makes the response identity explicit
        # even for HTTP stacks that keep an in-process resource cache.
        "ETag": f'"localfut20-scmp-r{max(0, int(revision))}"',
    }


def _scmp_campaign() -> dict[str, Any]:
    saved = _scmp_state()
    campaign = SCMP.campaign_payload(
        _scmp_metrics(saved), saved.get("claims", {}),
        _int_or(saved.get("xp"), 0), _int_or(saved.get("startTime"), now_s()), now_s(),
    )
    revision = max(0, _int_or(STATE.get("scmp_revision", 0), 0))
    refresh_required = _scmp_requires_full_campaign_envelope(revision)
    campaign.update({
        "revision": revision,
        "objectivesRevision": revision,
        "lastUpdated": now_s(),
        # This is a native SCMP model-invalidation field, not a generic HTTP
        # cache hint. It remains true until the client fetches /campaign/active
        # for the new revision.
        "needsGroupsRefresh": refresh_required,
    })
    return campaign


def _scmp_diagnostics(campaign: dict[str, Any]) -> dict[str, Any]:
    """Compact, complete SCMP state for trace-driven client-cache debugging."""
    groups: list[dict[str, Any]] = []
    for category in campaign.get("categoriesList", []):
        if not isinstance(category, dict):
            continue
        for group in category.get("groupsList", []):
            if not isinstance(group, dict):
                continue
            objectives = []
            for objective in group.get("objectives", []):
                if not isinstance(objective, dict):
                    continue
                objectives.append({
                    "id": _int_or(objective.get("objectiveId"), 0),
                    "current": _int_or(objective.get("currentProgress"), 0),
                    "target": _int_or(objective.get("progressCount"), 0),
                    "state": str(objective.get("state", "")),
                })
            groups.append({
                "categoryId": _int_or(category.get("categoryId"), 0),
                "groupId": _int_or(group.get("groupId"), 0),
                "groupState": _int_or(group.get("groupState", group.get("state")), 0),
                "objectives": objectives,
            })
    return {
        "campaignId": _int_or(campaign.get("campaignId"), SCMP.CAMPAIGN_ID),
        "revision": _int_or(campaign.get("revision"), 0),
        "xp": _int_or(campaign.get("userXp"), 0),
        "needsGroupsRefresh": bool(campaign.get("needsGroupsRefresh")),
        "groups": groups,
    }


def _log_scmp_snapshot(label: str, campaign: dict[str, Any]) -> None:
    log.info(
        "[SCMP-TRACE] %s %s", label,
        json.dumps(_scmp_diagnostics(campaign), separators=(",", ":"), ensure_ascii=False),
    )


def _scmp_live_fields(campaign: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach a current SCMP snapshot to responses the retail client refreshes.

    The aliases cover the FIFA 20 native user and match-report readers.  They
    all reference one immutable payload, so XP/progress/claim state cannot
    disagree inside a response.
    """
    current = campaign if isinstance(campaign, dict) else _scmp_campaign()
    xp = max(0, _int_or(current.get("userXp"), 0))
    levels = current.get("levels", []) if isinstance(current.get("levels"), list) else []
    season_level = max(
        (int(level.get("levelId", 1)) for level in levels
         if xp >= _int_or(level.get("xpThreshold"), 0)),
        default=1,
    )
    revision = max(0, _int_or(STATE.get("scmp_revision", 0), 0))
    return {
        "campaignId": _int_or(current.get("campaignId"), SCMP.CAMPAIGN_ID),
        "userXp": xp,
        "seasonLevel": season_level,
        "objectivesRevision": revision,
        "seasonalCampaign": current,
        "scmpCampaign": current,
        "campaignProgress": current,
    }


def _with_scmp_live(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.update(_scmp_live_fields())
    return result


def _scmp_claim(kind: str, claim_id: int, option_id: int = 0) -> tuple[int, dict[str, Any]]:
    """Claim an earned reward exactly once; XP, coins and packs commit together."""
    with STATE.lock:
        saved = _scmp_state()
        claims = saved.get("claims", {})
        if not isinstance(claims, dict):
            claims = {}
        bucket = claims.get(kind, {})
        if not isinstance(bucket, dict):
            bucket = {}
        key = str(claim_id)
        if key in bucket:
            campaign = _scmp_campaign()
            return 200, {
                "success": True, "alreadyClaimed": True, "awards": [],
                **_scmp_live_fields(campaign),
            }

        reward: tuple[str, int, int] | None = None
        metrics = _scmp_metrics(saved)
        if kind == "objectives":
            definition = SCMP.objective_definition(claim_id)
            if definition is None:
                return 404, {"success": False, "reason": "OBJECTIVE_NOT_FOUND"}
            _, objective = definition
            if SCMP.metric_value(metrics, objective[3]) < objective[4]:
                return 409, {"success": False, "reason": "OBJECTIVE_INCOMPLETE"}
            reward = ("xp", objective[5], 1)
        elif kind == "groups":
            definition = SCMP.group_definition(claim_id)
            if definition is None:
                return 404, {"success": False, "reason": "GROUP_NOT_FOUND"}
            _, group = definition
            claimed_objectives = claims.get("objectives", {})
            if not isinstance(claimed_objectives, dict) or any(
                str(objective[0]) not in claimed_objectives for objective in group["objectives"]
            ):
                return 409, {"success": False, "reason": "GROUP_INCOMPLETE"}
            reward = group["reward"]
        elif kind == "levels":
            reward = SCMP.level_reward(claim_id, option_id)
            if reward is None:
                return 404, {"success": False, "reason": "LEVEL_REWARD_NOT_FOUND"}
            if _int_or(saved.get("xp"), 0) < SCMP.level_threshold(claim_id):
                return 409, {"success": False, "reason": "LEVEL_LOCKED"}
        else:
            return 404, {"success": False, "reason": "CLAIM_NOT_FOUND"}

        reward_type, value, count = reward
        if reward_type == "pack" and not get_pack_cfg(value):
            log.error("Objectives reward pack is missing: %s", value)
            return 500, {"success": False, "reason": "REWARD_PACK_UNAVAILABLE"}
        bucket[key] = now_s()
        claims[kind] = bucket
        saved["claims"] = claims
        if reward_type == "xp":
            saved["xp"] = max(0, _int_or(saved.get("xp"), 0)) + value * count

        cursor = STATE.conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            if reward_type == "coin":
                row = cursor.execute("SELECT value FROM kv WHERE key='credits'").fetchone()
                credits = _int_or(json.loads(row[0]), 0) if row else 0
                cursor.execute(
                    "INSERT INTO kv(key,value) VALUES('credits',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(max(0, credits + value * count)),),
                )
            elif reward_type == "pack":
                data = json.dumps({"source": f"Objectives {kind} {claim_id}", "awardedAt": now_s()})
                for _ in range(count):
                    cursor.execute("INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)", (value, data))
            cursor.execute(
                "INSERT INTO kv(key,value) VALUES('scmp_campaign_v1',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(saved, separators=(",", ":")),),
            )
            revision = max(0, _int_or(STATE.get("scmp_revision", 0), 0)) + 1
            cursor.execute(
                "INSERT INTO kv(key,value) VALUES('scmp_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(revision),),
            )
            STATE.conn.commit()
        except Exception:
            STATE.conn.rollback()
            raise
    log.info("OBJECTIVES claimed kind=%s id=%s reward=%s", kind, claim_id, reward)
    claimed_campaign = _scmp_campaign()
    _log_scmp_snapshot(f"claim {kind}/{claim_id} response", claimed_campaign)
    return 200, {
        "success": True, "alreadyClaimed": False,
        "awards": [SCMP.reward_wire(reward)],
        "credits": STATE.credits(),
        **_scmp_live_fields(claimed_campaign),
    }


def route_fut(method: str, raw_path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    # ProtoHttp sends the POW auth path with a double leading slash in the
    # successful FIFA 20 trace. urlsplit("//pow/auth") would otherwise treat
    # "pow" as a network location and lose the /pow prefix.
    if raw_path.startswith("//pow/"):
        raw_path = "/" + raw_path.lstrip("/")
    parsed = urllib.parse.urlsplit(raw_path)
    path = "/" + parsed.path.lstrip("/")
    # FIX13/16: when FUT_RS4_BASE_URL ends with /ut/ and the client also
    # prefixes /ut/, the request arrives as /ut/ut/game/... Collapse doubled
    # prefixes. Also accept paths that omit the leading /ut (some builds
    # append game/... onto a base that already includes /ut/).
    while path.lower().startswith("/ut/ut/"):
        path = "/ut/" + path[7:]
    low = path.lower().rstrip("/") or "/"
    if low.startswith("/game/fifa20/") or low.startswith("/v2/"):
        path = "/ut" + path
        low = path.lower().rstrip("/") or "/"
    if low.startswith("/auth") and not low.startswith("/ut/"):
        path = "/ut" + path
        low = path.lower().rstrip("/") or "/"
    if low.startswith("/shards"):
        path = "/ut" + path
        low = path.lower().rstrip("/") or "/"
    query = urllib.parse.parse_qs(parsed.query)
    payload = safe_json(body)
    host_hdr = (headers.get("Host") or headers.get("host") or "").split(":")[0].lower()
    log.info(
        "[FUT-REQ] host=%s method=%s raw=%s normalized=%s query=%s body=%s",
        host_hdr or "-", method, raw_path, low, dict(query), (body or b"")[:300],
    )

    # ---- Nucleus / EA accounts / gateway (hosts-pinned to local TLS:443) ----
    # After accountinfo the native client often asks EA for an auth code before
    # POST /ut/auth. If those hostnames are pinned to 127.0.0.1, the requests
    # land here. Without handlers they used to get empty JSON and the client
    # aborted with no further FUT traffic.
    if (
        low.startswith("/connect/")
        or low.startswith("/proxy/identity/")
        or low.startswith("/pid/")
        or low.startswith("/ids/")
        or "accounts.ea.com" in host_hdr
        or "gateway.ea.com" in host_hdr
        or low in ("/token", "/oauth/token", "/connect/token", "/connect/auth")
    ):
        sid = _ut_sid()
        access_token = STATE.token()
        phishing = "fut20-local-phish-" + sid[-12:]
        nucleus = int(CFG["nucleus_id"])
        persona = int(CFG["persona_id"])
        FLOW.mark("NUCLEUS", "FUT", f"{method} {path}", sid)
        log.warning(
            "[FUT-HANDOFF] Nucleus/OAuth host=%s method=%s path=%s response_type=%s",
            host_hdr, method, low, query.get("response_type", [""])[0],
        )
        # The retail FUT 20 hand-off immediately following accountinfo is:
        #   GET accounts.ea.com/connect/auth?client_id=FOS-SERVER
        #       &redirect_uri=nucleus:rest&response_type=code&access_token=...
        # The client expects this compact code document, then constructs its
        # own POST /ut/auth.  Do not return an OAuth token bundle for this
        # request: several FIFA 20 builds reject that as the wrong contract.
        if low == "/connect/auth" and method == "GET" and query.get("response_type", [""])[0].lower() == "code":
            log.warning("[FUT-HANDOFF] issuing local auth code; expecting POST /ut/auth next sid=%s", sid)
            return 200, {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            }, json_bytes({"code": "LOCAL-FUT20-AUTH-CODE"})

        if low in ("/proxy/identity/pids/me", "/pid/me", "/ids/me"):
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json_bytes({
                "pid": {
                    "pidId": str(nucleus),
                    "externalRefValue": str(nucleus),
                    "type": "NUCLEUS",
                    "status": "ACTIVE",
                    "dob": str(CFG["date_of_birth"]) + "T00:00:00Z",
                },
            })

        return 200, {"Content-Type": "application/json"}, json_bytes({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": access_token,
            "id_token": access_token,
            "pid": nucleus,
            "pidId": nucleus,
            "personaId": persona,
            "nucleusId": nucleus,
            "displayName": str(CFG["display_name"]),
            "email": "local@localhost",
            "status": "ACTIVE",
            "phishingToken": phishing,
        })

    # STORE-SCOUT v27: capture the exact endpoints FIFA 20 actually uses for
    # store catalog/title/description data. This is diagnostic only and does
    # not change the working pack/coin/item behavior.
    if any(token in low for token in ("store", "pack", "purchase", "loc")):
        log.warning(
            "STORE-SCOUT REQUEST method=%s path=%s query=%s accept=%s contentType=%s body=%s",
            method,
            raw_path,
            query,
            headers.get("Accept", headers.get("accept", "")),
            headers.get("Content-Type", headers.get("content-type", "")),
            body[:1000],
        )

    # ---- POW / EASFC bootstrap --------------------------------------------
    if low == "/pow/auth":
        sid = _pow_sid()
        FLOW.mark("AUTH", "FUT", f"{method} {path}", sid)
        return 200, {"X-POW-SID": sid}, json_bytes({
            "sid": sid,
            "serverTime": now_s(),
            "lastOnlineTime": now_s(),
        })

    if low.startswith("/pow/"):
        if low == "/pow/bank/user/account":
            return 200, {}, json_bytes({
                "currencies": [{
                    "currency": "pow_funds",
                    "funds": 0,
                    "fundsCapInfo": [
                        {"period": "daily", "fundsEarned": 0},
                        {"period": "weekly", "fundsEarned": 0},
                    ],
                }]
            })
        if low == "/pow/store/game/fifa20/catalog/list":
            return 200, {}, json_bytes({
                "catalogs": [
                    {"catalogId": 0, "name": "FUT Store"},
                    {"catalogId": 1, "name": "FIFAPoints"},
                ]
            })
        if "/catalog/0/item/list" in low:
            return 200, {}, json_bytes({"items": [], "endOfList": True})
        return 200, {}, json_bytes({})

    # ---- FUT authentication -----------------------------------------------
    if low in ("/ut/auth", "/ut/auth/v1", "/ut/auth/v2", "/auth", "/auth/v1", "/auth/v2"):
        if method == "DELETE":
            FLOW.mark("FUT_AUTH", "FUT", f"{method} {path}", _ut_sid())
            return 200, {}, json_bytes({})
        sid = _ut_sid()
        FLOW.mark("FUT_AUTH", "FUT", f"{method} {path}", sid)
        host = str(CFG.get("local_host", "127.0.0.1"))
        port = int(CFG["fut_port"])
        log.warning(
            "[FIX21] /ut/auth method=%s host=%s body=%s -> sid=%s",
            method, host_hdr, (body or b"")[:800], sid,
        )
        # FIFA 20 native expects a richer auth payload + phishing token that
        # subsequent phishing/trusteddevice calls can echo.
        phishing = "fut20-local-phish-" + sid[-12:]
        now = now_s()
        game_sku = str(CFG.get("game_sku", "FFA20PCC"))
        fut_sku = str(CFG.get("fut_sku", "FUT20PCC"))
        sku_access = {
            game_sku: now,
            fut_sku: now,
            "FFA20PCC": now,
            "FFA20PCE": now,
            "FFA20PCA": now,
            "FFA20PCG": now,
            "FUT20PCC": now,
            "FUT20PCE": now,
            "FUT20PCA": now,
            "FUT20PCG": now,
        }
        body = {
            "sid": sid,
            "sessionId": sid,
            "protocol": "http",
            "ipPort": f"{host}:{port}",
            "serverTime": now,
            # In FIFA 20's /ut/auth contract `token` is the phishing token
            # the client sends back in X-UT-PHISHING-TOKEN; it is not the
            # Nucleus bearer token used to acquire the auth code.
            "phishingToken": phishing,
            "remoteConfigEnabled": True,
            "nucleusPersonaId": int(CFG["persona_id"]),
            "personaName": str(CFG["display_name"]),
            "nucleusUserId": int(CFG["nucleus_id"]),
            "authCode": "LOCAL-FUT20-AUTH-CODE",
            "token": phishing,
            "sku": fut_sku,
            "skuAccessList": sku_access,
            "onlineAccess": True,
            "returningUser": True,
            "credits": STATE.credits(),
            "clubId": int(CFG["club_id"]),
            "clubName": str(CFG["club_name"]),
            "hubUrl": f"http://{host}:{port}/ut/game/fifa20/hub",
        }
        log.warning("[FIX21] /ut/auth enriched sid=%s hubUrl advertised", sid)
        return 200, {
            "X-UT-SID": sid,
            "X-UT-PHISHING-TOKEN": phishing,
            "Easw-Session-Data-Nucleus-Id": str(CFG["nucleus_id"]),
            "Nucleus-Id": str(CFG["nucleus_id"]),
            "Persona-Id": str(CFG["persona_id"]),
            "X-UT-Route": f"http://{host}:{port}/ut/game/fifa20/hub",
        }, json_bytes(body)

    # Accept shards with or without trailing slash / query already stripped
    if low in ("/ut/shards", "/ut/shards/v2", "/shards", "/shards/v2"):
        FLOW.mark("FUT_INIT", "FUT", f"{method} {path}", _ut_sid())
        host = str(CFG.get("local_host", "127.0.0.1"))
        port = int(CFG["fut_port"])
        return 200, {}, json_bytes({
            "shardInfo": [{
                "customdata1": f"{host}:{port}",
                "customdata2": f"http://{host}:{port}",
                "name": "FIFA20LocalFUT",
                "platform": "pc",
                "clientFacingIpPort": f"{host}:{port}",
                "isOnline": True,
            }]
        })

    # The Cards/FUT bootstrap expects a JSON array here.  Returning the generic
    # unknown-route object ({}) makes the retail client reject the response and
    # stop during FUT initialization.  An empty array means there are no remote
    # resource overrides, matching the working FIFA 21 local-server contract.
    if low in (
        "/ut/game/fifa20/resourcemapping",
        "/ut/v2/game/fifa20/resourcemapping",
    ):
        FLOW.mark("RESOURCE_MAPPING", "FUT", f"{method} {path}", _ut_sid())
        return 200, {}, json_bytes([])

    # FIFA 20 PC uses both /user/accountinfo and /v2/user/accountinfo.
    if low in (
        "/ut/game/fifa20/user/accountinfo",
        "/ut/game/fifa20/v2/user/accountinfo",
        "/ut/v2/game/fifa20/user/accountinfo",
    ):
        FLOW.mark("ACCOUNT", "FUT", f"{method} {path}", _ut_sid())
        # Issue a local SID so later hub/auth calls have a session even if
        # the client skipped the formal /ut/auth step on this path.
        sid = _ut_sid()
        body = account_info()
        log.info(
            "[FIX22] accountinfo schema=retail-preauth persona=%s sku=%s sid=%s query=%s; expecting /connect/auth then /ut/auth",
            CFG["persona_id"], CFG["game_sku"], sid, dict(query),
        )
        # This remains a normal FUT response with local identity headers.  The
        # body intentionally contains no sid/token/authCode: those belong to
        # the *next* /ut/auth response.
        return 200, {
            "X-UT-SID": sid,
            "Easw-Session-Data-Nucleus-Id": str(CFG["nucleus_id"]),
            "Nucleus-Id": str(CFG["nucleus_id"]),
            "Persona-Id": str(CFG["persona_id"]),
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        }, json_bytes(body)

    # Handoff: userMassInfo endpoint for LOCAL auth mode
    # FIFA in LOCAL mode (AUTH_TYPE=LOCAL) calls this after accountinfo to get bulk startup data
    if low in ("/ut/usermassinfo", "/ut/v2/usermassinfo"):
        FLOW.mark("MASS_INFO", "FUT", f"{method} {path}", _ut_sid())
        sid = _ut_sid()
        log.info("[HANDOFF] userMassInfo called - LOCAL auth mode startup data")
        log.info("[HANDOFF] userMassInfo returning startup payload: userInfo + squad + loans")
        return 200, {
            "X-UT-SID": sid,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        }, json_bytes(user_mass_info())

    # Static client-side localization/config requests observed in FUT.
    if low == "/fut/loc/pc/leaderboards.eng_us.xml":
        return 404, {"Content-Type": "text/plain"}, b"not found"
    if low == "/fut/tutorials/cmn":
        return 404, {"Content-Type": "text/plain"}, b"not found"
    if low == "/fut/packs/loc/storepackdescriptions.en_us.xml":
        # FUT store pack localization is an XLIFF translation file.
        # The native client resolves the visible title/description through
        # FUT_STORE_PACK_<packId>_NAME / _DESC keys rather than from the
        # human-readable name fields in purchasegroup/all.
        settings = load_pack_settings()
        loc_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<xliff version="1.2">',
            '  <file original="storepackdescriptions" source-language="en-US" datatype="plaintext">',
            '    <body>',
        ]

        exported = []
        for pid_s, cfg in sorted(
            settings.get("packs", {}).items(),
            key=lambda kv: int(kv[0])
        ):
            try:
                pid = int(pid_s)
            except Exception:
                continue

            name = str(cfg.get("name", f"Custom Pack {pid}") or f"Custom Pack {pid}")
            bio = str(cfg.get("bio", "") or "")

            name_xml = html.escape(name, quote=False)
            bio_xml = html.escape(bio, quote=False)

            # Desktop FUT store name.
            key_name = f"FUT_STORE_PACK_{pid}_NAME"
            loc_lines.append(
                f'      <trans-unit id="{key_name}" resname="{key_name}">'
                f'<source>{name_xml}</source></trans-unit>'
            )

            # Pack detail/description panel.
            key_desc = f"FUT_STORE_PACK_{pid}_DESC"
            loc_lines.append(
                f'      <trans-unit id="{key_desc}" resname="{key_desc}">'
                f'<source>{bio_xml}</source></trans-unit>'
            )

            # FIFA web/mobile-era localization files also expose a MOBILE name.
            # Keeping it here is harmless for the PC client and improves
            # compatibility with builds that request the same key family.
            key_mobile = f"FUT_STORE_PACK_{pid}_NAME_MOBILE"
            loc_lines.append(
                f'      <trans-unit id="{key_mobile}" resname="{key_mobile}">'
                f'<source>{name_xml}</source></trans-unit>'
            )
            exported.append((pid, name, bio))

        loc_lines.extend([
            '    </body>',
            '  </file>',
            '</xliff>',
        ])
        xml = ("\n".join(loc_lines)).encode("utf-8")

        log.warning("STORE-SCOUT LOC RESPONSE xml=%s", xml.decode("utf-8", errors="replace"))
        log.info("STORE PACK LOC XLIFF packs=%s", exported)

        return 200, {
            "Content-Type": "application/xml; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }, xml

    # Static presentation/config files consumed in parallel immediately after
    # /ut/auth.  Serve their real content and MIME type instead of the generic
    # unknown-route JSON object.
    startup_content = _bundled_fut_startup_content(low)
    if startup_content is not None and method == "GET":
        content_body, content_type = startup_content
        log.info("Serving bundled FUT startup content path=%s bytes=%d", low, len(content_body))
        return 200, {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }, content_body

    if method == "GET" and low.startswith("/fut/sbc/") and low.endswith(".png"):
        set_image = re.fullmatch(
            r"/fut/sbc/gen4/sets/images/sbc_set_image_(\d+)-localfut20-sbc(?:-v[23456789])?\.png",
            low,
        )
        if set_image:
            set_id = int(set_image.group(1)) - 1000000
            asset = ROOT / "assets" / "sbc_sets" / f"{set_id}.png"
            if set_id in SET_BY_ID and asset.is_file():
                return 200, {
                    "Content-Type": "image/png",
                    "Cache-Control": "public, max-age=86400",
                }, asset.read_bytes()
        return 200, {
            "Content-Type": "image/png",
            "Cache-Control": "no-store",
        }, _SBC_PLACEHOLDER_PNG

    # Card-shell textures and individual player heads are not part of the
    # database. Cache the original FIFA 20 CDN assets on first request. A
    # missing binary must be a real 404: returning the generic JSON object here
    # makes the native renderer treat two bytes of `{}` as a DDS texture.
    if method == "GET":
        dynamic_content = _dynamic_fut20_content(path)
        if dynamic_content is not None:
            status, content_body, content_type = dynamic_content
            is_player_head = _PLAYER_HEAD_RE.fullmatch(path.lower()) is not None
            return status, {
                "Content-Type": content_type,
                # Rapid Preview Selection can issue overlapping texture loads.
                # Revalidating each player head and closing that response's
                # connection prevents FIFA from retaining a prior card's DDS.
                "Cache-Control": (
                    "no-store, no-cache, must-revalidate, max-age=0"
                    if is_player_head else "public, max-age=31536000, immutable"
                ),
                **({"Pragma": "no-cache", "Connection": "close"} if is_player_head else {}),
            }, content_body

    # Any other FUT presentation binary we can't serve (an alternate "dynamic
    # image" / gallery path for a player head, misc textures). Try to resolve it
    # as a head first so a real portrait we already have is used; otherwise a
    # real 404 -- never the generic `{}` object, which the native renderer
    # decodes as texture bytes and paints over the card.
    if method == "GET" and low.startswith("/fut/") and low.endswith(
        (".dds", ".png", ".jpg", ".jpeg", ".webp", ".big", ".bin", ".ktx", ".dat")
    ):
        head_id = re.search(r"/p?(\d{3,})\.(?:dds|png|jpg|jpeg|webp)$", low)
        if head_id:
            resolved = _dynamic_fut20_content(
                f"/fut/playerheads/g4/single/p{int(head_id.group(1))}.dds"
            )
            if resolved is not None and resolved[0] == 200:
                return 200, {"Content-Type": "application/octet-stream",
                             "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                             "Pragma": "no-cache", "Connection": "close"}, resolved[1]
        return 404, {"Content-Type": "text/plain"}, b""

    # Exact FIFA 20 game namespace.
    if low == "/ut/game/fifa20/settings":
        return 200, {}, json_bytes({"configs": [
            {"type": "IsStoreEnabled", "value": "1"},
            {"type": "cardPackStoreEnabled", "value": "1"},
            {"type": "pointsPackStoreEnabled", "value": "1"},
            {"type": "enableDraftMode", "value": "1"},
            {"type": "enableOfflineDraftMode", "value": "1"},
            {"type": "enableSinglePlayerDraftMode", "value": "1"},
            {"type": "enableSquadBuildingChallenges", "value": "1"},
            {"type": "isSBCEnabled", "value": "1"},
            {"type": "enableRivals", "value": "1"},
            {"type": "enableDivisionRivals", "value": "1"},
            {"type": "divisionRivalsEnabled", "value": "1"},
            {"type": "enableFUTChampions", "value": "1"},
            {"type": "futChampionsEnabled", "value": "1"},
            {"type": "enableWeekendLeague", "value": "1"},
            {"type": "enableSeasonalCampaigns", "value": "1"},
            {"type": "enablePlayerPicks", "value": "1"},
            {"type": "isPlayerPicksTemporaryStorageNotEmpty",
             "value": "1" if PICK_STATE.pending_flag(STATE) else "0"},
            {"type": "squadBattlesEnabled", "value": "1"},
            {"type": "squadBattleEnabled", "value": "1"},
            {"type": "enableSquadBattles", "value": "1"},
            {"type": "squadBattleFeatureEnabled", "value": "1"},
            {"type": "singlePlayerSquadBattleEnabled", "value": "1"},
            {"type": "squadBattleCouchPlayEnabled", "value": "1"},
            {"type": "enableOfflineSquadBattles", "value": "1"},
            # General Leaderboards has no local backing service.  This is the
            # retail feature gate CardsDLL exposes for that navigation entry;
            # disabling it prevents the native screen from constructing a
            # missing leaderboard model and dereferencing a null pointer.
            {"type": "usLeaderboardAvailable", "value": "0"},
        ]})

    # These exact endpoints and response shapes are ported from unknown20's
    # verified FIFA 20 runtime. The full opponent squads are important: a
    # menu-only event document leaves the client waiting on the faded intro.
    if low == "/ut/game/fifa20/sqbt/user/hub" and method == "GET":
        FLOW.mark("SQUAD_BATTLES", "FUT", f"{method} {path}", _ut_sid())
        _seasonal_set_mode("")
        return 200, {"Cache-Control": "no-store"}, json_bytes(_native_squad_battle_hub())

    sqbt_opponent = re.fullmatch(r"/ut/game/fifa20/sqbt/user/opponentsquad/(\d+)", low)
    if sqbt_opponent and method == "GET":
        requested = int(sqbt_opponent.group(1))
        state = _sb_state()
        opponent = next(
            (entry for entry in state.get("opponents", [])
             if isinstance(entry, dict) and _int_or(entry.get("id"), -1) == requested),
            None,
        )
        squad = (
            json.loads(json.dumps(opponent.get("squad", {})))
            if isinstance(opponent, dict) and isinstance(opponent.get("squad"), dict)
            else _native_squad_battle_opponent(requested)
        )
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            squad)

    if (low.startswith("/ut/game/fifa20/featuredsquad/") and method == "GET"
            and not low.startswith("/ut/game/fifa20/featuredsquad/user/stats")):
        consumer = _market_first(query, "featureConsumerId", default="sqbttotw").lower()
        if low.endswith("/stats"):
            event = _native_squad_battle_event()
            feature_stats = _sb_feature_stats_payload(_sb_state(), consumer)
            return 200, {"Cache-Control": "no-store"}, json_bytes({
                **feature_stats,
                "eventStartTime": event["eventStartTime"],
                "eventEndTime": event["eventEndTime"],
                "secondsUntilEnd": event["secondsUntilEnd"],
            })
        if low.endswith("/history"):
            return 200, {"Cache-Control": "no-store"}, json_bytes(
                _totw_featured_history())
        detail = re.fullmatch(r"/ut/game/fifa20/featuredsquad/(\d+)", low)
        if detail:
            requested = int(detail.group(1))
            if requested == _SB_FEATURED_CUSTOM_ID or consumer == "sqbt":
                return 200, {"Cache-Control": "no-store"}, json_bytes(
                    _sb_custom_featured_opponent(_sb_state())["squad"])
            return 200, {"Cache-Control": "no-store"}, json_bytes(
                _native_totw_squad(requested))
        return 404, {"Cache-Control": "no-store"}, json_bytes({"error": "featured squad not found"})

    if low.startswith("/ut/game/fifa20/featuredsquad/user/stats") and method == "GET":
        event = _native_squad_battle_event()
        consumer = _market_first(query, "featureConsumerId", default="sqbttotw").lower()
        feature_stats = _sb_feature_stats_payload(_sb_state(), consumer)
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            **feature_stats,
            "rank": 0,
            "gamesRemaining": event["gamesRemaining"],
            "remainingMatches": event["remainingMatches"],
            "matchesRemaining": event["remainingMatches"],
            "maxMatches": event["maxMatches"],
            "eventStartTime": event["eventStartTime"],
            "eventEndTime": event["eventEndTime"],
            "secondsUntilStart": event["secondsUntilStart"],
            "secondsUntilEnd": event["secondsUntilEnd"],
        })

    if ("prize" in low or "reward" in low or "leaderboard" in low
            or "top100" in low or low.endswith("/rank") or low.endswith("/history")) \
            and ("sqbt" in low or "squadbattle" in low or "featuredsquad" in low):
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _sb_route(low, method, query, payload))

    if ("sqbt" in low or "squadbattle" in low) and (
            low.endswith("/refresh") or "opponent" in low or low.endswith("/hub")) \
            and method in ("GET", "POST", "PUT"):
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _sb_route(low, method, query, payload))

    if ("sqbt" in low or "squadbattle" in low or "squad/battle" in low
            or "squad_battle" in low or "featuredsquad" in low) \
            and method in ("GET", "POST", "PUT"):
        opponents = [_native_squad_battle_opponent(i)
                     for i in range(len(_SQUAD_BATTLE_PRESETS))]
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "squads": opponents, "opponents": opponents,
            "squadBattles": opponents, "opponentSquads": opponents,
            "availableSquads": opponents, "squadBattleSquads": opponents,
            "items": opponents,
            "sqbtOppSquadList": {
                "nextSquadRefreshTimeStamp": now_s() + 24 * 60 * 60,
                "sqbtOppSquads": opponents,
            },
            "refreshAvailable": True, "matchesRemaining": 40,
            **_native_squad_battle_event(),
        })

    if low in ("/fut/seasonal/seasonalconfig.json",
               "/ut/game/fifa20/seasonal/seasonalconfig.json") and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(_seasonal_config_json())

    if low in ("/ut/game/fifa20/rivals", "/ut/game/fifa20/rivals/user/hub") \
            and method == "GET":
        FLOW.mark("RIVALS", "FUT", f"{method} {path}", _ut_sid())
        _seasonal_set_mode("rivals")
        rivals_progress = _rivals_progress()
        if (rivals_progress["weeklyGames"] > 0
                and rivals_progress["rewardsClaimedWeek"] != _seasonal_window()["weekIndex"]):
            _rivals_claim_rewards()
        return 200, {"Cache-Control": "no-store"}, json_bytes(_rivals_hub_json())

    if (re.fullmatch(r"/ut/game/fifa20/rivals/user/(?:rewards|prize|prizes|claim)", low)
            or re.search(r"/rivals/.*(?:reward|prize|claim)", low)) \
            and method in ("GET", "POST", "PUT"):
        return 200, {"Cache-Control": "no-store"}, json_bytes(_rivals_claim_rewards())

    if low.startswith("/ut/game/fifa20/rivals/") and method in ("GET", "POST", "PUT"):
        _seasonal_set_mode("rivals")
        return 200, {"Cache-Control": "no-store"}, json_bytes(_rivals_hub_json())

    if low in ("/ut/game/fifa20/champion", "/ut/game/fifa20/champion/user/hub") \
            and method == "GET":
        FLOW.mark("CHAMPIONS", "FUT", f"{method} {path}", _ut_sid())
        _seasonal_set_mode("champion")
        champion_progress = _champion_progress()
        if (champion_progress["games"] >= _CHAMPION_FINALS_MATCHES
                and champion_progress["rewardsClaimedWeek"] != _seasonal_window()["weekIndex"]):
            _champion_claim_rewards()
        return 200, {"Cache-Control": "no-store"}, json_bytes(_champion_hub_json())

    if low == "/ut/game/fifa20/champion/events/upcoming" and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes({"championEvents": []})
    if low == "/ut/game/fifa20/champion/user/stats" and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(_champion_stats_json())
    if low == "/ut/game/fifa20/champion/leaderboard" and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(_champion_leaderboard_json())
    if re.fullmatch(r"/ut/game/fifa20/champion/type/[^/]+/period/[^/]+/friends/stats", low) \
            and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            {"entries": [], "friends": [], "stats": [], "totalCount": 0})
    if (re.fullmatch(r"/ut/game/fifa20/champion/user/(?:prize|prizes|rewards|claim)", low)
            or re.search(r"/champion/.*(?:reward|prize|claim)", low)) \
            and method in ("GET", "POST", "PUT"):
        return 200, {"Cache-Control": "no-store"}, json_bytes(_champion_claim_rewards())
    if low.startswith("/ut/game/fifa20/champion/") and method in ("GET", "POST", "PUT"):
        _seasonal_set_mode("champion")
        return 200, {"Cache-Control": "no-store"}, json_bytes(_champion_hub_json())

    # ---- Native Player Picks ------------------------------------------------
    # A completed SBC grants a real miscPlayerPick token. FIFA explicitly
    # redeems that token, opens the temporary-choice model, and POSTs the chosen
    # player resource to the selection route. This is the retail FIFA 20 flow.
    if low == "/ut/game/fifa20/item/nontargeted" and method in ("POST", "PUT"):
        token_id = _int_or(_market_first(query, "itemId", "id"), 0)
        if token_id <= 0 and isinstance(payload, dict):
            token_id = _sb_find_int(payload, ("itemId", "id", "playerPickItemId"), 0)
        status, response = _open_native_player_pick(token_id)
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    if low in (
        "/ut/game/fifa20/playerpicks",
        "/ut/game/fifa20/playerpicks/pending",
        "/ut/game/fifa20/playerpick/pending",
    ) and method == "GET":
        response = _native_player_pick_payload()
        log.info("PLAYER PICK pending count=%d", response.get("count", 0))
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    selection_route = re.fullmatch(
        r"/ut/game/fifa20/playerpicks/item/([0-9]+)/(?:select|selection|claim)", low
    )
    legacy_selection_route = re.fullmatch(
        r"/ut/game/fifa20/playerpicks?/([0-9]+)/(?:select|selection|claim)", low
    )
    if (selection_route or legacy_selection_route) and method in ("POST", "PUT", "DELETE"):
        selected_value = int((selection_route or legacy_selection_route).group(1))
        current = PICK_STATE.active(STATE)
        if current:
            active_items = [item for item in current.get("items", []) if isinstance(item, dict)]
            selected_item = next((
                item for item in active_items
                if selected_value in {
                    _int_or(item.get("id"), 0), _int_or(item.get("resourceId"), 0),
                    _int_or(item.get("assetId"), 0),
                }
            ), None)
            duplicate = bool(
                selected_item
                and _int_or(selected_item.get("resourceId"), 0) in STATE.owned_player_resource_ids()
            )
            receipt = PICK_STATE.select(STATE, selected_value, duplicate)
        else:
            receipt = PICK_STATE.receipt(STATE, selected_value)
        if not receipt:
            return 404, {"Cache-Control": "no-store"}, json_bytes({
                "success": False, "reason": "PLAYER_PICK_SELECTION_NOT_FOUND",
            })
        # FIFA 20 expects the chosen native item itself at the response root.
        # Wrapping this in itemData/playerPicks makes the client crash after the
        # animation even though the database mutation succeeded.
        native_selected = _native_player_item(dict(receipt["item"]))
        log.info(
            "PLAYER PICK selected token=%d candidate=%d resource=%d duplicate=%s",
            _int_or(receipt.get("token_id"), 0),
            _int_or(receipt.get("candidate_id"), 0),
            _int_or(receipt.get("resource_id"), 0),
            bool(native_selected.get("duplicate")),
        )
        return 200, {"Cache-Control": "no-store"}, json_bytes(native_selected)

    # ---- FIFA 20 Squad Building Challenges ---------------------------------
    if low == "/ut/game/fifa20/sbs/sets" and method == "GET":
        payload_out = _sbc_sets_payload()
        set_count = sum(len(category.get("sets", [])) for category in payload_out["categories"])
        log.info("SBC SETS categories=%d sets=%d", len(payload_out["categories"]), set_count)
        return 200, {"Cache-Control": "no-store"}, json_bytes(payload_out)

    sbc_set_challenges = re.fullmatch(r"/ut/game/fifa20/sbs/setid/(\d+)/challenges", low)
    if sbc_set_challenges and method == "GET":
        set_id = int(sbc_set_challenges.group(1))
        payload_out = _sbc_challenges_payload(set_id)
        if payload_out is None:
            return 404, {}, json_bytes({"error": "SBC_SET_NOT_FOUND", "setId": set_id})
        log.info("SBC CHALLENGES set=%d count=%d", set_id, len(payload_out["challenges"]))
        return 200, {"Cache-Control": "no-store"}, json_bytes(payload_out)

    sbc_squad_route = re.fullmatch(r"/ut/game/fifa20/sbs/challenge/(\d+)/squad", low)
    if sbc_squad_route:
        challenge_id = int(sbc_squad_route.group(1))
        catalog_entry = CHALLENGE_BY_ID.get(challenge_id)
        if not catalog_entry:
            return 404, {}, json_bytes({"error": "SBC_CHALLENGE_NOT_FOUND", "challengeId": challenge_id})
        set_entry, challenge_entry = catalog_entry
        if method == "GET":
            return 200, {"Cache-Control": "no-store"}, json_bytes(_sbc_squad_response(set_entry, challenge_entry))
        if method in ("PUT", "POST"):
            saved = STATE.save_sbc_squad(challenge_id, payload, int(challenge_entry.get("players", 11)))
            response = _sbc_squad_response(set_entry, challenge_entry)
            response["saved"] = True
            response["savedPlayerCount"] = len(saved.get("players", []))
            log.info("SBC SQUAD SAVE challenge=%d players=%d", challenge_id, response["savedPlayerCount"])
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    sbc_validate_route = re.fullmatch(r"/ut/game/fifa20/sbs/challenge/(\d+)/validate", low)
    if sbc_validate_route and method in ("GET", "POST"):
        challenge_id = int(sbc_validate_route.group(1))
        catalog_entry = CHALLENGE_BY_ID.get(challenge_id)
        if not catalog_entry:
            return 404, {}, json_bytes({"error": "SBC_CHALLENGE_NOT_FOUND", "challengeId": challenge_id})
        _, challenge_entry = catalog_entry
        if isinstance(payload, dict) and isinstance(payload.get("players"), list):
            STATE.save_sbc_squad(challenge_id, payload, int(challenge_entry.get("players", 11)))
        valid, checks, _ = _sbc_evaluate(challenge_entry, STATE.sbc_squad(challenge_id))
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "challengeId": challenge_id, "valid": valid, "meetsRequirements": valid,
            "requirements": checks,
        })

    sbc_challenge_route = re.fullmatch(r"/ut/game/fifa20/sbs/challenge/(\d+)(/submit)?", low)
    if sbc_challenge_route:
        challenge_id = int(sbc_challenge_route.group(1))
        catalog_entry = CHALLENGE_BY_ID.get(challenge_id)
        if not catalog_entry:
            return 404, {}, json_bytes({"error": "SBC_CHALLENGE_NOT_FOUND", "challengeId": challenge_id})
        set_entry, challenge_entry = catalog_entry
        explicit_submit = bool(sbc_challenge_route.group(2))

        if method == "GET":
            return 200, {"Cache-Control": "no-store"}, json_bytes(_sbc_squad_response(set_entry, challenge_entry))

        if method in ("POST", "PUT"):
            incoming_players = (
                isinstance(payload, dict)
                and (
                    isinstance(payload.get("players"), list)
                    or (
                        isinstance(payload.get("squad"), dict)
                        and isinstance(payload["squad"].get("players"), list)
                    )
                )
            )
            # FIFA 20 opens the native SBC editor with an empty POST to this
            # route. It expects the singular squad/workspace document back.
            # Treating that request as a failed submission makes the game fall
            # back to the active XI and later eject the user from FUT on Back.
            # A plain base-route POST is always the editor-open action, even if
            # a prior session left a valid workspace. The 0.9.18 shortcut
            # consumed that saved squad merely by opening the challenge.
            # Native submission is the later PUT (with the validation query),
            # a POST carrying players/query, or an explicit /submit request.
            query_is_submit = any(
                str(key).lower() == "skipusersquadvalidation" for key in query
            )
            if (method == "POST" and not explicit_submit and not query_is_submit
                    and not incoming_players):
                log.info("SBC WORKSPACE OPEN challenge=%d isolated=true", challenge_id)
                return 200, {"Cache-Control": "no-store"}, json_bytes(
                    _sbc_squad_response(set_entry, challenge_entry)
                )

            repeatable = bool(set_entry.get("repeatable", False))
            challenge_counts, _ = STATE.sbc_completion_counts()
            if challenge_counts.get(challenge_id, 0) and not repeatable:
                # Native FIFA can retry a request after a connection hiccup.
                # Treat that retry as idempotent and never issue rewards twice.
                return 200, {"Cache-Control": "no-store"}, json_bytes({
                    "success": True, "challengeComplete": True, "alreadyCompleted": True,
                    "challengeId": challenge_id, "setId": int(set_entry["set_id"]),
                    "awards": [], **_credits_payload(),
                })

            if incoming_players:
                STATE.save_sbc_squad(challenge_id, payload, int(challenge_entry.get("players", 11)))
            saved = STATE.sbc_squad(challenge_id)
            valid, checks, item_ids = _sbc_evaluate(challenge_entry, saved)
            if not valid:
                log.warning("SBC SUBMIT REJECT challenge=%d checks=%s", challenge_id, checks)
                return 200, {"Cache-Control": "no-store"}, json_bytes({
                    "success": False,
                    "challengeComplete": False,
                    "meetsRequirements": False,
                    "challengeId": challenge_id,
                    "setId": int(set_entry["set_id"]),
                    "validation": checks,
                    "error": {"code": "SBC_REQUIREMENTS_NOT_MET", "reason": "Squad does not meet the challenge requirements"},
                    **_credits_payload(),
                })

            try:
                result = STATE.complete_sbc(
                    challenge_id=challenge_id,
                    set_id=int(set_entry["set_id"]),
                    item_ids=item_ids,
                    challenge_awards=list(challenge_entry.get("awards", [])),
                    set_challenge_ids=[int(entry["challenge_id"]) for entry in set_entry.get("challenges", [])],
                    set_awards=list(set_entry.get("awards", [])),
                    repeatable=repeatable,
                )
            except ValueError as exc:
                log.warning("SBC SUBMIT CONFLICT challenge=%d error=%s", challenge_id, exc)
                return 200, {"Cache-Control": "no-store"}, json_bytes({
                    "success": False, "challengeComplete": False,
                    "challengeId": challenge_id, "setId": int(set_entry["set_id"]),
                    "error": {"code": "SBC_SUBMISSION_CONFLICT", "reason": str(exc)},
                    **_credits_payload(),
                })

            awarded = _sbc_awards_payload(list(result.pop("awards", [])))
            response = {
                **result,
                "success": True,
                "challengeComplete": True,
                "meetsRequirements": True,
                "validation": checks,
                "awards": awarded,
                "rewards": awarded,
                **_credits_payload(),
            }
            log.warning(
                "SBC COMPLETE challenge=%d set=%d consumed=%s setComplete=%s rewards=%s",
                challenge_id, int(set_entry["set_id"]), item_ids, response["setCompleted"], awarded,
            )
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low.startswith("/ut/game/fifa20/phishing/trusteddevice") or low.startswith("/ut/game/fifa20/phishing"):
        sid = _ut_sid()
        tok = "fut20-local-phish-" + sid[-12:]
        return 200, {"X-UT-SID": sid, "X-UT-PHISHING-TOKEN": tok}, json_bytes({
            "trusted": True, "exists": True, "locked": False, "changed": False,
            "token": tok, "isTrusted": True, "phishingToken": tok,
            "completed": True, "answerRequired": False, "attempts": 0,
            "question": "", "questionId": 0,
        })

    if low == "/ut/game/fifa20/match/reset":
        state = _sb_state()
        if state.get("matchInProgress"):
            state["matchInProgress"] = False
            _sb_save(state)
        return 200, {}, json_bytes({"reset": True})

    if low == "/ut/game/fifa20/user":
        FLOW.mark("FUT_INIT", "FUT", f"{method} {path}", _ut_sid())
        # The retail client refreshes /user immediately after match settlement.
        # Never allow an intermediary/native HTTP cache to reuse the pre-match
        # model; the payload contains the current objectivesRevision + campaign.
        return 200, {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }, json_bytes(_native_user())

    if low == "/ut/game/fifa20/user/club":
        FLOW.mark("CLUB", "FUT", f"{method} {path}", _ut_sid())
        return 200, {}, json_bytes(club_info())

    if low == "/ut/game/fifa20/userdata":
        return 200, {}, json_bytes({"onlineELORating": 0, "onlineRatedUser": False, "accountResetCount": 0})

    if low == "/ut/v2/game/fifa20/store/transaction":
        return 200, {}, json_bytes({"state": "NOTRANSACTION"})

    if low == "/ut/game/fifa20/clientdata/tutorialpopups":
        return 200, {}, json_bytes({"entries": []})
    if low == "/ut/game/fifa20/clientdata/onboarding":
        # The local profile is pre-provisioned with its club and active squad.
        # Acknowledge writes, then report the native completed state.
        return 200, {}, json_bytes(_completed_onboarding_client_data())
    if low == "/ut/game/fifa20/clientdata/squad":
        return 200, {}, json_bytes({"entries": [{"key": 5, "value": 0}]})
    if low == "/ut/game/fifa20/clientdata/pilesize":
        return 200, {}, json_bytes({"entries": [{"key": 2, "value": 100}, {"key": 4, "value": 100}]})
    if low == "/ut/game/fifa20/clientdata/userhubdata":
        campaign = _scmp_campaign()
        response = {
            "entries": [{"key": 0, "value": 13}, {"key": 1, "value": 12}],
            # FIFA writes this document during every post-match hub transition.
            # Carry the native SCMP invalidation bit on that already-observed
            # transition rather than fabricating a logout or relying on Blaze.
            "campaignId": _int_or(campaign.get("campaignId"), SCMP.CAMPAIGN_ID),
            "objectivesRevision": _int_or(campaign.get("revision"), 0),
            "needsGroupsRefresh": bool(campaign.get("needsGroupsRefresh")),
        }
        _log_scmp_snapshot(f"clientdata/userHubData {method} response", campaign)
        return 200, _scmp_response_headers(_int_or(campaign.get("revision"), 0)), json_bytes(response)
    if low == "/ut/game/fifa20/clientdata/managerquest":
        return 200, {}, json_bytes({"entries": [{"key": 0, "value": 148528}, {"key": 1, "value": 148528}, {"key": 2, "value": 0}]})
    if low == "/ut/game/fifa20/clientdata/loanplayerreward":
        return 200, {}, json_bytes(_completed_loan_client_data())
    if low == "/ut/game/fifa20/clientdata/squadbuildingsets":
        # FIFA 20 writes this immediately after a successful mini-challenge.
        # Acknowledge the native document and persist it so the subsequent GET
        # sees the same completed-set marker instead of falling through to {}.
        key = "clientdata_squad_building_sets"
        if method in ("PUT", "POST"):
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            document = {"entries": entries if isinstance(entries, list) else []}
            STATE.set(key, document)
            return 200, {"Cache-Control": "no-store"}, json_bytes(document)
        saved = STATE.get(key, {"entries": []})
        if not isinstance(saved, dict) or not isinstance(saved.get("entries"), list):
            saved = {"entries": []}
        return 200, {"Cache-Control": "no-store"}, json_bytes(saved)

    if low == "/ut/game/fifa20/loan/players":
        return 200, {}, json_bytes({"loans": []})

    if low == "/ut/game/fifa20/squad/list" and method == "GET":
        # FutSquadListServerResponse requires activeSquadId and complete squad
        # DTOs. Its native factory dereferences players[0..22] immediately.
        return 200, {"Cache-Control": "no-store"}, json_bytes(_native_squad_list())

    if low == "/ut/game/fifa20/squad" and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(_native_squad_list())

    if low == "/ut/game/fifa20/squad" and method in ("PUT", "POST"):
        src = dict(payload) if isinstance(payload, dict) else {}
        has_id = _int_or(src.get("id") or src.get("squadId"), 0) > 0
        if method == "POST":
            # FIFA 20's empty "Create New Concept" template is wire-identical
            # to a normal empty squad and incorrectly says REGULAR_SQUAD. In the
            # native flow observed here it is posted from the currently selected
            # empty template; preserve that concept intent immediately instead
            # of waiting for the first concept player to be added. The name check
            # also covers a direct empty concept creation from a populated squad.
            requested_id = _int_or(src.get("id") or src.get("squadId"), 0)
            template = STATE.get_squad(requested_id) if requested_id > 0 else None
            template_is_empty = bool(
                isinstance(template, dict)
                and not any(
                    isinstance(slot, dict)
                    and (
                        _int_or(slot.get("itemId") or slot.get("id"), 0) > 0
                        or _int_or(slot.get("conceptResourceId"), 0) > 0
                    )
                    for slot in template.get("players", [])
                )
            )
            requested_name = str(src.get("squadName") or src.get("name") or "").casefold()
            named_as_concept = any(token in requested_name for token in ("concept", "dream"))
            explicit_concept = (
                bool(src.get("dreamSquad", False))
                or str(src.get("squadType") or "").upper() == "DREAM_SQUAD"
            )
            inferred_from_native_flow = template_is_empty and len(STATE.list_squads()) >= 2
            if explicit_concept or named_as_concept or inferred_from_native_flow:
                src["squadType"] = "DREAM_SQUAD"
                src["dreamSquad"] = True
                log.info(
                    "CONCEPT CREATE inferred requestedTemplate=%d emptyTemplate=%s nameHint=%s nativeFlow=%s",
                    requested_id, template_is_empty, named_as_concept, inferred_from_native_flow,
                )
        # POST to the collection is always a create. FIFA includes the active
        # squad id in its template, so using has_id alone turns the create into
        # a destructive update. PUT retains the legacy upsert behaviour.
        saved = STATE.save_squad(src, create=(method == "POST" or not has_id))
        sid = _int_or(saved.get("id"), 1)
        log.info("SQUAD CREATE id=%d name=%s type=%s", sid, saved.get("squadName"), saved.get("squadType"))
        # SquadCreateDelegate stores this response directly as a squad. An
        # id-only acknowledgement leaves formation/players undefined and
        # crashes when FIFA opens the newly created squad.
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _native_squad(squad_id=sid, source=saved)
        )

    if low == "/ut/game/fifa20/squad/active":
        FLOW.mark("SQUAD", "FUT", f"{method} {path}", _ut_sid())
        if method in ("PUT", "POST") and isinstance(payload, dict):
            sid = _int_or(payload.get("id") or payload.get("squadId"), 0)
            if sid > 0:
                STATE.set_active_squad(sid)
        return 200, {}, json_bytes(_native_squad())

    squad_clone = re.fullmatch(r"/ut/game/fifa20/squad/(\d+)/clone", low)
    if squad_clone and method in ("PUT", "POST"):
        source_id = _int_or(squad_clone.group(1), 0)
        source = STATE.get_squad(source_id)
        if not source:
            return 404, {}, json_bytes({"error": "SQUAD_NOT_FOUND", "id": source_id})
        clone = dict(source)
        clone.pop("id", None)
        clone.pop("squadId", None)
        if isinstance(payload, dict):
            clone.update(payload)
            if "squadName" in payload and "name" not in payload:
                clone["name"] = payload["squadName"]
            elif "name" in payload and "squadName" not in payload:
                clone["squadName"] = payload["name"]
        clone["active"] = bool(clone.get("active", False))
        saved = STATE.save_squad(clone, create=True)
        sid = _int_or(saved.get("id"), 1)
        log.info("SQUAD CLONE source=%d id=%d type=%s", source_id, sid, saved.get("squadType"))
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _native_squad(squad_id=sid, source=saved)
        )

    if low == "/ut/game/fifa20/squad/clone" and method in ("PUT", "POST"):
        # FIFA's native copy/convert workflow posts srcId and dstSquadName to
        # this collection endpoint (without embedding the id in the URL).
        source_id = _int_or(
            payload.get("srcId") if isinstance(payload, dict) else 0, 0
        )
        source = STATE.get_squad(source_id)
        if not source:
            return 404, {}, json_bytes({"error": "SQUAD_NOT_FOUND", "id": source_id})
        clone = dict(source)
        clone.pop("id", None)
        clone.pop("squadId", None)
        if isinstance(payload, dict):
            clone["squadName"] = str(
                payload.get("dstSquadName") or payload.get("squadName")
                or payload.get("name") or source.get("squadName") or "Squad Copy"
            )
            clone["name"] = clone["squadName"]
            if "squadType" in payload:
                clone["squadType"] = payload["squadType"]
            if "dreamSquad" in payload:
                clone["dreamSquad"] = payload["dreamSquad"]
        clone["active"] = False
        saved = STATE.save_squad(clone, create=True)
        sid = _int_or(saved.get("id"), 1)
        log.info("SQUAD CLONE source=%d id=%d type=%s", source_id, sid, saved.get("squadType"))
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _native_squad(squad_id=sid, source=saved)
        )

    squad_by_id = re.fullmatch(r"/ut/game/fifa20/squad/(\d+)", low)
    if squad_by_id and method == "GET":
        sid = _int_or(squad_by_id.group(1), 1)
        saved = STATE.get_squad(sid)
        if saved:
            # FIFA 20 selects a squad by loading this resource; it does not
            # issue a second /squad/active request.  Synchronize server state
            # here so the following Hub response cannot expose a different,
            # possibly empty squad as active.
            STATE.set_active_squad(sid)
            saved = STATE.get_squad(sid) or saved
            log.info("SQUAD SELECT activeId=%d", sid)
            return 200, {"Cache-Control": "no-store"}, json_bytes(
                _native_squad(squad_id=sid, source=saved)
            )
        return 404, {}, json_bytes({"error": "SQUAD_NOT_FOUND", "id": sid})

    if squad_by_id and method in ("PUT", "POST"):
        sid = _int_or(squad_by_id.group(1), 1)
        src = dict(payload) if isinstance(payload, dict) else {}
        src["id"] = sid
        saved = STATE.save_squad(src)
        # SquadEditDelegate keeps using the save response while it updates
        # cards and while the Squad screen is being torn down.  An id-only
        # acknowledgement leaves the delegate's squad pointer incomplete;
        # CardsDLL then dereferences a null member during the Hub transition.
        # Return the same complete native object used by create/get/list so a
        # successful edit is always a directly loadable squad.
        return 200, {"Cache-Control": "no-store"}, json_bytes(
            _native_squad(squad_id=sid, source=saved)
        )

    if squad_by_id and method == "DELETE":
        sid = _int_or(squad_by_id.group(1), 0)
        deleted = STATE.delete_squad(sid)
        return (200 if deleted else 404), {"Cache-Control": "no-store"}, json_bytes({
            "id": sid, "success": deleted,
        })

    delete_squad = re.fullmatch(
        r"/ut/delete/(?:game/)?fifa20/squad/(\d+)", low
    )
    if delete_squad and method in ("DELETE", "POST", "PUT"):
        sid = _int_or(delete_squad.group(1), 0)
        deleted = STATE.delete_squad(sid)
        return (200 if deleted else 404), {"Cache-Control": "no-store"}, json_bytes({
            "id": sid, "success": deleted,
        })

    if low == "/ut/game/fifa20/eventfeed":
        return 200, {}, json_bytes({"event": [], "timestamp": now_s()})

    # FIFA 20's native Seasonal Campaign / Objectives service. The retail DLL
    # parses categoriesList -> groupsList -> objectives and levels -> rewardOptionList.
    objective_claim = re.fullmatch(
        r"/ut/game/fifa20/scmp/(?:campaign|learning)/group/(\d+)/objective/(\d+)/rewards", low
    )
    if objective_claim and method in ("GET", "POST", "PUT"):
        group_id, objective_id = map(int, objective_claim.groups())
        definition = SCMP.objective_definition(objective_id)
        if definition is None or definition[0]["groupId"] != group_id:
            return 404, {"Cache-Control": "no-store"}, json_bytes({
                "success": False, "reason": "OBJECTIVE_NOT_FOUND",
            })
        status, response = _scmp_claim("objectives", objective_id)
        if status == 200:
            response["objectiveId"] = objective_id
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    group_claim = re.fullmatch(
        r"/ut/game/fifa20/scmp/(?:campaign|learning)/group/(\d+)/rewards", low
    )
    if group_claim and method in ("GET", "POST", "PUT"):
        status, response = _scmp_claim("groups", int(group_claim.group(1)))
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    level_claim = re.fullmatch(
        r"/ut/game/fifa20/scmp/campaign/level/(\d+)/reward/(\d+)", low
    )
    if level_claim and method in ("GET", "POST", "PUT"):
        level_id, option_id = map(int, level_claim.groups())
        status, response = _scmp_claim("levels", level_id, option_id)
        if status == 200:
            response["successfulLevelRewards"] = [level_id]
            response["awardedPrizes"] = [
                {
                    "awardType": award["awardType"],
                    "awardCount": award["count"],
                    "awardValue": award["value"],
                    "halId": award["halId"],
                    "untradeable": award["isUntradeable"],
                    "loan": 0,
                }
                for award in response["awards"]
            ]
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    group_details = re.fullmatch(
        r"/ut/game/fifa20/scmp/(?:campaign/active|learning)/category/(\d+)/details", low
    )
    if group_details and method == "GET":
        category_id = int(group_details.group(1))
        requested_ids = {
            _int_or(value, 0) for value in query.get("groupIdList", []) + query.get("groupidlist", [])
            for value in re.split(r"[,;]", value)
        }
        campaign = _scmp_campaign()
        revision = max(0, _int_or(campaign.get("revision"), 0))
        category = next(
            (entry for entry in campaign["categoriesList"] if entry["categoryId"] == category_id),
            None,
        )
        progress, target, objective_state = _scmp_objective_status(campaign, 2301)
        full_envelope = _scmp_requires_full_campaign_envelope(revision)
        _log_scmp_snapshot(
            f"learning/category/{category_id}/details request groups={sorted(requested_ids)}",
            campaign,
        )
        log.info(
            "[SCMP-LIVE] details category=%d revision=%d fullCampaign=%s "
            "draftDebut=%d/%d state=%s",
            category_id, revision, full_envelope, progress, target, objective_state,
        )
        headers = _scmp_response_headers(revision)
        if category is None:
            missing_payload = {"groupsList": [], "groups": []}
            if full_envelope:
                missing_payload.update(campaign)
                missing_payload.update({
                    "campaign": campaign,
                    "activeCampaign": campaign,
                })
            missing_payload.update(_scmp_live_fields(campaign))
            return 404, headers, json_bytes(missing_payload)
        groups = [entry for entry in category["groupsList"]
                  if not requested_ids or entry["groupId"] in requested_ids]

        # Retail initializes the visible Season Objectives model from the full
        # active-campaign response, then asks /learning/category/.../details for
        # the selected groups.  After a match FIFA 20 performs only the latter
        # request.  On a changed objective revision, make that first details
        # response a schema-compatible superset of BOTH resources: the normal
        # category fields remain at the root, while the complete current
        # campaign hierarchy is also present at the root and in explicit
        # campaign aliases.  This gives the native model a full replacement
        # snapshot without forcing logout/re-authentication.
        response: dict[str, Any] = {
            "categoryId": category_id,
            "name": category.get("name", ""),
            "type": _int_or(category.get("type"), 0),
            "priority": _int_or(category.get("priority"), 0),
            "groupsList": groups,
            "groups": groups,
        }
        if full_envelope:
            response.update(campaign)
            # Restore the selected category's child list after the campaign
            # root merge, and provide explicit aliases used by other native FUT
            # responses in this server.
            response.update({
                "categoryId": category_id,
                "groupsList": groups,
                "groups": groups,
                "campaign": campaign,
                "activeCampaign": campaign,
            })
        response.update(_scmp_live_fields(campaign))
        _log_scmp_snapshot(f"learning/category/{category_id}/details response", campaign)
        return 200, headers, json_bytes(response)

    if low in (
        "/ut/game/fifa20/scmp/campaign/active",
        "/ut/game/fifa20/scmp/campaign",
    ) and method == "GET":
        campaign = _scmp_campaign()
        revision = max(0, _int_or(campaign.get("revision"), 0))
        progress, target, objective_state = _scmp_objective_status(campaign, 2301)
        _note_scmp_active_fetch(revision)
        _log_scmp_snapshot("campaign/active response", campaign)
        log.info(
            "[SCMP-LIVE] active campaign fetched revision=%d draftDebut=%d/%d state=%s",
            revision, progress, target, objective_state,
        )
        return 200, _scmp_response_headers(revision), json_bytes(campaign)

    if low in (
        "/ut/game/fifa20/scmp/campaign/rewards/all",
        "/ut/game/fifa20/scmp/learning/campaign/rewards/all",
        "/ut/game/fifa20/scmp/world/campaign/rewards/all",
    ) and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "rewards": [], "rewardsList": [],
        })

    if low == "/ut/delete/auth" and method in ("POST", "DELETE"):
        # FIFA sends this when leaving Ultimate Team.  It is not part of the
        # live-refresh mechanism, but treating it as a real session teardown
        # removes the previous UNKNOWN fallback and keeps SCMP diagnostics
        # unambiguous on the next authentication.
        log.info("[FUT] Local FUT session delete/auth acknowledged sid=%s", _ut_sid())
        return 200, {"Cache-Control": "no-store"}, json_bytes({"success": True})

    if low == "/ut/game/fifa20/hub":
        FLOW.mark("FUT_HOME", "FUT", f"{method} {path}", _ut_sid())
        sid = _ut_sid()
        log.info("[FIX21] HUB reached — marking FUT READY sid=%s", sid)
        sb_state = _sb_state()
        sb_hub = _sb_hub_payload(sb_state)
        _market_finalize()
        _market_seed()
        trade_rows = STATE.list_auctions("user")
        active_market_rows = [
            row for row in STATE.list_auctions()
            if str(row.get("status")) == "active" and _int_or(row.get("expires_at"), 0) > now_s()
        ]
        auction_item_ids = {
            _int_or(row.get("item_id"), 0) for row in trade_rows
            if _int_or(row.get("item_id"), 0) > 0
        }
        pending_trade_count = sum(
            1 for item in STATE.list_items("trade")
            if _int_or(item.get("id"), 0) not in auction_item_ids
        )
        watch_rows = [
            row for row in STATE.list_auctions("cpu")
            if _int_or(row.get("watched"), 0) or str(row.get("highest_bidder")) == "user"
        ]
        selling_count = sum(1 for row in trade_rows if str(row.get("status")) == "active")
        sold_count = sum(1 for row in trade_rows if str(row.get("status")) == "sold")
        winning_count = sum(1 for row in watch_rows if str(row.get("highest_bidder")) == "user")
        campaign = _scmp_campaign()
        return 200, {
            "X-UT-SID": sid,
            "X-UT-PHISHING-TOKEN": "fut20-local-phish-" + sid[-12:],
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }, json_bytes({
            "auctionCount": len(active_market_rows),
            "liveAuctions": len(active_market_rows),
            "liveTransfers": len(active_market_rows),
            "clubPlayers": len(STATE.list_items("club")) + len(STATE.list_items("squad")),
            "tradePile": {
                "count": len(trade_rows) + pending_trade_count,
                "selling": selling_count,
                "sold": sold_count,
            },
            "watchlist": {"count": len(watch_rows), "outbid": 0, "winning": winning_count},
            "squad": _native_hub_squad(),
            "sid": sid,
            "sessionId": sid,
            "credits": STATE.credits(),
            "personaId": int(CFG["persona_id"]),
            "nucleusId": int(CFG["nucleus_id"]),
            "clubId": int(CFG["club_id"]),
            "clubName": str(CFG["club_name"]),
            "onlineAccess": True,
            "returningUser": True,
            "squadBattlesEnabled": True,
            "squadBattleEnabled": True,
            "usLeaderboardAvailable": False,
            "squadBattlePoints": sb_hub["score"],
            "squadBattle": sb_hub,
            **_credits_payload(),
            "user": _native_user(),
            # The Hub is the first stable FUT model FIFA requests after a match.
            # Expose SCMP at the same top-level aliases used by the native user
            # refresh so a changed revision is visible without leaving FUT.
            **_scmp_live_fields(campaign),
        })

    if low == "/ut/game/fifa20/clubuser":
        return 200, {}, json_bytes({"user": [{
            "persona": str(CFG["display_name"]),
            "personaId": int(CFG["persona_id"]),
            "public": True,
        }]})

    if low == "/ut/game/fifa20/leaderboards/options":
        # This unsupported native screen is disabled in the retail settings
        # and FUT Hub. Returning a fabricated 200 options model in 1.0.15
        # froze the Club tab immediately after this request; restore the
        # previous disabled fallback. Mode-specific boards remain separate.
        log.info("General leaderboards requested; rejecting unsupported native screen")
        return 404, {"Cache-Control": "no-store"}, json_bytes({
            "reason": "FEATURE_DISABLED", "code": "LEADERBOARDS_UNAVAILABLE",
        })

    if low == "/ut/game/fifa20/club/stats/consumables":
        response = _consumable_stats()
        log.info(
            "Consumable sticker-book stats requested; category=%s rows=%s",
            response["category"], len(response["stat"]),
        )
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    # Both /stats/year and /stats/consumables use the same native
    # FutStickerBookStats2ServerResponse. Its category member is required.
    if low == "/ut/game/fifa20/club/stats/year":
        response = _club_stats("year")
        log.info(
            "My Club year stats requested; category=%s rows=%s",
            response["category"], len(response["stat"]),
        )
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    # FIFA requests /club/stats/staff immediately before /club/stats/year.
    # The generic ~4k club-stats payload is the wrong wire shape; the client
    # expects a compact "bonus" array for staff bonuses. An empty list is
    # safe when the club has no staff cards (the seeded 11-player club).
    if low == "/ut/game/fifa20/club/stats/staff":
        log.info("My Club staff stats requested; returning empty bonus array")
        return 200, {"Cache-Control": "no-store"}, json_bytes({"bonus": []})

    if low.startswith("/ut/game/fifa20/club/consumables"):
        response = _club_consumable_search(query)
        log.info(
            "Consumable club search requested path=%s query=%s; returned=%s total=%s",
            low, dict(query), response["count"], response["total"],
        )
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low.startswith("/ut/game/fifa20/club/stats/"):
        category = low.split("/club/stats/", 1)[1].split("/", 1)[0]
        return 200, {"Cache-Control": "no-store"}, json_bytes(_club_stats(category))

    if low == "/ut/game/fifa20/club":
        FLOW.mark("CLUB", "FUT", f"{method} {path}", _ut_sid())
        requested_type = str(_market_first(query, "type") or "player").lower()
        exact_owned_lookup = str(_market_first(query, "filter") or "").lower() == "exact"
        active_saved_squad = next(
            (squad for squad in STATE.list_squads() if bool(squad.get("active", False))),
            None,
        )
        if (
            method == "GET"
            and requested_type == "player"
            and not exact_owned_lookup
            and isinstance(active_saved_squad, dict)
            and str(active_saved_squad.get("squadType", "")).upper() == "DREAM_SQUAD"
        ):
            # FIFA 20 keeps calling /club after an empty concept squad is
            # created. Retail FUT transparently switches that picker to the
            # concept catalog. Do the same server-side, while leaving exact
            # /club ownership probes untouched.
            log.info(
                "CONCEPT SEARCH automatic squadId=%s originalPath=%s",
                active_saved_squad.get("id", active_saved_squad.get("squadId", 0)),
                raw_path,
            )
            return 200, {"Cache-Control": "no-store"}, json_bytes(
                _dream_player_search(query)
            )
        return 200, {"Cache-Control": "no-store"}, json_bytes(_club_player_search(query))

    # Legacy compatibility endpoint used by some FIFA 20 client builds /
    # tooling. Returns the list of owned item IDs (players + staff + club
    # items) so the client can reconcile its local cache.
    if low in ("/ut/game/fifa20/clubitemids", "/ut/game/fifa20/clubItemIds"):
        all_items = STATE.list_items("club") + STATE.list_items("squad")
        ids = [int(item.get("id", 0)) for item in all_items if isinstance(item, dict) and int(item.get("id", 0)) > 0]
        log.info("clubItemIds requested; returning %d owned item ids", len(ids))
        return 200, {"Cache-Control": "no-store"}, json_bytes({"itemIds": ids})

    if low == "/ut/game/fifa20/user/credits":
        return 200, {}, json_bytes(_credits_payload())

    if low in (
        "/ut/game/fifa20/store",
        "/ut/game/fifa20/store/purchasegroup",
        "/ut/game/fifa20/store/purchasegroup/all",
        "/ut/game/fifa20/store/purchasegroup/cardpack",
        "/ut/game/fifa20/store/purchasegroup/mypacks",
    ):
        FLOW.mark("STORE", "FUT", f"{method} {path}", _ut_sid())
        store_payload = _store_purchasegroups()
        log.warning("STORE-SCOUT PURCHASEGROUP RESPONSE %s", json.dumps(store_payload, ensure_ascii=False))
        return 200, {"Cache-Control": "no-store"}, json_bytes(store_payload)

    if low in (
        "/ut/game/fifa20/store/packquantities",
        "/ut/game/fifa20/store/packquantity",
    ):
        counts = STATE.unopened_pack_counts()
        rows = [
            {"packId": pack_id, "quantity": quantity, "unopenedQuantity": quantity}
            for pack_id, quantity in sorted(counts.items())
        ]
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "packQuantities": rows, "packs": rows, "count": sum(counts.values()),
        })

    if low == "/ut/game/fifa20/purchased/items":
        FLOW.mark("PACKS", "FUT", f"{method} {path}", _ut_sid())
        if method == "POST":
            pack_id = int(payload.get("packId", 3)) if isinstance(payload, dict) else 3
            pack_cfg = get_pack_cfg(pack_id)
            if str(pack_cfg.get("content_type", "")).lower() == "player_pick":
                status, response = _purchase_store_player_pick(pack_id, pack_cfg)
                return status, {"Cache-Control": "no-store"}, json_bytes(response)
            is_reward_pack = bool(pack_cfg.get("reward_only", False))
            earned_pack = STATE.consume_unopened_pack(pack_id)
            if is_reward_pack and not earned_pack:
                log.warning("PACK OPEN BLOCKED packId=%s reason=no-earned-token", pack_id)
                return 200, {}, json_bytes({
                    "success": False,
                    "reason": "PACK_NOT_OWNED",
                    "duplicateItemIdList": [],
                    "itemList": [],
                    "itemData": [],
                    "items": [],
                    **_credits_payload(),
                })

            price = 0 if earned_pack else max(0, int(pack_cfg.get("price", 0)))
            if not earned_pack and isinstance(payload, dict) and int(payload.get("useCredits", 1)):
                if STATE.credits() < price:
                    return 200, {}, json_bytes({"duplicateItemIdList": [], "itemData": [], **_credits_payload()})
                STATE.add_credits(-price)
            log.warning("CARDS-SCOUT PACK_OPEN_BEGIN packId=%s ownedClub=%d ownedSquad=%d",
                        pack_id, len(STATE.list_items("club")), len(STATE.list_items("squad")))
            try:
                pack = STATE.create_pack(pack_id)
            except Exception:
                # Never lose an SBC prize if pack generation encounters an error.
                if earned_pack:
                    STATE.grant_unopened_pack(pack_id)
                raise
            # Warm exact portraits concurrently, but never hold the purchase
            # response open for remote image work. The game can render the pack
            # immediately and receives each identity-keyed texture as it asks.
            _prime_pack_player_heads(pack.get("itemData", []))
            log.warning("CARDS-SCOUT PACK_OPEN_ITEMS ids=%s resources=%s duplicates=%s",
                        [x.get("id") for x in pack.get("itemData", [])],
                        [x.get("resourceId") for x in pack.get("itemData", [])],
                        pack.get("duplicateItemIdList", []))
            # FIFA's native pack-purchase response uses itemList as the ordered
            # reveal list.  Do not also send itemData/items here: those extra
            # aliases caused the PC client to rebuild the collection and fall
            # back to its resourceId ordering instead of our reveal order.
            ordered_pack_items = [_native_pack_item(x) for x in pack.get("itemData", [])]
            log.warning("PACK DISPLAY ORDER ratings=%s rareflags=%s ids=%s timestamps=%s resources=%s",
                        [x.get("rating") for x in ordered_pack_items],
                        [x.get("rareflag") for x in ordered_pack_items],
                        [x.get("id") for x in ordered_pack_items],
                        [x.get("timestamp") for x in ordered_pack_items],
                        [x.get("resourceId") for x in ordered_pack_items])
            return 200, {}, json_bytes({
                "numberItems": int(pack.get("numberItems", len(ordered_pack_items))),
                "purchasedPackId": int(pack.get("purchasedPackId", pack_id)),
                "duplicateItemIdList": pack["duplicateItemIdList"],
                # Feed the same desired order through every FIFA 20 response alias.
                "itemList": ordered_pack_items,
                "itemData": ordered_pack_items,
                "items": ordered_pack_items,
                **_credits_payload(),
            })
        # When there are unassigned pack cards, preserve their duplicate markers on
        # refreshes of purchased/items. With no unassigned cards this stays exactly
        # the normal empty bootstrap shape used while entering FUT.
        unassigned = STATE.list_items("unassigned")

        # FIFA refreshes /purchased/items after the pack-open POST and uses this
        # refreshed itemData order for the reveal/New Items screen. Keep this
        # order identical to create_pack(): highest rating first.
        def _unassigned_reveal_sort_key(item):
            try:
                rating = int(item.get("rating", 0) or 0)
            except Exception:
                rating = 0
            try:
                rareflag = int(item.get("rareflag", 0) or 0)
            except Exception:
                rareflag = 0
            try:
                rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
            except Exception:
                rid = 0
            # Same ordering as create_pack(): specials first, then OVR descending.
            is_special = bool(item.get("special", False)) or rareflag > 1
            return (0 if is_special else 1, -rating, -rareflag, -rid)

        unassigned = sorted(unassigned, key=_unassigned_reveal_sort_key)

        duplicate_item_list = []
        for item in unassigned:
            try:
                iid = int(item.get("id", 0) or 0)
                rid = int(item.get("resourceId", item.get("assetId", 0)) or 0)
            except Exception:
                continue
            owned_item = STATE.owned_player_item_by_resource_id(rid)
            owned_iid = int(owned_item.get("id", 0) or 0) if isinstance(owned_item, dict) else 0
            if iid and owned_iid:
                duplicate_item_list.append({"itemId": iid, "duplicateItemId": owned_iid})
        native_unassigned = [_native_pack_item(x) for x in unassigned]
        log.warning("UNASSIGNED DISPLAY ORDER ratings=%s rareflags=%s resources=%s",
                    [x.get("rating") for x in native_unassigned],
                    [x.get("rareflag") for x in native_unassigned],
                    [x.get("resourceId") for x in native_unassigned])
        return 200, {}, json_bytes({
            "numberItems": len(native_unassigned),
            "duplicateItemIdList": duplicate_item_list,
            "itemData": native_unassigned,
        })

    if low == "/ut/game/fifa20/tradepile" and method == "GET":
        response = _market_tradepile()
        inactive = sum(
            1 for row in response.get("auctionInfo", [])
            if isinstance(row, dict) and row.get("tradeState") == "inactive"
        )
        log.info("TRANSFER MARKET trade pile count=%s inactive=%s selling=%s sold=%s",
                 response.get("count"), inactive, response.get("selling"), response.get("sold"))
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/marketdata/pricelimits" and method == "GET":
        def_id = _int_or(_market_first(query, "defId", "maskedDefId", "assetId"), 0)
        response = _market_price_limits(def_id)
        log.info("TRANSFER MARKET price limits defId=%s response=%s", def_id, response)
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/marketdata/item/pricelimits" and method == "GET":
        raw_ids: list[str] = []
        for key, values in query.items():
            if str(key).lower() in ("itemidlist", "itemid"):
                for value in values:
                    raw_ids.extend(str(value).split(","))
        item_ids = [_int_or(value, 0) for value in raw_ids if _int_or(value, 0) > 0]
        response = _market_item_price_limits(item_ids)
        log.info("TRANSFER MARKET item price limits itemIds=%s response=%s", item_ids, response)
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low in ("/ut/game/fifa20/trade/status/lite", "/ut/game/fifa20/trade/status") and method == "GET":
        raw_ids: list[str] = []
        for key, values in query.items():
            if str(key).lower() in ("tradeids", "tradeidlist", "tradeid"):
                for value in values:
                    raw_ids.extend(str(value).split(","))
        trade_ids = [_int_or(value, 0) for value in raw_ids if _int_or(value, 0) > 0]
        include_bank = _market_first(query, "includeBankInfo").lower() in ("1", "true", "yes")
        response = _market_trade_status(trade_ids, include_bank)
        log.info("TRANSFER MARKET status tradeIds=%s found=%s includeBank=%s",
                 trade_ids, len(response.get("auctionInfo", [])), include_bank)
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/transfermarket" and method == "GET":
        response = _market_search(query)
        log.info("TRANSFER MARKET search query=%s results=%s total=%s",
                 dict(query), response.get("count"), response.get("total"))
        return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/auctionhouse" and method == "POST":
        status, response = _market_create_listing(payload if isinstance(payload, dict) else {})
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    relist_match = re.fullmatch(r"/ut/game/fifa20/auctionhouse(?:/(\d+))?", low)
    if relist_match and method == "PUT":
        trade_id = _int_or(relist_match.group(1), 0) or _int_or(
            payload.get("tradeId") if isinstance(payload, dict) else 0, 0
        )
        auction = STATE.get_auction(trade_id)
        if not auction or str(auction.get("seller_type")) != "user":
            return 404, {}, json_bytes({"reason": "AUCTION_NOT_FOUND", "credits": STATE.credits()})
        if str(auction.get("status")) != "expired":
            return 409, {}, json_bytes({"reason": "AUCTION_NOT_RELISTABLE", "credits": STATE.credits()})
        duration = _int_or(
            payload.get("duration") if isinstance(payload, dict) else auction.get("duration"),
            _int_or(auction.get("duration"), 3600),
        )
        item_data = dict(auction.get("item_data", {}))
        recommended = _market_recommended_price(
            _int_or(auction.get("resource_id"), 0), _market_item_kind(item_data)
        )
        bot_budget = _market_round_price(int(recommended * (0.96 + (trade_id % 10) / 100.0)))
        action_at = now_s() + 12 + (trade_id % 29)
        for key in ("_cpuBuyAt", "_cpuActionAt", "_cpuMaxBid", "_cpuBidCount"):
            item_data.pop(key, None)
        if _int_or(auction.get("buy_now_price"), 0) <= bot_budget:
            item_data["_cpuBuyAt"] = action_at
        elif _int_or(auction.get("starting_bid"), 0) <= bot_budget:
            item_data["_cpuActionAt"] = action_at
            item_data["_cpuMaxBid"] = bot_budget
        auction = STATE.update_auction(
            trade_id, status="active", current_bid=0, highest_bidder="",
            expires_at=now_s() + max(60, duration), item_data=item_data,
        ) or auction
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "auctionInfo": [_market_auction_info(auction)], "credits": STATE.credits(),
        })

    bid_match = re.fullmatch(r"/ut/game/fifa20/(?:trade|auctionhouse)/(\d+)/(?:bid|offer)", low)
    if bid_match and method in ("PUT", "POST"):
        status, response = _market_place_bid(
            int(bid_match.group(1)), payload if isinstance(payload, dict) else {},
        )
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    clear_trade = re.fullmatch(r"/ut/game/fifa20/(?:trade|tradepile)/(\d+)", low)
    if clear_trade and method == "DELETE":
        status, response = _market_clear_trade(int(clear_trade.group(1)))
        return status, {"Cache-Control": "no-store"}, json_bytes(response)

    # Retail FIFA 20's LT "Clear Sold Items" action is a GET to the delete
    # namespace, not a DELETE/POST to trade/sold. The old unknown-route 200
    # made the client hide cards locally while leaving every auction persisted.
    clear_sold_route = low in (
        "/ut/delete/game/fifa20/trade/sold",
        "/ut/game/fifa20/trade/sold",
    ) and method in ("GET", "DELETE", "POST", "PUT")
    clear_tradepile_route = low == "/ut/game/fifa20/tradepile" and method in (
        "DELETE", "POST", "PUT"
    )
    if clear_sold_route or clear_tradepile_route:
        requested_ids: list[int] = []

        def collect_trade_ids(value: Any) -> None:
            if isinstance(value, list):
                for entry in value:
                    collect_trade_ids(entry)
                return
            if isinstance(value, dict):
                direct = _int_or(
                    value.get("tradeId") or value.get("trade_id") or value.get("id"), 0
                )
                if direct > 0 and direct not in requested_ids:
                    requested_ids.append(direct)
                for key, nested in value.items():
                    if key not in {"tradeId", "trade_id", "id"}:
                        collect_trade_ids(nested)
                return
            direct = _int_or(value, 0)
            if direct > 0 and direct not in requested_ids:
                requested_ids.append(direct)

        if isinstance(payload, dict):
            for key in ("tradeIdList", "tradeId", "auctionInfo", "items", "itemData"):
                if key in payload:
                    collect_trade_ids(payload.get(key))
        if isinstance(payload, list):
            collect_trade_ids(payload)
        if not requested_ids:
            requested_ids = [
                _int_or(row.get("trade_id"), 0) for row in STATE.list_auctions("user")
                if str(row.get("status")) in ("sold", "expired")
            ]
        for trade_id in requested_ids:
            _market_clear_trade(trade_id)
        return 200, {"Cache-Control": "no-store"}, json_bytes(_market_tradepile())

    watch_match = re.fullmatch(r"/ut/game/fifa20/watchlist(?:/(\d+))?", low)
    if watch_match and method == "GET":
        return 200, {"Cache-Control": "no-store"}, json_bytes(_market_watchlist())
    if watch_match and method in ("PUT", "POST", "DELETE"):
        trade_id = _int_or(watch_match.group(1), 0) or _int_or(
            payload.get("tradeId") if isinstance(payload, dict) else 0, 0
        ) or _int_or(_market_first(query, "tradeId"), 0)
        auction = STATE.get_auction(trade_id)
        if auction and str(auction.get("seller_type")) == "cpu":
            STATE.update_auction(trade_id, watched=0 if method == "DELETE" else 1)
        return 200, {"Cache-Control": "no-store"}, json_bytes(_market_watchlist())

    consumable_apply = re.fullmatch(r"/ut/game/fifa20/item/resource/(\d+)/?", low)
    if consumable_apply and method in ("POST", "PUT"):
        resource_id = int(consumable_apply.group(1))
        target_ids: list[int] = []

        def collect_apply_targets(value: Any) -> None:
            if isinstance(value, list):
                for entry in value:
                    collect_apply_targets(entry)
                return
            if isinstance(value, dict):
                iid = _int_or(value.get("id") or value.get("itemId") or value.get("itemid"), 0)
                if iid > 0 and iid not in target_ids:
                    target_ids.append(iid)
                    return
                for nested in value.values():
                    collect_apply_targets(nested)
                return
            iid = _int_or(value, 0)
            if iid > 0 and iid not in target_ids:
                target_ids.append(iid)

        if isinstance(payload, dict):
            for key in ("apply", "itemData", "items", "targets"):
                if key in payload:
                    collect_apply_targets(payload.get(key))
            if not target_ids:
                collect_apply_targets(payload)
        elif isinstance(payload, list):
            collect_apply_targets(payload)

        updated = _apply_consumable_resource(resource_id, target_ids)
        log.info(
            "Applied FUT consumable resource=%s targets=%s updated=%s",
            resource_id, target_ids, [x.get("id") for x in updated if isinstance(x, dict)],
        )
        # The retail client immediately decodes this response and refreshes
        # the selected card.  A successful zero-byte response applies the DB
        # mutation but then makes FIFA abandon FUT because there is no item
        # document to decode.
        native_updated = [
            _native_player_item(item)
            if str(item.get("itemType", "player") or "player").lower() == "player"
            else _native_manager_item(item)
            if str(item.get("itemType", "") or "").lower() == "manager"
            else _native_pack_item(item)
            for item in updated if isinstance(item, dict)
        ]
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "itemData": native_updated,
            "items": native_updated,
            "consumableConsumed": bool(updated),
        })

    # The pack-results screen asks the server to resolve the newly created
    # item-instance IDs after the animation finishes.  The generic fallback
    # response ({}) leaves FIFA polling this route forever on an empty room.
    if low == "/ut/game/fifa20/item" and method == "GET":
        requested_ids = []
        for raw_value in query.get("idList", []):
            for raw_id in str(raw_value).split(","):
                try:
                    item_id = int(raw_id.strip())
                except (TypeError, ValueError):
                    continue
                if item_id > 0 and item_id not in requested_ids:
                    requested_ids.append(item_id)
                if len(requested_ids) >= 200:
                    break
            if len(requested_ids) >= 200:
                break

        items = []
        for item_id in requested_ids:
            item = STATE.get_item(item_id)
            if isinstance(item, dict):
                items.append(_native_pack_item(item))
        log.info("Resolved FUT item lookup requested=%s found=%s",
                 requested_ids, [item.get("id") for item in items])
        return 200, {"Cache-Control": "no-store"}, json_bytes({"itemData": items})

    # Some FIFA 20 result flows use an instance URL for the same lookup.
    single_item_lookup = re.fullmatch(r"/ut/game/fifa20/item/(\d+)", low)
    if single_item_lookup and method == "GET":
        item_id = int(single_item_lookup.group(1))
        item = STATE.get_item(item_id)
        if not isinstance(item, dict):
            return 404, {}, json_bytes({"reason": "item not found"})
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "itemData": [_native_pack_item(item)],
        })

    # Customize uses a dedicated instance PUT and expects an empty response.
    # The client sends itemState="active" plus an activation slot on some
    # builds, and an explicit activeHomeKit/... state on others.
    if single_item_lookup and method == "PUT":
        item_id = int(single_item_lookup.group(1))
        stored_item = STATE.get_item(item_id)
        if not isinstance(stored_item, dict):
            return 404, {}, json_bytes({"reason": "item not found"})
        stored_type = str(stored_item.get("itemType", "")).lower()
        if stored_type not in {"kit", "custom", "ball", "stadium"}:
            pile = str(payload.get("pile", stored_item.get("pile", "club"))) if isinstance(payload, dict) else "club"
            updated = STATE.update_item_fields(
                item_id,
                {"pile": pile, "itemState": str(payload.get("itemState", stored_item.get("itemState", "free")))}
                if isinstance(payload, dict) else {"pile": pile},
            )
            return (200 if updated else 404), {"Cache-Control": "no-store"}, b""
        requested_state = str(
            payload.get("itemState", payload.get("state", ""))
            if isinstance(payload, dict) else ""
        )
        activate_slot = _int_or(
            payload.get("activateSlotNumber", payload.get("slot", -1))
            if isinstance(payload, dict) else -1,
            -1,
        )
        activated = STATE.activate_club_item(item_id, requested_state, activate_slot)
        if activated is None:
            return 404, {}, json_bytes({"reason": "club item not found"})
        log.info(
            "Activated club item id=%s resource=%s state=%s slot=%s",
            item_id, activated.get("resourceId"), activated.get("itemState"), activate_slot,
        )
        return 200, {"Cache-Control": "no-store"}, b""

    # ---- Native FIFA discard / quick-sell routes -------------------------
    # Single item:
    #   DELETE /ut/game/fifa20/item/<itemId>
    # Multiple items ("Quick Sell All"):
    #   POST /ut/delete/game/fifa20/item
    #
    # FIFA expects totalCredits + items[] in the response.
    single_discard = re.match(r"^/ut/game/fifa20/item/(\d+)$", low)
    if single_discard and method == "DELETE":
        iid = int(single_discard.group(1))
        total_credits, deleted_ids = STATE.discard_items([iid])
        return 200, {}, json_bytes({
            "totalCredits": total_credits,
            "credits": total_credits,
            "items": [{"id": x} for x in deleted_ids],
            "currencies": [{
                "name": "coins",
                "funds": total_credits,
                "finalFunds": total_credits,
            }],
        })

    if low == "/ut/delete/game/fifa20/item" and method == "POST":
        ids = []
        if isinstance(payload, dict):
            raw_ids = payload.get("itemId", [])
            if not isinstance(raw_ids, list):
                raw_ids = [raw_ids]
            ids.extend(raw_ids)

            # Be tolerant if this particular PC build sends itemData instead.
            raw_data = payload.get("itemData", [])
            if isinstance(raw_data, dict):
                raw_data = [raw_data]
            if isinstance(raw_data, list):
                for x in raw_data:
                    if isinstance(x, dict):
                        ids.append(x.get("id") or x.get("itemId"))

        normalized_ids = list(dict.fromkeys(
            _int_or(value, 0) for value in ids if _int_or(value, 0) > 0
        ))
        cleared_sale_items: list[int] = []
        terminal_by_item = {
            _int_or(row.get("item_id"), 0): row
            for row in STATE.list_auctions("user")
            if _int_or(row.get("item_id"), 0) > 0
            and str(row.get("status", "active")) in ("sold", "expired")
        }
        for item_id in normalized_ids:
            auction = terminal_by_item.get(item_id)
            if not auction:
                continue
            status, _response = _market_clear_trade(_int_or(auction.get("trade_id"), 0))
            if status == 200:
                cleared_sale_items.append(item_id)

        remaining_ids = [item_id for item_id in normalized_ids if item_id not in cleared_sale_items]
        total_credits, deleted_ids = STATE.discard_items(remaining_ids)
        deleted_ids = list(dict.fromkeys([*cleared_sale_items, *deleted_ids]))
        total_credits = STATE.credits()
        if cleared_sale_items:
            log.info(
                "TRANSFER MARKET permanently cleared sold itemIds=%s remainingAuctions=%s",
                cleared_sale_items, len(STATE.list_auctions("user")),
            )
        return 200, {}, json_bytes({
            "totalCredits": total_credits,
            "credits": total_credits,
            "items": [{"id": x} for x in deleted_ids],
            "currencies": [{
                "name": "coins",
                "funds": total_credits,
                "finalFunds": total_credits,
            }],
        })

    if low == "/ut/game/fifa20/item" and method in ("PUT", "POST"):
        results = []
        duplicate_results = []
        data_list = payload.get("itemData", []) if isinstance(payload, dict) else []
        if isinstance(data_list, dict):
            data_list = [data_list]
        batch_mode = len(data_list) > 1

        for change in data_list:
            try:
                iid = int(change.get("id") or change.get("itemId"))
            except Exception:
                continue
            pile = str(change.get("pile", "club"))

            if pile in ("club", "squad") and STATE.would_duplicate_owned_player(iid):
                # Keep the duplicate physically in Unassigned in every case.
                STATE.mark_item_duplicate(iid, True)
                duplicate_results.append({"itemId": iid})

                if batch_mode:
                    # Store All: explicitly acknowledge the item while telling FIFA
                    # that its resulting pile is still Unassigned.  v5 omitted the
                    # duplicate from itemData entirely; the database was correct but
                    # the native client optimistically animated every submitted card
                    # into the club.  Pairing this result with duplicateItemIdList
                    # gives the client both pieces of state without using success=False
                    # (which made FIFA 20 treat the whole batch as fatal in v3).
                    results.append({
                        "id": iid,
                        "pile": "unassigned",
                        "success": True,
                    })
                    log.info("Store All kept duplicate item %d visibly unassigned", iid)
                    continue

                # Native FIFA 20 PC handles duplicate state in NewItemsScreen itself.
                # Returning a failed /item move causes this build to tear down FUT auth,
                # so acknowledge the request while preserving the card in Unassigned.
                results.append({
                    "id": iid,
                    "pile": "unassigned",
                    "success": True,
                    "IS_DUPLICATE": True,
                })
                log.info("Kept duplicate item %d in unassigned using native UI marker", iid)
                continue

            if pile == "trade":
                # A card sent from the active squad to the Transfer List cannot
                # remain referenced by that squad. Keep the item itself unlisted
                # until POST /auctionhouse creates the auction.
                STATE.detach_item_from_squads(iid)
                ok = STATE.update_item_fields(iid, {"pile": "trade", "itemState": "free"}) is not None
                log.info("TRANSFER MARKET moved owned item=%s to inactive trade pile success=%s", iid, ok)
            else:
                ok = STATE.update_item_pile(iid, pile)
            result = {"id": iid, "pile": pile, "success": ok}
            # Development-card Store All is sensitive to a bare id-only
            # acknowledgement. Echo its stack identity so FIFA refreshes the
            # owned consumables collection immediately.
            stored_after = STATE.get_item(iid)
            if (ok and isinstance(stored_after, dict)
                    and str(stored_after.get("itemType", "")).lower() == "development"):
                result.update({
                    "resourceId": _int_or(stored_after.get("resourceId"), 0),
                    "stackCount": max(1, _int_or(stored_after.get("stackCount"), 1)),
                    "itemType": "development",
                })
            results.append(result)

        response = {"itemData": results}
        if duplicate_results:
            response["duplicateItemIdList"] = duplicate_results
        return 200, {}, json_bytes(response)

    suffix = low
    m = re.match(r"^/ut/(?:game/)?[^/]+/(.*)$", low)
    if m:
        suffix = "/" + m.group(1)

    if suffix in ("/usermassinfo", "/user/massinfo"):
        return 200, {}, json_bytes(user_mass_info())
    if suffix.startswith("/defid"):
        return 200, {"Cache-Control": "no-store"}, json_bytes(_dream_player_search(query))

    # ---- FUT Draft ------------------------------------------------------------
    if "draft" in low:
        log.warning("DRAFT-SCOUT REQUEST method=%s path=%s query=%s body=%s",
                    method, raw_path, dict(query), body[:2000])

    if low == "/ut/game/fifa20/squad/mode/draft/state" and method == "GET":
        mode = str(query.get("mode", ["SINGLE_PLAYER"])[0] or "SINGLE_PLAYER").upper()
        state_payload = _draft_state_payload(_draft_run())
        log.warning("DRAFT-SCOUT STATE_RESPONSE mode=%s payload=%s",
                    mode, json.dumps(state_payload, separators=(",", ":")))
        return 200, {"Cache-Control": "no-store"}, json_bytes(state_payload)

    draft_purchase = re.fullmatch(r"/ut/game/fifa20/purchase/mode/(\d+)/draft", low)
    if draft_purchase and method == "POST":
        with _draft_lock:
            run = _draft_run()
            resumed = run is not None
            currency = str(payload.get("currency", "COINS") if isinstance(payload, dict) else "COINS").upper()
            if run is None:
                if currency in ("COINS", "CREDITS"):
                    if STATE.credits() < 15000:
                        return 403, {}, json_bytes({"reason": "insufficientCoins", **_credits_payload()})
                    STATE.add_credits(-15000)
                run = _draft_new_run("SINGLE_PLAYER" if draft_purchase.group(1) != "0" else "ONLINE")
                _draft_save(run)
            response = [{"COINS": STATE.credits(), "POINTS": 0, "DRAFT_TOKEN": 0}]
            log.warning("DRAFT-SCOUT PURCHASE mode=%s currency=%s resumed=%s state=%s balance=%s",
                        draft_purchase.group(1), currency, resumed, run["state"], STATE.credits())
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/squad/mode/draft/choose/difficulty" and method in ("PUT", "POST"):
        with _draft_lock:
            run = _draft_run()
            if run is None:
                return 409, {}, json_bytes({"reason": "DRAFT_NOT_ACTIVE"})
            requested = str(payload.get("difficultyName", "AMATEUR") if isinstance(payload, dict) else "AMATEUR").upper()
            aliases = {"PROFESSIONAL": "PRO", "WORLD_CLASS": "WORLDCLASS"}
            requested = aliases.get(requested, requested)
            run["difficulty"] = _DRAFT_DIFFICULTIES.index(requested) if requested in _DRAFT_DIFFICULTIES else 0
            run["state"] = "FORMATION_DRAFT"
            _draft_save(run)
            log.warning("DRAFT-SCOUT CHOOSE_DIFFICULTY value=%s index=%s", requested, run["difficulty"])
            return 200, {"Cache-Control": "no-store"}, json_bytes({})

    draft_choices = re.search(r"/draft/choices/(difficulty|formation|captain|player|manager)$", low)
    if draft_choices and method in ("GET", "PUT", "POST"):
        with _draft_lock:
            run = _draft_run()
            if run is None:
                return 409, {}, json_bytes({"reason": "DRAFT_NOT_ACTIVE"})
            kind = draft_choices.group(1)
            raw_position = payload.get("positionId", payload.get("positionid", 0)) if isinstance(payload, dict) else 0
            try:
                position_id = int(raw_position or 0)
            except (TypeError, ValueError):
                position_id = 0
            response = _draft_choices(run, kind, position_id)
            log.warning("DRAFT-SCOUT CHOICES kind=%s position=%s count=%s state=%s",
                        kind, position_id, len(response.get("choices", [])), run["state"])
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low.endswith("/draft/choose") and method in ("PUT", "POST"):
        with _draft_lock:
            run = _draft_run()
            if run is None:
                return 409, {}, json_bytes({"reason": "DRAFT_NOT_ACTIVE"})
            try:
                choice_index = int(payload.get("choiceIndex", 0) if isinstance(payload, dict) else 0)
                position_id = int(payload.get("positionId", run.get("currentPositionId", 0))
                                  if isinstance(payload, dict) else run.get("currentPositionId", 0))
            except (TypeError, ValueError):
                choice_index, position_id = 0, int(run.get("currentPositionId", 0) or 0)
            old_state = run["state"]
            layout_applied = _draft_apply_slot_order(
                run,
                payload.get("swapPlayerDefIds") if isinstance(payload, dict) else None,
            )
            _draft_choose(run, choice_index, position_id)
            log.warning("DRAFT-SCOUT CHOOSE oldState=%s choice=%s position=%s newState=%s picks=%s layoutApplied=%s",
                        old_state, choice_index, position_id, run["state"],
                        len(run.get("picked", {})), layout_applied)
            return 200, {"Cache-Control": "no-store"}, json_bytes({})

    if low.endswith("/draft/autocomplete") and method in ("POST", "PUT"):
        with _draft_lock:
            run = _draft_run()
            if run is None:
                return 409, {}, json_bytes({"reason": "DRAFT_NOT_ACTIVE"})
            if not run.get("formation"):
                if len(run.get("formationChoices", [])) != 5:
                    _draft_choices(run, "formation", 0)
                run["formation"] = run["formationChoices"][0]
            selection_rng = random.Random(
                f"fut20-draft-autofill-selection:{int(run.get('seed', 1) or 1)}"
            )
            selected_indices: list[int] = []
            for position_id in range(23):
                if str(position_id) in run.get("picked", {}):
                    continue
                choices = _draft_player_choices(run, position_id, captain=False)
                if choices:
                    selected_index = selection_rng.randrange(len(choices))
                    selected_indices.append(selected_index)
                    run.setdefault("picked", {})[str(position_id)] = choices[selected_index]
            if not run.get("manager"):
                managers = _draft_manager_choices(run)
                if managers:
                    manager_index = selection_rng.randrange(len(managers))
                    selected_indices.append(manager_index)
                    run["manager"] = managers[manager_index]
                else:
                    run["manager"] = None
            run["state"] = "READY_FOR_MATCH"
            _draft_save(run)
            response = _draft_autofill_response(run)
            encoded = json_bytes(response)
            log.warning(
                "DRAFT-SCOUT AUTOCOMPLETE picks=%s manager=%s selectedIndices=%s "
                "response=operation-envelope bytes=%s",
                len(run.get("picked", {})), bool(run.get("manager")),
                selected_indices, len(encoded),
            )
            return 200, {"Cache-Control": "no-store"}, encoded

    if (low.endswith("/draft/mode/1/stats") or
            ("/squad/mode/" in low and low.endswith("/stats"))) and method == "GET":
        run = _draft_run()
        wins = int(run.get("wins", 0) or 0) if run else 0
        eliminated = bool(run and run.get("state") == "READY_FOR_REWARDS")
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "bestBuilderScore": 0, "concededGoals": 0,
            "draftChampion": 1 if wins >= 4 else 0,
            "draftsCompleted": 1 if eliminated or wins >= 4 else 0,
            "gamesLost": 1 if eliminated else 0, "gamesWon": wins,
            "passAccuracyTotal": 0, "possessionPercentage": 0,
            "possesionTotal": 0, "possessionTotal": 0, "scoredGoals": 0,
        })

    if "/grant/award" in low and method in ("GET", "POST", "PUT"):
        with _draft_lock:
            run = _draft_run()
            reward = int(run.get("pendingCoinReward", 0) or 0) if run else 0
            if run and reward <= 0 and run.get("state") == "READY_FOR_REWARDS":
                # A draft finished under an older source build can still be
                # claimed after upgrade. Repair only its pending coin amount;
                # preserve the exact packs already chosen for that run.
                reward = _draft_reward_for_run(run, _int_or(run.get("wins"), 0))[0]
            pack_rewards = [
                int(pack_id) for pack_id in (run.get("pendingPackRewards", []) if run else [])
                if int(pack_id) > 0 and get_pack_cfg(int(pack_id))
            ]
            if reward:
                STATE.add_credits(reward)
            # FIFA 20's native Draft carousel uses a numeric award enum, not
            # the string enum used by SBCs/the Web App.  The supplied retail
            # screenshots prove type=2 is the coin tile (the old pack row
            # rendered as "1 Coins"); type=0 is the pack tile and its halId
            # must be the actual pack id.  Keep the wire object minimal so the
            # native FutGetDraftAwardServerResponse parser sees its 3 fields.
            response = [{"halId": 0, "type": 2, "value": max(0, reward)}]
            for pack_id in pack_rewards:
                STATE.grant_unopened_pack(
                    pack_id,
                    json.dumps({"source": "draft", "wins": int(run.get("wins", 0) or 0)}),
                )
                response.append({"halId": pack_id, "type": 0, "value": 1})
            STATE.clear_draft_run()
            log.warning("DRAFT-SCOUT AWARD coins=%s packs=%s", reward, pack_rewards)
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)

    if low == "/ut/game/fifa20/match/end" and method in ("POST", "PUT"):
        reason = str(payload.get("endReason", "LOSS") if isinstance(payload, dict) else "LOSS").upper()
        # Hub entry records the active online mode. Handle it before any stale
        # Squad Battles state left by an interrupted local match.
        if _seasonal_mode() in ("rivals", "champion"):
            seasonal_mode = _seasonal_mode()
            seasonal = _seasonal_record_match_end(
                reason, _match_report_key(
                    payload, seasonal_mode,
                    STATE.get("generic_current_match_key", ""),
                )
            )
            if seasonal is not None:
                balance = STATE.credits()
                return 200, {"Cache-Control": "no-store"}, json_bytes({
                    "endReason": reason, "won": 1 if reason == "WIN" else 0,
                    "draw": 1 if reason == "DRAW" else 0,
                    "loss": 1 if reason not in ("WIN", "DRAW") else 0,
                    "credits": balance, "coins": balance,
                    "currencies": [
                        {"name": "coins", "funds": balance, "finalFunds": balance},
                        {"name": "points", "funds": 0, "finalFunds": 0},
                    ],
                    **seasonal,
                    **_scmp_live_fields(),
                })
        sb_state = _sb_state()
        # Only an actually running Squad Battles match may claim this shared
        # endpoint. A cached result from an earlier SB match was swallowing the
        # native Draft WIN report and causing FIFA's "under review" outcome.
        if sb_state.get("matchInProgress"):
            return 200, {"Cache-Control": "no-store"}, json_bytes(_sb_end_match(payload))
        with _draft_lock:
            run = _draft_run()
            if run is not None:
                report_gid = _int_or(payload.get("gid"), 0) if isinstance(payload, dict) else 0
                report_items = payload.get("items", []) if isinstance(payload, dict) else []
                # Backing out of FIFA's Home/Away/controller screen emits a
                # DNF-shaped report before a game session exists: gid=0 and no
                # player items. This is a cancelled launch, never a Draft loss.
                pre_match_cancel = (
                    reason == "DNF"
                    and report_gid == 0
                    and not report_items
                    and _int_or(run.get("currentMatchGid"), 0) == 0
                )
                if pre_match_cancel:
                    run["state"] = "READY_FOR_MATCH"
                    run["currentMatchGid"] = 0
                    run["lastSettledGid"] = 0
                    run["lastMatchEndResponse"] = {}
                    run["pendingCoinReward"] = 0
                    run["pendingPackRewards"] = []
                    _draft_save(run)
                    balance = STATE.credits()
                    response = {
                        "gid": 0, "gameId": 0, "matchReportId": 0,
                        "endReason": "DNF", "secondsPlayed": 0,
                        "opponentTeamId": _int_or(
                            run.get("currentOpponentTeamId"),
                            _DRAFT_OPPONENT_CLUBS[min(3, _int_or(run.get("wins"), 0))][0],
                        ),
                        "completionAward": 0, "rewardCoins": 0, "totalCoins": 0,
                        "credits": balance, "coins": balance,
                        "draftWins": _int_or(run.get("wins"), 0),
                        "gamesWonCurrentMatch": _int_or(run.get("wins"), 0),
                        "squadState": "READY_FOR_MATCH", "draftEliminated": False,
                        "won": 0, "draw": 0, "loss": 0,
                        "currencies": [
                            {"name": "coins", "funds": balance, "finalFunds": balance},
                            {"name": "points", "funds": 0, "finalFunds": 0},
                        ],
                        **_scmp_live_fields(),
                    }
                    log.warning(
                        "DRAFT-SCOUT MATCH_CANCELLED gid=0 wins=%s state=READY_FOR_MATCH",
                        run.get("wins", 0),
                    )
                    return 200, {"Cache-Control": "no-store"}, json_bytes(response)
                cached_gid = _int_or(run.get("lastSettledGid"), 0)
                cached_response = run.get("lastMatchEndResponse", {})
                if (report_gid > 0 and report_gid == cached_gid
                        and isinstance(cached_response, dict) and cached_response):
                    log.warning("DRAFT-SCOUT MATCH_END duplicate gid=%s wins=%s",
                                report_gid, run.get("wins", 0))
                    return 200, {"Cache-Control": "no-store"}, json_bytes(
                        _with_scmp_live(cached_response))
                # Draft Objectives must follow the settled Draft result, not
                # a generic FUT outcome. Accept the native/result aliases FIFA
                # can emit, but only an actual Draft WIN advances draft.wins.
                # Every settled Draft match still advances draft.matches, which
                # is what Draft Debut and Draft Run use.
                raw_draft_reason = reason
                if raw_draft_reason in ("WIN", "WON", "VICTORY"):
                    reason = "WIN"
                else:
                    # Offline Draft cannot progress on a draw; every non-win
                    # settlement ends the run as a loss (QUIT/DNF included).
                    reason = "LOSS"

                if reason == "WIN":
                    run["wins"] = min(4, int(run.get("wins", 0) or 0) + 1)
                    run["state"] = "READY_FOR_REWARDS" if run["wins"] >= 4 else "READY_FOR_MATCH"
                    eliminated = run["wins"] >= 4
                    if eliminated:
                        reward, packs = _draft_reward_for_run(run, 4)
                        run["pendingCoinReward"] = reward
                        run["pendingPackRewards"] = list(packs)
                        run["draftsCompleted"] = int(run.get("draftsCompleted", 0) or 0) + 1
                    else:
                        reward = 0
                        run["pendingCoinReward"] = 0
                        run["pendingPackRewards"] = []
                else:
                    run["losses"] = int(run.get("losses", 0) or 0) + 1
                    run["state"] = "READY_FOR_REWARDS"
                    eliminated = True
                    reward, packs = _draft_reward_for_run(
                        run, int(run.get("wins", 0) or 0)
                    )
                    run["pendingCoinReward"] = reward
                    run["pendingPackRewards"] = list(packs)
                    run["draftsCompleted"] = int(run.get("draftsCompleted", 0) or 0) + 1
                    reason = "LOSS"
                balance = STATE.credits()
                response = {
                    "gid": _int_or(payload.get("gid"), 0) if isinstance(payload, dict) else 0,
                    "gameId": _int_or(payload.get("gid"), 0) if isinstance(payload, dict) else 0,
                    "matchReportId": _int_or(payload.get("matchReportId"), 0) if isinstance(payload, dict) else 0,
                    "endReason": reason,
                    "secondsPlayed": int(payload.get("secondsPlayed", 0) or 0) if isinstance(payload, dict) else 0,
                    "matchDifficulty": _draft_game_difficulty(run),
                    "sqbtMatchDifficulty": _draft_game_difficulty(run),
                    "opponentTeamId": _int_or(
                        run.get("currentOpponentTeamId"),
                        _DRAFT_OPPONENT_CLUBS[min(3, _int_or(run.get("wins"), 0))][0],
                    ),
                    "completionAward": reward,
                    "rewardCoins": reward,
                    "totalCoins": reward,
                    "credits": balance,
                    "coins": balance,
                    "draftWins": int(run.get("wins", 0) or 0),
                    "gamesWonCurrentMatch": int(run.get("wins", 0) or 0),
                    "squadState": str(run.get("state", "READY_FOR_MATCH")),
                    "draftEliminated": eliminated,
                    "currencies": [{"name": "coins", "funds": balance, "finalFunds": balance},
                                   {"name": "points", "funds": 0, "finalFunds": 0}],
                    "won": 1 if reason == "WIN" else 0,
                    "draw": 0,
                    "loss": 0 if reason == "WIN" else 1,
                }
                STATE.record_global_match(
                    reason,
                    _match_report_key(
                        payload, "draft",
                        f"{run.get('draftId', 1)}:{run.get('wins', 0)}:{run.get('losses', 0)}",
                    ),
                    xp_eligible=raw_draft_reason in (
                        "WIN", "WON", "VICTORY", "DRAW", "TIE",
                        "LOSS", "LOST", "DEFEAT",
                    ),
                )
                response.update(_scmp_live_fields())
                run["lastSettledGid"] = report_gid
                run["lastMatchEndResponse"] = dict(response)
                _draft_save(run)
                log.warning("DRAFT-SCOUT MATCH_END reason=%s wins=%s state=%s",
                            reason, run["wins"], run["state"])
                return 200, {"Cache-Control": "no-store"}, json_bytes(response)
        if sb_state.get("lastMatchResponse"):
            # Idempotent retry of the most recent SB report when no Draft run
            # exists. A real Draft always wins the branch above.
            return 200, {"Cache-Control": "no-store"}, json_bytes(_sb_end_match(payload))
        # Friendlies and any other completed native mode reach the shared
        # endpoint without a Draft/SQBT/seasonal context. They still count in
        # the club-wide header record.
        generic_result = reason if reason in ("WIN", "DRAW") else "LOSS"
        generic_key = str(STATE.get("generic_current_match_key", "") or "")
        STATE.record_global_match(
            generic_result, _match_report_key(payload, "generic", generic_key),
            xp_eligible=reason in ("WIN", "DRAW", "LOSS"),
        )
        record = STATE.global_match_record()
        return 200, {"Cache-Control": "no-store"}, json_bytes({
            "endReason": generic_result,
            "won": 1 if generic_result == "WIN" else 0,
            "draw": 1 if generic_result == "DRAW" else 0,
            "loss": 1 if generic_result == "LOSS" else 0,
            **record,
            **_credits_payload(),
            **_scmp_live_fields(),
        })

    if low == "/ut/game/fifa20/match" and method in ("POST", "PUT"):
        # Concept squads are planning-only in retail FUT. Their high transient
        # browse IDs must never be accepted as owned match items. Draft uses a
        # separate saved run, so it remains launchable even if a concept squad
        # was the last normal squad viewed.
        active_saved_squad = next(
            (squad for squad in STATE.list_squads() if bool(squad.get("active", False))),
            None,
        )
        active_has_concepts = bool(
            isinstance(active_saved_squad, dict)
            and any(
                isinstance(slot, dict)
                and (
                    _int_or(slot.get("conceptResourceId"), 0) > 0
                    or bool(slot.get("dream", False))
                    or bool(slot.get("concept", False))
                )
                for slot in active_saved_squad.get("players", [])
            )
        )
        if (_draft_run() is None and isinstance(active_saved_squad, dict)
                and (
                    str(active_saved_squad.get("squadType", "")).upper() == "DREAM_SQUAD"
                    or active_has_concepts
                )):
            log.warning(
                "MATCH rejected planning-only concept squad id=%s",
                active_saved_squad.get("id", active_saved_squad.get("squadId", 0)),
            )
            return 409, {"Cache-Control": "no-store"}, json_bytes({
                "reason": "CONCEPT_SQUAD_NOT_PLAYABLE",
                "dreamSquad": True,
                "success": False,
            })
        # After the Squad Battles POST, FIFA sends a PUT containing only the
        # generated gid and the selected squad item IDs. Those fields carry no
        # SQBT marker, so an unfinished/abandoned Draft used to capture this
        # continuation and return DRAFT_NOT_READY_FOR_MATCH. The active match
        # state is authoritative for this second-stage handshake.
        if method == "PUT":
            active_sb_state = _sb_state()
            if bool(active_sb_state.get("matchInProgress")):
                active_opponent = _sb_selected(active_sb_state)
                if active_opponent is not None:
                    response = _sb_match_response(active_sb_state, active_opponent)
                    gid = _sb_find_int(payload, ("gid", "gameId"), 0)
                    if gid > 0:
                        active_sb_state["currentMatchGid"] = gid
                        response["gid"] = gid
                        response["gameId"] = gid
                        _sb_save(active_sb_state)
                    log.warning(
                        "SQUAD-BATTLES MATCH_HANDSHAKE gid=%s draftBypassed=%s",
                        gid, _draft_run() is not None,
                    )
                    return 200, {"Cache-Control": "no-store"}, json_bytes(response)
        if _sb_is_match_request(payload, query):
            tracked = _sb_start_match(payload, query)
            if str(tracked.get("reason", "")) == "NO_SQUAD_BATTLE_OPPONENTS":
                return 409, {"Cache-Control": "no-store"}, json_bytes(tracked)
            requested_slot = _sb_requested_opponent(payload, query)
            requested_event = _sb_find_int(
                payload, ("sqbtEventId",), _int_or(tracked.get("sqbtEventId"), 0)
            )
            selected_state = _sb_state()
            featured_match = bool(selected_state.get("selectedFeatured"))
            if featured_match:
                # Featured squads have no 0..3 regular carousel slot. Preserve
                # the selected feature consumer so the custom All-Stars tile is
                # not silently replaced by the TOTW during launch.
                feature_consumer = str(
                    selected_state.get("selectedFeaturedType", "sqbttotw")
                )
                featured_id = (
                    _SB_FEATURED_CUSTOM_ID
                    if feature_consumer == "sqbt" else _TOTW_LATEST_ID
                )
                response = dict(tracked)
                response.update({
                    "sqbtEventId": requested_event,
                    "sqbtOpponentSquadId": featured_id,
                    "sqbtOppid": featured_id,
                    "featureConsumerId": feature_consumer,
                    "featuredSquadId": featured_id,
                    "sqbtMatchDifficulty": _int_or(tracked.get("matchDifficulty"), 3),
                })
                opponent_slot = featured_id
            else:
                # _sb_start_match already resolved the visible native slot to
                # this round's persisted, freshly generated opponent.  Reusing
                # the old static helper here silently replaced that team during
                # the launch handshake and defeated round rotation.
                opponent_slot = max(0, min(
                    _int_or(tracked.get("sqbtOpponentSquadId"), requested_slot),
                    len(_SQUAD_BATTLE_PRESETS) - 1,
                ))
                response = dict(tracked)
                response.update({
                    # Echo FIFA's launch identifiers. Replacing the event id with
                    # the local persistence timestamp makes the native game-session
                    # handoff refer to a different competition than the UI selected.
                    "sqbtEventId": requested_event,
                    "sqbtOpponentSquadId": opponent_slot,
                    "sqbtOppid": opponent_slot,
                    "sqbtMatchDifficulty": _int_or(tracked.get("matchDifficulty"), 3),
                    "matchDifficulty": _int_or(tracked.get("matchDifficulty"), 3),
                })
            _seasonal_set_mode("")
            log.warning(
                "SQUAD-BATTLES MATCH event=%s opponent=%s difficulty=%s draftBypassed=%s",
                response.get("sqbtEventId"), response.get("sqbtOpponentSquadId"),
                response.get("matchDifficulty"), _draft_run() is not None,
            )
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)
        draft_run = _draft_run()
        if draft_run is not None:
            if draft_run.get("state") != "READY_FOR_MATCH":
                return 409, {}, json_bytes({"reason": "DRAFT_NOT_READY_FOR_MATCH"})
            # Entering Draft explicitly retires any cached/incomplete Squad
            # Battles match context without touching weekly points or history.
            raw_sb_state = STATE.get(_SB_STATE_KEY, {})
            if isinstance(raw_sb_state, dict) and (
                    raw_sb_state.get("matchInProgress") or raw_sb_state.get("lastMatchResponse")):
                raw_sb_state["matchInProgress"] = False
                raw_sb_state["lastMatchResponse"] = {}
                _sb_save(raw_sb_state)
            _seasonal_set_mode("")
            if isinstance(payload, dict):
                if "gid" in payload:
                    draft_run["currentMatchGid"] = _int_or(payload.get("gid"), 0)
                else:
                    draft_run["currentMatchGid"] = 0
                round_index = min(3, max(0, _int_or(draft_run.get("wins"), 0)))
                draft_run["currentOpponentTeamId"] = _DRAFT_OPPONENT_CLUBS[round_index][0]
                _draft_save(draft_run)
            response = _native_draft_match_response(draft_run)
            log.warning("DRAFT-SCOUT MATCH starters=%s opponentTeamId=%s difficulty=%s",
                        sum(1 for slot in response["squad"].get("players", [])[:11]
                            if isinstance(slot.get("itemData"), dict)),
                        response.get("opponentTeamId"), response.get("matchDifficulty"))
            return 200, {"Cache-Control": "no-store"}, json_bytes(response)
        if _seasonal_mode() in ("rivals", "champion"):
            sequence = _int_or(STATE.get("generic_match_sequence", 0), 0) + 1
            STATE.set("generic_match_sequence", sequence)
            STATE.set("generic_current_match_key", f"seasonal:{sequence}")
            squad = _native_squad()
            actives = _active_club_items()
            squad["actives"] = actives
            squad["club"] = [dict(item) for item in actives]
            squad["stadiumAssetId"] = _DRAFT_STADIUM_ID
            squad["stadiumId"] = _DRAFT_STADIUM_ID
            squad["home"] = True
            squad["isHomeTeam"] = True
            opponent = _native_squad_battle_opponent(2)
            opponent["isHomeTeam"] = False
            opponent["home"] = False
            return 200, {"Cache-Control": "no-store"}, json_bytes({
                "squad": squad, "homeSquad": squad,
                "opponentSquad": opponent, "awaySquad": opponent,
                "opponent": opponent, "opponentTeamId": 2,
                "isHomeTeam": True, "home": True, "startDateTime": now_s(),
                "stadiumAssetId": _DRAFT_STADIUM_ID,
                "stadiumId": _DRAFT_STADIUM_ID,
            })
        sequence = _int_or(STATE.get("generic_match_sequence", 0), 0) + 1
        STATE.set("generic_match_sequence", sequence)
        STATE.set("generic_current_match_key", f"generic:{sequence}")
        return 200, {}, json_bytes({})

    offline_squad = re.fullmatch(r"/ut/game/fifa20/offlinesquad/(\d+)", low)
    if offline_squad and method == "GET":
        requested = int(offline_squad.group(1))
        raw_sb_state = STATE.get(_SB_STATE_KEY, {})
        sb_match_active = (
            isinstance(raw_sb_state, dict)
            and bool(raw_sb_state.get("matchInProgress"))
        )
        if sb_match_active:
            selected = _sb_selected(raw_sb_state)
            if selected is not None and isinstance(selected.get("squad"), dict):
                squad = json.loads(json.dumps(selected["squad"]))
            else:
                squad = _native_squad_battle_opponent(requested)
            log.warning("SQUAD-BATTLES OFFLINE_SQUAD id=%s draftBypassed=%s",
                        requested, _draft_run() is not None)
        else:
            run = _draft_run()
            if run is not None:
                round_index = min(3, max(0, int(run.get("wins", 0) or 0)))
                squad = _draft_opponent_squad(round_index)
                squad["squadType"] = "DRAFT_SQUAD"
                game_difficulty = _draft_game_difficulty(run)
                squad.update({
                    "matchDifficulty": game_difficulty,
                    "sqbtMatchDifficulty": game_difficulty,
                    "difficulty": game_difficulty,
                    "difficultyId": game_difficulty,
                    "gameDifficulty": game_difficulty,
                    "difficultyName": _DRAFT_DIFFICULTIES[game_difficulty - 1],
                })
                log.warning("DRAFT-SCOUT OFFLINE_SQUAD id=%s round=%s", requested, round_index)
            else:
                squad = _native_squad_battle_opponent(requested)
                log.warning("SQUAD-BATTLES OFFLINE_SQUAD id=%s", requested)
        squad["id"] = requested
        squad["squadId"] = requested
        squad["active"] = True
        squad["valid"] = True
        squad["home"] = True
        squad["isHomeTeam"] = True
        return 200, {"Cache-Control": "no-store"}, json_bytes(squad)

    if CFG.get("debug_unknown_http", True):
        log.warning("Unknown FUT/POW request: %s %s body=%s", method, raw_path, body[:500])
    
    # Handoff: Log unhandled /ut/ routes explicitly so we see what FIFA tries next
    if "/ut/" in low:
        log.warning("[HANDOFF-UNKNOWN] FIFA tried /ut/ path but no handler: %s %s", method, raw_path)
        return 200, {"X-FIFA20LocalFUT-Unknown": "1"}, json_bytes({})
    
    return 200, {"X-FIFA20LocalFUT-Unknown": "1"}, json_bytes({})


class FutHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FIFA20LocalFUT/0.3"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("FUT8080 %s - %s", self.client_address[0], fmt % args)

    def _go(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(n) if n else b""
        hdrs = {k: v for k, v in self.headers.items()}
        host_hdr = self.headers.get("Host", self.headers.get("host", ""))
        # A post-accountinfo Nucleus request has to arrive over the local TLS
        # listener.  Log the transport before dispatch so a hostname/cert
        # problem is distinguishable from an HTTP routing problem.
        tls_cipher = getattr(self.connection, "cipher", lambda: None)()
        transport = "https" if tls_cipher else "http"
        log.info("[FUT-TRANSPORT] scheme=%s host=%s method=%s path=%s", transport, host_hdr or "-", self.command, self.path)
        if host_hdr and "127.0.0.1" not in host_hdr and "localhost" not in host_hdr.lower():
            log.info("[FUT-HOST] Host=%s path=%s method=%s (external hostname pinned to local)", host_hdr, self.path, self.command)
        try:
            status, extra, out = route_fut(self.command, self.path, hdrs, body)
        except Exception:
            log.exception("FUT handler error")
            status, extra, out = 500, {}, json_bytes({"error": "local server exception"})
        FLOW.log_http("FUT", self.command, self.path, len(body), len(out), status, _ut_sid())
        self.send_response(status)
        content_type = extra.pop("Content-Type", "application/json; charset=utf-8")
        connection = extra.pop("Connection", "keep-alive")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", connection)
        # Native FIFA 20 expects nucleus/session headers on FUT responses even
        # for local auth; without them accountinfo succeeds but /ut/auth is never started.
        self.send_header("Easw-Session-Data-Nucleus-Id", str(CFG["nucleus_id"]))
        self.send_header("Nucleus-Id", str(CFG["nucleus_id"]))
        self.send_header("Persona-Id", str(CFG["persona_id"]))
        # Always emit session + phishing headers on FUT traffic so the client
        # does not abort after accountinfo when /ut/auth was skipped.
        ut_sid = _ut_sid()
        self.send_header("X-UT-SID", ut_sid)
        self.send_header("X-UT-PHISHING-TOKEN", "fut20-local-phish-" + ut_sid[-12:])
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-UT-SID,X-UT-PHISHING-TOKEN,Nucleus-Id,Persona-Id")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(out)
        if connection.lower() == "close":
            self.close_connection = True

    do_GET = _go
    do_POST = _go
    do_PUT = _go
    do_DELETE = _go
    do_OPTIONS = _go


class RedirectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalFUT20Redirector/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("REDIR42231 %s - %s", self.client_address[0], fmt % args)

    def _reply(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n else b""
        seq = self.headers.get("X-BLAZE-SEQNO", "0")
        log.info("[Redirector] request method=%s path=%s seq=%s requestBytes=%d bodyPreview=%s", self.command, self.path, seq, len(raw), raw[:300])
        FLOW.mark("SERVER_INSTANCE", "Redirector", f"{self.command} {self.path}")
        local_host = str(CFG.get("local_host", "127.0.0.1"))
        ip_int = 2130706433  # 127.0.0.1 in host-order integer form used by old redirector XML.
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<serverinstanceinfo>
    <address member="0">
        <valu>
            <hostname>{html.escape(local_host)}</hostname>
            <ip>{ip_int}</ip>
            <port>{int(CFG["blaze_port"])}</port>
        </valu>
    </address>
    <secure>0</secure>
</serverinstanceinfo>'''.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(xml)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(xml)
        FLOW.log_http("QoS", self.command, self.path, 0, len(xml), status, STATE.sid())
        FLOW.log_http("Redirector", self.command, self.path, len(raw), len(xml), 200, STATE.sid())

    do_POST = _reply
    do_GET = _reply


class QosHandler(BaseHTTPRequestHandler):
    """FIFA 20 QoS HTTP service learned from the successful V3 capture."""
    protocol_version = "HTTP/1.1"
    server_version = "LocalFUT20QoS/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("QOS17502 %s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.lower()
        qos_ip_le = 16777343  # 127.0.0.1 interpreted as little-endian uint32.

        if path == "/qos/qos":
            qtyp = q.get("qtyp", ["1"])[0]
            if qtyp == "2":
                xml = (
                    f'<?xml version="1.0" encoding="utf-8"?><qos>'
                    f'<numprobes>10</numprobes><qosport>{int(CFG["qos_port"])}</qosport>'
                    f'<probesize>1200</probesize><qosip>{qos_ip_le}</qosip>'
                    f'<requestid>1234</requestid><reqsecret>5678</reqsecret></qos>'
                ).encode("utf-8")
            else:
                xml = (
                    f'<?xml version="1.0" encoding="utf-8"?><qos>'
                    f'<numprobes>0</numprobes><qosport>{int(CFG["qos_port"])}</qosport>'
                    f'<probesize>0</probesize><qosip>{qos_ip_le}</qosip>'
                    f'<requestid>1</requestid><reqsecret>0</reqsecret></qos>'
                ).encode("utf-8")
            status = 200
        elif path == "/qos/firewall":
            xml = (
                f'<?xml version="1.0" encoding="utf-8"?><firewall>'
                f'<ips><ips>{qos_ip_le}</ips><ips>{qos_ip_le}</ips></ips>'
                f'<numinterfaces>2</numinterfaces>'
                f'<ports><ports>{int(CFG["qos_port"])}</ports><ports>{int(CFG["qos_port"])}</ports></ports>'
                f'<requestid>1234</requestid><reqsecret>5678</reqsecret></firewall>'
            ).encode("utf-8")
            status = 200
        elif path == "/qos/firetype":
            xml = b'<?xml version="1.0" encoding="utf-8"?><firetype><firetype>2</firetype></firetype>'
            status = 200
        else:
            xml = b"not found"
            status = 404

        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=utf-8" if status == 200 else "text/plain")
        self.send_header("Content-Length", str(len(xml)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(xml)


class EaswHandler(BaseHTTPRequestHandler):
    """Legacy EASW service used between Blaze login and FUT /ut/auth."""
    protocol_version = "HTTP/1.1"
    server_version = "FIFA20LocalEASW/0.3"

    ROSTER_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<squad>
  <squadInfoSet>
    <squadInfo platform="pc64">
      <dbSchemaCRC>596426fe3525c25b90593b800b9ce5c3c61ccab5</dbSchemaCRC>
      <dbMajor>1</dbMajor><dbMinor>0</dbMinor>
      <dbMajorCRC>0</dbMajorCRC><dbMinorCRC>0</dbMinorCRC>
      <dbMajorLoc></dbMajorLoc><dbMinorLoc></dbMinorLoc>
      <dbFUTVer>1</dbFUTVer><dbFUTCRC>0</dbFUTCRC><dbFUTLoc></dbFUTLoc>
    </squadInfo>
    <squadInfo platform="pc64">
      <dbSchemaCRC>00e20ae6ad44d892ad15ea7f060ce6d97ca9e730</dbSchemaCRC>
      <dbMajor>1</dbMajor><dbMinor>0</dbMinor>
      <dbMajorCRC>0</dbMajorCRC><dbMinorCRC>0</dbMinorCRC>
      <dbMajorLoc></dbMajorLoc><dbMinorLoc></dbMinorLoc>
      <dbFUTVer>1</dbFUTVer><dbFUTCRC>0</dbFUTCRC><dbFUTLoc></dbFUTLoc>
    </squadInfo>
  </squadInfoSet>
</squad>'''

    @staticmethod
    def _routing_xml() -> bytes:
        host = html.escape(str(CFG.get("local_host", "127.0.0.1")), quote=True)
        fut = int(CFG["fut_port"])
        easw = int(CFG["easw_port"])
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<routing fileVersion="1" refresh="0">
  <route key="FUT_RS4_BASE_URL" value="http://{host}:{fut}/ut/" />
  <route key="FIFA_POW_URL" value="http://{host}:{fut}/" />
  <route key="FIFA_RS4_URL" value="http://{host}:{fut}" />
  <route key="ONLINE/SERVER_RS4" value="http://{host}:{fut}" />
  <route key="FUT_URI" value="http://{host}:{fut}/ut/" />
  <route key="ROSTERUPDATE_URL" value="http://{host}:{easw}/roster" />
  <route key="ROUTINGCFGFILE_URL" value="http://{host}:{easw}/routing" />
</routing>'''
        return xml.encode("utf-8")

    @staticmethod
    def _ctl_xml() -> bytes:
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<ctl>
  <status>ok</status>
  <serverTime>{now_s()}</serverTime>
</ctl>'''
        return xml.encode("utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("EASW42232 %s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: bytes, content_type: str = "text/xml", extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        FLOW.log_http("EASW", self.command, self.path, 0, len(body), status, STATE.sid())

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n:
            self.rfile.read(n)
        path = urllib.parse.urlsplit(self.path).path.lower()
        session_headers = {
            "EASW-Session": STATE.sid(),
            "EASW-Token": STATE.token(),
            "EASW-Userid": str(CFG["nucleus_id"]),
        }
        self._send(
            200,
            b'<?xml version="1.0" encoding="UTF-8"?>\n<result/>\n',
            "text/xml",
            session_headers,
        )

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path.lower()
        session_headers = {
            "EASW-Session": STATE.sid(),
            "EASW-Token": STATE.token(),
            "EASW-Userid": str(CFG["nucleus_id"]),
        }
        if path == "/roster":
            FLOW.mark("EASW_ROSTER", "EASW", f"{self.command} {self.path}", STATE.sid())
            self._send(200, self.ROSTER_XML, extra=session_headers)
        elif path == "/routing":
            FLOW.mark("EASW_ROUTING", "EASW", f"{self.command} {self.path}", STATE.sid())
            self._send(200, self._routing_xml(), extra=session_headers)
        elif path == "/ctl":
            self._send(200, self._ctl_xml(), extra=session_headers)
        elif path.startswith("/easw/"):
            self._send(200, b'<?xml version="1.0" encoding="UTF-8"?>\n<result/>\n', "text/xml", extra=session_headers)
        else:
            self._send(404, b"not found", "text/plain")


# ---- Blaze Fire2 / TDF ----------------------------------------------------
# FIFA 20's 44321 connection uses the Fire2 Blaze frame used by later
# BlazeSDK builds. The captured client packet is Util.PreAuth (component 9,
# command 7). 0.1.2 only recorded it; 0.1.3 parses/replies and keeps the
# session alive so later requests can be learned/handled locally.

_TDF_VARINT = 0x0
_TDF_STRING = 0x1
_TDF_BLOB = 0x2
_TDF_GROUP = 0x3
_TDF_LIST = 0x4
_TDF_MAP = 0x5
_TDF_UNION = 0x6
_TDF_OBJECT_TYPE = 0x8
_TDF_OBJECT_ID = 0x9


def _tdf_tag(tag: str | bytes, value_type: int) -> bytes:
    """Encode a 1-4 character Blaze TDF tag plus type byte."""
    raw = tag.encode("ascii") if isinstance(tag, str) else bytes(tag)
    if not 1 <= len(raw) <= 4:
        raise ValueError(f"TDF tag must be 1-4 bytes: {raw!r}")
    out = [0, 0, 0, int(value_type) & 0xFF]
    if len(raw) > 0:
        out[0] |= (raw[0] & 0x40) << 1
        out[0] |= (raw[0] & 0x10) << 2
        out[0] |= (raw[0] & 0x0F) << 2
    if len(raw) > 1:
        out[0] |= (raw[1] & 0x40) >> 5
        out[0] |= (raw[1] & 0x10) >> 4
        out[1] |= (raw[1] & 0x0F) << 4
    if len(raw) > 2:
        out[1] |= (raw[2] & 0x40) >> 3
        out[1] |= (raw[2] & 0x10) >> 2
        out[1] |= (raw[2] & 0x0C) >> 2
        out[2] |= (raw[2] & 0x03) << 6
    if len(raw) > 3:
        out[2] |= (raw[3] & 0x40) >> 1
        out[2] |= raw[3] & 0x1F
    return bytes(out)


def _tdf_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        # Blaze serializes signed integers through their unsigned bit pattern.
        value &= (1 << 64) - 1
    if value < 0x40:
        return bytes([value])
    out = bytearray([(value & 0x3F) | 0x80])
    value >>= 6
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _tdf_str_value(value: str) -> bytes:
    raw = str(value).encode("utf-8")
    return _tdf_varint(len(raw) + 1) + raw + b"\x00"


def _tdf_field_int(tag: str, value: int) -> bytes:
    return _tdf_tag(tag, _TDF_VARINT) + _tdf_varint(value)


def _tdf_field_str(tag: str, value: str) -> bytes:
    return _tdf_tag(tag, _TDF_STRING) + _tdf_str_value(value)


def _tdf_field_blob(tag: str, value: bytes) -> bytes:
    raw = bytes(value)
    return _tdf_tag(tag, _TDF_BLOB) + _tdf_varint(len(raw)) + raw


def _tdf_field_object_id(tag: str, component: int, entity_type: int, entity_id: int) -> bytes:
    """Encode Blaze Heat2 ObjectId: component, entity type, then 64-bit id as varints."""
    return (
        _tdf_tag(tag, _TDF_OBJECT_ID)
        + _tdf_varint(component)
        + _tdf_varint(entity_type)
        + _tdf_varint(entity_id)
    )


def _tdf_field_group(tag: str, body: bytes) -> bytes:
    return _tdf_tag(tag, _TDF_GROUP) + body + b"\x00"


def _tdf_field_list_int(tag: str, values: list[int] | tuple[int, ...]) -> bytes:
    out = bytearray(_tdf_tag(tag, _TDF_LIST))
    out.append(_TDF_VARINT)
    out.extend(_tdf_varint(len(values)))
    for value in values:
        out.extend(_tdf_varint(value))
    return bytes(out)


def _tdf_field_list_groups(tag: str, groups: list[bytes]) -> bytes:
    out = bytearray(_tdf_tag(tag, _TDF_LIST))
    out.append(_TDF_GROUP)
    out.extend(_tdf_varint(len(groups)))
    for body in groups:
        out.extend(body)
        out.append(0)  # each group body terminator
    return bytes(out)


def _tdf_field_map_str_str(tag: str, values: list[tuple[str, str]]) -> bytes:
    out = bytearray(_tdf_tag(tag, _TDF_MAP))
    out.extend((_TDF_STRING, _TDF_STRING))
    out.extend(_tdf_varint(len(values)))
    for key, value in values:
        out.extend(_tdf_str_value(key))
        out.extend(_tdf_str_value(value))
    return bytes(out)


def _tdf_field_map_str_group(tag: str, values: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(_tdf_tag(tag, _TDF_MAP))
    out.extend((_TDF_STRING, _TDF_GROUP))
    out.extend(_tdf_varint(len(values)))
    for key, body in values:
        out.extend(_tdf_str_value(key))
        out.extend(body)
        out.append(0)  # group terminator
    return bytes(out)


def _tdf_decode_varint(raw: bytes, pos: int = 0) -> tuple[int, int]:
    if pos >= len(raw):
        raise ValueError("truncated TDF varint")
    first = raw[pos]
    pos += 1
    value = first & 0x3F
    shift = 6
    if first & 0x80:
        while True:
            if pos >= len(raw):
                raise ValueError("truncated TDF varint")
            b = raw[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
    return value, pos


def _tdf_get_string(raw: bytes, tag: str) -> str | None:
    marker = _tdf_tag(tag, _TDF_STRING)
    p = raw.find(marker)
    if p < 0:
        return None
    p += 4
    try:
        n, p = _tdf_decode_varint(raw, p)
    except ValueError:
        return None
    if n <= 0 or p + n > len(raw):
        return None
    value = raw[p:p+n]
    if value.endswith(b"\x00"):
        value = value[:-1]
    return value.decode("utf-8", errors="replace")


def _tdf_get_strings(raw: bytes, tag: str) -> list[str]:
    """Return every string field with *tag* from a Blaze TDF payload."""
    marker = _tdf_tag(tag, _TDF_STRING)
    values: list[str] = []
    cursor = 0
    while cursor < len(raw):
        p = raw.find(marker, cursor)
        if p < 0:
            break
        p += len(marker)
        try:
            n, value_pos = _tdf_decode_varint(raw, p)
        except ValueError:
            break
        if n <= 0 or value_pos + n > len(raw):
            cursor = p
            continue
        value = raw[value_pos:value_pos + n]
        if value.endswith(b"\x00"):
            value = value[:-1]
        values.append(value.decode("utf-8", errors="replace"))
        cursor = value_pos + n
    return values


def _tdf_get_int(raw: bytes, tag: str) -> int | None:
    marker = _tdf_tag(tag, _TDF_VARINT)
    p = raw.find(marker)
    if p < 0:
        return None
    try:
        value, _ = _tdf_decode_varint(raw, p + len(marker))
        return value
    except ValueError:
        return None


def _blaze_filter_for_profanity_payload(request: bytes) -> bytes:
    """Approve FIFA 20 user text using Blaze Util.FilterForProfanity's schema.

    FIFA sends a TLST list of FilteredUserText structures with DIRT set to
    FILTER_RESULT_UNPROCESSED (2).  The response uses the same structure and
    must return each UTXT with DIRT set to FILTER_RESULT_PASSED (0).  An empty
    success body makes the native squad-name workflow abort before POST /squad.
    """
    text_values = _tdf_get_strings(request, "UTXT")
    groups: list[bytes] = []
    for value in text_values:
        entry = bytearray()
        entry += _tdf_field_int("DIRT", 0)  # FILTER_RESULT_PASSED
        entry += _tdf_field_str("UTXT", value)
        groups.append(bytes(entry))
    return _tdf_field_list_groups("TLST", groups)



def _tdf_debug_summary(raw: bytes) -> str:
    """Best-effort non-mutating TDF scout used only for compatibility logging."""
    if not raw:
        return "<empty>"
    # Extract printable ASCII runs; method/adaptor names often survive here.
    strings = []
    cur = bytearray()
    for b in raw:
        if 32 <= b <= 126:
            cur.append(b)
        else:
            if len(cur) >= 3:
                strings.append(cur.decode("ascii", errors="replace"))
            cur.clear()
    if len(cur) >= 3:
        strings.append(cur.decode("ascii", errors="replace"))

    # Best-effort scan for packed Blaze TDF 3-char tags. Do not attempt to parse
    # values recursively; malformed guesses are harmless because this is logging only.
    tags = []
    for i in range(max(0, len(raw) - 3)):
        chunk = raw[i:i+3]
        # Packed TDF tags commonly decode to uppercase alnum/_ names. Keep only
        # plausible candidates to avoid a wall of noise.
        try:
            v = int.from_bytes(chunk, "big")
            chars = [
                ((v >> 18) & 0x3F) + 32,
                ((v >> 12) & 0x3F) + 32,
                ((v >> 6) & 0x3F) + 32,
                (v & 0x3F) + 32,
            ]
            name = ''.join(chr(c) for c in chars).rstrip()
            if 2 <= len(name) <= 4 and all(c.isupper() or c.isdigit() or c == '_' for c in name):
                if name not in tags:
                    tags.append(name)
        except Exception:
            pass
        if len(tags) >= 24:
            break
    return f"hex={raw[:256].hex()} ascii={strings[:20]} tags={tags}"

def _blaze_preauth_payload() -> bytes:
    """FIFA 20 PC PreAuth payload reconstructed from the successful Aurora trace.

    The important part is QOSS: FIFA uses the Blaze reply to discover the QoS
    HTTP service. 0.1.8 conflated that service with Blaze itself; the original
    topology uses Blaze on 44321 and QoS on 17502.
    """
    local_host = str(CFG.get("local_host", "127.0.0.1"))
    fut = int(CFG["fut_port"])
    local_identity_base = f"http://{local_host}:{fut}/"
    core_conf = _tdf_field_map_str_str(
        "CONF",
        [
            ("connIdleTimeout", "120s"),
            ("defaultRequestTimeout", "80s"),
            ("pingPeriod", "20s"),
            ("voipHeadsetUpdateRate", "1000"),
            ("xlspConnectionIdleTimeout", "300"),
            # FIFA 21's working path supplies this contract during PreAuth,
            # before OSDK_CORE is fetched. Keep FIFA 20's registered EA client
            # ID so EA App can issue the legitimate code, while directing the
            # subsequent identity hand-off to this local service.
            ("blazeServerClientId", "FOS-SERVER"),
            ("blazeSdkClientId", "FOS-SERVER"),
            ("blazeSdkClientSecret", ""),
            ("nucleusConnect", local_identity_base),
            ("identityRedirectUri", local_identity_base + "localfut/callback"),
        ],
    )

    bwps = bytearray()
    bwps += _tdf_field_str("PSA", "127.0.0.1")
    bwps += _tdf_field_int("PSP", int(CFG["qos_port"]))
    bwps += _tdf_field_str("SNA", "qos")

    latency = bytearray()
    latency += _tdf_field_str("PSA", "127.0.0.1")
    latency += _tdf_field_int("PSP", int(CFG["qos_port"]))
    latency += _tdf_field_str("SNA", "ea-sjc")

    qoss = bytearray()
    qoss += _tdf_field_group("BWPS", bytes(bwps))
    qoss += _tdf_field_int("LNP", 10)
    qoss += _tdf_field_map_str_group("LTPS", [("ea-sjc", bytes(latency))])
    qoss += _tdf_field_int("SVID", 1161889797)
    qoss += _tdf_field_int("TIME", 5000)

    body = bytearray()
    body += _tdf_field_str("ASRC", "303107")
    body += _tdf_field_list_int("CIDS", [1, 9, 35, 30722])
    body += _tdf_field_group("CONF", core_conf)
    body += _tdf_field_int("EEFA", 1)
    body += _tdf_field_str("ESRC", "303107")
    body += _tdf_field_str("INST", "fifa-2020-pc")
    body += _tdf_field_int("MAID", 89)
    body += _tdf_field_int("MINR", 0)
    body += _tdf_field_str("NASP", "cem_ea_id")
    body += _tdf_field_str("PILD", "")
    body += _tdf_field_str("PLAT", "pc")
    body += _tdf_field_group("QOSS", bytes(qoss))
    body += _tdf_field_str("RSRC", "303107")
    body += _tdf_field_str("SVER", "Blaze 15.1.1.8.2")
    return bytes(body)


def _blaze_auth_payload() -> bytes:
    persona = int(CFG["persona_id"])
    nucleus = int(CFG["nucleus_id"])
    session = bytearray()
    session += _tdf_field_int("1CON", 0)
    session += _tdf_field_int("BUID", persona)
    session += _tdf_field_int("FRST", 0)
    control_session = _control_session()
    session_key = str(control_session.get("accessToken")) if control_session else STATE.token()
    session += _tdf_field_str("KEY", session_key)
    session += _tdf_field_int("LLOG", now_s())
    session += _tdf_field_str("MAIL", "local@localhost")
    pdtl = bytearray()
    pdtl += _tdf_field_str("DSNM", str(CFG["display_name"]))
    pdtl += _tdf_field_int("LAST", 0)
    pdtl += _tdf_field_int("PID", persona)
    pdtl += _tdf_field_int("PLAT", 4)
    # PersonaStatus.ACTIVE.  A zero/unknown status can make the title reject
    # online play before it reaches FUT even though the session exists.
    pdtl += _tdf_field_int("STAS", 2)
    pdtl += _tdf_field_int("XREF", nucleus)
    session += _tdf_field_group("PDTL", bytes(pdtl))
    session += _tdf_field_int("UID", nucleus)

    body = bytearray()
    body += _tdf_field_int("CNTX", 0)
    body += _tdf_field_int("ERRC", 0)
    body += _tdf_field_str("SKEY", session_key)
    body += _tdf_field_int("ANON", 0)
    body += _tdf_field_int("NTOS", 0)
    body += _tdf_field_group("SESS", bytes(session))
    # SPAM is Blaze's isOfLegalContactAge flag (not unsolicited-email state).
    # It must be true for an adult local profile; false maps to FIFA's
    # "not eligible ... due to an age restriction" gate.
    body += _tdf_field_int("SPAM", 1)
    body += _tdf_field_int("UNDR", 0)
    return bytes(body)


def _blaze_entitlements_payload() -> bytes:
    ent = bytearray()
    ent += _tdf_field_str("DEVI", "")
    ent += _tdf_field_str("GDAY", "2014-09-25T00:00Z")
    ent += _tdf_field_str("GNAM", "FIFA15PC")
    ent += _tdf_field_int("ID", 1024871)
    ent += _tdf_field_int("ISCO", 0)
    ent += _tdf_field_int("PID", 0)
    ent += _tdf_field_str("PJID", "1024871")
    ent += _tdf_field_int("PRCA", 2)
    ent += _tdf_field_str("PRID", "FIFA15_PC_ONLINE_ACCESS")
    ent += _tdf_field_int("STAT", 1)
    ent += _tdf_field_int("STRC", 0)
    ent += _tdf_field_str("TAG", "ONLINE_ACCESS")
    ent += _tdf_field_str("TDAY", "")
    ent += _tdf_field_int("TYPE", 1)
    ent += _tdf_field_int("UCNT", 0)
    ent += _tdf_field_int("VER", 0)
    return _tdf_field_list_groups("NLST", [bytes(ent)])


def _blaze_postauth_payload() -> bytes:
    telemetry = bytearray()
    telemetry += _tdf_field_str("ADRS", "127.0.0.1")
    telemetry += _tdf_field_int("ANON", 0)
    telemetry += _tdf_field_str("DISA", "")
    telemetry += _tdf_field_str("FILT", "-UION/****")
    telemetry += _tdf_field_int("PORT", 0)
    telemetry += _tdf_field_int("SDLY", 15000)
    telemetry += _tdf_field_str("SESS", "LOCALFUT15")
    telemetry += _tdf_field_str("SKEY", "")
    telemetry += _tdf_field_int("SPCT", 0)

    ticker = bytearray()
    ticker += _tdf_field_str("ADRS", "127.0.0.1")
    ticker += _tdf_field_int("PORT", 0)
    ticker += _tdf_field_str("SKEY", "")

    urop = bytearray()
    urop += _tdf_field_int("TMOP", 1)
    urop += _tdf_field_int("UID", int(CFG["persona_id"]))

    body = bytearray()
    body += _tdf_field_group("TELE", bytes(telemetry))
    body += _tdf_field_group("TICK", bytes(ticker))
    body += _tdf_field_group("UROP", bytes(urop))
    return bytes(body)


def _blaze_ping_payload() -> bytes:
    # Different Blaze generations use STIM or TIME. Supplying both is harmless
    # for tag-addressed TDF readers and maximizes compatibility with FIFA 20.
    t = now_s()
    return _tdf_field_int("STIM", t) + _tdf_field_int("TIME", t)


def _blaze_empty_config_payload() -> bytes:
    return _tdf_field_map_str_str("CONF", [])


@dataclass
class Fire2Packet:
    payload_size: int
    metadata_size: int
    component: int
    command: int
    msg_num: int
    msg_type: int
    metadata: bytes
    payload: bytes
    raw: bytes


def _fire2_try_parse(buf: bytearray) -> Fire2Packet | None:
    if len(buf) < 16:
        return None
    payload_size = int.from_bytes(buf[0:4], "big")
    metadata_size = int.from_bytes(buf[4:6], "big")
    total = 16 + metadata_size + payload_size
    if payload_size > 16 * 1024 * 1024 or metadata_size > 1024 * 1024:
        raise ValueError(f"unreasonable Fire2 sizes payload={payload_size} metadata={metadata_size}")
    if len(buf) < total:
        return None
    raw = bytes(buf[:total])
    del buf[:total]
    component = int.from_bytes(raw[6:8], "big")
    command = int.from_bytes(raw[8:10], "big")
    msg_num = int.from_bytes(raw[10:13], "big")
    msg_type = raw[13] >> 5
    metadata = raw[16:16 + metadata_size]
    payload = raw[16 + metadata_size:]
    return Fire2Packet(payload_size, metadata_size, component, command, msg_num, msg_type, metadata, payload, raw)


def _fire2_build(component: int, command: int, msg_num: int, msg_type: int, payload: bytes = b"", metadata: bytes = b"") -> bytes:
    out = bytearray()
    out += len(payload).to_bytes(4, "big")
    out += len(metadata).to_bytes(2, "big")
    out += int(component).to_bytes(2, "big")
    out += int(command).to_bytes(2, "big")
    out += (int(msg_num) & 0xFFFFFF).to_bytes(3, "big")
    out += bytes([(int(msg_type) & 0x07) << 5, 0, 0])
    out += metadata
    out += payload
    return bytes(out)


def _save_fire2(packet: Fire2Packet, peer: tuple[str, int], kind: str = "rx") -> Path:
    stamp = f"{int(time.time()*1000)}-{peer[0].replace(':','_')}-{peer[1]}"
    path = LOGS / f"fire2-{kind}-c{packet.component}-k{packet.command}-m{packet.msg_num}-{stamp}.bin"
    path.write_bytes(packet.raw)
    path.with_suffix(".hex.txt").write_text(packet.raw.hex(" "), encoding="utf-8")
    return path


def _blaze_fifa35_auth_bootstrap_payload() -> bytes:
    """Exact FIFA 20 c35/k10 login-response shape learned from the V3 trace."""
    persona = int(CFG["persona_id"])
    nucleus = int(CFG["nucleus_id"])

    pdtl = bytearray()
    pdtl += _tdf_field_str("DSNM", str(CFG["display_name"]))
    pdtl += _tdf_field_int("PID", persona)
    pdtl += _tdf_field_int("PLAT", 4)

    sess = bytearray()
    sess += _tdf_field_int("1CON", 0)
    sess += _tdf_field_int("BUID", nucleus)
    sess += _tdf_field_int("FRST", 0)
    control = _control_session()
    session_key = str(control.get("accessToken")) if control else STATE.token()
    sess += _tdf_field_str("KEY", session_key)
    sess += _tdf_field_int("LLOG", now_s())
    sess += _tdf_field_str("MAIL", "local@localhost")
    sess += _tdf_field_group("PDTL", bytes(pdtl))
    sess += _tdf_field_int("UID", nucleus)

    body = bytearray()
    body += _tdf_field_int("ANON", 0)
    body += _tdf_field_int("NTOS", 0)
    body += _tdf_field_group("SESS", bytes(sess))
    body += _tdf_field_int("SPAM", 1)
    body += _tdf_field_int("UNDR", 0)
    return bytes(body)


def _blaze_qos_data_group() -> bytes:
    body = bytearray()
    body += _tdf_field_int("DBPS", 0)
    body += _tdf_field_int("NATT", 0)
    body += _tdf_field_int("UBPS", 0)
    return bytes(body)


def _blaze_extended_data_minimal() -> bytes:
    """Minimal UserSessionExtendedData used in the captured login notifications."""
    body = bytearray()
    body += _tdf_field_int("HWFG", 0)
    body += _tdf_field_group("QDAT", _blaze_qos_data_group())
    body += _tdf_field_int("UATT", 0)
    return bytes(body)


def _blaze_notify_user_added() -> bytes:
    """UserSessions notification 2 (UserAdded), rebuilt with local identity."""
    persona = int(CFG["persona_id"])
    user = bytearray()
    user += _tdf_field_int("AID", persona)
    user += _tdf_field_int("ALOC", 1701729619)
    user += _tdf_field_blob("EXBB", b"")
    user += _tdf_field_int("EXID", 0)
    user += _tdf_field_int("ID", persona)
    user += _tdf_field_str("NAME", str(CFG["display_name"]))
    # These FIFA-specific additions are present in the successful V3 packet.
    user += _tdf_field_str("NASP", "cem_ea_id")
    user += _tdf_field_int("ORIG", persona)
    user += _tdf_field_int("PIDI", 0)

    body = bytearray()
    body += _tdf_field_group("DATA", _blaze_extended_data_minimal())
    body += _tdf_field_group("USER", bytes(user))
    return bytes(body)


def _blaze_notify_login_session() -> bytes:
    """FIFA 20's UserSessions notification 8 captured immediately after c35/k10."""
    persona = int(CFG["persona_id"])
    body = bytearray()
    body += _tdf_field_int("BUID", persona)
    body += _tdf_field_object_id("CGID", 30722, 2, persona)
    body += _tdf_field_str("DSNM", str(CFG["display_name"]))
    body += _tdf_field_str("KEY", str(_control_session().get("accessToken")) if _control_session() else STATE.token())
    body += _tdf_field_int("LAST", now_s())
    body += _tdf_field_str("MAIL", "local@localhost")
    body += _tdf_field_str("NASP", "cem_ea_id")
    body += _tdf_field_int("PID", persona)
    body += _tdf_field_int("PLAT", 0)
    body += _tdf_field_int("UID", persona)
    body += _tdf_field_int("USTP", 0)
    body += _tdf_field_int("XREF", 0)
    return bytes(body)


def _blaze_notify_extended_data_update() -> bytes:
    """UserSessions notification 1, the small login-time extended-data update."""
    body = bytearray()
    body += _tdf_field_group("DATA", _blaze_extended_data_minimal())
    body += _tdf_field_int("USID", int(CFG["persona_id"]))
    return bytes(body)


def _blaze_notify_user_updated() -> bytes:
    """UserSessions notification 5 (UserUpdated)."""
    body = bytearray()
    body += _tdf_field_int("FLGS", 3)
    body += _tdf_field_int("ID", int(CFG["persona_id"]))
    return bytes(body)


def _blaze_login_notifications() -> list[tuple[int, int, bytes, str]]:
    # Exact order observed between the c35/k10 reply and Util.PostAuth.
    return [
        (30722, 2, _blaze_notify_user_added(), "UserSessions.UserAdded"),
        (30722, 8, _blaze_notify_login_session(), "UserSessions.LoginSession"),
        (30722, 1, _blaze_notify_extended_data_update(), "UserSessions.ExtendedDataUpdate"),
        (30722, 5, _blaze_notify_user_updated(), "UserSessions.UserUpdated"),
    ]


# SCMP/Season Objectives is cached by the native FUT frontend for the current
# authenticated session.  Match settlement happens on FUT HTTP while Blaze is
# a separate long-lived socket.  Queue a small native UserSessions invalidation
# here and let the Blaze handler itself send it on a later heartbeat/request.
# This avoids cross-thread writes to the same socket and mirrors asynchronous
# server notifications more closely than attaching extra JSON to /user or /hub.
_scmp_push_lock = threading.RLock()
_scmp_push_revision = 0
_scmp_push_remaining = 0
_scmp_push_next_at = 0.0


def _schedule_scmp_live_refresh(revision: int) -> None:
    global _scmp_push_revision, _scmp_push_remaining, _scmp_push_next_at
    with _scmp_push_lock:
        _scmp_push_revision = max(_scmp_push_revision, int(revision))
        # Three pulses cover the native post-match transition.  They are sent
        # only from the Blaze handler thread, on normal client traffic.
        _scmp_push_remaining = 3
        _scmp_push_next_at = time.monotonic() + 0.60
    log.info("[SCMP-PUSH] queued objective refresh revision=%d pulses=3", int(revision))


def _maybe_send_scmp_live_refresh(sock: socket.socket) -> None:
    global _scmp_push_remaining, _scmp_push_next_at
    with _scmp_push_lock:
        if _scmp_push_remaining <= 0 or time.monotonic() < _scmp_push_next_at:
            return
        revision = _scmp_push_revision
        attempt = 4 - _scmp_push_remaining
        _scmp_push_remaining -= 1
        _scmp_push_next_at = time.monotonic() + 0.90

    # 1.0.26 used only notifications 1 + 5.  The tester trace showed all
    # three pulses arriving while the native SCMP campaign stayed stale.
    # On the first pulse, also re-announce LoginSession (notification 8).
    # It is the same authenticated persona/session key: this is not a fake
    # logout/login, does not rotate ownership/auth, and does not start a new
    # FIFA process.  It simply mirrors the retail session event that precedes
    # FUT's startup service refresh.  Later pulses remain lightweight.
    notifications: list[tuple[int, int, bytes, str]] = []
    if attempt == 1:
        notifications.append(
            (30722, 8, _blaze_notify_login_session(),
             "UserSessions.LoginSessionRefresh")
        )
    notifications.extend((
        (30722, 1, _blaze_notify_extended_data_update(),
         "UserSessions.UserSessionExtendedDataUpdate"),
        (30722, 5, _blaze_notify_user_updated(), "UserSessions.UserUpdated"),
    ))
    for component, command, payload, label in notifications:
        sock.sendall(_fire2_build(component, command, 0, 2, payload))
        log.info(
            "[SCMP-PUSH] FIRE2 TX NOTIFY %s c=%d cmd=%d payload=%d "
            "revision=%d attempt=%d",
            label, component, command, len(payload), revision, attempt,
        )


def _blaze_osdk_core_config() -> list[tuple[str, str]]:
    fut = int(CFG["fut_port"])
    easw = int(CFG["easw_port"])
    local_host = str(CFG.get("local_host", "127.0.0.1"))
    fut_base = f"http://{local_host}:{fut}/ut/"
    # Ported from the working FIFA 21 EA-App mode. EA App owns the legitimate
    # Origin/Nucleus identity session; Blaze and FUT remain local.
    log.info("[FIFA21-PORT] OSDK EA-App mode: AUTH_TYPE=NUCLEUS OSDK_AUTH_REQUIRED=1 ORIGIN_LOGIN_ENABLED=1")
    return [
        # Core OSDK/online bootstrap. The Spring18-era client expects more
        # than a single FUT URL before it can establish its local online state.
        ("AUTH_TYPE", "NUCLEUS"),
        ("CONTENT_URL", f"http://{local_host}:{easw}/content"),
        ("CTL_UPDATE_URL", f"http://{local_host}:{easw}/ctl"),
        ("FIFA_POW_CONTENT_SERVER_URL", f"http://{local_host}:{fut}/"),
        ("FIFA_POW_NUCLEUS_PROXY_URL", f"http://{local_host}:{fut}/"),
        ("FIFA_POW_URL", f"http://{local_host}:{fut}/"),
        ("FUT_URI", fut_base),
        ("FUT_RS4_BASE_URL", fut_base),
        ("FUTBOOTCFGFILE_URL", fut_base),
        ("FUT/ENABLED", "1"),
        ("FUTDYNAMICMESSAGES_CUSTOMURL", f"{local_host}:{fut}"),
        ("FUTDYNAMICMESSAGES_URL_BASE", f"http://{local_host}:{fut}"),
        ("NUCLEUS_LOGIN_ENABLED", "1"),
        ("nucleusHost", local_host),
        ("nucleusPort", str(fut)),
        ("nucleusPortSSL", str(fut)),
        ("nucleusBaseUri", "/"),
        ("environment", "prod"),
        ("useNucleusSSL", "0"),
        ("ONLINE/FUTDYNAMICMESSAGES_CUSTOMURL", f"{local_host}:{fut}"),
        ("ONLINE/FUTDYNAMICMESSAGES_TUTORIAL_MSG_URL", f"http://{local_host}:{fut}/"),
        ("ONLINE/SERVER_RS4", f"http://{local_host}:{fut}"),
        ("FIFA_RS4_URL", f"http://{local_host}:{fut}"),
        ("FIFA_RS4_TIMEOUT", "30"),
        ("ORIGIN_LOGIN_ENABLED", "1"),
        ("OSDK_AUTH_REQUIRED", "1"),
        ("OSDK_ONLINE_ENABLED", "1"),
        ("OSDK_SERVER_VERSION", "3.15.08.0"),
        ("CLIENT_TIMEOUT", "90"),
        ("REQUEST_TIMEOUT", "80"),
        ("AUTOLOGIN", "0"),
        ("ALLOW_OFFLINE", "1"),
        ("EMAIL_OPT_IN", "0"),
        ("EASW/ENABLED", "1"),
        ("OSDK_EASW_ALLOWED_LOCALES", "en_US,en_GB,en_US.UTF-8,enUS,enGB,fr_FR,de_DE,es_ES,it_IT,pt_BR,nl_NL"),
        ("OSDK_EASW_CONNECT_RETRY_PERIOD", "30"),
        ("OSDK_EASW_AUTH_URL", f"http://{local_host}:{easw}/easw/auth"),
        ("OSDK_EASW_EVENT_URL", f"http://{local_host}:{easw}/easw/event"),
        ("OSDK_EASW_MEDIA_URL", f"http://{local_host}:{easw}/easw/media"),
        ("OSDK_EASW_REQ_URL", f"http://{local_host}:{easw}/easw/req"),
        ("ROSTER_URL", f"http://{local_host}:{easw}/roster"),
        ("ROSTERUPDATE_URL", f"http://{local_host}:{easw}/roster"),
        ("ROUTINGCFGFILE_URL", f"http://{local_host}:{easw}/routing"),
        ("USE_TOKEN_AUTH", "1"),
        # FIX21: explicit hand-off targets so client continues past accountinfo
        ("FUT_AUTH_URL", f"http://{local_host}:{fut}/ut/auth"),
        ("FUT_HUB_URL", f"http://{local_host}:{fut}/ut/game/fifa20/hub"),
        ("UT_AUTH_URL", f"http://{local_host}:{fut}/ut/auth"),
        ("UT_HUB_URL", f"http://{local_host}:{fut}/ut/game/fifa20/hub"),
        ("FUT_OFFLINE_MODE", "0"),
        ("UTAS_BASE_URL", f"http://{local_host}:{fut}/ut/"),
        ("UTAS_AUTH_URL", f"http://{local_host}:{fut}/ut/auth"),
    ]


def _blaze_fetch_config_payload(cfid: str) -> bytes:
    if cfid == "OSDK_CORE":
        log.info("[FUT-HANDOFF] OSDK_CORE advertises FUT_RS4_BASE_URL=%s", dict(_blaze_osdk_core_config()).get("FUT_RS4_BASE_URL"))
    items = _blaze_osdk_core_config() if cfid == "OSDK_CORE" else []
    body = bytearray()
    body += _tdf_field_map_str_str("CONF", items)
    body += _tdf_field_str("ID", cfid)
    body += _tdf_field_str("ITYP", "")
    body += _tdf_field_str("TOKN", "")
    return bytes(body)


def _blaze_telemetry_payload() -> bytes:
    body = bytearray()
    body += _tdf_field_str("ADRS", "127.0.0.1")
    body += _tdf_field_int("ANON", 0)
    body += _tdf_field_str("DISA", "1")
    body += _tdf_field_str("FILT", "")
    body += _tdf_field_int("LOC", 1701729619)
    body += _tdf_field_str("NOOK", "")
    body += _tdf_field_int("PORT", 9988)
    body += _tdf_field_int("SDLY", 0)
    body += _tdf_field_str("SESS", "")
    body += _tdf_field_str("SKEY", "")
    body += _tdf_field_int("SPCT", 0)
    body += _tdf_field_str("STIM", "")
    return bytes(body)


def _blaze_fifa_postauth_payload() -> bytes:
    pss = bytearray()
    pss += _tdf_field_int("PORT", 0)
    pss += _tdf_field_int("RPRT", 0)
    pss += _tdf_field_int("TIID", 0)

    tick = bytearray()
    tick += _tdf_field_str("ADRS", "127.0.0.1")
    tick += _tdf_field_int("PORT", 8999)
    tick += _tdf_field_str("SKEY", "")

    urop = bytearray()
    urop += _tdf_field_int("TMOP", 0)
    urop += _tdf_field_int("UID", int(CFG["persona_id"]))

    body = bytearray()
    body += _tdf_field_group("PSS", bytes(pss))
    body += _tdf_field_group("TELE", _blaze_telemetry_payload())
    body += _tdf_field_group("TICK", bytes(tick))
    body += _tdf_field_group("UROP", bytes(urop))
    return bytes(body)


def _blaze_entitlements_fifa20_payload() -> bytes:
    groups: list[bytes] = []
    entitlements = STATE.get("entitlements", [])
    if not isinstance(entitlements, list) or not entitlements:
        entitlements = [
            {"id": 1, "grantName": str(CFG["game_sku"]), "tag": "ONLINE_ACCESS", "status": "ACTIVE"},
            {"id": 2, "grantName": str(CFG["fut_sku"]), "tag": "FUT_ACCESS", "status": "ACTIVE"},
        ]
    for idx, model in enumerate(entitlements, start=1):
        model = model if isinstance(model, dict) else {}
        grant_name = str(model.get("grantName") or model.get("name") or CFG["game_sku"])
        tag = str(model.get("tag") or "ONLINE_ACCESS")
        ent = bytearray()
        ent += _tdf_field_str("DEVI", "")
        ent += _tdf_field_str("GDAY", "")
        ent += _tdf_field_str("GNAM", grant_name)
        ent += _tdf_field_int("ID", int(model.get("id", idx) or idx))
        ent += _tdf_field_int("ISCO", 0)
        ent += _tdf_field_int("PID", int(CFG["persona_id"]))
        ent += _tdf_field_str("PJID", "")
        ent += _tdf_field_int("PRCA", 0)
        ent += _tdf_field_str("PRID", grant_name)
        ent += _tdf_field_int("STAT", 1)
        ent += _tdf_field_int("STRC", 0)
        ent += _tdf_field_str("TAG", tag)
        ent += _tdf_field_str("TDAY", "")
        ent += _tdf_field_int("TYPE", 1)
        ent += _tdf_field_int("UCNT", 0)
        ent += _tdf_field_int("VER", 0)
        groups.append(bytes(ent))
    return _tdf_field_list_groups("NLST", groups)


def _blaze_game_report_result_payload(request: bytes) -> bytes:
    """Build the asynchronous success result FIFA waits for after full time.

    SubmitOfflineGameReport itself has an empty RPC response.  The Blaze
    GameReporting contract completes that operation with ResultNotification
    (component 28, notification 0x72).  Without it FIFA waits for its roughly
    45-second result timeout while all players stand on the pitch.
    """
    reporting_id = _tdf_get_int(request, "GRID") or 0
    history_id = reporting_id or max(1, int(time.time() * 1000))
    body = bytearray()
    body += _tdf_field_int("EROR", 0)
    body += _tdf_field_int("FNL", 1)
    body += _tdf_field_int("GHID", history_id)
    body += _tdf_field_int("GRID", reporting_id)
    return bytes(body)


def _fire2_local_response(packet: Fire2Packet) -> tuple[bytes, str]:
    c, k = packet.component, packet.command

    # Util component - these shapes now mirror the successful FIFA 20 PC trace.
    if (c, k) == (9, 7):
        FLOW.mark("BLAZE_PREAUTH", "Blaze", f"component=0x{c:X} command=0x{k:X}", STATE.sid())
        return _blaze_preauth_payload(), "Util.PreAuth"
    if (c, k) == (9, 2):
        return _tdf_field_int("STIM", now_s()), "Util.Ping"
    if (c, k) == (9, 1):
        cfid = _tdf_get_string(packet.payload, "CFID") or ""
        FLOW.mark("CONFIG", "Blaze", f"FetchClientConfig {cfid}", STATE.sid())
        log.info("Util.FetchClientConfig CFID=%s", cfid)
        return _blaze_fetch_config_payload(cfid), f"Util.FetchClientConfig({cfid})"
    if (c, k) == (9, 8):
        FLOW.mark("SESSION", "Blaze", "Util.PostAuth", STATE.sid())
        return _blaze_fifa_postauth_payload(), "Util.PostAuth"
    if (c, k) == (9, 5):
        return _blaze_telemetry_payload(), "Util.GetTelemetryServer"
    if (c, k) == (9, 12):
        return _tdf_field_map_str_str("SMAP", [("FirstTimeFlag", "0")]), "Util.GetUserOptions"
    if (c, k) == (9, 10):
        key = _tdf_get_string(packet.payload, "KEY") or ""
        value = "0" if key == "FirstTimeFlag" else ""
        return _tdf_field_str("DATA", value) + _tdf_field_str("KEY", key), f"Util.GetUserOption({key})"
    if (c, k) == (9, 20):
        # Used by the native Regular/Concept Squad naming dialog immediately
        # after Apply. The request and response are both FilterUserTextResponse.
        values = _tdf_get_strings(packet.payload, "UTXT")
        FLOW.mark("TEXT_FILTER", "Blaze", f"approved={len(values)}", STATE.sid())
        return _blaze_filter_for_profanity_payload(packet.payload), "Util.FilterForProfanity"
    if (c, k) in ((9, 22), (9, 23), (9, 28)):
        # cmd 23 (0x17) observed post-PostAuth with ACTV tag (client activity /
        # presence ping). Empty success reply is enough to keep the session.
        return b"", "Util.ClientState/Metrics"

    # Single-player FUT matches submit their final stats through Blaze's
    # offline-report RPC.  The direct response is intentionally empty; the
    # client advances only after the matching ResultNotification sent by the
    # connection loop below.
    if (c, k) == (28, 2):
        return b"", "GameReporting.SubmitOfflineGameReport"

    # Component 10 command 5 is a subscription-state exchange.  An empty body
    # causes the client to retry roughly 30 times per second and starves the FUT
    # bootstrap.  Echo the requested state in the expected RSUB integer field.
    if (c, k) == (10, 5):
        subscribed = _tdf_get_int(packet.payload, "RSUB")
        return _tdf_field_int("RSUB", 1 if subscribed else 0), "Subscription.SetState"

    # FIFA 20 title-specific login bridge. The V3 capture proved this is not
    # an AUTH/EXTB/EXTI echo: the response is a FullLogin-style session object.
    if (c, k) == (35, 10):
        FLOW.mark("SESSION", "Blaze", "FIFA35.LoginSession", STATE.sid())
        log.info(
            "FIFA35 login request: payload=%s; replying with captured-compatible local session",
            packet.payload.hex(),
        )
        return _blaze_fifa35_auth_bootstrap_payload(), "FIFA35.LoginSession"

    # UserSessions request handlers. cmd 8 as a *request* (not the notify) is
    # the client asking for current session data after our forced login.
    # Returning empty left the client without a usable session → stalled after
    # accountinfo. Reply with the same LoginSession object we notify with.
    if (c, k) == (30722, 8):
        return _blaze_notify_login_session(), "UserSessions.GetLoginSession"
    # cmd 20 is UpdateNetworkInfo / extended-data update from the client.
    # Ack with the minimal extended-data object so the client marks itself online.
    if (c, k) == (30722, 20):
        return _blaze_notify_extended_data_update(), "UserSessions.UpdateNetworkInfo"

    # UserSessions / game bootstrap calls that returned no body in the trace.
    if (c, k) in (
        (21, 10),
        (15, 2),
        (10, 1),
        (25, 6),
        (11, 2600),
        (7, 15), (7, 3),
        (11, 1600),
        (0, 0),
    ):
        return b"", "captured-empty"

    # FIFA ticker/filter support.
    if (c, k) == (2249, 1):
        entry = bytearray()
        entry += _tdf_field_str("ID", "O_TKfilter")
        entry += _tdf_field_int("LOCF", 0)
        entry += _tdf_field_int("TOGG", 0)
        return _tdf_field_list_groups("LSST", [bytes(entry)]), "Ticker.GetSettings"
    if (c, k) == (2249, 2):
        entry = bytearray()
        entry += _tdf_field_str("ID", "O_SG_TCKR")
        # list<string> LSET
        lset = bytearray(_tdf_tag("LSET", _TDF_LIST))
        lset.append(_TDF_STRING)
        lset += _tdf_varint(1)
        lset += _tdf_str_value("O_TKfilter")
        entry += lset
        return _tdf_field_list_groups("LGRP", [bytes(entry)]), "Ticker.GetGroups"

    # Authentication. FIFA 20 asks for ListEntitlements (command 32) after the
    # title-specific login is complete.
    if c == 1 and k == 0x20:
        FLOW.mark("ENTITLEMENT", "Blaze", "Authentication.ListEntitlements", STATE.sid())
        return _blaze_entitlements_fifa20_payload(), "Authentication.ListEntitlements"
    if c == 1 and k in (0x28, 0x32, 0x3C, 0x98, 0x0A):
        FLOW.mark("AUTH", "Blaze", f"Authentication.LocalLogin(0x{k:X})", STATE.sid())
        return _blaze_auth_payload(), f"Authentication.LocalLogin(0x{k:X})"
    if c == 1 and k in (0x1D, 0x30):
        FLOW.mark("ENTITLEMENT", "Blaze", f"Authentication.Entitlements(0x{k:X})", STATE.sid())
        return _blaze_entitlements_fifa20_payload(), f"Authentication.Entitlements(0x{k:X})"
    if c == 1 and k == 0x24:
        return _tdf_field_str("AUTH", STATE.token()), "Authentication.GetAuthToken"
    if c == 1 and k in (0x1E, 0x5A, 0x64, 0x6E):
        return _blaze_auth_payload(), f"Authentication.AccountPersona(0x{k:X})"
    if c == 1 and k == 0x46:
        return b"", "Authentication.Logout"

    return b"", "unknown-empty"


class BlazeOrHttpHandler(socketserver.BaseRequestHandler):
    """Port 44321 local Blaze listener (with legacy HTTP fallback)."""

    HTTP_PREFIXES = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"OPTIONS ", b"HEAD ")

    def handle(self) -> None:
        sock: socket.socket = self.request
        peer = self.client_address
        sock.settimeout(120)
        try:
            first = sock.recv(65536)
        except Exception:
            return
        if not first:
            return
        if first.startswith(self.HTTP_PREFIXES):
            self._http(sock, first)
            return

        FLOW.mark("BLAZE_CONNECTED", "Blaze", f"{peer[0]}:{peer[1]}", STATE.sid())
        buf = bytearray(first)
        while True:
            try:
                packet = _fire2_try_parse(buf)
            except Exception as exc:
                stamp = f"{int(time.time()*1000)}-{peer[1]}"
                raw_path = LOGS / f"blaze44321-unparsed-{stamp}.bin"
                raw_path.write_bytes(bytes(buf))
                log.exception("44321 Fire2 framing failed; saved %s: %s", raw_path.name, exc)
                return

            if packet is None:
                try:
                    more = sock.recv(65536)
                except socket.timeout:
                    log.info("44321 client %s:%s timed out", *peer)
                    return
                except (ConnectionResetError, ConnectionAbortedError):
                    return
                if not more:
                    return
                buf.extend(more)
                continue

            # FIFA 20 emits component 10/command 5 as a very high-frequency
            # subscription heartbeat. It is handled below, but saving and
            # logging every identical frame creates thousands of files and can
            # drown the useful FUT startup trace.
            quiet_subscription = (packet.component, packet.command) == (10, 5)
            if not quiet_subscription:
                path = _save_fire2(packet, peer, "rx")
                log.info(
                    "FIRE2 RX c=%d cmd=%d(0x%X) msg=%d type=%d payload=%d metadata=%d saved=%s",
                    packet.component, packet.command, packet.command, packet.msg_num,
                    packet.msg_type, len(packet.payload), len(packet.metadata), path.name,
                )

            # Fire2 Message=0; Reply=1; Ping=4; PingReply=5.
            if packet.msg_type == 4:
                reply = _fire2_build(packet.component, packet.command, packet.msg_num, 5, b"")
                sock.sendall(reply)
                log.info("FIRE2 TX PingReply msg=%d", packet.msg_num)
                continue
            if packet.msg_type != 0:
                continue

            payload, label = _fire2_local_response(packet)
            reply = _fire2_build(packet.component, packet.command, packet.msg_num, 1, payload)
            try:
                sock.sendall(reply)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            if not quiet_subscription:
                log.info(
                    "FIRE2 TX %s c=%d cmd=%d msg=%d payload=%d",
                    label, packet.component, packet.command, packet.msg_num, len(payload),
                )
                FLOW.log_blaze(packet, label, len(payload), STATE.sid())

            # A settled FUT HTTP match may have advanced Season Objectives.
            # Send the queued native invalidation from this same Blaze thread
            # on ordinary post-match traffic so frames cannot interleave.
            try:
                _maybe_send_scmp_live_refresh(sock)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

            if label == "GameReporting.SubmitOfflineGameReport":
                result_payload = _blaze_game_report_result_payload(packet.payload)
                try:
                    sock.sendall(_fire2_build(28, 0x72, 0, 2, result_payload))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                log.info(
                    "FIRE2 TX NOTIFY GameReporting.Result c=28 cmd=114 payload=%d grid=%d",
                    len(result_payload), _tdf_get_int(packet.payload, "GRID") or 0,
                )

            # Match the proven FIFA 21 ordering: a successful native
            # Authentication.LocalLogin reply is immediately followed by the
            # UserSessions notification sequence. Without these notifications
            # FIFA 20 discards the otherwise-successful login and logs out.
            if label == "Authentication.LocalLogin(0xA)":
                self._natural_login_sent = True
                for ncomp, ncmd, npayload, nlabel in _blaze_login_notifications():
                    try:
                        sock.sendall(_fire2_build(ncomp, ncmd, 0, 2, npayload))
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return
                    log.info(
                        "[FIFA21-PORT] FIRE2 TX NOTIFY %s c=%d cmd=%d payload=%d after native login",
                        nlabel, ncomp, ncmd, len(npayload),
                    )

            # FIX12: retail FIFA20 often does OSDK_CORE → Authentication.Logout
            # and never requests FIFA35.LoginSession itself. FIX11 only pushed
            # the FIFA35 object as a notification; the client ignored it and
            # looped. After the *first* Logout on this connection we now:
            #   1) push FIFA35.LoginSession (notification)
            #   2) push the full UserSessions login notification sequence
            #      (UserAdded / LoginSession / ExtendedDataUpdate / UserUpdated)
            # so the client receives the same post-login state it would after a
            # real c35/k10 request. Subsequent Logouts on the same socket are
            # left alone to avoid notification spam.
            if (
                packet.component == 1
                and packet.command == 0x46
                and label == "Authentication.Logout"
                and bool(CFG.get("force_title_login_after_logout", False))
                and not getattr(self, "_natural_login_sent", False)
                and not getattr(self, "_fix12_title_login_sent", False)
            ):
                self._fix12_title_login_sent = True
                try:
                    FLOW.mark("SESSION", "Blaze", "FIFA35.LoginSession", STATE.sid())
                    fallback = _fire2_build(35, 10, 0, 2, _blaze_fifa35_auth_bootstrap_payload())
                    sock.sendall(fallback)
                    log.warning(
                        "[FIX12] Sent fallback FIFA35.LoginSession after Authentication.Logout msg=%d",
                        packet.msg_num,
                    )
                    for ncomp, ncmd, npayload, nlabel in _blaze_login_notifications():
                        nframe = _fire2_build(ncomp, ncmd, 0, 2, npayload)
                        sock.sendall(nframe)
                        log.info(
                            "[FIX12] FIRE2 TX NOTIFY %s c=%d cmd=%d payload=%d",
                            nlabel, ncomp, ncmd, len(npayload),
                        )
                    # Also push entitlements so the client has ONLINE_ACCESS +
                    # FUT_ACCESS before it decides whether /ut/auth is allowed.
                    ent_payload = _blaze_entitlements_fifa20_payload()
                    ent_frame = _fire2_build(1, 0x20, 0, 2, ent_payload)
                    sock.sendall(ent_frame)
                    log.info(
                        "[FIX14] FIRE2 TX NOTIFY Authentication.ListEntitlements payload=%d",
                        len(ent_payload),
                    )
                    log.warning(
                        "[FIX14] Completed forced title-session + UserSessions + entitlements bootstrap after Logout"
                    )
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

            if label == "FIFA35.LoginSession":
                for ncomp, ncmd, npayload, nlabel in _blaze_login_notifications():
                    nframe = _fire2_build(ncomp, ncmd, 0, 2, npayload)
                    try:
                        sock.sendall(nframe)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return
                    log.info(
                        "FIRE2 TX NOTIFY %s c=%d cmd=%d payload=%d",
                        nlabel, ncomp, ncmd, len(npayload),
                    )

            if label == "unknown-empty":
                scout = _tdf_debug_summary(packet.payload)
                log.warning(
                    "CARDS-SCOUT Unhandled Blaze request c=%d cmd=%d(0x%X) msg=%d payload=%d :: %s",
                    packet.component, packet.command, packet.command, packet.msg_num, len(packet.payload), scout,
                )
                try:
                    scout_path = LOGS / f"cards-scout-c{packet.component}-k{packet.command}-m{packet.msg_num}-{int(time.time()*1000)}.json"
                    scout_path.write_text(json.dumps({
                        "component": packet.component, "command": packet.command, "commandHex": hex(packet.command),
                        "msgNum": packet.msg_num, "payloadHex": packet.payload.hex(), "summary": scout,
                    }, indent=2), encoding="utf-8")
                    log.warning("CARDS-SCOUT saved %s", scout_path.name)
                except Exception:
                    log.exception("CARDS-SCOUT failed to save diagnostic")

    def _http(self, sock: socket.socket, first: bytes) -> None:
        buf = bytearray(first)
        while b"\r\n\r\n" not in buf and len(buf) < 1024 * 1024:
            more = sock.recv(65536)
            if not more:
                break
            buf.extend(more)
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
        req = lines[0].split(" ")
        if len(req) < 2:
            return
        method, path = req[0], req[1]
        hdrs: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip()] = v.strip()
        need = int(hdrs.get("Content-Length", "0") or "0")
        body = bytearray(rest)
        while len(body) < need:
            more = sock.recv(min(65536, need - len(body)))
            if not more:
                break
            body.extend(more)
        status, extra, out = route_fut(method, path, hdrs, bytes(body[:need]))
        reason = "OK" if status == 200 else "Error"
        response_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(out)),
            "Connection": "close",
            "X-UT-SID": STATE.sid(),
            **extra,
        }
        raw = [f"HTTP/1.1 {status} {reason}\r\n"] + [f"{k}: {v}\r\n" for k, v in response_headers.items()] + ["\r\n"]
        sock.sendall("".join(raw).encode("ascii") + out)


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---- Local Origin LSX -----------------------------------------------------
# OriginSDK 9.x/10.x uses a server-first challenge, then AES-128-ECB encrypted
# hex frames. 0.1.1 incorrectly waited for a plaintext client request first,
# which deadlocked the SDK and could make FIFA exit on the language screen.


class OriginRandom:
    """Origin/EA SDK deterministic PRNG used for LSX session-key derivation."""

    def __init__(self, seed: int):
        self.seed = int(seed) & 0xFFFFFFFF

    def set_seed(self, seed: int) -> None:
        self.seed = int(seed) & 0xFFFFFFFF

    def next(self) -> int:
        # Same LCG used by the legacy Origin SDK.
        self.seed = (self.seed * 214013 + 2531011) & 0xFFFFFFFF
        return (self.seed >> 16) & 0x7FFF


class OriginCrypto:
    def __init__(self, seed: int = 0):
        self.key = bytes(range(16))
        self.set_key(seed)

    def set_key(self, seed: int) -> None:
        seed = int(seed) & 0xFFFFFFFF
        if seed == 0:
            self.key = bytes(range(16))
            return
        rng = OriginRandom(7)
        new_seed = (rng.next() + seed) & 0xFFFFFFFF
        rng.set_seed(new_seed)
        self.key = bytes((rng.next() & 0xFF) for _ in range(16))

    def encrypt(self, text: str) -> bytes:
        if Cipher is None:
            raise RuntimeError(f"cryptography package is unavailable: {_crypto_import_error}")
        padder = padding.PKCS7(128).padder()
        raw = padder.update(text.encode("utf-8")) + padder.finalize()
        enc = Cipher(algorithms.AES(self.key), modes.ECB()).encryptor()
        return enc.update(raw) + enc.finalize()

    def decrypt(self, ciphertext: bytes) -> str:
        if Cipher is None:
            raise RuntimeError(f"cryptography package is unavailable: {_crypto_import_error}")
        dec = Cipher(algorithms.AES(self.key), modes.ECB()).decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
        return raw.decode("utf-8")

    def challenge_response(self, challenge: str) -> str:
        response = self.encrypt(challenge).hex()
        b = response.encode("ascii")
        if len(b) < 2:
            raise ValueError("challenge response was too short")
        seed = (b[0] << 8) | b[1]
        self.set_key(seed)
        return response

    def set_session_from_response(self, response: str) -> int:
        b = response.encode("ascii", errors="strict")
        if len(b) < 2:
            raise ValueError("ChallengeResponse.response is too short")
        seed = (b[0] << 8) | b[1]
        self.set_key(seed)
        return seed


def _xml_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _lsx_extract_request(request_xml: str) -> tuple[str, str, ET.Element | None]:
    rid = "0"
    name = "Unknown"
    op: ET.Element | None = None
    try:
        root = ET.fromstring(request_xml)
        req = root.find(".//Request")
        if req is not None:
            rid = req.attrib.get("id", rid)
            children = list(req)
            if children:
                op = children[0]
                name = op.tag.split("}")[-1]
    except Exception:
        pass
    return rid, name, op


def _lsx_response_envelope(rid: str, inner: str, sender: str = "EbisuSDK") -> str:
    return f'<LSX><Response id="{_xml_attr(rid)}" sender="{_xml_attr(sender)}">{inner}</Response></LSX>'


def _lsx_xml_response(request_xml: str) -> str:
    rid, name, op = _lsx_extract_request(request_xml)
    player = str(CFG["display_name"])
    nucleus = str(CFG["nucleus_id"])
    persona = str(CFG["persona_id"])

    if name == "GetConfig":
        services = (
            ("EbisuSDK", "SDK"),
            ("EbisuSDK", "PROFILE"),
            ("XMPP", "PRESENCE"),
            ("XMPP", "FRIENDS"),
            ("Commerce", "COMMERCE"),
            ("EbisuSDK", "RECENTPLAYER"),
            ("EbisuSDK", "IGO"),
            ("EbisuSDK", "MISC"),
            ("EALS", "LOGIN"),
            ("EbisuSDK", "UTILITY"),
            ("XMPP", "XMPP"),
            ("XMPP", "CHAT"),
            ("EbisuSDK", "IGO_EVENT"),
            ("EALS", "EALS_EVENTS"),
            ("EbisuSDK", "LOGIN_EVENT"),
            ("XMPP", "INVITE_EVENT"),
            ("EbisuSDK", "PROFILE_EVENT"),
            ("XMPP", "PRESENCE_EVENT"),
            ("XMPP", "FRIENDS_EVENT"),
            ("Commerce", "COMMERCE_EVENT"),
            ("XMPP", "CHAT_EVENT"),
            ("EbisuSDK", "DOWNLOAD_EVENT"),
            ("EbisuSDK", "PERMISSION"),
            ("EbisuSDK", "RESOURCES"),
            ("EbisuSDK", "BLOCKED_USERS"),
            ("EbisuSDK", "BLOCKED_USER_EVENT"),
            ("EbisuSDK", "GET_USERID"),
            ("EbisuSDK", "ONLINE_STATUS_EVENT"),
            ("EbisuSDK", "ACHIEVEMENT"),
            ("EbisuSDK", "ACHIEVEMENT_EVENT"),
            ("EbisuSDK", "BROADCAST_EVENT"),
            ("PI", "PROGRESSIVE_INSTALLATION"),
            ("PI", "PROGRESSIVE_INSTALLATION_EVENT"),
            ("EbisuSDK", "CONTENT"),
        )
        inner = '<GetConfigResponse>' + ''.join(
            f'<Service Name="{name}" Facility="{facility}" />' for name, facility in services
        ) + '</GetConfigResponse>'
    elif name == "GetSettings":
        inner = ('<GetSettingsResponse Language="en_US" Environment="production" '
                 'IsIGOAvailable="false" IsIGOEnabled="false" '
                 'IsTelemetryEnabled="false" IsManualOffline="false" />')
    elif name == "GetSetting":
        setting = ""
        if op is not None:
            setting_id = op.attrib.get("SettingId", "")
            setting = {
                "LANGUAGE": "en_US",
                "ENVIRONMENT": "production",
                "IS_IGO_AVAILABLE": "false",
                "IS_IGO_ENABLED": "false",
                "IS_TELEMETRY_ENABLED": "false",
                "IS_MANUAL_OFFLINE": "false",
            }.get(setting_id, "")
        inner = f'<GetSettingResponse Setting="{_xml_attr(setting)}" />'
    elif name == "GetInternetConnectedState":
        inner = '<InternetConnectedState connected="1" />'
    elif name == "GetUtcTime":
        utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inner = f'<GetUtcTimeResponse Time="{utc}" />'
    elif name == "GetAuthToken":
        inner = '<AuthToken value="LOCAL-FUT15-ACCESS" />'
    elif name == "GetAuthCode":
        inner = '<AuthCode value="LOCAL-FUT15-AUTH-CODE" />'
    elif name == "GetProfile":
        under_age = "true" if mock_account_is_under_age() else "false"
        inner = (f'<GetProfileResponse UserIndex="0" UserId="{_xml_attr(nucleus)}" '
                 f'PersonaId="{_xml_attr(persona)}" Persona="{_xml_attr(player)}" AvatarId="" '
                 f'Country="PT" IsUnderAge="{under_age}" IsSubscriber="false" '
                 'IsTrialSubscriber="false" SubscriberLevel="0" GeoCountry="PT" '
                 'CommerceCountry="PT" CommerceCurrency="EUR" IsSteamSubscriber="false" />')
    elif name == "GetBlockList":
        inner = '<GetBlockListResponse />'
    elif name == "QueryFriends":
        inner = '<QueryFriendsResponse />'
    elif name == "QueryAreFriends":
        inner = '<QueryAreFriendsResponse />'
    elif name in ("QueryContent",):
        inner = '<QueryContentResponse />'
    elif name in ("QueryEntitlements",):
        inner = '<QueryEntitlementsResponse />'
    elif name == "GetPresence":
        inner = '<GetPresenceResponse />'
    elif name == "QueryPresence":
        inner = '<QueryPresenceResponse />'
    elif name == "GetWalletBalance":
        inner = '<GetWalletBalanceResponse Balance="0" Currency="EUR" />'
    elif name == "GetGameInfo":
        # Legacy OriginSDK/FIFA 20 asks for individual title-state values.
        # Returning an empty GetGameInfoResponse makes the client interpret
        # UPTODATE as false and show the mandatory-update dialog.
        game_info_id = ""
        if op is not None:
            game_info_id = op.attrib.get("GameInfoId", "").upper()
        game_info_values = {
            "UPTODATE": "true",
            "LANGUAGES": "en_US,pt_PT,fr_FR,de_DE,es_ES,it_IT",
            "FREETRIAL": "false",
            "EXPIRATION": "0000-00-00T00:00:00",
            "EXPIRATION_DURATION": "0",
            "INSTALLED_VERSION": "1.0.0.0",
            "INSTALLED_LANGUAGE": "en_US",
            "AVAILABLE_VERSION": "1.0.0.0",
            "DISPLAY_NAME": "EA SPORTS FIFA 20",
            "FULLGAME_PURCHASED": "true",
            "FULLGAME_IS_RELEASED": "true",
            "FULLGAME_RELEASE_DATE": "2014-09-25T00:00:00",
            "MAX_GROUP_SIZE": "22",
            "ENTITLEMENT_SOURCE": "LOCAL",
        }
        game_info = game_info_values.get(game_info_id, "")
        log.info("LSX GetGameInfo %s -> %s", game_info_id or "<missing>", game_info)
        inner = f'<GetGameInfoResponse GameInfo="{_xml_attr(game_info)}" />'
    elif name == "GetAllGameInfo":
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        inner = (f'<GetAllGameInfoResponse UpToDate="true" '
                 'Languages="en_US,pt_PT,fr_FR,de_DE,es_ES,it_IT" FreeTrial="false" '
                 'FullGamePurchased="true" FullGameReleased="true" '
                 'FullGameReleaseDate="2014-09-25T00:00:00" Expiration="" '
                 f'SystemTime="{now}" HasExpiration="false" InstalledVersion="1.0.0.0" '
                 'InstalledLanguage="en_US" AvailableVersion="1.0.0.0" '
                 'DisplayName="EA SPORTS FIFA 20" MaxGroupSize="22" EntitlementSource="LOCAL" />')
    elif name == "RequestLicense":
        # Ported from the working FIFA 21 runtime: License is a required LSX
        # attribute even in offline mode.  Omitting it makes OriginSDK retry,
        # invalidate the license, and terminate FIFA a few seconds later.
        inner = '<RequestLicenseResponse License="" />'
    else:
        # Most setter/notification calls use ErrorSuccess in OriginSDK. Keeping
        # the response explicit also makes unsupported calls obvious in the log.
        inner = '<ErrorSuccess Code="0" Description="Local FUT success" />'
        log.warning("LSX unmodelled request %s; returning generic ErrorSuccess", name)

    return _lsx_response_envelope(rid, inner)


def _recv_nul_frame(sock: socket.socket, buffer: bytearray) -> bytes | None:
    while b"\x00" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buffer.extend(chunk)
    idx = buffer.index(0)
    frame = bytes(buffer[:idx])
    del buffer[: idx + 1]
    return frame


def _send_plain_frame(sock: socket.socket, xml: str) -> None:
    sock.sendall(xml.encode("utf-8") + b"\x00")


def _send_encrypted_frame(sock: socket.socket, crypto: OriginCrypto, xml: str) -> None:
    payload = crypto.encrypt(xml).hex().encode("ascii") + b"\x00"
    sock.sendall(payload)


class LSXHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(30)
        peer = self.client_address
        log.info("Origin LSX client connected from %s:%s", *peer)
        buffer = bytearray()

        if Cipher is None:
            log.error("Origin LSX requires the Python 'cryptography' package: %s", _crypto_import_error)
            return

        # 1) Server must speak first. This is the critical fix over 0.1.1.
        challenge_key = secrets.token_hex(16).upper()
        challenge = (f'<LSX><Event sender="EALS"><Challenge key="{challenge_key}" '
                     'version="3" build="10.6.1.8" /></Event></LSX>')
        try:
            _send_plain_frame(sock, challenge)
            log.info("LSX challenge sent to %s:%s key=%s", *peer, challenge_key)

            # 2) ChallengeResponse is still plaintext/NUL-delimited.
            frame = _recv_nul_frame(sock, buffer)
            if frame is None:
                log.warning("LSX client disconnected before ChallengeResponse")
                return
            text = frame.decode("utf-8", errors="replace")
            log.info("LSX plaintext handshake request: %s", text[:3000])
            rid, name, op = _lsx_extract_request(text)
            if name != "ChallengeResponse" or op is None:
                log.warning("Expected ChallengeResponse, received %s", name)
                return
            response = op.attrib.get("response", "")
            returned_key = op.attrib.get("key", "")
            if len(response) < 2:
                log.warning("LSX ChallengeResponse had no usable response attribute")
                return

            # Exact legacy LSX challenge behavior, matching the open-source
            # Maxima Origin/LSX implementation:
            #   1) ChallengeResponse.response must be AES-128-ECB/PKCS7 of the
            #      server Challenge.key using the primary key 00..0f.
            #   2) ChallengeResponse.key is a *client-generated* MD5-ish nonce.
            #      The server encrypts THAT value with the primary key and returns
            #      it as ChallengeAccepted.response.
            #   3) Protocol version 2 keeps the primary 00..0f key for every
            #      subsequent encrypted LSX frame. Version 3 derives a secondary
            #      key from the first two ASCII bytes of the accept response.
            protocol_version = op.attrib.get("version", "3")
            primary_crypto = OriginCrypto(0)
            expected_response = primary_crypto.encrypt(challenge_key).hex()
            if response.lower() == expected_response.lower():
                log.info("LSX challenge response validated for protocol %s", protocol_version)
            else:
                log.warning("LSX challenge response did not validate; expected=%s got=%s", expected_response, response)
                return

            if not returned_key:
                log.warning("LSX ChallengeResponse is missing the client key attribute")
                return
            log.info("LSX client key attribute: %s", returned_key)

            # Server-side mutual challenge response. This was the missing piece in
            # 0.1.4/0.1.5: echoing the client's response is not valid for FIFA 20.
            accept_response = primary_crypto.encrypt(returned_key).hex()

            if protocol_version == "2":
                seed = 0
                crypto = OriginCrypto(0)
                log.info("LSX protocol 2 retaining primary OriginSDK AES key")
            elif protocol_version == "3":
                rb = accept_response.encode("ascii")
                seed = (rb[0] << 8) | rb[1]
                crypto = OriginCrypto(seed)
                log.info("LSX protocol 3 using ChallengeAccepted-derived session key (seed=%d)", seed)
            else:
                log.warning("Unsupported LSX ChallengeResponse version %s", protocol_version)
                return

            accepted = _lsx_response_envelope(
                rid,
                f'<ChallengeAccepted response="{_xml_attr(accept_response)}" />',
                sender="EALS",
            )
            _send_plain_frame(sock, accepted)
            log.info("LSX challenge accepted for %s:%s; protocol=%s session seed=%d", *peer, protocol_version, seed)

            # EA Desktop/Origin also exposes current login state as an LSX event.
            # FIFA 20 checks this before it attempts Blaze authentication.
            login_event = (
                '<LSX><Event sender="EbisuSDK">'
                '<Login IsLoggedIn="true" UserIndex="0" LoginReasonCode="ALREADY_ONLINE" />'
                '</Event></LSX>'
            )
            try:
                _send_encrypted_frame(sock, crypto, login_event)
                log.info("LSX local signed-in Login event sent to %s:%s", *peer)
            except Exception:
                log.exception("Unable to send LSX Login event to %s:%s", *peer)

            # 3) Subsequent LSX messages are ASCII hex of AES-128-ECB/PKCS7 XML.
            sock.settimeout(120)
            while True:
                frame = _recv_nul_frame(sock, buffer)
                if frame is None:
                    break
                if not frame:
                    continue
                try:
                    encrypted = bytes.fromhex(frame.decode("ascii"))
                    request_xml = crypto.decrypt(encrypted)
                except Exception as exc:
                    stamp = f"{int(time.time()*1000)}-{peer[1]}"
                    (LOGS / f"lsx-decode-fail-{stamp}.bin").write_bytes(frame)
                    log.exception("LSX encrypted frame could not be decoded/decrypted: %s", exc)
                    break

                log.info("LSX decrypted request: %s", request_xml[:6000])
                response_xml = _lsx_xml_response(request_xml)
                log.info("LSX encrypted response: %s", response_xml[:6000])
                _send_encrypted_frame(sock, crypto, response_xml)

        except socket.timeout:
            log.info("Origin LSX client %s:%s timed out/closed", *peer)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            log.info("Origin LSX client %s:%s disconnected", *peer)
        except Exception:
            log.exception("Origin LSX handler failed for %s:%s", *peer)

def start_server_thread(server: Any, name: str) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    t.start()
    return t


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--host", default=str(CFG.get("bind_host", "127.0.0.1")))
    args = parser.parse_args()

    host = args.host
    servers = []
    active = {}

    def required_http(key, handler, label):
        port = int(CFG[key])
        try:
            srv = ThreadingHTTPServer((host, port), handler)
            servers.append(srv)
            active[label] = (srv, port)
            log.info("Bound REQUIRED %s on %s:%d", label, host, port)
            return srv
        except OSError as e:
            log.error("FAILED REQUIRED %s on %s:%d: %s", label, host, port, e)
            return None

    def required_tcp(key, handler, label):
        port = int(CFG[key])
        try:
            srv = ThreadingTCPServer((host, port), handler)
            servers.append(srv)
            active[label] = (srv, port)
            log.info("Bound REQUIRED %s on %s:%d", label, host, port)
            return srv
        except OSError as e:
            log.error("FAILED REQUIRED %s on %s:%d: %s", label, host, port, e)
            return None

    def optional_http(key, handler, label):
        port = int(CFG[key])
        try:
            srv = ThreadingHTTPServer((host, port), handler)
            servers.append(srv)
            active[label] = (srv, port)
            log.info("Bound optional %s on %s:%d", label, host, port)
            return srv
        except OSError as e:
            log.warning("Skipping optional %s on %s:%d: %s", label, host, port, e)
            return None

    def optional_tcp(key, handler, label):
        port = int(CFG[key])
        try:
            srv = ThreadingTCPServer((host, port), handler)
            servers.append(srv)
            active[label] = (srv, port)
            log.info("Bound optional %s on %s:%d", label, host, port)
            return srv
        except OSError as e:
            log.warning("Skipping optional %s on %s:%d: %s", label, host, port, e)
            return None

    # FIFA 20 path: Blaze and FUT are required. Older FIFA15 compatibility
    # listeners are optional so EA App/Origin-owned ports do not abort startup.
    fut = required_http("fut_port", FutHandler, "FUT")
    blaze = required_tcp("blaze_port", BlazeOrHttpHandler, "Blaze")
    if fut is None or blaze is None:
        log.error("FIFA 20 cannot continue until the REQUIRED port above is free.")
        log.error("Run CHECK_PORTS.cmd and send the output/screenshot.")
        for srv in servers:
            try:
                srv.server_close()
            except Exception:
                pass
        return 2

    legacy_fut = optional_http("legacy_fut_port", FutHandler, "FUT-compat")
    redir = optional_http("redirect_port", RedirectHandler, "Cleartext-redirector-debug")
    qos = optional_http("qos_port", QosHandler, "QoS")
    easw = optional_http("easw_port", EaswHandler, "EASW")
    lsx = optional_tcp("lsx_port", LSXHandler, "Origin-LSX")

    # FIX18/19: HTTPS:443 for utas.* / accounts.ea.com (hosts-pinned to local).
    fut_tls = None
    try:
        import ssl
        cert = Path(str(CFG.get("certificate_path") or (PROJECT_ROOT / "runtime-source" / "certs" / "spring18-cert.pem")))
        key = Path(str(CFG.get("certificate_key_path") or (PROJECT_ROOT / "runtime-source" / "certs" / "spring18-key.pem")))
        if not cert.is_absolute():
            cert = PROJECT_ROOT / cert
        if not key.is_absolute():
            key = PROJECT_ROOT / key
        tls_port = int(CFG.get("fut_tls_port", 443))

        class _TlsFutServer(ThreadingHTTPServer):
            def get_request(self):
                try:
                    return super().get_request()
                except ssl.SSLError as e:
                    log.warning("[FIX19] TLS handshake failed (client rejected cert or protocol): %s", e)
                    raise
                except OSError as e:
                    log.warning("[FIX19] TLS accept error: %s", e)
                    raise

        tls_srv = _TlsFutServer((host, tls_port), FutHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except Exception:
            pass
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except Exception:
            pass
        def _log_fut_tls_sni(_sock, server_name, _context):
            log.warning("[FUT-TLS] ClientHello SNI=%s", server_name or "<none>")
        ctx.set_servername_callback(_log_fut_tls_sni)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        tls_srv.socket = ctx.wrap_socket(tls_srv.socket, server_side=True)
        servers.append(tls_srv)
        active["FUT-TLS"] = (tls_srv, tls_port)
        fut_tls = tls_srv
        log.info("Bound optional FUT-TLS on %s:%d cert=%s", host, tls_port, cert.name)
    except OSError as e:
        log.warning("Skipping optional FUT-TLS on 443: %s (run as Admin if access denied)", e)
    except Exception:
        log.exception("Skipping optional FUT-TLS (cert/ssl setup failed)")

    thread_names = {
        "FUT": "FUT-8080",
        "Blaze": "Blaze-44321",
        "FUT-compat": "FUT-compat",
        "Cleartext-redirector-debug": "Redirector-debug",
        "QoS": "QoS",
        "EASW": "EASW",
        "Origin-LSX": "OriginLSX",
        "FUT-TLS": "FUT-TLS-443",
    }
    for label, (srv, port) in active.items():
        start_server_thread(srv, thread_names.get(label, label))

    print("=" * 70)
    FLOW.mark("LOCAL_SERVICES_READY", "FIFA20", f"Blaze {host}:{CFG['blaze_port']} FUT {host}:{CFG['fut_port']}")
    print(f"{APP_NAME} {VERSION} LOCAL SERVICES READY")
    print(f" REQUIRED Blaze : {host}:{CFG['blaze_port']}  {'OK' if blaze else 'FAILED'}")
    print(f" REQUIRED FUT   : http://{host}:{CFG['fut_port']}/  {'OK' if fut else 'FAILED'}")
    print(f" Optional FUT-TLS: https://{host}:443/  {'OK' if fut_tls else 'SKIPPED'}")
    print(f" Optional LSX   : {host}:{CFG['lsx_port']}  {'OK' if lsx else 'SKIPPED'}")
    print(f" Optional QoS   : http://{host}:{CFG['qos_port']}/  {'OK' if qos else 'SKIPPED'}")
    print(f" Optional EASW  : http://{host}:{CFG['easw_port']}/  {'OK' if easw else 'SKIPPED'}")
    print(f" Optional FUTc  : http://{host}:{CFG['legacy_fut_port']}/  {'OK' if legacy_fut else 'SKIPPED'}")
    print(f" Optional RDBG  : {host}:{CFG['redirect_port']}  {'OK' if redir else 'SKIPPED'}")
    print(f" Local club     : {CFG['club_name']} / {CFG['display_name']} / {STATE.credits()} coins")
    print(f" State/logs     : {RUNTIME_ROOT}")
    print("=" * 70)
    log.info("[FIFA20] Required localhost services ready; FUT READY is not reported until /ut/game/fifa20/hub is reached")
    log.info("[FIX22] Retail hand-off after ACCOUNT is /connect/auth?response_type=code, then POST /ut/auth")
    log.info("[FIX22] After ACCOUNT watch for: [FUT-HANDOFF], /ut/auth, /ut/game/fifa20/hub, or TLS handshake lines")
    if fut_tls:
        log.info("[FIX18] FUT-TLS listening on 443 — post-accountinfo HTTPS auth should appear here if client uses utas.* hosts")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
