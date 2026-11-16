#!/bin/bash
# From a Linux machine (or WSL) with QNX SDP 8.0 installed to QNX's own sensor samples running on simulated chips.
#
#   scripts/demo.sh [imu|climate|joystick|all] [SECONDS]
#
# Builds what is missing (QEMU 11 for vhost-user-spi, the two drivers, the upstream samples, the guest image), boots
# the guest under software emulation, starts the back end, runs the sample(s) for SECONDS (default 10) and prints their
# output. The guest keeps running afterwards; stop it with scripts/run_guest.sh stop.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${PATCHBAY_WORK:-$HOME/Patchbay-work}"
WHICH="${1:-all}"
SECS="${2:-10}"
say() { printf '\n== %s\n' "$*"; }

say "Checking prerequisites"
SDP="${QNX_SDP:-$HOME/qnx800}"
[ -f "$SDP/qnxsdp-env.sh" ] || { echo "QNX SDP 8.0 not found at $SDP (set QNX_SDP)" >&2; exit 1; }
for tool in make cc ssh scp git; do command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }; done
echo "QNX SDP: $SDP"

if [ ! -x "$WORK/qemu/bin/qemu-system-x86_64" ]; then
    say "Building QEMU 11.1.3 (once; about 10 minutes with 4 jobs)"
    "$HERE/build_qemu.sh" "$WORK/qemu"
fi

say "Building i2c-virtio, spi-virtio, pbprobe and the upstream samples"
"$HERE/build_drivers.sh" x86_64 | grep -E '^built|error' || true

say "Building the guest image"
"$HERE/../image/build.sh" | tail -2

say "Starting the back end and booting the guest (QEMU, TCG)"
"$HERE/run_guest.sh" stop >/dev/null 2>&1 || true
"$HERE/run_guest.sh" start

run_sample() {  # name, binary
    local dir; dir="/tmp/demo-$(date +%s)"
    say "$1: $2 for $SECS s, unmodified upstream binary, in $dir"
    "$HERE/run_guest.sh" ssh "mkdir -p $dir && cd $dir && ($2 > out.txt 2>&1 &) ; sleep $SECS; slay -f $2; cat out.txt" |
        tr '\r' '\n' | grep -v '^$' | tail -n 24
}
case "$WHICH" in
    imu) run_sample "BNO055 IMU on /dev/i2c1" imu-bno055 ;;
    climate) run_sample "SHT3x on /dev/i2c1" temp-humidity-sht3x ;;
    joystick) run_sample "MCP3008 on /dev/io-spi/spi0/dev0" mcp-3008-joystick ;;
    all)
        run_sample "BNO055 IMU on /dev/i2c1" imu-bno055
        run_sample "SHT3x on /dev/i2c1" temp-humidity-sht3x
        run_sample "MCP3008 on /dev/io-spi/spi0/dev0" mcp-3008-joystick ;;
    *) echo "usage: $0 [imu|climate|joystick|all] [SECONDS]" >&2; exit 64 ;;
esac

say "The bus as the back end saw it (last lines of its trace)"
tail -n 6 "$WORK/run/backend.log"
cat <<EOF

Next: inject a fault while a sample runs, for example
  scripts/run_guest.sh ctl fault nak --bus i2c --target 0x28 --count 3
  scripts/run_guest.sh ctl fault stuck --bus i2c ; sleep 3 ; scripts/run_guest.sh ctl clear
or run the whole fault matrix: python3 scripts/fault_matrix.py
Stop the guest: scripts/run_guest.sh stop
EOF
