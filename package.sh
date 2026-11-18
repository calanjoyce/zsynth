#!/bin/sh
# Build the ZSynth .deb for the CardputerZero AppStore.
# Usage: ./package.sh user@host   (a Cardputer Zero: the engine is compiled
#                                  there, for arm64)
# Writes dist/zsynth_<version>_arm64.deb. Name, version, summary, author and
# license come from app-builder.json.
set -e
HOST=${1:?usage: ./package.sh user@host}
cd "$(dirname "$0")"

field() { python3 -c 'import json,sys
d = json.load(open("app-builder.json"))
for k in sys.argv[1].split("."):
    d = d[k]
print(d)' "$1"; }
PKG=$(field package_name)
VER=$(field version)
SUMMARY=$(field store.summary)
AUTHOR=$(field store.author.display_name)
GH=$(field store.author.github)
LICENSE=$(field store.license)
REPO=$(field store.source_repo)
case "$AUTHOR$LICENSE" in *TODO*)
    echo "app-builder.json: fill in store.license and store.author.display_name first" >&2
    exit 1;;
esac
GHID=$(gh api "users/$GH" --jq .id)
MAINT="$AUTHOR <$GHID+$GH@users.noreply.github.com>"
DEB=${PKG}_${VER}_arm64.deb

B=zsynth-pkg
ssh "$HOST" "rm -rf ~/$B && mkdir -p ~/$B"
scp -q engine.c zsynth.py make_icon.py zsynth.desktop LICENSE "$HOST":$B/
scp -qr samples songs "$HOST":$B/
ssh "$HOST" "PKG='$PKG' VER='$VER' SUMMARY='$SUMMARY' MAINT='$MAINT' REPO='$REPO' DEB='$DEB' sh -s" <<'EOF'
set -e
cd ~/zsynth-pkg
dpkg -s libpulse-dev >/dev/null 2>&1 || sudo apt-get install -y --no-install-recommends libpulse-dev
gcc -O2 -Wall -o zsynth-engine engine.c -lpulse-simple -lpulse -lm -lpthread
R=root
install -d $R/DEBIAN $R/opt/zsynth $R/usr/share/APPLaunch/applications \
    $R/usr/share/APPLaunch/share/images $R/usr/share/doc/$PKG
install -m 755 zsynth.py zsynth-engine $R/opt/zsynth/
cp -r samples songs $R/opt/zsynth/
chmod -R u=rwX,go=rX $R/opt/zsynth/samples $R/opt/zsynth/songs
install -m 644 zsynth.desktop $R/usr/share/APPLaunch/applications/zsynth.desktop
python3 make_icon.py $R/usr/share/APPLaunch/share/images/zsynth.png 90
chmod 644 $R/usr/share/APPLaunch/share/images/zsynth.png
{
    echo "ZSynth: $REPO"
    echo
    cat LICENSE
    echo
    echo "Starter samples (/opt/zsynth/samples): CC0 1.0, from the Versilian"
    echo "Community Sample Library, https://github.com/sgossner/VCSL."
    echo "See /opt/zsynth/samples/CREDITS.md."
} > $R/usr/share/doc/$PKG/copyright
chmod 644 $R/usr/share/doc/$PKG/copyright
SIZE=$(du -sk --exclude=DEBIAN $R | cut -f1)
cat > $R/DEBIAN/control <<CTL
Package: $PKG
Version: $VER
Architecture: arm64
Maintainer: $MAINT
Installed-Size: $SIZE
Depends: libc6 (>= 2.34), libpulse0, python3 (>= 3.11), python3-pygame, python3-numpy, fonts-dejavu-core, fonts-dejavu-extra
Recommends: pulseaudio-utils
Section: sound
Priority: optional
Homepage: $REPO
Description: $SUMMARY
 A pocket groovebox for the Cardputer Zero: lead and bass synths, a mic
 sampler and a drum kit, eight patterns per song (1-32 steps, any time
 signature), a song chain, and libraries of songs, samples and instruments.
 Includes 18 CC0 starter samples and a demo song.
CTL
dpkg-deb --root-owner-group -Zxz --build $R "$DEB" >/dev/null
dpkg-deb --info "$DEB" | sed -n '/Package:/,$p'
EOF
mkdir -p dist
scp -q "$HOST:$B/$DEB" dist/
ssh "$HOST" "rm -rf ~/$B"
ls -l "dist/$DEB"
