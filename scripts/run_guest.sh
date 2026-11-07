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
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HOME/.local/bin/python3.12" python3.12 python3; do
        if command -v "$c" >/dev/null; then PY="$(command -v "$c")"; break; fi
    done
fi
if [ -z "${QEMU:-}" ]; then
    if [ -x "$WORK/qemu/bin/qemu-system-x86_64" ]; then QEMU="$WORK/qemu/bin/qemu-system-x86_64"; else QEMU=qemu-system-x86_64; fi
fi
mkdir -p "$RUN"
KEY="$RUN/guest_key"
# Each start gets a session directory for its sockets; "current" points at the latest one. QEMU creates the device
# sockets (server=on); each back end start gets its own control socket, named in control.path.
session() { readlink -f "$RUN/current"; }

ssh_guest() {
    ssh -i "$KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -o ConnectTimeout=5 -o BatchMode=yes root@127.0.0.1 "$@"
}

ctl() {
    PYTHONPATH="$REPO" "$PY" -m patchbay ctl --control "$(cat "$RUN/control.path")" "$@"
}

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# The back end of the current session: alive, and its command line names this session's sockets.
backend_alive() {
    alive "$RUN/backend.pid" && [ -e "$RUN/current" ] &&
        tr '\0' ' ' <"/proc/$(cat "$RUN/backend.pid")/cmdline" 2>/dev/null | grep -q "$(session)/"
}

new_session() {
    local s; s="$RUN/session-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$s"
    ln -sfn "$s" "$RUN/current"
}

start_backend() {
    if backend_alive; then return; fi
    [ -e "$RUN/current" ] || new_session
    local stamp s; stamp="$(date +%Y%m%d-%H%M%S)"; s="$(session)"
    BACKEND_LOG="$RUN/backend-$stamp.log"
    echo "$s/control-$stamp.sock" >"$RUN/control.path"
    PYTHONPATH="$REPO" nohup "$PY" -m patchbay serve --i2c "$s/i2c.sock" --spi "$s/spi.sock" \
        --control "$s/control-$stamp.sock" --connect --trace >"$BACKEND_LOG" 2>&1 &
    echo $! >"$RUN/backend.pid"
    ln -sf "$BACKEND_LOG" "$RUN/backend.log"
    for _ in $(seq 50); do grep -q 'ready:' "$BACKEND_LOG" 2>/dev/null && return; sleep 0.1; done
    echo "back end did not start; see $BACKEND_LOG" >&2
    exit 1
}

has_device() { "$QEMU" -device help 2>/dev/null | grep -q "\"$1\""; }

start_guest() {
    if alive "$RUN/qemu.pid"; then return; fi
    # QEMU waits for the back end to connect before it boots: without one it would wait forever
    backend_alive || { echo "no back end for this session; run: $0 backend" >&2; return 1; }
    install -m 600 "$REPO/.guest/ssh/id_ed25519" "$KEY" 2>/dev/null || cp "$REPO/.guest/ssh/id_ed25519" "$KEY"
    chmod 600 "$KEY"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    CONSOLE="$RUN/console-$stamp.log"
    ln -sf "$CONSOLE" "$RUN/console.log"
    local spi=() srv=server=on,wait=off s
    s="$(session)"
    if has_device vhost-user-spi-pci; then
        # shellcheck disable=SC2054  # the commas belong to QEMU's option syntax
        spi=(-chardev "socket,id=vspi,path=$s/spi.sock,$srv" -device vhost-user-spi-pci,chardev=vspi)
    else
        echo "note: $QEMU has no vhost-user-spi-pci (QEMU 11.0 or later has it); SPI is left out" >&2
    fi
    # QEMU listens on the device sockets and the back end connects (patchbay serve --connect): when the back end
    # restarts it connects again, and QEMU restarts the queues of the running guest.
    # timeout: QEMU's start blocks until the back end connects; give up rather than wait forever
    timeout 120 "$QEMU" -accel tcg,thread=multi -machine pc,memory-backend=mem -cpu max -smp 2 -m 1G \
        -object memory-backend-memfd,id=mem,size=1G,share=on \
        -chardev "socket,id=vi2c,path=$s/i2c.sock,$srv" -device vhost-user-i2c-pci,chardev=vi2c \
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
    done
}

case "${1:-start}" in
    start) new_session; start_backend; start_guest ;;
    backend) start_backend ;;
    stop) stop_all ;;
    restart-backend)
        # the controller disappears and comes back: stop the back end, wait, start a new one
        if alive "$RUN/backend.pid"; then kill "$(cat "$RUN/backend.pid")"; fi
        sleep "${2:-3}"
        start_backend ;;
    kill-backend)
        if alive "$RUN/backend.pid"; then kill "$(cat "$RUN/backend.pid")"; fi ;;
    ssh) shift; ssh_guest "$@" ;;
    ctl) shift; ctl "$@" ;;
    *) echo "usage: $0 start|stop|ssh CMD|ctl ARGS" >&2; exit 64 ;;
esac
