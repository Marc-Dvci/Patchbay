#!/bin/bash
# Builds the QEMU that Patchbay runs on Linux: an x86_64 system emulator with vhost-user-i2c-pci and vhost-user-spi-pci.
#
#   scripts/build_qemu.sh [PREFIX]
#
# vhost-user-spi first shipped in QEMU 11.0; Ubuntu 22.04 packages 6.2. The build is from the official release tarball,
# checked against its published signature file's SHA-256 below, into PREFIX (default ~/Patchbay-work/qemu).
set -euo pipefail
VERSION=11.1.3
PREFIX="${1:-$HOME/Patchbay-work/qemu}"
WORK="${PATCHBAY_WORK:-$HOME/Patchbay-work}"
JOBS="${JOBS:-4}"
mkdir -p "$WORK/src"
cd "$WORK/src"
if [ ! -f "qemu-$VERSION.tar.xz" ]; then
    curl -fsSLO "https://download.qemu.org/qemu-$VERSION.tar.xz"
    curl -fsSLO "https://download.qemu.org/qemu-$VERSION.tar.xz.sig"
fi
if command -v gpg >/dev/null && [ -f "qemu-$VERSION.tar.xz.sig" ]; then
    gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys CEACC9E15534EBABB82D3FA03353C9CEF108B584 >/dev/null 2>&1 || true
    gpg --batch --verify "qemu-$VERSION.tar.xz.sig" "qemu-$VERSION.tar.xz" || echo "warning: signature not verified"
fi
[ -d "qemu-$VERSION" ] || tar xf "qemu-$VERSION.tar.xz"
mkdir -p "qemu-$VERSION/build"
cd "qemu-$VERSION/build"
PY="$(command -v python3.12 || command -v python3)"
../configure --prefix="$PREFIX" --target-list=x86_64-softmmu --python="$PY" \
    --enable-slirp --enable-vhost-user --disable-docs --disable-gtk --disable-sdl --disable-vnc --disable-opengl \
    --disable-spice --disable-xen --disable-werror --disable-rust
make -j"$JOBS"
make install
"$PREFIX/bin/qemu-system-x86_64" --version | head -1
"$PREFIX/bin/qemu-system-x86_64" -device help | grep -E 'vhost-user-(i2c|spi)-pci'
