#!/bin/bash
# Deploy update to Pi — run on the Pi itself:
#   curl -fsSL https://raw.githubusercontent.com/rwkaspar/digital_photo_frame/main/scripts/deploy_update.sh | bash
set -e

REPO_DIR="$HOME/digital_photo_frame"
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "Error: $REPO_DIR not found. Clone the repo first:"
    echo "  git clone https://github.com/rwkaspar/digital_photo_frame.git"
    exit 1
fi
cd "$REPO_DIR"

echo "=== Updating Photo Frame ==="

# 1. Back up config (has real credentials)
HAS_CONFIG=false
if [ -f config_frame.yaml ]; then
    cp config_frame.yaml config_frame.yaml.bak
    HAS_CONFIG=true
    echo "Config backed up"
    git checkout config_frame.yaml
fi

# 2. Pull latest
git pull --ff-only
echo "Code updated"

# 3. Restore real config
if [ "$HAS_CONFIG" = true ]; then
    cp config_frame.yaml.bak config_frame.yaml
fi

# 5. Patch config: add new fields if missing
CFG="config_frame.yaml"

# Add setup_complete: true (already set up)
if ! grep -q '^setup_complete:' "$CFG"; then
    sed -i '1s/^/setup_complete: true\n/' "$CFG"
    echo "Added setup_complete: true"
fi

# Add energy_save section
if ! grep -q '^energy_save:' "$CFG"; then
    echo -e "\nenergy_save:\n  method: ddcci" >> "$CFG"
    echo "Added energy_save config"
fi

# Remove local_api_base (no longer used)
if grep -q 'local_api_base' "$CFG"; then
    sed -i '/local_api_base/d' "$CFG"
    echo "Removed local_api_base"
fi

# 6. Update pip deps if requirements changed
if [ -f venv/bin/pip ]; then
    venv/bin/pip install --quiet -r requirements.txt
    echo "Pip deps updated"
fi

# 7. Restart services
sudo systemctl restart photo_frame_server
sleep 2
sudo systemctl restart photo_frame_cage
echo "Services restarted"

echo "=== Done! ==="
