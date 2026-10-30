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

> **Tip - finding your DID**: if you can't find the camera's DID (e.g. `ABCD-123456-EFGHI`),
> turn on push notifications for the camera in the ABUS app. The notification your phone
> receives contains the DID.

```bash
python src/abus_rtsp_bridge.py --config config.yaml
```

The bridge is configured only through one YAML (or JSON) file, given with `--config PATH` or
`ABUS_CONFIG_FILE=PATH` - there are no per-setting command-line flags or environment variables.
Run `python src/abus_rtsp_bridge.py -h` for the two options it does take.

## Configuration

Every camera is an entry of the `cameras` list (at least one); the other sections are shared by
all of them. See [config.example.yaml](config.example.yaml) for a complete, runnable example:

```yaml
cameras:
  - name: front_door               # unique; letters, digits, _ and -
    did: ABCD-012345-EFGHI         # did and/or target_ip is required when there are several cameras
    password: changeme
    # target_ip: 192.168.1.64      # skips broadcast discovery
    # bind_ip: 192.168.1.10        # local IPv4 for the discovery socket
    # timeout: 5.0                 # discovery seconds before the P2P/WAN fallback
    # resolution / disable_audio / disable_talkback: override the rtsp defaults below
    onvif_port: 8000               # ONVIF is off unless set; must be unique per camera
    # onvif_ws_discovery: true     # optional, with onvif_port
    # onvif_ptz_step: 2            # optional, with onvif_port
  - name: garage
    did: WXYZ-987654-ABCDE
    password: garage-password
rtsp:                              # shared
  bind_ip: 0.0.0.0
  port: 8554
  resolution: 0                    # 0=bySetting 1=fullHD 2=HD 3=SD 4=automatic
  disable_audio: false
  disable_talkback: false
ptz:                               # shared REST API
  enabled: true
  http_host: 0.0.0.0
  http_port: 8080
auth:
  username: null                   # both username+password must be set together, or neither
  password: null
diagnostics:
  debug: false
  dump_raw: null
  skip_video_start: false
  skip_audio_start: false
```

A typo in a section/key name is rejected with a clear error rather than silently ignored. A Home
Assistant add-on's own `/data/options.json` works as-is too, since JSON is valid YAML.

Each camera runs in its own supervised worker process (a crash in one never affects the others),
and log lines are prefixed with the camera name. All cameras share one RTSP port, told apart by
path, and one REST port, told apart by the first path segment; ONVIF is per camera:

| Service | Address |
|---|---|
| RTSP | `rtsp://<host>:8554/<name>` (`rtsp.bind_ip`/`rtsp.port`, shared) |
| PTZ/snapshot/health REST | `http://<host>:8080/<name>/...` (`ptz.http_host`/`ptz.http_port`, shared), see below |
| ONVIF | `http://<host>:<onvif_port>`, only for cameras that set `onvif_port` |

So the example above serves `rtsp://<host>:8554/front_door` and `rtsp://<host>:8554/garage`.
Duplicate names, DIDs and ONVIF ports are rejected at startup.

**One REST server for all cameras**: the REST API is served once, with the camera name as the
first path segment:

```bash
curl http://<host>:8080/garage/ptz/left?step=4
curl http://<host>:8080/front_door/snapshot -o snapshot.jpg
curl http://<host>:8080/cameras        # list camera names
curl http://<host>:8080/health         # worst status across all cameras (503 if any is unhealthy)
```

Everything under `/<camera>/` is the per-camera API described below (`/ptz/...`, `/light/...`,
`/siren/...`, `/snapshot`, `/play`, `/health`, `/directions`). The shared `auth` credentials
apply (except to `/health` and `/<camera>/health`). Each camera's own REST and RTSP servers
only listen on loopback (`ptz.http_port` + 1 + N and `rtsp.port` + 1 + N for camera number N,
counting from 0) and are not meant to be used directly. The Docker health check uses the
aggregated `/health`.

## Running with Docker

Image: [`gimparm/abus-rtsp-bridge:latest`](https://hub.docker.com/r/gimparm/abus-rtsp-bridge).
The container is configured only through the YAML file described above: mount it at
`/config/config.yaml`, or point `ABUS_CONFIG_FILE` at another path (see
[config.example.yaml](config.example.yaml)).

**Local LAN discovery** (`--network host`) - needed because UDP broadcast discovery must see
the camera's physical LAN segment directly, which only works on a real Linux host (bare
metal, Raspberry Pi, or a Linux VM with a *bridged*, not NAT'd, NIC on that LAN - **not**
Docker Desktop on Windows/Mac, whose containers stay behind its own internal virtual
network regardless of `--network host`):

