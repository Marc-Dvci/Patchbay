#!/bin/bash
# Builds i2c-virtio and spi-virtio with QNX SDP 8.0, and the upstream sample programs Patchbay runs unmodified.
#
#   scripts/build_drivers.sh [ARCH]      ARCH is x86_64 (default) or aarch64le
#
# QNX_SDP (default ~/qnx800) holds qnxsdp-env.sh. Outputs go to driver/build/ARCH and $PATCHBAY_WORK/samples/ARCH.
set -euo pipefail
ARCH="${1:-x86_64}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck disable=SC1091
source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" >/dev/null
make -C "$REPO/driver" ARCH="$ARCH" -j"${JOBS:-4}"
"$HERE/build_samples.sh" "$ARCH"
