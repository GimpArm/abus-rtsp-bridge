"""Tiny, dependency-free HTTP Basic auth helper shared by the PTZ REST server and the
ONVIF SOAP server (src/abus_rtsp_bridge.py, src/onvif_server.py)."""
from __future__ import annotations

import base64
import hmac
from typing import Optional


def require_basic_auth(handler, username: Optional[str], password: Optional[str],
                        realm: str = "restricted") -> bool:
    """Check the request's Authorization header against username/password. If both are
    falsy, auth is disabled and this always returns True. On missing/invalid credentials,
    sends the 401 response (with a Basic challenge) itself and returns False - the caller
    must stop processing the request in that case."""
    if not username and not password:
        return True
    ok = False
    header = handler.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[len("Basic "):].encode("ascii")).decode("utf-8")
            got_user, _, got_pass = decoded.partition(":")
            ok = (hmac.compare_digest(got_user, username or "")
                  and hmac.compare_digest(got_pass, password or ""))
        except Exception:
            ok = False
    if ok:
        return True
    body = b"Unauthorized"
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", f'Basic realm="{realm}"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return False
