#!/usr/bin/env python3
"""Play an audio file (e.g. an mp3 fetched from a URL) on the camera's speaker.

The file is downloaded, decoded with GStreamer to mono S16LE PCM at the rate the camera's talk
path expects (see talkback.py) and then fed to the shared TalkbackController in real time, so it
goes through exactly the same SPEAKER_START / audio / SPEAKER_STOP flow as RTSP talkback.

Used by POST /play?source=<url> on the REST server (ptz_rest_api.py). Download and decode happen
before the request is answered, so a bad URL or an undecodable file is reported to the caller;
playback itself continues in the background.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

import talkback
from logutil import log

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    _IMPORT_ERROR: Optional[Exception] = None
except (ImportError, ValueError) as _exc:  # depends on system packages
    Gst = None  # type: ignore
    _IMPORT_ERROR = _exc

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 180
FETCH_TIMEOUT_S = 10
DECODE_TIMEOUT_S = 30
# Audio is paced in real time in chunks of this length.
CHUNK_S = 0.04


class AudioError(Exception):
    """A failure that maps to an HTTP status for the REST caller."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _fetch(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AudioError(400, "audio must be an http:// or https:// URL")
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise AudioError(502, f"could not download audio: {exc}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise AudioError(413, f"audio file is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    return data


def _decode_to_pcm(data: bytes, sample_rate: int) -> bytes:
    if _IMPORT_ERROR is not None:
        raise AudioError(501, "audio playback needs GStreamer + PyGObject") from _IMPORT_ERROR
    Gst.init(None)
    max_bytes = MAX_AUDIO_SECONDS * sample_rate * 2
    pipeline = Gst.parse_launch(
        "appsrc name=src ! decodebin ! audioconvert ! audioresample ! "
        f"audio/x-raw,format=S16LE,rate={sample_rate},channels=1,layout=interleaved ! "
        "appsink name=sink sync=false")
    src, sink, bus = pipeline.get_by_name("src"), pipeline.get_by_name("sink"), pipeline.get_bus()
    pcm = bytearray()
    deadline = time.monotonic() + DECODE_TIMEOUT_S
    try:
        pipeline.set_state(Gst.State.PLAYING)
        src.emit("push-buffer", Gst.Buffer.new_wrapped(data))
        src.emit("end-of-stream")
        while True:
            sample = sink.emit("try-pull-sample", Gst.SECOND // 2)
            if sample is None:
                if sink.props.eos:
                    break
                error = bus.pop_filtered(Gst.MessageType.ERROR)
                if error is not None:
                    raise AudioError(422, f"could not decode audio: {error.parse_error()[0].message}")
                if time.monotonic() > deadline:
                    raise AudioError(422, "decoding the audio timed out")
                continue
            buffer = sample.get_buffer()
            ok, info = buffer.map(Gst.MapFlags.READ)
            if ok:
                pcm += bytes(info.data)
                buffer.unmap(info)
            if len(pcm) > max_bytes:
                raise AudioError(413, f"audio is longer than {MAX_AUDIO_SECONDS} seconds")
    finally:
        pipeline.set_state(Gst.State.NULL)
    if not pcm:
        raise AudioError(422, "no audio could be decoded from the file")
    return bytes(pcm)


class AudioPlayer:
    """Plays one file at a time through the session's shared TalkbackController."""

    def __init__(self, session) -> None:
        self._session = session
        self._busy = threading.Lock()

    def play_url(self, url: str) -> float:
        """Download and decode `url`, start playing it in the background and return its length in
        seconds. Raises AudioError (with an HTTP status) if it can't be played."""
        if not self._session.session_key:
            raise AudioError(503, "camera session not established yet")
        if not self._busy.acquire(blocking=False):
            raise AudioError(409, "audio is already playing")
        try:
            rate = talkback.talk_sample_rate(self._session)
            pcm = _decode_to_pcm(_fetch(url), rate)
            seconds = len(pcm) / (rate * 2)
            # Silence up to the controller's frame size so the tail of the file is really sent.
            pcm += bytes(-len(pcm) % talkback.pcm_flush_unit(self._session))
        except BaseException:
            self._busy.release()
            raise
        log(f"[talk] playing {seconds:.1f}s of audio from {url}")
        threading.Thread(target=self._play, args=(pcm, rate), name="audio-playback", daemon=True).start()
        return seconds

    def _play(self, pcm: bytes, rate: int) -> None:
        chunk = int(rate * 2 * CHUNK_S) // 2 * 2
        start = time.monotonic()
        try:
            for index, offset in enumerate(range(0, len(pcm), chunk)):
                delay = start + index * CHUNK_S - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._session.talkback.push_pcm(pcm[offset:offset + chunk])
        except Exception as exc:
            log(f"[talk] audio playback failed: {exc}")
        finally:
            self._busy.release()
