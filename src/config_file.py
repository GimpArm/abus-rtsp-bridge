#!/usr/bin/env python3
"""YAML configuration file support - the only way to configure the bridge. Every camera is an
entry of the `cameras` list; the other sections are defaults shared by all cameras. A Home
Assistant add-on's own /data/options.json also loads through this same code path (JSON is valid
YAML).

Schema (see config.example.yaml for a complete example):

    cameras:                       # required, at least one
      - name: front_door           # required, unique, [A-Za-z0-9_-]; used in logs and as the RTSP path
        did: ABCD-123456-EFGHI     # did and/or target_ip is required per camera when there are several
        password: secret           # required
        # optional: bind_ip, target_ip, timeout (discovery seconds, default 5), resolution,
        #           disable_audio, disable_talkback, keep_stream_open (these default to the rtsp section)
        # disabled: true skips the camera entirely (default false)
        # ONVIF is off for a camera unless onvif_port is set (unique per camera); then optionally
        # onvif_ws_discovery (default true) and onvif_ptz_step (default 2):
        # onvif_port: 8000
    rtsp:                          # shared
      bind_ip: 0.0.0.0             # listen address; serves rtsp://<bind_ip>:<port>/<camera name>
      port: 8554
      resolution: 0                # 0=bySetting 1=fullHD 2=HD 3=SD 4=automatic
      disable_audio: false
      disable_talkback: false      # true == no ONVIF RTSP audio backchannel
      keep_stream_open: false      # true == keep the camera streaming after the last RTSP client leaves
    ptz:                           # shared REST API (served as /<camera name>/ptz/<direction> etc.)
      enabled: true
      http_host: 0.0.0.0
      http_port: 8080
    auth:
      username: null               # both username+password must be set together, or neither
      password: null
    diagnostics:
      debug: false
      dump_raw: null               # path, or omit/null to disable
      skip_video_start: false
      skip_audio_start: false

The bridge runs one supervised worker per camera. All cameras share one RTSP port
(rtsp.bind_ip/rtsp.port) and are told apart by path: rtsp_gateway.py listens there and forwards each
connection to that camera's worker, which only listens on loopback (port rtsp.port+1+N for camera
N). Likewise the REST API is served once, on ptz.http_host/http_port, by rest_gateway.py, and each
camera's own REST server only listens on loopback, port ptz.http_port+1+N. ONVIF is per camera.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

import ioctl_protocol as ioctl

DEFAULT_RTSP_BIND_IP = "0.0.0.0"
DEFAULT_RTSP_PORT = 8554
DEFAULT_PTZ_HTTP_HOST = "0.0.0.0"
DEFAULT_PTZ_HTTP_PORT = 8080

_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CAMERA_KEYS = frozenset({"did", "password", "bind_ip", "target_ip", "timeout",
                          "resolution", "disable_audio", "disable_talkback", "keep_stream_open",
                          "onvif_port", "onvif_ws_discovery", "onvif_ptz_step", "disabled"})

# Every setting a worker reads, with the value used when the config doesn't set it.
_DEFAULTS: dict[str, Any] = {
    "did": None, "password": None, "bind_ip": None, "target_ip": None, "timeout": 5.0,
    "resolution": ioctl.QUALITY_BY_SETTING, "disable_audio": False, "disable_talkback": False,
    "keep_stream_open": False,
    "rtsp_bind_ip": DEFAULT_RTSP_BIND_IP, "rtsp_port": DEFAULT_RTSP_PORT,
    "no_ptz_http": False, "ptz_http_host": DEFAULT_PTZ_HTTP_HOST, "ptz_http_port": DEFAULT_PTZ_HTTP_PORT,
    "no_onvif": True, "onvif_port": None, "no_ws_discovery": False, "onvif_ptz_step": 2,
    "auth_username": None, "auth_password": None,
    "debug": False, "dump_raw": None, "skip_video_start": False, "skip_audio_start": False,
}


class ConfigError(ValueError):
    """Raised for a structurally invalid config file (wrong shape, unknown key)."""


# Shared sections: (key, argparse-style dest, invert-boolean)
_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "rtsp": [("bind_ip", "rtsp_bind_ip", False), ("port", "rtsp_port", False),
             ("resolution", "resolution", False),
             ("disable_audio", "disable_audio", False), ("disable_talkback", "disable_talkback", False),
             ("keep_stream_open", "keep_stream_open", False)],
    "ptz": [("enabled", "no_ptz_http", True), ("http_host", "ptz_http_host", False),
            ("http_port", "ptz_http_port", False)],
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
            if value is None:
                continue
            result[dest] = (not value) if invert else value

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
    """Merge defaults, shared settings and each enabled `cameras` entry into one flat settings dict
    per camera (name -> dict keyed like _DEFAULTS) and reject anything invalid or colliding.
    Entries with `disabled: true` are skipped. ONVIF is
    off unless the entry sets `onvif_port`. Each camera's own REST and RTSP servers are moved to
    loopback ports (`ptz_http_port`, `rtsp_loopback_port`) behind rest_gateway.py/rtsp_gateway.py."""
    entries = raw.get("cameras")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("the config must define at least one camera under 'cameras'")
    if bool(shared.get("auth_username")) != bool(shared.get("auth_password")):
        raise ConfigError("auth.username and auth.password must be given together")

    cameras: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    base = {**_DEFAULTS, **shared}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"cameras[{index}] must be a mapping, got {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not _CAMERA_NAME_RE.match(name):
            raise ConfigError(f"cameras[{index}].name is required and may only contain letters, digits, '_' and '-'")
        if name in names:
            raise ConfigError(f"duplicate camera name '{name}'")
        names.add(name)
        unknown_keys = set(entry) - _CAMERA_KEYS - {"name"}
        if unknown_keys:
            raise ConfigError(f"unknown key(s) in camera '{name}': {', '.join(sorted(unknown_keys))}")
        if entry.get("disabled"):
            continue

        overrides = {key: value for key, value in entry.items()
                     if key not in ("name", "disabled") and value is not None}
        settings = {**base, **{k: v for k, v in overrides.items() if k != "onvif_ws_discovery"}}
        if not settings["password"]:
            raise ConfigError(f"camera '{name}' needs a password")
        settings["rtsp_url"] = f"rtsp://{base['rtsp_bind_ip']}:{base['rtsp_port']}/{name}"
        settings["rtsp_loopback_port"] = base["rtsp_port"] + 1 + index
        settings["ptz_http_host"] = "127.0.0.1"
        settings["ptz_http_port"] = base["ptz_http_port"] + 1 + index
        settings["no_onvif"] = "onvif_port" not in overrides
        if "onvif_ws_discovery" in overrides:
            settings["no_ws_discovery"] = not overrides["onvif_ws_discovery"]
        cameras[name] = settings

    if not cameras:
        raise ConfigError("all cameras are disabled - enable at least one")
    if len(cameras) > 1:
        for name, settings in cameras.items():
            if not settings["did"] and not settings["target_ip"]:
                raise ConfigError(f"camera '{name}' needs a did or target_ip so discovery picks the right camera")
        _require_unique("did", {n: s["did"] for n, s in cameras.items()})
        _require_unique("target_ip", {n: s["target_ip"] for n, s in cameras.items()})
    _require_unique("ONVIF port", {n: s["onvif_port"] for n, s in cameras.items() if not s["no_onvif"]})
    return cameras


def _load_cameras(path: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    raw = _read_raw(path)
    shared = _shared_settings(raw)
    return {**_DEFAULTS, **shared}, _resolve_cameras(raw, shared)


def list_cameras(path: str) -> list[str]:
    """Camera names from the file's `cameras` list. Raises ConfigError for an invalid config, so
    the supervisor can fail fast before starting any worker."""
    return list(_load_cameras(path)[1])


def rtsp_gateway_config(path: str) -> dict[str, Any]:
    """Settings for the shared RTSP gateway: listen host/port and each camera's loopback RTSP port."""
    shared, cameras = _load_cameras(path)
    return {"host": shared["rtsp_bind_ip"], "port": shared["rtsp_port"],
            "camera_ports": {name: s["rtsp_loopback_port"] for name, s in cameras.items()}}


def rest_gateway_config(path: str) -> Optional[dict[str, Any]]:
    """Settings for the single REST gateway (host, port, shared credentials and each camera's
    loopback REST port), or None when the REST server is disabled."""
    shared, cameras = _load_cameras(path)
    if shared["no_ptz_http"]:
        return None
    return {"host": shared["ptz_http_host"], "port": shared["ptz_http_port"],
            "username": shared["auth_username"], "password": shared["auth_password"],
            "camera_ports": {name: s["ptz_http_port"] for name, s in cameras.items()}}


def load_config(path: str, camera: str) -> dict[str, Any]:
    """Settings of one camera: defaults, then the shared sections, then that camera's entry.
    Raises ConfigError on an invalid file (unknown section/key, wrong shape, missing password...),
    so a typo in a Home Assistant add-on's options doesn't silently do nothing."""
    cameras = _load_cameras(path)[1]
    if camera not in cameras:
        raise ConfigError(f"unknown camera {camera!r}; configured: {', '.join(cameras)}")
    return cameras[camera]
