# Digital Photo Frame

A digital photo frame system for Raspberry Pi Zero 2 W with Synology NAS integration. Photos are automatically selected, resized, and prepared on the NAS, then synced to one or more Pi frames over Tailscale.

## How It Works

```
Synology NAS (weekly Task Scheduler, Mon 03:00)
  prepare_photos.py
    ├── Connects to Synology Photos via share link(s)
    ├── Selects 200 photos (weighted random, favors unseen)
    ├── Skips videos, supports HEIC
    ├── Same-orientation: crop to fill (no bars)
    ├── Cross-orientation: blur-fill background
    ├── Generates horizontal/ (1920x1200)
    └── Generates vertical/   (1200x1920)

        │ rsync over Tailscale (weekly Mon 04:00)
        ▼

Raspberry Pi Zero 2 W
  mpv --vo=drm → slideshow directly on framebuffer
```

Frames can be anywhere with internet access — Tailscale mesh VPN connects them to the NAS without port forwarding.

## Features

- **NAS-side processing** — heavy image work runs on the NAS, not the Pi
- **Multi-frame support** — one NAS prepares photos for any number of frames
- **Both orientations** — horizontal (1920x1200) and vertical (1200x1920)
- **Smart fill** — same orientation crops to fill, cross-orientation gets blur-fill backgrounds
- **HEIC support** — handles iPhone photos natively via pillow-heif
- **Videos skipped** — automatically filters out .mov, .mp4, etc.
- **Smart selection** — weighted random favoring unseen photos, resets after cooldown
- **Lightweight viewer** — mpv with DRM output, no X11 or browser needed
- **Remote frames** — Tailscale VPN allows frames anywhere, not just your LAN
- **Automatic schedule** — NAS prepares weekly, Pi syncs 1 hour later

## Requirements

### Hardware
- Synology NAS (any model running DSM 7)
- Raspberry Pi Zero 2 W (or any Pi) per frame
- Display with HDMI input

### Software
- Python 3.7+ on both NAS and Pi
- Tailscale on NAS and each Pi (for remote access)
- SSH key access from Pi to NAS (for rsync)
- rsync service enabled on the NAS (DSM > Control Panel > File Services > rsync)

## Setup

### 1. NAS Setup

Create the working directory and venv:

```bash
mkdir -p /volume2/docker/frame/scripts
cd /volume2/docker/frame/scripts
python3 -m venv .venv
.venv/bin/pip install -r /path/to/nas/requirements.txt
```

Copy `nas/prepare_photos.py` and `nas/config.yaml` to `/volume2/docker/frame/scripts/`.

Edit `config.yaml` — set your Synology Photos share link(s) and passphrase(s):

```yaml
synology:
  local_api_base: "https://localhost:5443"
  share_urls:
    - "https://photos.example.com/mo/sharing/AbCdEfG"
  share_passphrases:
    - "your-passphrase"

selection:
  photos_per_week: 200
  state_db: "/volume2/docker/frame/state.db"

output:
  dir: "/volume2/docker/frame/frame_photos"
```

Test run:

```bash
.venv/bin/python prepare_photos.py config.yaml
```

Set up a weekly scheduled task in **DSM > Control Panel > Task Scheduler**:
- User: root
- Schedule: Monday 03:00
- Command: `/volume2/docker/frame/scripts/.venv/bin/python /volume2/docker/frame/scripts/prepare_photos.py /volume2/docker/frame/scripts/config.yaml`

### 2. Tailscale Setup

Install Tailscale on the NAS and each Pi so they can reach each other from anywhere:

- **NAS**: Install via Package Center or [Tailscale docs](https://tailscale.com/kb/1131/synology)
- **Pi**: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`

Note the NAS's Tailscale IP (e.g. `100.x.y.z`) for the Pi config.

### 3. Pi Setup

#### Install

```bash
sudo apt update
sudo apt install -y git python3 python3-venv mpv fbi
git clone https://github.com/rwkaspar/digital_photo_frame.git
cd digital_photo_frame
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

#### Configure

Edit `config_frame.yaml` for this frame:

```yaml
frame:
  name: "living-room"
  orientation: "horizontal"    # or "vertical"

sync:
  nas_host: "100.x.y.z"       # Tailscale IP of NAS
  nas_user: "robert"           # SSH user on the NAS
  nas_path: "/volume2/docker/frame/frame_photos"
  local_path: "/srv/frame/photos"

slideshow:
  interval: 30                 # seconds per photo
```

#### SSH key setup (passwordless rsync)

```bash
ssh-keygen -t ed25519 -N ""
ssh-copy-id robert@100.x.y.z
```

#### Test

```bash
# Test sync
chmod +x sync_from_nas.sh
./sync_from_nas.sh

# Test slideshow (Ctrl+C to stop)
sudo ./start_slideshow.sh
```

#### Enable services

```bash
# Photo sync timer (weekly Monday 04:00)
sudo cp photo_frame_nas_sync.service /etc/systemd/system/
sudo cp photo_frame_nas_sync.timer /etc/systemd/system/
sudo systemctl enable --now photo_frame_nas_sync.timer

# Slideshow (starts on boot)
sudo cp photo_frame_fbi.service /etc/systemd/system/
sudo systemctl enable --now photo_frame_fbi
```

## Project Structure

```
digital_photo_frame/
├── nas/
│   ├── prepare_photos.py        # NAS: photo selection + processing
│   ├── config.yaml              # NAS: configuration template
│   ├── requirements.txt         # NAS: Pillow, pillow-heif, PyYAML, requests
│   └── README.md                # NAS: detailed DSM setup guide
├── start_slideshow.sh           # Pi: mpv DRM slideshow launcher
├── config_frame.yaml            # Pi: per-frame configuration
├── sync_from_nas.sh             # Pi: rsync photos from NAS
├── photo_frame_fbi.service      # Pi: systemd slideshow service
├── photo_frame_nas_sync.service # Pi: systemd sync service
├── photo_frame_nas_sync.timer   # Pi: weekly sync timer
└── requirements.txt             # Pi: Python dependencies
```

## Troubleshooting

### Sync not working

```bash
# Test SSH connectivity
ssh robert@100.x.y.z "ls /volume2/docker/frame/frame_photos/horizontal/"

# Check rsync is enabled on NAS: DSM > Control Panel > File Services > rsync

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

# Check slideshow service
sudo systemctl status photo_frame_fbi
journalctl -u photo_frame_fbi -n 20

# Test mpv directly
sudo mpv --vo=drm --drm-device=/dev/dri/card0 --image-display-duration=5 /srv/frame/photos/*.jpg
```

### NAS prep failing

```bash
# Check logs
tail -f /volume2/docker/frame/prepare_photos.log

# Check output
ls /volume2/docker/frame/frame_photos/horizontal/ | wc -l
ls /volume2/docker/frame/frame_photos/vertical/ | wc -l
```

### Black screen after reboot

```bash
# The DRM display may need a VT switch
sudo chvt 2; sleep 1; sudo chvt 1

# Or restart the slideshow
sudo systemctl restart photo_frame_fbi
```

## License

This project is open source and available under the MIT License.
