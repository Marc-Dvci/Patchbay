/*
 * Host unit tests for driver/core: the split ring's driver half and the I2C/SPI framing.
 * Built with AddressSanitizer and UBSan by `make -C driver check`. Each test is named for the behaviour it pins.
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pb_i2c.h"
#include "pb_le.h"
#include "pb_spi.h"
#include "pb_vring.h"

static int failures;
static int checks;

#define CHECK(cond)                                                                      \
    do {                                                                                 \
        checks++;                                                                        \
        if (!(cond)) {                                                                   \
            failures++;                                                                  \
            fprintf(stderr, "%s:%d: %s: check failed: %s\n", __FILE__, __LINE__, __func__, #cond); \
        }                                                                                \
    } while (0)

static _Alignas(16) uint8_t ring_mem[16384];

/* The device's half, minimal: complete the request at avail slot `slot` with `len` written. */
static void device_complete(struct pb_vq *q, uint16_t head, uint32_t len)
{
    volatile uint8_t *used = q->used;
    uint16_t idx = *(volatile uint16_t *)(used + 2);
    volatile uint8_t *e = used + 4 + 8 * (idx % q->size);
    *(volatile uint32_t *)e = head;
    *(volatile uint32_t *)(e + 4) = len;
    *(volatile uint16_t *)(used + 2) = (uint16_t)(idx + 1);
}

static void test_layout_matches_virtio_alignment(void)
{
    size_t a, u;
    size_t total = pb_vq_layout(4, &a, &u);
    CHECK(a == 64);            /* 4 descriptors of 16 bytes */
    CHECK(u % 4 == 0);         /* used ring 4-byte aligned */
    CHECK(u >= a + 6 + 2 * 4); /* after flags, idx, ring[4], used_event */
    CHECK(total >= u + 6 + 8 * 4);
    CHECK(total % 16 == 0);
}

static void test_init_refuses_sizes_that_are_not_powers_of_two(void)
{
    struct pb_vq q;
    CHECK(pb_vq_init(&q, ring_mem, 0x1000, 0) == -EINVAL);
    CHECK(pb_vq_init(&q, ring_mem, 0x1000, 3) == -EINVAL);
    CHECK(pb_vq_init(&q, ring_mem, 0x1000, 512) == -EINVAL);
    CHECK(pb_vq_init(&q, ring_mem + 8, 0x1000, 4) == -EINVAL);  /* misaligned */
    CHECK(pb_vq_init(&q, ring_mem, 0x1000, 4) == 0);
    CHECK(q.num_free == 4);
}

static void test_a_chain_is_readable_buffers_then_writable_ones(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 8);
    struct pb_buf out[2] = { { 0x2000, 8 }, { 0x3000, 5 } };
    struct pb_buf in[1] = { { 0x4000, 1 } };
    uint16_t head;
    CHECK(pb_vq_add(&q, out, 2, in, 1, &head) == 0);
    CHECK(q.num_free == 5);
    uint16_t i = head;
    CHECK(q.desc[i].addr == 0x2000 && q.desc[i].len == 8 && q.desc[i].flags == PB_DESC_F_NEXT);
    i = q.desc[i].next;
    CHECK(q.desc[i].addr == 0x3000 && q.desc[i].flags == PB_DESC_F_NEXT);
    i = q.desc[i].next;
    CHECK(q.desc[i].addr == 0x4000 && q.desc[i].flags == PB_DESC_F_WRITE);
    CHECK(q.avail[1] == 0);  /* not published yet */
    pb_vq_publish(&q);
    CHECK(q.avail[1] == 1 && q.avail[2] == head);
}

