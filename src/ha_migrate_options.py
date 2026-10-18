#!/usr/bin/env python3
"""Home Assistant add-on helper: one-time migration of the legacy single-camera options
(camera.did/target_ip/bind_ip) into the `cameras` list, so upgrading users see their camera in
the multi-camera form. Persists the result through the Supervisor API and writes the migrated
options to OUT_PATH for the current run. Writes nothing (and always exits 0) when there is
nothing to migrate or saving fails, so the add-on then just starts with its original options.

Usage: ha_migrate_options.py IN_PATH OUT_PATH
"""
import copy
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"
DEFAULT_CAMERA_NAME = "abus"
DEFAULT_ONVIF_PORT = 8000
_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _camera_name(options: dict) -> str:
    # The camera is served at rtsp://host:port/<name>, so reuse the old URL's path to keep it working.
    path = urllib.parse.urlsplit((options.get("rtsp") or {}).get("url") or "").path.strip("/")
    return path if _CAMERA_NAME_RE.match(path) else DEFAULT_CAMERA_NAME


def migrate(options: dict) -> dict | None:
    """Return the options with the single camera moved into `cameras`, or None if not needed."""
    if options.get("cameras"):
        return None
    camera = options.get("camera") or {}
    if not camera.get("did") and not camera.get("target_ip"):
        return None

    migrated = copy.deepcopy(options)
    entry = {"name": _camera_name(options)}
    for key in ("did", "target_ip", "bind_ip"):
        value = migrated["camera"].pop(key, None)
        if value:
            entry[key] = value
    migrated["cameras"] = [entry]
    # ONVIF is per camera (and opt-in) in multi-camera mode; carry the old root settings over.
    onvif = migrated.get("onvif") or {}
    if onvif.get("enabled", True):
        entry["onvif_port"] = onvif.get("port") or DEFAULT_ONVIF_PORT
        if onvif.get("ws_discovery") is not None:
            entry["onvif_ws_discovery"] = onvif["ws_discovery"]
        if onvif.get("ptz_step") is not None:
            entry["onvif_ptz_step"] = onvif["ptz_step"]
    migrated["onvif"] = {}
    # The root RTSP URL becomes just a bind address/port; the path is now the camera name.
    rtsp = migrated["rtsp"] = migrated.get("rtsp") or {}
    parsed_url = urllib.parse.urlsplit(rtsp.pop("url", None) or "")
    if parsed_url.hostname:
        rtsp.setdefault("bind_ip", parsed_url.hostname)
    if parsed_url.port:
        rtsp.setdefault("port", parsed_url.port)
    return migrated


def save(options: dict, token: str) -> None:
    request = urllib.request.Request(
        SUPERVISOR_OPTIONS_URL, data=json.dumps({"options": options}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def main() -> None:
    in_path, out_path = sys.argv[1:3]
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    with open(in_path, encoding="utf-8") as f:
        options = json.load(f)
    migrated = migrate(options)
    if migrated is None:
        return
    try:
        save(migrated, token)
    except Exception as exc:
        print(f"[migrate] could not save migrated options, keeping legacy single-camera config: {exc}",
              file=sys.stderr)
        return
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(migrated, f)
    entry_name = migrated["cameras"][0]["name"]
    print(f"[migrate] moved single-camera settings into cameras[0] ('{entry_name}')")


if __name__ == "__main__":
    main()
