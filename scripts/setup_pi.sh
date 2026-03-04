#!/bin/bash
# Photo Frame — First-boot setup script
# Runs as root on the Pi. Expects the repo in the user's home directory
# (copied there by photo_frame_bootstrap.sh on first boot).
set -e

# Detect user and paths from script location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
FRAME_HOME="$(dirname "$REPO_DIR")"
FRAME_USER="$(basename "$FRAME_HOME")"
PHOTOS_DIR="/srv/frame/photos"

echo "=== Photo Frame Setup ==="

# ---- Verify ----
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root"
    exit 1
fi
if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: repo not found at $REPO_DIR"
    exit 1
fi

# ---- System packages ----
echo "Installing packages..."
apt-get update -qq
apt-get install -y -qq \
    git python3-venv python3-dev \
    labwc wlr-randr seatd \
    chromium \
    ddcutil i2c-tools \
    network-manager \
    libjpeg-dev zlib1g-dev libffi-dev libheif-dev \
    fonts-noto-color-emoji \
    2>/dev/null

# ---- User groups ----
echo "Setting up user groups..."
usermod -aG video,input,render,netdev,i2c "${FRAME_USER}"

# ---- Photos directory ----
echo "Creating photos directory..."
mkdir -p "${PHOTOS_DIR}/horizontal" "${PHOTOS_DIR}/vertical"
# Copy default placeholder images so the frame has something to show immediately
if [ -d "${REPO_DIR}/viewer/defaults/horizontal" ]; then
    cp "${REPO_DIR}/viewer/defaults/horizontal/"*.jpg "${PHOTOS_DIR}/horizontal/" 2>/dev/null || true
    cp "${REPO_DIR}/viewer/defaults/vertical/"*.jpg "${PHOTOS_DIR}/vertical/" 2>/dev/null || true
fi
chown -R "${FRAME_USER}:${FRAME_USER}" "${PHOTOS_DIR}"

# ---- Python venv ----
echo "Setting up Python venv..."
cd "$REPO_DIR"
su - "${FRAME_USER}" -c "cd ${REPO_DIR} && python3 -m venv venv && venv/bin/pip install --quiet -r requirements.txt"

# ---- Config file ----
if [ ! -f "${REPO_DIR}/config_frame.yaml" ]; then
    echo "Creating default config..."
    cat > "${REPO_DIR}/config_frame.yaml" << 'YAML'
setup_complete: false
frame:
  name: photo-frame
  orientation: horizontal
photos:
  base_dir: /srv/frame/photos
  blur_darken: 0.6
  blur_radius: 40
  horizontal:
    width: 1920
    height: 1200
  vertical:
    width: 1200
    height: 1920
  quality: 85
  state_db: /srv/frame/photos/state.db
  tmp_dir: /tmp/frame_downloads
slideshow:
  interval: 10
  fade_duration: 1.0
  transition: fade
synology:
  share_urls: []
  share_passphrases: []
google_photos:
  share_urls: []
immich:
  share_urls: []
  share_passphrases: []
energy_save:
  method: ddcci
logging:
  level: INFO
  file: /var/log/photo_frame.log
YAML
    chown "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}/config_frame.yaml"
fi

# ---- labwc config (uses XDG_CONFIG_HOME pointed at repo dir) ----
echo "Configuring labwc..."
# autostart needs frame_user paths
cat > "${REPO_DIR}/labwc/autostart" << 'LABWC_AUTOSTART'
# Apply display rotation from config
ORIENTATION=$(python3 -c "
import yaml
try:
    with open('$REPO_DIR_PLACEHOLDER/config_frame.yaml') as f:
        print(yaml.safe_load(f).get('frame',{}).get('orientation','horizontal'))
except: print('horizontal')
" 2>/dev/null)
if [ "$ORIENTATION" = "vertical" ]; then
    wlr-randr --output HDMI-A-1 --transform 90
fi

/usr/lib/chromium/chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --check-for-update-interval=31536000 \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --disable-component-update \
    --disable-pinch \
    --enable-features=VirtualKeyboard \
    --ozone-platform=wayland \
    http://localhost:8080/ &
LABWC_AUTOSTART
# Fix the placeholder (heredoc can't expand within single quotes)
sed -i "s|\$REPO_DIR_PLACEHOLDER|${REPO_DIR}|g" "${REPO_DIR}/labwc/autostart"
chmod +x "${REPO_DIR}/labwc/autostart"

# ---- Systemd services ----
echo "Installing systemd services..."

cat > /etc/systemd/system/photo_frame_server.service << EOF
[Unit]
Description=Digital Photo Frame Viewer Server
After=network.target

[Service]
Type=simple
User=${FRAME_USER}
WorkingDirectory=${REPO_DIR}
Environment="PORT=8080"
ExecStart=${REPO_DIR}/venv/bin/python -m frame.server ${REPO_DIR}/config_frame.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photo_frame_cage.service << EOF
[Unit]
Description=Digital Photo Frame Viewer (labwc + Chromium)
After=network.target photo_frame_server.service
Wants=photo_frame_server.service

[Service]
Type=simple
User=${FRAME_USER}
SupplementaryGroups=video input render
TTYPath=/dev/tty1
Environment="XDG_RUNTIME_DIR=/tmp/frame-runtime"
Environment="XDG_CONFIG_HOME=${REPO_DIR}"
ExecStartPre=/bin/mkdir -p /tmp/frame-runtime
ExecStartPre=/bin/chmod 700 /tmp/frame-runtime
ExecStartPre=/bin/sleep 5
ExecStartPre=+${REPO_DIR}/scripts/set_touch_cal.sh
ExecStart=/usr/bin/labwc -s /bin/true
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photo_frame_update.service << EOF
[Unit]
Description=Digital Photo Frame Auto-Update
After=network-online.target
Wants=network-online.target
Before=photo_frame_server.service

[Service]
Type=oneshot
User=${FRAME_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/scripts/update.sh
TimeoutStartSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/photo_frame_update.timer << 'EOF'
[Unit]
Description=Daily auto-update for photo frame

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ---- Polkit rule (NetworkManager without sudo for frame_user) ----
echo "Setting up polkit..."
cat > /etc/polkit-1/rules.d/10-photo-frame-wifi.rules << EOF
// Allow photo frame user to manage WiFi via NetworkManager
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "${FRAME_USER}") {
        return polkit.Result.YES;
    }
});
EOF

