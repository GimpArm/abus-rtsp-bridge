#!/usr/bin/env python3
"""Single REST entry point for a multi-camera setup. Runs in the supervisor process and forwards
/<camera>/<anything the PTZ REST server serves> (e.g. /garage/ptz/left, /garage/snapshot) to that
camera's own worker, which only listens on loopback. Also serves:

  GET /cameras  - configured camera names
  GET /health   - worst status across all cameras (503 if any is unhealthy or unreachable)

Basic auth is checked here with the shared credentials and the Authorization header is passed on
to the worker. Like the workers, /health and /<camera>/health are never auth-gated.
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import http_basic_auth
from logutil import debug_log, log

_STATUS_SEVERITY = ["ok", "starting", "stalled", "reconnecting", "unhealthy", "unreachable"]
_FAILING_STATUSES = {"unhealthy", "unreachable"}


def _fetch_health(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)  # a 503 "unhealthy" still carries the JSON body
        except ValueError:
            return {"status": "unreachable"}
    except (OSError, ValueError):
        return {"status": "unreachable"}


class _GatewayHandler(http.server.BaseHTTPRequestHandler):
    camera_ports: dict[str, int]
    username: Optional[str] = None
    password: Optional[str] = None
    server_version = "abus-rest-gateway/1.0"

    def log_message(self, fmt: str, *args) -> None:
        debug_log(f"[rest-gateway] {self.address_string()} - {fmt % args}")

    def _reply(self, code: int, body: dict) -> None:
        self._reply_raw(code, "application/json", json.dumps(body).encode("utf-8"))

    def _reply_raw(self, code: int, content_type: Optional[str], data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_health(self) -> None:
        cameras = {name: _fetch_health(port) for name, port in self.camera_ports.items()}
        worst = max((body.get("status", "unreachable") for body in cameras.values()),
                    key=lambda status: _STATUS_SEVERITY.index(status) if status in _STATUS_SEVERITY
                    else len(_STATUS_SEVERITY))
        failing = worst in _FAILING_STATUSES or worst not in _STATUS_SEVERITY
        self._reply(503 if failing else 200,
                    {"status": "unhealthy" if failing else worst, "cameras": cameras})

    def _forward(self, port: int, parsed: urllib.parse.ParseResult, parts: list[str]) -> None:
        url = f"http://127.0.0.1:{port}/{'/'.join(parts[1:])}"
        if parsed.query:
            url += f"?{parsed.query}"
        length = int(self.headers.get("Content-Length") or 0)
        headers = {}
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        request = urllib.request.Request(url, data=self.rfile.read(length) if length else None,
                                         method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                self._reply_raw(resp.status, resp.headers.get("Content-Type"), resp.read())
        except urllib.error.HTTPError as exc:
            self._reply_raw(exc.code, exc.headers.get("Content-Type"), exc.read())
        except OSError as exc:
            self._reply(502, {"error": f"camera worker unreachable: {exc}"})

    def _handle(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["health"]:
            self._reply_health()
            return
        if parts[1:] != ["health"] and not http_basic_auth.require_basic_auth(
                self, self.username, self.password, realm="abus-ptz"):
            return
        if parts == ["cameras"]:
            self._reply(200, {"cameras": list(self.camera_ports)})
            return
        if not parts or parts[0] not in self.camera_ports:
            self._reply(404, {"error": "unknown camera", "cameras": list(self.camera_ports),
                              "try": "/<camera>/ptz/<direction>?step=N, /<camera>/snapshot, /<camera>/health, /cameras, /health"})
            return
        self._forward(self.camera_ports[parts[0]], parsed, parts)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def start_rest_gateway(host: str, port: int, camera_ports: dict[str, int],
                       username: Optional[str] = None, password: Optional[str] = None,
                       ) -> http.server.ThreadingHTTPServer:
    """Listen on host:port and route /<camera>/... to each camera worker's loopback REST port."""
    handler_cls = type("_BoundGatewayHandler", (_GatewayHandler,),
                       {"camera_ports": camera_ports, "username": username, "password": password})
    server = http.server.ThreadingHTTPServer((host, port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"[rest-gateway] listening on http://{host}:{port}/<camera>/ptz/<direction> "
        f"(cameras: {', '.join(camera_ports)}; see /cameras, /health){' [auth required]' if username else ''}")
    return server
