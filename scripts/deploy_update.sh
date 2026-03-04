#!/bin/bash
# Deploy/update Photo Frame on Pi — true one-liner:
#   curl -fsSL https://raw.githubusercontent.com/rwkaspar/digital_photo_frame/main/scripts/deploy_update.sh | bash
set -e

REPO_URL="https://github.com/rwkaspar/digital_photo_frame.git"
REPO_DIR="$HOME/digital_photo_frame"

echo "=== Photo Frame Deploy ==="

# 1. Install system deps if missing
NEED_APT=false
command -v git &>/dev/null || NEED_APT=true
[ -f /usr/include/zlib.h ] || NEED_APT=true
[ -f /usr/include/libheif/heif.h ] || NEED_APT=true
if [ "$NEED_APT" = true ]; then
    echo "Installing system packages..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq git python3-venv python3-dev \
        libjpeg-dev zlib1g-dev libffi-dev libheif-dev 2>/dev/null
fi

# 2. Clone or pull
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "Cloning repo..."
    git clone "$REPO_URL" "$REPO_DIR"
else
    # Back up config before pull
    if [ -f "$REPO_DIR/config_frame.yaml" ]; then
        cp "$REPO_DIR/config_frame.yaml" "$REPO_DIR/config_frame.yaml.bak"
        echo "Config backed up"
        git -C "$REPO_DIR" checkout config_frame.yaml
    fi
    git -C "$REPO_DIR" pull --ff-only
    # Restore config
    if [ -f "$REPO_DIR/config_frame.yaml.bak" ]; then
        cp "$REPO_DIR/config_frame.yaml.bak" "$REPO_DIR/config_frame.yaml"
    fi
fi
cd "$REPO_DIR"
echo "Code ready"

# 3. Patch config: add new fields if missing
CFG="config_frame.yaml"
if [ -f "$CFG" ]; then
    if ! grep -q '^setup_complete:' "$CFG"; then
        sed -i '1s/^/setup_complete: true\n/' "$CFG"
        echo "Added setup_complete: true"
    fi
    if ! grep -q '^energy_save:' "$CFG"; then
        echo -e "\nenergy_save:\n  method: ddcci" >> "$CFG"
        echo "Added energy_save config"
    fi
    if grep -q 'local_api_base' "$CFG"; then
        sed -i '/local_api_base/d' "$CFG"
        echo "Removed local_api_base"
    fi
fi

# 4. Set up venv + install deps
if [ ! -f venv/bin/pip ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi
venv/bin/pip install --quiet -r requirements.txt
echo "Deps installed"

# 5. Restart services (if they exist)
if systemctl list-unit-files photo_frame_server.service &>/dev/null; then
    sudo systemctl restart photo_frame_server
    sleep 2
    sudo systemctl restart photo_frame_cage
    echo "Services restarted"
else
    echo "Services not installed yet — run scripts/setup_pi.sh for first-time setup"
fi

echo "=== Done! ==="
