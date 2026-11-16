#!/bin/bash
# Builds QNX's own sample programs, unmodified, from their upstream repository:
#   https://github.com/qnx/Raspberry-Pi-Hardware-Component-Samples (Apache-2.0), at the commit pinned below.
# Nothing from it is copied into this repository; the source is fetched into $PATCHBAY_WORK and built with its own
# Makefiles. The script checks that the working tree is clean, so what runs is exactly the upstream code.
#
#   scripts/build_samples.sh [ARCH]
set -euo pipefail
ARCH="${1:-x86_64}"
UPSTREAM=https://github.com/qnx/Raspberry-Pi-Hardware-Component-Samples.git
COMMIT=fb1f9dfeeee15b5e1fbc0b986a141633ebe44775
WORK="${PATCHBAY_WORK:-$HOME/Patchbay-work}"
SRC="$WORK/rpi-samples"
OUT="$WORK/samples/$ARCH"
# shellcheck disable=SC1091
[ -n "${QNX_HOST:-}" ] || source "${QNX_SDP:-$HOME/qnx800}/qnxsdp-env.sh" >/dev/null
[ -d "$SRC/.git" ] || git clone -q "$UPSTREAM" "$SRC"
git -C "$SRC" fetch -q origin "$COMMIT" 2>/dev/null || true
git -C "$SRC" checkout -q "$COMMIT"
if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
    echo "upstream samples have local changes; refusing to build them as 'unmodified'" >&2
    exit 1
fi
mkdir -p "$OUT"
# The helper libraries first, with no extra flags: the samples' sub-makes then find them up to date.
for lib in rpi_i2c rpi_spi rpi_gpio; do
    make -s -C "$SRC/common/$lib" PLATFORM="$ARCH" BUILD_PROFILE=release all >/dev/null
done
build() {  # dir artifact
    # "all" by name: in some of these Makefiles the first rule is a library, which would be the default goal.
    # LDFLAGS adds the helper libraries' directories for this ARCH: two Makefiles name aarch64le-debug ones.
    local libs="-L../../common/rpi_i2c/build/$ARCH-release -L../../common/rpi_spi/build/$ARCH-release"
    libs="$libs -L../../common/rpi_gpio/build/$ARCH-release"
    make -s -C "$SRC/$1" PLATFORM="$ARCH" BUILD_PROFILE=release LDFLAGS="$libs" all >/dev/null
    cp "$SRC/$1/build/$ARCH-release/$2" "$OUT/$2"
    echo "built $OUT/$2 from $1 at ${COMMIT:0:7}"
}
build adafruit-imu-bno055/c imu-bno055
build temp-humidity-sht3x/c temp-humidity-sht3x
build mcp-3008-joystick/c mcp-3008-joystick
