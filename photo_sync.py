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
import re
import sqlite3
import hashlib
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

    def get_album_name(self) -> str:
        """Get album name after share initialization.

        Tries SYNO.Foto.Browse.Album first, then SYNO.Foto.Sharing.Misc.
        """
        for api, key_path in [
            ('SYNO.Foto.Browse.Album', ('list', 0, 'name')),
            ('SYNO.Foto.Sharing.Misc', ('sharing', 'album_name')),
        ]:
            try:
                data = {
                    'api': api,
                    'method': 'list' if 'Album' in api else 'get',
                    'version': 1,
                    'offset': 0,
                    'limit': 1,
                }
                resp = self.session.post(self._local_api_url(api), data=data)
                result = resp.json()
                if result.get('success'):
                    obj = result.get('data', {})
                    for k in key_path:
                        if isinstance(k, int):
                            obj = obj[k]
                        else:
                            obj = obj.get(k, {})
                    if isinstance(obj, str) and obj:
                        return obj
            except Exception:
                continue
        return ''

    @classmethod
    def resolve_album_name(cls, share_url: str, passphrase: str,
                           local_api_base: str = 'https://100.101.43.67:5443') -> str:
        """Create a temporary client, auth, and return the album name."""
        try:
            client = cls(share_url, passphrase, local_api_base)
            if client.initialize_share():
                return client.get_album_name()
        except Exception as e:
            logger.warning(f"Failed to resolve Synology album name: {e}")
        return ''

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
# Google Photos client (public shared albums, no OAuth)
# ---------------------------------------------------------------------------

