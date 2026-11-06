#!/bin/sh
# Starts Patchbay's drivers in the guest: /dev/i2c1 for the samples' I2C bus 1, /dev/io-spi/spi0/dev0 for SPI 0.
# Each driver finds its controller on PCI; without one it reports that and exits.
if [ ! -e /dev/i2c1 ]; then
    i2c-virtio -t 1000 --u 1 && waitfor /dev/i2c1 5
fi
if [ ! -e /dev/io-spi/spi0/dev0 ]; then
    spi-virtio -c /system/etc/spi-virtio.conf && waitfor /dev/io-spi/spi0/dev0 5
fi
ls /dev/i2c* /dev/io-spi/*/* 2>/dev/null
