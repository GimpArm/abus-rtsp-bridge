#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for the REST gateway's aggregated /health endpoint (unauthenticated -
see rest_gateway.py): reports unhealthy if any camera is. Finds the REST port from the same
config file the bridge uses. Requires the REST API to be enabled (ptz.enabled) - if it's
disabled there is nothing for this to check.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app/src")
import config_file

config_path = os.environ.get("ABUS_CONFIG_FILE") or (
    "/data/options.json" if os.path.exists("/data/options.json") else "/config/config.yaml")
gateway = config_file.rest_gateway_config(config_path)
if gateway is None:
    sys.exit(0)
port = gateway["port"]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        status = json.load(resp)["status"]
except Exception as exc:
    print(f"health check request on port {port} failed: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"status={status}")
sys.exit(1 if status == "unhealthy" else 0)
