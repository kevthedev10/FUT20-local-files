"""Import the archived end-of-cycle FUTBIN FIFA 20 SBC group index.

The live historical page is protected by Cloudflare.  The Internet Archive
snapshot below was captured after FIFA 20's content cycle ended and contains
the complete FUTBIN group carousel, including the original group artwork,
descriptions, repeatability and group-reward labels.  Individual challenge
requirements are intentionally *not* inferred from this index.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tools" / "sbc_reference" / "futbin_group_index.json"
SNAPSHOT_URL = (
    "https://web.archive.org/web/20201102203822id_/"
    "https://www.futbin.com/20/squad-building-challenges"
)


def text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def parse_groups(document: str) -> list[dict[str, object]]:
    anchors = list(re.finditer(
        r'<a[^>]+href="/20/squad-building-challenges/ALL/(?P<id>\d+)"[^>]*>',
        document,
        re.IGNORECASE,
    ))
    groups: list[dict[str, object]] = []
    for index, match in enumerate(anchors):
        start = match.end()
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(document)
        block = document[start:end]
        name_match = re.search(r'<div class="set_name">(?P<value>.*?)</div>', block, re.I | re.S)
        image_match = re.search(
            r'<div class="set_image">\s*<img[^>]+(?:data-original|src)="(?P<value>[^"]+)"',
            block,
            re.I | re.S,
        )
        description_match = re.search(
            r'<div class="set_desc">(?P<value>.*?)</div>', block, re.I | re.S
        )
        repeat_match = re.search(
            r'<div class="set_repeatable">(?P<value>.*?)</div>', block, re.I | re.S
        )
        reward_names = [
            text(value) for value in re.findall(
                r'class="(?:pack|player)_small_reward_name_right\s*">(.*?)</div>',
                block,
                re.I | re.S,
            )
        ]
        reward_counts = [
            int(text(value) or 1) for value in re.findall(
                r'class="(?:pack|player)_small_reward_count_right\s*">(.*?)</div>',
                block,
                re.I | re.S,
            )
        ]
        if not name_match or not image_match:
            continue
        groups.append({
            "futbinSetId": int(match.group("id")),
            "name": text(name_match.group("value")),
            "description": text(description_match.group("value")) if description_match else "",
            "repeatable": "non-repeatable" not in text(repeat_match.group("value")).lower()
            if repeat_match else False,
            "imageUrl": html.unescape(image_match.group("value")),
            "groupRewards": [
                {"name": name, "count": reward_counts[i] if i < len(reward_counts) else 1}
                for i, name in enumerate(reward_names)
            ],
        })
    return groups


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    groups = parse_groups(fetch(SNAPSHOT_URL).decode("utf-8", "replace"))
    if len(groups) < 100:
        raise RuntimeError(f"archived FUTBIN catalogue unexpectedly contained {len(groups)} groups")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"source": SNAPSHOT_URL, "groups": groups}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Imported {len(groups)} FIFA 20 SBC groups to {args.output}")


if __name__ == "__main__":
    main()
