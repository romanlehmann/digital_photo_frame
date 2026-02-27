#!/usr/bin/env python3
"""
Photo sync for digital photo frame — self-contained Pi version.

Connects directly to Synology Photos via share links, downloads ALL album
photos, and processes them into horizontal + vertical orientations with
blur-fill backgrounds.  Tracks state in a local SQLite database so only
new/changed photos are downloaded on subsequent runs.

Can run standalone:  python photo_sync.py config_frame.yaml
Or be imported and driven by viewer_server.py (boot sync, sleep sync).
"""

import os
import sys
import sqlite3
import logging
import time
import shutil
import threading
import yaml
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlparse
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synology Photos API client
# ---------------------------------------------------------------------------

class SynologyPhotosClient:
    """Client for Synology Photos API via public share links."""

    def __init__(self, share_url: str, passphrase: str,
                 local_api_base: str = 'https://100.101.43.67:5443'):
        self.share_url = share_url
        self.passphrase = passphrase
        parsed = urlparse(share_url)
        self.external_base = f"{parsed.scheme}://{parsed.netloc}"
        self.local_base = local_api_base.rstrip('/')
        self.session = requests.Session()
        self.session.verify = False
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
        return f"{self.local_base}/webapi/entry.cgi/{api_name}"

    def initialize_share(self) -> bool:
        """Log in to the shared album to obtain a sharing_sid cookie."""
        try:
            logger.info("Initializing share session...")
            api_url = f"{self.local_base}/webapi/entry.cgi"

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

            if not result.get('success'):
                logger.error(f"Sharing login failed: {result}")
                return False

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

            VIDEO_EXTS = {'.mov', '.mp4', '.avi', '.mkv', '.wmv', '.m4v'}
            for item in items:
                filename = item.get('filename', '')
                ext = os.path.splitext(filename)[1].lower()
                if ext in VIDEO_EXTS:
                    continue
                if item.get('type') == 'video':
                    continue
                if item.get('type') == 'photo' or 'filename' in item:
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
    """SQLite database for tracking photo sync state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS photos (
                item_id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL,
                filesize INTEGER,
                taken_time INTEGER,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                downloaded INTEGER DEFAULT 0,
                download_failed INTEGER DEFAULT 0,
                h_filename TEXT,
                v_filename TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time TEXT NOT NULL,
                photos_scanned INTEGER,
                downloaded INTEGER,
                processed INTEGER,
                success INTEGER
            )
        ''')
        self.conn.commit()

    def update_items(self, items: List[Dict[str, Any]]) -> List[int]:
        """Update database with items from the API.

        Returns list of item_ids that were removed (stale).
        """
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

        # Remove entries no longer in the album
        item_ids = {item['id'] for item in items}
        cursor.execute('SELECT item_id FROM photos')
        db_ids = {row['item_id'] for row in cursor.fetchall()}
        stale_ids = list(db_ids - item_ids)
        if stale_ids:
            cursor.executemany(
                'DELETE FROM photos WHERE item_id = ?',
                [(i,) for i in stale_ids],
            )
            logger.info(f"Removed {len(stale_ids)} stale entries from database")

        self.conn.commit()
        logger.info(f"Updated {len(items)} items in database")
        return stale_ids

    def get_undownloaded(self) -> List[Dict[str, Any]]:
        """Get items that haven't been downloaded yet (with < 3 failures)."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT item_id, filename, filesize
            FROM photos
            WHERE downloaded = 0 AND download_failed < 3
        ''')
        return [dict(row) for row in cursor.fetchall()]

    def mark_downloaded(self, item_id: int, h_filename: str, v_filename: str):
        """Mark an item as successfully downloaded and processed."""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE photos
            SET downloaded = 1, h_filename = ?, v_filename = ?
            WHERE item_id = ?
        ''', (h_filename, v_filename, item_id))
        self.conn.commit()

    def mark_failed(self, item_id: int):
        """Increment the failure counter for an item."""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE photos
            SET download_failed = download_failed + 1
            WHERE item_id = ?
        ''', (item_id,))
        self.conn.commit()

    def get_filenames_for_stale(self, item_ids: List[int]) -> List[Tuple[str, str]]:
        """Get h_filename, v_filename for stale items (for cleanup)."""
        if not item_ids:
            return []
        cursor = self.conn.cursor()
        placeholders = ','.join('?' * len(item_ids))
        cursor.execute(f'''
            SELECT h_filename, v_filename FROM photos
            WHERE item_id IN ({placeholders})
        ''', item_ids)
        return [(row['h_filename'], row['v_filename']) for row in cursor.fetchall()]

    def get_counts(self) -> Dict[str, int]:
        """Get photo counts for status display."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) as total FROM photos')
        total = cursor.fetchone()['total']
        cursor.execute('SELECT COUNT(*) as done FROM photos WHERE downloaded = 1')
        done = cursor.fetchone()['done']
        cursor.execute('SELECT COUNT(*) as pending FROM photos WHERE downloaded = 0 AND download_failed < 3')
        pending = cursor.fetchone()['pending']
        return {'total': total, 'downloaded': done, 'pending': pending}

    def get_last_run(self) -> Optional[Dict[str, Any]]:
        """Get the most recent sync run info."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        return dict(row) if row else None

    def record_run(self, scanned: int, downloaded: int, processed: int, success: bool):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sync_runs (run_time, photos_scanned, downloaded, processed, success)
            VALUES (?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), scanned, downloaded, processed, int(success)))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

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
                  item_id: int, filename: str,
                  h_size: Tuple[int, int], v_size: Tuple[int, int],
                  blur_radius: int, blur_darken: float,
                  quality: int) -> Optional[Tuple[str, str]]:
    """Process a single photo into horizontal and vertical versions.

    Returns (h_filename, v_filename) on success, None on failure.
    Uses {item_id}_{stem}.jpg as filename for stability across syncs.
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

        stem = Path(filename).stem
        out_name = f"{item_id}_{stem}.jpg"

        h_path = output_dir / 'horizontal' / out_name
        h_img = create_blur_fill(image, *h_size, blur_radius, blur_darken)
        h_img.save(h_path, 'JPEG', quality=quality, optimize=True)
        del h_img

        v_path = output_dir / 'vertical' / out_name
        v_img = create_blur_fill(image, *v_size, blur_radius, blur_darken)
        v_img.save(v_path, 'JPEG', quality=quality, optimize=True)
        del v_img

        del image
        logger.debug(f"Processed: {filename} -> {out_name}")
        return (out_name, out_name)

    except Exception as e:
        logger.error(f"Failed to process {source_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Photo syncer (thread-safe, stoppable)
# ---------------------------------------------------------------------------

class PhotoSyncer:
    """Manages photo sync from Synology Photos to local storage."""

    def __init__(self, config: dict):
        self._config = config
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._running = False
        self._phase = 'idle'
        self._progress = {}
        self._last_error = None
        self._thread = None

    def run_sync(self):
        """Start a sync in a background thread. No-op if already running."""
        with self._lock:
            if self._running:
                logger.info("Sync already running, skipping")
                return
            self._running = True
            self._stop_event.clear()
            self._last_error = None

        self._thread = threading.Thread(target=self._sync_worker, daemon=True)
        self._thread.start()

    def stop(self):
        """Request the running sync to stop."""
        self._stop_event.set()

    def get_status(self) -> dict:
        """Return current sync status."""
        with self._lock:
            photos_config = self._config.get('photos', {})
            base_dir = Path(photos_config.get('base_dir', '/srv/frame/photos'))

            h_count = len(list((base_dir / 'horizontal').glob('*.jpg'))) if (base_dir / 'horizontal').exists() else 0
            v_count = len(list((base_dir / 'vertical').glob('*.jpg'))) if (base_dir / 'vertical').exists() else 0

            status = {
                'running': self._running,
                'phase': self._phase,
                'h_photos': h_count,
                'v_photos': v_count,
                'error': self._last_error,
            }
            status.update(self._progress)
            return status

    def _sync_worker(self):
        """Main sync logic — runs in background thread."""
        synology_config = self._config.get('synology', {})
        photos_config = self._config.get('photos', {})

        base_dir = Path(photos_config.get('base_dir', '/srv/frame/photos'))
        db_path = photos_config.get('state_db', str(base_dir / 'state.db'))
        tmp_dir = Path(photos_config.get('tmp_dir', '/tmp/frame_downloads'))

        h_size = (photos_config.get('horizontal', {}).get('width', 1920),
                  photos_config.get('horizontal', {}).get('height', 1200))
        v_size = (photos_config.get('vertical', {}).get('width', 1200),
                  photos_config.get('vertical', {}).get('height', 1920))
        blur_radius = photos_config.get('blur_radius', 40)
        blur_darken = photos_config.get('blur_darken', 0.6)
        quality = photos_config.get('quality', 85)

        # Ensure directories
        (base_dir / 'horizontal').mkdir(parents=True, exist_ok=True)
        (base_dir / 'vertical').mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        db = PhotoDatabase(db_path)

        try:
            # --- Phase 1: Connect to albums and list all photos ---
            self._set_phase('listing')
            share_urls = synology_config.get('share_urls', [])
            share_passphrases = synology_config.get('share_passphrases', [])

            if len(share_urls) != len(share_passphrases):
                raise ValueError("share_urls and share_passphrases must have the same length")

            albums = [(url, pw) for url, pw in zip(share_urls, share_passphrases) if url]
            if not albums:
                raise ValueError("No share URLs configured")

            all_items = []
            item_client_map = {}
            clients = []

            for album_idx, (share_url, passphrase) in enumerate(albums):
                if self._stop_event.is_set():
                    logger.info("Sync stopped by request")
                    return

                logger.info(f"Connecting to album {album_idx + 1}/{len(albums)}...")
                client = SynologyPhotosClient(
                    share_url=share_url,
                    passphrase=passphrase,
                    local_api_base=synology_config.get('local_api_base', 'https://100.101.43.67:5443'),
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
                raise ValueError("No photos found in any album")

            logger.info(f"Total photos across all albums: {len(all_items)}")

            # --- Phase 2: Update database, clean stale files ---
            self._set_phase('updating_db')
            stale_ids = db.update_items(all_items)

            # Clean up processed files for removed photos
            if stale_ids:
                stale_files = db.get_filenames_for_stale(stale_ids)
                for h_fn, v_fn in stale_files:
                    if h_fn:
                        (base_dir / 'horizontal' / h_fn).unlink(missing_ok=True)
                    if v_fn:
                        (base_dir / 'vertical' / v_fn).unlink(missing_ok=True)
                logger.info(f"Cleaned {len(stale_files)} stale photo files")

            # --- Phase 3: Download and process new photos ---
            self._set_phase('downloading')
            undownloaded = db.get_undownloaded()
            total = len(undownloaded)
            downloaded = 0
            processed = 0
            t_start = time.time()

            self._progress = {'total': len(all_items), 'pending': total, 'downloaded': 0}

            for idx, item in enumerate(undownloaded):
                if self._stop_event.is_set():
                    logger.info("Sync stopped by request")
                    break

                item_id = item['item_id']
                filename = item['filename']
                download_path = tmp_dir / filename

                client_idx = item_client_map.get(item_id)
                if client_idx is None:
                    logger.warning(f"Skipping item {item_id}: no client found")
                    db.mark_failed(item_id)
                    continue

                client = clients[client_idx]

                if not client.download_item(item_id, download_path):
                    logger.warning(f"Skipping item {item_id}: download failed")
                    db.mark_failed(item_id)
                    download_path.unlink(missing_ok=True)
                    continue

                downloaded += 1

                result = process_photo(
                    download_path, base_dir, item_id, filename,
                    h_size, v_size, blur_radius, blur_darken, quality,
                )

                download_path.unlink(missing_ok=True)

                if result:
                    h_fn, v_fn = result
                    db.mark_downloaded(item_id, h_fn, v_fn)
                    processed += 1
                else:
                    db.mark_failed(item_id)

                self._progress = {
                    'total': len(all_items),
                    'pending': total - idx - 1,
                    'downloaded': downloaded,
                }

                if (idx + 1) % 20 == 0:
                    elapsed = time.time() - t_start
                    per_photo = elapsed / (idx + 1)
                    remaining = per_photo * (total - idx - 1)
                    logger.info(
                        f"Progress: {idx + 1}/{total} "
                        f"({processed} OK, ~{remaining:.0f}s remaining)"
                    )

                time.sleep(0.2)

            db.record_run(len(all_items), downloaded, processed, True)
            elapsed = time.time() - t_start
            logger.info(f"Sync done: {processed}/{total} new photos in {elapsed:.1f}s")
            self._set_phase('idle')

        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            self._last_error = str(e)
            db.record_run(0, 0, 0, False)
            self._set_phase('error')
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            db.close()
            with self._lock:
                self._running = False

    def _set_phase(self, phase: str):
        with self._lock:
            self._phase = phase


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config_frame.yaml'

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

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

    syncer = PhotoSyncer(config)
    syncer.run_sync()

    # Wait for completion
    while syncer.get_status()['running']:
        time.sleep(2)

    status = syncer.get_status()
    if status.get('error'):
        logger.error(f"Sync finished with error: {status['error']}")
        sys.exit(1)
    else:
        logger.info(f"Sync complete. H: {status['h_photos']}, V: {status['v_photos']}")


if __name__ == '__main__':
    main()
