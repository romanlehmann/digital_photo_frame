#!/usr/bin/env python3
"""
Photo preparation script for digital photo frame.
Runs on Synology NAS to select and process photos weekly.

For each selected photo, generates two versions:
- horizontal/ (1920x1200) - landscape orientation with blur-fill if needed
- vertical/   (1200x1920) - portrait orientation with blur-fill if needed

Photos are downscaled to the target resolution and saved as optimized JPEGs.
"""

import os
import sys
import sqlite3
import logging
import random
import time
import yaml
from pathlib import Path
from datetime import datetime
from typing import List, Tuple
from PIL import Image, ImageFilter, ImageEnhance, ImageOps

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}


def setup_logging(config: dict):
    """Configure logging from config."""
    log_config = config.get('logging', {})
    level = getattr(logging, log_config.get('level', 'INFO').upper())
    log_file = log_config.get('file', '')

    handlers = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )


logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def scan_photos(source_dir: str, recursive: bool = True) -> List[Path]:
    """Scan source directory for image files."""
    source = Path(source_dir)
    if not source.exists():
        logger.error(f"Source directory does not exist: {source_dir}")
        return []

    photos = []
    iterator = source.rglob('*') if recursive else source.glob('*')

    for path in iterator:
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            photos.append(path)

    logger.info(f"Found {len(photos)} photos in {source_dir}")
    return photos


