#!/usr/bin/env python3
"""Low-level ABUS LAN wire protocol - message framing, DID encode/decode, ack building.

This mirrors the exact LAN discovery/auth/session protocol reverse-engineered from
full_capture3.pcap (real Android app <-> camera traffic on the same LAN):

Wire format for every UDP datagram:
    byte 0    : 0xF1 magic
    byte 1    : message type (0x30 discover-req, 0x41/0x42 alive req/ack,
                0xE0/0xE1 ping/pong keepalive, 0xD0 data, 0xD1 ack)
    bytes 2-3 : big-endian uint16 length of the body that follows

For 0xD0 (data) / 0xD1 (ack) frames, the body itself is a small "reliable channel"
wrapper confirmed against the capture:
    byte 0    : 0xD1 fixed tag
    bytes 1-3 : big-endian 24-bit per-direction sequence number
    bytes 4-5 : little-endian uint16 length of the inner payload
    bytes 6-7 : big-endian uint16 subtype/channel id (1 = auth, 4 = small IOCTL)
    bytes 8.. : inner payload (the auth header struct for subtype 1)

The auth payload (subtype 1) is a fixed-layout struct:
    bytes 0-1 : AuthType (little-endian): 1=challenge, 2=response, 3=ok, 4=failed
    bytes 2-3 : data size (little-endian)
    bytes 4-15: reserved/zero
    bytes 16..: payload (16-byte challenge / AES response / encrypted session key)

The AES key is the view password, UTF-8 encoded and zero-padded (or truncated) to 16
bytes, used with AES-128-ECB (see crypto_utils.py). This was verified against the real
capture: encrypting the camera's challenge with that key reproduces the app's response
bytes exactly, and decrypting the AUTH_TYPE_OK payload with the same key yields the
session key.
"""
from __future__ import annotations

import socket
import struct
from typing import List, Optional, Tuple

DISCOVERY_PORT = 32108
CAMERA_PORT = 16411

MSG_SEARCH = 0x30
MSG_ALIVE_REQ = 0x41
MSG_ALIVE_ACK = 0x42
MSG_PING = 0xE0
MSG_PONG = 0xE1
# Undocumented: the camera sends this (zero-length body) right as it stops a preview/video
# session on its own (observed live after ~50 frames, every time) - re-request video on it.
MSG_STREAM_END = 0xF0
MSG_DATA = 0xD0
MSG_ACK = 0xD1

CHANNEL_AUTH = 1
CHANNEL_IOCTL = 4
CHANNEL_REALTIME_AV = 1  # DRW header's channel byte (parse_drw_header) - distinct namespace
# from the old 8-byte model's CHANNEL_AUTH/IOCTL subtype field despite the same value 1;
# confirmed via capture analysis: this is the realtime audio/video data channel specifically.

AUTH_TYPE_CHALLENGE = 1
AUTH_TYPE_RESPONSE = 2
AUTH_TYPE_OK = 3
AUTH_TYPE_FAILED = 4


def find_local_ipv4_candidates() -> List[str]:
    candidates: List[str] = []
    seen = set()
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if family != socket.AF_INET:
                continue
            addr = sockaddr[0]
            if addr.startswith("127."):
                continue
            if addr not in seen:
                seen.add(addr)
                candidates.append(addr)
    except OSError:
        pass

    if not candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                if local_ip and local_ip not in seen:
                    candidates.append(local_ip)
        except OSError:
            pass
    return candidates


def encode_did(did: str) -> bytes:
    """Encode a DID like 'ABCD-123456-EFGHI' into the 20-byte wire form seen in the capture.

    Layout: prefix (4 bytes) + 6 zero bytes + number (2 bytes big-endian) + suffix (5 bytes) + 3 zero bytes.
    """
    prefix, number, suffix = did.split("-")
    return (
        prefix.encode("ascii").ljust(4, b"\x00")[:4]
        + b"\x00" * 6
        + int(number).to_bytes(2, "big")
        + suffix.encode("ascii").ljust(5, b"\x00")[:5]
        + b"\x00" * 3
    )


