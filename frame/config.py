"""Application state and configuration management."""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class AppState:
    """Central application state replacing global singletons.

    Holds config, syncer, energy manager, sysinfo cache, wifi manager,
    and album name cache. Passed to the HTTP handler factory.
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self.load_config()
        self.syncer = None
        self.energy_save = None
        self.sysinfo_cache = None
        self.wifi_manager = None
        self.album_name_cache = {}  # url -> resolved album name
        self.wizard_mode = False

    def load_config(self) -> dict:
        """Load config from YAML file."""
        with open(self.config_path, 'r') as f:
            return yaml.safe_load(f) or {}

    def save_config(self):
        """Write current config back to YAML file."""
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)

    def has_album_sources(self) -> bool:
        """Check if any real photo album sources are configured."""
        def has_real_urls(urls):
            return any(u for u in urls if u and 'REPLACE_ME' not in u and 'example.com' not in u)
        has_synology = has_real_urls(self.config.get('synology', {}).get('share_urls', []))
        has_google = has_real_urls(self.config.get('google_photos', {}).get('share_urls', []))
        has_immich = has_real_urls(self.config.get('immich', {}).get('share_urls', []))
        return has_synology or has_google or has_immich

    def init_syncer(self):
        """Initialize the photo syncer if sources are configured (no auto-sync)."""
        if self.has_album_sources():
            from frame.sync import PhotoSyncer
            self.syncer = PhotoSyncer(self.config)
