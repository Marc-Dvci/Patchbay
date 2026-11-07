"""Runs every fault against the drivers in a booted guest and writes the fault matrix.

    python3 scripts/fault_matrix.py [--out docs/results]

Needs a guest started with scripts/run_guest.sh start. For each row: inject the fault through the back end's control
socket, run pbprobe in the guest (the same devctl() calls as QNX's samples), compare with the expected outcome, clear
the fault, check that the next transfer succeeds and that the driver process is still running.

The guest runs under software emulation (QEMU, TCG). The table reports outcomes only; pbprobe's guest_ms figure is
in the raw log to show that each call returned, not as a timing measurement.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = [str(ROOT / "scripts" / "run_guest.sh")]
LINE = re.compile(r"^(?P<op>\S+) .*rc=(?P<rc>\S+) data=(?P<data>\S+) guest_ms=(?P<ms>\d+)")

I2C_PROBE = "pbprobe i2c-read 1 0x28 0x00 1"  # BNO055 CHIP_ID: a0
SHT_PROBE = "pbprobe i2c-write 1 0x44 0xF3 0x2D"  # SHT3x read-status command: acknowledged when present
SPI_PROBE = "pbprobe spi-xchg 0 0 018000"  # MCP3008 channel 0, the sample's frame


@dataclass
class Result:
    op: str
    rc: str
    data: str
    raw: str


def guest(cmd: str, timeout: float = 60) -> str:
    p = subprocess.run([*RUN, "ssh", cmd], capture_output=True, text=True, timeout=timeout)
    return p.stdout + p.stderr


def probe(cmd: str, count: int = 1) -> list[Result]:
    out = guest(cmd if count == 1 else f"{cmd} {count}")
    res = []
    for line in out.splitlines():
        m = LINE.match(line.strip())
        if m:
            res.append(Result(m["op"], m["rc"], m["data"], line.strip()))
    if not res:
        res.append(Result("?", "no-answer", "-", out.strip()))
    return res


def ctl(*args: str) -> dict[str, object]:
    p = subprocess.run([*RUN, "ctl", *args], capture_output=True, text=True, timeout=30)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": p.stdout + p.stderr}


def alive(name: str) -> bool:
    return name in guest("pidin ar")


def one_bit_from(data: str, want: str) -> bool:
    if len(data) != len(want) or data == "-":
        return False
    return bin(int(data, 16) ^ int(want, 16)).count("1") == 1


@dataclass
class Row:
    fault: str
    bus: str
    expected: str
    inject: Callable[[], None]
    check: Callable[[], tuple[bool, str]]
    recover: str  # probe that must succeed after clearing
    driver: str
    needs_recovery: bool = True


def run(rows: list[Row], log: list[str]) -> list[tuple[Row, bool, str, bool, bool]]:
    out = []
    for row in rows:
        ctl("clear")
        row.inject()
        ok, observed = row.check()
        ctl("clear")
        if not row.needs_recovery:
            running = alive(row.driver) and alive("spi-virtio")
            log.append(f"## {row.fault} ({row.bus})\nobserved: {observed}\n")
            out.append((row, ok, observed, True, running))
            print(f"{'PASS' if ok and running else 'FAIL'}  {row.bus} {row.fault}: {observed}", flush=True)
            continue
        after = probe(row.recover)
        recovered = after[-1].rc == "EOK"
        if not recovered:  # one more try after a device reset
            time.sleep(1)
            after = probe(row.recover)
            recovered = after[-1].rc == "EOK"
        running = alive(row.driver)
        log.append(f"## {row.fault} ({row.bus})\nobserved: {observed}\nafter clear: {after[-1].raw}\n")
        out.append((row, ok, observed, recovered, running))
        print(f"{'PASS' if ok and recovered and running else 'FAIL'}  {row.bus} {row.fault}: {observed}", flush=True)
    return out


def fault(kind: str, bus: str, *extra: str) -> Callable[[], None]:
    def inject() -> None:
        reply = ctl("fault", kind, "--bus", bus, *extra)
        if not reply.get("ok"):
            raise RuntimeError(f"back end refused the fault: {reply}")

    return inject


def corrupt_against_frozen_reading(cmd: str, bus: str, target: str) -> Callable[[], tuple[bool, str]]:
    """Stop simulated time, take a clean reading, inject one corruption, read again, start time again."""

    def check() -> tuple[bool, str]:
        ctl("freeze")
        try:
            clean = probe(cmd)[0]
            fault("corrupt", bus, "--target", target, "--count", "1", "--seed", "1")()
            bad = probe(cmd)[0]
        finally:
            ctl("thaw")
        ok = clean.rc == "EOK" and bad.rc == "EOK" and one_bit_from(bad.data, clean.data)
        return ok, f"clean {clean.data}, corrupted {bad.rc} {bad.data}"

    return check


def expect(cmd: str, pred: Callable[[list[Result]], bool], count: int = 1) -> Callable[[], tuple[bool, str]]:
    def check() -> tuple[bool, str]:
        r = probe(cmd, count)
        return pred(r), "; ".join(f"{x.rc} {x.data}" for x in r)

    return check


def build_rows() -> list[Row]:
    i2c, spi = "i2c-virtio", "spi-virtio"
    rows = [
        Row(
            "none (baseline)",
            "I2C",
            "EOK, a0",
            lambda: None,
            expect(I2C_PROBE, lambda r: r[0].rc == "EOK" and r[0].data == "a0"),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "nak, count 1",
            "I2C",
            "first EIO, second EOK a0",
            fault("nak", "i2c", "--target", "0x28", "--count", "1"),
            expect(I2C_PROBE, lambda r: [x.rc for x in r] == ["EIO", "EOK"] and r[1].data == "a0", 2),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "unplug 0x28",
            "I2C",
            "EIO at 0x28; SHT3x at 0x44 still EOK",
            fault("unplug", "i2c", "--target", "0x28"),
            lambda: (lambda a, b: (a[0].rc == "EIO" and b[0].rc == "EOK", f"0x28 {a[0].rc}; 0x44 {b[0].rc}"))(
                probe(I2C_PROBE), probe(SHT_PROBE)
            ),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "corrupt, count 1, seed 1",
            "I2C",
            "EOK with one bit different from a0, then a0",
            fault("corrupt", "i2c", "--target", "0x28", "--count", "1", "--seed", "1"),
            expect(I2C_PROBE, lambda r: r[0].rc == "EOK" and one_bit_from(r[0].data, "a0") and r[1].data == "a0", 2),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "stuck",
            "I2C",
            "EIO after the 1000 ms deadline, device reset",
            fault("stuck", "i2c"),
            expect(I2C_PROBE, lambda r: r[0].rc == "EIO"),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "delay 300 ms",
            "I2C",
            "EOK a0 (under the deadline)",
            fault("delay", "i2c", "--delay-ms", "300", "--count", "1"),
            expect(I2C_PROBE, lambda r: r[0].rc == "EOK" and r[0].data == "a0"),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "delay 2000 ms",
            "I2C",
            "EIO (over the deadline), device reset",
            fault("delay", "i2c", "--delay-ms", "2000", "--count", "1"),
            expect(I2C_PROBE, lambda r: r[0].rc == "EIO"),
            I2C_PROBE,
            i2c,
        ),
        Row(
            "error, count 1",
            "I2C",
            "EIO",
            fault("error", "i2c", "--count", "1"),
            expect(I2C_PROBE, lambda r: r[0].rc == "EIO"),
            I2C_PROBE,
            i2c,
        ),
    ]
    rows += [
        Row(
            "none (baseline)",
            "SPI",
            "EOK, 3 bytes",
            lambda: None,
            expect(SPI_PROBE, lambda r: r[0].rc == "EOK" and len(r[0].data) == 6),
            SPI_PROBE,
            spi,
        ),
        Row(
            "unplug cs0",
            "SPI",
            "EOK with 000000: SPI cannot see a missing chip",
            fault("unplug", "spi", "--target", "0"),
            expect(SPI_PROBE, lambda r: r[0].rc == "EOK" and r[0].data == "000000"),
            SPI_PROBE,
            spi,
        ),
        Row(
            "corrupt cs0, count 1, seed 1",
            "SPI",
            "EOK with exactly one bit different from a clean reading",
            lambda: None,
            corrupt_against_frozen_reading(SPI_PROBE, "spi", "0"),
            SPI_PROBE,
            spi,
        ),
        Row(
            "stuck",
            "SPI",
            "ETIMEDOUT after the 1000 ms deadline, device reset",
            fault("stuck", "spi"),
            expect(SPI_PROBE, lambda r: r[0].rc in ("ETIMEDOUT", "EIO")),
            SPI_PROBE,
            spi,
        ),
        Row(
            "error, count 1",
            "SPI",
            "EIO",
            fault("error", "spi", "--target", "0", "--count", "1"),
            expect(SPI_PROBE, lambda r: r[0].rc == "EIO"),
            SPI_PROBE,
            spi,
        ),
        Row(
            "16-bit words requested",
            "SPI",
            "SET_CONFIG EINVAL: the controller offers 8 only",
            lambda: None,
            expect("pbprobe spi-cfg 0 0 0x410 1000000", lambda r: r[0].rc == "EINVAL"),
            SPI_PROBE,
            spi,
        ),
        Row(
            "clock over max_freq_hz",
            "SPI",
            "SET_CONFIG EINVAL",
            lambda: None,
            expect("pbprobe spi-cfg 0 0 0x408 60000000", lambda r: r[0].rc == "EINVAL"),
            SPI_PROBE,
            spi,
        ),
    ]
    # Last, because the controller does not come back for this guest: QEMU 11.1.3 does not reconnect a vhost-user
    # back end (see docs/RESULTS.md). Both drivers must fail every transfer within the deadline and keep running.
    rows.append(
        Row(
            "back end process stopped",
            "I2C+SPI",
            "every transfer fails within the deadline; both drivers keep running",
            lambda: subprocess.run([*RUN, "kill-backend"], check=False) and None,
            backend_gone_check,
            "",
            "i2c-virtio",
            needs_recovery=False,
        )
    )
    return rows


def backend_gone_check() -> tuple[bool, str]:
    a, b = probe(I2C_PROBE, 2), probe(SPI_PROBE, 2)
    ok = all(x.rc == "EIO" for x in a) and all(x.rc in ("EIO", "ETIMEDOUT") for x in b)
    qemu = subprocess.run(
        [
            "bash",
            "-c",
            f"kill -0 $(cat {Path.home()}/Patchbay-work/run/qemu.pid) 2>/dev/null && echo alive || echo gone",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    raw = " | ".join(x.raw[:80] for x in a + b if x.rc == "no-answer")
    if qemu == "gone":
        return ok, "QEMU exited when the back end process did, so there was no guest left to test"
    return ok, "; ".join(f"{x.op} {x.rc}" for x in a + b) + (f"; raw: {raw}" if raw else "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(ROOT / "docs" / "results"))
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    results = run(build_rows(), log)
    passed = sum(1 for _, ok, _, rec, run_ in results if ok and rec and run_)
    lines = [
        "# Fault matrix",
        "",
        f"Run {started}, guest in QEMU, TCG. Command: `python3 scripts/fault_matrix.py`. "
        f"{passed} of {len(results)} rows as expected.",
        "",
        "| Bus | Fault | Expected | Observed (rc, data) | As expected | Next transfer after clearing | Driver running |",
        "|---|---|---|---|---|---|---|",
    ]
    for row, ok, observed, rec, run_ in results:
        lines.append(
            f"| {row.bus} | {row.fault} | {row.expected} | {observed} | {'yes' if ok else '**no**'} | "
            f"{('EOK' if rec else '**failed**') if row.needs_recovery else 'n/a'} | {'yes' if run_ else '**no**'} |"
        )
    (out / "fault_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"fault_matrix-{stamp}.log").write_text("\n".join(log), encoding="utf-8")
    print(f"{passed}/{len(results)} rows as expected; table in {out / 'fault_matrix.md'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
