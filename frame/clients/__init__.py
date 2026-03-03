"""Photo source clients for Synology, Google Photos, and Immich."""

from .synology import SynologyPhotosClient
from .google_photos import GooglePhotosClient
from .immich import ImmichClient

__all__ = ['SynologyPhotosClient', 'GooglePhotosClient', 'ImmichClient']
