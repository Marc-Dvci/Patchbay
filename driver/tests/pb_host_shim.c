/* Host build only: sizes of the core's structures, so the Python cross tests can check their ctypes mirrors. */
#include <stddef.h>

#include "pb_i2c.h"
#include "pb_spi.h"
#include "pb_vring.h"

size_t pb_test_sizeof_vq(void);
size_t pb_test_sizeof_i2c_plan(void);
size_t pb_test_sizeof_spi_req(void);
size_t pb_test_sizeof_i2c_seg(void);

size_t pb_test_sizeof_vq(void) { return sizeof(struct pb_vq); }
size_t pb_test_sizeof_i2c_plan(void) { return sizeof(struct pb_i2c_plan); }
size_t pb_test_sizeof_spi_req(void) { return sizeof(struct pb_spi_req); }
size_t pb_test_sizeof_i2c_seg(void) { return sizeof(struct pb_i2c_seg); }
