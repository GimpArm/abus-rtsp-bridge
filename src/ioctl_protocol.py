#!/usr/bin/env python3
"""IOCTL command layer built on top of wire_protocol.py's raw framing - command type
constants (video/audio start-stop, PTZ, light, siren) and the plaintext IOCTL header builder.
"""
from __future__ import annotations

import struct

# IOCTL_TYPE_VIDEO_START; app -> device IOCTRL requests always use xor_key 2
# (DEFAULT_XOR_KEY_IOCTRL).
IOCTRL_TYPE_VIDEO_START = 1
IOCTRL_TYPE_VIDEO_STOP = 2
IOCTRL_TYPE_AUDIO_START = 3
DEFAULT_XOR_KEY_IOCTRL = 2

# Video/audio-start payload quality/resolution constants.
QUALITY_BY_SETTING = 0
QUALITY_FULL_HD = 1
QUALITY_HD = 2
QUALITY_SD = 3
QUALITY_AUTOMATIC = 4

# Pan/tilt IOCTRL, sent on swipe-release in the real app as one 8-byte payload per axis moved:
# byte[0]=direction, byte[1]=step count (magnitude, roughly 1-16 for a full-width swipe),
# bytes[2:8]=0. Not a continuous joystick - each call is a single discrete "move N steps".
IOCTRL_TYPE_PTZ_COMMAND = 9
PTZ_STOP = 0
PTZ_UP = 1
PTZ_DOWN = 2
PTZ_LEFT = 3
PTZ_RIGHT = 4
PTZ_LEFT_UP = 5
PTZ_LEFT_DOWN = 6
PTZ_RIGHT_UP = 7
PTZ_RIGHT_DOWN = 8
PTZ_AUTO_SCAN = 11
PTZ_CALIBRATION = 16
PTZ_DIRECTIONS = {
    "stop": PTZ_STOP, "up": PTZ_UP, "down": PTZ_DOWN, "left": PTZ_LEFT, "right": PTZ_RIGHT,
    "left_up": PTZ_LEFT_UP, "left_down": PTZ_LEFT_DOWN, "right_up": PTZ_RIGHT_UP,
    "right_down": PTZ_RIGHT_DOWN, "auto_scan": PTZ_AUTO_SCAN, "calibration": PTZ_CALIBRATION,
}
# Same IOCTRL type 9 channel, but byte[1] here is a saved preset slot index (0-2, the app's
# 3 position buttons) rather than a step count. IOCTRL_PTZ_SET_PRESET_POINT=13 (save current
# position to a slot) is deliberately NOT implemented - the app's own admin-auth prompt
# around that action suggested it needs a separate/elevated auth flow, and it's a one-time
# setup step done from the app anyway.
PTZ_GOTO_PRESET = 12

# Simple on/off switches, each an 8-byte payload with byte[0]=1(on)/0(off) and the rest zero -
# NOT the same protocol as the richer schedule/color "WK light" module (IOCTRL type 208,
# 128-byte payload) some other camera models expose; this camera's light button uses the
# simple switch.
IOCTRL_TYPE_LIGHT_CONTROL = 192
IOCTRL_TYPE_SIREN = 212


def build_ioctl_head(ioctrl_type: int, data_size: int, xor_key: int = DEFAULT_XOR_KEY_IOCTRL) -> bytes:
    """Plaintext IOCTL header (16 bytes) - caller AES-encrypts this before sending."""
    return struct.pack("<HH", ioctrl_type, data_size) + bytes([xor_key]) + b"\x00" * 11