```bash
docker run --rm --network host \
  -v ./config.yaml:/config/config.yaml:ro \
  gimparm/abus-rtsp-bridge:latest
```

**P2P/WAN cloud fallback** - for everywhere else (Docker Desktop, a container not on the
camera's LAN/VLAN, a cloud/remote host, etc.). No special networking is needed; just publish
the ports you want reachable. Each camera's `did` is **required** here (the cloud rendezvous
lookup is keyed by it) - LAN discovery is tried first and simply times out after the camera's
`timeout` seconds before falling back automatically:

```bash
docker run --rm \
  -p 8554:8554 -p 8080:8080 \
  -v ./config.yaml:/config/config.yaml:ro \
  gimparm/abus-rtsp-bridge:latest
```

Also publish each camera's `onvif_port` if you enabled ONVIF (e.g. `-p 8000:8000`).

## PTZ control (REST)

If the camera supports pan/tilt, a lightweight REST server (stdlib `http.server`, no extra
dependencies) starts alongside the RTSP stream at `http://0.0.0.0:8080` by default. Every
route below lives under the camera's name, e.g. `/garage/ptz/up`:

```bash
curl http://<host>:8080/<camera>/ptz/up                # move up, default step
curl http://<host>:8080/<camera>/ptz/left?step=8        # move left, bigger step
curl http://<host>:8080/<camera>/ptz/stop
curl http://<host>:8080/<camera>/directions             # list valid direction names
```

Directions: `up`, `down`, `left`, `right`, `left_up`, `left_down`, `right_up`, `right_down`,
`stop`, `auto_scan`, `calibration`. This is IOCTRL type 9 - a single discrete "move N steps"
command per call, the same one the real app sends once per axis on swipe-release (not a
continuous joystick). Use `ptz.http_host`/`ptz.http_port` to change the bind address, or
`ptz.enabled: false` to disable it.

