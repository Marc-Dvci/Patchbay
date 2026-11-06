#include "pb_spi.h"

#include <errno.h>
#include <string.h>

#include "pb_le.h"

static uint32_t rd32(const uint8_t *p)
{
    uint32_t v;
    memcpy(&v, p, 4);
    return pb_le32(v);
}

static void wr32(uint8_t *p, uint32_t v)
{
    v = pb_le32(v);
    memcpy(p, &v, 4);
}

void pb_spi_config_decode(const uint8_t raw[PB_SPI_CONFIG_LEN], struct pb_spi_config *cfg)
{
    cfg->cs_max_number = raw[0];
    cfg->cs_change_supported = raw[1];
    cfg->tx_nbits_supported = raw[2];
    cfg->rx_nbits_supported = raw[3];
    cfg->bits_per_word_mask = rd32(raw + 4);
    cfg->mode_func_supported = rd32(raw + 8);
    cfg->max_freq_hz = rd32(raw + 12);
    cfg->max_word_delay_ns = rd32(raw + 16);
    cfg->max_cs_setup_ns = rd32(raw + 20);
    cfg->max_cs_hold_ns = rd32(raw + 24);
    cfg->max_cs_inactive_ns = rd32(raw + 28);
}

uint32_t pb_spi_mode_from_qnx(uint32_t qnx_mode, uint8_t *bits_per_word)
{
    uint32_t m = 0;
    if (qnx_mode & PB_QNX_CPHA_1) {
        m |= PB_SPI_CPHA;
    }
    if (qnx_mode & PB_QNX_CPOL_1) {
        m |= PB_SPI_CPOL;
    }
    if (qnx_mode & PB_QNX_CSPOL_HIGH) {
        m |= PB_SPI_CS_HIGH;
    }
    if ((qnx_mode & PB_QNX_BORDER_MSB) == 0u) {
        m |= PB_SPI_LSB_FIRST;
    }
    if (bits_per_word != NULL) {
        *bits_per_word = (uint8_t)(qnx_mode & PB_QNX_WORD_WIDTH_MASK);
    }
    return m;
}

int pb_spi_check(const struct pb_spi_config *cfg, uint32_t mode, uint8_t bpw, uint32_t freq_hz)
{
    uint32_t mf = cfg->mode_func_supported;
    if (bpw == 0u || bpw > 32u) {
        return -EINVAL;
    }
    if (cfg->bits_per_word_mask != 0u && (cfg->bits_per_word_mask & (1u << (bpw - 1u))) == 0u) {
        return -EINVAL;
    }
    if (cfg->max_freq_hz != 0u && freq_hz > cfg->max_freq_hz) {
        return -EINVAL;
    }
    if ((mf & ((mode & PB_SPI_CPHA) ? PB_SPI_MF_CPHA_1 : PB_SPI_MF_CPHA_0)) == 0u) {
        return -EINVAL;
    }
    if ((mf & ((mode & PB_SPI_CPOL) ? PB_SPI_MF_CPOL_1 : PB_SPI_MF_CPOL_0)) == 0u) {
        return -EINVAL;
    }
    if ((mode & PB_SPI_CS_HIGH) && (mf & PB_SPI_MF_CS_HIGH) == 0u) {
        return -EINVAL;
    }
    if ((mode & PB_SPI_LSB_FIRST) && (mf & PB_SPI_MF_LSB_FIRST) == 0u) {
        return -EINVAL;
    }
    if ((mode & PB_SPI_LOOP) && (mf & PB_SPI_MF_LOOPBACK) == 0u) {
        return -EINVAL;
    }
    return 0;
}

int pb_spi_plan(uint8_t cs, uint8_t bpw, uint32_t mode, uint32_t freq_hz, const void *tx, uint32_t tlen,
                uint32_t rlen, uint8_t *arena, uint64_t arena_addr, size_t arena_len, struct pb_spi_req *req)
{
    if (arena == NULL || req == NULL || (tlen != 0u && tx == NULL) || (tlen == 0u && rlen == 0u)) {
        return -EINVAL;
    }
    if (tlen != 0u && rlen != 0u && tlen != rlen) {
        return -EINVAL;
    }
    size_t out_len = PB_SPI_HEAD_LEN + tlen;
    size_t in_off = (out_len + 7u) & ~(size_t)7u;
    if (in_off + rlen + 1u > arena_len) {
        return -ENOBUFS;
    }
    uint8_t *h = arena;
    memset(h, 0, PB_SPI_HEAD_LEN);
    h[0] = cs;
    h[1] = bpw;
    h[2] = 0;   /* cs_change: chip select is released at the end of the transfer */
    h[3] = 1;   /* tx_nbits: single */
    h[4] = 1;   /* rx_nbits: single */
    wr32(h + 8, mode);
    wr32(h + 12, freq_hz);
    if (tlen != 0u) {
        memcpy(h + PB_SPI_HEAD_LEN, tx, tlen);
    }
    memset(arena + in_off, 0, rlen);
    arena[in_off + rlen] = 0xFF;  /* a result the device never wrote reads as an error */
    req->out_off = 0;
    req->out.addr = arena_addr;
    req->out.len = (uint32_t)out_len;
    req->in_off = in_off;
    req->in.addr = arena_addr + in_off;
    req->in.len = rlen + 1u;
    return 0;
}

unsigned pb_spi_collect(const struct pb_spi_req *req, const uint8_t *arena, uint32_t used, void *rx, uint32_t rlen)
{
    if (used < req->in.len) {
        return PB_SPI_TRANS_ERR;
    }
    uint8_t result = arena[req->in_off + req->in.len - 1u];
    if (result == PB_SPI_TRANS_OK && rlen != 0u && rx != NULL) {
        memcpy(rx, arena + req->in_off, rlen);
    }
    return result <= PB_SPI_TRANS_ERR ? result : PB_SPI_TRANS_ERR;
}
