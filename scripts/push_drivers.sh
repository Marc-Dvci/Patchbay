#!/bin/bash
# Development loop: rebuild the drivers and copy them into the running guest's /tmp, without a new image.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck disable=SC1091
source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" >/dev/null
make -s -C "$REPO/driver" ARCH=x86_64 -j4
KEY="${PATCHBAY_WORK:-$HOME/Patchbay-work}/run/guest_key"
scp -q -i "$KEY" -P 2252 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    "$REPO/driver/build/x86_64/i2c-virtio" "$REPO/driver/build/x86_64/spi-virtio" root@127.0.0.1:/tmp/
echo "copied to the guest's /tmp"
