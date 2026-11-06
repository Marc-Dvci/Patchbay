#!/bin/bash
# Starts the Patchbay back end and boots the QNX guest in QEMU on Linux, headless, under software emulation (TCG).
#
#   scripts/run_guest.sh start      back end, then QEMU; returns once the guest answers on ssh
#   scripts/run_guest.sh stop       stops this guest and this back end, by their pidfiles
#   scripts/run_guest.sh ssh CMD    runs CMD in the guest
#   scripts/run_guest.sh ctl ARGS   python -m patchbay ctl ARGS, against this back end
#
# vhost-user needs the guest's RAM in shared memory (memory-backend-memfd, share=on) and a Unix socket per device,
# which is why the guest runs in QEMU on Linux. Ports: ssh 127.0.0.1:2252, serial console 127.0.0.1:5530.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${PATCHBAY_WORK:-$HOME/Patchbay-work}"
RUN="$WORK/run"
IMAGE="${PATCHBAY_IMAGE:-$WORK/image/output}"
SSH_PORT=2252
SERIAL_PORT=5530
PY="${PYTHON:-$(command -v python3.12 || command -v python3)}"
if [ -z "${QEMU:-}" ]; then
    if [ -x "$WORK/qemu/bin/qemu-system-x86_64" ]; then QEMU="$WORK/qemu/bin/qemu-system-x86_64"; else QEMU=qemu-system-x86_64; fi
fi
mkdir -p "$RUN"
KEY="$RUN/guest_key"

ssh_guest() {
    ssh -i "$KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -o ConnectTimeout=5 -o BatchMode=yes root@127.0.0.1 "$@"
}

ctl() {
    PYTHONPATH="$REPO" "$PY" -m patchbay ctl --control "$RUN/control.sock" "$@"
}

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_backend() {
    if alive "$RUN/backend.pid"; then return; fi
    rm -f "$RUN/i2c.sock" "$RUN/spi.sock" "$RUN/control.sock"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    BACKEND_LOG="$RUN/backend-$stamp.log"
    PYTHONPATH="$REPO" nohup "$PY" -m patchbay serve --i2c "$RUN/i2c.sock" --spi "$RUN/spi.sock" \
        --control "$RUN/control.sock" --trace >"$BACKEND_LOG" 2>&1 &
    echo $! >"$RUN/backend.pid"
    ln -sf "$BACKEND_LOG" "$RUN/backend.log"
    for _ in $(seq 50); do [ -S "$RUN/control.sock" ] && return; sleep 0.1; done
    echo "back end did not start; see $BACKEND_LOG" >&2
    exit 1
}

has_device() { "$QEMU" -device help 2>/dev/null | grep -q "\"$1\""; }

start_guest() {
    if alive "$RUN/qemu.pid"; then return; fi
    install -m 600 "$REPO/.guest/ssh/id_ed25519" "$KEY" 2>/dev/null || cp "$REPO/.guest/ssh/id_ed25519" "$KEY"
    chmod 600 "$KEY"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    CONSOLE="$RUN/console-$stamp.log"
    ln -sf "$CONSOLE" "$RUN/console.log"
    local spi=()
    if has_device vhost-user-spi-pci; then
        spi=(-chardev "socket,id=vspi,path=$RUN/spi.sock" -device vhost-user-spi-pci,chardev=vspi)
    else
        echo "note: $QEMU has no vhost-user-spi-pci (QEMU 11.0 or later has it); SPI is left out" >&2
    fi
    "$QEMU" -accel tcg,thread=multi -machine pc,memory-backend=mem -cpu max -smp 2 -m 1G \
        -object memory-backend-memfd,id=mem,size=1G,share=on \
        -chardev "socket,id=vi2c,path=$RUN/i2c.sock" -device vhost-user-i2c-pci,chardev=vi2c \
        "${spi[@]}" \
        -drive "file=$IMAGE/disk-qemu.vmdk,if=ide,id=drv0,snapshot=on" \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" -device virtio-net-pci,netdev=net0 \
        -kernel "$IMAGE/ifs.bin" -display none -vga none \
        -chardev "socket,id=s0,host=127.0.0.1,port=$SERIAL_PORT,server=on,wait=off,logfile=$CONSOLE" \
        -serial chardev:s0 -pidfile "$RUN/qemu.pid" -daemonize
    local t0=$SECONDS
    while [ $((SECONDS - t0)) -lt "${BOOT_TIMEOUT:-240}" ]; do
        if ssh_guest true 2>/dev/null; then
            echo "guest up after $((SECONDS - t0)) s (QEMU, TCG); console: $CONSOLE"
            return 0
        fi
        sleep 3
    done
    echo "guest did not answer on ssh in ${BOOT_TIMEOUT:-240} s; console: $CONSOLE" >&2
    return 1
}

stop_all() {
    for p in qemu backend; do
        if alive "$RUN/$p.pid"; then kill "$(cat "$RUN/$p.pid")"; fi
        rm -f "$RUN/$p.pid"
    done
}

case "${1:-start}" in
    start) start_backend; start_guest ;;
    backend) start_backend ;;
    stop) stop_all ;;
    ssh) shift; ssh_guest "$@" ;;
    ctl) shift; ctl "$@" ;;
    *) echo "usage: $0 start|stop|ssh CMD|ctl ARGS" >&2; exit 64 ;;
esac
