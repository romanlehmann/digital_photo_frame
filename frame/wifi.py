"""WiFi management: connectivity check, hotspot fallback, captive portal support."""

import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

HOTSPOT_SSID = 'PhotoFrame-Setup'
HOTSPOT_PASSWORD = 'photoframe'
HOTSPOT_CON_NAME = 'PhotoFrame-Hotspot'


class WiFiManager:
    """Manages WiFi connectivity and hotspot fallback."""

    def __init__(self, recovery_enabled=True, recovery_timeout_sec=600, recovery_check_interval_sec=30):
        self.mode = 'normal'  # 'normal' or 'hotspot'
        self._lock = threading.Lock()
        self.recovery_enabled = bool(recovery_enabled)
        self.recovery_timeout_sec = max(60, int(recovery_timeout_sec))
        self.recovery_check_interval_sec = max(10, int(recovery_check_interval_sec))
        self._watchdog_stop_event = threading.Event()
        self._watchdog_thread = None
        self._last_connected_at = time.time()
        self._last_recovery_attempt_at = 0.0

    def check_connectivity(self):
        """Check if wlan0 has a working non-hotspot WiFi connection."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device'],
                capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 4 and parts[0] == 'wlan0' and parts[2] == 'connected':
                    if parts[3] != HOTSPOT_CON_NAME:
                        return True
            return False
        except Exception as e:
            logger.error(f"Connectivity check failed: {e}")
            return False

    def start_watchdog(self):
        """Start background WiFi recovery monitoring."""
        with self._lock:
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop_event.clear()
            self._last_connected_at = time.time()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name='wifi-recovery-watchdog',
            )
            self._watchdog_thread.start()
            logger.info(
                "WiFi recovery watchdog started "
                f"(enabled={self.recovery_enabled}, timeout={self.recovery_timeout_sec}s)"
            )

    def stop_watchdog(self):
        """Stop background WiFi recovery monitoring."""
        self._watchdog_stop_event.set()
        thread = self._watchdog_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._watchdog_thread = None

    def set_recovery_enabled(self, enabled):
        """Enable/disable automatic WiFi recovery."""
        with self._lock:
            self.recovery_enabled = bool(enabled)
            if self.recovery_enabled:
                self._last_connected_at = time.time()
                self._last_recovery_attempt_at = 0.0
        logger.info(f"WiFi recovery enabled set to {self.recovery_enabled}")

    def _watchdog_loop(self):
        while not self._watchdog_stop_event.wait(self.recovery_check_interval_sec):
            try:
                if self.mode != 'normal':
                    self._last_connected_at = time.time()
                    continue

                now = time.time()
                if self.check_connectivity():
                    self._last_connected_at = now
                    continue

                if not self.recovery_enabled:
                    continue

                offline_for = now - self._last_connected_at
                cooldown = now - self._last_recovery_attempt_at
                if offline_for < self.recovery_timeout_sec:
                    continue
                if cooldown < self.recovery_timeout_sec:
                    continue

                logger.warning(
                    f"No WiFi connectivity for {int(offline_for)}s; attempting recovery"
                )
                self._last_recovery_attempt_at = now
                self._attempt_wifi_recovery()

                if self.check_connectivity():
                    self._last_connected_at = time.time()
                    logger.info("WiFi recovery successful")
                else:
                    logger.warning("WiFi recovery attempt finished without connectivity")
            except Exception as e:
                logger.error(f"WiFi recovery watchdog error: {e}")

    def _run_cmd(self, args, timeout=15):
        """Run subprocess command with logging."""
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            stderr = (result.stderr or '').strip()
            stdout = (result.stdout or '').strip()
            logger.warning(
                f"Command failed ({result.returncode}): {' '.join(args)} "
                f"{stderr or stdout}"
            )
        return result

    def _get_active_connection_name(self):
        """Return active wlan0 connection profile name or empty string."""
        try:
            result = self._run_cmd(
                ['nmcli', '-t', '-f', 'GENERAL.CONNECTION', 'device', 'show', 'wlan0'],
                timeout=10,
            )
            if result.returncode != 0:
                return ''
            line = (result.stdout or '').strip()
            if ':' in line:
                return line.split(':', 1)[1].strip()
            return ''
        except Exception:
            return ''

    def _attempt_wifi_recovery(self):
        """Attempt to recover WiFi by cycling radio and reconnecting."""
        with self._lock:
            if self.mode != 'normal':
                return

            conn_name = self._get_active_connection_name()
            self._run_cmd(['nmcli', 'radio', 'wifi', 'off'], timeout=10)
            time.sleep(2)
            self._run_cmd(['nmcli', 'radio', 'wifi', 'on'], timeout=10)
            time.sleep(4)

            if conn_name and conn_name != '--':
                self._run_cmd(['nmcli', 'connection', 'up', conn_name], timeout=20)

            self._run_cmd(['nmcli', 'device', 'connect', 'wlan0'], timeout=20)

    def start_hotspot(self):
        """Start WiFi hotspot with captive portal redirect."""
        with self._lock:
            if self.mode == 'hotspot':
                return
            logger.info(f"Starting hotspot: {HOTSPOT_SSID}")
            try:
                # Disconnect any existing WiFi first
                subprocess.run(
                    ['nmcli', 'device', 'disconnect', 'wlan0'],
                    capture_output=True, timeout=10)
                time.sleep(1)

                # Start hotspot via NetworkManager
                result = subprocess.run(
                    ['nmcli', 'device', 'wifi', 'hotspot',
                     'ifname', 'wlan0',
                     'con-name', HOTSPOT_CON_NAME,
                     'ssid', HOTSPOT_SSID,
                     'password', HOTSPOT_PASSWORD],
                    capture_output=True, text=True, timeout=15)
                if result.returncode != 0:
                    logger.error(f"Hotspot start failed: {result.stderr}")
                    return

                # Redirect port 80 -> 8080 for captive portal detection
                subprocess.run(
                    ['sudo', 'iptables', '-t', 'nat', '-A', 'PREROUTING',
                     '-i', 'wlan0', '-p', 'tcp', '--dport', '80',
                     '-j', 'REDIRECT', '--to-port', '8080'],
                    capture_output=True, timeout=5)

                self.mode = 'hotspot'
                logger.info("Hotspot started successfully")
            except Exception as e:
                logger.error(f"Failed to start hotspot: {e}")

    def stop_hotspot(self):
        """Tear down hotspot and iptables rule."""
        with self._lock:
            if self.mode != 'hotspot':
                return
            logger.info("Stopping hotspot")
            try:
                # Remove iptables redirect
                subprocess.run(
                    ['sudo', 'iptables', '-t', 'nat', '-D', 'PREROUTING',
                     '-i', 'wlan0', '-p', 'tcp', '--dport', '80',
                     '-j', 'REDIRECT', '--to-port', '8080'],
                    capture_output=True, timeout=5)
                # Bring down and delete hotspot connection
                subprocess.run(
                    ['nmcli', 'connection', 'down', HOTSPOT_CON_NAME],
                    capture_output=True, timeout=10)
                subprocess.run(
                    ['nmcli', 'connection', 'delete', HOTSPOT_CON_NAME],
                    capture_output=True, timeout=10)
                self.mode = 'normal'
                logger.info("Hotspot stopped")
            except Exception as e:
                logger.error(f"Failed to stop hotspot: {e}")

    def scan_networks(self):
        """Scan for available WiFi networks. Returns list of dicts."""
        try:
            # Trigger rescan
            subprocess.run(
                ['nmcli', 'device', 'wifi', 'rescan'],
                capture_output=True, timeout=15)
            time.sleep(2)
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
                capture_output=True, text=True, timeout=10)
            networks = []
            seen = set()
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                # nmcli -t uses : as separator; SSID may contain escaped colons
                parts = line.split(':')
                if len(parts) < 3:
                    continue
                ssid = parts[0].replace('\\:', ':')
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                signal = 0
                try:
                    signal = int(parts[1])
                except ValueError:
                    pass
                security = parts[2] if len(parts) > 2 else ''
                networks.append({
                    'ssid': ssid,
                    'signal': signal,
                    'security': security,
                    'secured': bool(security and security != '--'),
                })
            # Sort by signal strength descending
            networks.sort(key=lambda n: n['signal'], reverse=True)
            return networks
        except Exception as e:
            logger.error(f"WiFi scan failed: {e}")
            return []

    def connect_to_network(self, ssid, password=''):
        """Stop hotspot, connect to network, verify. Restart hotspot on failure.

        Returns (success: bool, message: str).
        This is meant to be called from a background thread.
        """
        logger.info(f"Attempting to connect to: {ssid}")
        was_hotspot = self.mode == 'hotspot'

        # Stop hotspot if active
        if was_hotspot:
            self.stop_hotspot()
            time.sleep(2)

        try:
            # Try connecting
            cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
            if password:
                cmd += ['password', password]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                logger.warning(f"nmcli connect failed: {result.stderr}")
                if was_hotspot:
                    self.start_hotspot()
                return False, result.stderr.strip() or 'Connection failed'

            # Verify connectivity (wait for DHCP)
            for attempt in range(10):
                time.sleep(2)
                if self.check_connectivity():
                    logger.info(f"Connected to {ssid} successfully")
                    self.mode = 'normal'
                    self._last_connected_at = time.time()
                    return True, 'Connected'

            # Connection established but no IP / connectivity
            logger.warning(f"Connected to {ssid} but no connectivity")
            if was_hotspot:
                subprocess.run(
                    ['nmcli', 'connection', 'down', ssid],
                    capture_output=True, timeout=10)
                self.start_hotspot()
            return False, 'Connected but no internet access'

        except Exception as e:
            logger.error(f"Connect error: {e}")
            if was_hotspot:
                self.start_hotspot()
            return False, str(e)

    def get_status(self):
        """Return current WiFi status."""
        status = {
            'mode': self.mode,
            'ssid': '',
            'ip': '',
            'hotspot_ssid': HOTSPOT_SSID,
            'hotspot_password': HOTSPOT_PASSWORD,
            'network_recovery_enabled': self.recovery_enabled,
            'network_recovery_timeout_sec': self.recovery_timeout_sec,
        }
        try:
            if self.mode == 'normal':
                result = subprocess.run(
                    ['iwgetid', '-r'], capture_output=True, text=True, timeout=5)
                status['ssid'] = result.stdout.strip()
                result = subprocess.run(
                    ['hostname', '-I'], capture_output=True, text=True, timeout=5)
                ips = result.stdout.strip().split()
                status['ip'] = ips[0] if ips else ''
        except Exception:
            pass
        return status
