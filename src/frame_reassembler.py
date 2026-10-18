#!/usr/bin/env python3
"""Reassembles the post-auth AV data-channel byte stream into complete H.264/PCM frames.

See abus-protocol.md for the full reverse-engineering history; the short version: this
camera's H.264 bitstream has real quirks (documented there) but standard decoders handle it
fine (see gst_rtsp_server.py) - this module's job is purely reconstructing the camera's
encrypted/XOR'd frame stream into plain access units, nothing codec-specific beyond that.
"""
from __future__ import annotations

import struct

import logutil
import crypto_utils

# Codec IDs used by the post-auth AV frame header.
CODEC_H264 = 3

# The camera never transmits SPS/PPS - the app's own H.264 decoder setup uses these same
# fixed csd-0 (SPS) / csd-1 (PPS) byte arrays instead of parsing them from the stream. Every
# frame we receive is a non-IDR slice referencing these hardcoded parameter sets.
H264_SPS = bytes([0, 0, 0, 1, 103, 100, 0, 40, 172, 52, 197, 1, 224, 17, 31, 120, 11, 80, 16, 16, 31, 0, 0, 3, 3, 233, 0, 0, 234, 96, 148])
H264_PPS = bytes([0, 0, 0, 1, 104, 238, 60, 128])

CODECID_A_AAC = 1283
# Confirmed live: despite requesting AAC via IOCTRL_TYPE_AUDIO_START, this camera actually
# sends raw PCM audio (codec_id=1279) - AAC (1283) never arrives. The app's own fallback
# audio-device setup for any non-AAC/ADPCM codec (which is what this camera's PCM frames
# hit) uses: 8kHz sample rate, mono, 16-bit signed samples (native little-endian format).
CODECID_A_PCM = 1279
CODECID_A_ADPCM = 1280
PCM_SAMPLE_RATE_HZ = 8000
# Frame-header flag byte the app's ThreadIntercom stamps on every outgoing microphone frame.
TALK_FRAME_FLAG = 2
STREAM_IO_TYPE_AV_FRAME = 3


def build_av_frame(session_key: bytes, codec_id: int, flag: int, payload: bytes) -> bytes:
    """Inverse of FrameReassembler for the app -> camera direction (P2PDev.sendPCMAudioDataToDevice +
    encryptPacket): 4-byte micro-header (24-bit LE size = len(payload)+16, then stream-io-type 3),
    then the 16-byte frame header AES-ECB encrypted with the session key, then the payload
    unmodified (xor_key 0). Header layout (AVFrameHead): codec_id LE16, [2]=0, [3]=flag,
    [4]=xor_key 0, [5:8]=codec parameters (0 for raw PCM), [8:12]=data size LE32, [12:16]=timestamp
    (the app leaves it 0)."""
    header = struct.pack("<HBBBBHII", codec_id, 0, flag, 0, 0, 0, len(payload), 0)
    encrypted = crypto_utils.new_ecb_cipher(session_key).encrypt(header)
    return (len(payload) + 16).to_bytes(3, "little") + bytes([STREAM_IO_TYPE_AV_FRAME]) + encrypted + payload