static void test_a_full_ring_refuses_and_stays_unchanged(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 4);
    struct pb_buf b[3] = { { 0x2000, 8 }, { 0x3000, 8 }, { 0x4000, 1 } };
    uint16_t h1, h2;
    CHECK(pb_vq_add(&q, b, 2, b + 2, 1, &h1) == 0);
    uint16_t free_before = q.num_free, head_before = q.free_head, avail_before = q.avail_idx;
    CHECK(pb_vq_add(&q, b, 2, b + 2, 1, &h2) == -ENOSPC);
    CHECK(q.num_free == free_before && q.free_head == head_before && q.avail_idx == avail_before);
}

static void test_zero_length_buffers_and_empty_requests_are_refused(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 4);
    struct pb_buf z = { 0x2000, 0 }, ok = { 0x2000, 4 };
    uint16_t h;
    CHECK(pb_vq_add(&q, &z, 1, NULL, 0, &h) == -EINVAL);
    CHECK(pb_vq_add(&q, NULL, 0, NULL, 0, &h) == -EINVAL);
    CHECK(pb_vq_add(&q, &ok, 1, NULL, 0, NULL) == -EINVAL);
    CHECK(q.num_free == 4);
}

static void test_completions_in_any_order_free_every_descriptor(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 8);
    struct pb_buf o = { 0x2000, 8 }, i = { 0x3000, 2 };
    uint16_t h[4];
    for (int k = 0; k < 4; k++) {
        CHECK(pb_vq_add(&q, &o, 1, &i, 1, &h[k]) == 0);
    }
    CHECK(q.num_free == 0);
    pb_vq_publish(&q);
    device_complete(&q, h[2], 2);
    device_complete(&q, h[0], 1);
    device_complete(&q, h[3], 2);
    device_complete(&q, h[1], 0);
    uint16_t got;
    uint32_t len;
    uint16_t order[4];
    uint32_t lens[4];
    for (int k = 0; k < 4; k++) {
        CHECK(pb_vq_get_used(&q, &got, &len) == 1);
        order[k] = got;
        lens[k] = len;
    }
    CHECK(order[0] == h[2] && order[1] == h[0] && order[2] == h[3] && order[3] == h[1]);
    CHECK(lens[0] == 2 && lens[1] == 1 && lens[2] == 2 && lens[3] == 0);
    CHECK(pb_vq_get_used(&q, &got, &len) == 0);
    CHECK(q.num_free == 8);
    /* and the free list is whole: eight one-descriptor requests fit again */
    for (int k = 0; k < 8; k++) {
        uint16_t hh;
        CHECK(pb_vq_add(&q, &o, 1, NULL, 0, &hh) == 0);
    }
}

static void test_a_used_id_not_in_flight_is_an_error(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 4);
    struct pb_buf o = { 0x2000, 8 };
    uint16_t h, got;
    uint32_t len;
    pb_vq_add(&q, &o, 1, NULL, 0, &h);
    pb_vq_publish(&q);
    device_complete(&q, (uint16_t)(h + 1), 0);  /* never added */
    uint16_t free_before = q.num_free;
    CHECK(pb_vq_get_used(&q, &got, &len) == -EIO);
    CHECK(q.num_free == free_before);
    device_complete(&q, 99, 0);  /* outside the ring */
    CHECK(pb_vq_get_used(&q, &got, &len) == -EIO);
}

static void test_reset_forgets_requests_in_flight(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 4);
    struct pb_buf o = { 0x2000, 8 };
    uint16_t h;
    pb_vq_add(&q, &o, 1, NULL, 0, &h);
    pb_vq_publish(&q);
    pb_vq_reset(&q);
    CHECK(q.num_free == 4 && q.avail_idx == 0 && q.avail[1] == 0);
}

static void test_no_notify_flag_suppresses_the_kick(void)
{
    struct pb_vq q;
    pb_vq_init(&q, ring_mem, 0x10000, 4);
    CHECK(pb_vq_kick_needed(&q) == 1);
    *(volatile uint16_t *)q.used = PB_USED_F_NO_NOTIFY;
    CHECK(pb_vq_kick_needed(&q) == 0);
}

