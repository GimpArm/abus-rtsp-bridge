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
# Configure with a YAML file (see config.example.yaml), mounted at /config/config.yaml or
# pointed to by ABUS_CONFIG_FILE (see scripts/entrypoint.sh).
#
# Example (on a Linux host that is actually on the camera's LAN):
#   docker build -t abus-rtsp-bridge .
#   docker run --rm --network host \
#     -v ./config.yaml:/config/config.yaml:ro \
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
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels PyGObject pycryptodome PyYAML


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
COPY src/*.py ./src/
COPY scripts/* /
RUN chmod +x /entrypoint.sh

EXPOSE 8554 8080 8000
EXPOSE 3702/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python3 /healthcheck.py

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]