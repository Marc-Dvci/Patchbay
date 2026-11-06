/*
 * The driver's half of a split virtqueue (virtio 1.2, section 2.7). Pure C: no QNX header, so it builds and is tested
 * on the host.
 *
 * Layout in one contiguous block of DMA memory, little-endian:
 *   descriptor table   size x { le64 addr; le32 len; le16 flags; le16 next; }       at offset 0, 16-byte aligned
 *   available ring     le16 flags; le16 idx; le16 ring[size]; le16 used_event      2-byte aligned
 *   used ring          le16 flags; le16 idx; { le32 id; le32 len; }[size]; le16 avail_event   4-byte aligned
 *
 * One request is one chain: device-readable buffers first, then device-writable ones. The driver never negotiates
 * VIRTIO_RING_F_EVENT_IDX or VIRTIO_RING_F_INDIRECT_DESC, so used_event and avail_event are never read.
 */
#ifndef PB_VRING_H
#define PB_VRING_H

#include <stddef.h>
#include <stdint.h>

#define PB_VQ_MAX           256u
#define PB_DESC_F_NEXT      1u
#define PB_DESC_F_WRITE     2u
#define PB_AVAIL_F_NO_INTERRUPT 1u
#define PB_USED_F_NO_NOTIFY 1u

struct pb_desc {
    uint64_t addr;
    uint32_t len;
    uint16_t flags;
    uint16_t next;
};

struct pb_used_elem {
    uint32_t id;
    uint32_t len;
};

/* A buffer as the device sees it: its DMA (guest-physical) address and length. */
struct pb_buf {
    uint64_t addr;
    uint32_t len;
};

struct pb_vq {
    uint16_t size;
    uint16_t free_head;
    uint16_t num_free;
    uint16_t avail_idx;  /* the driver's copy, published by pb_vq_publish() */
    uint16_t last_used;
    volatile struct pb_desc *desc;
    volatile uint16_t *avail;   /* [0] flags, [1] idx, [2 + i] ring */
    volatile uint8_t *used;     /* flags, idx, elements */
    uint64_t desc_addr, avail_addr, used_addr;  /* the same three, as the device addresses them */
    uint16_t chain_len[PB_VQ_MAX];  /* 0 for a free head */
};

/* Bytes the block needs, and where the available and used rings start in it. */
size_t pb_vq_layout(uint16_t size, size_t *avail_off, size_t *used_off);

/* mem is at least pb_vq_layout(size) bytes at DMA address addr. size is a power of 2 up to PB_VQ_MAX. 0 or -EINVAL. */
int pb_vq_init(struct pb_vq *q, void *mem, uint64_t addr, uint16_t size);

/* Forget every request in flight: after a device reset. The memory is zeroed and relinked. */
void pb_vq_reset(struct pb_vq *q);

/*
 * Queue one request with nout device-readable then nin device-writable buffers. On success the head is stored and 0
 * returned; the device sees it only after pb_vq_publish(). -ENOSPC when too few descriptors are free, -EINVAL for an
 * empty request or a zero-length buffer.
 */
int pb_vq_add(struct pb_vq *q, const struct pb_buf *out, unsigned nout, const struct pb_buf *in, unsigned nin,
              uint16_t *head);

/* Make every added request visible to the device. A release fence orders the descriptors before the index. */
void pb_vq_publish(struct pb_vq *q);

/* Whether the device asked not to be notified (VIRTQ_USED_F_NO_NOTIFY). */
int pb_vq_kick_needed(const struct pb_vq *q);

/*
 * Take one completed request: 1 with its head and the byte count the device wrote, 0 when there is none, -EIO when
 * the device returned a head that is not in flight. That request's descriptors go back to the free list.
 */
int pb_vq_get_used(struct pb_vq *q, uint16_t *head, uint32_t *len);

/* Ask the device not to interrupt (1) or to interrupt again (0). */
void pb_vq_set_no_interrupt(struct pb_vq *q, int on);

#endif
