#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for the PTZ REST server's /health endpoint (unauthenticated -
see _PtzRequestHandler in src/abus_rtsp_bridge.py). Reads ABUS_PTZ_HTTP_PORT at runtime so
it stays correct even if the port is overridden. Requires the PTZ REST server to be enabled
(ABUS_NO_PTZ_HTTP unset) - if it's disabled there is nothing for this to check.
"""
import json
import os
import sys
import urllib.request

port = os.environ.get("ABUS_PTZ_HTTP_PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        status = json.load(resp)["status"]
except Exception as exc:
    print(f"health check request failed: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"status={status}")
sys.exit(1 if status == "unhealthy" else 0)
