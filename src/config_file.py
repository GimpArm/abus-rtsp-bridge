#!/usr/bin/env python3
"""Structured YAML configuration file support - an alternative to passing every setting as a
CLI flag/environment variable. Primarily for wrapping this bridge in something that only
speaks a single config file (e.g. a Home Assistant add-on's options), but usable standalone
via --config / ABUS_CONFIG_FILE too. JSON is valid YAML, so a Home Assistant add-on's own
/data/options.json also loads correctly through this same code path.

Schema (every section/key is optional; anything omitted keeps its normal CLI/argparse
default). Unlike ABUS_* environment variables (one flat key per setting), this groups
related settings together:

    camera:
      did: ABCD-123456-EFGHI      # --did
      password: secret            # --password (required, here or on the CLI)
      bind_ip: 192.168.1.10       # --bind-ip
      target_ip: 192.168.1.64     # --target-ip
      timeout: 5.0                # --timeout (seconds)
    rtsp:
      url: rtsp://0.0.0.0:8554/abus   # --rtsp-url
      resolution: 0                    # 0=bySetting 1=fullHD 2=HD 3=SD 4=automatic
      disable_audio: false
    ptz:
      enabled: true              # false == --no-ptz-http
      http_host: 0.0.0.0
      http_port: 8080
    onvif:
      enabled: true              # false == --no-onvif
      ws_discovery: true          # false == --no-ws-discovery
      port: 8000
      ptz_step: 2
    auth:
      username: null             # both username+password must be set together, or neither
      password: null
    diagnostics:
      debug: false
      dump_raw: null             # path, or omit/null to disable
      skip_video_start: false
      skip_audio_start: false

Multiple cameras: add a `cameras` list. The sections above then act as defaults shared by every
camera, and each list entry overrides them. The bridge runs one supervised worker per camera:

    cameras:
      - name: front_door         # required, unique, [A-Za-z0-9_-]; used in logs and the default RTSP path
        did: ABCD-123456-EFGHI   # did and/or target_ip is required per camera, so discovery is unambiguous
        password: secret         # defaults to camera.password
        # optional overrides: bind_ip, target_ip, timeout, resolution, disable_audio,
        #                     rtsp_url, onvif_port

Unless overridden, camera number N (0-based) gets RTSP port 8554+N with path /<name> and ONVIF
port 8000+N (the offset is applied to the shared rtsp/onvif values). The PTZ/snapshot/health REST
API is served once, on the shared ptz.http_host/http_port, as /<name>/ptz/<direction> etc. (see
rest_gateway.py); each camera's own REST server only listens on loopback, port 8081+N.

See config.example.yaml for a complete, runnable example.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_RTSP_URL = "rtsp://0.0.0.0:8554/abus"
DEFAULT_RTSP_PORT = 8554
DEFAULT_PTZ_HTTP_HOST = "0.0.0.0"
DEFAULT_PTZ_HTTP_PORT = 8080
DEFAULT_ONVIF_PORT = 8000

_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CAMERA_KEYS = frozenset({"did", "password", "bind_ip", "target_ip", "timeout", "rtsp_url",
                          "resolution", "disable_audio", "onvif_port"})


class ConfigError(ValueError):
    """Raised for a structurally invalid config file (wrong shape, unknown key)."""


# (section, key) -> (argparse dest, invert-boolean)
_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "camera": [("did", "did", False), ("password", "password", False),
               ("bind_ip", "bind_ip", False), ("target_ip", "target_ip", False),
               ("timeout", "timeout", False)],
    "rtsp": [("url", "rtsp_url", False), ("resolution", "resolution", False),
             ("disable_audio", "disable_audio", False)],
    "ptz": [("enabled", "no_ptz_http", True), ("http_host", "ptz_http_host", False),
            ("http_port", "ptz_http_port", False)],
    "onvif": [("enabled", "no_onvif", True), ("ws_discovery", "no_ws_discovery", True),
              ("port", "onvif_port", False), ("ptz_step", "onvif_ptz_step", False)],
    "auth": [("username", "auth_username", False), ("password", "auth_password", False)],
    "diagnostics": [("debug", "debug", False), ("dump_raw", "dump_raw", False),
                     ("skip_video_start", "skip_video_start", False),
                     ("skip_audio_start", "skip_audio_start", False)],
}


def _read_raw(path: str) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level config must be a mapping, got {type(raw).__name__}")
    return raw


def _shared_settings(raw: dict[str, Any]) -> dict[str, Any]:
    unknown_sections = set(raw) - set(_FIELDS) - {"cameras"}
    if unknown_sections:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown_sections))}")

    result: dict[str, Any] = {}
    for section, fields in _FIELDS.items():
        section_data = raw.get(section)
        if section_data is None:
            continue
        if not isinstance(section_data, dict):
            raise ConfigError(f"'{section}' must be a mapping, got {type(section_data).__name__}")
        known_keys = {key for key, _dest, _invert in fields}
        unknown_keys = set(section_data) - known_keys
        if unknown_keys:
            raise ConfigError(f"unknown key(s) in '{section}': {', '.join(sorted(unknown_keys))}")
        for key, dest, invert in fields:
            if key not in section_data:
                continue
            value = section_data[key]
            result[dest] = (not value) if invert else value

    return result


def _offset_rtsp_url(base_url: str, name: str, index: int) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    netloc = f"{parsed.hostname or '0.0.0.0'}:{(parsed.port or DEFAULT_RTSP_PORT) + index}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc, path=f"/{name}"))


def _require_unique(label: str, values: dict[str, Any]) -> None:
    seen: dict[Any, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if value in seen:
            raise ConfigError(f"cameras '{seen[value]}' and '{name}' share the same {label} ({value})")
        seen[value] = name


def _resolve_cameras(raw: dict[str, Any], shared: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge each `cameras` entry over the shared settings (name -> flat argparse-dest dict),
    deriving per-camera RTSP/ONVIF ports where the entry doesn't set them, and reject
    anything that would make two cameras collide. Each camera's own REST server is moved to
    loopback (port shared+1+N) since rest_gateway.py is the public REST entry point."""
    entries = raw.get("cameras") or []
    if not isinstance(entries, list):
        raise ConfigError(f"'cameras' must be a list, got {type(entries).__name__}")

    cameras: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"cameras[{index}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not _CAMERA_NAME_RE.match(name):
            raise ConfigError(f"cameras[{index}].name is required and may only contain letters, digits, '_' and '-'")
        if name in cameras:
            raise ConfigError(f"duplicate camera name '{name}'")
        unknown_keys = set(entry) - _CAMERA_KEYS - {"name"}
        if unknown_keys:
            raise ConfigError(f"unknown key(s) in camera '{name}': {', '.join(sorted(unknown_keys))}")

        overrides = {key: value for key, value in entry.items() if key != "name" and value is not None}
        settings = {**shared, **overrides}
        if "rtsp_url" not in overrides:
            settings["rtsp_url"] = _offset_rtsp_url(shared.get("rtsp_url", DEFAULT_RTSP_URL), name, index)
        settings["ptz_http_host"] = "127.0.0.1"
        settings["ptz_http_port"] = shared.get("ptz_http_port", DEFAULT_PTZ_HTTP_PORT) + 1 + index
        if "onvif_port" not in overrides:
            settings["onvif_port"] = shared.get("onvif_port", DEFAULT_ONVIF_PORT) + index
        cameras[name] = settings

    if len(cameras) > 1:
        for name, settings in cameras.items():
            if not settings.get("did") and not settings.get("target_ip"):
                raise ConfigError(f"camera '{name}' needs a did or target_ip so discovery picks the right camera")
        _require_unique("did", {n: s.get("did") for n, s in cameras.items()})
        _require_unique("target_ip", {n: s.get("target_ip") for n, s in cameras.items()})
        _require_unique("RTSP port", {n: urllib.parse.urlsplit(s["rtsp_url"]).port or DEFAULT_RTSP_PORT
                                      for n, s in cameras.items()})
        _require_unique("ONVIF port", {n: s["onvif_port"] for n, s in cameras.items()
                                       if not s.get("no_onvif")})
    return cameras


