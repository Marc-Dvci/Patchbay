/*
 * virtio over PCI, modern interface (virtio 1.2, section 4.1), for QNX 8.0 drivers.
 *
 * Finds the device through the PCI server (libpci), maps the common, notify, ISR and device configuration structures
 * named by its vendor capabilities, allocates one physically contiguous DMA block for the virtqueue and the request
 * arena, and waits for completions on an interrupt pulse with a deadline. Every wait ends: on completion, on the
 * deadline, or on a device that has set DEVICE_NEEDS_RESET.
 */
#ifndef PB_VPCI_H
#define PB_VPCI_H

#include <stddef.h>
#include <stdint.h>

#include <pci/pci.h>

#include "pb_vring.h"

#define PB_VIRTIO_F_VERSION_1 (1ull << 32)

struct pb_vpci {
    pci_bdf_t bdf;
    pci_devhdl_t hdl;
    volatile uint8_t *common;
    volatile uint8_t *notify_base;
    uint32_t notify_mult;
    volatile uint8_t *notify;     /* this queue's doorbell */
    volatile uint8_t *isr;
    volatile uint8_t *devcfg;
    uint32_t devcfg_len;
    void *bar_map[6];
    size_t bar_len[6];
    int irq;
    int iid;            /* interrupt id, -1 when polling */
    int chid, coid;
    uint64_t offered;   /* features the device offered */
    uint64_t features;  /* features negotiated */
    uint64_t wanted;    /* features the driver asks for, beyond VERSION_1 */
    uint16_t qsize;
    struct pb_vq vq;
    uint8_t *dma;       /* ring block, then arena */
    uint64_t dma_addr;
    size_t dma_len;
    uint8_t *arena;
    uint64_t arena_addr;
    size_t arena_len;
    unsigned resets;    /* device resets done to recover */
    unsigned timeouts;
    int verbose;
};

/*
 * Attach to the index-th virtio device with this virtio device ID, negotiate VERSION_1 plus `wanted` (when offered),
 * set up queue 0 with up to max_qsize entries and an arena of arena_len bytes. 0 or an errno; errbuf says why.
 */
int pb_vpci_open(struct pb_vpci *v, uint16_t virtio_id, unsigned index, uint64_t wanted, uint16_t max_qsize,
                 size_t arena_len, char *errbuf, size_t errlen);

/* Reset the device and bring it back with the same features and memory: the recovery after a timeout. 0 or EIO. */
int pb_vpci_restart(struct pb_vpci *v);

/* Read len bytes of device configuration at offset, consistent across config_generation. 0 or EIO. */
int pb_vpci_read_config(struct pb_vpci *v, uint32_t offset, void *buf, uint32_t len);

/*
 * Make n already-added requests visible, notify the device, and wait for all of them. heads[i] is each request's
 * head; used[i] gets the length the device wrote. 0 when all completed; ETIMEDOUT when the deadline passed (the
 * device is then reset, so no descriptor stays with it); EIO when the device misbehaved or needs a reset.
 */
int pb_vpci_run(struct pb_vpci *v, const uint16_t *heads, unsigned n, uint32_t *used, uint64_t timeout_ns);

void pb_vpci_close(struct pb_vpci *v);

#endif
