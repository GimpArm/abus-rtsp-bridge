# Debian-based image for the ABUS LAN camera -> RTSP bridge.
#
# UDP broadcast discovery needs direct access to the camera's physical LAN segment.
# This only works with `--network host` on a real Linux host (bare metal, Raspberry
# Pi, or a Linux VM with a *bridged* NIC on that LAN).
#
# Docker Desktop on Windows/Mac (WSL2 or Hyper-V backend) does NOT expose the
# physical LAN even with --network host: the container stays behind Docker
# Desktop's internal virtual network, so the camera is unreachable. Run this on a
# real Linux host on the same LAN, or run src/abus_rtsp_bridge.py directly
# without Docker.
#
# Configure via environment variables (see scripts/entrypoint.sh):
#   ABUS_PASSWORD       (required) camera view password / security code
#   ABUS_DID            optional DID to match, e.g. ABCD-123456-EFGHI
#   ABUS_BIND_IP        optional local IPv4 to bind the discovery socket to
#   ABUS_TARGET_IP      optional known camera IP on the same LAN
#   ABUS_RTSP_URL       destination RTSP URL (default rtsp://0.0.0.0:8554/abus)
#   ABUS_TIMEOUT        discovery timeout in seconds (default 5.0)
#   ABUS_RESOLUTION     video quality: 0=bySetting 1=fullHD 2=HD 3=SD 4=auto
#   ABUS_PTZ_HTTP_PORT  PTZ REST server port (default 8080); ABUS_NO_PTZ_HTTP=1 disables it
#   ABUS_ONVIF_PORT     ONVIF device/media/PTZ SOAP port (default 8000)
#   ABUS_ONVIF_PTZ_STEP how far one ONVIF PTZ click moves the camera (default 2)
#   ABUS_NO_ONVIF       set to disable the ONVIF service entirely
#   ABUS_NO_WS_DISCOVERY set to disable the WS-Discovery multicast responder only
#   ABUS_AUTH_USERNAME / ABUS_AUTH_PASSWORD   optional, both required together - HTTP/RTSP
#                       Basic auth shared by the RTSP stream, ONVIF service, and PTZ REST
#   ABUS_DEBUG          set to enable verbose per-packet/per-frame diagnostic logging
#
# Example (on a Linux host that is actually on the camera's LAN):
#   docker build -t abus-rtsp-bridge .
#   docker run --rm --network host \
#     -e ABUS_PASSWORD=<password> \
#     -e ABUS_DID=<did> \
#     -e ABUS_BIND_IP=<bind-ip> \
#     -e ABUS_TARGET_IP=<target-ip> \
#     abus-rtsp-bridge

# =========================================================
# STAGE 1: Builder
# Compiles wheels for PyGObject and pycryptodome with full build tooling.
# =========================================================
FROM python:3.14-slim-trixie AS builder

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        pkg-config \
        python3-dev \
        libgirepository-2.0-dev \
        libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels PyGObject pycryptodome


# =========================================================
# STAGE 2: Runner
# Slim final stage with non-dev runtime shared libraries.
# =========================================================
FROM python:3.14-slim-trixie AS runner

RUN apt-get update && apt-get install --no-install-recommends -y \
        tini \
        libgirepository-2.0-0 \
        libcairo2 \
        gir1.2-glib-2.0 \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-rtsp-server-1.0 \
        gstreamer1.0-tools \
        gstreamer1.0-libav \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
    && rm -rf /var/lib/apt/lists/*

# Install pre-built wheels from builder stage
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

WORKDIR /app
COPY src/abus_rtsp_bridge.py ./src/abus_rtsp_bridge.py
COPY src/logutil.py ./src/logutil.py
COPY src/wire_protocol.py ./src/wire_protocol.py
COPY src/crypto_utils.py ./src/crypto_utils.py
COPY src/ioctl_protocol.py ./src/ioctl_protocol.py
COPY src/frame_reassembler.py ./src/frame_reassembler.py
COPY src/camera_session.py ./src/camera_session.py
COPY src/ptz_rest_api.py ./src/ptz_rest_api.py
COPY src/supervisor.py ./src/supervisor.py
COPY src/gst_rtsp_server.py ./src/gst_rtsp_server.py
COPY src/onvif_server.py ./src/onvif_server.py
COPY src/p2p_handshake.py ./src/p2p_handshake.py
COPY src/http_basic_auth.py ./src/http_basic_auth.py
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
COPY scripts/healthcheck.py /healthcheck.py

ENV ABUS_RTSP_URL="rtsp://0.0.0.0:8554/abus" \
    ABUS_TIMEOUT="5.0"

EXPOSE 8554 8080 8000
EXPOSE 3702/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python3 /healthcheck.py

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]