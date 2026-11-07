#include "pb_vpci.h"

#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/mman.h>
#include <sys/neutrino.h>
#include <sys/siginfo.h>

/* PCI vendor capability for virtio (virtio 1.2, 4.1.4) */
#define CAP_ID_VNDR        0x09u
#define CAP_COMMON         1u
#define CAP_NOTIFY         2u
#define CAP_ISR            3u
#define CAP_DEVICE         4u

/* struct virtio_pci_common_cfg (virtio 1.2, 4.1.4.3) */
#define CC_DFSELECT        0u
#define CC_DF              4u
#define CC_GFSELECT        8u
#define CC_GF              12u
#define CC_MSIX            16u
#define CC_NUMQ            18u
#define CC_STATUS          20u
#define CC_GENERATION      21u
#define CC_Q_SELECT        22u
#define CC_Q_SIZE          24u
#define CC_Q_MSIX          26u
#define CC_Q_ENABLE        28u
#define CC_Q_NOFF          30u
#define CC_Q_DESC          32u
#define CC_Q_DRIVER        40u
#define CC_Q_DEVICE        48u

/* device status (virtio 1.2, 2.1) */
#define S_ACKNOWLEDGE      1u
#define S_DRIVER           2u
#define S_DRIVER_OK        4u
#define S_FEATURES_OK      8u
#define S_NEEDS_RESET      0x40u
#define S_FAILED           0x80u

#define NO_VECTOR          0xFFFFu
#define PULSE_IRQ          (_PULSE_CODE_MINAVAIL + 1)
#define PULSE_STOP         (_PULSE_CODE_MINAVAIL + 2)
#define IRQ_PRIORITY       21

static void seterr(char *buf, size_t len, const char *fmt, ...)
{
    if (buf == NULL || len == 0u) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, len, fmt, ap);
    va_end(ap);
}