def list_cameras(path: str) -> list[str]:
    """Camera names from the file's `cameras` list, or [] for a plain single-camera config.
    Raises ConfigError for an invalid config, so the supervisor can fail fast before
    starting any worker."""
    raw = _read_raw(path)
    return list(_resolve_cameras(raw, _shared_settings(raw)))


def rest_gateway_config(path: str) -> Optional[dict[str, Any]]:
    """Settings for the single REST gateway (host, port, shared credentials and each camera's
    loopback REST port), or None for a single-camera config or when the REST server is disabled."""
    raw = _read_raw(path)
    shared = _shared_settings(raw)
    cameras = _resolve_cameras(raw, shared)
    if not cameras or shared.get("no_ptz_http"):
        return None
    return {"host": shared.get("ptz_http_host", DEFAULT_PTZ_HTTP_HOST),
            "port": shared.get("ptz_http_port", DEFAULT_PTZ_HTTP_PORT),
            "username": shared.get("auth_username"), "password": shared.get("auth_password"),
            "camera_ports": {name: s["ptz_http_port"] for name, s in cameras.items()}}


def load_config(path: str, camera: Optional[str] = None) -> dict[str, Any]:
    """Parse a structured YAML (or JSON) config file into a flat dict of
    argparse-dest-name -> value, suitable for `parser.set_defaults(**result)`. Only keys
    actually present in the file are included, so anything omitted keeps its CLI default.
    For a multi-camera file, `camera` selects which entry's settings (shared defaults plus
    that camera's overrides) to return. Raises ConfigError on an unrecognized section/key or
    the wrong shape, so a typo in a Home Assistant add-on's options doesn't silently do
    nothing."""
    raw = _read_raw(path)
    shared = _shared_settings(raw)
    cameras = _resolve_cameras(raw, shared)
    if not cameras:
        if camera:
            raise ConfigError(f"--camera {camera!r} given, but the config has no 'cameras' list")
        return shared
    if camera is None:
        raise ConfigError("the config defines 'cameras' - select one with --camera NAME "
                          "(the supervisor does this automatically)")
    if camera not in cameras:
        raise ConfigError(f"unknown camera {camera!r}; configured: {', '.join(cameras)}")
    return cameras[camera]
