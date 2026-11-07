# Results

Every figure here comes from a command in this repository. The guest ran in **QEMU 11.1.3 under software emulation
(QEMU, TCG)** inside WSL2 on Windows, with no KVM. Patchbay claims outcomes only (completed, failed, timed out,
recovered, counts), not timing.

## Kill tests

| Test | Command | Result |
|---|---|---|
| Does QNX already ship virtio I2C or SPI? | `qnxsoftwarecenter_clt -list`, then the `provides` lists in the software center's package metadata (`packages.json` in its server cache) | What the metadata lists for `com.qnx.qnx800.target.driver.virtio` (all versions): the binaries `devb_virtio`, `devc_virtio` and `startup_armv8_fm`. No package in the metadata lists an I2C or SPI virtio driver. This is the metadata's listing, not a listing of the package's files: downloading the package needs a myQNX login, which this check did not use. No public QNX port of a virtio I2C or SPI driver was found. |
| Can a QNX guest do one I2C transaction against a simulated chip on this PC? | `scripts/run_guest.sh start`, then `imu-bno055` in the guest | Yes: `BNO055 Chip ID: 0xA0` through `i2c-virtio` and `/dev/i2c1` (QEMU, TCG). |

## The guest sees both controllers

`scripts/evidence.sh` → [`results/guest_devices.txt`](results/guest_devices.txt): PCI `1af4:1062` (virtio I2C,
device ID 34) and `1af4:106d` (virtio SPI, device ID 45); `/dev/i2c1`, `/dev/io-spi/spi0/dev0` and `dev1`;
`i2c-virtio` and `spi-virtio` running.

## QNX's samples, unmodified

