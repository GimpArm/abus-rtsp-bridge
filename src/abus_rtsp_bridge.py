#!/usr/bin/env python3
"""ABUS LAN camera to RTSP bridge - CLI entry point.

This is deliberately just orchestration now - the actual protocol/session logic lives in:
  - wire_protocol.py       - raw F1/D0/D1/DRW framing (see its docstring for the wire format)
  - crypto_utils.py        - AES-128-ECB helpers
  - ioctl_protocol.py      - IOCTL command layer (video/audio/PTZ/light/siren)
  - frame_reassembler.py   - AV data-channel reconstruction into H.264/PCM access units
  - camera_session.py      - CameraSession: discover -> alive -> auth -> stream lifecycle
  - ptz_rest_api.py        - PTZ/snapshot/health REST server
  - rest_gateway.py        - single REST entry point in front of all cameras' REST servers
  - rtsp_gateway.py        - single RTSP port in front of all cameras' RTSP servers
  - gst_rtsp_server.py     - GStreamer decode/re-encode + RTSP serving
  - onvif_server.py        - ONVIF device/media/PTZ SOAP service
  - p2p_handshake.py       - CS2/iLnkP2P rendezvous protocol (LAN discovery fallback)
  - supervisor.py          - self-restarting process wrapper (one worker process per camera)
  - config_file.py         - YAML config (the `cameras` list plus shared sections)
See abus-protocol.md (repo memory) for the full reverse-engineering history.

Run:
python src/abus_rtsp_bridge.py --config config.yaml

Every camera is an entry of the config file's `cameras` list (see config.example.yaml). The
supervisor starts one worker per camera, each started internally with `--camera NAME`.

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
import logutil
import onvif_server
import supervisor
from camera_session import CameraSession
from logutil import log
from ptz_rest_api import start_ptz_http_server
from rest_gateway import start_rest_gateway
from rtsp_gateway import start_rtsp_gateway


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
    p.add_argument("--config", default=os.environ.get("ABUS_CONFIG_FILE"), help="Path to the YAML (or JSON) config file - see config_file.py's module docstring / config.example.yaml for the schema. Also settable via ABUS_CONFIG_FILE.")
    p.add_argument("--camera", default=None, help="Internal: the camera (from the config's `cameras` list) this worker runs. Set by the supervisor.")
    cli = p.parse_args()
    if not cli.config:
        p.error("--config (or ABUS_CONFIG_FILE) is required")
    if not cli.camera:
        p.error("--camera is required for a worker (the supervisor sets it)")
    try:
        args = argparse.Namespace(camera=cli.camera, **config_file.load_config(cli.config, cli.camera))
    except Exception as exc:
        p.error(f"--config {cli.config!r}: {exc}")
    rtsp_url = args.rtsp_url

    logutil.set_debug(args.debug)
    logutil.set_prefix(args.camera)

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
            advertised_rtsp_url = rtsp_url
            parsed_rtsp = urllib.parse.urlsplit(rtsp_url)
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
            parsed_rtsp = urllib.parse.urlsplit(rtsp_url)
            bind_rtsp_url = urllib.parse.urlunsplit(parsed_rtsp._replace(netloc=f"127.0.0.1:{args.rtsp_loopback_port}"))
            log(f"[rtsp] streaming to {rtsp_url} (internal worker port {bind_rtsp_url}, behind the RTSP gateway)")
            session.read_stream(bind_rtsp_url, resolution=args.resolution,
                                 auth_username=args.auth_username, auth_password=args.auth_password,
                                 enable_audio=not args.disable_audio, gst_server_ref=gst_server_ref,
                                 enable_talkback=not args.disable_talkback,
                                 keep_stream_open=args.keep_stream_open)
    finally:
        session.close()

    return 0


def _launch_cameras() -> list[tuple[str, list[str]]]:
    """Start the shared RTSP and REST gateways and return one (label, extra argv) worker per camera
    in the config file."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--config", default=os.environ.get("ABUS_CONFIG_FILE"))
    known, _ = pre.parse_known_args()
    if not known.config:
        raise config_file.ConfigError("--config (or ABUS_CONFIG_FILE) is required")
    names = config_file.list_cameras(known.config)
    start_rtsp_gateway(**config_file.rtsp_gateway_config(known.config))
    rest_gateway = config_file.rest_gateway_config(known.config)
    if rest_gateway:
        start_rest_gateway(**rest_gateway)
    return [(name, ["--camera", name]) for name in names]


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
            worker_args = _launch_cameras()
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        try:
            raise SystemExit(supervisor.supervise(worker_args))
        except KeyboardInterrupt:
            log("\n[stop] interrupted")
            raise SystemExit(130)
