#!/bin/sh
# Starts the bridge with its YAML config file: $ABUS_CONFIG_FILE, else Home Assistant's
# /data/options.json, else /config/config.yaml. Extra arguments passed to
# `docker run <image> ...` are forwarded as CLI flags and override the file.
set -e

if [ -z "$ABUS_CONFIG_FILE" ]; then
    if [ -f /data/options.json ]; then
        ABUS_CONFIG_FILE=/data/options.json
        # One-time upgrade: copy legacy single-camera options into the `cameras` list (best effort).
        rm -f /tmp/options.migrated.json
        python3 /app/src/ha_migrate_options.py /data/options.json /tmp/options.migrated.json || true
        if [ -f /tmp/options.migrated.json ]; then ABUS_CONFIG_FILE=/tmp/options.migrated.json; fi
    else
        ABUS_CONFIG_FILE=/config/config.yaml
    fi
fi

if [ ! -f "$ABUS_CONFIG_FILE" ]; then
    echo "Config file not found: $ABUS_CONFIG_FILE - mount one at /config/config.yaml or set ABUS_CONFIG_FILE (see config.example.yaml)" >&2
    exit 1
fi

exec python3 /app/src/abus_rtsp_bridge.py --config "$ABUS_CONFIG_FILE" "$@"
