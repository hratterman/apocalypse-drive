#!/usr/bin/env python3
"""
Generate Apocalypse Drive app icons in all required sizes.

Style: yellow ASCII-block letter A on a dark navy background, matching the
terminal-theme banner on Apocalypse.html. Single glyph, centered, padded.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICONSET = ASSETS / "icon.iconset"

# Apocalypse Drive yellow (terminal amber, slightly hotter than the old orange)
FG = (255, 214, 0, 255)       # #FFD600
BG = (10, 14, 26, 255)         # near-black navy, matches terminal theme
SHADOW = (255, 214, 0, 60)     # soft glow

# The "A" in the same block-shaded style as the banner on Apocalypse.html.
# Hand-laid so each row is exactly the same width and the A reads cleanly
# at small sizes. 8 rows tall, 11 cols wide.
A_GLYPH = [
    "   █████╗   ",
    "  ██╔══██╗  ",
    "  ███████║  ",
    "  ██╔══██║  ",
    "  ██║  ██║  ",
    "  ╚═╝  ╚═╝  ",
]


def render_glyph(size: int) -> Image.Image:
    """Render the ASCII A glyph centered on a square `size`x`size` canvas."""
    img = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)

    # Find a monospace font that ships on macOS / Linux / Windows.
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        "C:\\Windows\\Fonts\\consola.ttf",
        "C:\\Windows\\Fonts\\lucon.ttf",
    ]
    font_path = next((p for p in candidates if os.path.exists(p)), None)

    glyph_lines = A_GLYPH
    rows = len(glyph_lines)
    cols = max(len(line) for line in glyph_lines)

    # Target the glyph to fill ~75% of canvas height, with margins.
    target_height = int(size * 0.78)
    # Each row of block chars is ~1.1x its width in monospace; estimate font
    # size from rows.
    font_size = max(6, int(target_height / rows))

    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    # Measure actual rendered size of the full block.
    block_text = "\n".join(glyph_lines)
    bbox = draw.multiline_textbbox((0, 0), block_text, font=font, spacing=0)
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]

    # If the chosen font_size makes the glyph too wide for the canvas, scale
    # down by width instead.
    if bw > size * 0.92 and font_path:
        font_size = max(6, int(font_size * (size * 0.92) / bw))
        font = ImageFont.truetype(font_path, font_size)
        bbox = draw.multiline_textbbox((0, 0), block_text, font=font, spacing=0)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]

    x = (size - bw) // 2 - bbox[0]
    y = (size - bh) // 2 - bbox[1]

    # Soft glow layer for the larger sizes.
    if size >= 128:
        glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
            gdraw.multiline_text(
                (x + dx, y + dy), block_text, font=font, fill=SHADOW, spacing=0
            )
        img.alpha_composite(glow)

    draw.multiline_text((x, y), block_text, font=font, fill=FG, spacing=0)
    return img


def render_tray(size: int = 64) -> Image.Image:
    """Tray icon: simpler, single-color A, transparent background."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "C:\\Windows\\Fonts\\consola.ttf",
    ]
    font_path = next((p for p in candidates if os.path.exists(p)), None)
    if font_path:
        font = ImageFont.truetype(font_path, int(size * 0.78))
    else:
        font = ImageFont.load_default()
    text = "A"
    bbox = draw.textbbox((0, 0), text, font=font)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - bw) // 2 - bbox[0], (size - bh) // 2 - bbox[1]),
        text,
        font=font,
        fill=FG,
    )
    return img


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    ICONSET.mkdir(exist_ok=True)

    sizes = [16, 32, 48, 64, 128, 256, 512, 1024]
    print("[icons] rendering yellow ASCII-A icons...")
    for s in sizes:
        out = ASSETS / f"icon-{s}.png"
        img = render_glyph(s)
        img.save(out, "PNG")
        print(f"  [ok] {out.name} ({s}x{s})")

    # Tray icon (transparent background).
    tray = render_tray(64)
    tray.save(ASSETS / "icon-tray.png", "PNG")
    print(f"  [ok] icon-tray.png (transparent)")

    # macOS .iconset → .icns
    iconset_map = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for s, name in iconset_map:
        Image.open(ASSETS / f"icon-{s}.png").save(ICONSET / name, "PNG")

    if sys.platform == "darwin":
        icns = ASSETS / "icon.icns"
        try:
            subprocess.run(
                ["iconutil", "-c", "icns", str(ICONSET), "-o", str(icns)],
                check=True,
            )
            print(f"  [ok] icon.icns")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"  [warn] iconutil failed: {e}")

    # Windows .ico (multi-resolution)
    ico = ASSETS / "icon.ico"
    base = Image.open(ASSETS / "icon-256.png")
    base.save(
        ico,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"  [ok] icon.ico")

    print("[icons] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
