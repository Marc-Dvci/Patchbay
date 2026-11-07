/*
 * pbprobe: one line per bus transfer, for Patchbay's fault matrix. Uses the same devctl() calls as QNX's samples.
 *
 *   pbprobe i2c-read  BUS ADDR REG LEN [COUNT]     DCMD_I2C_SENDRECV: write REG, read LEN bytes
 *   pbprobe i2c-write BUS ADDR REG BYTE            DCMD_I2C_SEND: write REG then BYTE
 *   pbprobe i2c-recv  BUS ADDR LEN                 DCMD_I2C_RECV: read LEN bytes
 *   pbprobe spi-xchg  BUS DEV HEX [COUNT]          DCMD_SPI_DATA_XCHNG, full duplex
 *   pbprobe spi-cfg   BUS DEV MODE HZ              DCMD_SPI_SET_CONFIG
 *
 * Each line: op, errno name, received bytes in hex, and the call's duration on the guest's own clock. Under software
 * emulation that duration is not a timing measurement; it shows the call returned and roughly when.
 */
#include <devctl.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <hw/i2c.h>
#include <hw/io-spi.h>

static const char *ename(int e)
{
    switch (e) {
    case EOK: return "EOK";
    case EIO: return "EIO";
    case ETIMEDOUT: return "ETIMEDOUT";
    case EINVAL: return "EINVAL";
    case ENOENT: return "ENOENT";
    case EBUSY: return "EBUSY";
    case ENODEV: return "ENODEV";
    default: return "E?";
    }
}

static double ms_since(const struct timespec *t0)
{
    struct timespec t1;
    clock_gettime(CLOCK_MONOTONIC, &t1);
    return (double)(t1.tv_sec - t0->tv_sec) * 1e3 + (double)(t1.tv_nsec - t0->tv_nsec) / 1e6;
}

static void hex(const uint8_t *b, unsigned n)
{
    for (unsigned i = 0; i < n; i++) {
        printf("%02x", b[i]);
    }
    if (n == 0) {
        printf("-");
    }
}

static int open_path(const char *fmt, unsigned a, unsigned b)
{
    char path[64];
    snprintf(path, sizeof(path), fmt, a, b);
    int fd = open(path, O_RDWR);
    if (fd < 0) {
        printf("open %s %s\n", path, ename(errno));
    }
    return fd;
}

static int i2c_read(unsigned bus, unsigned addr, unsigned reg, unsigned len, unsigned count)
{
    int fd = open_path("/dev/i2c%u", bus, 0);
    if (fd < 0) {
        return 1;
    }
    struct {
        i2c_sendrecv_t h;
        uint8_t buf[256];
    } m;
    if (len > sizeof(m.buf)) {
        return 2;
    }
    for (unsigned k = 0; k < count; k++) {
        memset(&m, 0, sizeof(m));
        m.h.slave.addr = addr;
        m.h.slave.fmt = I2C_ADDRFMT_7BIT;
        m.h.send_len = 1;
        m.h.recv_len = len;
        m.h.stop = 1;
        m.buf[0] = (uint8_t)reg;
        struct timespec t0;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        int rc = devctl(fd, DCMD_I2C_SENDRECV, &m, sizeof(m.h) + (len > 1 ? len : 1), NULL);
        printf("i2c-read addr=0x%02x reg=0x%02x rc=%s data=", addr, reg, ename(rc));
        if (rc == EOK) {
            hex(m.buf, len);
        } else {
            hex(NULL, 0);
        }
        printf(" guest_ms=%.0f\n", ms_since(&t0));
        fflush(stdout);
    }
    close(fd);
    return 0;
}

static int i2c_write(unsigned bus, unsigned addr, unsigned reg, unsigned byte)
{
    int fd = open_path("/dev/i2c%u", bus, 0);
    if (fd < 0) {
        return 1;
    }
    struct {
        i2c_send_t h;
        uint8_t buf[2];
    } m = { .h = { .slave = { addr, I2C_ADDRFMT_7BIT }, .len = 2, .stop = 1 }, .buf = { (uint8_t)reg, (uint8_t)byte } };
    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    int rc = devctl(fd, DCMD_I2C_SEND, &m, sizeof(m), NULL);
    printf("i2c-write addr=0x%02x reg=0x%02x rc=%s data=- guest_ms=%.0f\n", addr, reg, ename(rc), ms_since(&t0));
    close(fd);
    return 0;
}

