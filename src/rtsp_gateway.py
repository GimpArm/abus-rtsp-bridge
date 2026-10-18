#!/usr/bin/env python3
"""Single RTSP entry point for a multi-camera setup. Runs in the supervisor process, listens on
the shared RTSP port and routes each connection by the first path segment of its first
routable request (rtsp://host:8554/<camera>/...) to that camera's worker, which only listens
on loopback. After that first request the connection is a plain byte pipe, so the stream (RTP
interleaved over RTSP/TCP), Basic auth and everything else are handled by the worker's own
RTSP server exactly as in single-camera mode.
"""
from __future__ import annotations

import asyncio
import threading
import urllib.parse

from logutil import debug_log, log

_MAX_HEAD_BYTES = 16 * 1024
_HEAD_TIMEOUT_S = 10.0
_PUBLIC_METHODS = "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER"


def _parse_head(head: bytes) -> tuple[str, str, str]:
    """(method, first path segment, CSeq) of an RTSP request head; empty strings if absent."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    method = parts[0].upper()
    segment = ""
    if len(parts) >= 2:
        path = urllib.parse.urlsplit(parts[1]).path
        segment = next((p for p in path.split("/") if p), "")
    cseq = next((line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("cseq:")), "0")
    return method, segment, cseq


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _handle_client(camera_ports: dict[str, int], reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
    backend_writer = None
    try:
        while True:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S)
            method, segment, cseq = _parse_head(head)
            port = camera_ports.get(segment)
            if port is not None:
                break
            if method == "OPTIONS":
                # Capability probe on the bare server URL (no camera in the path yet).
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: {_PUBLIC_METHODS}\r\n\r\n".encode())
                await writer.drain()
                continue
            writer.write(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
            await writer.drain()
            return

        try:
            backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            debug_log(f"[rtsp-gateway] camera '{segment}' worker unreachable: {exc}")
            writer.write(f"RTSP/1.0 503 Service Unavailable\r\nCSeq: {cseq}\r\n\r\n".encode())
            await writer.drain()
            return
        backend_writer.write(head)
        await backend_writer.drain()
        pumps = [asyncio.ensure_future(_pump(reader, backend_writer)),
                 asyncio.ensure_future(_pump(backend_reader, writer))]
        _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
        pass
    finally:
        for w in (writer, backend_writer):
            if w is not None:
                w.close()


def start_rtsp_gateway(host: str, port: int, camera_ports: dict[str, int]) -> None:
    """Listen on host:port and route rtsp://.../<camera>/... to each worker's loopback RTSP port.
    Raises OSError if the port can't be bound."""
    ready = threading.Event()
    errors: list[OSError] = []

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.start_server(
                lambda r, w: _handle_client(camera_ports, r, w), host, port, limit=_MAX_HEAD_BYTES))
        except OSError as exc:
            errors.append(exc)
            return
        finally:
            ready.set()
        loop.run_forever()

    threading.Thread(target=run, name="rtsp-gateway", daemon=True).start()
    ready.wait()
    if errors:
        raise OSError(f"RTSP gateway failed to bind {host}:{port}: {errors[0]}")
    log(f"[rtsp-gateway] listening on rtsp://{host}:{port}/<camera> (cameras: {', '.join(camera_ports)})")
