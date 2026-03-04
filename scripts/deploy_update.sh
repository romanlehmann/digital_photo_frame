#!/bin/bash
# Update an already-running photo frame to the latest version.
# Run on the Pi: bash ~/digital_photo_frame/scripts/deploy_update.sh
#
# For FIRST-TIME setup, use prepare_sd.sh on the dev machine instead.
set -e

REPO_DIR="$HOME/digital_photo_frame"
cd "$REPO_DIR"

echo "=== Updating Photo Frame ==="

# 1. Back up config
cp config_frame.yaml config_frame.yaml.bak
echo "Config backed up"

# 2. Pull latest
git checkout config_frame.yaml
git pull --ff-only
echo "Code updated"

# 3. Restore config
cp config_frame.yaml.bak config_frame.yaml

# 4. Patch config: add new fields if missing
if ! grep -q '^setup_complete:' config_frame.yaml; then
    sed -i '1s/^/setup_complete: true\n/' config_frame.yaml
fi
if ! grep -q '^energy_save:' config_frame.yaml; then
    echo -e "\nenergy_save:\n  method: ddcci" >> config_frame.yaml
fi
if grep -q 'local_api_base' config_frame.yaml; then
    sed -i '/local_api_base/d' config_frame.yaml
fi

# 5. Update pip deps
venv/bin/pip install --quiet -r requirements.txt
echo "Deps updated"

# 6. Restart services
sudo systemctl restart photo_frame_server
sleep 2
sudo systemctl restart photo_frame_cage
echo "Services restarted"

echo "=== Done! ==="
