#!/bin/bash
# Photo Frame — full deploy & setup in one command:
#   curl -fsSL https://raw.githubusercontent.com/rwkaspar/digital_photo_frame/main/scripts/deploy_update.sh | sudo bash
set -e

FRAME_USER="${SUDO_USER:-$(whoami)}"
FRAME_HOME="$(eval echo ~$FRAME_USER)"
REPO_URL="https://github.com/rwkaspar/digital_photo_frame.git"
REPO_DIR="${FRAME_HOME}/digital_photo_frame"
PHOTOS_DIR="/srv/frame/photos"

echo "=== Photo Frame Deploy (user: $FRAME_USER) ==="

# Must run as root (for apt, systemd, etc.)
if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

# ---- 1. System packages ----
echo "[1/8] System packages..."
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

# ---- 2. Clone or pull ----
echo "[2/8] Getting code..."
if [ ! -d "$REPO_DIR/.git" ]; then
    su - "$FRAME_USER" -c "git clone $REPO_URL $REPO_DIR"
else
    # Back up config before pull
    if [ -f "$REPO_DIR/config_frame.yaml" ]; then
        cp "$REPO_DIR/config_frame.yaml" "$REPO_DIR/config_frame.yaml.bak"
        su - "$FRAME_USER" -c "cd $REPO_DIR && git checkout config_frame.yaml && git pull --ff-only"
        cp "$REPO_DIR/config_frame.yaml.bak" "$REPO_DIR/config_frame.yaml"
    else
        su - "$FRAME_USER" -c "cd $REPO_DIR && git pull --ff-only"
    fi
fi

# ---- 3. Python venv + deps ----
echo "[3/8] Python environment..."
su - "$FRAME_USER" -c "cd $REPO_DIR && { [ -f venv/bin/pip ] || python3 -m venv venv; } && venv/bin/pip install --quiet -r requirements.txt"

# ---- 4. Photos directory ----
echo "[4/8] Photos directory..."
mkdir -p "${PHOTOS_DIR}/horizontal" "${PHOTOS_DIR}/vertical"
if [ -d "${REPO_DIR}/viewer/defaults/horizontal" ] && [ -z "$(ls -A ${PHOTOS_DIR}/horizontal/ 2>/dev/null)" ]; then
    cp "${REPO_DIR}/viewer/defaults/horizontal/"*.jpg "${PHOTOS_DIR}/horizontal/" 2>/dev/null || true
    cp "${REPO_DIR}/viewer/defaults/vertical/"*.jpg "${PHOTOS_DIR}/vertical/" 2>/dev/null || true
fi
chown -R "${FRAME_USER}:${FRAME_USER}" "${PHOTOS_DIR}"

# ---- 5. Config file ----
echo "[5/8] Config..."
CFG="${REPO_DIR}/config_frame.yaml"
if [ ! -f "$CFG" ]; then
    cat > "$CFG" << 'YAML'
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
    chown "${FRAME_USER}:${FRAME_USER}" "$CFG"
else
    # Patch existing config with new fields (keep false to preserve wizard)
    grep -q '^setup_complete:' "$CFG" || sed -i '1s/^/setup_complete: false\n/' "$CFG"
    grep -q '^energy_save:' "$CFG" || echo -e "\nenergy_save:\n  method: ddcci" >> "$CFG"
    grep -q 'local_api_base' "$CFG" && sed -i '/local_api_base/d' "$CFG"
fi

# ---- 6. User groups + permissions ----
echo "[6/8] Permissions..."
usermod -aG video,input,render,netdev,i2c "${FRAME_USER}" 2>/dev/null || true

# Polkit (NetworkManager without sudo)
cat > /etc/polkit-1/rules.d/10-photo-frame-wifi.rules << EOF
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user === "${FRAME_USER}") {
        return polkit.Result.YES;
    }
});
EOF

# Sudoers
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

# ---- 7. labwc + systemd ----
echo "[7/8] Services..."

# labwc autostart
cat > "${REPO_DIR}/labwc/autostart" << LABWC_AUTOSTART
# Apply display rotation from config
ORIENTATION=\$(python3 -c "
import yaml
try:
    with open('${REPO_DIR}/config_frame.yaml') as f:
        print(yaml.safe_load(f).get('frame',{}).get('orientation','horizontal'))
except: print('horizontal')
" 2>/dev/null)
if [ "\$ORIENTATION" = "vertical" ]; then
    wlr-randr --output HDMI-A-1 --transform 90
fi

/usr/lib/chromium/chromium \\
    --kiosk \\
    --noerrdialogs \\
    --disable-infobars \\
    --no-first-run \\
    --check-for-update-interval=31536000 \\
    --disable-session-crashed-bubble \\
    --disable-features=TranslateUI \\
    --disable-component-update \\
    --disable-pinch \\
    --enable-features=VirtualKeyboard \\
    --ozone-platform=wayland \\
    http://localhost:8080/ &
LABWC_AUTOSTART
chmod +x "${REPO_DIR}/labwc/autostart"

# Systemd units
cat > /etc/systemd/system/photo_frame_server.service << EOF
[Unit]
Description=Digital Photo Frame Server
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
Description=Digital Photo Frame Display (labwc + Chromium)
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

# Quiet boot
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    for param in "quiet" "loglevel=0" "vt.global_cursor_default=0" "logo.nologo"; do
        grep -q "$param" "$CMDLINE" || sed -i "s/$/ $param/" "$CMDLINE"
    done
fi
systemctl disable getty@tty1 2>/dev/null || true

# Enable + start
systemctl daemon-reload
systemctl enable seatd photo_frame_server photo_frame_cage photo_frame_update.service
systemctl start seatd 2>/dev/null || true
chown -R "${FRAME_USER}:${FRAME_USER}" "${REPO_DIR}"
su - "${FRAME_USER}" -c "git config --global --add safe.directory ${REPO_DIR}"

# ---- 8. Touchscreen check ----
echo "[8/8] Checking touchscreen..."
TOUCH_ID="27c0:0859"
if ! lsusb | grep -q "$TOUCH_ID"; then
    echo ""
    echo "!! No touchscreen detected !!"
    echo "Please unplug and replug the screen's USB cable."
    echo "Waiting..."
    while ! lsusb | grep -q "$TOUCH_ID"; do
        sleep 2
    done
    echo "Touchscreen found!"
    sleep 1
fi

# ---- 9. Start ----
echo "Starting frame..."
systemctl restart photo_frame_server
sleep 2
systemctl restart photo_frame_cage

echo ""
echo "=== Done! Frame is starting. ==="
echo "If no WiFi is configured, connect to the 'PhotoFrame-Setup' hotspot."
