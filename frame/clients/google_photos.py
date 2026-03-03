"""Google Photos client for public shared albums (no OAuth)."""

import re
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any

import requests

logger = logging.getLogger(__name__)


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
