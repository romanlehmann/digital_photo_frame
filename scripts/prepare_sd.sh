#!/bin/bash
# Prepare a freshly-flashed Raspberry Pi SD card for photo frame setup.
# Works via the FAT32 boot partition — compatible with Windows, macOS, and Linux.
#
# Usage:
#   1. Flash Raspberry Pi OS with Pi Imager:
#      - User: frame_user (set a password)
#      - SSH: enabled
#      - WiFi: optional (hotspot fallback will handle it)
#   2. Re-insert the SD card
#   3. Run: bash scripts/prepare_sd.sh [boot_mount]
#
# On first boot, the bootstrap script copies the repo from the boot partition
# to the user's home directory and installs the full setup service.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# ---- Find boot partition ----
if [ -n "$1" ]; then
    BOOT="$1"
else
    # Auto-detect: look for FAT32 boot partition with cmdline.txt
    for candidate in \
        /media/*/bootfs \
        /media/*/*bootfs* \
        /media/*/boot \
        /run/media/*/bootfs \
        /run/media/*/boot \
        /Volumes/bootfs \
        /Volumes/boot \
        /mnt/bootfs \
        /mnt/boot; do
        if [ -f "$candidate/cmdline.txt" ]; then
            BOOT="$candidate"
            break
        fi
    done
fi

if [ -z "$BOOT" ] || [ ! -f "$BOOT/cmdline.txt" ]; then
    echo "ERROR: Could not find boot partition (FAT32 with cmdline.txt)."
    echo ""
    echo "Usage: $0 [/path/to/boot/partition]"
    echo ""
    echo "After flashing with Pi Imager, re-insert the SD card."
    echo "Common mount points:"
    echo "  Linux:  /media/$USER/bootfs"
    echo "  macOS:  /Volumes/bootfs"
    exit 1
fi

echo "Using boot partition at: $BOOT"

# ---- Copy repo to boot partition ----
DEST="$BOOT/photo_frame"
echo "Copying repo to ${DEST}..."
rm -rf "$DEST"
mkdir -p "$DEST"

rsync -a \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='nas/' \
    --exclude='.claude/' \
    --exclude='config_frame.yaml' \
    "$REPO_DIR/" "$DEST/"

# Include .git for auto-update (git pull)
rsync -a "$REPO_DIR/.git" "$DEST/"

echo "Repo copied ($(du -sh "$DEST" | cut -f1))"

# ---- Copy bootstrap script to boot root ----
cp "$SCRIPT_DIR/photo_frame_bootstrap.sh" "$BOOT/photo_frame_bootstrap.sh"

# ---- Inject bootstrap call into first-boot mechanism ----
BOOTSTRAP_CMD="/bin/bash /boot/firmware/photo_frame_bootstrap.sh"
INJECTED=false

# Strategy 1: cloud-init user-data (Pi Imager 2.0+ / Trixie)
if [ -f "$BOOT/user-data" ]; then
    if ! grep -q "photo_frame_bootstrap" "$BOOT/user-data"; then
        echo "Injecting bootstrap into cloud-init user-data..."
        if grep -q "^runcmd:" "$BOOT/user-data"; then
            # Append to existing runcmd section
            sed -i "/^runcmd:/a\\  - ${BOOTSTRAP_CMD}" "$BOOT/user-data"
        else
            # Add runcmd section at end
            printf "\nruncmd:\n  - %s\n" "$BOOTSTRAP_CMD" >> "$BOOT/user-data"
        fi
        INJECTED=true
    else
        echo "Bootstrap already in user-data"
        INJECTED=true
    fi
fi

# Strategy 2: firstrun.sh (Legacy Bookworm / Pi Imager <2.0)
if [ "$INJECTED" = false ] && [ -f "$BOOT/firstrun.sh" ]; then
    if ! grep -q "photo_frame_bootstrap" "$BOOT/firstrun.sh"; then
        echo "Injecting bootstrap into firstrun.sh..."
        if grep -q "^exit 0" "$BOOT/firstrun.sh"; then
            sed -i "/^exit 0/i\\${BOOTSTRAP_CMD}" "$BOOT/firstrun.sh"
        else
            echo "$BOOTSTRAP_CMD" >> "$BOOT/firstrun.sh"
        fi
        INJECTED=true
    else
        echo "Bootstrap already in firstrun.sh"
        INJECTED=true
    fi
fi

# Strategy 3: cmdline.txt systemd.run= (universal fallback)
if [ "$INJECTED" = false ]; then
    echo "Injecting bootstrap into cmdline.txt (systemd.run fallback)..."
    if ! grep -q "photo_frame_bootstrap" "$BOOT/cmdline.txt"; then
        sed -i "s|$| systemd.run=${BOOTSTRAP_CMD} systemd.run_success_action=reboot|" "$BOOT/cmdline.txt"
    fi
    INJECTED=true
fi

# ---- Summary ----
INJECT_METHOD="unknown"
if [ -f "$BOOT/user-data" ] && grep -q "photo_frame_bootstrap" "$BOOT/user-data"; then
    INJECT_METHOD="cloud-init (user-data)"
elif [ -f "$BOOT/firstrun.sh" ] && grep -q "photo_frame_bootstrap" "$BOOT/firstrun.sh"; then
    INJECT_METHOD="firstrun.sh"
else
    INJECT_METHOD="cmdline.txt (systemd.run)"
fi

# ---- Eject SD card ----
echo ""
echo "Ejecting SD card..."
sync
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    diskutil eject "$BOOT" 2>/dev/null && echo "SD card ejected safely." || echo "Could not eject - please remove safely."
else
    # Linux: unmount all partitions of the same device
    DEVICE=$(df "$BOOT" 2>/dev/null | tail -1 | awk '{print $1}' | sed 's/[0-9]*$//')
    if [ -n "$DEVICE" ] && [ -b "$DEVICE" ]; then
        udisksctl power-off -b "$DEVICE" 2>/dev/null && echo "SD card ejected safely." \
            || (umount "$BOOT" 2>/dev/null && echo "SD card unmounted. Safe to remove.") \
            || echo "Could not eject - please remove safely."
    else
        umount "$BOOT" 2>/dev/null && echo "SD card unmounted. Safe to remove." || echo "Could not eject - please remove safely."
    fi
fi

echo ""
echo "=== SD card prepared! ==="
echo ""
echo "Injection method: ${INJECT_METHOD}"
echo ""
echo "What happens next:"
echo "  1. Insert the SD card into the Pi"
echo "  2. First boot: Pi Imager settings apply (user, WiFi, SSH)"
echo "     Then bootstrap copies repo and installs setup service - reboot"
echo "  3. Second boot: Full setup runs (packages, venv, config) - reboot"
echo "  4. Third boot: Photo frame starts, wizard on screen"
echo ""
echo "Monitor progress: ssh frame_user@<ip> journalctl -fu photo-frame-firstboot"