If the camera supports saved PTZ presets (the app's 3 position buttons), you can move to
one:

```bash
curl http://<host>:8080/<camera>/ptz/preset/0     # go to saved position 0 (valid: 0, 1, 2)
```

This is the same IOCTRL type 9 channel, sub-command 12 (`IOCTRL_PTZ_PRESET_POINT`).
Saving/overwriting a preset (sub-command 13) is intentionally NOT exposed here - the app's
own UI prompts for admin/setup auth around that action, and it's a one-time setup step
easier done from the app itself.

The same REST server also serves a snapshot of the current video frame:

```bash
curl http://<host>:8080/<camera>/snapshot -o snapshot.jpg
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
curl http://<host>:8080/<camera>/siren/on
curl http://<host>:8080/<camera>/siren/off
curl http://<host>:8080/<camera>/light/on
curl http://<host>:8080/<camera>/light/off
```

## Health check

`GET /health` on the same REST server (never Basic-auth-gated, unlike every other route
above - it exposes no control surface or video) reports liveness/readiness as JSON. Without a
camera name it aggregates all cameras (worst status wins, per-camera detail under `cameras`);
`/<camera>/health` reports one camera:

```bash
curl http://<host>:8080/health
curl http://<host>:8080/<camera>/health
```

`status` is one of `starting` (still discovering/authenticating with the camera - this
server is started before that completes specifically so /health is reachable from process
startup), `ok`, `stalled` (streaming but no video for >20s - usually self-recovers within a
few seconds), `reconnecting` (session was lost and a fresh discover/auth cycle is underway -
normal, self-healing), or `unhealthy` (reconnecting for >2 minutes straight - something is
genuinely stuck, e.g. the camera is unreachable). Only `unhealthy` returns HTTP 503; every
other status is 200, since a Docker `HEALTHCHECK` shouldn't restart the container over
normal transient recovery. The Dockerfile already wires this up via `scripts/healthcheck.py`.

## Two-way audio (talkback)

The RTSP server supports the standard ONVIF audio **backchannel**: a client that sends
`Require: www.onvif.org/ver20/backchannel` in its DESCRIBE request (go2rtc - and therefore
Frigate's and Home Assistant's two-way talk - does this) gets one extra send-only audio media in
the SDP, `PCMA/8000` (G.711 A-law). Whatever audio it sends back is decoded to 8 kHz mono PCM and
played on the camera's speaker, the same way the phone app's talk button does: the bridge sends
the camera `IOCTRL_TYPE_SPEAKER_START`, streams the audio in 640-byte frames, and sends
`IOCTRL_TYPE_SPEAKER_STOP` after one second without audio (or when the last viewer leaves).
While the speaker is open the camera's own audio is muted in the stream (as in the app) to avoid
feedback, except on models the app treats as bidirectional (e.g. DC-X15/X16/X18/X19, DP-D2,
RVDP-RAS, and `DO` doorbells of the RVDP-OAS/OCS/GSO families), which keep playing it. The model
is read from the camera's device-info response. Cameras whose DID starts with `CO` or `DO` take
16 kHz ADPCM instead of PCM and send 16 kHz PCM (or 8 kHz ADPCM) back; both are handled
automatically but haven't been tested on such a camera. Clients that don't ask for the backchannel see the stream exactly as before.

With go2rtc (Frigate), if it doesn't request the backchannel by default, add `#backchannel=1` to
the RTSP source URL, e.g. `rtsp://<host>:8554/<camera>#backchannel=1`. A backchannel client gets its own decode/encode
pipeline, so expect roughly double the CPU while it is connected. Turn the feature off with
`rtsp.disable_talkback: true` (or per camera `disable_talkback`).

### Playing an audio file (REST)

`POST /play?source=<url>` downloads an audio file (mp3, or anything GStreamer can decode) and plays
it on the camera's speaker through the same talk path - no RTSP client needed:

```bash
curl -X POST "http://<host>:8080/<camera>/play?source=http://example.com/doorbell.mp3"
```

The file is fetched and decoded before the request is answered, so a bad URL or an undecodable
file gives an error (`400` not an http(s) URL, `413` too large - 10 MB / 3 minutes max, `422` can't
be decoded, `502` download failed); on success the answer is `202` with the clip length and
playback continues in the background. Only one clip plays at a time per camera (`409` otherwise,
`503` while the camera session is still being established). The download is made by the bridge
itself, so enable `auth` if the REST port is reachable by anything you don't trust. This works
independently of `disable_talkback`, which only controls the RTSP backchannel.

## PTZ control (ONVIF)

A minimal ONVIF Profile S-ish device (`src/onvif_server.py`, no third-party ONVIF/SOAP
library) is available for each camera that sets `onvif_port` (at `http://<lan-ip>:<onvif_port>`),
for NVRs/clients that expect ONVIF specifically (e.g. Synology Surveillance Station, Blue Iris,
ONVIF Device Manager). Add each camera to your NVR as its own ONVIF device:

- WS-Discovery (UDP multicast `239.255.255.250:3702`) so clients can auto-discover it.
- Device service (`/onvif/device_service`): GetDeviceInformation, GetCapabilities,
  GetServices, GetScopes, GetSystemDateAndTime.
- Media service (`/onvif/media_service`): GetProfiles/GetProfile, GetVideoSources,
  GetStreamUri (returns this bridge's real RTSP URL).
- PTZ service (`/onvif/ptz_service`): GetNodes/GetNode, GetConfigurations/GetConfiguration,
  ContinuousMove, RelativeMove, Stop, GetStatus, GetPresets, GotoPreset (3 fixed slots `0`-`2`,
  matching the app's own 3 saved-position buttons and the REST `/<camera>/ptz/preset/<0-2>` above -
  SetPreset isn't implemented, same reasoning as the REST API).

**Limitation**: the camera's own protocol only supports a discrete "move N steps" command,
not a real "move until told to stop" primitive - so each ONVIF `ContinuousMove`/
`RelativeMove` call triggers one discrete move (direction derived from the requested
`PanTilt` vector's sign; magnitude is a small, fixed step - see below - NOT scaled by the
vector's own magnitude, since real-world PTZ clients like Frigate send full velocity on
every click regardless of intended distance), and `Stop` is a no-op. NVR UIs that repeat
`ContinuousMove` while a directional button is held still get a reasonable "keep moving"
experience out of this. Auth is HTTP Basic (see below), same credentials as RTSP/REST. Set
`onvif_ws_discovery: false` on a camera to keep its SOAP service but skip the multicast
responder.

Each click moves the camera by `onvif_ptz_step` (default 2) on the camera's own 1-16 raw
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
