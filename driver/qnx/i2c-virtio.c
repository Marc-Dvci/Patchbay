/*
 * i2c-virtio: an I2C master driver for QNX 8.0 on the I2C DDK (libi2c-master), for the virtio I2C adapter.
 *
 *   i2c-virtio [-I index] [-t timeout_ms] [-v] --u unit
 *
 * libi2c-master provides main(), the resource manager at /dev/i2c<unit> and the devctl() interface; this file
 * provides the hardware functions it calls through i2c_master_getfuncs(). A send() without a stop condition is kept
 * and sent with the next message as one combined transaction, so DCMD_I2C_SENDRECV becomes a write and a read with a
 * repeated start between them, as on a real bus.
 */
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <hw/i2c.h>
#include <sys/slog2.h>

#include "pb_i2c.h"
#include "pb_vpci.h"

#define DEFAULT_TIMEOUT_MS 1000u
#define ARENA_BYTES        (2u * (PB_I2C_OUT_HDR + 1u + PB_I2C_MAX_LEN) + 64u)

struct i2cv {
    struct pb_vpci v;
    unsigned index;
    uint64_t timeout_ns;
    unsigned verbose;
    unsigned addr;
    unsigned speed;
    unsigned max_segs;      /* requests per transaction the queue can hold, at two descriptors each */
    int zero_len;
    unsigned npending;      /* write segments held for a combined transaction */
    struct pb_i2c_seg pending[PB_I2C_MAX_SEGS];
    uint8_t pending_data[PB_I2C_MAX_SEGS][PB_I2C_MAX_LEN];
};

static void say(const struct i2cv *d, unsigned level, const char *fmt, ...)
{
    if (d->verbose < level) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "i2c-virtio: ");
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static int parse_args(struct i2cv *d, int argc, char *argv[])
{
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "-I") == 0 && i + 1 < argc) {
            d->index = (unsigned)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "-t") == 0 && i + 1 < argc) {
            unsigned long ms = strtoul(argv[++i], NULL, 0);
            if (ms == 0u || ms > 60000u) {
                fprintf(stderr, "i2c-virtio: -t takes 1 to 60000 ms\n");
                return -1;
            }
            d->timeout_ns = (uint64_t)ms * 1000000ull;
        } else if (strcmp(a, "-v") == 0) {
            d->verbose++;
        }
        /* everything else belongs to libi2c-master (--u, --b and the rest) */
    }
    return 0;
}

static void *i2cv_init(int argc, char *argv[])
{
    struct i2cv *d = calloc(1, sizeof(*d));
    if (d == NULL) {
        return NULL;
    }
    d->timeout_ns = (uint64_t)DEFAULT_TIMEOUT_MS * 1000000ull;
    if (parse_args(d, argc, argv) != 0) {
        free(d);
        return NULL;
    }
    if (getenv("PB_TRACE") != NULL) {
        fprintf(stderr, "i2c-virtio: init, %d arguments, index %u\n", argc, d->index);
    }
    char err[160] = "";
    int rc = pb_vpci_open(&d->v, PB_VIRTIO_ID_I2C, d->index, PB_I2C_F_ZERO_LENGTH_REQUEST, 64u, ARENA_BYTES, err,
                          sizeof(err));
    if (rc != EOK) {
        fprintf(stderr, "i2c-virtio: %s\n", err);
        free(d);
        return NULL;
    }
    d->v.verbose = (int)d->verbose;
    d->zero_len = (d->v.features & PB_I2C_F_ZERO_LENGTH_REQUEST) != 0u;
    d->max_segs = d->v.qsize / 2u;
    if (d->max_segs > PB_I2C_MAX_SEGS) {
        d->max_segs = PB_I2C_MAX_SEGS;
    }
    fprintf(stderr, "i2c-virtio: virtio I2C adapter %u at PCI %x:%x.%x, queue %u, irq %d%s, timeout %llu ms\n",
            d->index, PCI_BUS(d->v.bdf), PCI_DEV(d->v.bdf), PCI_FUNC(d->v.bdf), d->v.qsize, d->v.irq,
            d->v.iid >= 0 ? "" : " (polling)", (unsigned long long)(d->timeout_ns / 1000000ull));
    return d;
}

