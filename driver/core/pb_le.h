/* Little-endian conversion for virtio structures. Both QNX targets (x86_64, aarch64le) are little-endian already. */
#ifndef PB_LE_H
#define PB_LE_H

#include <stdint.h>

#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
static inline uint16_t pb_le16(uint16_t v) { return __builtin_bswap16(v); }
static inline uint32_t pb_le32(uint32_t v) { return __builtin_bswap32(v); }
static inline uint64_t pb_le64(uint64_t v) { return __builtin_bswap64(v); }
#else
static inline uint16_t pb_le16(uint16_t v) { return v; }
static inline uint32_t pb_le32(uint32_t v) { return v; }
static inline uint64_t pb_le64(uint64_t v) { return v; }
#endif

#endif
