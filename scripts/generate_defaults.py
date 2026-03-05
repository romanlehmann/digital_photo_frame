#!/usr/bin/env python3
"""Generate default placeholder landscape images for the photo frame.

Creates synthetic gradient landscapes so the frame has something to show
immediately on first boot, before any photo sync has happened.

Run once during development; commit the generated JPGs to the repo.
"""

import math
import random
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter


H_WIDTH, H_HEIGHT = 1920, 1200
V_WIDTH, V_HEIGHT = 1200, 1920

SCENES = [
    {
        'name': 'default_sunset',
        'sky': [(15, 10, 45), (140, 50, 30), (220, 120, 40), (250, 180, 80)],
        'horizon': 0.55,
        'ground': (25, 40, 20),
        'ground_highlight': (50, 60, 25),
        'mountains': [
            {'peak': 0.32, 'left': -0.15, 'right': 0.45, 'color': (40, 30, 45), 'snow': False},
            {'peak': 0.28, 'left': 0.20, 'right': 0.65, 'color': (50, 35, 50), 'snow': False},
            {'peak': 0.35, 'left': 0.50, 'right': 1.15, 'color': (55, 40, 52), 'snow': False},
        ],
        'sun': {'x': 0.5, 'y': 0.50, 'radius': 40, 'color': (255, 200, 100), 'glow': 120},
        'water': True,
        'water_horizon': 0.70,
        'stars': False,
        'clouds': [(0.2, 0.15, 200), (0.6, 0.10, 160), (0.85, 0.18, 140)],
    },
    {
        'name': 'default_daylight',
        'sky': [(40, 100, 190), (80, 150, 210), (150, 190, 230), (200, 215, 240)],
        'horizon': 0.58,
        'ground': (45, 90, 35),
        'ground_highlight': (70, 120, 50),
        'mountains': [
            {'peak': 0.35, 'left': -0.05, 'right': 0.40, 'color': (70, 85, 70), 'snow': True},
            {'peak': 0.30, 'left': 0.25, 'right': 0.70, 'color': (80, 95, 75), 'snow': True},
            {'peak': 0.38, 'left': 0.55, 'right': 1.10, 'color': (75, 90, 72), 'snow': True},
        ],
        'sun': {'x': 0.75, 'y': 0.15, 'radius': 35, 'color': (255, 250, 220), 'glow': 100},
        'water': False,
        'stars': False,
        'clouds': [(0.15, 0.20, 250), (0.45, 0.12, 200), (0.70, 0.25, 220), (0.90, 0.08, 180)],
    },
    {
        'name': 'default_twilight',
        'sky': [(5, 5, 25), (15, 10, 45), (50, 20, 70), (100, 50, 80)],
        'horizon': 0.50,
        'ground': (15, 18, 15),
        'ground_highlight': (25, 28, 22),
        'mountains': [
            {'peak': 0.28, 'left': -0.10, 'right': 0.50, 'color': (25, 20, 35), 'snow': False},
            {'peak': 0.32, 'left': 0.35, 'right': 1.10, 'color': (30, 22, 40), 'snow': False},
        ],
        'sun': None,
        'moon': {'x': 0.7, 'y': 0.18, 'radius': 20, 'color': (220, 220, 200)},
        'water': True,
        'water_horizon': 0.65,
        'stars': True,
        'clouds': [(0.3, 0.22, 80)],
    },
    {
        'name': 'default_morning',
        'sky': [(80, 120, 180), (150, 170, 200), (200, 200, 210), (240, 210, 170)],
        'horizon': 0.55,
        'ground': (50, 85, 35),
        'ground_highlight': (80, 115, 50),
        'mountains': [
            {'peak': 0.33, 'left': 0.00, 'right': 0.50, 'color': (90, 105, 90), 'snow': True},
            {'peak': 0.36, 'left': 0.35, 'right': 0.80, 'color': (100, 115, 95), 'snow': True},
            {'peak': 0.40, 'left': 0.65, 'right': 1.15, 'color': (95, 108, 88), 'snow': False},
        ],
        'sun': {'x': 0.85, 'y': 0.45, 'radius': 30, 'color': (255, 230, 160), 'glow': 90},
        'water': False,
        'stars': False,
        'clouds': [(0.10, 0.18, 230), (0.40, 0.10, 200), (0.65, 0.22, 210)],
    },
]


