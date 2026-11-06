#!/bin/bash
# Development loop: stop this guest, rebuild drivers, samples and image, boot again.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/run_guest.sh" stop || true
"$HERE/build_drivers.sh" x86_64 2>&1 | grep -v -E "find: .build.|^make" || true
[ -x "$HERE/../driver/build/x86_64/i2c-virtio" ] || exit 1
"$HERE/../image/build.sh"
"$HERE/run_guest.sh" start
