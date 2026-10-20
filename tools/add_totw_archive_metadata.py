"""Add explicit selector identity without changing verified squad contents."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "localfut20" / "totw_archive.json"


def main() -> None:
    data = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    squads = data.get("squads", [])
    for archive_index, squad in enumerate(squads, 1):
        real_week = int(squad["id"])
        moments = 27 <= real_week <= 32
        squad.update({
            "archive_index": archive_index,
            "release_type": "totw_moments" if moments else "totw",
            "real_totw_number": real_week,
            "actual_week": real_week,
            "moments_number": real_week - 26 if moments else None,
            # Retail FIFA 20 kept WEEK 27..32 on the outside card while the
            # squad title identified TOTW Moments 1..6.
            "display_label": f"WEEK {real_week}",
        })
    data["archiveCount"] = len(squads)
    data["firstRealWeek"] = min(int(x["id"]) for x in squads)
    data["lastRealWeek"] = max(int(x["id"]) for x in squads)
    data["missingRegularWeeks"] = [33, 34, 35, 36, 37]
    ARCHIVE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Annotated {len(squads)} real archive entries through TOTW {data['lastRealWeek']}")


if __name__ == "__main__":
    main()
