"""Build the bundled FIFA 20 TOTW archive from the published squad list.

The generated JSON is the runtime source of truth.  The game never needs
network access; this importer only exists so the provenance and matching
rules remain reproducible.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYERS = ROOT / "localfut20" / "players.json"
OUTPUT = ROOT / "localfut20" / "totw_archive.json"
SOURCE = "https://www.fifauteam.com/fifa-20-totw/"
FIFPLAY = "https://www.fifplay.com/fifa-20-totw/"
REFERENCE = ROOT / "tools" / "totw_reference"

ALIASES = {
    "mathias de ligt": "matthijs de ligt",
    "santi cazrola": "santi cazorla",
    "neymar": "neymar jr",
    "erling haland": "erling braut haland",
    "sven bender": "lars bender",
    "robert skov": "robert skov",
    "raul jimenez": "raul jimenez",
    "jose callejon": "jose callejon",
    "angel di maria": "angel di maria",
    "sergio aguero": "sergio aguero",
    "ilkay gundogan": "ilkay gundogan",
    "nicolo barella": "nicolo barella",
    "caglar soyuncu": "caglar soyuncu",
    "moussa marega": "moussa marega",
    "wahbi khazri": "wahbi khazri",
    "mislav orsic": "mislav orsic",
    "lucas moura": "lucas moura",
    "fred": "fred",
    "allan": "allan",
    "isco": "isco",
    "marquinhos": "marquinhos",
    "willian": "willian",
    "nani": "nani",
    "pizzi": "pizzi",
    "paulinho": "paulinho",
    "fernandinho": "fernandinho",
    "fabinho": "fabinho",
    "casemiro": "casemiro",
    "thiago": "thiago",
}


def norm(value: object) -> str:
    text = str(value or "").lower().translate(str.maketrans({
        "ı": "i", "ø": "o", "ł": "l", "æ": "ae", "œ": "oe",
        "đ": "d", "ð": "d", "þ": "th",
    }))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8")


def published_squads(page: str) -> list[dict]:
    headings = list(re.finditer(r"<center>(TOTW (?:\d{2}|MOMENTS \d))</center>", page))
    squads: list[dict] = []
    for index, heading in enumerate(headings):
        label = heading.group(1)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(page)
        section = page[heading.start():end]
        official = re.search(
            r"<center>OFFICIAL SQUAD</center>.*?"
            r"<div[^>]+class=\"elementor-tab-content[^\"]*\"[^>]*>(.*?)</div>",
            section,
            re.S,
        )
        if not official:
            continue
        rows = []
        for source_line in re.split(r"<br\s*/?>", official.group(1)):
            clean = html.unescape(re.sub(r"<[^>]+>", "", source_line)).strip()
            match = re.search(
                r"(\?\?|\d{2})\s+([A-Z]{1,3}):\s*(.*?)\s*\(([^()]*)\)\s*[a-z]?\s*$",
                clean,
            )
            if not match:
                continue
            rating, position, name, club = match.groups()
            rows.append({
                "rating": 0 if rating == "??" else int(rating),
                "position": position.strip(),
                "name": name.strip(),
                "club": club.strip(),
            })
        if rows:
            squads.append({"sourceLabel": label, "rows": rows})
    return squads


def player_names(player: dict) -> set[str]:
    values = {
        player.get("name"), player.get("commonName"), player.get("lastName"),
        f"{player.get('firstName', '')} {player.get('lastName', '')}",
    }
    return {norm(value) for value in values if norm(value)}


def choose_player(row: dict, players: list[dict], name_index: dict[str, list[dict]], moments: bool) -> dict:
    wanted = norm(row["name"])
    wanted = norm(ALIASES.get(wanted, wanted))
    candidates = list(name_index.get(wanted, ()))
    if not candidates:
        # Some catalogue common names are shorter than the published spelling.
        candidates = [player for player in players if any(
            wanted in alias or alias in wanted for alias in player_names(player)
        ) and len(wanted) >= 5]
    if not candidates:
        raise LookupError(f"No catalogue identity for {row}")

    def score(player: dict) -> tuple:
        rating = int(player.get("rating", 0) or 0)
        rarity = int(player.get("rareflag", 0) or 0)
        expected = {58} if moments else {3, 4}
        return (
            abs(rating - int(row["rating"])),
            0 if rarity in expected else 1,
            0 if norm(player.get("preferredPosition")) == norm(row["position"]) else 1,
            0 if norm(player.get("teamName")) == norm(row["club"]) else 1,
            0 if int(player.get("resourceId", 0) or 0) > 16_000_000 else 1,
            int(player.get("resourceId", 0) or 0),
        )
    return min(candidates, key=score)


def correct_source_row(label: str, row: dict) -> dict:
    row = dict(row)
    if label == "TOTW 20" and norm(row["name"]) == "gianluigi donnarumma":
        row["rating"] = 87  # The archive page has a 97 typo; the card is 87.
    if label == "TOTW MOMENTS 2" and norm(row["name"]) == "radja nainggolan" and row["rating"] == 81:
        row["name"], row["club"] = "Edwin Cardona", "Club Tijuana"
    elif label == "TOTW MOMENTS 2" and norm(row["name"]) == "edwin cardona" and row["rating"] == 87:
        row["name"], row["club"] = "Radja Nainggolan", "Cagliari"
    return row


def fifplay_rows(label: str) -> tuple[list[dict], list[dict]]:
    """Cross-check the official roster and read FIFPlay's explicitly listed XI.

    The saved pages are provenance snapshots, not runtime dependencies. The
    URL's player ID identifies the real person even when its card revision
    differs from the catalogue version (the low 24 bits are the player ID).
    """
    if "MOMENTS" in label:
        number = int(label.rsplit(" ", 1)[-1])
        path = REFERENCE / f"fifplay-moments-{number}.md"
    else:
        number = int(label.rsplit(" ", 1)[-1])
        path = REFERENCE / f"fifplay-totw-{number}.md"
    if not path.exists():
        raise FileNotFoundError(f"Missing independent squad reference: {path}")
    page = path.read_text(encoding="utf-8")
    sections = re.search(
        r"#### Starting 11\s*(.*?)\s*#### Substitutes and Reserves\s*"
        r"(.*?)(?:\n\[Team of the Week\]|\nTOTW Moments squad|\n####|\n##### Related)",
        page, re.S,
    )
    if not sections:
        raise ValueError(f"Missing XI/substitute sections: {path}")
    groups = []
    for section in sections.groups():
        rows = []
        for position, content in re.findall(r"^\s*\*\s+([A-Z]+):\s*(.+)$", section, re.M):
            # Jina's Markdown contains bold links on most pages; the first
            # two weeks have plain bold names without player links.
            linked = re.search(r"\[([^]]+)\]\([^)]*/players/(\d+)/[^)]*\)\*\*\s*\((\d+)\)", content)
            plain = re.search(r"\*\*([^*]+)\*\*\s*\((\d+)\)", content)
            if linked:
                name, resource_id, rating = linked.groups()
            elif plain:
                name, rating = plain.groups()
                resource_id = "0"
            else:
                raise ValueError(f"Unparsed FIFPlay row in {path.name}: {content}")
            rows.append({"position": position, "name": name, "rating": int(rating),
                         "assetId": int(resource_id) & 0xFFFFFF,
                         "linkedResourceId": int(resource_id)})
        groups.append(rows)
    return groups[0], groups[1]


def match_reference(row: dict, records: list[dict], *, used: set[int]) -> dict:
    asset = int(row["assetId"])
    matches = [record for record in records if record["resourceId"] not in used
               and (not asset or record["resourceId"] & 0xFFFFFF == asset)]
    if not asset:
        name = norm(row["name"])
        alias = {"moreno": "gerard moreno", "ignacio monreal eraso": "nacho monreal",
                 "thiago emiliano da silva": "thiago silva", "philippe coutinho": "coutinho",
                 "blanco": "ruben blanco"}
        name = alias.get(name, name)
        matches = [record for record in matches if name in
                   {norm(record["sourceName"]), norm(record["name"])}]
        if not matches:
            # The historical first two FIFPlay pages contain no player links
            # and occasionally have a damaged Unicode accent in the mirror.
            matches = [record for record in records if record["resourceId"] not in used
                       and record["rating"] == row["rating"]
                       and record["position"] == row["position"]]
    if len(matches) != 1:
        raise ValueError(f"Ambiguous/unmatched FIFPlay selection {row}: {matches}")
    return matches[0]


def select_catalog_card(record: dict, row: dict, players: list[dict], *, moments: bool) -> dict:
    """Use the independently checked card, never a closest-rating substitute."""
    asset = record["resourceId"] & 0xFFFFFF
    expected = int(row["rating"])
    position = row["position"]
    matching_identity = [card for card in players
                         if int(card.get("resourceId", 0)) & 0xFFFFFF == asset]
    choices = [card for card in matching_identity
               if int(card.get("resourceId", 0)) & 0xFFFFFF == asset
               and int(card.get("rareflag", 0)) in ({58} if moments else {3, 4})]
    rated_choices = [card for card in choices if int(card.get("rating", 0)) == expected]
    if rated_choices:
        choices = rated_choices
    else:
        # A few published squads point to a different special-card rarity
        # in this project's catalogue. Preserve the exact linked asset and
        # disclosed rating instead of silently replacing the player/card.
        linked = next((card for card in matching_identity
                       if int(card["resourceId"]) == row.get("linkedResourceId", 0)
                       and int(card["rating"]) == expected), None)
        if linked is not None:
            return linked
        # Both public pages occasionally print an impossible rating (e.g.
        # week 16 Kane 92). The linked in-form asset itself resolves to 93.
        linked = next((card for card in choices
                       if int(card["resourceId"]) == row.get("linkedResourceId", 0)), None)
        if linked is not None:
            return linked
        raise LookupError(f"No exact FIFA 20 card for {record['sourceName']}: {row}")
    # FIFPlay sometimes links to a later upgrade even though its article
    # prints the earlier rating. Rating must take precedence over the link.
    choices.sort(key=lambda card: (
        0 if str(card.get("preferredPosition", "")).upper() == position else 1,
        0 if int(card["resourceId"]) == row.get("linkedResourceId", 0) else 1,
        0 if int(card["resourceId"]) == int(record["resourceId"]) else 1,
        int(card["resourceId"]),
    ))
    return choices[0]


FORMATIONS = {
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
}

# These FIFPlay articles list only ten starters and put the omitted card in
# the substitutes block. Independent contemporary XI reports establish the
# first three; Moments 1's only unlisted official member is de Ligt.
MISSING_STARTERS = {
    "TOTW 05": "Ruslan Malinovskyi",
    "TOTW 07": "Christian Pulisic",
    "TOTW 19": "Thiago",
    "TOTW MOMENTS 1": "Matthijs de Ligt",
}

# FIFAUTeam's archive contains headings for weeks 44 and 45 but its
# ``OFFICIAL SQUAD`` blocks are empty.  These two published squads are kept
# here as exact FIFA 20 resource IDs.  Week 44 is independently listed by
# FIFPlay; week 45 is corroborated by the contemporary in-game squad image
# and by the contiguous WeFUT card-release block bundled in players.json.
# This is deliberately explicit: never fill an archive hole with a closest
# name/rating guess.
SUPPLEMENTAL_SQUADS = {
    44: {
        "source": "https://www.fifplay.com/fifa-20-totw-44/",
        "formation": "f343",
        "starters": [
            16966906, 33747054, 33801636, 50531803, 50552822, 33762525,
            33796633, 100843699, 33721829, 17001032, 33763763,
        ],
        "players": [
            16966906, 33747054, 33801636, 50531803, 50552822, 33762525,
            33796633, 100843699, 33721829, 17001032, 33763763, 33718137,
            33780467, 16939496, 16940881, 16977694, 33776933, 33796118,
            50571490, 16986745, 33756320, 33799324, 33763348,
        ],
    },
    45: {
        "source": "https://www.actugaming.net/fifa-20-devoile-la-45e-team-of-the-week-360744/",
        "formation": "f343",
        "starters": [
            67301905, 50541147, 33798902, 16989678, 67319327, 33740993,
            50537761, 33754536, 33770786, 16969845, 67310990,
        ],
        "players": [
            67301905, 50541147, 33798902, 16989678, 67319327, 33740993,
            50537761, 33754536, 33770786, 16969845, 67310990, 16917449,
            16997005, 16993366, 16949330, 33775911, 16986735, 33769875,
            17009494, 17021798, 33794410, 33793356, 16999057,
        ],
    },
}


def supplemental_squad(number: int, definition: dict, player_by_id: dict[int, dict]) -> dict:
    resource_ids = [int(value) for value in definition["players"]]
    starter_ids = [int(value) for value in definition["starters"]]
    if len(resource_ids) != 23 or len(set(resource_ids)) != 23:
        raise ValueError(f"TOTW {number} must contain 23 unique cards")
    if len(starter_ids) != 11 or not set(starter_ids) <= set(resource_ids):
        raise ValueError(f"TOTW {number} has an invalid starting XI")
    records = []
    for resource_id in resource_ids:
        card = player_by_id.get(resource_id)
        if card is None or int(card.get("rareflag", 0)) != 3:
            raise LookupError(f"Missing exact in-form card {resource_id} for TOTW {number}")
        records.append({
            "resourceId": resource_id,
            "name": str(card.get("name") or ""),
            "rating": int(card.get("rating", 0) or 0),
            "position": str(card.get("preferredPosition") or ""),
            "sourceName": str(card.get("name") or ""),
            "sourceRating": int(card.get("rating", 0) or 0),
            "sourcePosition": str(card.get("preferredPosition") or ""),
            "sourceClub": str(card.get("teamName") or ""),
            "catalogTeam": str(card.get("teamName") or ""),
            "rareflag": 3,
        })
    return {
        "id": number,
        "name": f"FIFA 20 Team of the Week {number}",
        "shortName": f"Team of the Week {number}",
        "kind": "TOTW",
        "sourceLabel": f"TOTW {number:02d}",
        "formation": str(definition["formation"]),
        "formationProvenance": "verified from contemporary in-game squad presentation",
        "startingResourceIds": starter_ids,
        "fifplaySource": str(definition["source"]),
        "players": records,
        "resourceIds": starter_ids + [rid for rid in resource_ids if rid not in starter_ids],
    }


def position_cost(card_position: str, slot: str) -> int:
    if card_position == slot:
        return 0
    pair = {"LB": "LWB", "RB": "RWB", "LM": "LW", "RM": "RW",
            "ST": "CF", "CM": "CDM", "CAM": "CM"}
    if pair.get(card_position) == slot or pair.get(slot) == card_position:
        return 1
    return 4 if slot != "GK" and card_position != "GK" else 30


def formation_order(starters: list[dict]) -> tuple[str, list[int]]:
    """Match the published XI to the closest supported native layout.

    FIFPlay does not publish an explicit formation ID, so this is deliberately
    marked as an inference in the JSON. The XI membership is never inferred.
    """
    best = None
    for formation, slots in FORMATIONS.items():
        available = list(range(11))
        order = []
        cost = 0
        for slot in slots:
            chosen = min(available, key=lambda i: (position_cost(starters[i]["position"], slot), i))
            cost += position_cost(starters[chosen]["position"], slot)
            order.append(chosen)
            available.remove(chosen)
        candidate = (cost, formation, order)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[1], best[2]


def build(page: str) -> dict:
    raw = json.loads(PLAYERS.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("players"), list):
        players = raw["players"]
    elif isinstance(raw, dict):
        players = list(raw.values())
    else:
        players = raw
    squads = published_squads(page)
    name_index: dict[str, list[dict]] = {}
    for player in players:
        for alias in player_names(player):
            name_index.setdefault(alias, []).append(player)
    player_by_id = {int(player.get("resourceId", 0)): player for player in players}
    output = []
    next_moments_id = 27
    for squad in squads:
        label = squad["sourceLabel"]
        moments = "MOMENTS" in label
        number = int(re.search(r"(\d+)$", label).group(1))
        squad_id = next_moments_id if moments else number
        if moments:
            next_moments_id += 1
        rows = [correct_source_row(label, row) for row in squad["rows"]]
        resolved = []
        for row in rows:
            player = choose_player(row, players, name_index, moments)
            resolved.append({
                "resourceId": int(player["resourceId"]),
                "name": str(player.get("name") or row["name"]),
                "rating": int(player.get("rating", 0) or 0),
                "position": str(player.get("preferredPosition") or row["position"]),
                "sourceName": row["name"],
                "sourceRating": int(row["rating"]),
                "sourcePosition": row["position"],
                "sourceClub": row["club"],
                "catalogTeam": str(player.get("teamName") or ""),
                "rareflag": int(player.get("rareflag", 0) or 0),
            })
        first_eleven, reserves = fifplay_rows(label)
        used: set[int] = set()
        ordered_starters: list[dict] = []
        # The independent article sometimes misses one starter (weeks 5,
        # 7, 19 and Moments 1); retain only official FIFAUTeam membership.
        for reference in first_eleven:
            record = match_reference(reference, resolved, used=used)
            used.add(record["resourceId"])
            ordered_starters.append({"record": record, "reference": reference})
        if len(ordered_starters) < 11:
            omitted_name = norm(MISSING_STARTERS.get(label, ""))
            omissions = [record for record in resolved if record["resourceId"] not in used
                         and norm(record["sourceName"]) == omitted_name]
            if len(omissions) != 1:
                raise ValueError(f"Cannot establish missing starter for {label}: {omissions}")
            record = omissions[0]
            ordered_starters.append({"record": record, "reference": {
                "name": record["sourceName"], "position": record["sourcePosition"],
                "rating": record["sourceRating"] or record["rating"],
                "assetId": record["resourceId"] & 0xFFFFFF,
            }})
            used.add(record["resourceId"])
        if len(ordered_starters) != 11:
            raise ValueError(f"Invalid published XI length for {label}")
        # Every other linked FIFPlay selection must belong to the official
        # roster. Moments 5 repeats Keita Baldé in both sections; omit the
        # duplicate link, not another official player.
        reserve_matches = []
        for reference in reserves:
            if reference["assetId"] and any(
                reference["assetId"] == entry["record"]["resourceId"] & 0xFFFFFF
                for entry in ordered_starters
            ):
                continue
            record = match_reference(reference, resolved, used=used)
            used.add(record["resourceId"])
            reserve_matches.append((record, reference))
        if not moments and len(used) != len(resolved):
            raise ValueError(f"Official/FIFPlay membership differs for {label}")
        for record, reference in reserve_matches:
            try:
                card = select_catalog_card(record, reference, players, moments=moments)
            except LookupError as exc:
                raise LookupError(f"{label}: {exc}") from exc
            record.update(resourceId=int(card["resourceId"]), rating=int(card["rating"]),
                          position=str(card["preferredPosition"]),
                          catalogTeam=str(card.get("teamName") or ""),
                          rareflag=int(card.get("rareflag", 0)))
        for entry in ordered_starters:
            record, reference = entry["record"], entry["reference"]
            try:
                card = select_catalog_card(record, reference, players, moments=moments)
            except LookupError as exc:
                raise LookupError(f"{label}: {exc}") from exc
            record.update(resourceId=int(card["resourceId"]), rating=int(card["rating"]),
                          position=str(card["preferredPosition"]),
                          catalogTeam=str(card.get("teamName") or ""),
                          rareflag=int(card.get("rareflag", 0)))
        if len({record["resourceId"] for record in resolved}) != len(resolved):
            raise ValueError(f"Duplicate card in {label}")
        formation, slot_order = formation_order([entry["record"] for entry in ordered_starters])
        starter_ids = [ordered_starters[index]["record"]["resourceId"] for index in slot_order]
        ordered_ids = starter_ids + [record["resourceId"] for record in resolved
                                     if record["resourceId"] not in starter_ids]
        title = f"Team of the Week Moments {number}" if moments else f"Team of the Week {number}"
        output.append({
            "id": squad_id,
            "name": f"FIFA 20 {title}",
            "shortName": title,
            "kind": "TOTW Moments" if moments else "TOTW",
            "sourceLabel": label,
            "formation": formation,
            "formationProvenance": "inferred from FIFPlay Starting 11 positions",
            "startingResourceIds": starter_ids,
            "fifplaySource": (f"https://www.fifplay.com/fifa-20-totw-moments-{number}/" if moments
                              else f"https://www.fifplay.com/fifa-20-totw-{number}/"),
            "players": resolved,
            "resourceIds": ordered_ids,
        })
    existing_ids = {int(entry["id"]) for entry in output}
    for number, definition in SUPPLEMENTAL_SQUADS.items():
        if number not in existing_ids:
            output.append(supplemental_squad(number, definition, player_by_id))
    output.sort(key=lambda entry: int(entry["id"]))
    return {
        "source": SOURCE,
        "description": "Published FIFA 20 TOTW and TOTW Moments squads bundled for offline use.",
        "squads": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, help="Use a downloaded archive page")
    args = parser.parse_args()
    page = args.html.read_text(encoding="utf-8") if args.html else fetch(SOURCE)
    archive = build(page)
    OUTPUT.write_text(json.dumps(archive, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(archive['squads'])} published squads to {OUTPUT}")
    print(f"Players: {sum(len(s['resourceIds']) for s in archive['squads'])}")


if __name__ == "__main__":
    main()