def lerp_color(c1, c2, t):
    return tuple(int(a + (b - a) * max(0, min(1, t))) for a, b in zip(c1, c2))


def draw_sky_gradient(img, width, height, horizon_y, colors):
    """Draw a multi-stop vertical gradient for the sky."""
    draw = ImageDraw.Draw(img)
    stops = len(colors)
    for y in range(horizon_y):
        t = y / max(horizon_y - 1, 1)
        pos = t * (stops - 1)
        idx = min(int(pos), stops - 2)
        frac = pos - idx
        color = lerp_color(colors[idx], colors[idx + 1], frac)
        draw.line([(0, y), (width, y)], fill=color)


def draw_sun(img, width, height, sun):
    """Draw sun with glow effect."""
    if not sun:
        return
    cx = int(width * sun['x'])
    cy = int(height * sun['y'])
    r = sun['radius']
    glow_r = sun.get('glow', 100)

    # Glow layers
    overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for i in range(glow_r, 0, -2):
        alpha = int(30 * (1 - i / glow_r) ** 2)
        gc = tuple(sun['color']) + (alpha,)
        draw.ellipse([cx - i, cy - i, cx + i, cy + i], fill=gc)

    # Solid sun
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(sun['color']) + (255,))
    img.paste(Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB'))


def draw_moon(img, width, height, moon):
    """Draw a crescent moon."""
    if not moon:
        return
    cx = int(width * moon['x'])
    cy = int(height * moon['y'])
    r = moon['radius']
    draw = ImageDraw.Draw(img)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=moon['color'])
    # Crescent shadow
    shadow_offset = int(r * 0.4)
    # Sample sky color near moon for shadow
    sx, sy = min(cx + r + 5, width - 1), cy
    sky_color = img.getpixel((sx, sy))
    draw.ellipse([cx - r + shadow_offset, cy - r - 2, cx + r + shadow_offset, cy + r - 2],
                 fill=sky_color)


def draw_stars(img, width, horizon_y):
    """Scatter stars in the sky."""
    draw = ImageDraw.Draw(img)
    rng = random.Random(42)
    for _ in range(120):
        x = rng.randint(0, width - 1)
        y = rng.randint(0, int(horizon_y * 0.8))
        brightness = rng.randint(160, 255)
        size = rng.choice([0, 0, 0, 1])
        if size == 0:
            draw.point((x, y), fill=(brightness, brightness, brightness))
        else:
            draw.ellipse([x, y, x + 1, y + 1], fill=(brightness, brightness, brightness))


def draw_clouds(draw, width, height, clouds, horizon_y):
    """Draw simple layered clouds."""
    rng = random.Random(123)
    for cx_frac, cy_frac, brightness in clouds:
        cx = int(width * cx_frac)
        cy = int(horizon_y * cy_frac)
        # Cloud is a cluster of ellipses
        for _ in range(8):
            ox = rng.randint(-80, 80)
            oy = rng.randint(-12, 12)
            rw = rng.randint(40, 100)
            rh = rng.randint(12, 25)
            b = brightness + rng.randint(-20, 20)
            alpha_color = (b, b, b)
            draw.ellipse([cx + ox - rw, cy + oy - rh, cx + ox + rw, cy + oy + rh],
                         fill=alpha_color)


