#include "pb_i2c.h"

#include <errno.h>
#include <string.h>

#include "pb_le.h"

int pb_i2c_plan(const struct pb_i2c_seg *segs, unsigned n, int zero_len_ok, uint8_t *arena, uint64_t arena_addr,
                size_t arena_len, struct pb_i2c_plan *plan)
{
    if (segs == NULL || plan == NULL || arena == NULL || n == 0u || n > PB_I2C_MAX_SEGS) {
        return -EINVAL;
    }
    size_t need = 0;
    for (unsigned i = 0; i < n; i++) {
        const struct pb_i2c_seg *s = &segs[i];
        if (s->addr > 0x7Fu || s->len > PB_I2C_MAX_LEN || (s->len == 0u && !zero_len_ok)) {
            return -EINVAL;
        }
        if (s->len != 0u && ((s->read && s->rdata == NULL) || (!s->read && s->wdata == NULL))) {
            return -EINVAL;
        }
        uint32_t out_len = PB_I2C_OUT_HDR + (s->read ? 0u : s->len);
        uint32_t in_len = 1u + (s->read ? s->len : 0u);
        need = (need + out_len + 7u) & ~(size_t)7u;
        need = (need + in_len + 7u) & ~(size_t)7u;
    }
    if (need > arena_len) {
        return -ENOBUFS;
    }
    size_t off = 0;
    plan->n = n;
    for (unsigned i = 0; i < n; i++) {
        const struct pb_i2c_seg *s = &segs[i];
        struct pb_i2c_req *r = &plan->req[i];
        uint32_t flags = (i + 1u < n ? PB_I2C_FLAGS_FAIL_NEXT : 0u) | (s->read ? PB_I2C_FLAGS_M_RD : 0u);
        uint8_t *h = arena + off;
        uint16_t addr = pb_le16((uint16_t)(s->addr << 1));
        uint16_t pad = 0;
        uint32_t fl = pb_le32(flags);
        memcpy(h, &addr, 2);
        memcpy(h + 2, &pad, 2);
        memcpy(h + 4, &fl, 4);
        uint32_t out_len = PB_I2C_OUT_HDR;
        if (!s->read && s->len != 0u) {
            memcpy(h + PB_I2C_OUT_HDR, s->wdata, s->len);
            out_len += s->len;
        }
        r->out_off = off;
        r->out.addr = arena_addr + off;
        r->out.len = out_len;
        off = (off + out_len + 7u) & ~(size_t)7u;
        uint32_t in_len = 1u + (s->read ? s->len : 0u);
        memset(arena + off, 0xFF, in_len);  /* a status the device never wrote reads as an error */
        r->in_off = off;
        r->in.addr = arena_addr + off;
        r->in.len = in_len;
        off = (off + in_len + 7u) & ~(size_t)7u;
    }
    return 0;
}

int pb_i2c_collect(const struct pb_i2c_seg *segs, const struct pb_i2c_plan *plan, const uint8_t *arena,
                   const uint32_t *used)
{
    for (unsigned i = 0; i < plan->n; i++) {
        const struct pb_i2c_req *r = &plan->req[i];
        uint8_t status = arena[r->in_off + r->in.len - 1u];
        if (used[i] < r->in.len || status != PB_I2C_MSG_OK) {
            return (int)i;
        }
        if (segs[i].read && segs[i].len != 0u) {
            memcpy(segs[i].rdata, arena + r->in_off, segs[i].len);
        }
    }
    return -1;
}