/* --- I2C framing -------------------------------------------------------------------------------------------- */

static uint8_t arena[16384];

static void test_i2c_write_then_read_is_two_requests_with_fail_next_on_the_first(void)
{
    uint8_t reg = 0x1A, rx[6] = { 0 };
    struct pb_i2c_seg s[2] = { { 0x28, 0, 1, &reg, NULL }, { 0x28, 1, 6, NULL, rx } };
    struct pb_i2c_plan p;
    CHECK(pb_i2c_plan(s, 2, 1, arena, 0x80000, sizeof(arena), &p) == 0);
    CHECK(p.n == 2);
    const uint8_t *h0 = arena + p.req[0].out_off;
    CHECK(h0[0] == 0x50 && h0[1] == 0);        /* 0x28 << 1, little-endian */
    CHECK(h0[4] == 0x01);                     /* FAIL_NEXT, write */
    CHECK(h0[8] == 0x1A);                     /* write data after the header */
    CHECK(p.req[0].out.len == 9 && p.req[0].in.len == 1);
    const uint8_t *h1 = arena + p.req[1].out_off;
    CHECK(h1[4] == 0x02);                     /* M_RD, last: no FAIL_NEXT */
    CHECK(p.req[1].out.len == 8 && p.req[1].in.len == 7);
    CHECK(p.req[0].out.addr == 0x80000 + p.req[0].out_off);
    CHECK(p.req[1].in.addr % 8 == 0);
}

static void test_i2c_collect_copies_read_data_only_when_every_status_is_ok(void)
{
    uint8_t reg = 0, rx[2] = { 0 };
    struct pb_i2c_seg s[2] = { { 0x28, 0, 1, &reg, NULL }, { 0x28, 1, 2, NULL, rx } };
    struct pb_i2c_plan p;
    pb_i2c_plan(s, 2, 1, arena, 0, sizeof(arena), &p);
    uint32_t used[2] = { 1, 3 };
    CHECK(pb_i2c_collect(s, &p, arena, used) == 0);  /* statuses still 0xFF: never written */
    arena[p.req[0].in_off] = PB_I2C_MSG_OK;
    arena[p.req[1].in_off] = 0xA0;
    arena[p.req[1].in_off + 1] = 0xFB;
    arena[p.req[1].in_off + 2] = PB_I2C_MSG_OK;
    CHECK(pb_i2c_collect(s, &p, arena, used) == -1);
    CHECK(rx[0] == 0xA0 && rx[1] == 0xFB);
    arena[p.req[1].in_off + 2] = PB_I2C_MSG_ERR;
    CHECK(pb_i2c_collect(s, &p, arena, used) == 1);
    arena[p.req[1].in_off + 2] = PB_I2C_MSG_OK;
    used[1] = 2;  /* the device wrote less than data plus status */
    CHECK(pb_i2c_collect(s, &p, arena, used) == 1);
}

static void test_i2c_plan_refuses_bad_addresses_lengths_and_small_arenas(void)
{
    uint8_t b[8] = { 0 };
    struct pb_i2c_plan p;
    struct pb_i2c_seg ten_bit = { 0x80, 0, 1, b, NULL };
    CHECK(pb_i2c_plan(&ten_bit, 1, 1, arena, 0, sizeof(arena), &p) == -EINVAL);
    struct pb_i2c_seg probe = { 0x28, 0, 0, NULL, NULL };
    CHECK(pb_i2c_plan(&probe, 1, 0, arena, 0, sizeof(arena), &p) == -EINVAL);
    CHECK(pb_i2c_plan(&probe, 1, 1, arena, 0, sizeof(arena), &p) == 0);
    CHECK(p.req[0].out.len == 8 && p.req[0].in.len == 1);
    struct pb_i2c_seg big = { 0x28, 1, PB_I2C_MAX_LEN + 1, NULL, b };
    CHECK(pb_i2c_plan(&big, 1, 1, arena, 0, sizeof(arena), &p) == -EINVAL);
    struct pb_i2c_seg rd = { 0x28, 1, 8, NULL, b };
    CHECK(pb_i2c_plan(&rd, 1, 1, arena, 0, 16, &p) == -ENOBUFS);
    struct pb_i2c_seg nobuf = { 0x28, 1, 4, NULL, NULL };
    CHECK(pb_i2c_plan(&nobuf, 1, 1, arena, 0, sizeof(arena), &p) == -EINVAL);
}

