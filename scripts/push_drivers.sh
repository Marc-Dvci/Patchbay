#!/bin/bash
# Development loop: rebuild the drivers and copy them into the running guest's /tmp, without a new image.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck disable=SC1091
source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" >/dev/null
make -s -C "$REPO/driver" ARCH=x86_64 -j4
KEY="${PATCHBAY_WORK:-$HOME/Patchbay-work}/run/guest_key"
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
# a running binary cannot be overwritten: stop the drivers first
ssh "${SSH_OPTS[@]}" -p 2252 root@127.0.0.1 "slay -f i2c-virtio spi-virtio 2>/dev/null; sleep 1" || true
scp -q "${SSH_OPTS[@]}" -P 2252 \
    "$REPO/driver/build/x86_64/i2c-virtio" "$REPO/driver/build/x86_64/spi-virtio" root@127.0.0.1:/tmp/
echo "copied to the guest's /tmp"
