"""
Generate the app icon at multiple sizes.
Source: Pillow-drawn vector-ish design.
Outputs:
  - assets/icon-1024.png  (master)
  - assets/icon.icns      (macOS, multi-resolution)
  - assets/icon.ico       (Windows, multi-resolution)
  - assets/icon-tray.png  (16/22/32px tray icon set)
"""
import os
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent.parent / 'assets'
ASSETS.mkdir(parents=True, exist_ok=True)

# ---- Master 1024 icon ------------------------------------------------------
def draw_master(size=1024):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Color palette: one accent gold, one bg, no bevel theatrics
    bg_top = (24, 27, 36, 255)
    bg_bot = (10, 12, 17, 255)
    border = (50, 58, 78, 255)
    accent = (255, 169, 64, 255)
    bg_solid = (15, 17, 21, 255)

    radius = int(size * 0.22)

    # 1) Vertical gradient background, masked into the rounded square
    grad = Image.new('RGBA', (size, size), bg_solid)
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(bg_top[0] * (1 - t) + bg_bot[0] * t)
        g = int(bg_top[1] * (1 - t) + bg_bot[1] * t)
        b = int(bg_top[2] * (1 - t) + bg_bot[2] * t)
        gd.line([(0, y), (size, y)], fill=(r, g, b, 255))

    # Rounded mask
    mask = Image.new('L', (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)

    img.paste(grad, (0, 0), mask)

    # Border (very subtle)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius,
                        outline=border, width=max(1, int(size * 0.006)))

    # 2) Single ring, stroke weight matched to A stroke weight
    cx, cy = size // 2, int(size * 0.50)  # true vertical center
    ring_r = int(size * 0.36)
    ring_w = max(1, int(size * 0.030))  # thicker, matches A weight
    d.ellipse((cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
              outline=accent, width=ring_w)

    # 3) The "A" letterform: pure flat, optically centered
    # Optical centering: shift the A up by ~3% to compensate for visual heaviness
    # of the wide base.
    optical_offset = int(size * 0.025)
    apex_y = int(size * 0.32) - optical_offset
    base_y = int(size * 0.70) - optical_offset
    base_half_w = int(size * 0.18)
    inner_apex_y = apex_y + int(size * 0.10)
    inner_base_half_w = base_half_w - int(size * 0.06)

    # Outer triangle (filled accent)
    outer = [(cx, apex_y),
             (cx - base_half_w, base_y),
             (cx + base_half_w, base_y)]
    d.polygon(outer, fill=accent)

    # Inner triangle "hole": sample bg color at the right gradient position
    t = base_y / size
    cut_color = (int(bg_top[0] * (1 - t) + bg_bot[0] * t),
                 int(bg_top[1] * (1 - t) + bg_bot[1] * t),
                 int(bg_top[2] * (1 - t) + bg_bot[2] * t),
                 255)
    inner = [(cx, inner_apex_y),
             (cx - inner_base_half_w, base_y - int(size * 0.005)),
             (cx + inner_base_half_w, base_y - int(size * 0.005))]
    d.polygon(inner, fill=cut_color)

    # Crossbar (raised slightly higher, was too low before)
    bar_y = int(size * 0.54) - optical_offset
    bar_h = int(size * 0.045)
    span = base_y - inner_apex_y
    pos = (bar_y - inner_apex_y) / span
    x_at_bar = inner_base_half_w * pos
    d.rectangle((cx - x_at_bar - int(size * 0.005), bar_y,
                 cx + x_at_bar + int(size * 0.005), bar_y + bar_h),
                fill=accent)

    return img


# ---- Tray icon (small, monochrome amber dot, scales 16-32px) ---------------
def draw_tray(size=64):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = (255, 169, 64, 255)
    # Outer ring
    d.ellipse((int(size*0.06), int(size*0.06), int(size*0.94), int(size*0.94)),
              outline=accent, width=max(1, int(size*0.08)))
    # Solid inner triangle (the A apex)
    cx, cy = size//2, size//2
    half = int(size*0.22)
    poly = [(cx, cy - half), (cx - half, cy + half//2), (cx + half, cy + half//2)]
    d.polygon(poly, fill=accent)
    return img


def main():
    # Master + downsized PNGs
    master = draw_master(1024)
    master.save(ASSETS / 'icon-1024.png')
    print(f"  [ok] icon-1024.png")
    
    sizes = [512, 256, 128, 64, 48, 32, 16]
    pngs = {}
    for s in sizes:
        img = draw_master(s)  # redraw fresh to keep crisp edges, not blurry downsample
        path = ASSETS / f'icon-{s}.png'
        img.save(path)
        pngs[s] = path
        print(f"  [ok] icon-{s}.png")
    
    # Tray icon
    tray = draw_tray(64)
    tray.save(ASSETS / 'icon-tray.png')
    print(f"  [ok] icon-tray.png")
    
    # Windows .ico (multi-res)
    ico_path = ASSETS / 'icon.ico'
    master.save(ico_path, format='ICO',
                sizes=[(s, s) for s in [16, 32, 48, 64, 128, 256]])
    print(f"  [ok] icon.ico")
    
    # macOS .icns (requires `iconutil` from Xcode CLT, or fall back to Pillow)
    iconset = ASSETS / 'icon.iconset'
    iconset.mkdir(exist_ok=True)
    icns_sizes = [
        (16, '16x16'),
        (32, '16x16@2x'),
        (32, '32x32'),
        (64, '32x32@2x'),
        (128, '128x128'),
        (256, '128x128@2x'),
        (256, '256x256'),
        (512, '256x256@2x'),
        (512, '512x512'),
        (1024, '512x512@2x'),
    ]
    for size, name in icns_sizes:
        img = draw_master(size)
        img.save(iconset / f'icon_{name}.png')
    icns_path = ASSETS / 'icon.icns'
    if sys.platform == 'darwin':
        try:
            subprocess.check_call(['iconutil', '-c', 'icns', str(iconset), '-o', str(icns_path)])
            print(f"  [ok] icon.icns")
        except Exception as e:
            print(f"  [fail] icon.icns failed: {e}")
            print(f"    (PNG iconset still available at {iconset})")
    else:
        print(f"  - icon.icns (skipped, not on macOS, iconset preserved)")
    
    print(f"\nIcons written to {ASSETS}")


if __name__ == '__main__':
    main()
