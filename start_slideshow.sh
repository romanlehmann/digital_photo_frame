#!/bin/bash
# Framebuffer slideshow for digital photo frame.
# Uses mpv with DRM output — works with KMS on modern Pi OS.
# Lightweight enough for Pi Zero 2W (512MB RAM).
#
# Usage: ./start_slideshow.sh [config_file]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${1:-${SCRIPT_DIR}/config_frame.yaml}"

if [ ! -f "$CONFIG" ]; then
    echo "Error: config file not found: $CONFIG"
    exit 1
fi

# Parse YAML values (simple grep, no extra dependencies)
get_yaml() {
    grep "^  $1:" "$CONFIG" | head -1 | sed "s/.*: *\"\{0,1\}\([^\"]*\)\"\{0,1\}/\1/" | tr -d ' '
}

LOCAL_PATH=$(get_yaml local_path)
INTERVAL=$(get_yaml interval)
INTERVAL="${INTERVAL:-10}"

PHOTOS_DIR="${LOCAL_PATH}"

if [ ! -d "$PHOTOS_DIR" ]; then
    echo "Error: photos directory not found: $PHOTOS_DIR"
    exit 1
fi

# Count photos
PHOTO_COUNT=$(find "$PHOTOS_DIR" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | wc -l)

if [ "$PHOTO_COUNT" -eq 0 ]; then
    echo "Error: no photos found in $PHOTOS_DIR"
    exit 1
fi

echo "Starting slideshow: $PHOTO_COUNT photos, ${INTERVAL}s interval"

# Build a playlist file (shuffled)
PLAYLIST="/tmp/frame_playlist.txt"
find "$PHOTOS_DIR" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | sort -R > "$PLAYLIST"

# Launch mpv with DRM video output (no X11 needed)
exec mpv \
    --vo=drm \
    --image-display-duration="$INTERVAL" \
    --loop-playlist=inf \
    --shuffle \
    --no-audio \
    --no-osc \
    --no-input-default-bindings \
    --really-quiet \
    --playlist="$PLAYLIST"
