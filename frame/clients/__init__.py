"""Photo source clients for Synology, Google Photos, Immich, iCloud, and Nextcloud."""

from .synology import SynologyPhotosClient
from .google_photos import GooglePhotosClient
from .immich import ImmichClient
from .icloud import ICloudSharedAlbumClient
from .nextcloud import NextcloudClient

__all__ = [
    'SynologyPhotosClient',
    'GooglePhotosClient',
    'ImmichClient',
    'ICloudSharedAlbumClient',
    'NextcloudClient',
]