def draw_mountain(draw, width, peak_y, left_x, right_x, base_y, color, snow=False):
    """Draw a mountain with optional snow cap."""
    peak_x = (left_x + right_x) // 2
    draw.polygon([(left_x, base_y), (peak_x, peak_y), (right_x, base_y)], fill=color)
    if snow:
        snow_y = peak_y + int((base_y - peak_y) * 0.15)
        snow_color = tuple(min(255, c + 120) for c in color)
        draw.polygon([
            (peak_x - int((right_x - left_x) * 0.06), snow_y),
            (peak_x, peak_y),
            (peak_x + int((right_x - left_x) * 0.06), snow_y),
        ], fill=snow_color)


def draw_ground(draw, width, height, horizon_y, color, highlight):
    """Fill ground area with gradient."""
    for y in range(horizon_y, height):
        t = (y - horizon_y) / max(height - horizon_y - 1, 1)
        c = lerp_color(highlight, color, t)
        draw.line([(0, y), (width, y)], fill=c)


def draw_water(img, width, height, horizon_y, water_y):
    """Draw water with sky reflection."""
    water_start = int(height * water_y)
    # Reflect the sky portion into water
    sky_strip = img.crop((0, 0, width, water_start))
    reflected = sky_strip.transpose(Image.FLIP_TOP_BOTTOM)
    water_height = height - water_start
    reflected = reflected.resize((width, water_height), Image.LANCZOS)
    # Darken and blue-shift
    from PIL import ImageEnhance
    reflected = ImageEnhance.Brightness(reflected).enhance(0.5)
    img.paste(reflected, (0, water_start))
    # Horizontal ripple lines
    draw = ImageDraw.Draw(img)
    rng = random.Random(99)
    for y in range(water_start, height, 4):
        alpha = int(20 + 15 * ((y - water_start) / max(water_height, 1)))
        draw.line([(0, y), (width, y)],
                  fill=(alpha, alpha, alpha + 10))


def generate_landscape(scene, width, height):
    """Generate a single landscape image from a scene definition."""
    img = Image.new('RGB', (width, height))
    horizon_y = int(height * scene['horizon'])

    # Sky
    draw_sky_gradient(img, width, height, horizon_y, scene['sky'])

    # Stars (before other elements)
    if scene.get('stars'):
        draw_stars(img, width, horizon_y)

    # Sun or Moon (behind mountains)
    if scene.get('sun'):
        draw_sun(img, width, height, scene['sun'])
    if scene.get('moon'):
        draw_moon(img, width, height, scene['moon'])

    draw = ImageDraw.Draw(img)

    # Clouds
    if scene.get('clouds'):
        draw_clouds(draw, width, height, scene['clouds'], horizon_y)

    # Ground
    draw_ground(draw, width, height, horizon_y, scene['ground'], scene.get('ground_highlight', scene['ground']))

    # Mountains
    for m in scene['mountains']:
        peak_y = int(height * m['peak'])
        left_x = int(width * m['left'])
        right_x = int(width * m['right'])
        draw_mountain(draw, width, peak_y, left_x, right_x, horizon_y, m['color'], m.get('snow', False))

    # Water reflection
    if scene.get('water'):
        draw_water(img, width, height, horizon_y, scene.get('water_horizon', 0.7))

    # Soften slightly
    img = img.filter(ImageFilter.GaussianBlur(radius=2))

    return img


def create_blur_fill(image, target_w, target_h, blur_radius=40, darken=0.6):
    """Create image at target dimensions with blur-filled background."""
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
        h_img.save(h_path, 'JPEG', quality=90, optimize=True)
        print(f"  -> {h_path} ({h_img.size[0]}x{h_img.size[1]})")

        # Vertical (blur-fill from horizontal source)
        v_img = create_blur_fill(h_img, V_WIDTH, V_HEIGHT)
        v_path = v_dir / f"{name}.jpg"
        v_img.save(v_path, 'JPEG', quality=90, optimize=True)
        print(f"  -> {v_path} ({v_img.size[0]}x{v_img.size[1]})")

        del h_img, v_img

    print("Done!")


if __name__ == '__main__':
    main()
