# Digital Photo Frame

A self-contained digital photo frame system for Raspberry Pi Zero 2 W. Each Pi connects directly to photo sources (Synology Photos, Google Photos, Immich) via Tailscale or HTTPS, downloads and processes photos on-device, and displays them as a full-screen slideshow in a Chromium kiosk.

## How It Works

```
Photo Sources
  ├── Synology Photos (share links via Tailscale)
  ├── Google Photos  (public shared album URLs)
  └── Immich         (share links)
        │
        ▼
Raspberry Pi Zero 2 W
  frame.sync
    ├── Connects to all configured albums
    ├── Downloads new photos, removes deleted ones
    ├── Processes into current orientation (blur-fill backgrounds)
    └── Keeps ≤100 photos in alternate orientation as buffer

  frame.server (HTTP :8080)
    ├── Serves slideshow viewer (labwc + Chromium kiosk)
    ├── Settings overlay (touch hot corner)
    ├── Remote config page (/remote)
    ├── First-time setup wizard
    ├── WiFi hotspot fallback (captive portal)
    └── Sleep schedule with touch-to-wake

  viewer/index.html
    └── Full-screen slideshow with fade transitions + swipe nav
```

## Features

- **Multi-source sync** — pull from Synology Photos, Google Photos, and Immich shared albums simultaneously
- **Self-contained Pi** — no NAS processing needed, everything runs on the Pi
- **First-time setup wizard** — guided 7-step setup (language, WiFi, Tailscale, settings, sleep test, albums, done)
- **WiFi hotspot fallback** — creates `PhotoFrame-Setup` hotspot with captive portal when no WiFi is available
- **Auto-update on boot** — pulls latest code from GitHub before starting
- **Orientation-aware** — processes photos only for the active orientation (horizontal/vertical), keeps 100 in the other as buffer
- **Smart fill** — same-orientation photos crop to fill, cross-orientation get blur-fill backgrounds
- **Screen resolution detection** — auto-detects display resolution via wlr-randr or framebuffer
- **HEIC support** — handles iPhone photos natively via pillow-heif
- **Touch UI** — swipe navigation, hot corner settings overlay, touch-to-wake from sleep
- **Remote config** — phone-friendly web UI at `/remote` to manage albums, trigger sync (QR code from settings)
- **Album names** — resolves and displays actual album names, sorted alphabetically
- **Sleep schedule** — configurable sleep/wake times per weekday, stops Chromium to free RAM, configurable backlight control (DDC/CI, DPMS, brightness, or black-only fallback)
- **Stale cleanup** — photos removed from source albums are automatically deleted locally
- **Bilingual** — English and German (i18n)

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

### Automated Setup

Clone the repo to the Pi and run the setup script:

```bash
git clone https://github.com/rwkaspar/digital_photo_frame.git
cd digital_photo_frame
sudo bash scripts/setup_pi.sh
```

This installs all dependencies, creates the venv, sets up systemd services, and reboots. On first boot, the setup wizard guides you through WiFi, Tailscale, frame settings, and album configuration.

### Manual Setup

```bash
sudo apt update
sudo apt install -y git python3-venv python3-dev labwc wlr-randr seatd chromium ddcutil i2c-tools network-manager
git clone https://github.com/rwkaspar/digital_photo_frame.git
cd digital_photo_frame
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo usermod -aG video,input,render,netdev,i2c $USER
```

### Configuration

Edit `config_frame.yaml` or configure via the setup wizard / remote UI:

```yaml
setup_complete: false  # Set to true after wizard completes

frame:
  name: "living-room"
  orientation: "horizontal"  # or "vertical"

synology:
  share_urls:
    # Use Tailscale IP directly in the URL
    - "https://100.101.43.67:5443/mo/sharing/AbCdEfG"
  share_passphrases:
    - "your-passphrase"

google_photos:
  share_urls:
    - "https://photos.app.goo.gl/yourAlbumLink"

immich:
  share_urls: []
  share_passphrases: []

photos:
  base_dir: "/srv/frame/photos"
  quality: 85
  blur_radius: 40
  blur_darken: 0.6
  horizontal:
    width: 1920
    height: 1200
  vertical:
    width: 1200
    height: 1920

slideshow:
  interval: 10
  fade_duration: 1.0

energy_save:
  method: "ddcci"  # ddcci | dpms | brightness | black_only
```