Built from [qnx/Raspberry-Pi-Hardware-Component-Samples](https://github.com/qnx/Raspberry-Pi-Hardware-Component-Samples)
at `fb1f9df` by `scripts/build_samples.sh`, which refuses a modified tree. Output recorded by `scripts/evidence.sh 8`
(8 seconds each, QEMU, TCG, run of 7 Nov 2026 03:44):

| Sample | Device | Output | First lines |
|---|---|---|---|
| `imu-bno055` | `/dev/i2c1`, BNO055 at 0x28 | [457 lines](results/sample_imu-bno055.txt) | `BNO055 Chip ID: 0xA0`, calibration reaches `SYS=3, GYR=3, ACC=3, MAG=3`, then Euler, quaternion, accelerometer, gravity, gyroscope and magnetometer readings |
| `temp-humidity-sht3x` | `/dev/i2c1`, SHT3x at 0x44 | [16 lines](results/sample_temp-humidity-sht3x.txt) | `Temp:` about 22.6 °C, `Humidity:` about 45.5 % |
| `mcp-3008-joystick` | `/dev/io-spi/spi0/dev0`, MCP3008 on chip select 0 | [78 lines](results/sample_mcp-3008-joystick.txt) | `Joystick X:` and `Y:` codes from 0 to 1023 |

The back end's view of the same bus: [`results/backend_trace_tail.txt`](results/backend_trace_tail.txt), for example
`spi  cs0 tx 018000 rx3 ok 000262` (the sample's start bit, channel 0, and the 10-bit answer).

## Fault matrix

`python3 scripts/fault_matrix.py` (run by `scripts/evidence.sh`) → [`results/fault_matrix.md`](results/fault_matrix.md).
**15 of 16 rows as expected.** For each of those 15, the driver process was still running afterwards and the next
transfer after clearing the fault succeeded.

| Fault | Outcome at the client |
|---|---|
| I2C NAK for one transfer | that `devctl()` returns EIO, the next one EOK |
| I2C chip unplugged (0x28) | EIO at 0x28; the SHT3x at 0x44 still answers |
| I2C corrupted byte | EOK with one bit flipped (`a2` instead of `a0`); the next read is clean |
| I2C stuck bus, or a 2000 ms delay | EIO after the 1000 ms deadline; the driver resets the device and goes on |
| I2C 300 ms delay | EOK |
| I2C / SPI controller error | EIO |
| SPI chip unplugged | EOK with `000000`: SPI has no acknowledge, so a missing chip reads as zeros |
| SPI corrupted byte | EOK with one bit flipped against a reading taken with simulated time frozen |
| SPI stuck | ETIMEDOUT after the deadline, device reset |
| SPI 16-bit words, or a clock above `max_freq_hz` | `DCMD_SPI_SET_CONFIG` returns EINVAL |

**The failing row: the back end process stopped.** What happens: when the back end process exits, QEMU 11.1.3 exits
too, so no guest is left to test. That is how QEMU handles a vhost-user device whose back end goes away; the drivers
are never asked. "A device that disappears" is covered instead by the per-chip unplug rows and by the stuck rows (a
controller that stops answering).

### QEMU 11.1.3 aborts when a vhost-user back end reconnects (reproduction)

Making QEMU listen and the back end connect, so that a restarted back end could connect again, was tried and fails.
Recorded here, not reported upstream.

```bash
# QEMU 11.1.3 built by scripts/build_qemu.sh; Patchbay's back end from this repository
R=$(mktemp -d)
PYTHONPATH=. python3.12 -m patchbay serve --connect --i2c $R/i2c.sock --control $R/c1.sock &   # back end 1
~/Patchbay-work/qemu/bin/qemu-system-x86_64 -accel tcg -machine pc,memory-backend=mem -m 256M \
  -object memory-backend-memfd,id=mem,size=256M,share=on \
  -chardev socket,id=vi2c,path=$R/i2c.sock,server=on,wait=on -device vhost-user-i2c-pci,chardev=vi2c \
  -display none -vga none -nodefaults -S -monitor none &
sleep 6; kill %1; sleep 1                                                                    # back end 1 goes away
PYTHONPATH=. python3.12 -m patchbay serve --connect --i2c $R/i2c.sock --control $R/c2.sock &   # back end 2 connects
```

QEMU prints `Failed to set msg fds.` then
`../system/memory.c:2546: memory_region_del_eventfd: Assertion 'i != mr->ioeventfd_nb' failed.` and dumps core.
The same QEMU with `reconnect-ms=500` on a client-mode chardev fails earlier, at start-up:
`vhost_backend_init failed: Protocol error`, before the back end receives any message.

## A lost interrupt, found by the fault matrix: root cause and fix

The first run of the matrix showed SPI transfers returning EOK but only at their 1000 ms deadline (`guest_ms=1001` in
[`results/fault_matrix-20261107-030510.log`](results/)). An interim fix re-read the used ring every 10 ms; it hid the
problem, so it was replaced by the cause:

- With `PB_TRACE=1`, both drivers printed what the PCI server and the device's configuration space say about the
  interrupt. `i2c-virtio` (slot 2): `PCI server irq 10; config space interrupt line 10, pin 1`. `spi-virtio` (slot 3):
  `PCI server irq 10; config space interrupt line 11, pin 1`.
- With the re-check off (`PB_RECHECK_MS=0`), `pbprobe spi-xchg 0 0 018000 3` took 1002, 1000 and 1001 ms. Run while
  `pbprobe` loaded the I2C bus at the same time, the same transfers took 5, 3 and 3 ms: the SPI driver was only woken
  by the I2C controller's interrupts on IRQ 10. Its own interrupt went to IRQ 11, where nothing was attached.
- So the PCI server reported IRQ 10 for a device the firmware had routed to IRQ 11. `pb_vpci.c` (`start_irq()`) now
  attaches to the Interrupt Line register's value when it differs from the PCI server's on x86, and says so on the
  console. The periodic re-check is off by default; a transfer still re-reads the used ring at its deadline, so a lost
  interrupt costs one deadline, never a hang.
- After the fix, with no re-check: `pbprobe spi-xchg` took 2, 0 and 0 ms with the I2C bus idle
  ([`results/irq_check.txt`](results/irq_check.txt)), and every transfer after a cleared fault in the matrix returned
  in 1 to 4 ms. Guest-clock figures under TCG: they show which wake-up happened, not real-time behaviour.

## The patch panel's recorded session

[`patchbay/recordings/session.jsonl.gz`](../patchbay/recordings/session.jsonl.gz) was recorded on 7 Nov 2026 at
03:48 by `scripts/record_session.py`, against a guest started by `scripts/run_guest.sh start` (QNX 8.0 x86_64 in QEMU
11.1.3, TCG). 50 seconds: QNX's three samples running unmodified, while the script pulled the BNO055's cord, made the
SHT3x refuse three transfers, corrupted SHT3x and MCP3008 readings, held the I2C bus stuck for 4 seconds and delayed
two SPI answers by 1500 ms. It holds 3,049 I2C and 938 SPI transactions, 4,205 lines of sample output, 9 faults or
clears and 9 captions. `python -m patchbay panel --recorded` replays it; `tests/unit/test_panel.py` checks its
contents.

The first recording, a few minutes earlier, showed the SHT3x and joystick output arriving in a few large blocks: run
over ssh without a terminal, the samples' `printf` output is block-buffered. The samples now run on a guest terminal
(`ssh -tt`, `scripts/run_guest.sh ssh-tty`), so each line arrives as it is printed.

Seen in the recording: when its reads fail, `imu-bno055` prints `error with devctl: Input/output error` and then
values from buffers its read never filled (for example `Y=-40902509353289263805560000...`). The sample does not check
`bno055_read_vector()`'s return value. Patchbay does not change the sample; the panel shows what it prints.

## From a clean clone

`git clone` of this repository into a new folder, then `scripts/demo.sh all 6` from it (7 Nov 2026, 03:20, QNX SDP
8.0 in `~/qnx800`, QEMU 11.1.3 already built in `~/Patchbay-work/qemu`): it built the drivers, the upstream samples
and the image, booted the guest, and printed the output of all three samples (`Temp: 22.61°C, Humidity: 45.50%`,
`Joystick X:  705, Y:  867`, BNO055 readings) and the back end's trace. It exited with 0. Nothing in it had to be
fixed. The aarch64le build (`make -C driver ARCH=aarch64le`) produces `i2c-virtio`, `spi-virtio` and `pbprobe` as
`ELF 64-bit LSB pie executable, ARM aarch64`; they have not been run on an ARM guest.

## Host tests

`scripts/host_tests.sh` (no QNX SDP, no guest, no network):

| Kind | Count | What |
|---|---|---|
| C unit | 17 tests, 106 checks | `driver/tests/test_core.c`: ring and framing, under AddressSanitizer and UBSan |
| Python unit | 127 | device ring, vhost-user codec, BNO055, SHT3x, MCP3008, buses, faults, devices, decoder, panel API and replay |
| Cross | 13 | the driver's C core (host build, ctypes) against the back end's Python device in shared memory |
| Integration | 11 | the back end's vhost-user state machine over real sockets, memfd and eventfds; a panel fault button reaching the back end's control socket |
| Browser (e2e) | 4 | the panel in headless Chromium: axe-core WCAG 2.2 AA at 1440 and 390 px (0 violations), recorded mode disables faults, a fault button from the keyboard |

Coverage of the back end (`patchbay/`, lines and branches): 86 %. Mutation check (`scripts/mutation_check.sh`):
9 deliberate bugs in the C core, 9 caught, with a control run of the unmodified core first.

One bug found by these tests is pinned by a regression test: the Python device read a `bits_per_word_mask` of 0 as
"no width supported" instead of "no limitation" (found by the C/Python agreement property test).

## Found while bringing the drivers up in the guest

- libpci's x86 module reads configuration space with port I/O and `cli`: without
  `ThreadCtl(_NTO_TCTL_IO_LEVEL, ...)` the driver faulted in `hw_io_rd` (core dump read with `ntox86_64-gdb`).
- io-spi frees `bus->funcs` at shutdown, takes the DMA path whenever `dma_xfer` is set, and needs the driver to call
  `spi_create_devs()`; io-spi also reads `base=0` and `irq=0` in `spi.conf` as missing.
- The PCI server reported IRQ 10 for the SPI controller in slot 3, whose Interrupt Line register said 11 (see the lost interrupt above).
- QEMU's `vhost-user-i2c-pci` and `vhost-user-spi-pci` give a 4-entry queue, so each request uses exactly two
  descriptors and a write-then-read transaction fits.