# ---- Sudoers (iptables for captive portal) ----
echo "Setting up sudoers..."
cat > /etc/sudoers.d/photo-frame << EOF
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/sbin/iptables
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/sbin/ddcutil
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/wlopm
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop photo_frame_cage
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start photo_frame_cage
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart photo_frame_cage
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/dd
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/setterm
${FRAME_USER} ALL=(ALL) NOPASSWD: /sbin/shutdown
${FRAME_USER} ALL=(ALL) NOPASSWD: /sbin/reboot
${FRAME_USER} ALL=(ALL) NOPASSWD: /usr/bin/tailscale up *
${FRAME_USER} ALL=(ALL) NOPASSWD: /bin/sh -c curl -fsSL https\://tailscale.com/install.sh | sh
EOF
chmod 440 /etc/sudoers.d/photo-frame

# ---- Quiet boot ----
echo "Configuring quiet boot..."
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    # Add quiet boot params if not present
    for param in "quiet" "loglevel=0" "vt.global_cursor_default=0" "logo.nologo"; do
        if ! grep -q "$param" "$CMDLINE"; then
            sed -i "s/$/ $param/" "$CMDLINE"
        fi
    done
fi

# Disable login prompt on tty1
systemctl disable getty@tty1 2>/dev/null || true

# ---- Enable services ----
echo "Enabling services..."
systemctl daemon-reload
systemctl enable seatd photo_frame_server photo_frame_cage photo_frame_update.service photo_frame_update.timer
systemctl start seatd 2>/dev/null || true

# ---- Set repo ownership ----
chown -R "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}"

# ---- Initialize git repo if missing (e.g. deployed from ZIP download) ----
if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "Initializing git repo for auto-update..."
    su - "${FRAME_USER}" -c "cd ${REPO_DIR} && git init && git remote add origin https://github.com/rwkaspar/digital_photo_frame.git && git fetch origin main && git reset --mixed origin/main" 2>/dev/null || true
fi

# ---- Configure git safe directory (for auto-update as frame_user) ----
su - "${FRAME_USER}" -c "git config --global --add safe.directory ${REPO_DIR}"

# ---- Generate default placeholder photos ----
echo "Generating default photos..."
if [ -f "${REPO_DIR}/scripts/generate_defaults.py" ]; then
    su - "${FRAME_USER}" -c "cd ${REPO_DIR} && venv/bin/python scripts/generate_defaults.py" 2>/dev/null || true
fi

# ---- Touchscreen check (wait if not detected) ----
TOUCH_ID="27c0:0859"
if ! lsusb | grep -q "$TOUCH_ID"; then
    echo ""
    echo "============================================"
    echo "  No touchscreen detected!"
    echo "  Please unplug and replug the screen's"
    echo "  USB cable, then wait..."
    echo "============================================"
    while ! lsusb | grep -q "$TOUCH_ID"; do
        sleep 2
    done
    echo "Touchscreen found!"
    sleep 1
fi

echo ""
echo "=== Setup complete! ==="
echo "The frame will start automatically on next boot."
echo "If WiFi was not configured, the frame will create a"
echo "'PhotoFrame-Setup' hotspot for WiFi configuration."
echo ""
echo "Rebooting in 5 seconds..."
sleep 5
reboot
