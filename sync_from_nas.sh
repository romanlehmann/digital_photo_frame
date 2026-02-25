#!/bin/bash
# Sync photos from NAS to Pi.
# Pulls the correct orientation folder based on config_frame.yaml.
#
# Usage: ./sync_from_nas.sh [config_file]
# Default config: config_frame.yaml (next to this script)

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

ORIENTATION=$(get_yaml orientation)
NAS_HOST=$(get_yaml nas_host)
NAS_USER=$(get_yaml nas_user)
NAS_PATH=$(get_yaml nas_path)
LOCAL_PATH=$(get_yaml local_path)

if [ -z "$ORIENTATION" ] || [ -z "$NAS_HOST" ] || [ -z "$NAS_PATH" ] || [ -z "$LOCAL_PATH" ]; then
    echo "Error: missing required config values"
    exit 1
fi

SRC="${NAS_USER}@${NAS_HOST}:${NAS_PATH}/${ORIENTATION}/"
DEST="${LOCAL_PATH}/"

mkdir -p "$DEST"

echo "$(date -Iseconds) Syncing ${ORIENTATION} photos from ${SRC}"
rsync -avz --delete -e ssh "$SRC" "$DEST"
echo "$(date -Iseconds) Sync complete"