static void i2cv_fini(void *hdl)
{
    struct i2cv *d = hdl;
    pb_vpci_close(&d->v);
    free(d);
}

static int i2cv_version_info(i2c_libversion_t *version)
{
    version->major = I2CLIB_VERSION_MAJOR;
    version->minor = I2CLIB_VERSION_MINOR;
    version->revision = I2CLIB_REVISION;
    return 0;
}

/* Run the held segments plus `last` (when given) as one transaction. */
static i2c_status_t run(struct i2cv *d, const struct pb_i2c_seg *last)
{
    struct pb_i2c_seg segs[PB_I2C_MAX_SEGS];
    unsigned n = d->npending;
    memcpy(segs, d->pending, n * sizeof(segs[0]));
    if (last != NULL) {
        segs[n++] = *last;
    }
    d->npending = 0;
    if (n == 0u) {
        return I2C_STATUS_DONE;
    }
    struct pb_i2c_plan plan;
    if (pb_i2c_plan(segs, n, d->zero_len, d->v.arena, d->v.arena_addr, d->v.arena_len, &plan) != 0) {
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    uint16_t heads[PB_I2C_MAX_SEGS];
    for (unsigned i = 0; i < n; i++) {
        if (pb_vq_add(&d->v.vq, &plan.req[i].out, 1, &plan.req[i].in, 1, &heads[i]) != 0) {
            pb_vpci_restart(&d->v);  /* drop what was added but never published */
            return I2C_STATUS_DONE | I2C_STATUS_ERROR;
        }
    }
    uint32_t used[PB_I2C_MAX_SEGS] = { 0 };
    int rc = pb_vpci_run(&d->v, heads, n, used, d->timeout_ns);
    if (rc == ETIMEDOUT) {
        say(d, 1, "timeout for slave 0x%x: no answer in %llu ms, device reset (%u so far)", segs[0].addr,
            (unsigned long long)(d->timeout_ns / 1000000ull), d->v.resets);
        return I2C_STATUS_DONE | I2C_STATUS_BUSY;
    }
    if (rc != EOK) {
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    int failed = pb_i2c_collect(segs, &plan, d->v.arena, used);
    if (failed >= 0) {
        /* virtio-i2c reports one error status; it does not say whether the address or a data byte was refused. */
        say(d, 2, "VIRTIO_I2C_MSG_ERR for slave 0x%x, message %d of %u", segs[failed].addr, failed + 1, n);
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    return I2C_STATUS_DONE;
}

static i2c_status_t i2cv_send(void *hdl, void *buf, unsigned int len, unsigned int stop)
{
    struct i2cv *d = hdl;
    if (len > PB_I2C_MAX_LEN) {
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    if (!stop) {
        if (d->npending + 1u >= d->max_segs) {
            d->npending = 0;  /* no room left for the message that would end the transaction */
            return I2C_STATUS_DONE | I2C_STATUS_ERROR;
        }
        memcpy(d->pending_data[d->npending], buf, len);
        d->pending[d->npending] = (struct pb_i2c_seg){ .addr = (uint16_t)d->addr, .read = 0, .len = len,
                                                       .wdata = d->pending_data[d->npending] };
        d->npending++;
        return I2C_STATUS_DONE;
    }
    struct pb_i2c_seg s = { .addr = (uint16_t)d->addr, .read = 0, .len = len, .wdata = buf };
    return run(d, &s);
}

static i2c_status_t i2cv_recv(void *hdl, void *buf, unsigned int len, unsigned int stop)
{
    struct i2cv *d = hdl;
    (void)stop;  /* a read ends the transaction either way: the reply buffer has to be filled before returning */
    if (len > PB_I2C_MAX_LEN) {
        d->npending = 0;
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    struct pb_i2c_seg s = { .addr = (uint16_t)d->addr, .read = 1, .len = len, .rdata = buf };
    return run(d, &s);
}

static i2c_status_t i2cv_xfer(void *const hdl, void *const sbuf, const uint32_t slen, void *const rbuf,
                              const uint32_t rlen)
{
    struct i2cv *d = hdl;
    if (slen > PB_I2C_MAX_LEN || rlen > PB_I2C_MAX_LEN) {
        return I2C_STATUS_DONE | I2C_STATUS_ERROR;
    }
    d->npending = 0;
    if (slen != 0u) {
        memcpy(d->pending_data[0], sbuf, slen);
        d->pending[0] = (struct pb_i2c_seg){ .addr = (uint16_t)d->addr, .read = 0, .len = slen,
                                             .wdata = d->pending_data[0] };
        d->npending = 1;
    }
    if (rlen == 0u) {
        if (d->npending == 0u) {
            return run(d, &(struct pb_i2c_seg){ .addr = (uint16_t)d->addr, .read = 0, .len = 0 });
        }
        return run(d, NULL);
    }
    struct pb_i2c_seg s = { .addr = (uint16_t)d->addr, .read = 1, .len = rlen, .rdata = rbuf };
    return run(d, &s);
}

static int i2cv_abort(void *hdl, int rcvid)
{
    struct i2cv *d = hdl;
    (void)rcvid;
    d->npending = 0;
    return 0;
}

static int i2cv_set_slave_addr(void *hdl, unsigned int addr, i2c_addrfmt_t fmt)
{
    struct i2cv *d = hdl;
    if (fmt != I2C_ADDRFMT_7BIT || addr > 0x7Fu) {
        return -1;  /* the virtio I2C request carries a 7-bit address */
    }
    if (addr != d->addr) {
        d->npending = 0;
    }
    d->addr = addr;
    return 0;
}

static int i2cv_set_bus_speed(void *hdl, unsigned int speed, unsigned int *ospeed)
{
    struct i2cv *d = hdl;
    if (speed == 0u) {
        return -1;
    }
    /* virtio-i2c has no clock control: the speed is kept so that it reads back, and has no effect. */
    d->speed = speed;
    if (ospeed != NULL) {
        *ospeed = speed;
    }
    return 0;
}

static int i2cv_driver_info(void *hdl, i2c_driver_info_t *info)
{
    struct i2cv *d = hdl;
    info->speed_mode = I2C_SPEED_STANDARD | I2C_SPEED_FAST | I2C_SPEED_HIGH;
    info->addr_mode = I2C_ADDRFMT_7BIT;
    info->verbosity = d->verbose;
    info->caps = 0;
    return 0;
}

static int i2cv_ctl(void *hdl, int cmd, void *msg, int msglen, int *nbytes, int *info)
{
    (void)hdl;
    (void)cmd;
    (void)msg;
    (void)msglen;
    (void)nbytes;
    (void)info;
    return ENOTTY;
}

static int i2cv_bus_reset(void *hdl)
{
    struct i2cv *d = hdl;
    d->npending = 0;
    return pb_vpci_restart(&d->v) == EOK ? EOK : EIO;
}

int i2c_master_getfuncs(i2c_master_funcs_t *funcs, int tabsize)
{
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, init, i2cv_init, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, fini, i2cv_fini, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, send, i2cv_send, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, recv, i2cv_recv, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, abort, i2cv_abort, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, set_slave_addr, i2cv_set_slave_addr, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, set_bus_speed, i2cv_set_bus_speed, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, driver_info, i2cv_driver_info, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, ctl, i2cv_ctl, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, bus_reset, i2cv_bus_reset, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, version_info, i2cv_version_info, tabsize);
    I2C_ADD_FUNC(i2c_master_funcs_t, funcs, xfer, i2cv_xfer, tabsize);
    return 0;
}
