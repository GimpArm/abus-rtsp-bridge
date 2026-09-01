# ABUS P2P / live-view bridge

Reverse-engineered from the wire protocol and verified against real packet captures. See
[abus-protocol.md](abus-protocol.md) for the full history and every confirmed wire-format
detail; this file just covers what's here and how to run it.

*This project is an independent open-source tool and is not affiliated, associated, authorized, endorsed by, or in any way officially connected with ABUS, or any of its subsidiaries or affiliates.
*

[![Buy Me A Coffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/GimpArm)

## Running it

> **Only one session at a time**: this camera only allows a single active viewing session.
> While this bridge is connected, the official app cannot connect (and vice versa) - you'll
> need to close one before the other can view the stream.

```bash
python src/abus_rtsp_bridge.py --did <did> --password <password>
```

## Command-line arguments

| Flag | Default | Description |
|---|---|---|
| `--config PATH` | none | Path to a structured YAML (or JSON) config file - see [YAML configuration file](#yaml-configuration-file) below. Also settable via `ABUS_CONFIG_FILE`. |
| `--did` | none | DID to match (e.g. `ABCD-123456-EFGHI`). Optional on the LAN if `--target-ip` is given, but **required** for the P2P/WAN cloud fallback (see Docker examples below). |
| `--password` | *(required)* | Camera view password / security code. |
| `--bind-ip` | auto | Local IPv4 to bind the discovery socket to. |
| `--target-ip` | none | Known camera IP on the same LAN - skips broadcast discovery and probes this address directly. |
| `--rtsp-url` | `rtsp://0.0.0.0:8554/abus` | Destination RTSP URL to publish the stream on. |
| `--timeout` | `5.0` | Discovery timeout in seconds before falling back to P2P/WAN. |
| `--resolution` | `0` | Video quality: `0`=bySetting `1`=fullHD `2`=HD `3`=SD `4`=automatic. |
| `--disable-audio` | off | Disable the second (audio) RTP stream. Audio is served by default. |
| `--dump-raw PATH` | none | Diagnostic: write raw post-auth D0 frames to a file instead of streaming. |
| `--skip-video-start` | off | Diagnostic: never send `IOCTRL_TYPE_VIDEO_START`. |
| `--skip-audio-start` | off | Diagnostic: never send `IOCTRL_TYPE_AUDIO_START`. |
| `--debug` | off | Verbose per-packet/per-frame diagnostic logging. |
| `--ptz-http-port` | `8080` | Port for the PTZ REST server. |
| `--ptz-http-host` | `0.0.0.0` | Bind address for the PTZ REST server. |
| `--no-ptz-http` | off | Disable the PTZ REST server entirely. |
| `--onvif-port` | `8000` | Port for the ONVIF device/media/PTZ SOAP service. |
| `--no-onvif` | off | Disable the ONVIF service entirely. |
| `--no-ws-discovery` | off | Keep the ONVIF SOAP service but skip the WS-Discovery multicast responder. |
| `--onvif-ptz-step` | `2` | How far one ONVIF PTZ click moves the camera (1-16 raw step scale). |
| `--auth-username` | none | Require HTTP/RTSP Basic auth on the RTSP stream, ONVIF service, and PTZ REST server. Must be set together with `--auth-password`. |
| `--auth-password` | none | Password for `--auth-username`. |

Run `python src/abus_rtsp_bridge.py -h` for the exact, always-current list.

## YAML configuration file

Instead of (or alongside) individual flags/environment variables, every setting can be given
as one structured YAML file via `--config PATH` or `ABUS_CONFIG_FILE=PATH` - grouped by
topic rather than a flat list of `KEY: value` pairs. Any explicit CLI flag still overrides
the same setting from the file. See [config.example.yaml](config.example.yaml) for a
complete, runnable example; every section/key is optional:

```yaml
camera:
  did: ABCD-123456-EFGHI
  password: changeme
  bind_ip: 192.168.1.10
  target_ip: 192.168.1.64
  timeout: 5.0
rtsp:
  url: rtsp://0.0.0.0:8554/abus
  resolution: 0
  disable_audio: false
ptz:
  enabled: true
  http_host: 0.0.0.0
  http_port: 8080
onvif:
  enabled: true
  ws_discovery: true
  port: 8000
  ptz_step: 2
auth:
  username: null
  password: null
diagnostics:
  debug: false
  dump_raw: null
  skip_video_start: false
  skip_audio_start: false
```

A typo in a section/key name is rejected with a clear error rather than silently ignored.
This also makes wrapping the bridge in something that only speaks a single config file (e.g.
a Home Assistant add-on) straightforward - a Home Assistant add-on's own `/data/options.json`
works as-is too, since JSON is valid YAML.

## Running with Docker

Image: [`gimparm/abus-rtsp-bridge:latest`](https://hub.docker.com/r/gimparm/abus-rtsp-bridge).
`ABUS_*` environment variables map 1:1 to the CLI flags above (see `scripts/entrypoint.sh`);
anything passed after the image name on `docker run` is forwarded as extra CLI args too.

**Local LAN discovery** (`--network host`) - needed because UDP broadcast discovery must see
the camera's physical LAN segment directly, which only works on a real Linux host (bare
metal, Raspberry Pi, or a Linux VM with a *bridged*, not NAT'd, NIC on that LAN - **not**
Docker Desktop on Windows/Mac, whose containers stay behind its own internal virtual
network regardless of `--network host`):

```bash
docker run --rm --network host \
  -e ABUS_PASSWORD=<password> \
  -e ABUS_DID=<did> \
  -e ABUS_BIND_IP=<bind-ip> \
  -e ABUS_TARGET_IP=<target-ip> \
  gimparm/abus-rtsp-bridge:latest
```

**P2P/WAN cloud fallback** - for everywhere else (Docker Desktop, a container not on the
camera's LAN/VLAN, a cloud/remote host, etc.). No special networking is needed; just publish
the ports you want reachable. `--did` is **required** here (the cloud rendezvous lookup is
keyed by it) - LAN discovery is tried first and simply times out after `ABUS_TIMEOUT`
seconds before falling back automatically:

```bash
docker run --rm \
  -p 8554:8554 -p 8080:8080 -p 8000:8000 \
  -e ABUS_PASSWORD=<password> \
  -e ABUS_DID=<did> \
  gimparm/abus-rtsp-bridge:latest
```

## PTZ control (REST)

If the camera supports pan/tilt, a lightweight REST server (stdlib `http.server`, no extra
dependencies) starts alongside the RTSP stream at `http://0.0.0.0:8080` by default:

```bash
curl http://<host>:8080/ptz/up                # move up, default step
curl http://<host>:8080/ptz/left?step=8        # move left, bigger step
curl http://<host>:8080/ptz/stop
curl http://<host>:8080/directions             # list valid direction names
```

Directions: `up`, `down`, `left`, `right`, `left_up`, `left_down`, `right_up`, `right_down`,
`stop`, `auto_scan`, `calibration`. This is IOCTRL type 9 - a single discrete "move N steps"
command per call, the same one the real app sends once per axis on swipe-release (not a
continuous joystick). Use `--ptz-http-port`/`--ptz-http-host` to change the bind address, or
`--no-ptz-http` to disable it.

If the camera supports saved PTZ presets (the app's 3 position buttons), you can move to
one:

```bash
curl http://<host>:8080/ptz/preset/0     # go to saved position 0 (valid: 0, 1, 2)
```

This is the same IOCTRL type 9 channel, sub-command 12 (`IOCTRL_PTZ_PRESET_POINT`).
Saving/overwriting a preset (sub-command 13) is intentionally NOT exposed here - the app's
own UI prompts for admin/setup auth around that action, and it's a one-time setup step
easier done from the app itself.

The same REST server also serves a snapshot of the current video frame:

```bash
curl http://<host>:8080/snapshot -o snapshot.jpg
```

This is captured server-side from the live decoded video (a GStreamer `tee` right after
the software H.264 decode splits off a `jpegenc ! appsink` branch alongside the existing
RTSP encode path - see `gst_rtsp_server.py`'s `get_snapshot_jpeg()`), not from the camera
itself - the camera has no "take a snapshot while streaming" IOCTL (the Android app's own
snapshot button just grabs the currently-displayed decoded frame client-side too). Returns
`503` if no RTSP client is currently connected (the decode pipeline only runs while
streaming) or if no frame has been decoded yet.

If the camera has a built-in siren or spotlight/floodlight, the same REST server can
trigger them (fire-and-forget, no status feedback - matching the rest of this API):

```bash
curl http://<host>:8080/siren/on
curl http://<host>:8080/siren/off
curl http://<host>:8080/light/on
curl http://<host>:8080/light/off
```

## Health check

`GET /health` on the same REST server (never Basic-auth-gated, unlike every other route
above - it exposes no control surface or video) reports liveness/readiness as JSON:

```bash
curl http://<host>:8080/health
```

`status` is one of `starting` (still discovering/authenticating with the camera - this
server is started before that completes specifically so /health is reachable from process
startup), `ok`, `stalled` (streaming but no video for >20s - usually self-recovers within a
few seconds), `reconnecting` (session was lost and a fresh discover/auth cycle is underway -
normal, self-healing), or `unhealthy` (reconnecting for >2 minutes straight - something is
genuinely stuck, e.g. the camera is unreachable). Only `unhealthy` returns HTTP 503; every
other status is 200, since a Docker `HEALTHCHECK` shouldn't restart the container over
normal transient recovery. The Dockerfile already wires this up via `scripts/healthcheck.py`.

## PTZ control (ONVIF)

A minimal ONVIF Profile S-ish device (`src/onvif_server.py`, no third-party ONVIF/SOAP
library) also starts by default at `http://<lan-ip>:8000`, for NVRs/clients that expect
ONVIF specifically (e.g. Synology Surveillance Station, Blue Iris, ONVIF Device Manager):

- WS-Discovery (UDP multicast `239.255.255.250:3702`) so clients can auto-discover it.
- Device service (`/onvif/device_service`): GetDeviceInformation, GetCapabilities,
  GetServices, GetScopes, GetSystemDateAndTime.
- Media service (`/onvif/media_service`): GetProfiles/GetProfile, GetVideoSources,
  GetStreamUri (returns this bridge's real RTSP URL).
- PTZ service (`/onvif/ptz_service`): GetNodes/GetNode, GetConfigurations/GetConfiguration,
  ContinuousMove, RelativeMove, Stop, GetStatus, GetPresets, GotoPreset (3 fixed slots `0`-`2`,
  matching the app's own 3 saved-position buttons and the REST `/ptz/preset/<0-2>` above -
  SetPreset isn't implemented, same reasoning as the REST API).

**Limitation**: the camera's own protocol only supports a discrete "move N steps" command,
not a real "move until told to stop" primitive - so each ONVIF `ContinuousMove`/
`RelativeMove` call triggers one discrete move (direction derived from the requested
`PanTilt` vector's sign; magnitude is a small, fixed step - see below - NOT scaled by the
vector's own magnitude, since real-world PTZ clients like Frigate send full velocity on
every click regardless of intended distance), and `Stop` is a no-op. NVR UIs that repeat
`ContinuousMove` while a directional button is held still get a reasonable "keep moving"
experience out of this. Auth is HTTP Basic (see below), same credentials as RTSP/REST. Use
`--onvif-port` to change the port, `--no-onvif` to disable it entirely, or
`--no-ws-discovery` to keep the SOAP service but skip the multicast responder.

Each click moves the camera by `--onvif-ptz-step` (default 2) on the camera's own 1-16 raw
step scale. Calibrated against this camera's manufacturer spec (270° horizontal / 90°
vertical travel): ~34 raw steps = one full horizontal sweep (~7.9°/step), ~16 raw steps =
one full vertical sweep (~5.6°/step) - so the default is roughly a 16° horizontal / 11°
vertical nudge per click. Raise it if clicks move too little, lower it if still too far.


## Wire protocol summary

- Every UDP datagram: `0xF1` magic, msg_type byte, big-endian uint16 body length. msg types:
  `0x30` discover-req, `0x41`/`0x42` alive req/ack (DID), `0xE0`/`0xE1` ping/pong keepalive,
  `0xD0` data, `0xD1` ack/DRW.
- Auth: AES-128-ECB with the view password (UTF-8, zero-padded/truncated to 16 bytes) as key;
  encrypt the camera's challenge to respond, decrypt the AuthType-3 payload to get the session
  key used for everything after.
- The realtime AV data channel (DRW channel 1) is a continuous byte stream reassembled from
  each UDP packet's payload *after* sorting by the DRW header's own per-channel sequence
  number (packets can and do arrive out of order/duplicated over WiFi). Each message in that
  stream starts with a 4-byte micro-header (3-byte LE data_size + 1-byte type) followed by
  a 16-byte AES-ECB-encrypted frame header (codec_id, flag, xor_key, data_size)
  and then `data_size` bytes of XOR'd payload.
- Acks must reference the packet's own real DRW sequence number (not an independent counter),
  batched in groups of 17 or after 40ms, whichever comes first - the camera's flow control
  throttles hard if acks don't line up with what it actually sent.


Full details, the exact bugs that were found in each layer, and how they were confirmed are in
[abus-protocol.md](abus-protocol.md).