### Enable Services

```bash
sudo systemctl enable --now photo_frame_server photo_frame_cage
```

Sync runs manually ("Sync Now" button in settings) or automatically during sleep mode.

## Project Structure

```
digital_photo_frame/
├── frame/                     # Python package
│   ├── server.py              # HTTP server startup + handler factory
│   ├── routes.py              # All HTTP endpoints
│   ├── config.py              # AppState (replaces global singletons)
│   ├── energy.py              # EnergySaveManager + SysinfoCache
│   ├── sync.py                # PhotoSyncer (multi-source sync engine)
│   ├── database.py            # SQLite photo state DB
│   ├── processing.py          # Blur-fill photo processing (subprocess)
│   ├── wifi.py                # WiFi hotspot fallback manager
│   └── clients/
│       ├── synology.py        # Synology Photos API client
│       ├── google_photos.py   # Google Photos scraper
│       └── immich.py          # Immich API client
├── viewer/
│   ├── index.html             # Slideshow viewer (Chromium kiosk)
│   ├── remote.html            # Phone-friendly remote config UI
│   ├── setup.html             # WiFi setup captive portal page
│   └── wizard.html            # First-time setup wizard (7 steps)
├── systemd/                   # Systemd unit file references
├── scripts/
│   ├── setup_pi.sh            # First-boot Pi setup script
│   ├── update.sh              # Auto-update script (git pull + pip)
│   └── generate_defaults.py   # Generate placeholder photos
├── config_frame.yaml          # Per-frame configuration template
├── photo_sync.py              # Backward-compat shim → frame.sync
├── viewer_server.py           # Backward-compat shim → frame.server
└── requirements.txt
```

## Boot Flow

```
network-online.target
  → photo_frame_update.service  (git pull + pip install, fail-ok)
    → photo_frame_server.service  (frame.server)
        IF no WiFi → hotspot "PhotoFrame-Setup" + captive portal
        IF setup_complete=false → setup wizard
        IF normal → slideshow (no auto-sync)
      → photo_frame_cage.service  (labwc + Chromium kiosk)
```

## Remote Config (`/remote`)

Access from your phone by scanning the QR code shown in Settings > Photo Albums > Edit:

- **Synology Photos** — add/remove albums with share URL + passphrase
- **Google Photos** — add/remove shared album URLs (no auth needed)
- **Immich** — add/remove shared album links
- Album names are resolved automatically and displayed alphabetically
- Save persists to `config_frame.yaml`
- "Sync Now" triggers an immediate sync run

## Sleep Mode

Sleep mode stops Chromium (frees ~125MB RAM) and displays a black screen:

1. Stops `photo_frame_cage` service
2. Fills framebuffer with black (keeps HDMI signal active — no blue "no signal")
3. Turns off backlight via configured method:
   - `ddcci` — DDC/CI power mode standby (`ddcutil setvcp d6 4/1`)
   - `dpms` — Wayland DPMS (`wlopm --off/--on`)
   - `brightness` — DDC/CI brightness to 0 (`ddcutil setvcp 10 0/100`)
   - `black_only` — framebuffer only, no backlight control (fallback)
4. Triggers photo sync (more RAM available with Chromium stopped)
5. Listens for touch input to wake

Wake starts cage first, waits 5s for Chromium to render, then restores backlight. The setup wizard tests each method interactively to find the one that works for your monitor.

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

If the monitor shows a blue "no signal" screen during sleep, try a different sleep method in the config:

```bash
# Test DDC/CI power mode (default)
sudo ddcutil setvcp d6 4   # standby
sudo ddcutil setvcp d6 1   # wake

# Test DPMS
wlopm --off '*'
wlopm --on '*'

# Test brightness
sudo ddcutil setvcp 10 0    # off
sudo ddcutil setvcp 10 80   # restore
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