class FrameReassembler:
    """Decode the post-auth data-channel byte stream into complete frame-header/payload units.

    Each DRW packet's payload (after the confirmed 4-byte tag+channel+seq header stripped by
    parse_drw_header) is a *continuous* byte stream (not one message per UDP packet). Every
    new message in that stream starts with a 4-byte micro-header - confirmed via capture
    analysis: bytes[0:3] = 24-bit LE outer data_size (covers everything AFTER this 4-byte
    header: the 16-byte frame header PLUS its payload - i.e. outer_data_size ==
    inner_data_size + 16), byte[3] = stream_io_type. An earlier, fabricated
    2-byte-LE-total-len/2-byte-BE-subtype layout never matched the real wire format for
    frames >= 256 bytes (byte[2] of the real 3-byte size was silently ignored), which is why
    the byte-resync storm persisted even after fixing the outer DRW packet header - this
    layer was the real bug. Followed by a 16-byte AES-128-ECB-encrypted header (session key)
    that decodes to the real frame-header fields (codec_id, xor_key, data_size - the
    validation check is exactly `inner_data_size + 16 != outer_data_size`), then `data_size`
    bytes of payload XOR'd with that single xor_key byte. Large frames (e.g. H.264 video) span
    multiple D0 packets; continuation packets carry no header at all - they are just more raw
    bytes of the current message's payload.
    """

    def __init__(self, session_key: bytes):
        self._cipher = crypto_utils.new_ecb_cipher(session_key)
        self._remaining = 0
        self._xor_key = 0
        self._codec_id = 0
        self._stream_io_type = 0
        self._flag = 0
        self._buf = bytearray()
        self._pending = bytearray()
        self._resync_count = 0
        # ADPCM audio only: (pre-sample, index) from the header of the frame feed() last yielded.
        self.audio_params = (0, 0)

    def feed(self, chunk: bytes):
        """Feed raw tag/channel/seq-stripped DRW payload bytes. Yields
        (codec_id, stream_io_type, flag, payload_bytes) per completed frame - stream_io_type
        is byte[3] of the 4-byte micro-header (3=video/audio frame, 4=ioctrl, 5 in some cases,
        possibly discriminating multiple interleaved streams/channels), flag is the frame
        header's own keyframe indicator (0 == keyframe)."""
        self._pending += chunk
        pos = 0
        while True:
            if self._remaining == 0:
                if len(self._pending) - pos < 20:
                    break
                p = self._pending
                outer_data_size = p[pos] | (p[pos + 1] << 8) | (p[pos + 2] << 16)
                stream_io_type = p[pos + 3]
                header = self._cipher.decrypt(bytes(p[pos + 4:pos + 20]))
                codec_id = header[0] | (header[1] << 8)
                xor_key = header[4]
                data_size = header[8] | (header[9] << 8) | (header[10] << 16) | (header[11] << 24)
                if data_size + 16 != outer_data_size:
                    # Desynchronized (dropped/reordered packet) - drop this byte and resync.
                    # This was previously completely silent - if it ever gets stuck failing
                    # to resync, video would appear to "just stop" with zero visible cause,
                    # indistinguishable from the camera itself going quiet or an ack problem.
                    self._resync_count += 1
                    if self._resync_count <= 5 or self._resync_count % 500 == 0:
                        logutil.debug_log(f"[reassembler] byte-resync #{self._resync_count} at pos={pos} "
                            f"(outer_data_size={outer_data_size} vs data_size+16={data_size + 16})")
                    pos += 1
                    continue
                self._codec_id = codec_id
                self._stream_io_type = stream_io_type
                self._flag = header[3]
                self._xor_key = xor_key
                self.audio_params = (int.from_bytes(header[6:8], "little", signed=True), header[5])
                self._remaining = data_size
                self._buf = bytearray()
                pos += 20
            else:
                take = min(self._remaining, len(self._pending) - pos)
                if take <= 0:
                    break
                part = bytearray(self._pending[pos:pos + take])
                if self._xor_key:
                    for i in range(len(part)):
                        part[i] ^= self._xor_key
                self._buf += part
                self._remaining -= take
                pos += take
                if self._remaining == 0:
                    yield self._codec_id, self._stream_io_type, self._flag, bytes(self._buf)
        del self._pending[:pos]


class IoctlReassembler:
    """Camera -> client IOCTL responses on DRW channel 0 (what P2PDev's ThreadRecvIOCtrl reads).
    Same outer shape as the AV stream - 4-byte micro-header (24-bit LE size, stream-io-type 4),
    then a 16-byte AES-ECB(session_key) header - but the header is an AVIOCtrlHead: ioctl type
    LE16 @0, data size LE16 @2 (LE32 @8 when that is 0), xor_key @4; payload XOR'd with it.
    A message whose sizes don't add up is skipped as a whole."""

    def __init__(self, session_key: bytes):
        self._cipher = crypto_utils.new_ecb_cipher(session_key)
        self._pending = bytearray()

    def feed(self, chunk: bytes):
        """Yields (ioctl_type, payload) per complete type-4 message."""
        self._pending += chunk
        while len(self._pending) >= 4:
            size = self._pending[0] | (self._pending[1] << 8) | (self._pending[2] << 16)
            io_type = self._pending[3]
            if size < 16 or size > 1 << 16:
                self._pending.clear()  # hopelessly out of sync - wait for the next message
                return
            if len(self._pending) < 4 + size:
                return
            message = bytes(self._pending[4:4 + size])
            del self._pending[:4 + size]
            if io_type != 4:
                continue
            header = self._cipher.decrypt(message[:16])
            ioctl_type = header[0] | (header[1] << 8)
            data_size = (header[2] | (header[3] << 8)) or int.from_bytes(header[8:12], "little")
            if data_size + 16 != size:
                continue
            xor_key = header[4]
            payload = message[16:]
            yield ioctl_type, (bytes(b ^ xor_key for b in payload) if xor_key else payload)
