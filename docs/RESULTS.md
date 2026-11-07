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
(8 seconds each, QEMU, TCG):

| Sample | Device | Output | First lines |
|---|---|---|---|
| `imu-bno055` | `/dev/i2c1`, BNO055 at 0x28 | [476 lines](results/sample_imu-bno055.txt) | `BNO055 Chip ID: 0xA0`, calibration reaches `SYS=3, GYR=3, ACC=3, MAG=3`, then Euler, quaternion, accelerometer, gravity, gyroscope and magnetometer readings |
| `temp-humidity-sht3x` | `/dev/i2c1`, SHT3x at 0x44 | [16 lines](results/sample_temp-humidity-sht3x.txt) | `Temp: 22.78°C, Humidity: 46.93%` |
| `mcp-3008-joystick` | `/dev/io-spi/spi0/dev0`, MCP3008 on chip select 0 | [66 lines](results/sample_mcp-3008-joystick.txt) | `Joystick X:  686, Y:  877` |

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

## A bug the fault matrix found

The first run of the matrix showed SPI transfers after a fault returning EOK but only at their 1000 ms deadline
(`guest_ms=1001` in the raw log): a completion interrupt on the INTx line both controllers share was missed, and the
transfer was only reaped when the wait ended. `pb_vpci_run()` now re-reads the used ring at least every 10 ms while
it waits. The second run shows the same calls returning at 10 to 12 ms. Both raw logs are in `results/`. These are
guest-clock figures under TCG: they show the change, not real-time behaviour.

## Host tests

`scripts/host_tests.sh` (no QNX SDP, no guest, no network):

| Kind | Count | What |
|---|---|---|
| C unit | 17 tests, 106 checks | `driver/tests/test_core.c`: ring and framing, under AddressSanitizer and UBSan |
| Python unit | 95 | device ring, vhost-user codec, BNO055, SHT3x, MCP3008, buses, faults, devices |
| Cross | 13 | the driver's C core (host build, ctypes) against the back end's Python device in shared memory |
| Integration | 10 | the back end's vhost-user state machine over real sockets, memfd and eventfds |

Coverage of the back end (`patchbay/`, lines and branches): 85 %. Mutation check (`scripts/mutation_check.sh`):
9 deliberate bugs in the C core, 9 caught, with a control run of the unmodified core first.

One bug found by these tests is pinned by a regression test: the Python device read a `bits_per_word_mask` of 0 as
"no width supported" instead of "no limitation" (found by the C/Python agreement property test).

## Found while bringing the drivers up in the guest

- libpci's x86 module reads configuration space with port I/O and `cli`: without
  `ThreadCtl(_NTO_TCTL_IO_LEVEL, ...)` the driver faulted in `hw_io_rd` (core dump read with `ntox86_64-gdb`).
- io-spi frees `bus->funcs` at shutdown, takes the DMA path whenever `dma_xfer` is set, and needs the driver to call
  `spi_create_devs()`; io-spi also reads `base=0` and `irq=0` in `spi.conf` as missing.
- QEMU's `vhost-user-i2c-pci` and `vhost-user-spi-pci` give a 4-entry queue, so each request uses exactly two
  descriptors and a write-then-read transaction fits.
