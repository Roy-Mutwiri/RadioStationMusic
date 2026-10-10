"""Generate the Trade Fix Radio app icon.

Draws a dark rounded tile with a gold (XAUUSD) ring and a market-shaped waveform,
then writes a multi-size .ico for the desktop window and a favicon for the web UI.
Run:  .venv/Scripts/python desktop/make_icon.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BG = (12, 14, 20)
TILE = (22, 26, 36)
GOLD = (232, 180, 64)
GOLD_DIM = (150, 112, 36)
WAVE = (240, 244, 250)


def render(size: int = 1024) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded dark tile
    radius = int(s * 0.22)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=TILE)

    # Gold ring (a coin / a dial)
    cx = cy = s / 2
    r_outer = s * 0.40
    ring_w = s * 0.055
    d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), outline=GOLD, width=int(ring_w))
    r_inner = r_outer - ring_w * 1.9
    d.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner), outline=GOLD_DIM, width=max(2, int(s * 0.012)))

    # Waveform bars across the middle: heights follow a "breakout" curve
    n = 13
    span = r_inner * 1.5
    gap = span / n
    bar_w = gap * 0.55
    heights = [0.18, 0.28, 0.22, 0.42, 0.34, 0.62, 0.90, 0.62, 0.40, 0.56, 0.30, 0.24, 0.16]
    for i, h in enumerate(heights):
        x = cx - span / 2 + gap * (i + 0.5)
        half = (r_inner * 0.9) * h / 2
        colour = GOLD if i in (6,) else WAVE
        d.rounded_rectangle((x - bar_w / 2, cy - half, x + bar_w / 2, cy + half), radius=bar_w / 2, fill=colour)

    # Small gold "signal" dot on the ring, top-right, like a tuning marker
    ang = math.radians(-45)
    mx, my = cx + r_outer * math.cos(ang), cy + r_outer * math.sin(ang)
    mr = s * 0.045
    d.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=BG, outline=GOLD, width=int(s * 0.012))
    d.ellipse((mx - mr * 0.45, my - mr * 0.45, mx + mr * 0.45, my + mr * 0.45), fill=GOLD)
    return img


def main() -> None:
    master = render(1024)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = master.resize((256, 256), Image.LANCZOS)

    ico = HERE / "tradefix.ico"
    base.save(ico, format="ICO", sizes=[(n, n) for n in sizes])
    master.resize((512, 512), Image.LANCZOS).save(HERE / "tradefix.png")

    public = ROOT / "frontend" / "public"
    public.mkdir(exist_ok=True)
    base.save(public / "favicon.ico", format="ICO", sizes=[(n, n) for n in sizes])
    master.resize((192, 192), Image.LANCZOS).save(public / "icon-192.png")
    print(f"wrote {ico}, {HERE / 'tradefix.png'}, {public / 'favicon.ico'}")


if __name__ == "__main__":
    main()
