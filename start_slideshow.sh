#!/bin/bash
# Framebuffer slideshow for digital photo frame.
# Uses fbi (framebuffer imageviewer) — no X11 or browser needed.
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

# Kill any existing fbi instance
killall fbi 2>/dev/null || true

# Launch fbi slideshow
#   -a          = auto-zoom to fit screen
#   -t N        = N seconds per photo
#   -u          = random order
#   -T 1        = use virtual terminal 1
#   --noverbose = hide filename/info bar
#   --blend N   = blend transition (milliseconds)
exec fbi \
    -a \
    -t "$INTERVAL" \
    -u \
    -T 1 \
    --noverbose \
    --blend 500 \
    "$PHOTOS_DIR"/*.jpg "$PHOTOS_DIR"/*.jpeg "$PHOTOS_DIR"/*.png 2>/dev/null
