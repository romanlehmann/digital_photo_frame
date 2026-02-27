#!/bin/bash
# Prepare a freshly-flashed Raspberry Pi SD card for photo frame auto-setup.
#
# Usage:
#   1. Flash Raspberry Pi OS with Pi Imager:
#      - User: frame_user (set a password)
#      - SSH: enabled
#      - WiFi: optional (hotspot fallback will handle it)
#   2. Re-insert the SD card so both partitions mount
#   3. Run: bash scripts/prepare_sd.sh [rootfs_mount]
#
# If rootfs_mount is not given, we auto-detect it.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
FRAME_USER="frame_user"

# ---- Find rootfs mount ----
if [ -n "$1" ]; then
    ROOTFS="$1"
else
    # Auto-detect: look for common Raspberry Pi rootfs mounts
    for candidate in /media/*/rootfs /mnt/rootfs /media/*/*; do
        if [ -d "$candidate/etc/systemd" ]; then
            ROOTFS="$candidate"
            break
        fi
    done
fi

if [ -z "$ROOTFS" ] || [ ! -d "$ROOTFS/etc" ]; then
    echo "ERROR: Could not find rootfs mount."
    echo "Usage: $0 /path/to/rootfs"
    echo ""
    echo "After flashing with Pi Imager, re-insert the SD card."
    echo "The rootfs partition should mount automatically (e.g. /media/$USER/rootfs)."
    exit 1
fi

echo "Using rootfs at: $ROOTFS"

# ---- Copy repo to SD card ----
DEST="${ROOTFS}/home/${FRAME_USER}/digital_photo_frame"
echo "Copying repo to ${DEST}..."
sudo mkdir -p "$DEST"
sudo rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='nas/' \
    "$REPO_DIR/" "$DEST/"
# .git dir needed for auto-update (git pull)
sudo rsync -a "$REPO_DIR/.git" "$DEST/"

# ---- Create first-boot systemd service ----
echo "Installing first-boot service..."
SERVICE_FILE="${ROOTFS}/etc/systemd/system/photo-frame-firstboot.service"
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Photo Frame First Boot Setup
After=network.target
ConditionPathExists=!/home/${FRAME_USER}/.photo-frame-setup-done

[Service]
Type=oneshot
ExecStart=/bin/bash /home/${FRAME_USER}/digital_photo_frame/scripts/setup_pi.sh
ExecStartPost=/usr/bin/touch /home/${FRAME_USER}/.photo-frame-setup-done
ExecStartPost=/bin/systemctl disable photo-frame-firstboot.service
TimeoutStartSec=600
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Enable the service via symlink
sudo mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
sudo ln -sf /etc/systemd/system/photo-frame-firstboot.service \
    "${ROOTFS}/etc/systemd/system/multi-user.target.wants/photo-frame-firstboot.service"

echo ""
echo "=== SD card prepared! ==="
echo ""
echo "What happens next:"
echo "  1. Eject the SD card and insert into the Pi"
echo "  2. Pi boots, creates user '${FRAME_USER}' (from Pi Imager settings)"
echo "  3. First-boot service installs all packages and configures everything"
echo "     (this takes a few minutes — the Pi will reboot when done)"
echo "  4. After reboot, the photo frame starts automatically"
echo "     - If WiFi was configured: slideshow starts (once photos sync)"
echo "     - If no WiFi: 'PhotoFrame-Setup' hotspot appears for setup"
echo ""
echo "You can monitor progress via: ssh ${FRAME_USER}@<ip> journalctl -fu photo-frame-firstboot"