/* --- SPI framing -------------------------------------------------------------------------------------------- */

static void test_spi_mode_translation_follows_the_qnx_and_virtio_bit_definitions(void)
{
    uint8_t bpw = 0;
    /* the MCP3008 sample: MSB first, 8 bits, CPHA 0, CPOL 0 */
    CHECK(pb_spi_mode_from_qnx(PB_QNX_BORDER_MSB | 8u, &bpw) == 0u && bpw == 8);
    CHECK(pb_spi_mode_from_qnx(8u, &bpw) == PB_SPI_LSB_FIRST);
    CHECK(pb_spi_mode_from_qnx(PB_QNX_BORDER_MSB | PB_QNX_CPHA_1 | PB_QNX_CPOL_1 | 16u, &bpw) ==
          (PB_SPI_CPHA | PB_SPI_CPOL) && bpw == 16);
    CHECK(pb_spi_mode_from_qnx(PB_QNX_BORDER_MSB | PB_QNX_CSPOL_HIGH | 8u, NULL) == PB_SPI_CS_HIGH);
}

static void test_spi_check_refuses_what_the_controller_does_not_offer(void)
{
    struct pb_spi_config c = { .cs_max_number = 2, .bits_per_word_mask = 1u << 7,
                               .mode_func_supported = PB_SPI_MF_CPHA_0 | PB_SPI_MF_CPOL_0, .max_freq_hz = 1000000 };
    CHECK(pb_spi_check(&c, 0, 8, 1000000) == 0);
    CHECK(pb_spi_check(&c, 0, 16, 1000000) == -EINVAL);
    CHECK(pb_spi_check(&c, 0, 8, 1000001) == -EINVAL);
    CHECK(pb_spi_check(&c, PB_SPI_CPHA, 8, 1000) == -EINVAL);
    CHECK(pb_spi_check(&c, PB_SPI_CPOL, 8, 1000) == -EINVAL);
    CHECK(pb_spi_check(&c, PB_SPI_LSB_FIRST, 8, 1000) == -EINVAL);
    CHECK(pb_spi_check(&c, PB_SPI_CS_HIGH, 8, 1000) == -EINVAL);
    CHECK(pb_spi_check(&c, 0, 0, 1000) == -EINVAL);
    c.bits_per_word_mask = 0;  /* no limitation */
    c.max_freq_hz = 0;
    CHECK(pb_spi_check(&c, 0, 32, 4000000000u) == 0);
}

static void test_spi_config_decodes_the_32_byte_layout(void)
{
    uint8_t raw[PB_SPI_CONFIG_LEN] = { 2, 1, 0, 0, 0x80, 0, 0, 0, 0x6F, 0, 0, 0, 0x80, 0xF0, 0xFA, 0x02 };
    struct pb_spi_config c;
    pb_spi_config_decode(raw, &c);
    CHECK(c.cs_max_number == 2 && c.cs_change_supported == 1);
    CHECK(c.bits_per_word_mask == 0x80 && c.mode_func_supported == 0x6F);
    CHECK(c.max_freq_hz == 50000000u);
}

