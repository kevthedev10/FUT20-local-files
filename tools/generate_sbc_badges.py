"""Bundle the exact FIFA 20 FUTBIN group art on a native carousel canvas."""

from __future__ import annotations

import sys
import math
import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localfut20.sbc_catalog import SBC_CATALOG  # noqa: E402

OUT = ROOT / "localfut20" / "assets" / "sbc_sets"
REFERENCE = ROOT / "tools" / "sbc_reference"
SCALE = 3
SIZE = 256
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
FONT = next((candidate for candidate in FONT_CANDIDATES if candidate.is_file()), FONT_CANDIDATES[0])
FUTBIN_ART = {
    1: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000001-43070306-5e83.png",
    2: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000004-8c12d6f9-4923.png",
    10: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000002-89e9285a-7013.png",
    11: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000003-d31aeb07-ebaa.png",
    20: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000010-715fb8f2-b49a.png",
    21: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000011-08ee6b73-9355.png",
    22: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000012-8e24cd29-d095.png",
    30: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000005-b2cf60f7-2475.png",
    31: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000006-a26d2181-5442.png",
    32: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000007-c9a16a3d-3b2f.png",
    532: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000055-a444ad18-7c7f.png",
    542: "https://cdn.futbin.com/content/fifa20/img/sbc/sbc_set_image_1000255-cb6b079b-01f6.png",
}
DISPLAY_WIDTH = 220
DISPLAY_HEIGHT = 225
CANVAS_WIDTH = 375
CANVAS_HEIGHT = 520
RIGHT_INSET = 4
BOTTOM_INSET = 8


def normalize_visible_art(image: Image.Image) -> Image.Image:
    """Trim source transparency, then normalize the visible crest itself.

    Several official FIFA 20/FUTBIN set PNGs contain very different internal
    transparent margins.  Scaling the complete source image made some badges
    (notably player SBCs and upgrades) much smaller than others.  The retail
    carousel cares about the returned PNG bounds, so normalize the alpha bounds
    first and place every visible crest on the same calibrated canvas.
    """
    image = image.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is not None:
        image = image.crop(bbox)
    image.thumbnail((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.Resampling.LANCZOS)
    return image



def official_art(set_id: int) -> Image.Image | None:
    url = FUTBIN_ART.get(set_id)
    if not url:
        return None
    cache = REFERENCE / "futbin" / f"{set_id}.png"
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    image = Image.open(io.BytesIO(payload)).convert("RGBA")
    cache.parent.mkdir(parents=True, exist_ok=True)
    image.save(cache, optimize=True)
    return image


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size * SCALE)


def badge(entry: dict) -> Image.Image:
    set_id = int(entry["set_id"])
    category = str(entry["category"])
    image = Image.new("RGBA", (SIZE * SCALE, SIZE * SCALE))
    draw = ImageDraw.Draw(image)

    if category == "BASIC":
        outer, inner, gold = "#c9d7e7", "#0c346c", "#edcf70"
    elif category == "ADVANCED":
        outer, inner, gold = "#f8c778", "#241a56", "#ffcc5c"
    else:
        outer, inner, gold = "#d1dadb", "#164a56", "#d8edee"

    def pt(*values: int) -> tuple[int, ...]:
        return tuple(value * SCALE for value in values)

    shadow = [(x * SCALE, y * SCALE) for x, y in
              [(129, 16), (219, 45), (215, 157), (180, 210), (129, 247),
               (78, 210), (43, 157), (39, 45)]]
    shape = [(x * SCALE, y * SCALE) for x, y in
             [(128, 11), (216, 40), (211, 153), (176, 205), (128, 241),
              (80, 205), (45, 153), (40, 40)]]
    draw.polygon(shadow, fill="#1a2533")
    draw.polygon(shape, fill=outer)
    inset = [(x * SCALE, y * SCALE) for x, y in
             [(128, 22), (206, 48), (202, 149), (168, 198), (128, 229),
              (88, 198), (54, 149), (50, 48)]]
    draw.polygon(inset, fill=inner)
    draw.line([(65 * SCALE, 60 * SCALE), (191 * SCALE, 60 * SCALE)],
              fill=outer, width=2 * SCALE)
    draw.text(pt(128, 37), "FUT 20", font=font(14), fill=outer, anchor="mm")

    if set_id == 1:
        star = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            radius = 55 if index % 2 == 0 else 23
            star.append(((128 + math.cos(angle) * radius) * SCALE,
                         (121 + math.sin(angle) * radius) * SCALE))
        draw.polygon(star, fill=gold)
    elif set_id == 10:
        draw.text(pt(128, 121), "?", font=font(102), fill=gold, anchor="mm")
    elif set_id in (2, 21):
        draw.ellipse(pt(78, 72, 178, 172), outline=gold, width=6 * SCALE)
        draw.ellipse(pt(107, 72, 149, 172), outline=gold, width=4 * SCALE)
        draw.line(pt(81, 121, 175, 121), fill=gold, width=4 * SCALE)
    elif category == "ADVANCED":
        draw.polygon([pt(128, 68), pt(183, 112), pt(162, 164),
                      pt(94, 164), pt(73, 112)], outline=gold,
                     width=6 * SCALE)
        draw.polygon([pt(128, 92), pt(154, 118), pt(128, 146),
                      pt(102, 118)], fill=gold)
    else:
        draw.polygon([pt(128, 73), pt(178, 125), pt(151, 125),
                      pt(151, 164), pt(105, 164), pt(105, 125),
                      pt(78, 125)], fill=gold)

    label = ("FOUNDATION" if category == "BASIC" else
             "HYBRID" if category == "ADVANCED" else "UPGRADE")
    draw.rounded_rectangle(pt(62, 174, 194, 199), radius=4 * SCALE,
                           fill=outer)
    draw.text(pt(128, 186), label, font=font(11), fill=inner, anchor="mm")
    draw.text(pt(128, 215), f"{set_id:02d}", font=font(11), fill=outer,
              anchor="mm")
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for entry in SBC_CATALOG:
        set_id = int(entry["set_id"])
        image = official_art(set_id)
        if image is None:
            image = badge(entry)
        image = normalize_visible_art(image)

        # FIFA anchors the set texture from the upper image socket rather than
        # vertically centring its alpha bounds. A taller transparent canvas is
        # therefore the reliable way to put the correctly-sized crest directly
        # above the carousel shadow, as in the retail reference screenshots.
        canvas = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT))
        x = max(0, CANVAS_WIDTH - RIGHT_INSET - image.width)
        y = max(0, CANVAS_HEIGHT - BOTTOM_INSET - image.height)
        canvas.alpha_composite(image, (x, y))
        canvas.save(OUT / f"{set_id}.png", optimize=True)
    print(f"Rendered {len(SBC_CATALOG)} SBC badges in {OUT}")


if __name__ == "__main__":
    main()
