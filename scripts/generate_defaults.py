#!/usr/bin/env python3
"""Generate default placeholder landscape images for the photo frame.

Creates synthetic gradient landscapes so the frame has something to show
immediately on first boot, before any photo sync has happened.

Run once during development; commit the generated JPGs to the repo.
"""

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter


H_WIDTH, H_HEIGHT = 1920, 1200
V_WIDTH, V_HEIGHT = 1200, 1920

SCENES = [
    {
        'name': 'default_sunset',
        'sky': [(25, 25, 80), (180, 80, 40), (220, 140, 60)],
        'horizon': 0.55,
        'ground': (30, 45, 25),
        'mountains': [
            {'peak': 0.38, 'left': -0.1, 'right': 0.55, 'color': (50, 40, 50)},
            {'peak': 0.35, 'left': 0.35, 'right': 1.1, 'color': (60, 45, 55)},
        ],
    },
    {
        'name': 'default_daylight',
        'sky': [(70, 130, 200), (140, 180, 220), (200, 210, 230)],
        'horizon': 0.6,
        'ground': (60, 100, 40),
        'mountains': [
            {'peak': 0.42, 'left': -0.05, 'right': 0.5, 'color': (80, 100, 80)},
            {'peak': 0.40, 'left': 0.4, 'right': 1.05, 'color': (90, 110, 85)},
        ],
    },
    {
        'name': 'default_twilight',
        'sky': [(15, 15, 50), (60, 30, 80), (120, 60, 90)],
        'horizon': 0.5,
        'ground': (20, 25, 20),
        'mountains': [
            {'peak': 0.30, 'left': -0.1, 'right': 0.6, 'color': (30, 25, 40)},
            {'peak': 0.33, 'left': 0.3, 'right': 1.1, 'color': (35, 28, 45)},
        ],
    },
    {
        'name': 'default_morning',
        'sky': [(100, 150, 200), (180, 190, 210), (240, 220, 180)],
        'horizon': 0.58,
        'ground': (50, 80, 35),
        'mountains': [
            {'peak': 0.40, 'left': 0.1, 'right': 0.7, 'color': (100, 120, 100)},
            {'peak': 0.43, 'left': 0.55, 'right': 1.15, 'color': (110, 125, 105)},
        ],
    },
]


def lerp_color(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def draw_sky_gradient(draw, width, height, horizon_y, colors):
    """Draw a multi-stop vertical gradient for the sky."""
    stops = len(colors)
    for y in range(horizon_y):
        t = y / max(horizon_y - 1, 1)
        # Find which two stops we're between
        pos = t * (stops - 1)
        idx = min(int(pos), stops - 2)
        frac = pos - idx
        color = lerp_color(colors[idx], colors[idx + 1], frac)
        draw.line([(0, y), (width, y)], fill=color)


def draw_mountain(draw, width, peak_y, left_x, right_x, base_y, color):
    """Draw a triangle mountain shape."""
    peak_x = (left_x + right_x) // 2
    draw.polygon([(left_x, base_y), (peak_x, peak_y), (right_x, base_y)], fill=color)


def draw_ground(draw, width, height, horizon_y, color):
    """Fill ground area with a slight gradient."""
    for y in range(horizon_y, height):
        t = (y - horizon_y) / max(height - horizon_y - 1, 1)
        darker = lerp_color(color, tuple(max(0, c - 20) for c in color), t)
        draw.line([(0, y), (width, y)], fill=darker)


def generate_landscape(scene, width, height):
    """Generate a single landscape image from a scene definition."""
    img = Image.new('RGB', (width, height))
    draw = ImageDraw.Draw(img)

    horizon_y = int(height * scene['horizon'])

    # Sky
    draw_sky_gradient(draw, width, height, horizon_y, scene['sky'])

    # Ground
    draw_ground(draw, width, height, horizon_y, scene['ground'])

    # Mountains
    for m in scene['mountains']:
        peak_y = int(height * m['peak'])
        left_x = int(width * m['left'])
        right_x = int(width * m['right'])
        draw_mountain(draw, width, peak_y, left_x, right_x, horizon_y, m['color'])

    # Soften everything slightly
    img = img.filter(ImageFilter.GaussianBlur(radius=3))

    return img


def create_blur_fill(image, target_w, target_h, blur_radius=40, darken=0.6):
    """Create image at target dimensions with blur-filled background.

    Same logic as photo_sync.create_blur_fill — cross-orientation images get
    a blurred+darkened background with the sharp image centered.
    """
    from PIL import ImageEnhance

    img_w, img_h = image.size
    src_landscape = img_w >= img_h
    tgt_landscape = target_w >= target_h

    if src_landscape == tgt_landscape:
        scale_cover = max(target_w / img_w, target_h / img_h)
        cover_w = int(img_w * scale_cover)
        cover_h = int(img_h * scale_cover)
        result = image.resize((cover_w, cover_h), Image.LANCZOS)
        left = (cover_w - target_w) // 2
        top = (cover_h - target_h) // 2
        return result.crop((left, top, left + target_w, top + target_h))

    # Cross-orientation: blur-fill background
    scale_fit = min(target_w / img_w, target_h / img_h)
    fit_w = int(img_w * scale_fit)
    fit_h = int(img_h * scale_fit)

    scale_cover = max(target_w / img_w, target_h / img_h)
    cover_w = int(img_w * scale_cover)
    cover_h = int(img_h * scale_cover)

    bg = image.resize((cover_w, cover_h), Image.LANCZOS)
    left = (cover_w - target_w) // 2
    top = (cover_h - target_h) // 2
    bg = bg.crop((left, top, left + target_w, top + target_h))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    bg = ImageEnhance.Brightness(bg).enhance(darken)

    fg = image.resize((fit_w, fit_h), Image.LANCZOS)
    paste_x = (target_w - fit_w) // 2
    paste_y = (target_h - fit_h) // 2
    bg.paste(fg, (paste_x, paste_y))
    return bg


def main():
    repo_root = Path(__file__).resolve().parent.parent
    h_dir = repo_root / 'viewer' / 'defaults' / 'horizontal'
    v_dir = repo_root / 'viewer' / 'defaults' / 'vertical'
    h_dir.mkdir(parents=True, exist_ok=True)
    v_dir.mkdir(parents=True, exist_ok=True)

    for scene in SCENES:
        name = scene['name']
        print(f"Generating {name}...")

        # Horizontal (native landscape)
        h_img = generate_landscape(scene, H_WIDTH, H_HEIGHT)
        h_path = h_dir / f"{name}.jpg"
        h_img.save(h_path, 'JPEG', quality=85, optimize=True)
        print(f"  -> {h_path} ({h_img.size[0]}x{h_img.size[1]})")

        # Vertical (blur-fill from horizontal source)
        v_img = create_blur_fill(h_img, V_WIDTH, V_HEIGHT)
        v_path = v_dir / f"{name}.jpg"
        v_img.save(v_path, 'JPEG', quality=85, optimize=True)
        print(f"  -> {v_path} ({v_img.size[0]}x{v_img.size[1]})")

        del h_img, v_img

    print("Done!")


if __name__ == '__main__':
    main()
