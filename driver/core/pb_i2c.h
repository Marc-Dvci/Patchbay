/*
 * virtio-i2c request framing (virtio 1.2, section 5.19). Pure C.
 *
 * Each I2C message becomes one request in two descriptors, laid out in a DMA arena:
 *   device-readable   struct virtio_i2c_out_hdr { le16 addr; le16 padding; le32 flags; } followed by the write data
 *   device-writable   the read data, followed by struct virtio_i2c_in_hdr { u8 status; }
 * A device must not assume where descriptor boundaries fall (virtio 1.2, 2.6.4), so two descriptors per request is
 * enough, and a write-then-read transaction fits the 4-entry queue QEMU's vhost-user-i2c-pci gives.
 * Every request of a transaction except the last carries VIRTIO_I2C_FLAGS_FAIL_NEXT, so the device runs them as one
 * combined transaction and fails the rest if one fails. addr holds the 7-bit address shifted left by one.
 */
#ifndef PB_I2C_H
#define PB_I2C_H

#include <stddef.h>
#include <stdint.h>

#include "pb_vring.h"

#define PB_VIRTIO_ID_I2C            34u
#define PB_I2C_F_ZERO_LENGTH_REQUEST (1ull << 0)
#define PB_I2C_FLAGS_FAIL_NEXT      (1u << 0)
#define PB_I2C_FLAGS_M_RD           (1u << 1)
#define PB_I2C_MSG_OK               0u
#define PB_I2C_MSG_ERR              1u
#define PB_I2C_OUT_HDR              8u
#define PB_I2C_MAX_SEGS             8u
#define PB_I2C_MAX_LEN              4096u

struct pb_i2c_seg {
    uint16_t addr;   /* 7-bit */
    uint8_t read;
    uint32_t len;
    const void *wdata;   /* len bytes to write, for a write */
    void *rdata;         /* len bytes to fill, for a read */
};

struct pb_i2c_req {
    struct pb_buf out;
    struct pb_buf in;
    size_t out_off, in_off;  /* where each buffer sits in the arena */
};

struct pb_i2c_plan {
    unsigned n;
    struct pb_i2c_req req[PB_I2C_MAX_SEGS];
};

/*
 * Lay out n segments in the arena (cpu pointer, DMA address, length): headers and write data are copied in, read
 * areas reserved. Returns 0, -EINVAL (bad address, zero-length segment without zero_len_ok, length over
 * PB_I2C_MAX_LEN, more than PB_I2C_MAX_SEGS) or -ENOBUFS when the arena is too small.
 */
int pb_i2c_plan(const struct pb_i2c_seg *segs, unsigned n, int zero_len_ok, uint8_t *arena, uint64_t arena_addr,
                size_t arena_len, struct pb_i2c_plan *plan);

/*
 * After the device completed every request: copy read data out of the arena. used[i] is the length the device
 * reported for request i. Returns -1 when every request succeeded, else the index of the first one that failed (its
 * status byte is not OK, or the device wrote less than the request needs).
 */
int pb_i2c_collect(const struct pb_i2c_seg *segs, const struct pb_i2c_plan *plan, const uint8_t *arena,
                   const uint32_t *used);

#endif
