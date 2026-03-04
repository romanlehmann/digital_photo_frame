#!/bin/bash
# Photo Frame Bootstrap — runs once on first boot (as root).
# Copies the repo from /boot/firmware/photo_frame/ to the user's home,
# installs a first-boot service for full setup, and reboots.
#
# Invoked automatically via cloud-init runcmd, firstrun.sh, or systemd.run.

BOOT_REPO="/boot/firmware/photo_frame"
BLOG="/boot/firmware/bootstrap_log.txt"

log() {
    echo "$(date '+%H:%M:%S') $*" | tee -a "$BLOG"
}

# Don't use set -e — we want to log errors, not silently abort
trap 'log "ERROR on line $LINENO (exit $?)"' ERR

log "=== Photo Frame Bootstrap ==="

# ---- Detect target user ----
detect_user() {
    if id -u frame_user &>/dev/null; then
        echo "frame_user"
    else
        awk -F: '$3 >= 1000 && $3 < 60000 { print $1; exit }' /etc/passwd
    fi
}

FRAME_USER=$(detect_user)

# cloud-init may still be creating the user — wait up to 60s
if [ -z "$FRAME_USER" ]; then
    log "No user found yet, waiting for cloud-init..."
    for i in $(seq 1 30); do
        sleep 2
        FRAME_USER=$(detect_user)
        [ -n "$FRAME_USER" ] && break
    done
fi

if [ -z "$FRAME_USER" ]; then
    log "ERROR: No suitable user found after 60s. Aborting."
    sync
    exit 1
fi

FRAME_HOME=$(eval echo "~${FRAME_USER}")
DEST="${FRAME_HOME}/digital_photo_frame"

log "Target user: ${FRAME_USER} (${FRAME_HOME})"

# ---- Wait for home directory ----
for i in $(seq 1 30); do
    [ -d "$FRAME_HOME" ] && break
    log "Waiting for home directory... ($i/30)"
    sleep 2
done

if [ ! -d "$FRAME_HOME" ]; then
    log "ERROR: Home directory ${FRAME_HOME} not found after 60s"
    sync
    exit 1
fi

# ---- Verify boot repo exists ----
if [ ! -d "$BOOT_REPO" ]; then
    log "ERROR: Repo not found at ${BOOT_REPO}"
    sync
    exit 1
fi

# ---- Copy repo from boot partition ----
log "Copying repo to ${DEST}..."
mkdir -p "$DEST"
cp -a "${BOOT_REPO}/." "$DEST/"
chown -R "${FRAME_USER}:${FRAME_USER}" "$DEST"
log "Copy done ($(du -sh "$DEST" 2>/dev/null | cut -f1))"

# ---- Fix Windows CRLF line endings ----
log "Fixing line endings..."
find "$DEST" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.yaml' \
    -o -name '*.yml' -o -name '*.html' -o -name '*.css' -o -name '*.js' \
    -o -name '*.txt' -o -name '*.service' \) -exec sed -i 's/\r$//' {} +
chmod +x "$DEST"/scripts/*.sh

# ---- Install first-boot service ----
log "Installing first-boot service..."
cat > /etc/systemd/system/photo-frame-firstboot.service << EOF
[Unit]
Description=Photo Frame First Boot Setup
After=network.target
ConditionPathExists=!${FRAME_HOME}/.photo-frame-setup-done

[Service]
Type=oneshot
ExecStart=/bin/bash ${DEST}/scripts/setup_pi.sh
ExecStartPost=/usr/bin/touch ${FRAME_HOME}/.photo-frame-setup-done
ExecStartPost=/bin/systemctl disable photo-frame-firstboot.service
TimeoutStartSec=600
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable photo-frame-firstboot.service
log "Service installed and enabled"

# Verify service file exists on disk
sync
if [ -f /etc/systemd/system/photo-frame-firstboot.service ]; then
    log "Service file verified on disk"
else
    log "WARNING: Service file not found after write!"
fi

# ---- Clean up boot partition ----
log "Cleaning boot partition..."
rm -rf "$BOOT_REPO"
rm -f /boot/firmware/photo_frame_bootstrap.sh

log "=== Bootstrap complete — rebooting ==="
# Flush ALL filesystem buffers before reboot
sync
sync
sleep 3
reboot
