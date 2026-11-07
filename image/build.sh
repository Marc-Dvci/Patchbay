#!/bin/bash
# Builds Patchbay's QNX 8.0 guest image for QEMU x86_64 with mkqnximage.
#
#   image/build.sh [IMAGE_DIR]          default $PATCHBAY_WORK/image
#
# Run scripts/build_drivers.sh first. The image holds pci-server, sshd, both drivers, their configuration, and the
# upstream sample programs. The ssh key is made once in .guest/ssh and stays out of the repository.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${PATCHBAY_WORK:-$HOME/Patchbay-work}"
ARCH=x86_64
IMAGE_DIR="${1:-$WORK/image}"
# shellcheck disable=SC1091
source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" >/dev/null
KEY="$REPO/.guest/ssh/id_ed25519"
mkdir -p "$(dirname "$KEY")"
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C patchbay-guest -f "$KEY"
for f in "$REPO/driver/build/$ARCH/i2c-virtio" "$REPO/driver/build/$ARCH/spi-virtio" "$WORK/samples/$ARCH/imu-bno055"; do
    [ -f "$f" ] || { echo "missing $f: run scripts/build_drivers.sh" >&2; exit 1; }
done
mkdir -p "$IMAGE_DIR/local/snippets"
for f in "$HERE"/snippets/*.custom; do  # each generated snippet is overwritten in place
    sed -e "s#__PB_DRIVERS__#$REPO/driver/build/$ARCH#g" -e "s#__PB_SAMPLES__#$WORK/samples/$ARCH#g" \
        -e "s#__PB_IMAGE__#$HERE#g" "$f" >"$IMAGE_DIR/local/snippets/$(basename "$f")"
done
cd "$IMAGE_DIR"
mkqnximage --noprompt --force --build --type=qemu --arch=$ARCH --graphics=no --hostname=patchbay --cpu=2 \
    --ram=1G --sys-size=32 --data-size=16 --ssh-ident="$KEY.pub" >"$IMAGE_DIR/mkqnximage.log" 2>&1 \
    || { tail -30 "$IMAGE_DIR/mkqnximage.log"; exit 1; }
ls -la output/ifs.bin output/disk-qemu.vmdk
