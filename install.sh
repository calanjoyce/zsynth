#!/bin/sh
# Build ZSynth on the Cardputer Zero and add it to the launcher.
# Usage: ./install.sh user@host   (the Cardputer's SSH login)
set -e
HOST=${1:?usage: ./install.sh user@host}
cd "$(dirname "$0")"
ssh "$HOST" 'mkdir -p ~/zsynth'
scp -q engine.c zsynth.py make_icon.py zsynth.desktop "$HOST":zsynth/
scp -qr samples songs "$HOST":zsynth/
ssh "$HOST" '
set -e
cd ~/zsynth
dpkg -s libpulse-dev >/dev/null 2>&1 || sudo apt-get install -y --no-install-recommends libpulse-dev
gcc -O2 -Wall -o zsynth-engine engine.c -lpulse-simple -lpulse -lm -lpthread
sudo install -d /opt/zsynth
sudo install -m 755 zsynth.py zsynth-engine /opt/zsynth/
python3 make_icon.py /tmp/zsynth.png
sudo install -m 644 /tmp/zsynth.png /usr/share/APPLaunch/share/images/zsynth.png
sudo install -m 644 zsynth.desktop /usr/share/APPLaunch/applications/zsynth.desktop
mkdir -p ~/.local/share/zsynth/samples
# starter samples: add each one once (so one you delete stays deleted)
LIB=~/.local/share/zsynth/samples
for f in samples/*.wav; do
    n=$(basename "$f")
    grep -qxF "$n" $LIB/.starter 2>/dev/null && continue
    [ -e "$LIB/$n" ] || cp "$f" "$LIB/$n"
    echo "$n" >> $LIB/.starter
done
cp samples/CREDITS.md $LIB/.starter-credits.md
# demo songs, also added once
SL=~/.local/share/zsynth/songs
mkdir -p $SL
for f in songs/*.zsong; do
    n=$(basename "$f")
    grep -qxF "$n" $SL/.starter 2>/dev/null && continue
    [ -e "$SL/$n" ] || cp "$f" "$SL/$n"
    echo "$n" >> $SL/.starter
done
if pgrep -f "^[^ ]*python3 /opt/zsynth/zsynth.py" >/dev/null; then
    echo "installed (ZSynth is open: reopen it to get the new version)"
else
    systemctl --user restart APPLaunch.service
    echo installed
fi
'
