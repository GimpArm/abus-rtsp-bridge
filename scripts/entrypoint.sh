#!/bin/sh
# Maps ABUS_* environment variables to abus_rtsp_bridge.py CLI flags.
# Any extra arguments passed to `docker run <image> ...` are forwarded last,
# so they override the equivalent env-derived flag (argparse keeps the last value).
set -e

# The password can come from ABUS_PASSWORD OR from a config file's camera.password (e.g. a
# Home Assistant add-on passing ABUS_CONFIG_FILE=/data/options.json) - only require the env
# var here if no config file is set; abus_rtsp_bridge.py does the final validation either way.
if [ -z "$ABUS_CONFIG_FILE" ] && [ -z "$ABUS_PASSWORD" ]; then
    echo "Either ABUS_PASSWORD or ABUS_CONFIG_FILE (with camera.password set) is required" >&2
    exit 1
fi

user_args="$@"
set --
[ -n "$ABUS_CONFIG_FILE" ] && set -- "$@" --config "$ABUS_CONFIG_FILE"
[ -n "$ABUS_PASSWORD" ] && set -- "$@" --password "$ABUS_PASSWORD"
[ -n "$ABUS_DID" ] && set -- "$@" --did "$ABUS_DID"
[ -n "$ABUS_BIND_IP" ] && set -- "$@" --bind-ip "$ABUS_BIND_IP"
[ -n "$ABUS_TARGET_IP" ] && set -- "$@" --target-ip "$ABUS_TARGET_IP"
[ -n "$ABUS_RTSP_URL" ] && set -- "$@" --rtsp-url "$ABUS_RTSP_URL"
[ -n "$ABUS_TIMEOUT" ] && set -- "$@" --timeout "$ABUS_TIMEOUT"
[ -n "$ABUS_RESOLUTION" ] && set -- "$@" --resolution "$ABUS_RESOLUTION"
[ -n "$ABUS_PTZ_HTTP_PORT" ] && set -- "$@" --ptz-http-port "$ABUS_PTZ_HTTP_PORT"
[ -n "$ABUS_PTZ_HTTP_HOST" ] && set -- "$@" --ptz-http-host "$ABUS_PTZ_HTTP_HOST"
[ -n "$ABUS_NO_PTZ_HTTP" ] && set -- "$@" --no-ptz-http
[ -n "$ABUS_ONVIF_PORT" ] && set -- "$@" --onvif-port "$ABUS_ONVIF_PORT"
[ -n "$ABUS_ONVIF_PTZ_STEP" ] && set -- "$@" --onvif-ptz-step "$ABUS_ONVIF_PTZ_STEP"
[ -n "$ABUS_NO_ONVIF" ] && set -- "$@" --no-onvif
[ -n "$ABUS_NO_WS_DISCOVERY" ] && set -- "$@" --no-ws-discovery
[ -n "$ABUS_AUTH_USERNAME" ] && set -- "$@" --auth-username "$ABUS_AUTH_USERNAME"
[ -n "$ABUS_AUTH_PASSWORD" ] && set -- "$@" --auth-password "$ABUS_AUTH_PASSWORD"
[ -n "$ABUS_DEBUG" ] && set -- "$@" --debug

exec python3 /app/src/abus_rtsp_bridge.py "$@" $user_args
