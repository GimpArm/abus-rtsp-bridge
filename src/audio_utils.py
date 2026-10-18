#!/usr/bin/env python3
"""Small PCM helpers for the camera -> client audio path."""
from __future__ import annotations

import struct


def upsample_2x(pcm: bytes) -> bytes:
    """Double the sample rate of mono S16LE PCM by linear interpolation."""
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    out = []
    for i, s in enumerate(samples):
        out.append(s)
        out.append((s + samples[i + 1]) // 2 if i + 1 < n else s)
    return struct.pack(f"<{len(out)}h", *out)