class PhotoDatabase:
    """SQLite database for tracking photo selection state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                times_selected INTEGER DEFAULT 0,
                last_selected_week TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time TEXT NOT NULL,
                photos_scanned INTEGER,
                photos_selected INTEGER,
                photos_processed INTEGER,
                success INTEGER
            )
        ''')
        self.conn.commit()

    def update_photos(self, photo_paths: List[Path]):
        """Update database with current photo inventory."""
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()

        for path in photo_paths:
            cursor.execute('''
                INSERT INTO photos (file_path, first_seen, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET last_seen = ?
            ''', (str(path), now, now, now))

        self.conn.commit()
        logger.info(f"Updated {len(photo_paths)} photos in database")

    def get_weighted_selection(self, count: int, max_selections: int = 10) -> List[str]:
        """Select photos with weighted random, favoring less-shown ones."""
        cursor = self.conn.cursor()
        current_week = datetime.now().strftime('%G-W%V')

        cursor.execute('''
            SELECT file_path, times_selected, last_selected_week
            FROM photos
            WHERE times_selected < ?
        ''', (max_selections,))

        rows = cursor.fetchall()
        if not rows:
            logger.info("All photos hit selection limit, resetting counts")
            cursor.execute('UPDATE photos SET times_selected = 0')
            self.conn.commit()
            cursor.execute(
                'SELECT file_path, times_selected, last_selected_week FROM photos'
            )
            rows = cursor.fetchall()

        if not rows:
            return []

        # Build weighted list
        weighted = []
        for row in rows:
            weight = max(1, max_selections - (row['times_selected'] or 0))
            if row['last_selected_week'] != current_week:
                weight *= 2
            weighted.append((row['file_path'], weight))

        # Weighted selection without replacement
        selected = []
        total_weight = sum(w for _, w in weighted)

        for _ in range(min(count, len(weighted))):
            if total_weight <= 0:
                break
            r = random.random() * total_weight
            cumsum = 0
            for idx, (path, weight) in enumerate(weighted):
                cumsum += weight
                if cumsum >= r:
                    selected.append(path)
                    total_weight -= weight
                    weighted.pop(idx)
                    break

        logger.info(f"Selected {len(selected)} from {len(rows)} eligible photos")
        return selected

    def mark_selected(self, paths: List[str]):
        cursor = self.conn.cursor()
        current_week = datetime.now().strftime('%G-W%V')
        for path in paths:
            cursor.execute('''
                UPDATE photos
                SET times_selected = times_selected + 1, last_selected_week = ?
                WHERE file_path = ?
            ''', (current_week, path))
        self.conn.commit()

    def record_run(self, scanned: int, selected: int, processed: int, success: bool):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO runs (run_time, photos_scanned, photos_selected, photos_processed, success)
            VALUES (?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), scanned, selected, processed, int(success)))
        self.conn.commit()

    def close(self):
        self.conn.close()


def create_blur_fill(image: Image.Image, target_w: int, target_h: int,
                     blur_radius: int = 40, darken: float = 0.6) -> Image.Image:
    """
    Create image at target dimensions with blur-filled background.

    If the source image doesn't match the target aspect ratio, the sharp
    image is centered over a blurred+darkened version of itself that covers
    the full canvas.  When the aspect ratios nearly match (>95 % fill) the
    image is simply scaled to cover with a slight center crop.
    """
    img_w, img_h = image.size

    # How the image fits (letterbox) vs covers (crop) the canvas
    scale_fit = min(target_w / img_w, target_h / img_h)
    fit_w = int(img_w * scale_fit)
    fit_h = int(img_h * scale_fit)

    # If the image nearly fills the canvas, just scale-to-cover and crop
    if fit_w >= target_w * 0.95 and fit_h >= target_h * 0.95:
        scale_cover = max(target_w / img_w, target_h / img_h)
        cover_w = int(img_w * scale_cover)
        cover_h = int(img_h * scale_cover)
        result = image.resize((cover_w, cover_h), Image.LANCZOS)
        left = (cover_w - target_w) // 2
        top = (cover_h - target_h) // 2
        return result.crop((left, top, left + target_w, top + target_h))

    # --- Blurred background ---
    scale_cover = max(target_w / img_w, target_h / img_h)
    cover_w = int(img_w * scale_cover)
    cover_h = int(img_h * scale_cover)

    bg = image.resize((cover_w, cover_h), Image.LANCZOS)
    left = (cover_w - target_w) // 2
    top = (cover_h - target_h) // 2
    bg = bg.crop((left, top, left + target_w, top + target_h))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    if darken < 1.0:
        bg = ImageEnhance.Brightness(bg).enhance(darken)

    # --- Sharp foreground ---
    fg = image.resize((fit_w, fit_h), Image.LANCZOS)

    x = (target_w - fit_w) // 2
    y = (target_h - fit_h) // 2
    bg.paste(fg, (x, y))

    return bg


def process_photo(source_path: Path, output_dir: Path, index: int,
                  h_size: Tuple[int, int], v_size: Tuple[int, int],
                  blur_radius: int, blur_darken: float,
                  quality: int) -> bool:
    """Process a single photo into horizontal and vertical versions."""
    try:
        image = Image.open(source_path)
        image = ImageOps.exif_transpose(image)

        if image.mode == 'RGBA':
            bg = Image.new('RGB', image.size, (0, 0, 0))
            bg.paste(image, mask=image.split()[3])
            image = bg
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        stem = source_path.stem
        filename = f"{index:04d}_{stem}.jpg"

        # Horizontal version
        h_img = create_blur_fill(image, *h_size, blur_radius, blur_darken)
        h_path = output_dir / 'horizontal' / filename
        h_img.save(h_path, 'JPEG', quality=quality, optimize=True)

        # Vertical version
        v_img = create_blur_fill(image, *v_size, blur_radius, blur_darken)
        v_path = output_dir / 'vertical' / filename
        v_img.save(v_path, 'JPEG', quality=quality, optimize=True)

        logger.debug(f"Processed: {source_path.name} -> {filename}")
        return True

    except Exception as e:
        logger.error(f"Failed to process {source_path}: {e}")
        return False


def clean_output(output_dir: Path):
    """Remove old photos from output directories."""
    for subdir in ('horizontal', 'vertical'):
        d = output_dir / subdir
        if d.exists():
            count = 0
            for f in d.glob('*'):
                if f.is_file():
                    f.unlink()
                    count += 1
            logger.info(f"Cleaned {count} files from {subdir}/")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.yaml'
    )
    config = load_config(config_path)
    setup_logging(config)

    source_config = config['source']
    selection_config = config['selection']
    output_config = config['output']

    # Setup output directories
    output_dir = Path(output_config['dir'])
    (output_dir / 'horizontal').mkdir(parents=True, exist_ok=True)
    (output_dir / 'vertical').mkdir(parents=True, exist_ok=True)

    # Scan for photos
    photos = scan_photos(
        source_config['photos_dir'],
        recursive=source_config.get('recursive', True),
    )
    if not photos:
        logger.error("No photos found")
        sys.exit(1)

    # Initialize database
    db = PhotoDatabase(selection_config['state_db'])

    try:
        db.update_photos(photos)

        selected = db.get_weighted_selection(
            count=selection_config['photos_per_week'],
            max_selections=selection_config.get('max_show_count', 10),
        )

        if not selected:
            logger.error("No photos selected")
            db.record_run(len(photos), 0, 0, False)
            return

        # Clean old output
        clean_output(output_dir)

        # Processing parameters
        h_size = (output_config['horizontal']['width'],
                  output_config['horizontal']['height'])
        v_size = (output_config['vertical']['width'],
                  output_config['vertical']['height'])
        blur_radius = output_config.get('blur_radius', 40)
        blur_darken = output_config.get('blur_darken', 0.6)
        quality = output_config.get('quality', 85)

        processed = 0
        total = len(selected)
        t_start = time.time()

        for idx, path_str in enumerate(selected):
            path = Path(path_str)
            if not path.exists():
                logger.warning(f"File no longer exists: {path}")
                continue

            if process_photo(path, output_dir, idx, h_size, v_size,
                             blur_radius, blur_darken, quality):
                processed += 1

            if (idx + 1) % 20 == 0:
                elapsed = time.time() - t_start
                per_photo = elapsed / (idx + 1)
                remaining = per_photo * (total - idx - 1)
                logger.info(
                    f"Progress: {idx + 1}/{total} "
                    f"({processed} OK, ~{remaining:.0f}s remaining)"
                )

        db.mark_selected(selected)
        db.record_run(len(photos), len(selected), processed, True)

        elapsed = time.time() - t_start
        logger.info(
            f"Done: {processed}/{total} photos processed in {elapsed:.1f}s"
        )

    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        db.record_run(len(photos), 0, 0, False)
        raise
    finally:
        db.close()


if __name__ == '__main__':
    main()
