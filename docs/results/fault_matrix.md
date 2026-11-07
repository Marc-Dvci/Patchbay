# Fault matrix

Run 2026-11-07 03:09:38, guest in QEMU, TCG. Command: `python3 scripts/fault_matrix.py`. 15 of 16 rows as expected.

| Bus | Fault | Expected | Observed (rc, data) | As expected | Next transfer after clearing | Driver running |
|---|---|---|---|---|---|---|
| I2C | none (baseline) | EOK, a0 | EOK a0 | yes | EOK | yes |
| I2C | nak, count 1 | first EIO, second EOK a0 | EIO -; EOK a0 | yes | EOK | yes |
| I2C | unplug 0x28 | EIO at 0x28; SHT3x at 0x44 still EOK | 0x28 EIO; 0x44 EOK | yes | EOK | yes |
| I2C | corrupt, count 1, seed 1 | EOK with one bit different from a0, then a0 | EOK a2; EOK a0 | yes | EOK | yes |
| I2C | stuck | EIO after the 1000 ms deadline, device reset | EIO - | yes | EOK | yes |
| I2C | delay 300 ms | EOK a0 (under the deadline) | EOK a0 | yes | EOK | yes |
| I2C | delay 2000 ms | EIO (over the deadline), device reset | EIO - | yes | EOK | yes |
| I2C | error, count 1 | EIO | EIO - | yes | EOK | yes |
| SPI | none (baseline) | EOK, 3 bytes | EOK 0002fa | yes | EOK | yes |
| SPI | unplug cs0 | EOK with 000000: SPI cannot see a missing chip | EOK 000000 | yes | EOK | yes |
| SPI | corrupt cs0, count 1, seed 1 | EOK with exactly one bit different from a clean reading | clean 0000cb, corrupted EOK 0200cb | yes | EOK | yes |
| SPI | stuck | ETIMEDOUT after the 1000 ms deadline, device reset | ETIMEDOUT - | yes | EOK | yes |
| SPI | error, count 1 | EIO | EIO - | yes | EOK | yes |
| SPI | 16-bit words requested | SET_CONFIG EINVAL: the controller offers 8 only | EINVAL - | yes | EOK | yes |
| SPI | clock over max_freq_hz | SET_CONFIG EINVAL | EINVAL - | yes | EOK | yes |
| I2C+SPI | back end process stopped | every transfer fails within the deadline; both drivers keep running | ? no-answer; ? no-answer; QEMU process gone; raw: Connection to 127.0.0.1 closed by remote host. | ssh: connect to host 127.0.0.1 port 2252: Connection refused | **no** | n/a | **no** |
