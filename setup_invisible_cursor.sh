#!/bin/bash
# Create an invisible cursor theme for kiosk use.
# Run once on the Pi to set up the theme.

THEME_DIR="/usr/share/icons/InvisibleCursor/cursors"
sudo mkdir -p "$THEME_DIR"

# Create a 1x1 fully transparent cursor using Python + Pillow
python3 -c "
from PIL import Image
import struct, io

# Create 1x1 transparent image
img = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
pixels = img.tobytes()

# Write X11 cursor format
buf = io.BytesIO()
# Xcursor file header
buf.write(b'Xcur')           # magic
buf.write(struct.pack('<I', 4 * 4))  # header size
buf.write(struct.pack('<I', 1))      # version
buf.write(struct.pack('<I', 1))      # number of TOC entries
# TOC entry
buf.write(struct.pack('<I', 0xFFFD0002))  # type = image
buf.write(struct.pack('<I', 1))      # subtype (nominal size)
buf.write(struct.pack('<I', 36))     # position (after header + toc)
# Image chunk
buf.write(struct.pack('<I', 36))     # chunk header size
buf.write(struct.pack('<I', 0xFFFD0002))  # type
buf.write(struct.pack('<I', 1))      # subtype (nominal size)
buf.write(struct.pack('<I', 1))      # version
buf.write(struct.pack('<I', 1))      # width
buf.write(struct.pack('<I', 1))      # height
buf.write(struct.pack('<I', 0))      # xhot
buf.write(struct.pack('<I', 0))      # yhot
buf.write(struct.pack('<I', 1))      # delay
buf.write(struct.pack('<BBBB', 0, 0, 0, 0))  # 1 pixel, ARGB transparent

with open('/tmp/invisible_cursor', 'wb') as f:
    f.write(buf.getvalue())
"

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

echo "Invisible cursor theme installed."
echo "Set XCURSOR_THEME=InvisibleCursor to use it."
