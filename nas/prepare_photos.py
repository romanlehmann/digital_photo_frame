#!/usr/bin/env python3
"""
Photo preparation script for digital photo frame.
Runs on Synology NAS to select and process photos weekly.

Connects to Synology Photos via a share link, selects 200 photos with
weighted random, downloads the originals, and generates two versions:
- horizontal/ (1920x1200) - landscape orientation with blur-fill if needed
- vertical/   (1200x1920) - portrait orientation with blur-fill if needed
"""

import os
import sys
import sqlite3
import logging
import random
import time
import shutil
import yaml
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlparse
from PIL import Image, ImageFilter, ImageEnhance, ImageOps


def setup_logging(config: dict):
    """Configure logging from config."""
    log_config = config.get('logging', {})
    level = getattr(logging, log_config.get('level', 'DEBUG').upper())
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


# ---------------------------------------------------------------------------
# Synology Photos API client
# ---------------------------------------------------------------------------

class SynologyPhotosClient:
    """Client for Synology Photos API via public share links.

    Uses localhost API access to avoid reverse proxy cookie issues.
    """

    def __init__(self, share_url: str, passphrase: str,
                 local_api_base: str = 'https://localhost:5443'):
        self.share_url = share_url
        self.passphrase = passphrase
        # External URL for the reverse proxy
        parsed = urlparse(share_url)
        self.external_base = f"{parsed.scheme}://{parsed.netloc}"
        # Local URL for direct NAS access (bypasses Cloudflare cookie stripping)
        self.local_base = local_api_base.rstrip('/')
        self.session = requests.Session()
        self.session.verify = False  # localhost cert won't match
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        })
        self.share_token = self._extract_share_token(share_url)
        logger.info(f"Initialized client: token={self.share_token}")

    def _extract_share_token(self, share_url: str) -> str:
        parsed = urlparse(share_url)
        path_parts = parsed.path.strip('/').split('/')
        if 'sharing' in path_parts:
            idx = path_parts.index('sharing')
            if idx + 1 < len(path_parts):
                return path_parts[idx + 1]
        raise ValueError(f"Could not extract share token from: {share_url}")

    def _local_api_url(self, api_name: str) -> str:
        """Build the localhost API URL (bypasses Cloudflare)."""
        return f"{self.local_base}/webapi/entry.cgi/{api_name}"

    def _external_api_url(self, api_name: str) -> str:
        """Build the external API URL."""
        return f"{self.external_base}/webapi/entry.cgi/{api_name}"

    def initialize_share(self) -> bool:
        """Log in to the shared album to obtain a sharing_sid cookie.

        Uses localhost to bypass Cloudflare's Set-Cookie stripping.
        Flow: sharing login -> passphrase auth (if password-protected).
        """
        try:
            logger.info("Initializing share session...")
            api_url = f"{self.local_base}/webapi/entry.cgi"

            # Step 1: Log in to the share (do NOT set x-syno-sharing yet)
            login_data = {
                'api': 'SYNO.Core.Sharing.Login',
                'method': 'login',
                'version': 1,
                'sharing_id': self.share_token,
                'password': self.passphrase or '',
            }
            resp = self.session.post(api_url, data=login_data, timeout=10)
            result = resp.json()
            logger.info(f"Sharing login: {result}")
            logger.debug(f"Cookies after sharing login: {dict(self.session.cookies)}")

            if not result.get('success'):
                logger.error(f"Sharing login failed: {result}")
                return False

            # Now set the sharing header for all subsequent API calls
            self.session.headers['x-syno-sharing'] = self.share_token
            return True
        except Exception as e:
            logger.error(f"Failed to initialize share: {e}")
            return False

    def list_items(self, offset: int = 0, limit: int = 100) -> Optional[Dict[str, Any]]:
        """List items in the shared album."""
        api = 'SYNO.Foto.Browse.Item'
        data = {
            'api': api,
            'method': 'list',
            'version': 1,
            'offset': offset,
            'limit': limit,
        }
        try:
            resp = self.session.post(self._local_api_url(api), data=data)
            resp.raise_for_status()
            result = resp.json()
            if result.get('success'):
                return result.get('data', {})
            logger.error(f"list_items failed: {result}")
            return None
        except Exception as e:
            logger.error(f"list_items exception: {e}")
            return None

    def get_all_items(self) -> List[Dict[str, Any]]:
        """Get all photo items from the shared album with pagination."""
        all_items = []
        offset = 0
        limit = 100

        while True:
            data = self.list_items(offset=offset, limit=limit)
            if not data:
                break

            items = data.get('list', [])
            if not items:
                break

            for item in items:
                if item.get('type') in ('photo', 'video') or 'filename' in item:
                    all_items.append(item)

            logger.info(
                f"Fetched {len(items)} items (offset={offset}), "
                f"photos so far: {len(all_items)}"
            )

            if len(items) < limit:
                break

            offset += limit
            time.sleep(0.5)

        logger.info(f"Total photos fetched: {len(all_items)}")
        return all_items

    def download_item(self, item_id: int, output_path: Path) -> bool:
        """Download a single item to the specified path."""
        api = 'SYNO.Foto.Download'
        url = self._local_api_url(api)
        try:
            data = {
                'api': api,
                'method': 'download',
                'version': 1,
                'unit_id': f'[{item_id}]',
                'force_download': 'true',
            }
            resp = self.session.post(url, data=data, stream=True)
            resp.raise_for_status()

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            return True
        except Exception as e:
            logger.error(f"Failed to download item {item_id}: {e}")
            return False


