/*
 * virtio-spi request framing and mode translation. Pure C.
 *
 * Layouts from the virtio specification's SPI controller device, as Linux carries them in
 * include/uapi/linux/virtio_spi.h. One transfer is one request in two descriptors:
 *   device-readable   struct spi_transfer_head (32 bytes), then tx_buf when there is one
 *   device-writable   rx_buf when there is one, then struct spi_transfer_result { u8 result; }
 * A full-duplex transfer has tx_buf and rx_buf of the same length.
 */
#ifndef PB_SPI_H
#define PB_SPI_H

#include <stddef.h>
#include <stdint.h>

#include "pb_vring.h"

#define PB_VIRTIO_ID_SPI     45u
#define PB_SPI_HEAD_LEN      32u
#define PB_SPI_CONFIG_LEN    32u

#define PB_SPI_CPHA          (1u << 0)
#define PB_SPI_CPOL          (1u << 1)
#define PB_SPI_CS_HIGH       (1u << 2)
#define PB_SPI_LSB_FIRST     (1u << 3)
#define PB_SPI_LOOP          (1u << 4)

#define PB_SPI_MF_CPHA_0     (1u << 0)
#define PB_SPI_MF_CPHA_1     (1u << 1)
#define PB_SPI_MF_CPOL_0     (1u << 2)
#define PB_SPI_MF_CPOL_1     (1u << 3)
#define PB_SPI_MF_CS_HIGH    (1u << 4)
#define PB_SPI_MF_LSB_FIRST  (1u << 5)
#define PB_SPI_MF_LOOPBACK   (1u << 6)

#define PB_SPI_TRANS_OK      0u
#define PB_SPI_PARAM_ERR     1u
#define PB_SPI_TRANS_ERR     2u

/* QNX's SPI mode word, from <hw/io-spi.h>, restated so this file needs no QNX header. */
#define PB_QNX_WORD_WIDTH_MASK 0xFFu
#define PB_QNX_CPHA_1        (1u << 8)
#define PB_QNX_CPOL_1        (1u << 9)
#define PB_QNX_BORDER_MSB    (1u << 10)
#define PB_QNX_CSPOL_HIGH    (1u << 11)

struct pb_spi_config {
    uint8_t cs_max_number;
    uint8_t cs_change_supported;
    uint8_t tx_nbits_supported;
    uint8_t rx_nbits_supported;
    uint32_t bits_per_word_mask;
    uint32_t mode_func_supported;
    uint32_t max_freq_hz;
    uint32_t max_word_delay_ns;
    uint32_t max_cs_setup_ns;
    uint32_t max_cs_hold_ns;
    uint32_t max_cs_inactive_ns;
};

/* Decode the 32-byte device configuration space. */
void pb_spi_config_decode(const uint8_t raw[PB_SPI_CONFIG_LEN], struct pb_spi_config *cfg);

/* virtio mode bits and bits per word for a QNX mode word. */
uint32_t pb_spi_mode_from_qnx(uint32_t qnx_mode, uint8_t *bits_per_word);

/* 0 when the device supports this mode, word width and clock; -EINVAL otherwise. */
int pb_spi_check(const struct pb_spi_config *cfg, uint32_t virtio_mode, uint8_t bits_per_word, uint32_t freq_hz);

struct pb_spi_req {
    struct pb_buf out;
    struct pb_buf in;
    size_t out_off, in_off;
};

/*
 * Lay out one transfer: tlen bytes from tx (may be 0), rlen bytes to receive (may be 0). Both nonzero must be equal.
 * Returns 0, -EINVAL or -ENOBUFS.
 */
int pb_spi_plan(uint8_t cs, uint8_t bits_per_word, uint32_t mode, uint32_t freq_hz, const void *tx, uint32_t tlen,
                uint32_t rlen, uint8_t *arena, uint64_t arena_addr, size_t arena_len, struct pb_spi_req *req);

/* The device's result code, after copying rlen bytes of rx data out when it is PB_SPI_TRANS_OK. A short used length
 * reads as PB_SPI_TRANS_ERR. */
unsigned pb_spi_collect(const struct pb_spi_req *req, const uint8_t *arena, uint32_t used, void *rx, uint32_t rlen);

#endif