static void test_spi_full_duplex_plan_puts_head_and_tx_out_rx_and_result_in(void)
{
    uint8_t tx[3] = { 0x01, 0x80, 0x00 }, rx[3] = { 0 };
    struct pb_spi_req r;
    CHECK(pb_spi_plan(0, 8, 0, 1000000, tx, 3, 3, arena, 0x90000, sizeof(arena), &r) == 0);
    CHECK(r.out.len == PB_SPI_HEAD_LEN + 3 && r.in.len == 4);
    CHECK(arena[0] == 0 && arena[1] == 8 && arena[3] == 1 && arena[4] == 1);
    uint32_t freq;
    memcpy(&freq, arena + 12, 4);
    CHECK(pb_le32(freq) == 1000000u);
    CHECK(memcmp(arena + PB_SPI_HEAD_LEN, tx, 3) == 0);
    CHECK(pb_spi_collect(&r, arena, 4, rx, 3) == PB_SPI_TRANS_ERR);  /* result never written */
    arena[r.in_off] = 0;
    arena[r.in_off + 1] = 0x02;
    arena[r.in_off + 2] = 0x00;
    arena[r.in_off + 3] = PB_SPI_TRANS_OK;
    CHECK(pb_spi_collect(&r, arena, 4, rx, 3) == PB_SPI_TRANS_OK);
    CHECK(rx[1] == 0x02 && rx[2] == 0x00);
    CHECK(pb_spi_collect(&r, arena, 3, rx, 3) == PB_SPI_TRANS_ERR);  /* short used length */
    arena[r.in_off + 3] = PB_SPI_PARAM_ERR;
    CHECK(pb_spi_collect(&r, arena, 4, rx, 3) == PB_SPI_PARAM_ERR);
}

static void test_spi_plan_refuses_unequal_duplex_lengths_and_empty_transfers(void)
{
    uint8_t tx[4] = { 0 };
    struct pb_spi_req r;
    CHECK(pb_spi_plan(0, 8, 0, 1, tx, 4, 3, arena, 0, sizeof(arena), &r) == -EINVAL);
    CHECK(pb_spi_plan(0, 8, 0, 1, tx, 0, 0, arena, 0, sizeof(arena), &r) == -EINVAL);
    CHECK(pb_spi_plan(0, 8, 0, 1, NULL, 4, 0, arena, 0, sizeof(arena), &r) == -EINVAL);
    CHECK(pb_spi_plan(0, 8, 0, 1, NULL, 0, 4, arena, 0, sizeof(arena), &r) == 0);  /* read only */
    CHECK(r.out.len == PB_SPI_HEAD_LEN && r.in.len == 5);
    CHECK(pb_spi_plan(0, 8, 0, 1, tx, 4, 0, arena, 0, 20, &r) == -ENOBUFS);
}

int main(void)
{
    test_layout_matches_virtio_alignment();
    test_init_refuses_sizes_that_are_not_powers_of_two();
    test_a_chain_is_readable_buffers_then_writable_ones();
    test_a_full_ring_refuses_and_stays_unchanged();
    test_zero_length_buffers_and_empty_requests_are_refused();
    test_completions_in_any_order_free_every_descriptor();
    test_a_used_id_not_in_flight_is_an_error();
    test_reset_forgets_requests_in_flight();
    test_no_notify_flag_suppresses_the_kick();
    test_i2c_write_then_read_is_two_requests_with_fail_next_on_the_first();
    test_i2c_collect_copies_read_data_only_when_every_status_is_ok();
    test_i2c_plan_refuses_bad_addresses_lengths_and_small_arenas();
    test_spi_mode_translation_follows_the_qnx_and_virtio_bit_definitions();
    test_spi_check_refuses_what_the_controller_does_not_offer();
    test_spi_config_decodes_the_32_byte_layout();
    test_spi_full_duplex_plan_puts_head_and_tx_out_rx_and_result_in();
    test_spi_plan_refuses_unequal_duplex_lengths_and_empty_transfers();
    printf("%d tests, %d checks, %d failed\n", 17, checks, failures);
    return failures == 0 ? 0 : 1;
}
