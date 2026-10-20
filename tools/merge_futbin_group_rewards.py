"""Merge archived FUTBIN FIFA 20 group rewards into the playable catalogue.

FIFPlay supplies the complete 608-record/1,224-challenge structure but omits
most reward fields.  The bundled end-of-cycle FUTBIN index independently
preserves the visible group reward labels.  This merge is deliberately limited
to those labels; it never derives a reward from squad rating or cost.
"""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "localfut20" / "sbc_catalog_data.json"
FUTBIN = ROOT / "tools" / "sbc_reference" / "futbin_group_index.json"

PACK_IDS = {
    "bronze reward pack": 1001, "silver players pack": 1002,
    "gold players pack": 1003, "mixed players pack": 1004,
    "premium gold pack": 1005, "prime gold players pack": 1006,
    "rare players pack": 1007, "mega pack": 1008,
    "rare mega pack": 1009, "two rare gold players pack": 1010,
    "three common gold players pack": 1011, "two silver players pack": 1012,
    "premium gold players pack": 1013, "premium silver players pack": 1014,
    "jumbo gold pack": 1015, "jumbo premium gold pack": 1016,
    "rare gold pack": 1017, "premium bronze pack": 1018,
    "prime mixed players pack": 1025, "jumbo premium silver pack": 1026,
    "silver pack": 1027, "premium mixed players pack": 1041,
    "rare electrum players pack": 1042, "prime electrum players pack": 1043,
    "rare mixed players pack": 1044, "premium electrum players pack": 1045,
    "three silver players pack": 1046, "four gold common players": 1047,
    "small prime gold players pack": 1048,
    "small prime electrum players pack": 1049,
    "small rare gold players pack": 1050, "two players pack": 1051,
}


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).casefold())


def reward(label: str, count: int) -> dict[str, Any]:
    visible = clean(label)
    normalized = clean(re.sub(r"\bUNTRADEABLE\b", "", visible, flags=re.I)).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    pack_id = next(
        (value for name, value in sorted(PACK_IDS.items(), key=lambda row: len(row[0]), reverse=True)
         if name in normalized),
        0,
    )
    if pack_id:
        return {
            "type": "PACK", "pack_id": pack_id, "count": max(1, int(count)),
            "display_name": visible,
            "source": "Archived FUTBIN FIFA 20 group reward label",
        }
    is_pick = "player pick" in normalized or bool(re.search(r"\bpp\b", normalized))
    if is_pick:
        option = re.search(r"(?:1\s+of\s+|pick\s+)([2-5])", normalized)
        minimum = re.search(r"(\d{2})\+", normalized)
        return {
            "type": "PLAYER_PICK", "pool": "promo", "options": int(option.group(1)) if option else 3,
            "min_rating": int(minimum.group(1)) if minimum else 0,
            "count": max(1, int(count)), "display_name": visible,
            "source": "Archived FUTBIN FIFA 20 group reward label",
        }
    # Guaranteed/random card rewards are packs in the retail UI and reveal one
    # card when opened; they are not safe to turn into a specific footballer.
    if any(word in normalized for word in ("player", "icon", "totw", "totssf", "tots", "party bag")):
        minimum = re.search(r"(\d{2})\+", normalized)
        return {
            "type": "PACK", "pack_id": 1036, "count": max(1, int(count)),
            "min_rating": int(minimum.group(1)) if minimum else 0,
            "display_name": visible,
            "source": "Archived FUTBIN FIFA 20 guaranteed-card reward label",
        }
    return {
        "type": "PACK", "pack_id": 1005, "count": max(1, int(count)),
        "display_name": visible,
        "source": "Archived FUTBIN FIFA 20 reward label (unclassified presentation)",
    }


def main() -> None:
    catalog: list[dict[str, Any]] = json.loads(CATALOG.read_text(encoding="utf-8"))
    index = json.loads(FUTBIN.read_text(encoding="utf-8"))["groups"]
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in index:
        if row.get("groupRewards"):
            candidates[key(row.get("name"))].append(row)

    used: set[int] = set()
    merged = 0
    labels = 0
    for group in catalog:
        # Exact player cards recovered from the FIFA 20 database remain more
        # specific than a FUTBIN text label and must not be replaced.
        existing = group.get("awards") if isinstance(group.get("awards"), list) else []
        if any(str(item.get("type", "")).upper() in ("PLAYER", "PLAYER_PICK") for item in existing):
            continue
        pool = [row for row in candidates.get(key(group.get("name")), [])
                if int(row.get("futbinSetId", -1)) not in used]
        if not pool:
            continue
        description = clean(group.get("description")).casefold()
        chosen = max(
            pool,
            key=lambda row: SequenceMatcher(
                None, description, clean(row.get("description")).casefold()
            ).ratio(),
        )
        used.add(int(chosen["futbinSetId"]))
        awards = [reward(row.get("name", "Reward"), row.get("count", 1))
                  for row in chosen.get("groupRewards", [])]
        if awards:
            group["awards"] = awards
            group["futbin_set_id"] = int(chosen["futbinSetId"])
            group["reward_source"] = "Archived FUTBIN FIFA 20 end-of-cycle index"
            merged += 1
            labels += len(awards)

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Merged {labels} authoritative reward labels into {merged} playable SBC groups")


if __name__ == "__main__":
    main()
