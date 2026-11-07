# Patchbay

[![Tests](https://github.com/Marc-Dvci/Patchbay/actions/workflows/tests.yml/badge.svg)](https://github.com/Marc-Dvci/Patchbay/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![QNX SDP 8.0](https://img.shields.io/badge/QNX%20SDP-8.0-0a7d6e)](#build-and-run)

**Virtio I2C and SPI master drivers for QNX 8.0, built on the QNX Driver Development Kit, with a back end that
simulates real sensor chips, so QNX's own sensor samples run unmodified in a VM, with no board.**

[![The patch panel replaying a recorded session: QNX's BNO055 sample runs in the guest, the BNO055's cord is pulled, its transfers turn to "no acknowledge" and the sample prints Input/output errors, then the cord goes back in](docs/panel.gif)](docs/panel-1440.png)

> [!TIP]
> No QNX licence, no guest: `python3 -m patchbay panel --recorded` and open http://127.0.0.1:8093/. It replays a
> session recorded from a real guest, and says so on the page. Or `docker compose up demo` (no keys, serves the same
> page on 127.0.0.1:8093), or open this repository in a Codespace: the dev container starts the same page.

| | |
|---|---|
| Track | Low-level Software (device drivers with the QNX DDK) |
| Built with | QNX SDP 8.0 (`qcc`, `libi2c-master.a`, `libio-spi.a`, `libpci`), QEMU 11.1.3 vhost-user, Python 3.12 |
| Target | QNX 8.0 x86_64 guest in QEMU (also builds for aarch64le) |
| Headline | 3 unmodified QNX samples run on simulated BNO055, SHT3x and MCP3008; 16 of 16 fault rows as expected ([results](docs/RESULTS.md)) |

[For judges](#for-judges) · [The patch panel](#the-patch-panel) · [Run it](#build-and-run) ·
[How it works](#how-it-works) · [Evidence](#evidence) · [Limitations](#what-it-does-not-do)

## For judges

| Criterion | Where to look |
|---|---|
| Technological implementation | DDK drivers: [`driver/qnx/i2c-virtio.c`](driver/qnx/i2c-virtio.c) (`i2c_master_getfuncs`), [`driver/qnx/spi-virtio.c`](driver/qnx/spi-virtio.c) (`spi_init`, `spi_funcs_t`); virtio PCI transport [`driver/qnx/pb_vpci.c`](driver/qnx/pb_vpci.c); split ring [`driver/core/pb_vring.c`](driver/core/pb_vring.c); vhost-user back end [`patchbay/server.py`](patchbay/server.py); a lost interrupt traced to its cause ([RESULTS](docs/RESULTS.md#a-lost-interrupt-found-by-the-fault-matrix-root-cause-and-fix)) |
| Design | the [patch panel](#the-patch-panel): pull a cord, watch QNX's sample react; one script from nothing to a running sample: [`scripts/demo.sh`](scripts/demo.sh) |
| Potential impact | [The problem](#the-problem) |
| Innovation | real chip models from data sheets ([`patchbay/chips/`](patchbay/chips)); a fault matrix against QNX's own sample API ([`scripts/fault_matrix.py`](scripts/fault_matrix.py)); the driver's C core tested against an independent Python device ([`tests/cross/`](tests/cross)) |
| Evidence | [`docs/RESULTS.md`](docs/RESULTS.md), raw outputs in [`docs/results/`](docs/results) |

## The problem

QNX's sensor samples ([qnx/Raspberry-Pi-Hardware-Component-Samples](https://github.com/qnx/Raspberry-Pi-Hardware-Component-Samples))
open `/dev/i2c1` and `/dev/io-spi/spi0/dev0` on a Raspberry Pi. A student without a Pi, or without the sensor
breakout, cannot run them. QNX reports 24,000 students in its academic programme, and a sample that needs a board
and a breakout is out of reach for any of them who have neither.

The same gap exists in production. On a hypervisor, a guest reaches its peripherals through virtio: the guest runs a
virtio driver and the device lives in another VM or process. The virtio specification defines an I2C adapter (device
34) and an SPI controller (device 45). For QNX's own guest driver package, `com.qnx.qnx800.target.driver.virtio`, the
software center's package metadata lists block and console drivers only (see [RESULTS](docs/RESULTS.md#kill-tests)).
No public QNX port of a virtio I2C or SPI driver was found.

Patchbay fills both: the drivers are ordinary DDK drivers a QNX guest can use on any hypervisor that offers these
devices, and the back end gives a student the chips.

## The patch panel

A driver has no screen, so Patchbay draws the bench: the two buses on top, the chips below, a cord for each
connection. `scripts/run_guest.sh panel` opens it next to a running guest, with QNX's three samples running in it:

- every transfer on each bus as the back end saw it, with the chip's reading of it: `BNO055 @0x28 CHIP_ID = 0xA0`,
  `SHT3x @0x44 T 22.63 °C, RH 45.58 %, CRC ok`, `MCP3008 cs0 CH0 single-ended → 212 (0.68 V)`;
- buttons to pull a cord, make a chip refuse transfers, corrupt its readings, hold a bus stuck or delay it. Each sends
  the same control request as the fault matrix;
- the samples' own output beside it, as they print it in the guest.

![The patch panel at 1440 px, replaying the recorded session at the moment the BNO055's cord is pulled](docs/panel-1440.png)

`python3 -m patchbay panel --recorded` replays a 50-second session recorded from a real guest
([which run](docs/RESULTS.md#the-patch-panels-recorded-session)); its fault buttons are disabled, and the banner says
it is a recording. The page is served on 127.0.0.1 only, puts every value from the guest on the page as text, works
from the keyboard, and has no axe-core WCAG 2.2 AA violations at 1440 and 390 px (`tests/e2e/`).

## How it works

![Architecture: QNX's samples call the DDK libraries and Patchbay's drivers in the guest; QEMU's vhost-user devices hand the queues to the Patchbay back end, which simulates the chips, injects faults and feeds the patch panel](docs/patchbay-architecture.png)

<details><summary>The same as text</summary>

```
 QNX 8.0 guest (QEMU)                                   Linux host (or WSL2)
 imu-bno055  temp-humidity-sht3x  mcp-3008-joystick     python -m patchbay serve
   (upstream, unmodified)                                 vhost-user socket per device
   devctl(DCMD_I2C_*)   devctl(DCMD_SPI_*)                VirtioI2c ── I2cBus: BNO055 @0x28, SHT3x @0x44
 /dev/i2c1            /dev/io-spi/spi0/dev0               VirtioSpi ── SpiBus: MCP3008 on cs0
 libi2c-master.a      libio-spi.a       (QNX DDK)         FaultPlan <── control socket (JSON)
 i2c-virtio           spi-virtio        (Patchbay)
   pb_i2c / pb_spi framing, pb_vring split ring  (portable C, tested on the host)
   pb_vpci: libpci, virtio capabilities, INTx, deadlines
        │ PCI 1af4:1062, 1af4:106d
 QEMU vhost-user-i2c-pci, vhost-user-spi-pci ─────────── shared guest RAM (memfd), kick/call eventfds
```

</details>

QEMU keeps the PCI device model; the request queues are served by the back end, which reads and writes the guest's
memory directly. Each transfer has a deadline (1000 ms by default): a device that stops answering is reset, the
transfer fails with EIO (I2C) or ETIMEDOUT (SPI), and the next transfer starts on an empty queue.

## How QNX is used

| QNX part | Use |
|---|---|
| I2C DDK, `libi2c-master.a` | provides `main()`, the resource manager at `/dev/i2c<unit>` and the `DCMD_I2C_*` devctls; Patchbay supplies the hardware functions through `i2c_master_getfuncs()` |
| SPI DDK, `libio-spi.a` | parses `spi.conf`, runs a bus thread and a node per device; Patchbay supplies `spi_init()` and `spi_funcs_t` |
| PCI server, `libpci` | `pci_device_find`, `pci_device_attach`, capability walk in configuration space, `pci_device_map_as`, `pci_device_read_irq`, checked against the Interrupt Line register |
| Kernel | `InterruptAttachEvent()` with a pulse, `InterruptUnmask()` on a dedicated thread (the INTx line is shared), `ThreadCtl(_NTO_TCTL_IO_LEVEL)`, `mmap(MAP_PHYS)` and `mem_offset64()` for DMA memory |
| `mkqnximage` | builds the guest image with the drivers, `spi.conf`, the samples and `pbprobe` ([`image/`](image)) |

### Evidence of QNX use

- `driver/qnx/i2c-virtio.c`: `i2c_master_getfuncs()`, `i2cv_send()`, `i2cv_recv()`, `i2cv_xfer()`
- `driver/qnx/spi-virtio.c`: `spi_init()`, `spiv_xfer()`, `spiv_setcfg()`, `spi_create_devs()` call
- `driver/qnx/pb_vpci.c`: `pb_vpci_open()`, `find_caps()`, `bring_up()`, `pb_vpci_run()`, `start_irq()`, `irq_loop()`
- `driver/Makefile`: links `-l:libi2c-master.a` and `-l:libio-spi.a` with `qcc -Vgcc_ntox86_64`
- Output from the guest: [`docs/results/guest_devices.txt`](docs/results/guest_devices.txt),
  [`docs/results/sample_imu-bno055.txt`](docs/results/sample_imu-bno055.txt),
  [`docs/results/fault_matrix.md`](docs/results/fault_matrix.md)

## What it does not do

- No timing claims. The guest runs under software emulation (QEMU, TCG); results are functional only.
- virtio-i2c reports one error status, so the driver cannot tell an address NAK from a data NAK; both are EIO.
- 7-bit I2C addresses only; 8-bit SPI words (the back end offers 8; the driver passes 16 or 32 to a controller that offers them); no SPI DMA hooks: transfers already use the virtqueue's DMA memory.
- The simulated chips follow their data sheets for what the samples use; what each model leaves out is listed at the top of its source file.
- When the back end process stops, the guest stops with it: QEMU 11.1.3 exits when a driver first uses the device, and aborts if a back end reconnects. Recovery after a lost back end is neither offered nor tested ([RESULTS](docs/RESULTS.md#fault-matrix)).

> [!IMPORTANT]
> Real: the QNX drivers, the QNX sample binaries, the virtio devices in QEMU. Simulated: the chips behind the buses,
> and the faults.

## Evidence

All figures and their commands are in [`docs/RESULTS.md`](docs/RESULTS.md). In short (QEMU, TCG):

| Claim | Command | Test or file |
|---|---|---|
| The three samples run unmodified | `scripts/evidence.sh` | `docs/results/sample_*.txt` |
| 16 of 16 fault rows behave as expected; 15 keep the driver running, the 16th is a stopped guest | `python3 scripts/fault_matrix.py` | `docs/results/fault_matrix.md` |
| The C ring and framing agree with an independent Python device | `scripts/host_tests.sh` | `tests/cross/` |
| A recorded panel session from a real guest replays with no QNX | `python3 -m patchbay panel --recorded` | `tests/unit/test_panel.py` |
| The panel has no WCAG 2.2 AA violations (axe-core) and works from the keyboard | `pytest tests/e2e` | `tests/e2e/test_panel_browser.py` |
| Each of 9 deliberate bugs in the C core is caught | `scripts/mutation_check.sh` | `scripts/mutation_check.sh` |
| Chip models match their data sheets' examples (CRC-8 0xBEEF → 0x92, BNO055 IDs, MCP3008 bit order) | `scripts/host_tests.sh` | `tests/unit/test_sht3x.py`, `test_bno055.py`, `test_mcp3008.py` |

Tests: 17 C unit tests (106 checks, under ASan and UBSan) · 150 Python unit · 13 cross · 19 integration ·
5 browser. Back end coverage, lines and branches: 89 %.

## Build and run

**QNX version:** QNX SDP 8.0 (free QNX Everywhere licence). **Target:** QNX 8.0 x86_64 guest in QEMU 11.1.3.
**Toolchain:** `qcc -Vgcc_ntox86_64` (GCC 12.2) from the SDP. **Host:** Linux x86_64 or WSL2 (Ubuntu 22.04 tested),
Python 3.12. vhost-user needs QEMU on Linux, so QEMU for Windows cannot run it.

> [!TIP]
> One command, from a machine with QNX SDP 8.0 in `~/qnx800`:
> `scripts/demo.sh` builds QEMU 11.1.3 the first time, then the drivers, the samples and the image, boots the guest
> and prints the three samples' output.

Step by step:

```bash
sudo apt install build-essential ninja-build pkg-config libglib2.0-dev libpixman-1-dev libslirp-dev \
     python3-venv flex bison curl xz-utils
scripts/build_qemu.sh            # QEMU 11.1.3 with vhost-user-spi-pci, into ~/Patchbay-work/qemu
scripts/build_drivers.sh         # i2c-virtio, spi-virtio, pbprobe; the upstream samples at a pinned commit
image/build.sh                   # QNX image with mkqnximage
scripts/run_guest.sh start       # back end + guest; ssh on 127.0.0.1:2252
scripts/run_guest.sh ssh imu-bno055
scripts/run_guest.sh panel       # the patch panel, live, on http://127.0.0.1:8093/
scripts/run_guest.sh ctl fault nak --bus i2c --target 0x28 --count 3     # or from the command line
scripts/run_guest.sh stop
```

Host tests only (no QNX SDP needed):

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
scripts/host_tests.sh
```

## Layout

| Path | What |
|---|---|
| `driver/core/` | portable C: split virtqueue, virtio-i2c and virtio-spi framing |
| `driver/qnx/` | the two DDK drivers and the virtio PCI transport |
| `driver/tools/pbprobe.c` | one-line-per-transfer probe used by the fault matrix |
| `driver/tests/` | C unit tests of the core |
| `patchbay/` | the back end: vhost-user, virtio devices, buses, faults, chip models, decoder |
| `patchbay/panel.py`, `patchbay/panel_static/` | the patch panel's server and page |
| `patchbay/recordings/` | the recorded session the panel replays |
| `image/` | mkqnximage snippets, `spi.conf`, start script |
| `scripts/` | build, boot, demo, evidence, fault matrix, mutation check |
| `tests/` | Python unit, cross, integration and browser tests |

`scripts/host_tests.sh` runs everything that needs no QNX SDP.

## Built on

- QNX SDP 8.0 DDK libraries and headers (QNX Everywhere licence), not redistributed here.
- QEMU 11.1.3's `vhost-user-i2c-pci` and `vhost-user-spi-pci` (GPL-2.0-or-later), built from the official tarball, not included.
- QNX's sample programs, [qnx/Raspberry-Pi-Hardware-Component-Samples](https://github.com/qnx/Raspberry-Pi-Hardware-Component-Samples) (Apache-2.0), fetched and built at commit `fb1f9df`, not copied.
- Structure layouts from the virtio specification and Linux's `include/uapi/linux/virtio_i2c.h` and `virtio_spi.h`, restated, not copied.

## Licence

MIT, see [LICENSE](LICENSE).
