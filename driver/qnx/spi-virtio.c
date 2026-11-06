/*
 * spi-virtio: an SPI master driver for QNX 8.0 on the SPI DDK (libio-spi), for the virtio SPI controller.
 *
 *   spi-virtio [-c config_file] [-v]
 *
 * libio-spi provides main(), parses spi.conf, and runs one resource manager thread per bus with a node per device at
 * /dev/io-spi/<bus>/<dev>. This file provides spi_init() and the functions in spi_funcs_t. Per bus, the config's
 * bs= string takes index=N (which virtio SPI controller, default 0) and timeout=MS (per transfer, default 1000).
 * A device's devno is its chip select on the controller.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <hw/io-spi.h>

#include "pb_spi.h"
#include "pb_vpci.h"

#define DEFAULT_TIMEOUT_MS 1000u
#define MAX_XFER           SPI_BUF_MAX_64K
#define ARENA_BYTES        (PB_SPI_HEAD_LEN + 2u * MAX_XFER + 64u)

struct spiv {
    struct pb_vpci v;
    struct pb_spi_config cfg;
    unsigned index;
    uint64_t timeout_ns;
    int busno;
};

static int parse_bs(struct spiv *d, const char *bs)
{
    if (bs == NULL) {
        return 0;
    }
    char *copy = strdup(bs);
    if (copy == NULL) {
        return ENOMEM;
    }
    int rc = 0;
    char *save = NULL;
    for (char *tok = strtok_r(copy, ",", &save); tok != NULL; tok = strtok_r(NULL, ",", &save)) {
        if (strncmp(tok, "index=", 6) == 0) {
            d->index = (unsigned)strtoul(tok + 6, NULL, 0);
        } else if (strncmp(tok, "timeout=", 8) == 0) {
            unsigned long ms = strtoul(tok + 8, NULL, 0);
            if (ms == 0u || ms > 60000u) {
                rc = EINVAL;
                break;
            }
            d->timeout_ns = (uint64_t)ms * 1000000ull;
        } else {
            spi_slogf(1, "spi-virtio: unknown bs option '%s'", tok);
            rc = EINVAL;
            break;
        }
    }
    free(copy);
    return rc;
}

static void spiv_fini(void *const hdl)
{
    struct spiv *d = hdl;
    pb_vpci_close(&d->v);
    free(d);
}

static void spiv_drvinfo(const void *const hdl, spi_drvinfo_t *info)
{
    (void)hdl;
    info->version = (SPI_VERSION_MAJOR << SPI_VERMAJOR_SHIFT) | (SPI_VERSION_MINOR << SPI_VERMINOR_SHIFT) |
                    (SPI_REVISION << SPI_VERREV_SHIFT);
    strlcpy(info->name, "spi-virtio", sizeof(info->name));
    info->feature = 0;  /* no DMA interface: requests go through the virtqueue's own DMA arena */
}

static void spiv_devinfo(const void *const hdl, const spi_dev_t *const spi_dev, spi_devinfo_t *const info)
{
    (void)hdl;
    memcpy(info, &spi_dev->devinfo, sizeof(*info));
}

static int check_cfg(const struct spiv *d, const spi_cfg_t *cfg)
{
    uint8_t bpw = 0;
    uint32_t mode = pb_spi_mode_from_qnx(cfg->mode, &bpw);
    return pb_spi_check(&d->cfg, mode, bpw, cfg->clock_rate) == 0 ? EOK : EINVAL;
}

static int spiv_setcfg(const void *const hdl, spi_dev_t *spi_dev, const spi_cfg_t *const cfg)
{
    const struct spiv *d = hdl;
    if (cfg == NULL || spi_dev->devinfo.devno < 0 || spi_dev->devinfo.devno >= d->cfg.cs_max_number) {
        return EINVAL;
    }
    if (check_cfg(d, cfg) != EOK) {
        return EINVAL;
    }
    spi_dev->devinfo.cfg = *cfg;
    spi_dev->devinfo.current_clkrate = cfg->clock_rate;
    return EOK;
}

