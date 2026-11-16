#!/bin/bash
# Boots the guest, records the unmodified samples' output and the fault matrix into docs/results/, stops the guest.
#
#   scripts/evidence.sh [SECONDS]      how long each sample runs (default 10)
#
# Needs the image (image/build.sh) and the QEMU 11 build (scripts/build_qemu.sh). Guest: QEMU, TCG.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="$REPO/docs/results"
SECS="${1:-10}"
PY="${PYTHON:-$HOME/.local/bin/python3.12}"
mkdir -p "$OUT"
if [ -z "${NO_BOOT:-}" ]; then  # NO_BOOT=1: use the guest scripts/run_guest.sh start already booted
    "$HERE/run_guest.sh" start || { "$HERE/run_guest.sh" stop; sleep 2; "$HERE/run_guest.sh" start; } || exit 1
fi
"$HERE/run_guest.sh" ssh "uname -a; pidin ar | grep -E 'virtio'; ls /dev/i2c* /dev/io-spi/*/*; pci-tool -v | grep -A1 'vid/did: 1af4'" \
    >"$OUT/guest_devices.txt" 2>&1
for s in imu-bno055 temp-humidity-sht3x mcp-3008-joystick; do
    d="/tmp/ev-$s-$(date +%s)"
    "$HERE/run_guest.sh" ssh "mkdir -p $d && cd $d && ($s > out.txt 2>&1 &); sleep $SECS; slay -f $s; sleep 3; cat out.txt" |
        tr '\r' '\n' >"$OUT/sample_$s.txt" 2>&1
    echo "$s: $(wc -l <"$OUT/sample_$s.txt") lines"
done
grep -E '^(i2c|spi) ' "$(readlink -f "$HOME/Patchbay-work/run/backend.log")" | tail -n 40 >"$OUT/backend_trace_tail.txt"
"$PY" "$HERE/fault_matrix.py" --out "$OUT"
rc=$?
"$HERE/run_guest.sh" stop
exit $rc
