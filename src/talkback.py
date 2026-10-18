#!/usr/bin/env python3
"""Two-way audio (talkback): turns the PCM an RTSP client sends back to us into the camera's
intercom stream.

The RTSP server (gst_rtsp_server.py) hands every decoded chunk of mono S16LE PCM to
TalkbackController.push_pcm() - at the rate the camera's recorder uses (8 kHz, or 16 kHz for
OV-serial cameras, see talk_sample_rate()). Like the app's ThreadIntercom (P2PDev) this opens
the camera's speaker with IOCTRL_TYPE_SPEAKER_START before the first audio, feeds it fixed-size
frames, and closes it with IOCTRL_TYPE_SPEAKER_STOP - here after a short silence, since there is
no "button released" event on an RTSP backchannel.

Normal cameras get the PCM frames as they are (640 bytes = 40 ms). OV-serial cameras (DID
starting with CO/DO) get IMA ADPCM: the app reads 1024 PCM bytes (32 ms) at a time, encodes
them to 256 bytes and sends 768 ADPCM bytes (96 ms) per frame.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from ima_adpcm import ImaAdpcmEncoder
from logutil import log

# What the app reads from its AudioRecord per loop iteration, and (OV only) how many encoded
# bytes it accumulates before sending a frame.
PCM_FRAME_BYTES = 640
OV_READ_BYTES = 1024
OV_SEND_BYTES = 768
# Stop the intercom this long after the last audio (the app waits ~1s after release too).
IDLE_STOP_S = 1.0
# The app starts its AudioRecord (and so its first audio) a moment after SPEAKER_START.
START_SETTLE_S = 0.1


def talk_sample_rate(session) -> int:
    """Sample rate of the audio the camera expects (the app's AudioRecord rate)."""
    return 16000 if session.ov_serial else 8000


class TalkbackController:
    """Thread-safe: push_pcm() is called from GStreamer streaming threads."""

    def __init__(self, session) -> None:
        self._session = session
        self._lock = threading.Lock()
        self._encoder = ImaAdpcmEncoder() if session.ov_serial else None
        self._read_bytes = OV_READ_BYTES if self._encoder else PCM_FRAME_BYTES
        self._buffer = bytearray()
        self._encoded = bytearray()
        self._started = False
        self._epoch = -1
        self._last_audio = 0.0
        self._watchdog: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        """True while the camera's speaker is open on the current camera session."""
        return self._started and self._epoch == self._session.session_epoch

    def push_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            if not self._session.session_key:
                return  # reconnecting - nothing to send audio to
            if not self.active:
                try:
                    self._session.start_intercom()
                except OSError as exc:
                    log(f"[talk] could not start intercom: {exc}")
                    return
                self._started = True
                self._epoch = self._session.session_epoch
                self._buffer.clear()
                self._encoded.clear()
                if self._encoder:
                    self._encoder.reset()
                time.sleep(START_SETTLE_S)
                self._ensure_watchdog()
            self._last_audio = time.monotonic()
            self._buffer += pcm
            while len(self._buffer) >= self._read_bytes:
                frame = bytes(self._buffer[:self._read_bytes])
                del self._buffer[:self._read_bytes]
                if self._encoder:
                    self._encoded += self._encoder.encode(frame)
                    while len(self._encoded) >= OV_SEND_BYTES:
                        self._send(bytes(self._encoded[:OV_SEND_BYTES]))
                        del self._encoded[:OV_SEND_BYTES]
                else:
                    self._send(frame)

    def _send(self, audio: bytes) -> None:
        try:
            self._session.send_talk_audio(audio)
        except OSError as exc:
            log(f"[talk] send failed (ignored): {exc}")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if not self._started:
            return
        was_active = self.active
        self._started = False
        self._buffer.clear()
        self._encoded.clear()
        if was_active:
            try:
                self._session.stop_intercom()
            except OSError as exc:
                log(f"[talk] stop failed (ignored): {exc}")

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch_idle, name="talkback-idle", daemon=True)
            self._watchdog.start()

    def _watch_idle(self) -> None:
        # Exits once the intercom is closed; push_pcm() starts a fresh one next time.
        while True:
            time.sleep(0.25)
            with self._lock:
                if not self._started:
                    return
                if time.monotonic() - self._last_audio >= IDLE_STOP_S:
                    self._stop_locked()
                    return