# ---------------------------------------------------------------------------
# Photo state database
# ---------------------------------------------------------------------------

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
                item_id INTEGER UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                filesize INTEGER,
                taken_time INTEGER,
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

    def update_items(self, items: List[Dict[str, Any]]):
        """Update database with items from the API."""
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()

        for item in items:
            cursor.execute('''
                INSERT INTO photos (item_id, filename, filesize, taken_time,
                                    first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    filename = excluded.filename,
                    filesize = excluded.filesize
            ''', (
                item['id'],
                item.get('filename', ''),
                item.get('filesize', 0),
                item.get('time', 0),
                now,
                now,
            ))

        self.conn.commit()
        logger.info(f"Updated {len(items)} items in database")

    def get_weighted_selection(self, count: int, max_selections: int = 10) -> List[int]:
        """Select item IDs with weighted random, favoring less-shown ones."""
        cursor = self.conn.cursor()
        current_week = datetime.now().strftime('%G-W%V')

        cursor.execute('''
            SELECT item_id, times_selected, last_selected_week
            FROM photos
            WHERE times_selected < ?
        ''', (max_selections,))

        rows = cursor.fetchall()
        if not rows:
            logger.info("All photos hit selection limit, resetting counts")
            cursor.execute('UPDATE photos SET times_selected = 0')
            self.conn.commit()
            cursor.execute(
                'SELECT item_id, times_selected, last_selected_week FROM photos'
            )
            rows = cursor.fetchall()

        if not rows:
            return []

        weighted = []
        for row in rows:
            weight = max(1, max_selections - (row['times_selected'] or 0))
            if row['last_selected_week'] != current_week:
                weight *= 2
            weighted.append((row['item_id'], weight))

        selected = []
        total_weight = sum(w for _, w in weighted)

        for _ in range(min(count, len(weighted))):
            if total_weight <= 0:
                break
            r = random.random() * total_weight
            cumsum = 0
            for idx, (item_id, weight) in enumerate(weighted):
                cumsum += weight
                if cumsum >= r:
                    selected.append(item_id)
                    total_weight -= weight
                    weighted.pop(idx)
                    break

        logger.info(f"Selected {len(selected)} from {len(rows)} eligible photos")
        return selected

    def get_filename(self, item_id: int) -> str:
        """Get the filename for an item ID."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT filename FROM photos WHERE item_id = ?', (item_id,))
        row = cursor.fetchone()
        return row['filename'] if row else f'photo_{item_id}.jpg'

    def mark_selected(self, item_ids: List[int]):
        cursor = self.conn.cursor()
        current_week = datetime.now().strftime('%G-W%V')
        for item_id in item_ids:
            cursor.execute('''
                UPDATE photos
                SET times_selected = times_selected + 1, last_selected_week = ?
                WHERE item_id = ?
            ''', (current_week, item_id))
        self.conn.commit()

    def record_run(self, scanned: int, selected: int, processed: int, success: bool):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO runs (run_time, photos_scanned, photos_selected,
                              photos_processed, success)
            VALUES (?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), scanned, selected, processed,
              int(success)))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

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

    scale_fit = min(target_w / img_w, target_h / img_h)
    fit_w = int(img_w * scale_fit)
    fit_h = int(img_h * scale_fit)

    if fit_w >= target_w * 0.95 and fit_h >= target_h * 0.95:
        scale_cover = max(target_w / img_w, target_h / img_h)
        cover_w = int(img_w * scale_cover)
        cover_h = int(img_h * scale_cover)
        result = image.resize((cover_w, cover_h), Image.LANCZOS)
        left = (cover_w - target_w) // 2
        top = (cover_h - target_h) // 2
        return result.crop((left, top, left + target_w, top + target_h))

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


def process_photo(source_path: Path, output_dir: Path, index: int,
                  filename: str,
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

        stem = Path(filename).stem
        out_name = f"{index:04d}_{stem}.jpg"

        h_img = create_blur_fill(image, *h_size, blur_radius, blur_darken)
        h_img.save(output_dir / 'horizontal' / out_name,
                   'JPEG', quality=quality, optimize=True)

        v_img = create_blur_fill(image, *v_size, blur_radius, blur_darken)
        v_img.save(output_dir / 'vertical' / out_name,
                   'JPEG', quality=quality, optimize=True)

        logger.debug(f"Processed: {filename} -> {out_name}")
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.yaml'
    )
    config = load_config(config_path)
    setup_logging(config)

    synology_config = config['synology']
    selection_config = config['selection']
    output_config = config['output']

    # Setup output directories
    output_dir = Path(output_config['dir'])
    (output_dir / 'horizontal').mkdir(parents=True, exist_ok=True)
    (output_dir / 'vertical').mkdir(parents=True, exist_ok=True)

    # Temp directory for downloaded originals
    tmp_dir = Path(output_config.get('tmp_dir', '/tmp/frame_downloads'))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Support both single and multi-album config formats
    share_urls = synology_config.get('share_urls',
                                     [synology_config.get('share_url', '')])
    share_passphrases = synology_config.get('share_passphrases',
                                            [synology_config.get('share_passphrase', '')])

    if len(share_urls) != len(share_passphrases):
        logger.error("share_urls and share_passphrases must have the same length")
        sys.exit(1)

    # Filter out empty entries
    albums = [(url, pw) for url, pw in zip(share_urls, share_passphrases)
              if url]

    if not albums:
        logger.error("No share URLs configured")
        sys.exit(1)

    # Initialize database
    db = PhotoDatabase(selection_config['state_db'])

    try:
        # Fetch items from all configured albums
        all_items = []
        # Map item_id -> client index for downloading
        item_client_map = {}
        clients = []

        for album_idx, (share_url, passphrase) in enumerate(albums):
            logger.info(f"Connecting to album {album_idx + 1}/{len(albums)}...")
            client = SynologyPhotosClient(
                share_url=share_url,
                passphrase=passphrase,
                local_api_base=synology_config.get(
                    'local_api_base', 'https://localhost:5443'),
            )

            if not client.initialize_share():
                logger.error(f"Failed to initialize album {album_idx + 1}, skipping")
                continue

            items = client.get_all_items()
            logger.info(f"Album {album_idx + 1}: {len(items)} photos")

            for item in items:
                item_client_map[item['id']] = len(clients)
            all_items.extend(items)
            clients.append(client)

        if not all_items:
            raise Exception("No photos found in any album")

        logger.info(f"Total photos across all albums: {len(all_items)}")

        # Update database with album contents
        db.update_items(all_items)

        # Select photos for this week
        selected_ids = db.get_weighted_selection(
            count=selection_config['photos_per_week'],
            max_selections=selection_config.get('max_show_count', 10),
        )

        if not selected_ids:
            logger.error("No photos selected")
            db.record_run(len(all_items), 0, 0, False)
            return

        # Clean old output and temp files
        clean_output(output_dir)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Processing parameters
        h_size = (output_config['horizontal']['width'],
                  output_config['horizontal']['height'])
        v_size = (output_config['vertical']['width'],
                  output_config['vertical']['height'])
        blur_radius = output_config.get('blur_radius', 40)
        blur_darken = output_config.get('blur_darken', 0.6)
        quality = output_config.get('quality', 85)

        # Download and process each selected photo
        processed = 0
        total = len(selected_ids)
        t_start = time.time()

        for idx, item_id in enumerate(selected_ids):
            filename = db.get_filename(item_id)
            download_path = tmp_dir / filename

            # Find the right client for this item
            client_idx = item_client_map.get(item_id)
            if client_idx is None:
                logger.warning(f"Skipping item {item_id}: no client found")
                continue
            client = clients[client_idx]

            # Download original
            if not client.download_item(item_id, download_path):
                logger.warning(f"Skipping item {item_id}: download failed")
                continue

            # Process into horizontal + vertical
            if process_photo(download_path, output_dir, idx, filename,
                             h_size, v_size, blur_radius, blur_darken,
                             quality):
                processed += 1

            # Clean up downloaded original to save disk space
            download_path.unlink(missing_ok=True)

            if (idx + 1) % 20 == 0:
                elapsed = time.time() - t_start
                per_photo = elapsed / (idx + 1)
                remaining = per_photo * (total - idx - 1)
                logger.info(
                    f"Progress: {idx + 1}/{total} "
                    f"({processed} OK, ~{remaining:.0f}s remaining)"
                )

            time.sleep(0.2)  # Rate limiting

        db.mark_selected(selected_ids)
        db.record_run(len(all_items), len(selected_ids), processed, True)

        elapsed = time.time() - t_start
        logger.info(
            f"Done: {processed}/{total} photos processed in {elapsed:.1f}s"
        )

    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        db.record_run(0, 0, 0, False)
        raise
    finally:
        # Clean up temp directory
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        db.close()


if __name__ == '__main__':
    main()
