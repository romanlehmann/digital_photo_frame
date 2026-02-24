# Digital Photo Frame

A digital photo frame system for Raspberry Pi Zero 2 W with Synology NAS integration. Photos are automatically selected, resized, and prepared on the NAS, then synced to one or more Pi frames.

## How It Works

```
Synology NAS (weekly cron)
  prepare_photos.py
    ├── Fetches photos from Synology Photos (via share link)
    ├── Selects 200 photos (weighted random, favors unseen)
    ├── Generates horizontal/ (1920x1200) versions
    ├── Generates vertical/   (1200x1920) versions
    └── Blur-fills mismatched aspect ratios

        │ rsync (weekly, 1h later)
        ▼

Raspberry Pi Zero 2 W
  viewer_server.py (HTTP :8080)
    └── Chromium kiosk → slideshow with fade transitions
```

Portrait photos on a landscape frame (and vice versa) get a **blurred + darkened background fill** instead of black bars. Photos that nearly match the target aspect ratio are simply scaled to cover with a slight crop.

Each Pi frame syncs only the folder matching its orientation, so you can mount frames in either direction.

## Features

- **NAS-side processing** - heavy image work runs on the NAS, not the Pi
- **Multi-frame support** - one NAS prepares photos for any number of frames
- **Both orientations** - horizontal (1920x1200) and vertical (1200x1920)
- **Blur-fill backgrounds** - no black bars, ever
- **Smart selection** - weighted random favoring unseen photos, resets after cooldown
- **Web-based viewer** - fade transitions, shuffle, touch/swipe, hot corner shutdown
- **Automatic schedule** - NAS prepares weekly, Pi syncs 1 hour later

## Requirements

### Hardware
- Synology NAS (any model running DSM 7)
- Raspberry Pi Zero 2 W (or any Pi) per frame
- Display with 1920x1200 resolution (HDMI or DSI)

### Software
- Python 3.7+ on both NAS and Pi
- SSH key access from Pi to NAS (for rsync)

## Setup

### 1. NAS Setup

Install dependencies on the NAS:

```bash
pip3 install -r nas/requirements.txt
```

Edit `nas/config.yaml` — set your Synology Photos share link(s) and passphrase(s):

```yaml
synology:
  base_url: "http://localhost:5000"
  share_urls:
    - "https://photos.example.com/mo/sharing/AbCdEfG"
    - "https://photos.example.com/mo/sharing/HiJkLmN"
  share_passphrases:
    - "passphrase-for-first-album"
    - "passphrase-for-second-album"

selection:
  photos_per_week: 200
  state_db: "/volume2/docker/frame/state.db"

output:
  dir: "/volume2/docker/frame/frame_photos"
```

Test run:

```bash
python3 nas/prepare_photos.py nas/config.yaml
```

Set up a weekly scheduled task in **DSM > Control Panel > Task Scheduler**:
- Schedule: Monday 03:00
- Command: `python3 /path/to/nas/prepare_photos.py /path/to/nas/config.yaml`

### 2. Pi Setup

#### Install

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y git python3 python3-venv chromium-browser
git clone https://github.com/rwkaspar/digital_photo_frame.git
cd digital_photo_frame
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
```

#### Configure

Edit `config_frame.yaml` for this frame:

```yaml
frame:
  name: "living-room"          # Friendly name
  orientation: "horizontal"    # or "vertical"

sync:
  nas_host: "nas.local"        # NAS hostname or IP
  nas_user: "pi"               # SSH user on the NAS
  nas_path: "/volume2/docker/frame/frame_photos"
  local_path: "/srv/frame/photos"
```

#### SSH key setup (passwordless rsync)

```bash
ssh-keygen -t ed25519  # accept defaults
ssh-copy-id pi@nas.local
```

#### Test sync

```bash
chmod +x sync_from_nas.sh
./sync_from_nas.sh
```

#### Enable services

```bash
# Photo sync timer (weekly Monday 04:00, 1h after NAS prep)
sudo cp photo_frame_nas_sync.service /etc/systemd/system/
sudo cp photo_frame_nas_sync.timer /etc/systemd/system/
sudo systemctl enable --now photo_frame_nas_sync.timer

# Web viewer server
sudo cp photo_frame_server.service /etc/systemd/system/
sudo systemctl enable --now photo_frame_server.service

# Chromium kiosk
sudo cp photo_frame_viewer.service /etc/systemd/system/
sudo systemctl enable --now photo_frame_viewer.service
```

### 3. Pi Optimization

```bash
sudo raspi-config
# System Options > Boot / Auto Login > Console Autologin
# Performance Options > GPU Memory > 128
```

Disable screen blanking in `/boot/config.txt`:
```
hdmi_blanking=1
```

## Viewer Controls

- **ESC / Q** - Trigger shutdown dialog
- **SPACE / Right Arrow** - Skip to next image
- **Swipe** - Next image (touchscreen)
- **Hot corner** (top-left) - Shutdown Pi

## Project Structure

```
digital_photo_frame/
├── nas/
│   ├── prepare_photos.py      # NAS: photo selection + blur-fill processing
│   ├── config.yaml            # NAS: configuration
│   └── requirements.txt       # NAS: Python dependencies
├── viewer/
│   └── index.html             # Web-based slideshow viewer
├── viewer_server.py           # HTTP server (port 8080)
├── config_frame.yaml          # Pi: per-frame configuration
├── sync_from_nas.sh           # Pi: rsync photos from NAS
├── photo_frame_nas_sync.service  # Pi: systemd sync service
├── photo_frame_nas_sync.timer    # Pi: weekly sync timer
├── photo_frame_server.service    # Pi: HTTP server service
├── photo_frame_viewer.service    # Pi: Chromium kiosk service
└── requirements.txt           # Pi: Python dependencies
```

## Troubleshooting

### Sync not working

```bash
# Test SSH connectivity
ssh pi@nas.local ls /volume2/docker/frame/frame_photos/horizontal/

# Check sync timer
sudo systemctl status photo_frame_nas_sync.timer
journalctl -u photo_frame_nas_sync.service -n 20

# Manual sync
./sync_from_nas.sh
```

### No photos displaying

```bash
# Check photos exist locally
ls /srv/frame/photos/

# Check viewer server
sudo systemctl status photo_frame_server.service
curl http://localhost:8080/photos.json

# Check Chromium
sudo systemctl status photo_frame_viewer.service
```

### NAS prep failing

```bash
# Check logs
tail -f /volume2/docker/frame/prepare_photos.log

# Check output
ls /volume2/docker/frame/frame_photos/horizontal/ | wc -l
ls /volume2/docker/frame/frame_photos/vertical/ | wc -l
```

## License

This project is open source and available under the MIT License.
