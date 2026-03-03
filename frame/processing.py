"""Image processing: blur-fill backgrounds, subprocess execution."""

import os
import gc
import time
import json
import logging
import multiprocessing
from pathlib import Path
from typing import Tuple, Optional

from PIL import Image, ImageFilter, ImageEnhance, ImageOps

logger = logging.getLogger(__name__)


def create_blur_fill(image: Image.Image, target_w: int, target_h: int,
                     blur_radius: int = 40, darken: float = 0.6) -> Image.Image:
    """Create image at target dimensions with blur-filled background.

    Same-orientation: scale to cover + center crop.
    Cross-orientation: sharp image centered over blurred+darkened background.
    """
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

    if darken < 1.0:
        bg = ImageEnhance.Brightness(bg).enhance(darken)

    fg = image.resize((fit_w, fit_h), Image.LANCZOS)

    x = (target_w - fit_w) // 2
    y = (target_h - fit_h) // 2
    bg.paste(fg, (x, y))

    return bg


def process_photo(source_path: Path, output_dir: Path,
                  item_id, filename: str,
                  h_size: Tuple[int, int], v_size: Tuple[int, int],
                  blur_radius: int, blur_darken: float,
                  quality: int,
                  orientations: Tuple[str, ...] = ('horizontal', 'vertical'),
                  ) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Process a single photo into the requested orientations.

    Returns (h_filename, v_filename) on success — either may be None if that
    orientation was not requested.  Returns None on failure.
    """
    try:
        image = Image.open(source_path)
        image = ImageOps.exif_transpose(image)

        if image.mode == 'RGBA':
            bg = Image.new('RGB', image.size, (0, 0, 0))
            bg.paste(image, mask=image.split()[3])
            image = bg
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        # Cap source resolution to save memory — output is at most 1920x1200,
        # so anything beyond ~2560px on longest side is wasted.
        MAX_SRC_DIM = 2560
        if max(image.size) > MAX_SRC_DIM:
            image.thumbnail((MAX_SRC_DIM, MAX_SRC_DIM), Image.LANCZOS)

        stem = Path(filename).stem
        out_name = f"{item_id}_{stem}.jpg"
        h_fn = v_fn = None

        if 'horizontal' in orientations:
            h_path = output_dir / 'horizontal' / out_name
            h_img = create_blur_fill(image, *h_size, blur_radius, blur_darken)
            h_img.save(h_path, 'JPEG', quality=quality, optimize=True)
            del h_img
            h_fn = out_name

        if 'vertical' in orientations:
            v_path = output_dir / 'vertical' / out_name
            v_img = create_blur_fill(image, *v_size, blur_radius, blur_darken)
            v_img.save(v_path, 'JPEG', quality=quality, optimize=True)
            del v_img
            v_fn = out_name

        del image
        gc.collect()
        logger.debug(f"Processed: {filename} -> {out_name} ({', '.join(orientations)})")
        return (h_fn, v_fn)

    except Exception as e:
        logger.error(f"Failed to process {source_path}: {e}")
        return None


def _process_photo_worker(source_path, output_dir, item_id, filename,
                          h_size, v_size, blur_radius, blur_darken,
                          quality, orientations, result_file):
    """Subprocess target: process one photo, write result to a temp file.

    Runs in a separate process so ALL memory (PIL buffers, intermediates)
    is returned to the OS when the process exits — critical on Pi Zero 2W
    with only 425 MB RAM.
    """
    # Tell OOM killer to prefer this process over Chromium/server
    try:
        with open('/proc/self/oom_score_adj', 'w') as f:
            f.write('800')
    except OSError:
        pass
    result = process_photo(
        Path(source_path), Path(output_dir), item_id, filename,
        h_size, v_size, blur_radius, blur_darken, quality,
        orientations=orientations,
    )
    with open(result_file, 'w') as f:
        json.dump(result, f)


def _wait_for_memory(min_mb=80, max_wait=60):
    """Wait until enough memory is available before spawning a subprocess."""
    for _ in range(max_wait):
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        avail_kb = int(line.split()[1])
                        if avail_kb >= min_mb * 1024:
                            return True
                        break
        except OSError:
            return True
        time.sleep(1)
    return False


def process_photo_in_subprocess(source_path, output_dir, item_id, filename,
                                h_size, v_size, blur_radius, blur_darken,
                                quality, orientations, timeout=120):
    """Run process_photo in a child process to guarantee memory cleanup."""
    _wait_for_memory(min_mb=80)
    result_file = Path('/tmp') / f'frame_proc_{os.getpid()}_{item_id}.json'
    try:
        p = multiprocessing.Process(
            target=_process_photo_worker,
            args=(str(source_path), str(output_dir), item_id, filename,
                  h_size, v_size, blur_radius, blur_darken,
                  quality, orientations, str(result_file)),
        )
        p.start()
        p.join(timeout=timeout)
        if p.is_alive():
            p.kill()
            p.join()
            logger.error(f"Photo processing timed out for {filename}")
            return None
        if p.exitcode != 0:
            logger.error(f"Photo processing failed for {filename} (exit {p.exitcode})")
            return None
        if result_file.exists():
            with open(result_file) as f:
                data = json.load(f)
            return tuple(data) if data else None
        return None
    except Exception as e:
        logger.error(f"Subprocess error for {filename}: {e}")
        return None
    finally:
        result_file.unlink(missing_ok=True)
