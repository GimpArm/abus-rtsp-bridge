#!/usr/bin/env python3
"""IMA ADPCM (4 bits per 16-bit sample) encoder - a port of the app's own
com.decoder.util.ImaAdpcmCodec.encode(), used for talkback to "OV serial" cameras (DIDs starting
with CO/DO), which take 16 kHz ADPCM instead of raw PCM. This is the standard IMA/DVI algorithm;
two consecutive samples are packed per byte, first sample in the low nibble. The predictor state
carries on from call to call (the app only resets it when an intercom session starts).
"""
from __future__ import annotations

from typing import Optional

STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66,
    73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408,
    449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630,
    9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8)


class ImaAdpcmDecoder:
    """Port of com.decoder.util.AdpcmCodec.decode(): each byte holds two samples, low nibble
    first. The app resets the decoder once, from the first audio frame's header (pre-sample and
    index), and then lets the state run on across frames."""

    def __init__(self) -> None:
        self.reset(0, 0)

    def reset(self, pred: int, index: int) -> None:
        self._pred = max(-32768, min(32767, pred))
        self._index = max(0, min(88, index))

    def decode(self, data: bytes) -> bytes:
        """Returns signed 16-bit little-endian PCM, 4 bytes per input byte."""
        out = bytearray()
        for byte in data:
            for code in (byte & 15, byte >> 4):
                step = STEP_TABLE[self._index]
                diff = step >> 3
                if code & 1:
                    diff += step >> 2
                if code & 2:
                    diff += step >> 1
                if code & 4:
                    diff += step
                self._pred = max(-32768, min(32767, self._pred - diff if code & 8 else self._pred + diff))
                self._index = max(0, min(88, self._index + INDEX_TABLE[code & 7]))
                out += self._pred.to_bytes(2, "little", signed=True)
        return bytes(out)


class ImaAdpcmEncoder:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pred = 0
        self._index = 0
        self._pending: Optional[int] = None  # an odd trailing sample waiting for its pair

    def _nibble(self, sample: int) -> int:
        step = STEP_TABLE[self._index]
        diff = sample - self._pred
        code = 0
        if diff < 0:
            diff = -diff
            code = 8
        vpdiff = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            vpdiff += step
        if diff >= step >> 1:
            code |= 2
            diff -= step >> 1
            vpdiff += step >> 1
        if diff >= step >> 2:
            code |= 1
            vpdiff += step >> 2
        self._pred = max(-32768, min(32767, self._pred - vpdiff if code & 8 else self._pred + vpdiff))
        self._index = max(0, min(88, self._index + INDEX_TABLE[code & 7]))
        return code

    def encode(self, pcm: bytes) -> bytes:
        """Encode signed 16-bit little-endian PCM; returns len(samples)/2 bytes. A trailing odd
        sample is held back and paired with the first sample of the next call."""
        samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, len(pcm) - 1, 2)]
        if self._pending is not None:
            samples.insert(0, self._pending)
            self._pending = None
        if len(samples) % 2:
            self._pending = samples.pop()
        out = bytearray()
        for i in range(0, len(samples), 2):
            low = self._nibble(samples[i])
            high = self._nibble(samples[i + 1])
            out.append(low | (high << 4))
        return bytes(out)