class GooglePhotosClient:
    """Client for Google Photos shared albums via public share links.

    Fetches the shared album HTML page, extracts lh3.googleusercontent.com
    image URLs from embedded JS/HTML, and downloads full-resolution images
    by appending =w0 to the base URL.
    """

    def __init__(self, share_url: str):
        self.share_url = share_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        })

    def get_all_items(self) -> List[Dict[str, Any]]:
        """Fetch the shared album page and extract image URLs."""
        resp = self.session.get(self.share_url, timeout=30)
        resp.raise_for_status()

        # Extract lh3 image URLs from the page (embedded in JS data)
        pattern = r'https://lh3\.googleusercontent\.com/[a-zA-Z0-9_\-]+'
        raw_urls = list(set(re.findall(pattern, resp.text)))

        items = []
        for url in raw_urls:
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            items.append({
                'id': f'gph_{url_hash}',
                'filename': f'gphoto_{url_hash}.jpg',
                '_download_url': url + '=w0',  # full resolution
            })

        logger.info(f"Google Photos: found {len(items)} images in shared album")
        return items

    @staticmethod
    def resolve_album_name(share_url: str) -> str:
        """Fetch the shared album page and extract the album name from <title>."""
        try:
            resp = requests.get(share_url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            })
            match = re.search(r'<title>([^<]+)</title>', resp.text)
            if match:
                name = match.group(1).strip()
                for suffix in [' - Google Photos', ' \u2013 Google Photos']:
                    if name.endswith(suffix):
                        name = name[:-len(suffix)]
                if name and name != 'Google Photos':
                    return name
        except Exception as e:
            logger.warning(f"Failed to resolve Google album name: {e}")
        return ''

    def download_item(self, download_url: str, output_path: Path) -> bool:
        """Download a single image from Google Photos."""
        try:
            resp = self.session.get(download_url, stream=True, timeout=60)
            resp.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Failed to download Google photo: {e}")
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
                item_id TEXT PRIMARY KEY,
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
        self._migrate_item_id_to_text()

    def _migrate_item_id_to_text(self):
        """Migrate item_id column from INTEGER to TEXT if needed.

        Existing Synology rows get a 'syn_' prefix on their IDs.
        """
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(photos)")
        columns = cursor.fetchall()
        for col in columns:
            # col: (cid, name, type, notnull, default, pk)
            if col[1] == 'item_id' and col[2].upper() == 'INTEGER':
                logger.info("Migrating photos table: item_id INTEGER -> TEXT")
                cursor.execute('''
                    CREATE TABLE photos_new (
                        item_id TEXT PRIMARY KEY,
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
                    INSERT INTO photos_new
                    SELECT 'syn_' || CAST(item_id AS TEXT),
                           filename, filesize, taken_time,
                           first_seen, last_seen, downloaded,
                           download_failed, h_filename, v_filename
                    FROM photos
                ''')
                cursor.execute('DROP TABLE photos')
                cursor.execute('ALTER TABLE photos_new RENAME TO photos')
                self.conn.commit()
                logger.info("Migration complete")
                break

    def update_items(self, items: List[Dict[str, Any]]) -> List[Tuple[Optional[str], Optional[str]]]:
        """Update database with items from the API.

        Returns list of (h_filename, v_filename) tuples for removed (stale) items.
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

        # Find stale entries (no longer in any album)
        item_ids = {item['id'] for item in items}
        cursor.execute('SELECT item_id, h_filename, v_filename FROM photos')
        all_rows = cursor.fetchall()
        stale_files = []
        stale_ids = []
        for row in all_rows:
            if row['item_id'] not in item_ids:
                stale_ids.append(row['item_id'])
                stale_files.append((row['h_filename'], row['v_filename']))

        # Delete stale rows
        if stale_ids:
            cursor.executemany(
                'DELETE FROM photos WHERE item_id = ?',
                [(i,) for i in stale_ids],
            )
            logger.info(f"Removed {len(stale_ids)} stale entries from database")

        self.conn.commit()
        logger.info(f"Updated {len(items)} items in database")
        return stale_files

    def get_unprocessed(self, orientation: str) -> List[Dict[str, Any]]:
        """Get items not yet processed for the given orientation (with < 3 failures).

        Also returns 'other_filename' so caller knows if the other orientation exists.
        """
        col = 'h_filename' if orientation == 'horizontal' else 'v_filename'
        other_col = 'v_filename' if orientation == 'horizontal' else 'h_filename'
        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT item_id, filename, filesize, {other_col} as other_filename
            FROM photos
            WHERE {col} IS NULL AND download_failed < 3
        ''')
        return [dict(row) for row in cursor.fetchall()]

    def mark_processed(self, item_id, h_filename: str = None, v_filename: str = None):
        """Mark orientation-specific filenames after processing."""
        cursor = self.conn.cursor()
        updates = ['downloaded = 1']
        params = []
        if h_filename is not None:
            updates.append('h_filename = ?')
            params.append(h_filename)
        if v_filename is not None:
            updates.append('v_filename = ?')
            params.append(v_filename)
        params.append(item_id)
        cursor.execute(
            f'UPDATE photos SET {", ".join(updates)} WHERE item_id = ?', params)
        self.conn.commit()

    def mark_failed(self, item_id):
        """Increment the failure counter for an item."""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE photos
            SET download_failed = download_failed + 1
            WHERE item_id = ?
        ''', (item_id,))
        self.conn.commit()

    def cleanup_orientation(self, orientation: str, keep_count: int, base_dir: Path):
        """Delete processed files for an orientation beyond keep_count, clear DB refs.

        Returns the number of files deleted.
        """
        photo_dir = base_dir / orientation
        if not photo_dir.exists():
            return 0

        photos = sorted(photo_dir.glob('*.jpg'))
        if len(photos) <= keep_count:
            return 0

        to_delete = photos[keep_count:]
        deleted_names = set()
        for p in to_delete:
            deleted_names.add(p.name)
            p.unlink(missing_ok=True)

        # Clear DB references for deleted files
        col = 'h_filename' if orientation == 'horizontal' else 'v_filename'
        cursor = self.conn.cursor()
        for name in deleted_names:
            cursor.execute(f'UPDATE photos SET {col} = NULL WHERE {col} = ?', (name,))
        self.conn.commit()

        logger.info(f"Cleaned {len(to_delete)} {orientation} photos (kept {keep_count})")
        return len(to_delete)

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
        logger.debug(f"Processed: {filename} -> {out_name} ({', '.join(orientations)})")
        return (h_fn, v_fn)

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
        """Return current sync status, including pending count from DB when idle."""
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

            # When idle, query DB for pending count so settings always shows the queue
            if not self._running and 'pending' not in status:
                try:
                    db_path = photos_config.get('state_db', str(base_dir / 'state.db'))
                    if Path(db_path).exists():
                        db = PhotoDatabase(db_path)
                        counts = db.get_counts()
                        status['pending'] = counts.get('pending', 0)
                        status['total'] = counts.get('total', 0)
                        db.close()
                except Exception:
                    pass

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

        google_config = self._config.get('google_photos', {})

        try:
            # --- Phase 1: Connect to albums and list all photos ---
            self._set_phase('listing')
            share_urls = synology_config.get('share_urls', [])
            share_passphrases = synology_config.get('share_passphrases', [])

            if len(share_urls) != len(share_passphrases):
                raise ValueError("share_urls and share_passphrases must have the same length")

            syn_albums = [(url, pw) for url, pw in zip(share_urls, share_passphrases) if url]
            gph_urls = [u for u in google_config.get('share_urls', []) if u]

            if not syn_albums and not gph_urls:
                raise ValueError("No share URLs configured (Synology or Google)")

            all_items = []
            item_client_map = {}  # item_id -> (client_type, client_index)
            syn_clients = []
            gph_clients = []

            # -- Synology albums --
            for album_idx, (share_url, passphrase) in enumerate(syn_albums):
                if self._stop_event.is_set():
                    logger.info("Sync stopped by request")
                    return

                logger.info(f"Synology album {album_idx + 1}/{len(syn_albums)}...")
                client = SynologyPhotosClient(
                    share_url=share_url,
                    passphrase=passphrase,
                    local_api_base=synology_config.get('local_api_base', 'https://100.101.43.67:5443'),
                )

                if not client.initialize_share():
                    logger.error(f"Failed to initialize Synology album {album_idx + 1}, skipping")
                    continue

                items = client.get_all_items()
                logger.info(f"Synology album {album_idx + 1}: {len(items)} photos")

                for item in items:
                    # Prefix Synology IDs so they don't collide with Google IDs
                    item['id'] = f"syn_{item['id']}"
                    item_client_map[item['id']] = ('synology', len(syn_clients))
                all_items.extend(items)
                syn_clients.append(client)

            # -- Google Photos albums --
            for gph_idx, gph_url in enumerate(gph_urls):
                if self._stop_event.is_set():
                    logger.info("Sync stopped by request")
                    return

                logger.info(f"Google Photos album {gph_idx + 1}/{len(gph_urls)}...")
                try:
                    client = GooglePhotosClient(share_url=gph_url)
                    items = client.get_all_items()
                    logger.info(f"Google Photos album {gph_idx + 1}: {len(items)} photos")

                    for item in items:
                        item_client_map[item['id']] = ('google', len(gph_clients))
                    all_items.extend(items)
                    gph_clients.append(client)
                except Exception as e:
                    logger.error(f"Failed to fetch Google Photos album {gph_idx + 1}: {e}")

            if not all_items:
                raise ValueError("No photos found in any album")

            logger.info(f"Total photos across all albums: {len(all_items)}")

            # --- Phase 2: Update database, clean stale files ---
            self._set_phase('updating_db')
            stale_files = db.update_items(all_items)

            # Clean up processed files for removed photos
            if stale_files:
                for h_fn, v_fn in stale_files:
                    if h_fn:
                        (base_dir / 'horizontal' / h_fn).unlink(missing_ok=True)
                    if v_fn:
                        (base_dir / 'vertical' / v_fn).unlink(missing_ok=True)
                logger.info(f"Cleaned {len(stale_files)} stale photo files")

            # --- Phase 3: Download and process new photos ---
            self._set_phase('downloading')

            # Only process into the current orientation; keep ≤100 in the other
            orientation = self._config.get('frame', {}).get('orientation', 'horizontal')
            other_orientation = 'vertical' if orientation == 'horizontal' else 'horizontal'
            other_dir = base_dir / other_orientation
            other_count = len(list(other_dir.glob('*.jpg'))) if other_dir.exists() else 0
            other_limit = 100

            logger.info(f"Sync orientation: {orientation} (other has {other_count}/{other_limit})")

            unprocessed = db.get_unprocessed(orientation)
            total = len(unprocessed)
            batch_size = photos_config.get('batch_size', 10)
            downloaded = 0
            processed = 0
            t_start = time.time()

            if total > batch_size:
                logger.info(f"Batch limit: processing {batch_size} of {total} pending photos")

            self._progress = {'total': len(all_items), 'pending': total, 'downloaded': 0}

            # Build download_url lookup for Google items
            gph_download_urls = {}
            for ai in all_items:
                if '_download_url' in ai:
                    gph_download_urls[ai['id']] = ai['_download_url']

            for idx, item in enumerate(unprocessed):
                if self._stop_event.is_set():
                    logger.info("Sync stopped by request")
                    break

                if processed >= batch_size:
                    logger.info(f"Batch limit reached ({batch_size}), stopping for this run")
                    break

                item_id = item['item_id']
                filename = item['filename']
                download_path = tmp_dir / filename

                client_info = item_client_map.get(item_id)
                if client_info is None:
                    logger.warning(f"Skipping item {item_id}: no client found")
                    db.mark_failed(item_id)
                    continue

                client_type, client_idx = client_info

                # Download using the appropriate client
                if client_type == 'google':
                    download_url = gph_download_urls.get(item_id)
                    if not download_url:
                        logger.warning(f"Skipping Google item {item_id}: no download URL")
                        db.mark_failed(item_id)
                        continue
                    client = gph_clients[client_idx]
                    ok = client.download_item(download_url, download_path)
                else:
                    client = syn_clients[client_idx]
                    syn_id = int(item_id.replace('syn_', ''))
                    ok = client.download_item(syn_id, download_path)

                if not ok:
                    logger.warning(f"Skipping item {item_id}: download failed")
                    db.mark_failed(item_id)
                    download_path.unlink(missing_ok=True)
                    continue

                downloaded += 1

                # Decide which orientations to process
                process_orientations = [orientation]
                if other_count < other_limit and not item.get('other_filename'):
                    process_orientations.append(other_orientation)
                    other_count += 1

                result = process_photo(
                    download_path, base_dir, item_id, filename,
                    h_size, v_size, blur_radius, blur_darken, quality,
                    orientations=tuple(process_orientations),
                )

                download_path.unlink(missing_ok=True)

                if result:
                    h_fn, v_fn = result
                    db.mark_processed(item_id, h_fn, v_fn)
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