static int i2c_recv(unsigned bus, unsigned addr, unsigned len)
{
    int fd = open_path("/dev/i2c%u", bus, 0);
    if (fd < 0) {
        return 1;
    }
    struct {
        i2c_recv_t h;
        uint8_t buf[256];
    } m;
    if (len == 0 || len > sizeof(m.buf)) {
        return 2;
    }
    memset(&m, 0, sizeof(m));
    m.h.slave.addr = addr;
    m.h.slave.fmt = I2C_ADDRFMT_7BIT;
    m.h.len = len;
    m.h.stop = 1;
    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    int rc = devctl(fd, DCMD_I2C_RECV, &m, sizeof(m.h) + len, NULL);
    printf("i2c-recv addr=0x%02x reg=- rc=%s data=", addr, ename(rc));
    if (rc == EOK) {
        hex(m.buf, len);
    } else {
        hex(NULL, 0);
    }
    printf(" guest_ms=%.0f\n", ms_since(&t0));
    close(fd);
    return 0;
}

static int spi_xchg(unsigned bus, unsigned dev, const char *hexs, unsigned count)
{
    int fd = open_path("/dev/io-spi/spi%u/dev%u", bus, dev);
    if (fd < 0) {
        return 1;
    }
    unsigned n = (unsigned)strlen(hexs) / 2u;
    if (n == 0 || n > 256) {
        return 2;
    }
    struct {
        spi_xchng_t h;
        uint8_t data[256];
    } m;
    uint8_t tx[256];
    for (unsigned i = 0; i < n; i++) {
        unsigned v;
        sscanf(hexs + 2 * i, "%2x", &v);
        tx[i] = (uint8_t)v;
    }
    for (unsigned k = 0; k < count; k++) {
        m.h.nbytes = n;
        memcpy(m.data, tx, n);
        struct timespec t0;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        int rc = devctl(fd, DCMD_SPI_DATA_XCHNG, &m, sizeof(m.h) + n, NULL);
        printf("spi-xchg dev=%u tx=%s rc=%s data=", dev, hexs, ename(rc));
        if (rc == EOK) {
            hex(m.data, n);
        } else {
            hex(NULL, 0);
        }
        printf(" guest_ms=%.0f\n", ms_since(&t0));
        fflush(stdout);
    }
    close(fd);
    return 0;
}

static int spi_cfg(unsigned bus, unsigned dev, unsigned mode, unsigned hz)
{
    int fd = open_path("/dev/io-spi/spi%u/dev%u", bus, dev);
    if (fd < 0) {
        return 1;
    }
    spi_cfg_t c = { .mode = mode, .clock_rate = hz };
    int rc = devctl(fd, DCMD_SPI_SET_CONFIG, &c, sizeof(c), NULL);
    printf("spi-cfg dev=%u mode=0x%x hz=%u rc=%s data=- guest_ms=0\n", dev, mode, hz, ename(rc));
    close(fd);
    return 0;
}

int main(int argc, char *argv[])
{
    if (argc >= 6 && strcmp(argv[1], "i2c-read") == 0) {
        return i2c_read(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0),
                        strtoul(argv[5], NULL, 0), argc > 6 ? strtoul(argv[6], NULL, 0) : 1);
    }
    if (argc == 6 && strcmp(argv[1], "i2c-write") == 0) {
        return i2c_write(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0),
                         strtoul(argv[5], NULL, 0));
    }
    if (argc == 5 && strcmp(argv[1], "i2c-recv") == 0) {
        return i2c_recv(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0));
    }
    if (argc >= 5 && strcmp(argv[1], "spi-xchg") == 0) {
        return spi_xchg(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), argv[4],
                        argc > 5 ? strtoul(argv[5], NULL, 0) : 1);
    }
    if (argc == 6 && strcmp(argv[1], "spi-cfg") == 0) {
        return spi_cfg(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0),
                       strtoul(argv[5], NULL, 0));
    }
    fprintf(stderr, "usage: pbprobe i2c-read BUS ADDR REG LEN [COUNT] | i2c-write BUS ADDR REG BYTE |\n"
                    "               spi-xchg BUS DEV HEX [COUNT] | spi-cfg BUS DEV MODE HZ\n");
    return 64;
}
