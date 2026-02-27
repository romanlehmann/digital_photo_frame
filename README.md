# Digital Photo Frame

A self-contained digital photo frame system for Raspberry Pi Zero 2 W. Each Pi connects directly to Synology Photos and/or Google Photos shared albums via Tailscale, downloads and processes photos on-device, and displays them as a full-screen slideshow in a Chromium kiosk.

## How It Works

```
Photo Sources
  ├── Synology Photos (share links via Tailscale)
  └── Google Photos  (public shared album URLs)
        │
        ▼
Raspberry Pi Zero 2 W
  photo_sync.py
    ├── Connects to all configured albums
    ├── Downloads new photos, removes deleted ones
    ├── Processes into current orientation (blur-fill backgrounds)
    ├── Keeps ≤100 photos in alternate orientation as buffer
    └── Batched sync (10/run) to stay within RAM limits

  viewer_server.py (HTTP :8080)
    ├── Serves slideshow viewer (labwc + Chromium kiosk)
    ├── Settings overlay (touch hot corner)
    ├── Remote config page (/remote)
    └── Sleep schedule with touch-to-wake

  viewer/index.html
    └── Full-screen slideshow with fade transitions + swipe nav
```

## Features

- **Multi-source sync** — pull from Synology Photos and Google Photos shared albums simultaneously
- **Self-contained Pi** — no NAS processing needed, everything runs on the Pi
- **Orientation-aware** — processes photos only for the active orientation (horizontal/vertical), keeps 100 in the other as buffer
- **Smart fill** — same-orientation photos crop to fill, cross-orientation get blur-fill backgrounds
- **HEIC support** — handles iPhone photos natively via pillow-heif
- **Touch UI** — swipe navigation, hot corner settings overlay, touch-to-wake from sleep
- **Remote config** — phone-friendly web UI at `/remote` to manage albums, trigger sync
- **Album names** — resolves and displays actual album names, sorted alphabetically
- **Sleep schedule** — configurable sleep/wake times, stops Chromium to free RAM, DDC/CI backlight off, black framebuffer (no blue "no signal" screen)
- **Stale cleanup** — photos removed from source albums are automatically deleted locally
- **Orientation switch** — trims old orientation to 100 photos, syncs new orientation immediately

## Requirements

### Hardware
- Raspberry Pi Zero 2 W (or any Pi)
- Display with HDMI input (tested: ANMITE 14" 1920x1200 touchscreen)
- Optional: touchscreen for swipe/settings

### Software
- Raspberry Pi OS Trixie (Debian 13)
- Python 3.11+, labwc, Chromium, seatd
- Tailscale (for Synology Photos access over VPN)

## Setup

### 1. Pi Setup

```bash
sudo apt update
sudo apt install -y git python3 python3-venv labwc chromium seatd ddcutil
git clone https://github.com/rwkaspar/digital_photo_frame.git
cd digital_photo_frame
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Add user to required groups:
```bash
sudo usermod -aG video,input,render robert
```

### 2. Tailscale (for Synology access)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Note the NAS's Tailscale IP (e.g. `100.101.43.67`).

### 3. Configure

Edit `config_frame.yaml`:

```yaml
frame:
  name: "living-room"
  orientation: "horizontal"    # or "vertical"

synology:
  local_api_base: "https://100.101.43.67:5443"
  share_urls:
    - "https://photos.example.com/mo/sharing/AbCdEfG"
  share_passphrases:
    - "your-passphrase"

google_photos:
  share_urls:
    - "https://photos.app.goo.gl/yourAlbumLink"

photos:
  base_dir: "/srv/frame/photos"
  batch_size: 10
  quality: 85

slideshow:
  interval: 10
  fade_duration: 1.0
```

Or configure via the phone-friendly web UI at `http://<pi-ip>:8080/remote`.

### 4. Enable Services

```bash
sudo systemctl enable --now photo_frame_server
sudo systemctl enable --now photo_frame_cage
```

The server starts a boot sync 60 seconds after startup. Additional syncs run during sleep mode when Chromium is stopped (more RAM available).

## Project Structure

```
digital_photo_frame/
├── photo_sync.py              # Photo sync engine (Synology + Google Photos)
├── viewer_server.py           # HTTP server, settings, sleep, sync management
├── config_frame.yaml          # Per-frame configuration
├── viewer/
│   ├── index.html             # Slideshow viewer (Chromium kiosk)
│   └── remote.html            # Phone-friendly remote config UI
├── nas/                       # Legacy NAS-side scripts
│   ├── prepare_photos.py
│   └── README.md
└── requirements.txt           # Python dependencies
```

## Remote Config (`/remote`)

The remote config page lets you manage photo sources from your phone:

- **Synology Photos** — add/remove albums with share URL + passphrase
- **Google Photos** — add/remove shared album URLs (no auth needed)
- Album names are resolved automatically and displayed alphabetically
- Save persists to `config_frame.yaml`
- Sync Now triggers an immediate sync run

## Sleep Mode

Sleep mode stops Chromium (frees ~125MB RAM) and displays a black screen:

1. Stops `photo_frame_cage` service
2. Fills framebuffer with black (keeps HDMI signal active — no blue "no signal")
3. Turns off backlight via DDC/CI (`ddcutil setvcp 10 0`)
4. Triggers photo sync (more RAM available with Chromium stopped)
5. Listens for touch input to wake

Wake restores backlight and restarts the display service.

## Troubleshooting

### Sync not working

```bash
# Check sync status
curl http://localhost:8080/sync/status

# Check logs
journalctl -u photo_frame_server -n 50

# Trigger manual sync
curl -X POST http://localhost:8080/sync/trigger
```

### Blue screen during sleep

If the monitor shows a blue "no signal" screen during sleep, the framebuffer blank isn't working. Check:

```bash
# Test framebuffer black manually
sudo dd if=/dev/zero of=/dev/fb0 bs=1M count=10

# Test DDC/CI
ddcutil detect
ddcutil setvcp 10 0   # brightness off
ddcutil setvcp 10 80  # brightness restore
```

### No photos displaying

```bash
# Check photos exist
ls /srv/frame/photos/horizontal/ | wc -l
ls /srv/frame/photos/vertical/ | wc -l

# Check orientation
grep orientation config_frame.yaml

# Restart display
sudo systemctl restart photo_frame_cage
```

## License

This project is open source and available under the MIT License.
