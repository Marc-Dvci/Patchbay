"""vhost-user, back-end side: QEMU keeps the virtio PCI device model and hands the queues to this process.

Message layout (QEMU docs/interop/vhost-user.rst, "Message Specification"): le32 request, le32 flags, le32 size, then
`size` bytes of payload. File descriptors travel as SCM_RIGHTS ancillary data. Flags: bits 0-1 version (1), bit 2
reply, bit 3 need_reply.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass

HEADER = struct.Struct("<III")
VERSION = 0x1
FLAG_REPLY = 0x4
FLAG_NEED_REPLY = 0x8
MAX_PAYLOAD = 4096
MAX_FDS = 8

F_PROTOCOL_FEATURES = 1 << 30  # VHOST_USER_F_PROTOCOL_FEATURES, in the virtio feature word
VIRTIO_F_VERSION_1 = 1 << 32
VRING_NOFD_MASK = 1 << 8
VRING_IDX_MASK = 0xFF


class Req(enum.IntEnum):
    GET_FEATURES = 1
    SET_FEATURES = 2
    SET_OWNER = 3
    RESET_OWNER = 4
    SET_MEM_TABLE = 5
    SET_LOG_BASE = 6
    SET_LOG_FD = 7
    SET_VRING_NUM = 8
    SET_VRING_ADDR = 9
    SET_VRING_BASE = 10
    GET_VRING_BASE = 11
    SET_VRING_KICK = 12
    SET_VRING_CALL = 13
    SET_VRING_ERR = 14
    GET_PROTOCOL_FEATURES = 15
    SET_PROTOCOL_FEATURES = 16
    GET_QUEUE_NUM = 17
    SET_VRING_ENABLE = 18
    SEND_RARP = 19
    NET_SET_MTU = 20
    SET_BACKEND_REQ_FD = 21
    IOTLB_MSG = 22
    SET_VRING_ENDIAN = 23
    GET_CONFIG = 24
    SET_CONFIG = 25
    GET_INFLIGHT_FD = 31
    SET_INFLIGHT_FD = 32
    GET_MAX_MEM_SLOTS = 36
    ADD_MEM_REG = 37
    REM_MEM_REG = 38
    SET_STATUS = 39
    GET_STATUS = 40


class Proto(enum.IntFlag):
    MQ = 1 << 0
    LOG_SHMFD = 1 << 1
    RARP = 1 << 2
    REPLY_ACK = 1 << 3
    MTU = 1 << 4
    BACKEND_REQ = 1 << 5
    CROSS_ENDIAN = 1 << 6
    CRYPTO_SESSION = 1 << 7
    PAGEFAULT = 1 << 8
    CONFIG = 1 << 9


class ProtocolError(Exception):
    """The front end sent something this back end will not accept. The connection is closed."""


@dataclass(frozen=True)
class Message:
    req: int
    flags: int
    payload: bytes

    @property
    def need_reply(self) -> bool:
        return bool(self.flags & FLAG_NEED_REPLY)


@dataclass(frozen=True)
class MemRegion:
    gpa: int
    size: int
    uva: int
    mmap_offset: int


REGION = struct.Struct("<QQQQ")
VRING_STATE = struct.Struct("<II")
VRING_ADDR = struct.Struct("<IIQQQQ")
CONFIG_HDR = struct.Struct("<III")


def decode_header(raw: bytes) -> tuple[int, int, int]:
    if len(raw) != HEADER.size:
        raise ProtocolError("short header")
    req, flags, size = HEADER.unpack(raw)
    if flags & 0x3 != VERSION:
        raise ProtocolError(f"unsupported protocol version {flags & 0x3}")
    if size > MAX_PAYLOAD:
        raise ProtocolError(f"payload of {size} bytes is over the {MAX_PAYLOAD}-byte limit")
    return req, flags, size


def encode_reply(req: int, payload: bytes) -> bytes:
    return HEADER.pack(req, VERSION | FLAG_REPLY, len(payload)) + payload


def u64(payload: bytes) -> int:
    if len(payload) < 8:
        raise ProtocolError("expected a 64-bit payload")
    return int(struct.unpack_from("<Q", payload)[0])


def decode_mem_table(payload: bytes) -> list[MemRegion]:
    if len(payload) < 8:
        raise ProtocolError("short memory table")
    n, _pad = struct.unpack_from("<II", payload)
    if n > MAX_FDS or len(payload) < 8 + n * REGION.size:
        raise ProtocolError(f"memory table claims {n} regions")
    return [MemRegion(*REGION.unpack_from(payload, 8 + i * REGION.size)) for i in range(n)]


def decode_single_region(payload: bytes) -> MemRegion:
    """ADD_MEM_REG and REM_MEM_REG carry u64 padding, then one region."""
    if len(payload) < 8 + REGION.size:
        raise ProtocolError("short memory region")
    return MemRegion(*REGION.unpack_from(payload, 8))


def decode_vring_state(payload: bytes) -> tuple[int, int]:
    if len(payload) < VRING_STATE.size:
        raise ProtocolError("short vring state")
    a, b = VRING_STATE.unpack_from(payload)
    return int(a), int(b)


def decode_vring_addr(payload: bytes) -> tuple[int, int, int, int, int]:
    """(index, flags, desc, used, avail), addresses in the front end's address space."""
    if len(payload) < VRING_ADDR.size:
        raise ProtocolError("short vring address")
    index, flags, desc, used, avail, _log = VRING_ADDR.unpack_from(payload)
    return int(index), int(flags), int(desc), int(used), int(avail)


def decode_config_request(payload: bytes) -> tuple[int, int, int, bytes]:
    if len(payload) < CONFIG_HDR.size:
        raise ProtocolError("short config request")
    offset, size, flags = CONFIG_HDR.unpack_from(payload)
    if size > MAX_PAYLOAD - CONFIG_HDR.size:
        raise ProtocolError("config request too large")
    return int(offset), int(size), int(flags), payload[CONFIG_HDR.size : CONFIG_HDR.size + size]


def encode_config_reply(offset: int, size: int, flags: int, space: bytes) -> bytes:
    chunk = space[offset : offset + size]
    chunk += bytes(size - len(chunk))
    return CONFIG_HDR.pack(offset, size, flags) + chunk