static int spiv_xfer(void *const hdl, spi_dev_t *const spi_dev, uint8_t *const buf, const uint32_t tnbytes,
                     const uint32_t rnbytes)
{
    struct spiv *d = hdl;
    if (buf == NULL || (tnbytes == 0u && rnbytes == 0u)) {
        return EINVAL;
    }
    if (tnbytes > MAX_XFER || rnbytes > MAX_XFER) {
        return EINVAL;
    }
    const spi_cfg_t *cfg = &spi_dev->devinfo.cfg;
    uint8_t bpw = 0;
    uint32_t mode = pb_spi_mode_from_qnx(cfg->mode, &bpw);
    struct pb_spi_req req;
    int rc = pb_spi_plan((uint8_t)spi_dev->devinfo.devno, bpw, mode, cfg->clock_rate, buf, tnbytes, rnbytes,
                         d->v.arena, d->v.arena_addr, d->v.arena_len, &req);
    if (rc != 0) {
        return EINVAL;
    }
    uint16_t head;
    if (pb_vq_add(&d->v.vq, &req.out, 1, &req.in, 1, &head) != 0) {
        pb_vpci_restart(&d->v);
        return EIO;
    }
    uint32_t used = 0;
    rc = pb_vpci_run(&d->v, &head, 1, &used, d->timeout_ns);
    if (rc == ETIMEDOUT) {
        spi_slogf(1, "spi-virtio: spi%d dev%d: no answer in %llu ms, device reset (%u so far)", d->busno,
                  spi_dev->devinfo.devno, (unsigned long long)(d->timeout_ns / 1000000ull), d->v.resets);
        return ETIMEDOUT;
    }
    if (rc != EOK) {
        return EIO;
    }
    switch (pb_spi_collect(&req, d->v.arena, used, buf, rnbytes)) {
    case PB_SPI_TRANS_OK:
        return EOK;
    case PB_SPI_PARAM_ERR:
        return EINVAL;
    default:
        return EIO;
    }
}

static int spiv_dma_xfer(void *const hdl, spi_dev_t *spi_dev, dma_addr_t *addr, const uint32_t tnbytes,
                         const uint32_t rnbytes)
{
    return ENOTSUP;
}

static int spiv_dma_allocbuf(void *const hdl, dma_addr_t *addr, const uint32_t len)
{
    return ENOTSUP;
}

static int spiv_dma_freebuf(void *const hdl, dma_addr_t *addr)
{
    return ENOTSUP;
}

static spi_funcs_t spiv_funcs = {
    .spi_fini = spiv_fini,
    .drvinfo = spiv_drvinfo,
    .devinfo = spiv_devinfo,
    .setcfg = spiv_setcfg,
    .xfer = spiv_xfer,
    .dma_xfer = spiv_dma_xfer,
    .dma_allocbuf = spiv_dma_allocbuf,
    .dma_freebuf = spiv_dma_freebuf,
};

int spi_init(spi_bus_t *bus)
{
    struct spiv *d = calloc(1, sizeof(*d));
    if (d == NULL) {
        return ENOMEM;
    }
    d->timeout_ns = (uint64_t)DEFAULT_TIMEOUT_MS * 1000000ull;
    d->busno = bus->busno;
    int rc = parse_bs(d, bus->bs);
    if (rc != 0) {
        free(d);
        return rc;
    }
    char err[160] = "";
    rc = pb_vpci_open(&d->v, PB_VIRTIO_ID_SPI, d->index, 0, 64u, ARENA_BYTES, err, sizeof(err));
    if (rc != EOK) {
        spi_slogf(0, "spi-virtio: %s", err);
        fprintf(stderr, "spi-virtio: %s\n", err);
        free(d);
        return rc;
    }
    uint8_t raw[PB_SPI_CONFIG_LEN];
    if (pb_vpci_read_config(&d->v, 0, raw, sizeof(raw)) != EOK) {
        fprintf(stderr, "spi-virtio: cannot read the controller's configuration\n");
        pb_vpci_close(&d->v);
        free(d);
        return EIO;
    }
    pb_spi_config_decode(raw, &d->cfg);
    for (spi_dev_t *dev = bus->devlist; dev != NULL; dev = dev->next) {
        if (dev->devinfo.devno < 0 || dev->devinfo.devno >= d->cfg.cs_max_number) {
            fprintf(stderr, "spi-virtio: %s: devno %d, but the controller has %u chip selects\n", dev->devinfo.name,
                    dev->devinfo.devno, d->cfg.cs_max_number);
            pb_vpci_close(&d->v);
            free(d);
            return EINVAL;
        }
    }
    bus->funcs = &spiv_funcs;
    bus->drvhdl = d;
    fprintf(stderr, "spi-virtio: %s on virtio SPI controller %u at PCI %x:%x.%x, %u chip selects, max %u Hz, "
                    "queue %u, irq %d%s\n",
            bus->name, d->index, PCI_BUS(d->v.bdf), PCI_DEV(d->v.bdf), PCI_FUNC(d->v.bdf), d->cfg.cs_max_number,
            d->cfg.max_freq_hz, d->v.qsize, d->v.irq, d->v.iid >= 0 ? "" : " (polling)");
    return EOK;
}
