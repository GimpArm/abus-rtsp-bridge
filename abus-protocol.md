# ABUS camera LAN/P2P protocol reference

Reverse-engineered from the wire protocol and verified against real packet captures. This is
a summary of the wire protocol itself; it does not cover the debugging history behind it.

## Discovery

Two ways to find the camera and learn its `(ip, port)`:

1. **LAN broadcast**: send an empty `0x30` (search) message to UDP port 32108, broadcast.
   The camera replies with a `0x41` message containing its DID from its own UDP port
   (commonly 16411, but not guaranteed).
2. **P2P/WAN rendezvous fallback** (used when the bridge isn't on the camera's LAN, e.g. to
   avoid Docker `--network host`): a NAT-traversal protocol ("iLnkP2P"/"CS2"/"PPCS" - a
   generic third-party P2P camera SDK used by many white-label vendors, not specific to this
   brand). UDP packets to fixed rendezvous server IPs on port 32100, exchanging `0x20`
   (connect), `0xF9` (client verification, sent in the clear - no encryption involved despite
   its name), `0x21` (ack), `0x40` (route response). The `0x40` response contains TWO
   candidate routes (a "private LAN" address and an "external/relay" address), each with its
   own dynamically-assigned port (never a fixed constant - parse it from the payload). Only
   one of the two routes typically answers a real alive-probe with a genuine ack; the other
   route must be probed and confirmed before trusting it (silently accepting whichever
   "looks private" doesn't work reliably).

Once discovered, reuse the SAME local UDP socket/port for the rest of the session - the
camera indexes the client by `(ip, port)` learned during discovery/alive.

## Alive handshake

Client sends `0x41` (with its DID) to the camera's port; camera replies `0x42`. This just
confirms reachability before starting the real auth exchange.

## Authentication

- AES key = UTF-8 view-password bytes, zero-padded/truncated to 16 bytes, AES-128-ECB.
- Challenge/response over the reliable D0/D1 channel (subtype=1, "auth channel"), payload
  format: `LE16 AuthType + LE16 dataSize + 12 reserved + payload`, sent in
  **plaintext** (the auth exchange itself is the crypto handshake, not encrypted framing
  around it).
  - AuthType 1 = challenge (16-byte nonce) - encrypt it with the password key to respond.
  - AuthType 2 = response (16 bytes, the client's encrypted challenge).
  - AuthType 3 = ok (16 bytes) - decrypt with the password key to get the **session key**
    used for everything else (video/audio/IOCTL).
  - AuthType 4 = failed.

## Wire framing

- Every UDP datagram: `0xF1` magic, msg_type byte, big-endian uint16 body length.
- Reliable-channel wrapper (D0 = data, D1 = ack), used for auth/IOCTL/small messages:
  8-byte header (`0xD1` tag + 24-bit BE seq + LE16 inner length + BE16 subtype:
  `1`=auth, `4`=IOCTL) + payload. Only the FIRST fragment of a multi-fragment message has
  this header.
- The realtime AV data channel (DRW channel 1 specifically - a separate, much-less-common
  channel 0 also exists and must NOT be mixed into the same reassembly stream) uses its own,
  simpler 4-byte per-packet header: `0xD1` tag + 1-byte channel + 16-bit BE per-channel
  sequence number. Packets can arrive out of order/duplicated - reorder/dedup by this
  sequence number before reassembling (buffer ahead-of-expected packets briefly, skip a gap
  after a short stall rather than blocking forever).
- After reordering, channel-1 payload bytes form one continuous byte stream (not one message
  per UDP packet). Each new message in that stream: a 4-byte micro-header (**24-bit
  little-endian** data_size in bytes[0:3], type byte in byte[3]) + a 16-byte
  AES-128-ECB(session_key, no padding)-encrypted header (codec_id u16 LE @0, flag byte @3,
  xor_key byte @4, data_size u32 LE @8) + `data_size` bytes of payload XOR'd with the single
  xor_key byte. Validate `data_size + 16 == micro-header size` to detect/resync after a
  dropped or reordered packet.
- **Acks must be sent synchronously, immediately, per received data-channel packet** (not
  batched/delayed) - referencing the packet's own real per-channel sequence number. The ack
  packet's payload is `0xD1` tag + a 3-byte field encoding `0x0100 | ack_count` (NOT a
  sequence number, despite looking like one) followed by `ack_count` 2-byte big-endian
  ack-id entries. Getting any of this wrong (wrong ack cadence, wrong seq reference, wrong
  3-byte field encoding) causes the camera's own flow control to throttle hard and eventually
  send `0xF0` (stream-end) - see below.

## Video

- Codec is H.264, `codec_id == 3` in the post-auth frame header. The camera **never
  transmits SPS/PPS** NALs - the real app hardcodes them client-side; the bridge must
  prepend the same fixed SPS/PPS bytes once per session (see `frame_reassembler.py`).
- `flag` (byte 3 of the post-auth frame header) is the keyframe indicator (`flag==0` means a
  real IDR/keyframe) - the camera does send genuine keyframes, just not on a
  fixed/predictable schedule (can be several seconds into a session).
- Video only flows after the client sends `IOCTRL_TYPE_VIDEO_START` (ioctl_type=1) with an
  8-byte payload (channel, resolution, audioNotify). `IOCTRL_TYPE_STOP`=2
  stops it. There's a real-app-enforced minimum ~1000ms gap between a stop and the next
  start on the same session.

## Audio

- Despite requesting AAC via `IOCTRL_TYPE_AUDIO_START` (ioctl_type=3, same payload shape as
  video start), this camera always sends raw, unencoded **8kHz mono 16-bit PCM**
  (`codec_id == 1279`), not AAC - a device/firmware quirk, not a bug in the request. Started
  ~10ms after video start.

## IOCTL commands (all via the same AES-ECB + xor_key=2 encrypted 16-byte header format)

| ioctl_type | Meaning |
|---|---|
| 1 / 2 | VIDEO_START / VIDEO_STOP |
| 3 | AUDIO_START |
| 5 / 6 | DEVINFO_REQ / RESP (model/vendor/firmware string) |
| 9 | PTZ command (see below) |
| 192 | Simple on/off light switch |
| 212 | Siren |

### PTZ (ioctl_type 9)

8-byte payload: `byte[0]=direction/sub-command`, `byte[1]=step or preset index`, rest zero.
- Direction values: `0`=STOP, `1`=UP, `2`=DOWN, `3`=LEFT, `4`=RIGHT, `5`=LEFT_UP,
  `6`=LEFT_DOWN, `7`=RIGHT_UP, `8`=RIGHT_DOWN, `11`=AUTO_SCAN, `16`=CALIBRATION.
- Sub-command `12` = go to saved preset (byte[1] = slot 0-2); `13` = save current position to
  a slot (not implemented by this bridge - the app's own UI gates it behind admin/setup auth).
- The protocol only supports a discrete "move N steps" command (step magnitude roughly
  1-16) - there is no "move until told to stop" primitive. The real app only sends this on
  swipe-release, once per axis.

## Known camera behaviors that look like bugs but aren't

- **`0xF0`** (empty-body message, msg_type 0xF0) is a normal, intentional "closing this
  session" signal from the camera - not recoverable by "acking harder"; the only correct
  client reaction is a full fresh discover -> alive -> auth -> video-start cycle.
- The video bitstream has real (harmless once played back correctly) slice-header
  oddities (e.g. `num_ref_idx` values that look inconsistent with the SPS) - standard
  software decoders (avdec_h264/openh264dec) handle this fine as long as the reassembly/ack
  layer above is correct; no GPU/hardware decoder is required.

## See also

- Implementation lives in `src/wire_protocol.py` (framing), `src/crypto_utils.py` (AES),
  `src/ioctl_protocol.py` (IOCTL/PTZ constants), `src/frame_reassembler.py` (AV stream
  reconstruction), `src/camera_session.py` (the stateful session/reconnect logic),
  `src/p2p_handshake.py` (the WAN rendezvous fallback).