/* PB_TRACE=1 in the environment prints each step of bringing the device up, on stderr. */
static void trace(const char *fmt, ...)
{
    static int on = -1;
    if (on < 0) {
        const char *e = getenv("PB_TRACE");
        on = e != NULL && *e != '\0' && *e != '0';
    }
    if (!on) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "pb_vpci: ");
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static inline uint8_t r8(volatile uint8_t *b, unsigned o) { return *(volatile uint8_t *)(b + o); }
static inline uint16_t r16(volatile uint8_t *b, unsigned o) { return *(volatile uint16_t *)(b + o); }
static inline uint32_t r32(volatile uint8_t *b, unsigned o) { return *(volatile uint32_t *)(b + o); }
static inline void w8(volatile uint8_t *b, unsigned o, uint8_t v) { *(volatile uint8_t *)(b + o) = v; }
static inline void w16(volatile uint8_t *b, unsigned o, uint16_t v) { *(volatile uint16_t *)(b + o) = v; }
static inline void w32(volatile uint8_t *b, unsigned o, uint32_t v) { *(volatile uint32_t *)(b + o) = v; }
static inline void w64(volatile uint8_t *b, unsigned o, uint64_t v)
{
    w32(b, o, (uint32_t)v);
    w32(b, o + 4u, (uint32_t)(v >> 32));
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void sleep_us(unsigned us)
{
    struct timespec ts = { .tv_sec = 0, .tv_nsec = (long)us * 1000L };
    nanosleep(&ts, NULL);
}

static volatile uint8_t *map_bar(struct pb_vpci *v, unsigned bar, char *err, size_t errlen)
{
    if (bar > 5u) {
        seterr(err, errlen, "capability names BAR %u", bar);
        return NULL;
    }
    if (v->bar_map[bar] != NULL) {
        return v->bar_map[bar];
    }
    pci_ba_t ba[7];
    int_t nba = 7;
    pci_err_t pe = pci_device_read_ba(v->hdl, &nba, ba, pci_reqType_e_UNSPECIFIED);
    if (pe != PCI_ERR_OK) {
        seterr(err, errlen, "pci_device_read_ba: %s", pci_strerror(pe));
        return NULL;
    }
    for (int_t i = 0; i < nba; i++) {
        if (ba[i].bar_num != (int_t)bar) {
            continue;
        }
        if (ba[i].type != pci_asType_e_MEM) {
            seterr(err, errlen, "BAR %u is not memory space", bar);
            return NULL;
        }
        pci_ba_t xl;
        pe = pci_device_map_as(v->hdl, &ba[i], &xl);
        if (pe != PCI_ERR_OK) {
            seterr(err, errlen, "pci_device_map_as: %s", pci_strerror(pe));
            return NULL;
        }
        void *p = mmap_device_memory(NULL, (size_t)xl.size, PROT_READ | PROT_WRITE | PROT_NOCACHE, 0, xl.addr);
        if (p == MAP_FAILED) {
            seterr(err, errlen, "mmap_device_memory BAR %u: %s", bar, strerror(errno));
            return NULL;
        }
        v->bar_map[bar] = p;
        v->bar_len[bar] = (size_t)xl.size;
        trace("BAR %u at %#llx, %llu bytes, mapped", bar, (unsigned long long)xl.addr, (unsigned long long)xl.size);
        return p;
    }
    seterr(err, errlen, "BAR %u not assigned", bar);
    return NULL;
}

static int find_caps(struct pb_vpci *v, char *err, size_t errlen)
{
    uint8_t ptr = 0;
    uint32_t notify_mult = 0;
    uint16_t status = 0;
    pci_device_cfg_rd16(v->bdf, 0x06, &status);
    if ((status & 0x10u) == 0u) {
        seterr(err, errlen, "no PCI capability list");
        return ENODEV;
    }
    pci_device_cfg_rd8(v->bdf, 0x34, &ptr);
    trace("status %#x, first capability at %#x", status, ptr);
    for (int guard = 0; ptr >= 0x40u && guard < 48; guard++) {
        uint8_t id = 0, next = 0, type = 0, bar = 0;
        uint32_t off = 0, len = 0;
        pci_device_cfg_rd8(v->bdf, ptr, &id);
        pci_device_cfg_rd8(v->bdf, ptr + 1u, &next);
        trace("capability %#x at %#x", id, ptr);
        if (id == CAP_ID_VNDR) {
            pci_device_cfg_rd8(v->bdf, ptr + 3u, &type);
            pci_device_cfg_rd8(v->bdf, ptr + 4u, &bar);
            pci_device_cfg_rd32(v->bdf, ptr + 8u, &off);
            pci_device_cfg_rd32(v->bdf, ptr + 12u, &len);
            volatile uint8_t *base = NULL;
            if (type >= CAP_COMMON && type <= CAP_DEVICE) {
                base = map_bar(v, bar, err, errlen);
                if (base == NULL) {
                    return ENXIO;
                }
                if ((size_t)off + len > v->bar_len[bar]) {
                    seterr(err, errlen, "capability %u runs past BAR %u", type, bar);
                    return ENXIO;
                }
            }
            switch (type) {
            case CAP_COMMON:
                if (v->common == NULL) v->common = base + off;
                break;
            case CAP_NOTIFY:
                if (v->notify_base == NULL) {
                    pci_device_cfg_rd32(v->bdf, ptr + 16u, &notify_mult);
                    v->notify_base = base + off;
                    v->notify_mult = notify_mult;
                }
                break;
            case CAP_ISR:
                if (v->isr == NULL) v->isr = base + off;
                break;
            case CAP_DEVICE:
                if (v->devcfg == NULL) {
                    v->devcfg = base + off;
                    v->devcfg_len = len;
                }
                break;
            default:
                break;
            }
        }
        ptr = next;
    }
    if (v->common == NULL || v->notify_base == NULL || v->isr == NULL) {
        seterr(err, errlen, "virtio common, notify or ISR capability missing (a legacy-only device?)");
        return ENODEV;
    }
    return EOK;
}

static int wait_reset(struct pb_vpci *v)
{
    w8(v->common, CC_STATUS, 0);
    for (int i = 0; i < 1000; i++) {
        if (r8(v->common, CC_STATUS) == 0u) {
            return EOK;
        }
        sleep_us(100);
    }
    return EIO;
}

static int bring_up(struct pb_vpci *v, uint16_t max_qsize, char *err, size_t errlen)
{
    if (wait_reset(v) != EOK) {
        seterr(err, errlen, "device did not reset");
        return EIO;
    }
    uint8_t st = S_ACKNOWLEDGE;
    w8(v->common, CC_STATUS, st);
    st |= S_DRIVER;
    w8(v->common, CC_STATUS, st);

    w32(v->common, CC_DFSELECT, 0);
    uint64_t lo = r32(v->common, CC_DF);
    w32(v->common, CC_DFSELECT, 1);
    uint64_t hi = r32(v->common, CC_DF);
    v->offered = lo | hi << 32;
    if ((v->offered & PB_VIRTIO_F_VERSION_1) == 0u) {
        w8(v->common, CC_STATUS, S_FAILED);
        seterr(err, errlen, "device does not offer VIRTIO_F_VERSION_1");
        return ENOTSUP;
    }
    v->features = PB_VIRTIO_F_VERSION_1 | (v->offered & v->wanted);
    w32(v->common, CC_GFSELECT, 0);
    w32(v->common, CC_GF, (uint32_t)v->features);
    w32(v->common, CC_GFSELECT, 1);
    w32(v->common, CC_GF, (uint32_t)(v->features >> 32));
    st |= S_FEATURES_OK;
    w8(v->common, CC_STATUS, st);
    if ((r8(v->common, CC_STATUS) & S_FEATURES_OK) == 0u) {
        w8(v->common, CC_STATUS, S_FAILED);
        seterr(err, errlen, "device refused features %#llx", (unsigned long long)v->features);
        return ENOTSUP;
    }

    w16(v->common, CC_Q_SELECT, 0);
    uint16_t dev_size = r16(v->common, CC_Q_SIZE);
    if (dev_size == 0u) {
        seterr(err, errlen, "queue 0 does not exist");
        return ENODEV;
    }
    uint16_t qs = dev_size < max_qsize ? dev_size : max_qsize;
    while (qs & (qs - 1u)) {
        qs &= (uint16_t)(qs - 1u);  /* largest power of 2 below */
    }
    if (v->vq.size == 0u) {
        if (pb_vq_init(&v->vq, v->dma, v->dma_addr, qs) != 0) {
            seterr(err, errlen, "queue of %u entries", qs);
            return EINVAL;
        }
    } else {
        pb_vq_reset(&v->vq);
    }
    w16(v->common, CC_Q_SIZE, v->vq.size);
    w16(v->common, CC_Q_MSIX, NO_VECTOR);
    w64(v->common, CC_Q_DESC, v->vq.desc_addr);
    w64(v->common, CC_Q_DRIVER, v->vq.avail_addr);
    w64(v->common, CC_Q_DEVICE, v->vq.used_addr);
    w16(v->common, CC_Q_ENABLE, 1);
    w16(v->common, CC_MSIX, NO_VECTOR);
    v->notify = v->notify_base + (size_t)r16(v->common, CC_Q_NOFF) * v->notify_mult;
    st |= S_DRIVER_OK;
    w8(v->common, CC_STATUS, st);
    return EOK;
}

/* A waiting transfer wakes on its interrupt or at its deadline; the used ring is read once more at the deadline, so a
 * lost interrupt costs one deadline, never a hang. PB_RECHECK_MS=N also re-reads the ring every N ms, for diagnosing
 * interrupt delivery; it is off by default. */
static uint64_t recheck_ns(void)
{
    static int64_t ns = -1;
    if (ns < 0) {
        const char *e = getenv("PB_RECHECK_MS");
        ns = (int64_t)(e != NULL ? strtoul(e, NULL, 0) : 0ul) * 1000000;
    }
    return (uint64_t)ns;
}

static void *irq_loop(void *arg)
{
    struct pb_vpci *v = arg;
    for (;;) {
        struct _pulse pulse;
        if (MsgReceivePulse(v->chid, &pulse, sizeof(pulse), NULL) == -1) {
            continue;
        }
        if (pulse.code == PULSE_STOP) {
            return NULL;
        }
        if (pulse.code != PULSE_IRQ) {
            continue;
        }
        uint8_t isr = r8(v->isr, 0);  /* reading the ISR status deasserts this device's line */
        InterruptUnmask(v->irq, v->iid);  /* at once: the line may be shared with another driver */
        trace("irq %d pulse, ISR %#x", v->irq, isr);
        pthread_mutex_lock(&v->lock);
        v->irq_count++;
        pthread_cond_broadcast(&v->cond);
        pthread_mutex_unlock(&v->lock);
    }
}

static int start_irq(struct pb_vpci *v)
{
    pthread_condattr_t ca;
    pthread_condattr_init(&ca);
    pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
    pthread_cond_init(&v->cond, &ca);
    pthread_condattr_destroy(&ca);
    pthread_mutex_init(&v->lock, NULL);
    v->chid = ChannelCreate(_NTO_CHF_PRIVATE);
    v->coid = v->chid >= 0 ? ConnectAttach(0, 0, v->chid, _NTO_SIDE_CHANNEL, 0) : -1;
    int_t nirq = 1;
    pci_irq_t irq = -1;
    if (v->coid < 0 || pci_device_read_irq(v->hdl, &nirq, &irq) != PCI_ERR_OK || nirq < 1 || irq < 0) {
        return -1;
    }
    v->irq = irq;
    uint8_t line = 0xFF, pin = 0;
    pci_device_cfg_rd8(v->bdf, 0x3C, &line);
    pci_device_cfg_rd8(v->bdf, 0x3D, &pin);
    trace("PCI server irq %d; config space interrupt line %u, pin %u", irq, line, pin);
    /* On QEMU's pc machine the PCI server reported IRQ 10 for both Patchbay controllers, while the firmware had routed
     * the one in slot 3 to IRQ 11 and written 11 in its Interrupt Line register: its interrupts never reached IRQ 10,
     * and a transfer was only seen when another device's interrupt on 10 woke this driver (docs/RESULTS.md). For a
     * legacy INTx line on x86 the register is what the firmware actually programmed, so it wins when they differ. */
#if defined(__X86_64__) || defined(__x86_64__)
    if (pin != 0u && line != 0u && line < 0xFFu && (int)line != irq) {
        fprintf(stderr, "pb_vpci: PCI server says IRQ %d, Interrupt Line register says %u: using %u\n", irq, line,
                line);
        v->irq = line;
    }
#endif
    if (pthread_create(&v->irq_thread, NULL, irq_loop, v) != EOK) {
        return -1;
    }
    v->irq_thread_up = 1;
    pthread_setname_np(v->irq_thread, "virtio-irq");
    struct sigevent ev;
    SIGEV_PULSE_INIT(&ev, v->coid, IRQ_PRIORITY, PULSE_IRQ, 0);
    v->iid = InterruptAttachEvent(v->irq, &ev, _NTO_INTR_FLAGS_TRK_MSK);
    trace("irq %d attached as %d", v->irq, v->iid);
    return v->iid >= 0 ? 0 : -1;
}

static void stop_irq(struct pb_vpci *v)
{
    if (v->iid >= 0) {
        InterruptDetach(v->iid);
        v->iid = -1;
    }
    if (v->irq_thread_up) {
        MsgSendPulse(v->coid, IRQ_PRIORITY, PULSE_STOP, 0);
        pthread_join(v->irq_thread, NULL);
        v->irq_thread_up = 0;
    }
    if (v->coid >= 0) {
        ConnectDetach(v->coid);
        v->coid = -1;
    }
    if (v->chid >= 0) {
        ChannelDestroy(v->chid);
        v->chid = -1;
    }
}

int pb_vpci_open(struct pb_vpci *v, uint16_t virtio_id, unsigned index, uint64_t wanted, uint16_t max_qsize,
                 size_t arena_len, char *err, size_t errlen)
{
    memset(v, 0, sizeof(*v));
    v->iid = -1;
    v->chid = -1;
    v->coid = -1;
    v->wanted = wanted;
    /* libpci's x86 module reads configuration space with port I/O inside an interrupts-off section, which needs I/O
     * privilege (PROCMGR_AID_IO). Threads created later, such as io-spi's bus threads, inherit it. */
    if (ThreadCtl(_NTO_TCTL_IO_LEVEL, (void *)(uintptr_t)(_NTO_IO_LEVEL_1 | _NTO_TCTL_IO_LEVEL_INHERIT)) == -1) {
        seterr(err, errlen, "no I/O privilege: %s", strerror(errno));
        return EPERM;
    }
    /* Modern virtio PCI device IDs are 0x1040 + the virtio device ID (virtio 1.2, 4.1.2.1). */
    v->bdf = pci_device_find(index, 0x1AF4u, (pci_did_t)(0x1040u + virtio_id), PCI_CCODE_ANY);
    if (v->bdf == PCI_BDF_NONE) {
        seterr(err, errlen, "no virtio device %u (PCI 1af4:%04x) at index %u", virtio_id, 0x1040u + virtio_id, index);
        return ENODEV;
    }
    pci_err_t pe;
    v->hdl = pci_device_attach(v->bdf, pci_attachFlags_EXCLUSIVE_OWNER, &pe);
    if (v->hdl == NULL) {
        seterr(err, errlen, "pci_device_attach: %s", pci_strerror(pe));
        return EBUSY;
    }
    trace("attached to %x:%x.%x", PCI_BUS(v->bdf), PCI_DEV(v->bdf), PCI_FUNC(v->bdf));
    pci_cmd_t cmd = 0;
    pci_device_read_cmd(v->bdf, &cmd);
    trace("command register %#x", cmd);
    pci_device_write_cmd(v->hdl, (pci_cmd_t)(cmd | 0x0006u), &cmd);  /* memory space and bus master */
    trace("command register now %#x", cmd);

    int rc = find_caps(v, err, errlen);
    if (rc != EOK) {
        pb_vpci_close(v);
        return rc;
    }
    size_t ring = pb_vq_layout(PB_VQ_MAX, NULL, NULL);
    ring = (ring + 4095u) & ~(size_t)4095u;
    v->dma_len = ring + ((arena_len + 4095u) & ~(size_t)4095u);
    void *p = mmap(NULL, v->dma_len, PROT_READ | PROT_WRITE, MAP_PHYS | MAP_ANON | MAP_SHARED, NOFD, 0);
    if (p == MAP_FAILED) {
        seterr(err, errlen, "DMA allocation of %zu bytes: %s", v->dma_len, strerror(errno));
        pb_vpci_close(v);
        return ENOMEM;
    }
    off64_t pa = 0;
    size_t contig = 0;
    if (mem_offset64(p, NOFD, v->dma_len, &pa, &contig) != 0 || contig < v->dma_len) {
        seterr(err, errlen, "DMA block is not physically contiguous");
        munmap(p, v->dma_len);
        pb_vpci_close(v);
        return ENOMEM;
    }
    v->dma = p;
    trace("DMA block %zu bytes at %#llx", v->dma_len, (unsigned long long)pa);
    v->dma_addr = (uint64_t)pa;
    v->arena = v->dma + ring;
    v->arena_addr = v->dma_addr + ring;
    v->arena_len = v->dma_len - ring;

    rc = bring_up(v, max_qsize, err, errlen);
    if (rc != EOK) {
        pb_vpci_close(v);
        return rc;
    }
    v->qsize = v->vq.size;
    trace("device up: features %#llx, queue %u", (unsigned long long)v->features, v->qsize);
    if (start_irq(v) != 0) {
        stop_irq(v);  /* no interrupt: pb_vpci_run polls */
    }
    return EOK;
}

int pb_vpci_restart(struct pb_vpci *v)
{
    char err[128];
    v->resets++;
    return bring_up(v, v->vq.size, err, sizeof(err)) == EOK ? EOK : EIO;
}

int pb_vpci_read_config(struct pb_vpci *v, uint32_t offset, void *buf, uint32_t len)
{
    if (v->devcfg == NULL || (uint64_t)offset + len > v->devcfg_len) {
        return EIO;
    }
    for (int tries = 0; tries < 8; tries++) {
        uint8_t gen = r8(v->common, CC_GENERATION);
        for (uint32_t i = 0; i < len; i++) {
            ((uint8_t *)buf)[i] = r8(v->devcfg, offset + i);
        }
        if (r8(v->common, CC_GENERATION) == gen) {
            return EOK;
        }
    }
    return EIO;
}

int pb_vpci_run(struct pb_vpci *v, const uint16_t *heads, unsigned n, uint32_t *used, uint64_t timeout_ns)
{
    uint8_t done[PB_VQ_MAX] = { 0 };
    unsigned remaining = n;
    pb_vq_publish(&v->vq);
    if (pb_vq_kick_needed(&v->vq)) {
        w16(v->notify, 0, 0);  /* queue index 0 */
    }
    uint64_t deadline = now_ns() + timeout_ns;
    for (;;) {
        uint16_t h;
        uint32_t len;
        int r;
        unsigned seen = 0;
        if (v->iid >= 0) {
            pthread_mutex_lock(&v->lock);
            seen = v->irq_count;  /* taken before the ring is read, so no interrupt is missed */
            pthread_mutex_unlock(&v->lock);
        }
        while ((r = pb_vq_get_used(&v->vq, &h, &len)) > 0) {
            for (unsigned i = 0; i < n; i++) {
                if (!done[i] && heads[i] == h) {
                    done[i] = 1;
                    used[i] = len;
                    remaining--;
                    break;
                }
            }
        }
        if (r < 0) {
            pb_vpci_restart(v);
            return EIO;
        }
        if (remaining == 0u) {
            return EOK;
        }
        if (r8(v->common, CC_STATUS) & S_NEEDS_RESET) {
            pb_vpci_restart(v);
            return EIO;
        }
        uint64_t t = now_ns();
        if (t >= deadline) {
            v->timeouts++;
            pb_vpci_restart(v);
            return ETIMEDOUT;
        }
        uint64_t left = deadline - t;
        if (v->iid >= 0) {
            uint64_t step = recheck_ns();
            uint64_t wake = step != 0u && t + step < deadline ? t + step : deadline;
            struct timespec abs = { .tv_sec = (time_t)(wake / 1000000000ull),
                                    .tv_nsec = (long)(wake % 1000000000ull) };
            pthread_mutex_lock(&v->lock);
            while (v->irq_count == seen) {
                if (pthread_cond_timedwait(&v->cond, &v->lock, &abs) == ETIMEDOUT) {
                    break;
                }
            }
            pthread_mutex_unlock(&v->lock);
        } else {
            sleep_us(left > 200000ull ? 200u : (unsigned)(left / 1000u) + 1u);
        }
    }
}

void pb_vpci_close(struct pb_vpci *v)
{
    if (v->common != NULL) {
        (void)wait_reset(v);
    }
    stop_irq(v);
    if (v->dma != NULL) {
        munmap(v->dma, v->dma_len);
        v->dma = NULL;
    }
    for (int i = 0; i < 6; i++) {
        if (v->bar_map[i] != NULL) {
            munmap_device_memory(v->bar_map[i], v->bar_len[i]);
            v->bar_map[i] = NULL;
        }
    }
    if (v->hdl != NULL) {
        pci_device_detach(v->hdl);
        v->hdl = NULL;
    }
}
