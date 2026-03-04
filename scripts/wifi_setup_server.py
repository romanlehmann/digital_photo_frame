#!/usr/bin/env python3
"""Minimal WiFi setup server for first-boot (stdlib only, no pip packages).

Starts a hotspot, serves setup.html, handles WiFi scan/connect via nmcli.
Exits when internet connectivity is established (WiFi configured or Ethernet).
Must run as root.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time

HOTSPOT_SSID = "PhotoFrame-Setup"
HOTSPOT_PASSWORD = "photoframe"
PORT = 80
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
VIEWER_DIR = os.path.join(REPO_DIR, "viewer")


def run_cmd(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


def has_internet():
    _, rc = run_cmd(["ping", "-c", "1", "-W", "3", "8.8.8.8"], timeout=5)
    return rc == 0


def start_hotspot():
    run_cmd(["nmcli", "device", "wifi", "hotspot",
             "ssid", HOTSPOT_SSID, "password", HOTSPOT_PASSWORD])
    # Captive portal: redirect all HTTP to us
    run_cmd(["iptables", "-t", "nat", "-A", "PREROUTING",
             "-p", "tcp", "--dport", "80", "-j", "REDIRECT", "--to-port", str(PORT)])


def stop_hotspot():
    run_cmd(["iptables", "-t", "nat", "-F", "PREROUTING"])
    out, _ = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"])
    for line in out.split("\n"):
        if "hotspot" in line.lower() or HOTSPOT_SSID in line:
            name = line.split(":")[0]
            run_cmd(["nmcli", "connection", "down", name])
            break


def scan_wifi():
    run_cmd(["nmcli", "device", "wifi", "rescan"])
    time.sleep(2)
    out, _ = run_cmd(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
    networks = []
    seen = set()
    for line in out.split("\n"):
        parts = line.split(":")
        if len(parts) >= 3:
            ssid = parts[0].strip()
            if not ssid or ssid in seen or ssid == HOTSPOT_SSID:
                continue
            seen.add(ssid)
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            networks.append({"ssid": ssid, "signal": signal, "secured": bool(parts[2].strip())})
    networks.sort(key=lambda x: -x["signal"])
    return networks


def connect_wifi(ssid, password):
    stop_hotspot()
    time.sleep(1)
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    out, rc = run_cmd(cmd, timeout=30)
    if rc == 0:
        return True, "Connected"
    # Restart hotspot on failure
    start_hotspot()
    return False, out


class SetupHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[wifi-setup] {args[0]}")

    def do_GET(self):
        # Captive portal detection URLs + main page all serve setup.html
        if self.path in ("/", "/setup", "/setup.html",
                         "/generate_204", "/hotspot-detect.html",
                         "/connecttest.txt", "/ncsi.txt"):
            self._serve_file("setup.html", "text/html")
        else:
            self._redirect("/setup")

    def do_POST(self):
        if self.path == "/api/wifi/scan":
            self._send_json({"networks": scan_wifi()})
        elif self.path == "/api/wifi/connect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            # Respond before disconnecting hotspot
            self._send_json({"ok": True, "message": "Connecting..."})
            threading.Thread(target=self._bg_connect,
                             args=(body.get("ssid", ""), body.get("password", "")),
                             daemon=True).start()
        else:
            self.send_error(404)

    def _bg_connect(self, ssid, password):
        ok, msg = connect_wifi(ssid, password)
        if ok:
            print(f"[wifi-setup] Connected to {ssid}! Exiting.")
            os._exit(0)
        else:
            print(f"[wifi-setup] Failed: {msg}")

    def _serve_file(self, filename, content_type):
        path = os.path.join(VIEWER_DIR, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()


def connectivity_watcher():
    """Exit if internet appears (e.g. Ethernet plugged in)."""
    while True:
        time.sleep(10)
        if has_internet():
            print("[wifi-setup] Internet detected, exiting.")
            stop_hotspot()
            os._exit(0)


def main():
    print(f"[wifi-setup] Starting hotspot '{HOTSPOT_SSID}' (password: {HOTSPOT_PASSWORD})")
    start_hotspot()

    threading.Thread(target=connectivity_watcher, daemon=True).start()

    server = http.server.HTTPServer(("", PORT), SetupHandler)
    print(f"[wifi-setup] Serving setup page on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_hotspot()


if __name__ == "__main__":
    main()
