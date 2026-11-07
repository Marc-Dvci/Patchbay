# Patchbay drivers for QNX SDP 8.0, and the pure core built for the host.
#
#   make                 i2c-virtio and spi-virtio for x86_64 (needs qnxsdp-env.sh sourced)
#   make ARCH=aarch64le  the same for 64-bit ARM
#   make host            build/host/libpbcore.so and the C unit tests, with the host compiler
#   make check           run the C unit tests
ARCH ?= x86_64
OUT := build/$(ARCH)
QCC := qcc -Vgcc_nto$(ARCH)
WARN := -Wall -Wextra -Werror -Wno-unused-parameter
CFLAGS := -O2 -g -std=gnu11 $(WARN) -Icore -Iqnx

CORE := core/pb_vring.c core/pb_i2c.c core/pb_spi.c
CORE_H := $(wildcard core/*.h)

all: $(OUT)/i2c-virtio $(OUT)/spi-virtio

$(OUT)/i2c-virtio: qnx/i2c-virtio.c qnx/pb_vpci.c core/pb_vring.c core/pb_i2c.c qnx/pb_vpci.h $(CORE_H)
	@mkdir -p $(OUT)
	$(QCC) $(CFLAGS) -o $@ qnx/i2c-virtio.c qnx/pb_vpci.c core/pb_vring.c core/pb_i2c.c \
		-l:libi2c-master.a -lpci -lsecpol

$(OUT)/spi-virtio: qnx/spi-virtio.c qnx/pb_vpci.c core/pb_vring.c core/pb_spi.c qnx/pb_vpci.h $(CORE_H)
	@mkdir -p $(OUT)
	$(QCC) $(CFLAGS) -o $@ qnx/spi-virtio.c qnx/pb_vpci.c core/pb_vring.c core/pb_spi.c \
		-l:libio-spi.a -lpci -lsecpol -lslog2

# --- host -----------------------------------------------------------------------------------------------------------
HOSTCC ?= cc
HOSTFLAGS := -O1 -g -std=c11 $(WARN) -Icore -fPIC
HOST := build/host

host: $(HOST)/libpbcore.so $(HOST)/test_core

$(HOST)/libpbcore.so: $(CORE) $(CORE_H) tests/pb_host_shim.c
	@mkdir -p $(HOST)
	$(HOSTCC) $(HOSTFLAGS) -shared -o $@ $(CORE) tests/pb_host_shim.c

$(HOST)/test_core: tests/test_core.c $(CORE) $(CORE_H)
	@mkdir -p $(HOST)
	$(HOSTCC) $(HOSTFLAGS) -fsanitize=address,undefined -fno-omit-frame-pointer -o $@ tests/test_core.c $(CORE)

check: $(HOST)/test_core
	$(HOST)/test_core

clean:
	rm -rf build

.PHONY: all host check clean
