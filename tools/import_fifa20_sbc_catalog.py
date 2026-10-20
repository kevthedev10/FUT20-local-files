"""Normalize the complete FIFPlay FIFA 20 SBC archive for LocalFUT.

The public archive contains 608 index records.  Ten of those records no longer
expose a challenge block (duplicates, kits, or retired special pages), while
the other 598 groups contain the complete 1,224 playable mini-challenges.
Only those playable groups are emitted; every source requirement is retained
verbatim next to its normalized rule for auditing.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT.parents[1] / "sbc_handoff" / "full2" / "fifa20_sbcs.json"
DEFAULT_OUTPUT = ROOT / "localfut20" / "sbc_catalog_data.json"

BASIC_NAMES = {
    "Let's Get Started", "Let's Keep Going", "Let's Move Forward",
    "League and Nation Basics",
}
ADVANCED_NAMES = {
    "Hybrid Leagues", "Hybrid Nations", "League and Nation Hybrid",
    "National Pride",
}
PACK_IDS = {
    "rare mega pack": 1009, "mega pack": 1008,
    "jumbo premium gold pack": 1016, "jumbo gold pack": 1015,
    "rare players pack": 1007, "prime gold players pack": 1006,
    "premium gold players pack": 1013, "premium silver players pack": 1014,
    "gold players pack": 1003, "silver players pack": 1002,
    "mixed players pack": 1004, "premium gold pack": 1005,
    "rare gold pack": 1017, "premium bronze pack": 1018,
    "two rare gold players pack": 1010, "three common gold players pack": 1011,
    "two silver players pack": 1012, "bronze reward pack": 1001,
}


def clean(value: Any) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    # Two legacy archive pages decode UTF-8 C3 bytes as U+FFFD plus a soft
    # hyphen/control code.  Repair the known byte pattern before NFC cleanup.
    text = (text.replace("\u00c3\u00ad", "í").replace("\u00c3\u0081", "Á")
            .replace("\ufffd\xad", "í").replace("\ufffd\x81", "Á").replace("\ufffd", ""))
    return unicodedata.normalize("NFC", " ".join(text.split())).strip()


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).casefold())


def build_entity_maps(players_path: Path) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    players = json.loads(players_path.read_text(encoding="utf-8"))
    nations: dict[str, set[int]] = defaultdict(set)
    leagues: dict[str, set[int]] = defaultdict(set)
    clubs: dict[str, set[int]] = defaultdict(set)
    for meta in players.values():
        for label, value, bucket in (
            (meta.get("nationName"), meta.get("nation"), nations),
            (meta.get("leagueName"), meta.get("leagueId"), leagues),
            (meta.get("teamName"), meta.get("teamid"), clubs),
        ):
            key = compact(label or "")
            if key and int(value or 0) > 0:
                bucket[key].add(int(value))
    aliases = {
        "premierleague": "englishpremierleague1",
        "laliga": "laligasantander",
        "serieatim": "seriea",
        "ligue1": "ligue1conforama",
        "bundesliga": "bundesliga1",
        "mls": "majorleaguesoccer",
    }

    def collapse(bucket: dict[str, set[int]]) -> dict[str, int]:
        result = {key: min(values) for key, values in bucket.items() if len(values) == 1}
        for alias, target in aliases.items():
            if target in result:
                result[alias] = result[target]
        return result

    return collapse(nations), collapse(leagues), collapse(clubs)


def scope_and_value(text: str) -> tuple[str, int | None]:
    lowered = text.casefold()
    scope = "exact"
    if "min." in lowered or "minimum" in lowered:
        scope = "min"
    elif "max." in lowered or "maximum" in lowered:
        scope = "max"
    matches = re.findall(r"\b\d+\b", text)
    return scope, (int(matches[-1]) if matches else None)


def normalized_rule(text: str, entity_maps: tuple[dict[str, int], dict[str, int], dict[str, int]]) -> dict[str, Any]:
    raw = clean(text)
    low = raw.casefold()
    scope, value = scope_and_value(raw)
    nations, leagues, clubs = entity_maps

    def rule(kind: str, val: int, count: int = -1, supported: bool = True) -> dict[str, Any]:
        return {"kind": kind, "value": int(val), "scope": scope,
                "count": int(count), "text": raw, "supported": supported}

    if value is not None:
        if "team chemistry" in low:
            return rule("chemistry", value)
        if "team rating" in low or "overall rating" in low:
            return rule("team_rating", value)
        if "number of players" in low:
            return rule("player_count", value)
        if "rare players" in low:
            return rule("rare_count", 1, value)
        for label, quality in (("bronze players", 1), ("silver players", 2), ("gold players", 3)):
            if label in low:
                return rule("quality", quality, value)
        if "players from the same league" in low or "player from the same league" in low:
            return rule("same_league", value)
        if "players from the same nation" in low or "player from the same nation" in low:
            return rule("same_nation", value)
        if "players from the same club" in low or "player from the same club" in low:
            return rule("same_club", value)
        if "leagues in squad" in low or "different leagues" in low:
            return rule("league_count", value)
        if "nationalities in squad" in low or "nations in squad" in low or "different nations" in low:
            return rule("nation_count", value)
        if "clubs in squad" in low or "different clubs" in low:
            return rule("club_count", value)

        match = re.match(r"Players? from (.+?):\s*(?:Min\.|Max\.|Exactly)?\s*\d+", raw, re.I)
        if match:
            subject = compact(match.group(1))
            for bucket, kind in ((nations, "specific_nation"), (leagues, "specific_league"), (clubs, "specific_club")):
                if subject in bucket:
                    return rule(kind, bucket[subject], value)

        special_words = (
            "totw", "tots", "totssf", "special players", "icon swaps",
            "conmebol libertadores", "conmebol sudamericana", "ucl",
            "uefa champions league", "uefa europa league",
        )
        if any(word in low for word in special_words):
            # The raw source is authoritative.  Multi-rarity constraints cannot
            # be represented by FIFA 20's single PLAYER_RARITY wire enum, so
            # retain them as an auditable server rule instead of lying about a
            # specific card type.
            if "totw" in low and all(word not in low for word in ("tots", "totssf", "moments")):
                return rule("card_type_count", 3, value)
            return rule("special_count", 1, value, supported=False)

    return {"kind": "source_text", "value": 0, "scope": "exact", "count": -1,
            "text": raw, "supported": False}


def category_for(group: dict[str, Any]) -> tuple[int, str]:
    name = clean(group.get("name"))
    low = name.casefold()
    rewards = " ".join(clean(x) for x in group.get("group_rewards", []))
    event = clean(group.get("event")).casefold()
    if name in BASIC_NAMES:
        return 1, "BASIC"
    if name in ADVANCED_NAMES:
        return 2, "ADVANCED"
    if "upgrade" in low or "guaranteed" in low or "party bag" in low:
        return 3, "UPGRADES"
    description = clean(group.get("description")).casefold()
    player_markers = (
        "player reward" in rewards.casefold()
        or low.startswith(("flashback ", "player moments ", "potm ", "moments "))
        or "[loan]" in low or "[premium]" in low
        or "player of the month" in description or "potm " in description
        or "earn his otw" in description or "earn her otw" in description
        or "(untradeable) x 1" in description
        or ("earn" in description and any(marker in description for marker in (
            "flashback", "player moments", "summer heat", "player pick", "special card"
        )))
    )
    if player_markers:
        return 4, "PLAYERS"
    return 5, "LIVE"


@lru_cache(maxsize=1)
def load_players(players_path: Path) -> dict[str, dict[str, Any]]:
    return json.loads(players_path.read_text(encoding="utf-8"))


def player_reward(group: dict[str, Any], players_path: Path) -> dict[str, Any] | None:
    reward_text = " ".join(clean(x) for x in group.get("group_rewards", []))
    name = clean(group.get("name"))
    description = clean(group.get("description"))
    reward_signal = (
        "player reward" in reward_text.casefold()
        or "player of the month" in description.casefold()
        or "potm " in description.casefold()
        or "earn his otw" in description.casefold()
        or "(untradeable) x 1" in description.casefold()
        or ("[loan]" in name.casefold() and "try out the special" in description.casefold())
        or bool(re.search(r"\b(earn|try out).{0,60}(flashback|moments|summer heat|special card)\b", description, re.I))
    )
    if not reward_signal or "player pick" in description.casefold():
        return None
    players = load_players(players_path)
    query = re.sub(r"\[[^]]+\]", "", name)
    query = re.sub(r"^(Flashback|Player Moments|Premium|POTM|Summer Heat|OTW|Scream)\s+", "", query, flags=re.I).strip()
    tokens = {compact(part) for part in query.split() if len(compact(part)) >= 3}
    context = f"{name} {description}".casefold()
    promo_aliases = {
        "summer heat": ("summer heat",), "fut birthday": ("birthday",),
        "futmas": ("futmas",), "shapeshifters": ("shape",),
        "player moments": ("player moments", "moments"), "flashback": ("flashback",),
        "scream": ("scream",), "otw": ("ones to watch", "otw"),
        "player of the month": ("potm", "player of the month"),
        "potm": ("potm", "player of the month"),
        "totssf": ("totssf", "team of the season so far"),
        "tots": ("tots", "team of the season"),
        "headliner": ("headliner",), "future stars": ("future stars",),
    }
    wanted_rarity: tuple[str, ...] = ()
    for marker, aliases in promo_aliases.items():
        if marker in context:
            wanted_rarity = aliases
            break
    candidates: list[tuple[int, int, int, int]] = []
    for rid, meta in players.items():
        full = compact(" ".join(str(meta.get(k, "")) for k in ("name", "firstName", "lastName", "commonName")))
        score = sum(1 for token in tokens if token and token in full)
        if score and score >= max(1, len(tokens) - 1):
            rarity = str(meta.get("rarityName", "")).casefold()
            promo_score = 1 if wanted_rarity and any(alias in rarity for alias in wanted_rarity) else 0
            candidates.append((promo_score, score, int(meta.get("rating", 0)), int(rid)))
    if not candidates:
        return None
    resource_id = max(candidates)[3]
    loan_match = re.search(r"(\d+)-Match Loan", description, re.I)
    loan = int(loan_match.group(1)) if loan_match else (7 if "loan" in name.casefold() else 0)
    return {"type": "PLAYER", "resource_id": resource_id, "count": 1, "loan_games": loan,
            "source": "FIFPlay player reward + local FIFA 20 card database"}


def pack_reward_from_text(*values: Any) -> dict[str, Any] | None:
    source = " ".join(clean(value) for value in values if value)
    low = source.casefold()
    for name, pack_id in sorted(PACK_IDS.items(), key=lambda item: len(item[0]), reverse=True):
        if name in low:
            return {"type": "PACK", "pack_id": pack_id, "count": 1,
                    "source": f"FIFPlay text explicitly names {name.title()}"}
    return None


def make_catalog(source: list[dict[str, Any]], players_path: Path) -> list[dict[str, Any]]:
    entity_maps = build_entity_maps(players_path)
    result: list[dict[str, Any]] = []
    priorities: dict[int, int] = defaultdict(int)
    for group in source:
        challenges = group.get("challenges") or []
        if not challenges:
            continue
        set_id = int(group["sbc_id"])
        category_id, category = category_for(group)
        priorities[category_id] += 1
        group_awards: list[dict[str, Any]] = []
        resolved_player = player_reward(group, players_path)
        if resolved_player:
            group_awards.append(resolved_player)
        elif "player pick" in clean(group.get("description")).casefold():
            group_awards.append({"type": "PLAYER_PICK", "pool": "promo", "options": 2,
                                 "min_rating": 0, "count": 1,
                                 "source": "FIFPlay description explicitly identifies a player pick"})
        elif pack_reward_from_text(group.get("description"), *group.get("group_rewards", [])):
            group_awards.append(pack_reward_from_text(
                group.get("description"), *group.get("group_rewards", [])
            ))
        elif any("pack reward" in clean(x).casefold() for x in group.get("group_rewards", [])):
            group_awards.append({"type": "PACK", "pack_id": 1005, "count": 1,
                                 "source": "FIFPlay identifies a pack reward; exact pack name unavailable"})

        normalized_challenges = []
        for position, challenge in enumerate(challenges, 1):
            rules = [normalized_rule(text, entity_maps) for text in challenge.get("requirements", [])]
            player_rule = next((r for r in rules if r["kind"] == "player_count"), None)
            players = int(player_rule["value"]) if player_rule else 11
            awards: list[dict[str, Any]] = []
            exact_pack = pack_reward_from_text(
                challenge.get("description"), *challenge.get("rewards", [])
            )
            if exact_pack:
                awards.append(exact_pack)
            elif any("pack" in clean(x).casefold() for x in challenge.get("rewards", [])):
                awards.append({"type": "PACK", "pack_id": 1005, "count": 1,
                               "source": "FIFPlay identifies a pack reward; exact pack name unavailable"})
            normalized_challenges.append({
                "challenge_id": set_id * 100 + position,
                "name": clean(challenge.get("name")) or clean(group.get("name")),
                "description": clean(challenge.get("description")),
                "players": max(1, min(11, players)), "formation": "f442",
                "rules": rules, "awards": awards,
                "source_requirements": [clean(x) for x in challenge.get("requirements", [])],
                "source_rewards": [clean(x) for x in challenge.get("rewards", [])],
            })
        result.append({
            "set_id": set_id, "category_id": category_id, "category": category,
            "priority": priorities[category_id], "name": clean(group.get("name")),
            "description": clean(group.get("description")),
            "repeatable": clean(group.get("repeatable")).casefold() == "yes",
            "released": clean(group.get("released")), "expires": clean(group.get("expires")),
            "event": clean(group.get("event")), "awards": group_awards,
            "challenges": normalized_challenges, "source_url": clean(group.get("source_url")),
            "archive_index_id": set_id,
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    players_path = ROOT / "localfut20" / "players.json"
    source = json.loads(args.input.read_text(encoding="utf-8"))
    catalog = make_catalog(source, players_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    challenges = sum(len(group["challenges"]) for group in catalog)
    unmapped = sum(
        1 for group in catalog for challenge in group["challenges"]
        for rule in challenge["rules"] if not rule.get("supported", True)
    )
    print(f"Wrote {len(catalog)} playable SBC groups / {challenges} mini-challenges / {unmapped} audited unmapped rules")


if __name__ == "__main__":
    main()