def decode_did(body: bytes) -> Optional[str]:
    if len(body) < 20:
        return None
    prefix = body[0:4].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    number = int.from_bytes(body[10:12], "big")
    suffix = body[12:17].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    if not prefix or not suffix:
        return None
    return f"{prefix}-{number:06d}-{suffix}"


def build_f1(msg_type: int, payload: bytes = b"") -> bytes:
    return bytes([0xF1, msg_type & 0xFF]) + struct.pack(">H", len(payload)) + payload


def parse_f1(data: bytes) -> Optional[Tuple[int, bytes]]:
    if len(data) < 4 or data[0] != 0xF1:
        return None
    msg_type = data[1]
    length = struct.unpack_from(">H", data, 2)[0]
    body = data[4:4 + length]
    return msg_type, body


def build_d0(seq: int, subtype: int, payload: bytes) -> bytes:
    header = (
        bytes([0xD1])
        + (seq & 0xFFFFFF).to_bytes(3, "big")
        + struct.pack("<H", len(payload))
        + struct.pack(">H", subtype)
    )
    return build_f1(MSG_DATA, header + payload)


def parse_d0(body: bytes) -> Optional[dict]:
    if len(body) < 8 or body[0] != 0xD1:
        return None
    seq = int.from_bytes(body[1:4], "big")
    inner_len = struct.unpack_from("<H", body, 4)[0]
    subtype = struct.unpack_from(">H", body, 6)[0]
    payload = body[8:8 + inner_len]
    return {"seq": seq, "subtype": subtype, "payload": payload}


def parse_drw_header(body: bytes) -> Optional[Tuple[int, int, bytes]]:
    """The real per-packet DRW/data-channel header, confirmed via capture analysis - only 4
    bytes, NOT the 8-byte tag+3-byte-seq+len+subtype layout parse_d0()/build_d0() assume (that
    assumption was wrong for this specific channel):
        byte 0    : 0xD1 tag
        byte 1    : channel
        bytes 2-3 : big-endian 16-bit per-direction sequence number
        bytes 4.. : raw stream payload (NOT a separate "inner_len"/"subtype" sub-header -
                    that's the next layer up, framed by the reassembler's own micro-header
                    once you're inside the reassembled continuous byte stream - see
                    frame_reassembler.py).
    Returns (channel, seq, payload) or None if malformed."""
    if len(body) < 4 or body[0] != 0xD1:
        return None
    channel = body[1]
    seq = struct.unpack_from(">H", body, 2)[0]
    return channel, seq, body[4:]


def build_ack(ack_id: int) -> bytes:
    # Verified against ~2100 client ack packets across full_capture4.pcap: the 3-byte field
    # right after the 0xD1 tag is NOT a sequence number at all (our earlier "next_seq" reading
    # was wrong) - its low byte is always exactly the COUNT of 2-byte ack-ids that follow, and
    # the upper 2 bytes are a fixed 0x0100 marker (0x0000 only in the first couple of acks of
    # a session). i.e. the field is really `0x010000 | ack_count`, not a running sequence.
    body = bytes([0xD1]) + (0x010000 | 1).to_bytes(3, "big") + (ack_id & 0xFFFF).to_bytes(2, "big")
    return build_f1(MSG_ACK, body)


def build_batched_ack(ack_ids: List[int]) -> bytes:
    # Same wire format as build_ack(), just with N sequential 2-byte-BE ack-id entries instead
    # of one - matches the batched-ack format confirmed via capture analysis.
    body = bytes([0xD1]) + (0x010000 | (len(ack_ids) & 0xFF)).to_bytes(3, "big")
    for ack_id in ack_ids:
        body += (ack_id & 0xFFFF).to_bytes(2, "big")
    return build_f1(MSG_ACK, body)


def build_auth_head(auth_type: int, payload: bytes = b"") -> bytes:
    """Auth header: AuthType (LE16) + DataSize (LE16) + 12 reserved bytes + payload."""
    return struct.pack("<HH", auth_type, len(payload)) + b"\x00" * 12 + payload


def parse_auth_head(data: bytes) -> Optional[Tuple[int, bytes]]:
    if len(data) < 16:
        return None
    auth_type, data_size = struct.unpack_from("<HH", data, 0)
    return auth_type, data[16:16 + data_size]
