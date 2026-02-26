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

    def __init__(self, photos_dir):
        self.photos_dir = Path(photos_dir)
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
                ['hostname', '-I'], capture_output=True, text=True, timeout=5)
            info['ips'] = result.stdout.strip().split()
        except Exception:
            pass
        try:
            result = subprocess.run(
                ['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5)
            info['tailscale_ip'] = result.stdout.strip()
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
        photo_count = len(list(self.photos_dir.glob('*.jpg'))) if self.photos_dir.exists() else 0
        info['photo_count'] = photo_count
        self.data = info

    def get(self):
        return self.data


class EnergySaveManager:
    """Manages sleep schedule — viewer polls /schedule to show black screen."""

    SCHEDULE_FILE = '/tmp/frame-schedule.json'

    def __init__(self):
        self.enabled = False
        self.off_time = '22:00'
        self.on_time = '07:00'
        self._load()

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
        if not self.enabled:
            return False
        return self._in_off_period()

    def get_schedule(self):
        return {
            'enabled': self.enabled,
            'off_time': self.off_time,
            'on_time': self.on_time,
            'sleeping': self.is_sleeping(),
        }

    def update_schedule(self, data):
        self.enabled = data.get('enabled', self.enabled)
        self.off_time = data.get('off_time', self.off_time)
        self.on_time = data.get('on_time', self.on_time)
        self._save()

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
        elif path.startswith('/photos/'):
            # Serve photo file
            photo_name = path[8:]  # Remove '/photos/' prefix
            self.serve_photo(photo_name)
        else:
            self.send_error(404, "Not found")
    
    def do_POST(self):
        """Handle POST requests."""
        if self.path == '/shutdown':
            self.handle_shutdown()
        elif self.path == '/reboot':
            self.handle_reboot()
        elif self.path == '/schedule':
            self.handle_save_schedule()
        elif self.path == '/orientation':
            self.handle_save_orientation()
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

    def serve_photos_json(self):
        """Serve list of photos as JSON."""
        try:
            if not self.photos_dir.exists():
                photos = []
            else:
                # Get all image files
                extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp']
                photos = []
                for ext in extensions:
                    photos.extend([f'/photos/{p.name}' for p in self.photos_dir.glob(f'*{ext}')])
                    photos.extend([f'/photos/{p.name}' for p in self.photos_dir.glob(f'*{ext.upper()}')])
            
            content = json.dumps(photos).encode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
            
            logger.info(f"Served {len(photos)} photos in JSON")
            
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

    def handle_save_orientation(self):
        """Save orientation setting to config file."""
        global _config_path
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
            cfg.setdefault('frame', {})['orientation'] = new_orientation
            with open(_config_path, 'w') as f:
                yaml.dump(cfg, f, default_flow_style=False)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
        except Exception as e:
            logger.error(f"Error saving orientation: {e}")
            self.send_error(400, str(e))

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
    
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'config_frame.yaml'

    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        photos_dir = config.get('sync', {}).get('local_path', '/srv/frame/photos')
        viewer_dir = str(Path(__file__).parent / 'viewer')
        port = int(os.environ.get('PORT', 8080))
        
        # Ensure directories exist
        Path(photos_dir).mkdir(parents=True, exist_ok=True)
        
        slideshow_config = config.get('slideshow', {})

        # Start background services
        global _sysinfo_cache, _energy_save, _config_path
        _config_path = config_path
        _sysinfo_cache = SysinfoCache(photos_dir)
        _energy_save = EnergySaveManager()

        handler = create_handler(photos_dir, viewer_dir, slideshow_config)
        HTTPServer.allow_reuse_address = True
        server = HTTPServer(('0.0.0.0', port), handler)
        
        logger.info(f"Starting photo frame server on port {port}")
        logger.info(f"Photos directory: {photos_dir}")
        logger.info(f"Viewer directory: {viewer_dir}")
        logger.info(f"Open http://localhost:{port}/ to view")
        
        server.serve_forever()
        
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
