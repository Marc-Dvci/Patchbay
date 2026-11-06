#include "pb_vring.h"

#include <errno.h>
#include <string.h>

#include "pb_le.h"

static size_t align_up(size_t v, size_t a)
{
    return (v + a - 1u) & ~(a - 1u);
}

size_t pb_vq_layout(uint16_t size, size_t *avail_off, size_t *used_off)
{
    size_t a = (size_t)16u * size;
    size_t u = align_up(a + 6u + 2u * (size_t)size, 4u);
    if (avail_off != NULL) {
        *avail_off = a;
    }
    if (used_off != NULL) {
        *used_off = u;
    }
    return align_up(u + 6u + 8u * (size_t)size, 16u);
}

static void relink(struct pb_vq *q)
{
    size_t bytes = pb_vq_layout(q->size, NULL, NULL);
    memset((void *)q->desc, 0, bytes);
    for (uint16_t i = 0; i < q->size; i++) {
        q->desc[i].next = pb_le16((uint16_t)(i + 1u));
        q->chain_len[i] = 0;
    }
    q->free_head = 0;
    q->num_free = q->size;
    q->avail_idx = 0;
    q->last_used = 0;
}

int pb_vq_init(struct pb_vq *q, void *mem, uint64_t addr, uint16_t size)
{
    size_t avail_off, used_off;
    if (q == NULL || mem == NULL || size == 0u || size > PB_VQ_MAX || (size & (size - 1u)) != 0u) {
        return -EINVAL;
    }
    if (((uintptr_t)mem & 15u) != 0u || (addr & 15u) != 0u) {
        return -EINVAL;
    }
    (void)pb_vq_layout(size, &avail_off, &used_off);
    q->size = size;
    q->desc = (volatile struct pb_desc *)mem;
    q->avail = (volatile uint16_t *)((uint8_t *)mem + avail_off);
    q->used = (volatile uint8_t *)mem + used_off;
    q->desc_addr = addr;
    q->avail_addr = addr + avail_off;
    q->used_addr = addr + used_off;
    relink(q);
    return 0;
}

void pb_vq_reset(struct pb_vq *q)
{
    relink(q);
}

int pb_vq_add(struct pb_vq *q, const struct pb_buf *out, unsigned nout, const struct pb_buf *in, unsigned nin,
              uint16_t *head)
{
    unsigned total = nout + nin;
    if (total == 0u || (nout != 0u && out == NULL) || (nin != 0u && in == NULL) || head == NULL) {
        return -EINVAL;
    }
    if (total > q->num_free) {
        return -ENOSPC;
    }
    for (unsigned i = 0; i < total; i++) {
        const struct pb_buf *b = i < nout ? &out[i] : &in[i - nout];
        if (b->len == 0u) {
            return -EINVAL;
        }
    }
    uint16_t first = q->free_head;
    uint16_t idx = first;
    uint16_t last = first;
    for (unsigned i = 0; i < total; i++) {
        const struct pb_buf *b = i < nout ? &out[i] : &in[i - nout];
        uint16_t flags = (uint16_t)(i < nout ? 0u : PB_DESC_F_WRITE);
        if (i + 1u < total) {
            flags |= PB_DESC_F_NEXT;
        }
        q->desc[idx].addr = pb_le64(b->addr);
        q->desc[idx].len = pb_le32(b->len);
        q->desc[idx].flags = pb_le16(flags);
        last = idx;
        idx = pb_le16(q->desc[idx].next);
    }
    (void)last;
    q->free_head = idx;
    q->num_free = (uint16_t)(q->num_free - total);
    q->chain_len[first] = (uint16_t)total;
    q->avail[2u + (q->avail_idx & (q->size - 1u))] = pb_le16(first);
    q->avail_idx++;
    *head = first;
    return 0;
}

void pb_vq_publish(struct pb_vq *q)
{
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    q->avail[1] = pb_le16(q->avail_idx);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
}

int pb_vq_kick_needed(const struct pb_vq *q)
{
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    uint16_t flags = pb_le16(*(const volatile uint16_t *)q->used);
    return (flags & PB_USED_F_NO_NOTIFY) == 0u;
}

void pb_vq_set_no_interrupt(struct pb_vq *q, int on)
{
    q->avail[0] = pb_le16((uint16_t)(on ? PB_AVAIL_F_NO_INTERRUPT : 0u));
}

int pb_vq_get_used(struct pb_vq *q, uint16_t *head, uint32_t *len)
{
    uint16_t used_idx = pb_le16(*(const volatile uint16_t *)(q->used + 2u));
    if (used_idx == q->last_used) {
        return 0;
    }
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    const volatile uint8_t *e = q->used + 4u + 8u * (size_t)(q->last_used & (q->size - 1u));
    uint32_t id = pb_le32(*(const volatile uint32_t *)e);
    uint32_t n = pb_le32(*(const volatile uint32_t *)(e + 4u));
    q->last_used++;
    if (id >= q->size || q->chain_len[id] == 0u) {
        return -EIO;
    }
    /* Return the chain to the free list: walk it to its tail, then point the tail at the old free head. */
    uint16_t tail = (uint16_t)id;
    for (uint16_t i = 1; i < q->chain_len[id]; i++) {
        tail = pb_le16(q->desc[tail].next);
    }
    q->desc[tail].next = pb_le16(q->free_head);
    q->free_head = (uint16_t)id;
    q->num_free = (uint16_t)(q->num_free + q->chain_len[id]);
    q->chain_len[id] = 0;
    *head = (uint16_t)id;
    *len = n;
    return 1;
}
