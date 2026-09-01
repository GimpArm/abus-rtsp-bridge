#!/usr/bin/env python3
"""Process-wide logging helpers - timestamped, never-raising print() wrappers.

Kept as its own module (rather than living in abus_rtsp_bridge.py) since every other
module in this project needs to log, and importing the full bridge module just for log()
would create a needless circular/heavyweight dependency.
"""
import time

_T0 = time.time()
DEBUG = False


def set_debug(enabled: bool) -> None:
    """Toggle verbose per-packet/per-frame diagnostics (debug_log()) process-wide."""
    global DEBUG
    DEBUG = enabled


def log(msg: str) -> None:
    """print() with a wall-clock + elapsed-since-start timestamp, so reconnect cadence
    (e.g. time between 0xF0 events) can be measured directly from the log. Never raises -
    a closed/broken stdout (e.g. the terminal that launched this process going away) must
    not take down the camera stream."""
    now = time.time()
    line = (f"[{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now % 1 * 1000):03d} "
            f"+{now - _T0:8.3f}s] {msg}")
    try:
        print(line, flush=True)
    except (BrokenPipeError, OSError):
        pass


def debug_log(msg: str) -> None:
    """Verbose per-packet/per-frame diagnostics - only shown with --debug."""
    if DEBUG:
        log(msg)
