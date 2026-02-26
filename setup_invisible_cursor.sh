#!/bin/bash
# Create an invisible cursor theme for kiosk use.
# Run once on the Pi to set up the theme.

THEME_DIR="/usr/share/icons/InvisibleCursor/cursors"
sudo mkdir -p "$THEME_DIR"

# Create a 1x1 transparent Xcursor file using raw bytes (no dependencies)
python3 -c "
import struct, sys
buf = bytearray()
# Xcursor file header
buf += b'Xcur'                          # magic
buf += struct.pack('<I', 16)            # header size (4 fields x 4 bytes)
buf += struct.pack('<I', 1)             # version
buf += struct.pack('<I', 1)             # number of TOC entries
# TOC entry
buf += struct.pack('<I', 0xFFFD0002)    # type = image
buf += struct.pack('<I', 1)             # subtype (nominal size)
buf += struct.pack('<I', 28)            # position
# Image chunk
buf += struct.pack('<I', 36)            # chunk header size
buf += struct.pack('<I', 0xFFFD0002)    # type
buf += struct.pack('<I', 1)             # subtype
buf += struct.pack('<I', 1)             # version
buf += struct.pack('<I', 1)             # width
buf += struct.pack('<I', 1)             # height
buf += struct.pack('<I', 0)             # xhot
buf += struct.pack('<I', 0)             # yhot
buf += struct.pack('<I', 1)             # delay
buf += struct.pack('<I', 0)             # 1 pixel ARGB = transparent
sys.stdout.buffer.write(buf)
" > /tmp/invisible_cursor

# Install cursor for all standard cursor names
for name in default left_ptr arrow top_left_arrow pointer hand2 watch text \
    crosshair move not-allowed grab grabbing; do
    sudo cp /tmp/invisible_cursor "$THEME_DIR/$name"
done

# Create theme index
sudo tee /usr/share/icons/InvisibleCursor/index.theme > /dev/null << 'THEME'
[Icon Theme]
Name=InvisibleCursor
Comment=Transparent cursor for kiosk use
THEME

rm /tmp/invisible_cursor
echo "Invisible cursor theme installed."
