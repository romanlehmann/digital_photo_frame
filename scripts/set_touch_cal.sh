#!/bin/bash
# Set touchscreen calibration matrix based on orientation config.
# Must run as root BEFORE labwc starts (ExecStartPre=+).

CONFIG=/home/robert/digital_photo_frame/config_frame.yaml
RULES_FILE=/etc/udev/rules.d/99-touchscreen-cal.rules

ORIENTATION=$(python3 -c "
import yaml
try:
    with open('$CONFIG') as f:
        print(yaml.safe_load(f).get('frame',{}).get('orientation','horizontal'))
except: print('horizontal')
" 2>/dev/null)

if [ "$ORIENTATION" = "vertical" ]; then
    # Calibration matrix for 90° display rotation
    echo 'ENV{ID_INPUT_TOUCHSCREEN}=="1", ENV{LIBINPUT_CALIBRATION_MATRIX}="0 -1 1 1 0 0"' > "$RULES_FILE"
else
    # Normal — remove any custom calibration
    rm -f "$RULES_FILE"
fi

udevadm control --reload-rules
udevadm trigger
