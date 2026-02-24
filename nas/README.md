# NAS Setup Guide (Synology DSM 7)

This script runs on your Synology NAS. It selects 200 random photos from your library each week, generates horizontal and vertical versions with blur-filled backgrounds, and writes them to a shared folder that your Pi frames sync from.

## 1. Enable SSH on the NAS

1. Open **DSM > Control Panel > Terminal & SNMP**
2. Check **Enable SSH service**
3. Click **Apply**

## 2. Install Python 3

SSH into your NAS:

```bash
ssh your_admin_user@nas.local
```

Check if Python 3 is available:

```bash
python3 --version
```

If not installed, install it via **Package Center > Python 3.x** in DSM, or use:

```bash
sudo synopkg install Python3
```

## 3. Copy the script to the NAS

From your computer (not the NAS):

```bash
# Create directory on NAS
ssh your_admin_user@nas.local "mkdir -p /volume1/docker/frame/scripts"

# Copy files
scp nas/prepare_photos.py nas/config.yaml nas/requirements.txt \
    your_admin_user@nas.local:/volume1/docker/frame/scripts/
```

Or clone the repo directly on the NAS:

```bash
ssh your_admin_user@nas.local
cd /volume1/docker/frame
git clone https://github.com/rwkaspar/digital_photo_frame.git
```

## 4. Create virtual environment and install dependencies

```bash
ssh your_admin_user@nas.local
cd /volume1/docker/frame/scripts

# Create venv
python3 -m venv venv

# Activate and install dependencies
source venv/bin/activate
pip install -r requirements.txt

# Verify
python -c "from PIL import Image; import yaml; print('OK')"
deactivate
```

## 5. Configure

Edit the config file on the NAS:

```bash
nano /volume1/docker/frame/scripts/config.yaml
```

Key settings to adjust:

```yaml
source:
  # Point to your Synology Photos directory:
  #   Shared space:   /volume1/photo
  #   Personal space: /volume1/homes/YOUR_USERNAME/Photos
  photos_dir: "/volume1/photo"
  recursive: true

selection:
  photos_per_week: 200
  max_show_count: 10
  state_db: "/volume1/docker/frame/state.db"

output:
  # This folder will be synced to your Pi frames
  dir: "/volume1/frame_photos"
  horizontal:
    width: 1920
    height: 1200
  vertical:
    width: 1200
    height: 1920
  quality: 85
  blur_radius: 40
  blur_darken: 0.6
```

Create the output directory:

```bash
mkdir -p /volume1/frame_photos
```

## 6. Test run

```bash
/volume1/docker/frame/scripts/venv/bin/python \
    /volume1/docker/frame/scripts/prepare_photos.py \
    /volume1/docker/frame/scripts/config.yaml
```

This will take a few minutes (processing 200 photos). When done, verify:

```bash
ls /volume1/frame_photos/horizontal/ | wc -l   # should show 200
ls /volume1/frame_photos/vertical/ | wc -l     # should show 200
```

## 7. Set up weekly schedule

1. Open **DSM > Control Panel > Task Scheduler**
2. Click **Create > Scheduled Task > User-defined script**
3. **General** tab:
   - Task: `Prepare Photo Frame Photos`
   - User: `root`
4. **Schedule** tab:
   - Run on: `Monday`
   - First run time: `03:00`
   - Frequency: `Every week`
5. **Task Settings** tab:
   - User-defined script:
     ```
     /volume1/docker/frame/scripts/venv/bin/python /volume1/docker/frame/scripts/prepare_photos.py /volume1/docker/frame/scripts/config.yaml
     ```
   - (Optional) Send run details by email: check to receive error notifications

Click **OK** to save.

## 8. Set up rsync access for Pi frames

Each Pi will use rsync over SSH to pull photos. Create a dedicated user or use an existing one:

```bash
# On the NAS, ensure the sync user can read the output folder
chmod -R 755 /volume1/frame_photos
```

On **each Pi**, set up passwordless SSH:

```bash
ssh-keygen -t ed25519       # accept defaults, no passphrase
ssh-copy-id pi@nas.local    # enter password once
ssh pi@nas.local ls /volume1/frame_photos/   # should work without password
```

## Folder structure on the NAS

After setup:

```
/volume1/
├── photo/                        # Synology Photos library (source)
│   ├── 2024/
│   ├── 2025/
│   └── ...
├── docker/frame/
│   ├── scripts/
│   │   ├── prepare_photos.py     # The preparation script
│   │   ├── config.yaml           # Configuration
│   │   ├── requirements.txt      # Python dependencies
│   │   └── venv/                 # Python virtual environment
│   └── state.db                  # SQLite tracking database
└── frame_photos/                 # Output (synced to Pi frames)
    ├── horizontal/               # 1920x1200 versions
    │   ├── 0000_IMG_1234.jpg
    │   ├── 0001_IMG_5678.jpg
    │   └── ...
    └── vertical/                 # 1200x1920 versions
        ├── 0000_IMG_1234.jpg
        ├── 0001_IMG_5678.jpg
        └── ...
```

## Troubleshooting

### "No photos found"

Check that `photos_dir` in config.yaml points to the right directory:

```bash
# List what Synology Photos directories exist
ls /volume1/photo/
ls /volume1/homes/*/Photos/
```

### "Permission denied"

Run the script as root or fix permissions:

```bash
sudo /volume1/docker/frame/scripts/venv/bin/python \
     /volume1/docker/frame/scripts/prepare_photos.py \
     /volume1/docker/frame/scripts/config.yaml
```

### "No module named PIL"

The venv is missing dependencies. Reinstall:

```bash
cd /volume1/docker/frame/scripts
source venv/bin/activate
pip install -r requirements.txt
deactivate
```

### Check logs

```bash
tail -50 /var/log/frame_prepare.log
```

### Check selection state

```bash
sqlite3 /volume1/docker/frame/state.db "SELECT COUNT(*) FROM photos;"
sqlite3 /volume1/docker/frame/state.db "SELECT COUNT(*) FROM photos WHERE times_selected > 0;"
sqlite3 /volume1/docker/frame/state.db "SELECT * FROM runs ORDER BY id DESC LIMIT 5;"
```
