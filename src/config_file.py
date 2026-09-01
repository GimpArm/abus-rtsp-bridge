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

See config.example.yaml for a complete, runnable example.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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


def load_config(path: str) -> dict[str, Any]:
    """Parse a structured YAML (or JSON) config file into a flat dict of
    argparse-dest-name -> value, suitable for `parser.set_defaults(**result)`. Only keys
    actually present in the file are included, so anything omitted keeps its CLI default.
    Raises ConfigError on an unrecognized section/key or the wrong shape, so a typo in a
    Home Assistant add-on's options doesn't silently do nothing."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level config must be a mapping, got {type(raw).__name__}")

    unknown_sections = set(raw) - set(_FIELDS)
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
