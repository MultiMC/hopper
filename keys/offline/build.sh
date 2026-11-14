#!/bin/bash
# Build hopper's offline image: a live Arch Linux ISO for the updater's key ceremony (hopper's
# README, "Production's keys: the ceremony" and "The offline image"). It is also the kept
# working copy of the tools the ceremony depends on: built once, kept with its package list,
# and booted for every ceremony and renewal, so a system update elsewhere can't change them.
#
#     sudo keys/offline/build.sh <output dir>
#
# Needs archiso (mkarchiso, and its baseline profile in /usr/share/archiso/configs/baseline)
# and root (mkarchiso installs packages into a chroot). The image is archiso's baseline plus:
#
# - packages.x86_64 here: openssl, tinyxxd, age, python, yubikey-manager, pcsclite, ccid;
# - packages from the Arch Linux Archive as of the date in archive-date, not from the
#   mirrors, so a rebuild gets exactly the same packages;
# - no networking: nothing that configures it is installed, and systemd's network services
#   are masked;
# - pcscd started at boot, for the YubiKeys;
# - this hopper checkout's HEAD in /root/hopper (git archive: committed files only), its
#   commit in /root/hopper/COMMIT;
# - airootfs/ here (the login message).
#
# The output dir gets the ISO, the profile it was built from (to inspect), and the work dir is
# removed afterwards.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: sudo $0 <output dir>" >&2
    exit 2
fi
if [[ $EUID -ne 0 ]]; then
    echo "$0: mkarchiso needs root" >&2
    exit 1
fi
baseline=/usr/share/archiso/configs/baseline
if ! command -v mkarchiso >/dev/null || [[ ! -d $baseline ]]; then
    echo "$0: needs archiso (mkarchiso and $baseline)" >&2
    exit 1
fi

here=$(cd "$(dirname "$0")" && pwd)
hopper=$(cd "$here/../.." && pwd)
out=$(realpath -m "$1")
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
profile=$out/profile
rm -rf "$profile"
cp -a "$baseline" "$profile"

# The packages: baseline's and ours
grep -v '^#' "$here/packages.x86_64" | grep -v '^$' >> "$profile/packages.x86_64"

# From the Arch Linux Archive on one date, for both the image and the build
date=$(tr -d '[:space:]' < "$here/archive-date")
sed -i '/^Include = \/etc\/pacman.d\/mirrorlist/d' "$profile/pacman.conf"
sed -i "/^\[core\]\|^\[extra\]/a Server = https://archive.archlinux.org/repos/$date/\$repo/os/\$arch" "$profile/pacman.conf"
if [[ $(grep -c '^Server = https://archive.archlinux.org/' "$profile/pacman.conf") -ne 2 ]] \
        || grep -q '^Include = /etc/pacman.d/mirrorlist' "$profile/pacman.conf"; then
    echo "$0: baseline's pacman.conf isn't laid out as expected ([core] and [extra] from the mirrorlist)" >&2
    exit 1
fi

# The image's name, and the permissions of what we add
cat >> "$profile/profiledef.sh" <<EOF

# hopper's offline image (keys/offline/build.sh)
iso_name="hopper-offline"
iso_label="HOPPER_OFFLINE_${date//\//}"
iso_publisher="hopper"
iso_application="hopper's offline image for the updater's key ceremony"
iso_version="${date//\//-}"
file_permissions+=(["/root/hopper"]="0:0:700")
EOF

# The login message
cp -a "$here/airootfs/." "$profile/airootfs/"

# No networking: mask what could bring it up; pcscd at boot
systemd=$profile/airootfs/etc/systemd/system
mkdir -p "$systemd/sockets.target.wants"
for unit in systemd-networkd.service systemd-networkd.socket systemd-resolved.service \
            systemd-networkd-wait-online.service NetworkManager.service iwd.service \
            wpa_supplicant.service dhcpcd.service; do
    ln -sf /dev/null "$systemd/$unit"
done
ln -sf /usr/lib/systemd/system/pcscd.socket "$systemd/sockets.target.wants/pcscd.socket"

# This checkout's HEAD
mkdir -p "$profile/airootfs/root/hopper"
git -C "$hopper" archive HEAD | tar -x -C "$profile/airootfs/root/hopper"
git -C "$hopper" rev-parse HEAD > "$profile/airootfs/root/hopper/COMMIT"

mkarchiso -v -w "$work" -o "$out" "$profile"
echo "built $(ls "$out"/*.iso); hopper $(cat "$profile/airootfs/root/hopper/COMMIT")"
