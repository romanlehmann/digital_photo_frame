#!/usr/bin/env python3
"""
Simple HTTP server for the photo frame viewer.
Serves the viewer HTML and generates a JSON list of photos.
"""

import os
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SysinfoCache:
    """Background cache for system info to avoid slow shell calls on each request."""

    def __init__(self, photos_base_dir):
        self.photos_base = Path(photos_base_dir)
        self.data = {}
        self._update()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            time.sleep(30)
            self._update()

    def _update(self):
        import socket
        info = {'hostname': socket.gethostname(), 'ips': [], 'tailscale_ip': ''}
        try:
            result = subprocess.run(
                ['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5)
            info['tailscale_ip'] = result.stdout.strip()
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['hostname', '-I'], capture_output=True, text=True, timeout=5)
            all_ips = result.stdout.strip().split()
            # Filter out Tailscale IP and IPv6 addresses
            ts_ip = info['tailscale_ip']
            info['ips'] = [ip for ip in all_ips
                           if ip != ts_ip and ':' not in ip]
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['iwgetid', '-r'], capture_output=True, text=True, timeout=5)
            info['wifi_ssid'] = result.stdout.strip()
        except Exception:
            info['wifi_ssid'] = ''
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                info['cpu_temp'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            info['cpu_temp'] = None
        try:
            result = subprocess.run(
                ['df', '-h', '/'], capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                info['disk'] = f"{parts[2]}/{parts[1]} ({parts[4]})"
        except Exception:
            info['disk'] = ''
        h_dir = self.photos_base / 'horizontal'
        v_dir = self.photos_base / 'vertical'
        h_count = len(list(h_dir.glob('*.jpg'))) if h_dir.exists() else 0
        v_count = len(list(v_dir.glob('*.jpg'))) if v_dir.exists() else 0
        info['h_photos'] = h_count
        info['v_photos'] = v_count
        info['photo_count'] = f"H: {h_count} / V: {v_count}"
        self.data = info

    def get(self):
        return self.data


class EnergySaveManager:
    """Manages sleep schedule by stopping/starting the display service.

    During sleep: stops cage+Chromium (frees RAM, screen shows black console).
    On wake: restarts the display service.
    Touch-to-wake: reads raw input events during sleep to detect touch/tap.
    """

    SCHEDULE_FILE = '/tmp/frame-schedule.json'

    def __init__(self):
        self.enabled = False
        self.off_time = '22:00'
        self.on_time = '07:00'
        self.sleeping = False
        self._wake_event = threading.Event()
        self._load()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _load(self):
        try:
            with open(self.SCHEDULE_FILE) as f:
                data = json.load(f)
                self.enabled = data.get('enabled', False)
                self.off_time = data.get('off_time', '22:00')
                self.on_time = data.get('on_time', '07:00')
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.SCHEDULE_FILE, 'w') as f:
                json.dump({
                    'enabled': self.enabled,
                    'off_time': self.off_time,
                    'on_time': self.on_time,
                }, f)
        except Exception as e:
            logger.error(f"Error saving schedule: {e}")

    def is_sleeping(self):
        return self.sleeping

    def get_schedule(self):
        return {
            'enabled': self.enabled,
            'off_time': self.off_time,
            'on_time': self.on_time,
            'sleeping': self.sleeping,
        }

    def update_schedule(self, data):
        self.enabled = data.get('enabled', self.enabled)
        self.off_time = data.get('off_time', self.off_time)
        self.on_time = data.get('on_time', self.on_time)
        self._save()
        self._check()

    def wake_display(self):
        """Wake display immediately."""
        if self.sleeping:
            self._set_sleep(False)

    def _set_sleep(self, sleep):
        try:
            if sleep and not self.sleeping:
                self.sleeping = True
                # Stop cage to free RAM (returns to Linux console/framebuffer)
                subprocess.run(['sudo', 'systemctl', 'stop', 'photo_frame_cage'],
                             capture_output=True, timeout=15)
                time.sleep(1)
                # Fill framebuffer with black — keeps HDMI signal active
                # (avoids ANMITE "no signal" blue screen that DPMS causes)
                subprocess.run(['sudo', 'dd', 'if=/dev/zero', 'of=/dev/fb0',
                                'bs=1M', 'count=10'],
                             capture_output=True, timeout=5)
                # Hide console cursor and prevent text from appearing
                subprocess.run(['sudo', 'sh', '-c',
                                'setterm --cursor off --blank force --powerdown 0 > /dev/tty1'],
                             capture_output=True, timeout=5)
                # Turn off backlight via DDC/CI power mode standby
                subprocess.run(['sudo', 'ddcutil', 'setvcp', 'd6', '4'],
                             capture_output=True, timeout=10)
                logger.info("Sleep: stopped display, framebuffer black")
                # Trigger photo sync during sleep (cage stopped = more RAM)
                global _syncer
                if _syncer:
                    logger.info("Sleep: triggering photo sync")
                    _syncer.run_sync()
                # Start touch-to-wake listener
                self._wake_event.clear()
                threading.Thread(target=self._touch_wake_listener, daemon=True).start()
            elif not sleep and self.sleeping:
                self._wake_event.set()  # stop touch listener
                # Start cage first (Chromium needs time to render)
                subprocess.run(['sudo', 'systemctl', 'start', 'photo_frame_cage'],
                             capture_output=True, timeout=15)
                # Wait for Chromium to render the slideshow before turning on display
                time.sleep(5)
                # Restore monitor from DDC/CI standby
                subprocess.run(['sudo', 'ddcutil', 'setvcp', 'd6', '1'],
                             capture_output=True, timeout=10)
                self.sleeping = False
                logger.info("Wake: started display service")
        except Exception as e:
            logger.error(f"Sleep control error: {e}")

    def _touch_wake_listener(self):
        """Listen for touch/input events during sleep to trigger wake."""
        import glob
        import struct
        import select
        # Find input devices
        devices = glob.glob('/dev/input/event*')
        fds = []
        for dev in devices:
            try:
                fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
                fds.append(fd)
            except Exception:
                pass
        if not fds:
            logger.warning("Touch wake: no input devices found")
            return
        logger.info(f"Touch wake: listening on {len(fds)} input devices")
        try:
            while not self._wake_event.is_set():
                readable, _, _ = select.select(fds, [], [], 1.0)
                for fd in readable:
                    try:
                        # Read and discard input event data (24 bytes per event on 32-bit)
                        os.read(fd, 4096)
                    except Exception:
                        continue
                    if self.sleeping:
                        logger.info("Touch wake: input detected, waking display")
                        self.wake_display()
                        return
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except Exception:
                    pass

    def _check(self):
        if not self.enabled:
            if self.sleeping:
                self._set_sleep(False)
            return
        should_sleep = self._in_off_period()
        if should_sleep and not self.sleeping:
            self._set_sleep(True)
        elif not should_sleep and self.sleeping:
            self._set_sleep(False)

    def _loop(self):
        while True:
            self._check()
            time.sleep(30)

    def _in_off_period(self):
        from datetime import datetime
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        off_parts = self.off_time.split(':')
        on_parts = self.on_time.split(':')
        off_mins = int(off_parts[0]) * 60 + int(off_parts[1])
        on_mins = int(on_parts[0]) * 60 + int(on_parts[1])

        if off_mins > on_mins:
            # e.g. 22:00 - 07:00 (overnight)
            return now_mins >= off_mins or now_mins < on_mins
        else:
            # e.g. 01:00 - 06:00 (same day)
            return off_mins <= now_mins < on_mins


# Global singletons, initialized in main()
_sysinfo_cache = None
_energy_save = None
_config_path = None
_syncer = None
_config = None
_album_name_cache = {}  # url -> resolved album name
_wifi_manager = None


class PhotoFrameHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler for photo frame."""

    def __init__(self, *args, photos_dir='/srv/frame/photos', viewer_dir='/srv/frame/viewer', slideshow_config=None, **kwargs):
        self.photos_dir = Path(photos_dir)
        self.viewer_dir = Path(viewer_dir)
        self.slideshow_config = slideshow_config or {}
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        """Handle GET requests."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        
        if path == '/' or path == '/index.html':
            # Serve viewer HTML
            self.serve_viewer()
        elif path == '/config':
            # Serve slideshow config as JSON
            self.serve_config_json()
        elif path == '/list':
            # Serve photos list as JSON
            self.serve_photos_json()
        elif path == '/sysinfo':
            self.serve_sysinfo()
        elif path == '/brightness':
            self.serve_brightness()
        elif path == '/schedule':
            self.serve_schedule()
        elif path == '/orientation':
            self.serve_orientation()
        elif path == '/sync/status':
            self.serve_sync_status()
        elif path == '/remote':
            self.serve_remote()
        elif path == '/api/synology':
            self.serve_synology_config()
        elif path == '/api/google_photos':
            self.serve_google_photos_config()
        elif path == '/api/immich':
            self.serve_immich_config()
        elif path == '/api/album_names':
            self.serve_album_names()
        elif path == '/api/qrcode':
            self.serve_qrcode(parsed_path)
        elif path == '/setup':
            self.serve_setup()
        elif path == '/api/wifi/status':
            self.serve_wifi_status()
        elif path.startswith('/photos/'):
            # Serve photo file (supports /photos/horizontal/file.jpg)
            photo_name = path[8:]  # Remove '/photos/' prefix
            self.serve_photo(photo_name)
        else:
            self.handle_not_found()
    
    def do_POST(self):
        """Handle POST requests."""
        if self.path == '/shutdown':
            self.handle_shutdown()
        elif self.path == '/reboot':
            self.handle_reboot()
        elif self.path == '/schedule':
            self.handle_save_schedule()
        elif self.path == '/wake':
            self.handle_wake()
        elif self.path == '/orientation':
            self.handle_save_orientation()
        elif self.path == '/sync/trigger':
            self.handle_sync_trigger()
        elif self.path == '/api/interval':
            self.handle_save_interval()
        elif self.path == '/api/synology':
            self.handle_save_synology_config()
        elif self.path == '/api/google_photos':
            self.handle_save_google_photos_config()
        elif self.path == '/api/immich':
            self.handle_save_immich_config()
        elif self.path == '/api/wifi/scan':
            self.handle_wifi_scan()
        elif self.path == '/api/wifi/connect':
            self.handle_wifi_connect()
        else:
            self.send_error(404, "Not found")
    
    def serve_viewer(self):
        """Serve the viewer HTML."""
        viewer_file = self.viewer_dir / 'index.html'
        if not viewer_file.exists():
            self.send_error(404, "Viewer not found")
            return
        
        try:
            with open(viewer_file, 'rb') as f:
                content = f.read()
            
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            logger.error(f"Error serving viewer: {e}")
            self.send_error(500, str(e))

    def serve_config_json(self):
        """Serve slideshow configuration as JSON."""
        content = json.dumps(self.slideshow_config).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', len(content))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content)

    def _get_orientation(self):
        """Read current orientation from config."""
        global _config_path
        try:
            import yaml
            with open(_config_path) as f:
                cfg = yaml.safe_load(f)
            return cfg.get('frame', {}).get('orientation', 'horizontal')
        except Exception:
            return 'horizontal'

    def serve_photos_json(self):
        """Serve list of photos as JSON (orientation-aware, with fallback)."""
        try:
            orientation = self._get_orientation()
            photos_subdir = self.photos_dir / orientation
            fallback_orientation = 'vertical' if orientation == 'horizontal' else 'horizontal'
            fallback_subdir = self.photos_dir / fallback_orientation

            photos = []
            used_orientation = orientation

            if photos_subdir.exists():
                photos = [f'/photos/{orientation}/{p.name}'
                          for p in photos_subdir.glob('*.jpg')]

            # Fall back to other orientation if target folder is empty
            if not photos and fallback_subdir.exists():
                photos = [f'/photos/{fallback_orientation}/{p.name}'
                          for p in fallback_subdir.glob('*.jpg')]
                used_orientation = fallback_orientation
                if photos:
                    logger.info(f"No {orientation} photos, falling back to {fallback_orientation}")

            content = json.dumps(photos).encode('utf-8')

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)

            logger.info(f"Served {len(photos)} {used_orientation} photos in JSON")

        except Exception as e:
            logger.error(f"Error serving photos JSON: {e}")
            self.send_error(500, str(e))
    
    def serve_photo(self, photo_name):
        """Serve a photo file."""
        photo_path = self.photos_dir / photo_name
        
        if not photo_path.exists() or not photo_path.is_file():
            self.send_error(404, "Photo not found")
            return
        
        # Security check: ensure photo is within photos_dir
        try:
            photo_path.resolve().relative_to(self.photos_dir.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        
        try:
            with open(photo_path, 'rb') as f:
                content = f.read()
            
            # Determine content type
            ext = photo_path.suffix.lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.bmp': 'image/bmp'
            }
            content_type = content_types.get(ext, 'application/octet-stream')
            
            self.send_response(200)
            self.send_header('Content-type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as e:
            logger.error(f"Error serving photo {photo_name}: {e}")
            self.send_error(500, str(e))
    
    def handle_shutdown(self):
        """Handle shutdown request."""
        logger.warning("Shutdown requested via HTTP")
        
        try:
            # Send response first
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Shutdown initiated')
            
            # Schedule shutdown (with delay to allow response to send)
            def delayed_shutdown():
                time.sleep(2)
                subprocess.run(['sudo', 'shutdown', '-h', 'now'])
            
            thread = threading.Thread(target=delayed_shutdown)
            thread.daemon = True
            thread.start()
            
        except Exception as e:
            logger.error(f"Error handling shutdown: {e}")
            self.send_error(500, str(e))
    
    def serve_sysinfo(self):
        """Serve cached system info as JSON (updated every 30s in background)."""
        global _sysinfo_cache
        info = _sysinfo_cache.get() if _sysinfo_cache else {}
        content = json.dumps(info).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def serve_brightness(self):
        """Serve auto brightness value from ambient light sensor."""
        brightness = None
        try:
            # TODO: Read from ambient light sensor (e.g. BH1750, TSL2561)
            # For now, try reading from a file that a sensor script can write to
            sensor_file = Path('/tmp/frame-brightness')
            if sensor_file.exists():
                val = int(sensor_file.read_text().strip())
                brightness = max(10, min(100, val))
        except Exception:
            pass

        data = {'brightness': brightness}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def serve_schedule(self):
        """Serve energy save schedule."""
        global _energy_save
        data = _energy_save.get_schedule() if _energy_save else {}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_wake(self):
        """Wake display from DPMS sleep."""
        global _energy_save
        if _energy_save:
            _energy_save.wake_display()
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True}).encode())

    def handle_save_schedule(self):
        """Save energy save schedule."""
        global _energy_save
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            if _energy_save:
                _energy_save.update_schedule(data)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
        except Exception as e:
            logger.error(f"Error saving schedule: {e}")
            self.send_error(400, str(e))

    def serve_orientation(self):
        """Serve current orientation setting."""
        global _config_path
        orientation = 'horizontal'
        try:
            import yaml
            with open(_config_path) as f:
                cfg = yaml.safe_load(f)
            orientation = cfg.get('frame', {}).get('orientation', 'horizontal')
        except Exception:
            pass
        content = json.dumps({'orientation': orientation}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_save_interval(self):
        """Save slideshow interval to config and update in-memory config."""
        global _config_path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            import yaml
            data = json.loads(body)
            interval = int(data.get('interval', 300))
            if interval < 10:
                interval = 10
            if interval > 3600:
                interval = 3600

            with open(_config_path) as f:
                cfg = yaml.safe_load(f)
            cfg.setdefault('slideshow', {})['interval'] = interval
            with open(_config_path, 'w') as f:
                yaml.dump(cfg, f, default_flow_style=False)

            # Update in-memory slideshow config
            self.slideshow_config['interval'] = interval

            self._json_response({'ok': True, 'interval': interval})
        except Exception as e:
            logger.error(f"Failed to save interval: {e}")
            self._json_response({'ok': False, 'error': str(e)}, 400)

    def handle_save_orientation(self):
        """Save orientation setting, trigger sync if target folder is empty, restart cage."""
        global _config_path, _syncer
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            import yaml
            data = json.loads(body)
            new_orientation = data.get('orientation', 'horizontal')
            if new_orientation not in ('horizontal', 'vertical'):
                raise ValueError('Invalid orientation')

            with open(_config_path) as f:
                cfg = yaml.safe_load(f)
            old_orientation = cfg.get('frame', {}).get('orientation', 'horizontal')
            cfg.setdefault('frame', {})['orientation'] = new_orientation
            with open(_config_path, 'w') as f:
                yaml.dump(cfg, f, default_flow_style=False)

            # Check if the target orientation folder has photos
            photos_config = (_config or {}).get('photos', {})
            base_dir = Path(photos_config.get('base_dir', '/srv/frame/photos'))
            target_dir = base_dir / new_orientation
            has_photos = target_dir.exists() and any(target_dir.glob('*.jpg'))

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'sync_triggered': not has_photos}).encode())

            if new_orientation != old_orientation:
                # Restart cage to apply rotation (autostart reads config)
                def restart_cage():
                    time.sleep(1)
                    subprocess.run(['sudo', 'systemctl', 'restart', 'photo_frame_cage'],
                                 capture_output=True, timeout=15)
                    logger.info(f"Orientation changed to {new_orientation}, restarted cage")
                threading.Thread(target=restart_cage, daemon=True).start()
        except Exception as e:
            logger.error(f"Error saving orientation: {e}")
            self.send_error(400, str(e))

    def serve_sync_status(self):
        """Serve photo sync status as JSON."""
        global _syncer
        if _syncer:
            data = _syncer.get_status()
        else:
            data = {'running': False, 'phase': 'disabled', 'error': 'syncer not initialized'}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_sync_trigger(self):
        """Trigger a manual photo sync."""
        global _syncer
        if _syncer:
            _syncer.run_sync()
            data = {'ok': True, 'message': 'Sync started'}
        else:
            data = {'ok': False, 'message': 'Syncer not initialized'}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def serve_remote(self):
        """Serve the remote config HTML page."""
        remote_file = self.viewer_dir / 'remote.html'
        if not remote_file.exists():
            self.send_error(404, "Remote config page not found")
            return
        try:
            with open(remote_file, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            logger.error(f"Error serving remote page: {e}")
            self.send_error(500, str(e))

    def serve_synology_config(self):
        """Serve Synology share URLs and passphrases as JSON."""
        global _config
        synology = (_config or {}).get('synology', {})
        data = {
            'share_urls': synology.get('share_urls', []),
            'share_passphrases': synology.get('share_passphrases', []),
        }
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_save_synology_config(self):
        """Save Synology share URLs and passphrases to config."""
        global _config, _config_path, _syncer
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            import yaml
            data = json.loads(body)
            urls = data.get('share_urls', [])
            passphrases = data.get('share_passphrases', [])
            if not isinstance(urls, list) or not isinstance(passphrases, list):
                raise ValueError('share_urls and share_passphrases must be lists')
            if len(urls) != len(passphrases):
                raise ValueError('share_urls and share_passphrases must have the same length')

            # Update in-memory config
            _config.setdefault('synology', {})
            _config['synology']['share_urls'] = urls
            _config['synology']['share_passphrases'] = passphrases

            # Write to YAML
            with open(_config_path, 'w') as f:
                yaml.dump(_config, f, default_flow_style=False)

            # Create syncer if it didn't exist before and we now have URLs
            if _syncer is None and any(urls):
                from photo_sync import PhotoSyncer
                _syncer = PhotoSyncer(_config)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
            logger.info(f"Synology config saved: {len(urls)} album(s)")
        except Exception as e:
            logger.error(f"Error saving synology config: {e}")
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())

    def serve_google_photos_config(self):
        """Serve Google Photos share URLs as JSON."""
        global _config
        gph = (_config or {}).get('google_photos', {})
        data = {'share_urls': gph.get('share_urls', [])}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_save_google_photos_config(self):
        """Save Google Photos share URLs to config."""
        global _config, _config_path, _syncer
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            import yaml
            data = json.loads(body)
            urls = data.get('share_urls', [])
            if not isinstance(urls, list):
                raise ValueError('share_urls must be a list')

            # Update in-memory config
            _config.setdefault('google_photos', {})
            _config['google_photos']['share_urls'] = urls

            # Write to YAML
            with open(_config_path, 'w') as f:
                yaml.dump(_config, f, default_flow_style=False)

            # Create syncer if it didn't exist before and we now have URLs
            if _syncer is None and any(urls):
                from photo_sync import PhotoSyncer
                _syncer = PhotoSyncer(_config)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
            logger.info(f"Google Photos config saved: {len(urls)} album(s)")
        except Exception as e:
            logger.error(f"Error saving Google Photos config: {e}")
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())

    def serve_immich_config(self):
        """Serve Immich share URLs and passphrases as JSON."""
        global _config
        immich = (_config or {}).get('immich', {})
        data = {
            'share_urls': immich.get('share_urls', []),
            'share_passphrases': immich.get('share_passphrases', []),
        }
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_save_immich_config(self):
        """Save Immich share URLs and passphrases to config."""
        global _config, _config_path, _syncer
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            import yaml
            data = json.loads(body)
            urls = data.get('share_urls', [])
            passphrases = data.get('share_passphrases', [])
            if not isinstance(urls, list) or not isinstance(passphrases, list):
                raise ValueError('share_urls and share_passphrases must be lists')
            if len(urls) != len(passphrases):
                raise ValueError('share_urls and share_passphrases must have the same length')

            # Update in-memory config
            _config.setdefault('immich', {})
            _config['immich']['share_urls'] = urls
            _config['immich']['share_passphrases'] = passphrases

            # Write to YAML
            with open(_config_path, 'w') as f:
                yaml.dump(_config, f, default_flow_style=False)

            # Create syncer if it didn't exist before and we now have URLs
            if _syncer is None and any(urls):
                from photo_sync import PhotoSyncer
                _syncer = PhotoSyncer(_config)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
            logger.info(f"Immich config saved: {len(urls)} album(s)")
        except Exception as e:
            logger.error(f"Error saving Immich config: {e}")
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())

    def serve_album_names(self):
        """Resolve and return album names for all configured URLs.

        Returns {synology: {url: name, ...}, google: {url: name, ...}, immich: {url: name, ...}}.
        Uses an in-memory cache so repeated calls are fast.
        """
        global _config, _album_name_cache
        from photo_sync import SynologyPhotosClient, GooglePhotosClient, ImmichClient

        synology = (_config or {}).get('synology', {})
        google = (_config or {}).get('google_photos', {})
        immich = (_config or {}).get('immich', {})

        syn_urls = synology.get('share_urls', [])
        syn_passes = synology.get('share_passphrases', [])
        gph_urls = google.get('share_urls', [])
        imm_urls = immich.get('share_urls', [])
        imm_passes = immich.get('share_passphrases', [])
        local_api = synology.get('local_api_base', 'https://100.101.43.67:5443')

        result = {'synology': {}, 'google': {}, 'immich': {}}

        # Resolve Synology album names
        for url, pw in zip(syn_urls, syn_passes):
            if not url:
                continue
            if url in _album_name_cache:
                result['synology'][url] = _album_name_cache[url]
            else:
                name = SynologyPhotosClient.resolve_album_name(url, pw, local_api)
                if name:
                    _album_name_cache[url] = name
                    result['synology'][url] = name

        # Resolve Google Photos album names
        for url in gph_urls:
            if not url:
                continue
            if url in _album_name_cache:
                result['google'][url] = _album_name_cache[url]
            else:
                name = GooglePhotosClient.resolve_album_name(url)
                if name:
                    _album_name_cache[url] = name
                    result['google'][url] = name

        # Resolve Immich album names
        while len(imm_passes) < len(imm_urls):
            imm_passes.append('')
        for url, pw in zip(imm_urls, imm_passes):
            if not url:
                continue
            if url in _album_name_cache:
                result['immich'][url] = _album_name_cache[url]
            else:
                name = ImmichClient.resolve_album_name(url, pw)
                if name:
                    _album_name_cache[url] = name
                    result['immich'][url] = name

        content = json.dumps(result).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def serve_qrcode(self, parsed_path):
        """Generate QR code modules as JSON for client-side canvas rendering."""
        params = parse_qs(parsed_path.query)
        text = params.get('text', [''])[0]
        if not text:
            self.send_error(400, "Missing text parameter")
            return
        try:
            # Minimal QR code generation using segno (pure Python, no deps)
            import segno
            qr = segno.make(text, error='m')
            matrix = qr.matrix
            modules = []
            for row in matrix:
                modules.append([int(bool(cell)) for cell in row])
            data = {'modules': modules}
            content = json.dumps(data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except ImportError:
            logger.error("segno not installed — pip install segno")
            self.send_error(500, "QR library not available")
        except Exception as e:
            logger.error(f"QR generation error: {e}")
            self.send_error(500, str(e))

    def handle_not_found(self):
        """Handle 404 — redirect to /setup in hotspot mode (captive portal)."""
        global _wifi_manager
        if _wifi_manager and _wifi_manager.mode == 'hotspot':
            self.send_response(302)
            self.send_header('Location', '/setup')
            self.end_headers()
        else:
            self.send_error(404, "Not found")

    def serve_setup(self):
        """Serve the WiFi setup page."""
        setup_file = self.viewer_dir / 'setup.html'
        if not setup_file.exists():
            self.send_error(404, "Setup page not found")
            return
        try:
            with open(setup_file, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            logger.error(f"Error serving setup page: {e}")
            self.send_error(500, str(e))

    def serve_wifi_status(self):
        """Serve WiFi manager status as JSON."""
        global _wifi_manager
        if _wifi_manager:
            data = _wifi_manager.get_status()
        else:
            data = {'mode': 'normal', 'ssid': '', 'ip': ''}
        content = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_wifi_scan(self):
        """Scan for WiFi networks."""
        global _wifi_manager
        if not _wifi_manager:
            self._json_response({'networks': []})
            return
        networks = _wifi_manager.scan_networks()
        self._json_response({'networks': networks})

    def handle_wifi_connect(self):
        """Connect to a WiFi network (runs in background thread)."""
        global _wifi_manager, _syncer, _config
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            ssid = data.get('ssid', '')
            password = data.get('password', '')
            if not ssid:
                self._json_response({'ok': False, 'message': 'SSID required'}, 400)
                return

            # Respond immediately (phone will lose connection when hotspot drops)
            self._json_response({'ok': True, 'message': 'Connecting...'})

            def connect_background():
                global _syncer
                success, message = _wifi_manager.connect_to_network(ssid, password)
                if success:
                    logger.info(f"WiFi connected to {ssid}, initializing syncer")
                    # Initialize syncer now that we have connectivity
                    if _config and _syncer is None:
                        has_synology = any(_config.get('synology', {}).get('share_urls', []))
                        has_google = any(_config.get('google_photos', {}).get('share_urls', []))
                        has_immich = any(_config.get('immich', {}).get('share_urls', []))
                        if has_synology or has_google or has_immich:
                            from photo_sync import PhotoSyncer
                            _syncer = PhotoSyncer(_config)
                else:
                    logger.warning(f"WiFi connect failed: {message}")

            threading.Thread(target=connect_background, daemon=True).start()

        except Exception as e:
            logger.error(f"WiFi connect error: {e}")
            self._json_response({'ok': False, 'message': str(e)}, 400)

    def _json_response(self, data, status=200):
        """Helper to send a JSON response."""
        content = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def handle_reboot(self):
        """Handle reboot request."""
        logger.warning("Reboot requested via HTTP")
        try:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Reboot initiated')

            def delayed_reboot():
                time.sleep(2)
                subprocess.run(['sudo', 'reboot'])

            thread = threading.Thread(target=delayed_reboot)
            thread.daemon = True
            thread.start()
        except Exception as e:
            logger.error(f"Error handling reboot: {e}")
            self.send_error(500, str(e))

    def log_message(self, format, *args):
        """Override to use Python logging."""
        logger.info(f"{self.address_string()} - {format % args}")


def create_handler(photos_dir, viewer_dir, slideshow_config=None):
    """Create a handler with custom directories."""
    def handler(*args, **kwargs):
        return PhotoFrameHandler(*args, photos_dir=photos_dir, viewer_dir=viewer_dir, slideshow_config=slideshow_config, **kwargs)
    return handler


def main():
    """Main entry point."""
    import sys
    import yaml
    from wifi_manager import WiFiManager

    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config_frame.yaml'

    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        photos_config = config.get('photos', {})
        photos_dir = photos_config.get('base_dir', '/srv/frame/photos')
        viewer_dir = str(Path(__file__).parent / 'viewer')
        port = int(os.environ.get('PORT', 8080))

        # Ensure directories exist
        Path(photos_dir).mkdir(parents=True, exist_ok=True)
        (Path(photos_dir) / 'horizontal').mkdir(parents=True, exist_ok=True)
        (Path(photos_dir) / 'vertical').mkdir(parents=True, exist_ok=True)

        slideshow_config = config.get('slideshow', {})

        # Start background services
        global _sysinfo_cache, _energy_save, _config_path, _syncer, _config, _wifi_manager
        _config_path = config_path
        _config = config
        _sysinfo_cache = SysinfoCache(photos_dir)
        _energy_save = EnergySaveManager()

        # Check WiFi connectivity and start hotspot if needed
        _wifi_manager = WiFiManager()
        if _wifi_manager.check_connectivity():
            logger.info("WiFi connected — normal mode")
            # Initialize photo syncer (if any source is configured)
            _init_syncer(config)
        else:
            logger.warning("No WiFi connectivity — starting hotspot")
            _wifi_manager.start_hotspot()

        handler = create_handler(photos_dir, viewer_dir, slideshow_config)
        HTTPServer.allow_reuse_address = True
        server = HTTPServer(('0.0.0.0', port), handler)

        logger.info(f"Starting photo frame server on port {port}")
        logger.info(f"Photos directory: {photos_dir}")
        logger.info(f"Viewer directory: {viewer_dir}")
        if _wifi_manager.mode == 'hotspot':
            logger.info(f"Hotspot mode: connect to '{_wifi_manager.get_status()['hotspot_ssid']}'")
        else:
            logger.info(f"Open http://localhost:{port}/ to view")

        server.serve_forever()

    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def _init_syncer(config):
    """Initialize the photo syncer if sources are configured (no auto-sync)."""
    global _syncer
    has_synology = any(config.get('synology', {}).get('share_urls', []))
    has_google = any(config.get('google_photos', {}).get('share_urls', []))
    has_immich = any(config.get('immich', {}).get('share_urls', []))
    if has_synology or has_google or has_immich:
        from photo_sync import PhotoSyncer
        _syncer = PhotoSyncer(config)


if __name__ == '__main__':
    main()
