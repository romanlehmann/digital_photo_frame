#!/bin/bash
# Start Chromium in kiosk mode with minimal X11 (no desktop environment).
# Used by photo_frame_viewer.service.

set -euo pipefail

# Disable screen blanking and power management
xset s off
xset s noblank
xset -dpms

# Hide cursor after 3 seconds of inactivity
unclutter -idle 3 -root &

# Wait for the viewer server to be ready
for i in $(seq 1 30); do
    curl -s http://localhost:8080/ > /dev/null 2>&1 && break
    sleep 1
done

# Launch Chromium in kiosk mode (use binary directly to skip RAM warning)
exec /usr/lib/chromium/chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --check-for-update-interval=31536000 \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --disable-component-update \
    --disable-pinch \
    http://localhost:8080/
