#!/usr/bin/env python3
"""Lightweight, dependency-free REST server for PTZ control, snapshots, siren/light, and
health checks - GET/POST /ptz/<direction>, /ptz/preset/<0-2>, /light/<on|off>,
/siren/<on|off>, /snapshot, /health, /directions.

Runs in a daemon thread; shares the same authenticated CameraSession/UDP socket the RTSP
stream uses (PTZ is just another IOCTL, see CameraSession.move_ptz()).
"""
from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse
from typing import Optional, TYPE_CHECKING

import http_basic_auth
import ioctl_protocol as ioctl
from logutil import debug_log, log

if TYPE_CHECKING:
    from camera_session import CameraSession


class _PtzRequestHandler(http.server.BaseHTTPRequestHandler):
    """GET /ptz/<direction>[?step=N], GET /snapshot, GET /health, /light/<on|off>,
    /siren/<on|off> - direction is one of PTZ_DIRECTIONS' keys. `session`/`username`/
    `password`/`gst_server_ref` are bound per-instance by start_ptz_http_server() below."""

    session: "CameraSession"
    username: Optional[str] = None
    password: Optional[str] = None
    gst_server_ref: Optional[dict] = None
    server_version = "abus-ptz/1.0"

    def log_message(self, fmt: str, *args) -> None:
        debug_log(f"[ptz-http] {self.address_string()} - {fmt % args}")

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_jpeg(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_health(self) -> None:
        s = self.session
        now = time.time()
        discovered = s.camera_ip is not None
        authed = s.session_key is not None
        seconds_since_video = (now - s.last_video_data_time) if s.last_video_data_time else None
        reconnecting_seconds = (now - s.reconnecting_since) if s.reconnecting_since else None
        if not discovered or not authed:
            status = "starting"
        elif reconnecting_seconds is not None:
            # Reconnecting is expected/self-healing (see reconnect_until_success()) - only
            # treat it as unhealthy once it's gone on long enough that something is
            # genuinely stuck (e.g. camera unplugged), so Docker/orchestration can act on it.
            status = "unhealthy" if reconnecting_seconds > 120 else "reconnecting"
        elif s.is_streaming and seconds_since_video is not None and seconds_since_video > 20:
            status = "stalled"
        else:
            status = "ok"
        body = {
            "status": status,
            "discovered": discovered,
            "authed": authed,
            "streaming": s.is_streaming,
            "seconds_since_video": round(seconds_since_video, 1) if seconds_since_video is not None else None,
            "reconnecting": reconnecting_seconds is not None,
            "uptime_seconds": round(now - s.started_at, 1),
        }
        self._reply(200 if status != "unhealthy" else 503, body)

    def _handle(self) -> None:
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if len(parts) == 1 and parts[0] == "health":
            # Deliberately NOT gated by Basic auth - Docker's HEALTHCHECK and other monitors
            # need to reach this without the RTSP/PTZ credentials, and it exposes no control
            # surface or video, just connection/liveness state. Usable from process startup
            # (status="starting") since start_ptz_http_server() now runs before discover/auth.
            self._reply_health()
            return
        if not http_basic_auth.require_basic_auth(self, self.username, self.password, realm="abus-ptz"):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if len(parts) == 1 and parts[0] == "directions":
            self._reply(200, {"directions": sorted(ioctl.PTZ_DIRECTIONS)})
            return
        if len(parts) == 1 and parts[0] == "snapshot":
            gst_server = self.gst_server_ref.get("server") if self.gst_server_ref else None
            if gst_server is None:
                self._reply(503, {"error": "stream not started yet - connect an RTSP client first"})
                return
            jpeg = gst_server.get_snapshot_jpeg()
            if jpeg is None:
                self._reply(503, {"error": "no frame decoded yet"})
                return
            self._reply_jpeg(jpeg)
            return
        if len(parts) == 3 and parts[0] == "ptz" and parts[1] == "preset":
            try:
                index = int(parts[2])
            except ValueError:
                self._reply(400, {"error": "preset index must be an integer 0-2"})
                return
            try:
                self.session.goto_preset(index)
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._reply(500, {"error": str(exc)})
                return
            self._reply(200, {"ok": True, "preset": index})
            return
        if len(parts) == 2 and parts[0] in ("light", "siren"):
            action = parts[1].lower()
            if action not in ("on", "off"):
                self._reply(400, {"error": "action must be 'on' or 'off'"})
                return
            try:
                if parts[0] == "light":
                    self.session.set_light(action == "on")
                else:
                    self.session.set_siren(action == "on")
            except Exception as exc:
                self._reply(500, {"error": str(exc)})
                return
            self._reply(200, {"ok": True, parts[0]: action})
            return
        if len(parts) != 2 or parts[0] != "ptz":
            self._reply(404, {"error": "not found", "try": "/ptz/<direction>?step=N, /ptz/preset/<0-2>, /light/<on|off>, /siren/<on|off>, /snapshot, /health, /directions"})
            return
        direction_name = parts[1].lower()
        if direction_name not in ioctl.PTZ_DIRECTIONS:
            self._reply(400, {"error": f"unknown direction {direction_name!r}",
                               "directions": sorted(ioctl.PTZ_DIRECTIONS)})
            return
        try:
            step = int(query.get("step", ["4"])[0])
        except ValueError:
            self._reply(400, {"error": "step must be an integer"})
            return
        try:
            self.session.move_ptz(ioctl.PTZ_DIRECTIONS[direction_name], step)
        except Exception as exc:
            self._reply(500, {"error": str(exc)})
            return
        self._reply(200, {"ok": True, "direction": direction_name, "step": step})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


def start_ptz_http_server(session: "CameraSession", host: str, port: int,
                           username: Optional[str] = None, password: Optional[str] = None,
                           gst_server_ref: Optional[dict] = None,
                           ) -> http.server.ThreadingHTTPServer:
    """Lightweight, dependency-free REST server for PTZ control - GET/POST /ptz/<direction>,
    e.g. `curl http://host:8080/ptz/up` - GET /snapshot for a JPEG of the current frame, and
    GET /health (never auth-gated) for liveness/readiness, suitable as a Docker HEALTHCHECK.
    Runs in a daemon thread; shares the same authenticated CameraSession/UDP socket the RTSP
    stream uses (PTZ is just another IOCTL, see CameraSession.move_ptz()). HTTP Basic auth is
    enabled iff both username and password are given. `gst_server_ref` is a mutable
    {"server": ...} dict since this starts before read_stream() creates the actual
    GstAbusRtspServer instance."""
    handler_cls = type("_BoundPtzRequestHandler", (_PtzRequestHandler,),
                        {"session": session, "username": username, "password": password,
                         "gst_server_ref": gst_server_ref})
    server = http.server.ThreadingHTTPServer((host, port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"[ptz-http] listening on http://{host}:{port}/ptz/<direction> (see /directions, /snapshot, /health)"
        f"{' [auth required]' if username else ''}")
    return server
