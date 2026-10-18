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
      bind_ip: 0.0.0.0           # listen address; serves rtsp://<bind_ip>:<port>/abus
      port: 8554
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
        # optional overrides: bind_ip, target_ip, timeout, resolution, disable_audio
        onvif_port: 8000         # ONVIF is off for a camera unless this is set; unique per camera
        # onvif_ws_discovery: true   # optional, only with onvif_port
        # onvif_ptz_step: 2          # optional, only with onvif_port

All cameras share one RTSP port (rtsp.bind_ip/rtsp.port) and are told apart by path:
rtsp://<host>:8554/<name>. rtsp_gateway.py listens there and forwards each connection to that
camera's worker, which only listens on loopback (port shared+1+N). All ONVIF settings are
per camera: the root `onvif` section is ignored once `cameras` is set. The PTZ/snapshot/health REST
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

from logutil import log

DEFAULT_RTSP_BIND_IP = "0.0.0.0"
DEFAULT_RTSP_PORT = 8554
DEFAULT_RTSP_PATH = "abus"
DEFAULT_PTZ_HTTP_HOST = "0.0.0.0"
DEFAULT_PTZ_HTTP_PORT = 8080
DEFAULT_ONVIF_PORT = 8000

_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CAMERA_KEYS = frozenset({"did", "password", "bind_ip", "target_ip", "timeout",
                          "resolution", "disable_audio", "onvif_port", "onvif_ws_discovery",
                          "onvif_ptz_step"})


class ConfigError(ValueError):
    """Raised for a structurally invalid config file (wrong shape, unknown key)."""


# (section, key) -> (argparse dest, invert-boolean)
_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "camera": [("did", "did", False), ("password", "password", False),
               ("bind_ip", "bind_ip", False), ("target_ip", "target_ip", False),
               ("timeout", "timeout", False)],
    "rtsp": [("bind_ip", "rtsp_bind_ip", False), ("port", "rtsp_port", False),
             ("url", "rtsp_url", False),  # legacy: only its host/port are used, see _shared_settings
             ("resolution", "resolution", False),
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

# Dests that only come from a per-camera entry in multi-camera mode, never from shared sections.
_ONVIF_SETTINGS = frozenset({dest for _key, dest, _invert in _FIELDS["onvif"]}
                            | {"onvif_ws_discovery"})


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

    for dest in ("rtsp_bind_ip", "rtsp_port", "rtsp_url"):
        if result.get(dest) is None:
            result.pop(dest, None)
    legacy_url = urllib.parse.urlsplit(result.pop("rtsp_url")) if "rtsp_url" in result else None
    if legacy_url:
        if legacy_url.hostname:
            result.setdefault("rtsp_bind_ip", legacy_url.hostname)
        if legacy_url.port:
            result.setdefault("rtsp_port", legacy_url.port)

    return result


def _require_unique(label: str, values: dict[str, Any]) -> None:
    seen: dict[Any, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if value in seen:
            raise ConfigError(f"cameras '{seen[value]}' and '{name}' share the same {label} ({value})")
        seen[value] = name


def _resolve_cameras(raw: dict[str, Any], shared: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge each `cameras` entry over the shared settings (name -> flat argparse-dest dict) and
    reject anything that would make two cameras collide. Shared ONVIF settings are dropped: ONVIF
    is configured per camera and is off unless the entry sets `onvif_port`. Each camera's own REST
    server is moved to loopback (port shared+1+N) since rest_gateway.py is the public REST entry
    point; likewise each camera's RTSP server gets a loopback port (`rtsp_loopback_port`) behind
    rtsp_gateway.py."""
    entries = raw.get("cameras") or []
    if not isinstance(entries, list):
        raise ConfigError(f"'cameras' must be a list, got {type(entries).__name__}")

    cameras: dict[str, dict[str, Any]] = {}
    rtsp_ip = shared.get("rtsp_bind_ip", DEFAULT_RTSP_BIND_IP)
    rtsp_port = shared.get("rtsp_port", DEFAULT_RTSP_PORT)
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
        settings = {k: v for k, v in {**shared, **overrides}.items() if k not in _ONVIF_SETTINGS}
        settings["rtsp_url"] = f"rtsp://{rtsp_ip}:{rtsp_port}/{name}"
        settings["rtsp_loopback_port"] = rtsp_port + 1 + index
        settings["ptz_http_host"] = "127.0.0.1"
        settings["ptz_http_port"] = shared.get("ptz_http_port", DEFAULT_PTZ_HTTP_PORT) + 1 + index
        if "onvif_port" in overrides:
            settings["onvif_port"] = overrides["onvif_port"]
            if "onvif_ptz_step" in overrides:
                settings["onvif_ptz_step"] = overrides["onvif_ptz_step"]
            if "onvif_ws_discovery" in overrides:
                settings["no_ws_discovery"] = not overrides["onvif_ws_discovery"]
        else:
            settings["no_onvif"] = True
        cameras[name] = settings

    if len(cameras) > 1:
        for name, settings in cameras.items():
            if not settings.get("did") and not settings.get("target_ip"):
                raise ConfigError(f"camera '{name}' needs a did or target_ip so discovery picks the right camera")
        _require_unique("did", {n: s.get("did") for n, s in cameras.items()})
        _require_unique("target_ip", {n: s.get("target_ip") for n, s in cameras.items()})
        _require_unique("ONVIF port", {n: s["onvif_port"] for n, s in cameras.items()
                                       if not s.get("no_onvif")})
    return cameras


def list_cameras(path: str) -> list[str]:
    """Camera names from the file's `cameras` list, or [] for a plain single-camera config.
    Raises ConfigError for an invalid config, so the supervisor can fail fast before
    starting any worker."""
    raw = _read_raw(path)
    shared = _shared_settings(raw)
    names = list(_resolve_cameras(raw, shared))
    if names and raw.get("onvif"):
        log("[config] the root 'onvif' section is ignored with 'cameras' - set onvif_port (and "
            "optionally onvif_ws_discovery/onvif_ptz_step) on each camera instead")
    return names


def rtsp_gateway_config(path: str) -> Optional[dict[str, Any]]:
    """Settings for the shared RTSP gateway (listen host/port and each camera's loopback RTSP
    port), or None for a single-camera config."""
    raw = _read_raw(path)
    shared = _shared_settings(raw)
    cameras = _resolve_cameras(raw, shared)
    if not cameras:
        return None
    return {"host": shared.get("rtsp_bind_ip", DEFAULT_RTSP_BIND_IP),
            "port": shared.get("rtsp_port", DEFAULT_RTSP_PORT),
            "camera_ports": {name: s["rtsp_loopback_port"] for name, s in cameras.items()}}


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
