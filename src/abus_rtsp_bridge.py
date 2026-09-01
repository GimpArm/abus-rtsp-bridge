#!/usr/bin/env python3
"""ABUS LAN camera to RTSP bridge - CLI entry point.

This is deliberately just orchestration now - the actual protocol/session logic lives in:
  - wire_protocol.py       - raw F1/D0/D1/DRW framing (see its docstring for the wire format)
  - crypto_utils.py        - AES-128-ECB helpers
  - ioctl_protocol.py      - IOCTL command layer (video/audio/PTZ/light/siren)
  - frame_reassembler.py   - AV data-channel reconstruction into H.264/PCM access units
  - camera_session.py      - CameraSession: discover -> alive -> auth -> stream lifecycle
  - ptz_rest_api.py        - PTZ/snapshot/health REST server
  - gst_rtsp_server.py     - GStreamer decode/re-encode + RTSP serving
  - onvif_server.py        - ONVIF device/media/PTZ SOAP service
  - p2p_handshake.py       - CS2/iLnkP2P rendezvous protocol (LAN discovery fallback)
  - supervisor.py          - self-restarting process wrapper
See abus-protocol.md (repo memory) for the full reverse-engineering history.

Run:
python src/abus_rtsp_bridge.py --did <did> --password <password> --bind-ip <bind-ip> --target-ip <target-ip>

This process is meant to run unattended (e.g. in a Docker container, or detached from any
terminal) - it ignores SIGHUP and never lets a broken stdout (log consumer going away) kill
it, so losing whatever launched it does not stop the camera stream. Pass --debug for the
verbose per-packet/per-frame diagnostics used while investigating protocol issues; without
it only startup/connect/reconnect/error events are logged.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_file
import ioctl_protocol as ioctl
import logutil
import onvif_server
import supervisor
from camera_session import CameraSession
from logutil import log
from ptz_rest_api import start_ptz_http_server


def main() -> int:
    if hasattr(signal, "SIGHUP"):
        # A closed/gone controlling terminal (no TTY, session ended, etc.) must never stop
        # the stream - only an explicit SIGTERM/SIGINT (e.g. `docker stop`) should.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    p = argparse.ArgumentParser(description="ABUS LAN camera discovery, auth, and RTSP bridge")
    p.add_argument("--config", default=os.environ.get("ABUS_CONFIG_FILE"), help="Path to a structured YAML (or JSON) config file - see config_file.py's module docstring / config.example.yaml for the schema. Any explicit CLI flag below overrides the same setting from this file. Also settable via ABUS_CONFIG_FILE.")
    p.add_argument("--did", default=None, help="Optional DID to match, e.g. ABCD-123456-EFGHI")
    p.add_argument("--password", default=None, help="Camera view password / security code (required, here or in the config file)")
    p.add_argument("--bind-ip", default=None, help="Local IPv4 to bind the discovery socket to")
    p.add_argument("--target-ip", default=None, help="Known camera IP on same LAN")
    p.add_argument("--rtsp-url", default="rtsp://0.0.0.0:8554/abus", help="Destination RTSP URL to publish the stream")
    p.add_argument("--timeout", type=float, default=5.0, help="Discovery timeout in seconds")
    p.add_argument("--dump-raw", default=None, help="Write raw post-auth D0 frames to this file instead of ffmpeg (no ffmpeg required)")
    p.add_argument("--resolution", type=int, default=ioctl.QUALITY_BY_SETTING, help="Video quality/resolution: 0=bySetting 1=fullHD 2=HD 3=SD 4=automatic (default 0, matches the real app's live-view screen)")
    p.add_argument("--disable-audio", action="store_true", help="Disable serving the camera's audio (raw 8kHz mono PCM - confirmed live, despite the AAC we request via AUDIO_START) as a second RTP stream. Audio is served by default.")
    p.add_argument("--skip-video-start", action="store_true", help="Test flag: never send IOCTRL_TYPE_VIDEO_START - full_capture4.pcap shows the real app never sends it either, video just starts automatically after auth")
    p.add_argument("--skip-audio-start", action="store_true", help="Test flag: never send IOCTRL_TYPE_AUDIO_START - the first genuine real-app VIDEO_START session we captured (full_capture9.pcap) never sent it either")
    p.add_argument("--debug", action="store_true", help="Log verbose per-packet/per-frame diagnostics (byte-resyncs, heartbeats, keyframe/duplicate-frame tracking, non-video datagrams). Off by default now that the protocol is stable.")
    p.add_argument("--ptz-http-port", type=int, default=8080, help="Port for the PTZ REST server (GET/POST /ptz/<direction>?step=N). Default 8080.")
    p.add_argument("--ptz-http-host", default="0.0.0.0", help="Bind address for the PTZ REST server")
    p.add_argument("--no-ptz-http", action="store_true", help="Disable the PTZ REST server")
    p.add_argument("--onvif-port", type=int, default=8000, help="Port for the ONVIF device/media/PTZ SOAP service. Default 8000.")
    p.add_argument("--no-onvif", action="store_true", help="Disable the ONVIF service entirely")
    p.add_argument("--no-ws-discovery", action="store_true", help="Disable the WS-Discovery (UDP multicast) responder; the ONVIF HTTP service still runs, clients just need to be pointed at it manually")
    p.add_argument("--onvif-ptz-step", type=int, default=2, help="How far one ONVIF ContinuousMove/RelativeMove call (e.g. one NVR PTZ button click) moves the camera, on our 1-16 step scale (camera is calibrated at ~34 steps for a full 270-degree horizontal sweep, ~16 steps for a full 90-degree vertical sweep). Default 2 (a modest nudge); raise it if clicks move too little, lower if they still move too far.")
    p.add_argument("--auth-username", default=None, help="If set (together with --auth-password), require HTTP/RTSP Basic auth with this username on the RTSP stream, ONVIF service, and PTZ REST server. Unset (default) means none of them require auth.")
    p.add_argument("--auth-password", default=None, help="Password for --auth-username - both or neither must be set")

    # Pre-scan argv for --config so a file's settings can become the new argparse defaults
    # BEFORE the real parse below - that way an explicit CLI flag still overrides the same
    # setting from the file (argparse's normal set_defaults()-vs-explicit-arg precedence).
    config_pre, _ = p.parse_known_args()
    if config_pre.config:
        try:
            file_defaults = config_file.load_config(config_pre.config)
        except Exception as exc:
            p.error(f"--config {config_pre.config!r}: {exc}")
        p.set_defaults(**file_defaults)

    args = p.parse_args()

    if not args.password:
        p.error("--password is required (via --password, ABUS_PASSWORD, or the config file's camera.password)")
    if bool(args.auth_username) != bool(args.auth_password):
        p.error("--auth-username and --auth-password must be given together")

    logutil.set_debug(args.debug)

    session = CameraSession(args.password, bind_ip=args.bind_ip, did=args.did,
                             target_ip=args.target_ip, discover_timeout=args.timeout)
    session.skip_video_start = args.skip_video_start
    session.skip_audio_start = args.skip_audio_start
    try:
        if not args.no_ptz_http:
            # Started BEFORE discover/auth (rather than after) so /health is reachable
            # immediately at process startup - it reports status="starting" until discover()/
            # auth() succeed, which is what makes it usable as a Docker HEALTHCHECK across the
            # whole container lifecycle instead of only once streaming is already up.
            # Holder for the not-yet-created GstAbusRtspServer instance (it's only built
            # inside read_stream(), called below, after this server is already listening) -
            # lets the PTZ REST server's /snapshot route look it up lazily, per request.
            gst_server_ref: dict = {"server": None}
            start_ptz_http_server(session, args.ptz_http_host, args.ptz_http_port,
                                   username=args.auth_username, password=args.auth_password,
                                   gst_server_ref=gst_server_ref)
        else:
            gst_server_ref = None

        log("[discover] searching for ABUS camera on LAN")
        if not session.discover(target_ip=args.target_ip, timeout=args.timeout):
            log("[discover] no camera found on the LAN")
            return 1

        session.alive_handshake()

        log("[auth] starting challenge/response handshake")
        if not session.auth(timeout=args.timeout * 2):
            return 1

        if not args.no_onvif:
            # ONVIF XAddrs/GetStreamUri must advertise a real, reachable LAN address - the
            # RTSP URL's default host (0.0.0.0) only makes sense for OUR OWN listen socket.
            advertised_rtsp_url = args.rtsp_url
            parsed_rtsp = urllib.parse.urlsplit(args.rtsp_url)
            if parsed_rtsp.hostname in (None, "0.0.0.0"):
                netloc = f"{session.bind_ip}:{parsed_rtsp.port or 8554}"
                advertised_rtsp_url = urllib.parse.urlunsplit(parsed_rtsp._replace(netloc=netloc))
            if args.auth_username:
                # Embed credentials in the advertised URI so clients that just use whatever
                # GetStreamUri returns (rather than prompting the user separately) still work.
                parsed_advertised = urllib.parse.urlsplit(advertised_rtsp_url)
                netloc = f"{args.auth_username}:{args.auth_password}@{parsed_advertised.netloc}"
                advertised_rtsp_url = urllib.parse.urlunsplit(parsed_advertised._replace(netloc=netloc))
            onvif_server.start_onvif_server(
                host=session.bind_ip, http_port=args.onvif_port, rtsp_url=advertised_rtsp_url,
                move_ptz=session.move_ptz, goto_preset=session.goto_preset,
                serial_number=session.did or "ABUS-BRIDGE",
                ws_discovery=not args.no_ws_discovery, max_step_per_move=args.onvif_ptz_step,
                username=args.auth_username, password=args.auth_password, log=log)

        if args.dump_raw:
            # dump_stream() is a standalone diagnostic (no RTSP clients involved), so start
            # the camera stream immediately as before.
            session.preflight()
            session.start_video(resolution=args.resolution)
            time.sleep(0.01)
            session.start_audio()
            session.dump_stream(args.dump_raw)
        else:
            # read_stream() only requests video/audio once an RTSP client actually connects,
            # and stops it again when the last one disconnects - see read_stream() docstring.
            log(f"[rtsp] streaming to {args.rtsp_url}")
            session.read_stream(args.rtsp_url, resolution=args.resolution,
                                 auth_username=args.auth_username, auth_password=args.auth_password,
                                 enable_audio=not args.disable_audio, gst_server_ref=gst_server_ref)
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.argv.remove("--worker")
        try:
            raise SystemExit(main())
        except KeyboardInterrupt:
            log("\n[stop] interrupted")
            raise SystemExit(130)
    else:
        try:
            raise SystemExit(supervisor.supervise())
        except KeyboardInterrupt:
            log("\n[stop] interrupted")
            raise SystemExit(130)
