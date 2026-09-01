#!/usr/bin/env python3
"""Owns the whole discover -> alive -> auth -> stream lifecycle for one ABUS camera.

See wire_protocol.py for the raw framing this builds on, ioctl_protocol.py for the
command layer, frame_reassembler.py for AV data-channel reconstruction, and
abus-protocol.md (repo memory) for the full reverse-engineering history.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import struct
import threading
import time
import urllib.parse
from typing import List, Optional, Tuple

import crypto_utils
import frame_reassembler
import gst_rtsp_server
import ioctl_protocol as ioctl
import p2p_handshake
import wire_protocol as wire
from logutil import debug_log, log


class CameraSession:
    """Owns a single UDP socket for the whole discover -> alive -> auth -> stream flow.

    The camera indexes a session by the client's (ip, port), so reusing one socket
    (instead of opening a fresh one per stage) is required for it to recognize the
    client across the discovery reply and the later auth/data exchange.
    """

    def __init__(self, password: str, bind_ip: Optional[str] = None, did: Optional[str] = None,
                 target_ip: Optional[str] = None, discover_timeout: float = 5.0):
        self.password = password
        self.did = did
        self.target_ip = target_ip
        self.discover_timeout = discover_timeout
        self.bind_ip = bind_ip or (wire.find_local_ipv4_candidates() or ["0.0.0.0"])[0]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        try:
            self.sock.bind((self.bind_ip, 0))
        except OSError as exc:
            # bind_ip isn't assigned to any interface in this network namespace - common when
            # running in a container/VM that doesn't actually share the physical LAN interface
            # (e.g. Docker Desktop on Windows/Mac with --network host).
            log(f"[warn] cannot bind to {self.bind_ip} ({exc}); falling back to 0.0.0.0")
            self.bind_ip = "0.0.0.0"
            self.sock.bind((self.bind_ip, 0))
        self.sock.settimeout(1.0)
        self.camera_ip: Optional[str] = None
        self.camera_port: int = wire.CAMERA_PORT
        self.local_seq = 0
        self.ack_id = 0
        self.session_key: Optional[bytes] = None
        self.last_video_stop_time = 0.0
        # Health-check state (see /health in ptz_rest_api.py) - "discovered"/"authed" are
        # deliberately NOT separate flags, just read camera_ip/session_key directly, since
        # those are already the authoritative state and can't drift out of sync.
        self.started_at = time.time()
        self.is_streaming = False
        self.last_video_data_time: Optional[float] = None
        self.reconnecting_since: Optional[float] = None
        # Test-only flags, set by main() from CLI flags.
        self.skip_video_start = False
        self.skip_audio_start = False

    def close(self) -> None:
        self.sock.close()

    def _send(self, frame: bytes, target: Optional[Tuple[str, int]] = None) -> None:
        dest = target or (self.camera_ip, self.camera_port)
        if dest[0] is None:
            # reconnect() briefly clears camera_ip while re-discovering - a background
            # thread (keepalive) ticking during that window must not blow up.
            return
        self.sock.sendto(frame, dest)

    def _send_auth(self, auth_type: int, payload: bytes = b"") -> None:
        frame = wire.build_d0(self.local_seq, wire.CHANNEL_AUTH, wire.build_auth_head(auth_type, payload))
        self.local_seq += 1
        self._send(frame)

    def send_ioctl(self, ioctrl_type: int, payload: bytes = b"") -> None:
        """Send an app -> device IOCTRL request (e.g. VIDEO_START)."""
        if not self.session_key:
            raise RuntimeError("send_ioctl() requires a completed auth() call first")
        header = ioctl.build_ioctl_head(ioctrl_type, len(payload))
        encrypted_header = crypto_utils.new_ecb_cipher(self.session_key).encrypt(header)
        xored_payload = bytes(b ^ ioctl.DEFAULT_XOR_KEY_IOCTRL for b in payload)
        frame = wire.build_d0(self.local_seq, wire.CHANNEL_IOCTL, encrypted_header + xored_payload)
        self.local_seq += 1
        self._send(frame)

    def start_video(self, channel: int = 0, resolution: int = ioctl.QUALITY_BY_SETTING, audio_notify: int = 0) -> None:
        """Video-start IOCTRL payload matching the real app's live-view startup: the camera
        does not push any video on its own - the app must explicitly request it after auth.
        Defaults to QUALITY_BY_SETTING (0) - the real app's live-view screen requests this
        default resolution; an explicit-resolution request is what non-liveview screens
        (e.g. a one-shot snapshot request) use, and may behave differently.
        """
        payload = bytes([channel & 0xFF, 0, 0, 0, resolution & 0xFF, audio_notify & 0xFF, 0, 0])
        if self.skip_video_start:
            log("[video] skip_video_start set - NOT sending IOCTRL_TYPE_VIDEO_START (test mode)")
            return
        log(f"[video] sending IOCTRL_TYPE_VIDEO_START request (resolution={resolution})")
        self.send_ioctl(ioctl.IOCTRL_TYPE_VIDEO_START, payload)

    def start_audio(self, channel: int = 0) -> None:
        """The real app always sends this ~10ms after IOCTRL_TYPE_VIDEO_START, requesting AAC
        (overwriting bytes[6:8] with CODECID_A_AAC=1283 little-endian). Left as zero, the
        camera defaults to PCM instead - a real deviation from what the app actually sends,
        kept for byte-for-byte fidelity even though this camera ignores the request anyway
        (see CODECID_A_PCM in frame_reassembler.py)."""
        payload = bytearray([channel & 0xFF, 0, 0, 0, 0, 0, 0, 0])
        payload[6:8] = struct.pack("<H", frame_reassembler.CODECID_A_AAC)
        if self.skip_audio_start:
            log("[video] skip_audio_start set - NOT sending IOCTRL_TYPE_AUDIO_START (test mode)")
            return
        log("[video] sending IOCTRL_TYPE_AUDIO_START request (AAC, matching real app)")
        self.send_ioctl(ioctl.IOCTRL_TYPE_AUDIO_START, bytes(payload))

    def stop_video(self, channel: int = 0) -> None:
        """Sends the same video-stop-request payload as start_video()'s default, as
        ioctl_type=2 (IOCTRL_TYPE_VIDEO_STOP)."""
        payload = bytes([channel & 0xFF, 0, 0, 0, 0, 0, 0, 0])
        log("[video] sending IOCTRL_TYPE_VIDEO_STOP request")

        if not self.session_key:
            log("[video] stop_video skipped: No active session key authenticated yet.")
            self.last_video_stop_time = time.time()
            return

        self.send_ioctl(ioctl.IOCTRL_TYPE_VIDEO_STOP, payload)
        self.last_video_stop_time = time.time()

    def move_ptz(self, direction: int, step: int = 4) -> None:
        """One discrete pan/tilt move. direction is one of the PTZ_* constants (or PTZ_STOP=0
        to halt an ongoing auto-scan), step is the move magnitude (real app derives this from
        swipe distance, ~1-16)."""
        payload = bytes([direction & 0xFF, step & 0xFF, 0, 0, 0, 0, 0, 0])
        log(f"[ptz] sending IOCTRL_TYPE_PTZ_COMMAND direction={direction} step={step}")
        self.send_ioctl(ioctl.IOCTRL_TYPE_PTZ_COMMAND, payload)

    def goto_preset(self, index: int) -> None:
        """Move to a position previously saved on the camera (one of the app's 3 preset
        slots, index 0-2)."""
        if not 0 <= index <= 2:
            raise ValueError("preset index must be 0, 1, or 2")
        self.move_ptz(ioctl.PTZ_GOTO_PRESET, index)

    def set_light(self, on: bool) -> None:
        """Simple spotlight/floodlight on/off switch."""
        payload = bytes([1 if on else 0, 0, 0, 0, 0, 0, 0, 0])
        log(f"[light] sending IOCTRL_TYPE_LIGHT_CONTROL on={on}")
        self.send_ioctl(ioctl.IOCTRL_TYPE_LIGHT_CONTROL, payload)

    def set_siren(self, on: bool) -> None:
        """Start or stop the camera's built-in siren."""
        payload = bytes([1 if on else 0, 0, 0, 0, 0, 0, 0, 0])
        log(f"[siren] sending IOCTRL_TYPE_SIREN on={on}")
        self.send_ioctl(ioctl.IOCTRL_TYPE_SIREN, payload)

    def preflight(self) -> None:
        """Replicate the FULL ioctl sequence the real app sends when entering live view -
        confirmed present (with only minor variation in exact set/ordering/duplication) in
        ALL THREE decodable real-app captures (full_capture3/4/7.pcap), regardless of whether
        the app ends up needing a fresh VIDEO_START or joins an already-active stream:
        IOCTRL_TYPE_GET_ON_OFF_VALUE_REQ(17, empty), IOCTRL_TYPE_PUSH_APP_UTC_TIME(53, 4-byte
        LE unix timestamp + 4 zero bytes), IOCTRL_TYPE_DEVINFO_REQ(5, 4 zero bytes),
        IOCTRL_TYPE_LISTEVENT_SNAPSHOT_REQ(534, 8 zero bytes) - each sent twice, ~10-20ms
        apart (matches the SDK's own observed retry-on-no-immediate-ack behavior) - followed
        a beat later by IOCTRL_TYPE_GET_ARM_REQ(188)/IOCTRL_TYPE_RECORD_START(180), also
        doubled. Real captures show the exact gap before this second burst varies a lot
        (0.8s-5.1s) - not clearly a fixed timer, more likely response-driven - so this uses a
        conservative fixed ~1s approximation rather than a value pulled from any one capture.
        NOTE: none of these three captures ever shows IOCTRL_TYPE_VIDEO_START(1)/
        IOCTRL_TYPE_AUDIO_START(3) at all - every one of them observed an already-actively-
        streaming camera, not a genuine cold start - so start_video()/start_audio() (called
        separately, right after this) remain our own best-effort addition for the cold-start
        case our bridge always needs (we have no way to "join" an existing stream)."""
        self.send_ioctl(17)
        self.send_ioctl(17)
        utc_payload = struct.pack("<I", int(time.time())) + bytes(4)
        self.send_ioctl(53, utc_payload)
        self.send_ioctl(53, utc_payload)
        self.send_ioctl(5, bytes(4))
        self.send_ioctl(5, bytes(4))
        self.send_ioctl(534, bytes(8))
        self.send_ioctl(534, bytes(8))
        time.sleep(1.0)
        self.send_ioctl(188, bytes(8))
        self.send_ioctl(188, bytes(8))
        self.send_ioctl(180, bytes(8))

    def _ack(self) -> None:
        # Used by auth()/dump_stream() only - simple, proven, unbatched. The video hot loop in
        # read_stream() uses the batched _enqueue_ack()/_flush_acks() pair instead (see there
        # for the batching cadence and why).
        self._send(wire.build_ack(self.ack_id))
        self.ack_id += 1

    def discover(self, target_ip: Optional[str] = None, timeout: float = 5.0) -> bool:
        """
        First tries standard Layer-2 UDP local discovery.
        If no response occurs within the window, falls back to the P2P Anycast service.
        """
        log("[discover] Attempting local LAN UDP camera discovery...")
        targets: List[str] = []
        if target_ip:
            targets.append(target_ip)
        try:
            iface = ipaddress.IPv4Interface(f"{self.bind_ip}/24")
            targets.append(str(iface.network.broadcast_address))
        except ValueError:
            pass
        targets.append("255.255.255.255")

        # Loop local discovery for half the timeout period before failing over
        local_deadline = time.time() + (timeout / 2.0)
        req = wire.build_f1(wire.MSG_SEARCH)

        while time.time() < local_deadline and self.camera_ip is None:
            for target in targets:
                try:
                    self.sock.sendto(req, (target, wire.DISCOVERY_PORT))
                except OSError:
                    pass
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError, OSError):
                continue
            parsed = wire.parse_f1(data)
            if not parsed:
                continue
            msg_type, body = parsed
            if msg_type not in (wire.MSG_ALIVE_REQ, wire.MSG_ALIVE_ACK):
                continue
            found_did = wire.decode_did(body)
            if not found_did:
                continue
            if self.did and found_did.lower() != self.did.lower():
                continue
            self.camera_ip = addr[0]
            self.camera_port = addr[1]
            self.did = found_did
            log(f"[discover] Local LAN camera identified: ip={self.camera_ip} port={self.camera_port}")
            return True

        # --- P2P CLOUD FALLBACK SERVICE CHANNEL ---
        log("[discover] Local LAN search timed out. Engaging P2P Cloud Fallback Engine...")
        if not self.did:
            log("[discover] Error: P2P fallback requires a valid --did input parameter string.")
            return False

        uid_bytes = wire.encode_did(self.did)
        session_handle = struct.pack("<I", 0x553C7902)
        reversed_lan_ip = socket.inet_aton(self.bind_ip)[::-1]
        connect_payload = uid_bytes + session_handle + reversed_lan_ip + b"\x00" * 8
        encrypted_auth_payload = p2p_handshake.generate_authenticated_f9_payload()

        # Set a shorter local temporary timeout to cycle servers rapidly
        self.sock.settimeout(2.5)

        def _probe_route_alive(ip: str, port: int, attempts: int = 3, per_try_timeout: float = 0.3) -> bool:
            """Confirm a rendezvous-reported route is actually usable before committing to
            it - confirmed live (capture.pcap) that the reported "private LAN" route can go
            completely unanswered by anything resembling a real ack (the camera just echoes
            the same 0x41 alive request back, never a genuine 0x42) while the paired
            external/relay route on the very same session answers with proper 0x42 acks.
            Blindly trusting whichever route merely LOOKS private (this bridge often isn't
            actually on that LAN segment - the whole reason P2P exists) silently hung on
            garbage. Sends the same encode_did()-based F1/0x41 alive request the real app
            uses (not a bare "cs2" 0x40 punch - that's not what real traffic shows either)."""
            alive_payload = wire.encode_did(self.did)
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(per_try_timeout)
            try:
                for _ in range(attempts):
                    try:
                        self.sock.sendto(wire.build_f1(wire.MSG_ALIVE_REQ, alive_payload), (ip, port))
                    except OSError:
                        continue
                    deadline = time.time() + per_try_timeout
                    while time.time() < deadline:
                        try:
                            data, addr = self.sock.recvfrom(2048)
                        except (socket.timeout, ConnectionResetError, OSError):
                            break
                        if addr[0] != ip:
                            continue
                        parsed = wire.parse_f1(data)
                        if parsed and parsed[0] == wire.MSG_ALIVE_ACK:
                            return True
                return False
            finally:
                self.sock.settimeout(old_timeout)

        for server_ip in p2p_handshake.RENDEZVOUS_SERVERS:
            log(f"[discover] Querying WAN directory node -> {server_ip}...")
            try:
                # Transmit parallel multiplexed connection bursts
                self.sock.sendto(b"\xF1\x00\x00\x00", (server_ip, p2p_handshake.PORT_START))
                for offset in range(3):
                    packet = p2p_handshake.build_cs2_packet(0x20, connect_payload)
                    self.sock.sendto(packet, (server_ip, p2p_handshake.PORT_START + offset))

                auth_packet = p2p_handshake.build_cs2_packet(0xF9, encrypted_auth_payload)
                self.sock.sendto(auth_packet, (server_ip, p2p_handshake.PORT_START))

                # Wait for remote response routing maps - collect BOTH candidates (private
                # LAN and external/relay) before deciding, rather than committing to
                # whichever arrives/looks-private first (see _probe_route_alive() above).
                private_route: Optional[Tuple[str, int]] = None
                external_route: Optional[Tuple[str, int]] = None
                collect_deadline = time.time() + 2.0
                while time.time() < collect_deadline and not (private_route and external_route):
                    try:
                        data, addr = self.sock.recvfrom(2048)
                    except (socket.timeout, ConnectionResetError, OSError):
                        continue
                    if len(data) < 4 or data[0] != 0xF1:
                        continue
                    cmd = data[1]
                    length = struct.unpack(">H", data[2:4])[0]
                    payload = data[4:4+length]

                    if cmd == 0x21:
                        # Confirmed 0x00000000 in every working capture so far - no known
                        # failure sample to confirm the encoding, but surface anything else
                        # instead of silently treating every 0x21 as success regardless of
                        # content (the old behavior, unconditionally logging "cleared").
                        status = payload.hex() if payload else ""
                        if payload and any(payload):
                            log(f"[discover-p2p] Remote verification returned non-zero status "
                                f"0x{status} (expected all-zero) - proceeding, but this is unverified")
                        else:
                            log("[discover-p2p] Remote verification cleared.")
                        continue
                    if cmd != 0x40 or len(payload) < 8:
                        continue

                    # 1. Extract the IP bytes using the verified Little-Endian reversal layout
                    ip_bytes = payload[4:8][::-1]
                    route_ip = f"{ip_bytes[0]}.{ip_bytes[1]}.{ip_bytes[2]}.{ip_bytes[3]}"

                    try:
                        ip_obj = ipaddress.ip_address(route_ip)
                        is_private = ip_obj.is_private and route_ip != "0.0.0.0"
                        route_type = "Local LAN" if is_private else "External WAN"
                    except ValueError:
                        is_private = False
                        route_type = "External"

                    # BUG FIX: this used to hardcode 16411 (the LAN broadcast default) or
                    # 32100 (the rendezvous SERVER's own port, not a session port) instead
                    # of reading the actual assigned port - decoded straight from
                    # capture.pcap: bytes[2:4] of this exact payload, little-endian, gave
                    # port=60222 for the external/relay route and port=16402 for the LAN
                    # route in that capture - neither matches 16411/32100. Every P2P session
                    # gets its own dynamically-assigned port; there is no fixed constant.
                    route_port = struct.unpack_from("<H", payload, 2)[0]

                    log(f"[discover-p2p] Server reported {route_type} route -> {route_ip}:{route_port}")

                    if is_private:
                        private_route = (route_ip, route_port)
                    else:
                        external_route = (route_ip, route_port)

                # Try private first (lower latency when it genuinely works), but only commit
                # to whichever candidate actually answers a real alive request - see
                # _probe_route_alive()'s docstring for why "looks private" isn't enough.
                for candidate, label in ((private_route, "Local LAN"), (external_route, "External WAN/relay")):
                    if candidate is None:
                        continue
                    route_ip, route_port = candidate
                    log(f"[discover-p2p] Confirming {label} route is alive: {route_ip}:{route_port}...")
                    if _probe_route_alive(route_ip, route_port):
                        log(f"[discover-p2p] {label} route confirmed alive - using {route_ip}:{route_port}")
                        self.camera_ip = route_ip
                        self.camera_port = route_port
                        self.sock.settimeout(1.0)
                        return True
                    log(f"[discover-p2p] {label} route did not respond - trying next candidate")

            except socket.timeout:
                continue

        self.sock.settimeout(1.0)
        return self.camera_ip is not None

    def alive_handshake(self, timeout: float = 2.0) -> None:
        """Echo the DID alive packet back to the camera, mirroring the app's F1/41 exchange."""
        if not self.camera_ip or not self.did:
            return
        payload = wire.encode_did(self.did)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._send(wire.build_f1(wire.MSG_ALIVE_REQ, payload))
            try:
                data, _ = self.sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError, OSError):
                continue
            parsed = wire.parse_f1(data)
            if parsed and parsed[0] == wire.MSG_ALIVE_ACK:
                log("[alive] camera acknowledged session")
                return

    def auth(self, timeout: float = 10.0) -> bool:
        """Complete the AES challenge/response handshake over the F1/D0 reliable channel.

        BUG FIX: this used to short-circuit entirely whenever camera_port != CAMERA_PORT
        (i.e. discover() resolved a P2P route instead of a plain LAN broadcast), setting
        session_key to a "dummy" value (the padded password bytes) instead of a real
        negotiated key - "session established" logs looked fine (the P2P tunnel/socket
        itself worked), but every send_ioctl()/FrameReassembler AES operation downstream
        then used the wrong key, so every real frame's 16-byte header decrypted to garbage,
        failed the data_size+16 sanity check, and the reassembler spun in permanent
        byte-resync - no video ever came through. _send()/recvfrom() already only care about
        self.camera_ip/self.camera_port, not how they were discovered, so the exact same
        challenge/response the LAN path already uses works unchanged over a P2P-resolved
        address too - there is no separate "P2P auth" to bypass into.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(65535)
            except (socket.timeout, ConnectionResetError, OSError):
                continue

            parsed = wire.parse_f1(data)
            if not parsed:
                continue
            msg_type, body = parsed

            if msg_type == wire.MSG_PING:
                self._send(wire.build_f1(wire.MSG_PONG))
                continue
            if msg_type in (wire.MSG_PONG, wire.MSG_ALIVE_REQ, wire.MSG_ALIVE_ACK, wire.MSG_ACK):
                continue
            if msg_type != wire.MSG_DATA:
                continue

            d0 = wire.parse_d0(body)
            if not d0:
                continue
            self._ack()
            if d0["subtype"] != wire.CHANNEL_AUTH:
                continue

            auth = wire.parse_auth_head(d0["payload"])
            if not auth:
                continue
            auth_type, payload = auth

            if auth_type == wire.AUTH_TYPE_CHALLENGE and len(payload) >= 16:
                response = crypto_utils.encrypt_block(self.password, payload[:16])
                self._send_auth(wire.AUTH_TYPE_RESPONSE, response)
                log("[auth] sent AES response to camera challenge")
            elif auth_type == wire.AUTH_TYPE_OK and len(payload) >= 16:
                self.session_key = crypto_utils.decrypt_block(self.password, payload[:16])
                log(f"[auth] session established, session_key={self.session_key.hex()}")
                return True
            elif auth_type == wire.AUTH_TYPE_FAILED:
                log("[auth] camera rejected the password")
                return False

        log("[auth] timed out waiting for the camera")
        return False

    def reconnect(self, resolution: int = ioctl.QUALITY_BY_SETTING) -> bool:
        """Redo discover -> alive -> auth -> video-start from scratch on the same socket, for
        when the camera has fully dropped the session (0xF0 followed by silence even after
        re-sending IOCTRL_TYPE_VIDEO_START - verified live that alone does not resume it).
        Gets a new camera_port/session_key, so callers must replace their FrameReassembler.
        """
        log("[reconnect] camera session appears dead, reconnecting from scratch")
        stop_time = time.time()
        self.camera_ip = None
        self.camera_port = wire.CAMERA_PORT  # Reset to default baseline port
        self.session_key = None
        self.local_seq = 0
        self.ack_id = 0
        if not self.discover(target_ip=self.target_ip, timeout=self.discover_timeout):
            log("[reconnect] camera could not be located via Local LAN or P2P cloud")
            return False
        self.alive_handshake()
        if not self.auth(timeout=self.discover_timeout * 2):
            log("[reconnect] authentication track failed")
            return False
        elapsed = time.time() - stop_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self.preflight()
        self.start_video(resolution=resolution)
        time.sleep(0.01)
        self.start_audio()
        return True

    def reconnect_until_success(self, resolution: int = ioctl.QUALITY_BY_SETTING,
                                 max_backoff: float = 60.0) -> None:
        """Keep calling reconnect() until it succeeds, with capped exponential backoff.

        BUG FIX: callers used to treat a single failed reconnect() (e.g. the camera briefly
        not answering discovery, or an auth timeout) as something to just log and move past -
        but reconnect() unconditionally clears session_key/camera_ip up front, so a failed
        attempt left the session permanently dead: the read loop would spin forever with no
        camera_ip to match incoming packets against, and the NEXT RTSP client to connect would
        crash _on_client_connect()/preflight()/send_ioctl() with "requires a completed auth()
        call first" - confirmed live after ~24h uptime. A transient failure here is exactly the
        kind of thing this bridge exists to recover from, so keep retrying instead of giving up.
        """
        delay = 1.0
        self.reconnecting_since = time.time()
        while True:
            try:
                if self.reconnect(resolution=resolution):
                    self.reconnecting_since = None
                    return
            except OSError as exc:
                log(f"[reconnect] failed: {exc}")
            log(f"[reconnect] retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, max_backoff)

    def dump_stream(self, path: str) -> None:
        """Write raw post-auth F1/D0 bodies to disk for offline analysis - no ffmpeg required.

        Each record is: 4-byte running index + 4-byte body length + the raw D0 body
        (0xD1 tag + sequence + whatever follows), matching the format used to analyze
        full_capture3.pcap so the same offline tooling applies to a live capture too.
        """
        self.sock.settimeout(1.0)
        count = 0
        log(f"[dump] writing raw D0 frames to {path} (Ctrl+C to stop)")
        with open(path, "wb") as f:
            while True:
                try:
                    data, _ = self.sock.recvfrom(65535)
                except (socket.timeout, ConnectionResetError, OSError):
                    continue
                parsed = wire.parse_f1(data)
                if not parsed:
                    continue
                msg_type, body = parsed
                if msg_type == wire.MSG_PING:
                    self._send(wire.build_f1(wire.MSG_PONG))
                    continue
                if msg_type != wire.MSG_DATA:
                    continue
                d0 = wire.parse_d0(body)
                if not d0:
                    continue
                self._ack()
                f.write(struct.pack(">II", count, len(body)))
                f.write(body)
                f.flush()
                count += 1

    def read_stream(self, target_rtsp_url: str, resolution: int = ioctl.QUALITY_BY_SETTING,
                     auth_username: Optional[str] = None,
                     auth_password: Optional[str] = None, enable_audio: bool = True,
                     gst_server_ref: Optional[dict] = None) -> None:
        """Decode the post-auth data channel into H.264 access units and hand them to a
        GStreamer-backed RTSP server (gst_rtsp_server.py).

        Verified against full_capture3.pcap: see FrameReassembler for the AES-header + XOR-body
        framing. Only codec_id == CODEC_H264 frames are forwarded; audio/IOCTL frames are dropped.

        The camera is only asked to actually stream (IOCTRL_TYPE_VIDEO_START/AUDIO_START) once
        at least one RTSP client has PLAYed, and told to stop (IOCTRL_TYPE_VIDEO_STOP) the
        moment the last one disconnects - a player that stops and later resumes (e.g. VLC's own
        pause/stop button) gets a fresh request instead of relying on a session the camera may
        have already torn down silently.
        """
        if not self.session_key:
            raise RuntimeError("read_stream() requires a completed auth() call first")

        self.sock.settimeout(1.0)
        reassembler = frame_reassembler.FrameReassembler(self.session_key)

        # --- Trailing Disconnect Grace Period State ---
        disconnect_timer: Optional[threading.Timer] = None
        disconnect_lock = threading.Lock()
        # threading.Timer.cancel() cannot stop a callback that has already started running -
        # only one that hasn't fired yet. Without this token, a reconnect landing in that
        # narrow window (timer already past cancel()'s reach, not yet holding disconnect_lock)
        # would have its "resume streaming" state immediately stomped back to stopped once the
        # stale callback finally acquires the lock. Each new disconnect/reconnect bumps this;
        # _deferred_stop_video() only acts if its own captured generation still matches.
        disconnect_generation = 0

        # Sequence-based reordering for the data channel - the real DRW packet header
        # (confirmed via capture analysis) carries a 16-bit BE per-direction
        # sequence number; without honoring it, any real-world UDP reordering/duplication
        # (routine over WiFi) scrambles the concatenated byte stream fed to FrameReassembler,
        # producing exactly the kind of constant desync previously seen. expected_data_seq is
        # the next seq we're waiting to deliver in order; reorder_buffer holds
        # already-received-but-out-of-order packets (seq -> payload) until their turn comes,
        # or until they're old enough to just skip past (assume genuinely lost, not reordered).
        expected_data_seq: Optional[int] = None
        reorder_buffer: dict = {}
        last_seq_advance_time = time.time()
        REORDER_STALL_TIMEOUT = 0.2  # seconds to wait for a gap-filler before giving up on it
        REORDER_MAX_BUFFER = 64  # cap how many out-of-order packets we'll hold at once

        def _seq_delta(a: int, b: int) -> int:
            # (a - b) mod 65536, mapped to a signed range so "b is slightly ahead of a" reads
            # as a small negative number instead of a huge positive one (16-bit wraparound).
            d = (a - b) & 0xFFFF
            return d - 0x10000 if d > 0x7FFF else d

        def _reorder_deliver(seq: int, payload: bytes) -> List[bytes]:
            """Feed one just-received data-channel packet through the reorder buffer.
            Returns the list of payloads (0 or more) now ready to hand to the reassembler,
            in correct order."""
            nonlocal expected_data_seq, last_seq_advance_time
            ready: List[bytes] = []
            if expected_data_seq is None:
                expected_data_seq = seq
            # A gap has sat unfilled too long (the missing packet is most likely truly lost,
            # not just reordered) - skip past it rather than stalling video forever. Checked
            # up front, independent of whatever packet just arrived.
            if reorder_buffer and time.time() - last_seq_advance_time > REORDER_STALL_TIMEOUT:
                expected_data_seq = min(reorder_buffer.keys())
            delta = _seq_delta(seq, expected_data_seq)
            if delta == 0:
                ready.append(payload)
                expected_data_seq = (expected_data_seq + 1) & 0xFFFF
                last_seq_advance_time = time.time()
                # Draining: a single in-order arrival can unlock a whole run of previously
                # out-of-order/buffered packets that were waiting on this exact gap.
                while expected_data_seq in reorder_buffer:
                    ready.append(reorder_buffer.pop(expected_data_seq))
                    expected_data_seq = (expected_data_seq + 1) & 0xFFFF
                    last_seq_advance_time = time.time()
            elif delta > 0:
                # Ahead of what we're waiting for - genuinely out of order, buffer it.
                if len(reorder_buffer) < REORDER_MAX_BUFFER:
                    reorder_buffer[seq] = payload
                else:
                    # Buffer overflowing means we're stuck on a gap for too long - give up
                    # waiting for it and jump forward instead of stalling video indefinitely.
                    expected_data_seq = seq
                    reorder_buffer.clear()
                    reorder_buffer[seq] = payload
            # else: delta < 0 is a stale duplicate/very-late retransmit of something already
            # delivered (or skipped past above) - drop it silently, nothing to do.
            return ready

        is_streaming = False
        packet_count = 0
        frame_count = 0
        byte_count = 0
        stall_retries = 0
        last_video_data = time.time()
        # Diagnostics: has a real flag==0 keyframe EVER been seen this session, and how many
        # consecutive byte-identical frames (the camera genuinely resends the exact same
        # encoded frame sometimes, e.g. for static-scene bandwidth padding) are we currently in.
        seen_real_keyframe = False
        seen_real_audio = False
        last_frame_hash: Optional[str] = None
        dup_run_len = 0
        last_heartbeat_time = time.time()

        def _on_client_connect() -> None:
            nonlocal reassembler, is_streaming, packet_count, frame_count, byte_count, stall_retries, last_video_data
            nonlocal seen_real_keyframe, last_frame_hash, dup_run_len, last_heartbeat_time
            nonlocal expected_data_seq, last_seq_advance_time
            nonlocal seen_real_audio, disconnect_timer, disconnect_generation

            with disconnect_lock:
                if disconnect_timer is not None:
                    log("[rtsp] Client re-connected during active grace window. Canceling pending stop_video command.")
                    disconnect_timer.cancel()
                    disconnect_timer = None
                    # Invalidate any callback that already slipped past cancel() (see the
                    # disconnect_generation comment above) before it gets to acquire the lock.
                    disconnect_generation += 1
                    is_streaming = True
                    self.is_streaming = True
                    return

            log("[rtsp] client connected - starting camera video/audio stream")
            if not self.session_key:
                log("[rtsp] no active camera session yet (reconnecting) - client should retry")
                return
            elapsed = time.time() - self.last_video_stop_time
            if self.last_video_stop_time and elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            self.preflight()
            self.start_video(resolution=resolution)
            time.sleep(0.01)
            self.start_audio()
            reassembler = frame_reassembler.FrameReassembler(self.session_key)
            packet_count = 0
            frame_count = 0
            byte_count = 0
            stall_retries = 0
            last_video_data = time.time()
            seen_real_keyframe = False
            last_frame_hash = None
            dup_run_len = 0
            last_heartbeat_time = time.time()
            seen_real_audio = False
            expected_data_seq = None
            reorder_buffer.clear()
            last_seq_advance_time = time.time()
            with ack_lock:
                pending_ack_ids.clear()
            is_streaming = True
            self.is_streaming = True
            self.last_video_data_time = last_video_data

        def _deferred_stop_video(generation: int) -> None:
            nonlocal is_streaming, disconnect_timer
            with disconnect_lock:
                if generation != disconnect_generation:
                    # Superseded by a newer disconnect/reconnect cycle - Timer.cancel() can't
                    # stop a callback that already started running, so this token is the real
                    # guard against stomping a reconnect that raced past it.
                    return
                is_streaming = False
                self.is_streaming = False
                # BUG: without this, the timer object stays non-None after firing, so the
                # NEXT genuine fresh client connect (long after this grace window is over)
                # would wrongly hit _on_client_connect()'s "reconnected during grace window"
                # branch below - cancel()ing an already-fired timer (a harmless no-op) and
                # returning WITHOUT ever calling preflight()/start_video()/start_audio() or
                # resetting the reassembler. Every connect after the first grace-window
                # timeout would silently never start the camera stream again.
                disconnect_timer = None
            log("[rtsp] Grace window expired with zero active clients. Stopping camera video/audio stream.")
            try:
                self.stop_video()
            except OSError as exc:
                log(f"[video] stop_video failed (ignored): {exc}")

        def _on_client_disconnect() -> None:
            nonlocal is_streaming, disconnect_timer, disconnect_generation
            if not is_streaming:
                return

            log("[rtsp] Last client disconnected. Initiating a 30-second stream preserve grace window...")

            with disconnect_lock:
                if disconnect_timer is not None:
                    disconnect_timer.cancel()
                disconnect_generation += 1
                generation = disconnect_generation
                # Creates a background thread daemon task to hold the UDP sockets active
                disconnect_timer = threading.Timer(30.0, _deferred_stop_video, args=(generation,))
                disconnect_timer.daemon = True
                disconnect_timer.start()

        parsed_url = urllib.parse.urlparse(target_rtsp_url)
        rtsp_host, rtsp_port = parsed_url.hostname or "0.0.0.0", parsed_url.port or 8554
        rtsp_path = parsed_url.path.lstrip("/") or "abus"
        server = gst_rtsp_server.GstAbusRtspServer(
            sps_nal=frame_reassembler.H264_SPS[4:], pps_nal=frame_reassembler.H264_PPS[4:],
            host=rtsp_host, port=rtsp_port, path=rtsp_path,
            enable_audio=enable_audio,
            auth_username=auth_username, auth_password=auth_password,
            on_first_client_connect=_on_client_connect, on_last_client_disconnect=_on_client_disconnect,
        )
        try:
            server.start()
        except OSError as exc:
            log(f"[rtsp] could not bind {rtsp_host}:{rtsp_port} ({exc}) - is a stale instance still running? kill it first.")
            return
        if gst_server_ref is not None:
            gst_server_ref["server"] = server

        stop_keepalive = threading.Event()

        def _keepalive() -> None:
            # Must never die permanently - a single transient send error (e.g. racing a
            # reconnect()) used to kill this thread forever, silently starving every
            # subsequent session of the pings it needs to keep streaming.
            while not stop_keepalive.wait(1.0):
                if time.time() - last_video_data < 3.0:
                    continue
                try:
                    self._send(wire.build_f1(wire.MSG_PING))
                except Exception as exc:
                    log(f"[keepalive] send failed (ignored): {exc}")

        keepalive_thread = threading.Thread(target=_keepalive, daemon=True)
        keepalive_thread.start()

        # Ack-flush algorithm confirmed via capture analysis - the real app does NOT ack
        # every packet immediately: it accumulates pending ack-ids and flushes (sends one
        # batched ack) as soon as EITHER 17 (0x11) accumulate, OR ~40ms has elapsed since the
        # last flush (direct-LAN interval; 10ms is for relayed/cloud sessions) - whichever
        # comes first.
        pending_ack_ids: List[int] = []
        last_ack_flush_time = time.time()
        ack_lock = threading.Lock()
        stop_ack_flush = threading.Event()

        def _flush_acks() -> None:
            nonlocal last_ack_flush_time
            with ack_lock:
                ids = pending_ack_ids[:]
                pending_ack_ids.clear()
                last_ack_flush_time = time.time()
            if ids:
                self._send(wire.build_batched_ack(ids))

        def _enqueue_ack(ack_id: int) -> None:
            with ack_lock:
                pending_ack_ids.append(ack_id)
                should_flush = len(pending_ack_ids) >= 17
            if should_flush:
                _flush_acks()

        def _ack_flush_loop() -> None:
            # Must never die permanently - see the identical lesson learned about _keepalive()
            # above; a dead ack-flush thread means pending acks silently never get sent again.
            while not stop_ack_flush.wait(0.01):
                try:
                    if time.time() - last_ack_flush_time >= 0.04:
                        _flush_acks()
                except Exception as exc:
                    log(f"[ack] flush failed (ignored): {exc}")

        ack_flush_thread = threading.Thread(target=_ack_flush_loop, daemon=True)
        ack_flush_thread.start()

        seen_subtypes = set()
        last_packet_time = time.time()
        try:
            while True:
                try:
                    data, addr = self.sock.recvfrom(65535)
                except (socket.timeout, ConnectionResetError, OSError):
                    data = None
                    addr = None

                # BUG FIX: this stall check used to live only inside the recvfrom() timeout
                # branch above, so it silently NEVER fired whenever keepalive pings (0xE1)
                # kept arriving roughly once a second while video had genuinely stopped -
                # recvfrom() succeeds on those pings, so it never times out, so the "no video
                # for 5s" check never ran. Verified live: a session can go fully silent on
                # video while still exchanging pings, and the bridge would just sit there
                # until the RTSP client itself gave up and disconnected. Now checked on EVERY
                # loop iteration regardless of what (if anything) recvfrom() returned.
                if is_streaming and time.time() - last_video_data > 5.0:
                    stall_retries += 1
                    if stall_retries >= 3:
                        # Re-sending IOCTRL_TYPE_VIDEO_START alone doesn't resume a
                        # session the camera has fully dropped - verified live (twice) -
                        # only a full fresh discover/auth cycle does.
                        self.reconnect_until_success(resolution=resolution)
                        reassembler = frame_reassembler.FrameReassembler(self.session_key)
                        stall_retries = 0
                    else:
                        log("[video] no video data for 5s, re-requesting video start")
                        self.start_video(resolution=resolution)
                    last_video_data = time.time()
                    self.last_video_data_time = last_video_data

                if data is None:
                    continue
                # The socket is reused across reconnect() cycles, so a stray leftover packet
                # from the just-abandoned old session (e.g. a late 0xF0 the old session sends
                # right as we switch to a new camera_port) must not be mistaken for the new one.
                if addr != (self.camera_ip, self.camera_port):
                    continue
                parsed = wire.parse_f1(data)
                if not parsed:
                    debug_log(f"[udp] unparsed non-F1 datagram, {len(data)} bytes: {data[:16].hex()}")
                    continue
                msg_type, body = parsed
                if msg_type == wire.MSG_PING:
                    self._send(wire.build_f1(wire.MSG_PONG))
                    continue
                if msg_type == wire.MSG_STREAM_END:
                    if not is_streaming:
                        # Expected: this is the camera's own ack of our stop_video() request
                        # (or a stray late one), not an unrequested drop - nothing to recover.
                        log("[video] camera signaled stream end (0xF0) while idle, ignoring")
                        continue
                    # Verified live (twice, including post address-filter/keepalive-thread
                    # fixes): re-requesting video after 0xF0 never actually resumes the
                    # stream - it always ends in a reconnect anyway, so waiting through a
                    # resend + 5s timeout first only wastes ~5-10s per cycle. Reconnect
                    # immediately on the very first 0xF0 instead.
                    since_packet = time.time() - last_packet_time
                    log(f"[video] camera signaled stream end (0xF0) after {packet_count} packets/{frame_count} frames "
                        f"({since_packet:.3f}s since last D0 packet), reconnecting")
                    self.reconnect_until_success(resolution=resolution)
                    reassembler = frame_reassembler.FrameReassembler(self.session_key)
                    packet_count = 0
                    frame_count = 0
                    byte_count = 0
                    stall_retries = 0
                    last_video_data = time.time()
                    self.last_video_data_time = last_video_data
                    last_packet_time = time.time()
                    last_frame_hash = None
                    dup_run_len = 0
                    last_heartbeat_time = time.time()
                    expected_data_seq = None
                    reorder_buffer.clear()
                    last_seq_advance_time = time.time()
                    with ack_lock:
                        pending_ack_ids.clear()
                    continue
                if msg_type != wire.MSG_DATA or len(body) < 4 or body[0] != 0xD1:
                    debug_log(f"[udp] non-video F1 msg_type=0x{msg_type:02x} len={len(body)}")
                    continue
                last_packet_time = time.time()
                packet_count += 1
                drw = wire.parse_drw_header(body)
                if not drw:
                    continue
                channel, seq, drw_payload = drw
                # Ack the packet's own DRW sequence number (confirmed via capture analysis:
                # the real app stores each received seq into a per-channel array and later
                # sends that array verbatim as the ack payload) - a locally-incremented
                # counter unrelated to the real seq (the previous behavior) doesn't tell the
                # camera which of its actual packets were received, which likely explains the
                # post-burst throttling.
                _enqueue_ack(seq)
                if channel != wire.CHANNEL_REALTIME_AV:
                    # Other DRW channels (e.g. channel 0) have their own independent sequence
                    # space - mixing their payload bytes into this reassembler's byte stream
                    # was corrupting alignment for every video/audio frame downstream of them.
                    continue
                ordered_payloads = _reorder_deliver(seq, drw_payload)
                for ordered_payload in ordered_payloads:
                    for codec_id, subtype, flag, payload in reassembler.feed(ordered_payload):
                        if subtype not in seen_subtypes:
                            seen_subtypes.add(subtype)
                            debug_log(f"[data] first-seen micro-header subtype=0x{subtype:04x} codec_id={codec_id}")
                        if codec_id == frame_reassembler.CODECID_A_PCM:
                            # Confirmed live: this camera ignores the AAC we request via
                            # IOCTRL_TYPE_AUDIO_START and always sends raw 8kHz mono 16-bit
                            # PCM instead (codec_id=1279) - forward as-is, no repacking needed.
                            if payload:
                                if not seen_real_audio:
                                    seen_real_audio = True
                                    log(f"[audio] first real PCM frame received, len={len(payload)}")
                                server.push_audio_access_unit(payload)
                            continue
                        if codec_id != frame_reassembler.CODEC_H264:
                            continue
                        if not payload:
                            continue
                        last_video_data = time.time()
                        self.last_video_data_time = last_video_data
                        stall_retries = 0
                        frame_count += 1
                        byte_count += len(payload)
                        if time.time() - last_heartbeat_time >= 5.0:
                            # Proves frames are still genuinely flowing even when the sparser
                            # duplicate-run/keyframe diagnostics below have nothing new to report -
                            # avoid mistaking "no new diagnostic event" for "stream stopped".
                            last_heartbeat_time = time.time()
                            debug_log(f"[video] heartbeat: {frame_count} frames/{byte_count} bytes this session, "
                                f"currently {dup_run_len} frame(s) into a duplicate run")
                        if frame_count <= 10:
                            # The real app's own decode logic never attempts to decode until
                            # it sees flag==0 (real keyframe) at
                            # least once - so it's critical to know whether the very first frame(s)
                            # of a FRESH session are ever flag==0, even though aggregated flag
                            # counts across whole sessions have always shown flag==1 only.
                            # Full-payload hash+length (not just a 32-byte prefix) distinguishes a
                            # genuinely duplicated frame from merely a coincidentally-identical
                            # slice-header prefix (frame_num is always 0, so headers look similar).
                            digest = hashlib.md5(payload).hexdigest()[:12]
                            debug_log(f"[video] frame#{frame_count} flag={flag} len={len(payload)} md5={digest} first 32 bytes: {payload[:32].hex()}")
                        if flag == 0:
                            # Log EVERY real keyframe, not just the first - reveals whether the
                            # camera re-sends one periodically (which would explain the "3 states"/
                            # partial-refresh pattern seen visually: real device decoders handle the
                            # top portion of each frame fine but still hit the same broken
                            # ref_pic_list_modification defect partway through EVERY P-frame,
                            # leaving the rest black - so picture quality likely degrades between
                            # keyframes and only partially "resets" when a fresh one arrives).
                            digest = hashlib.md5(payload).hexdigest()[:12]
                            tag = "FIRST ONE THIS SESSION" if not seen_real_keyframe else "repeat keyframe"
                            seen_real_keyframe = True
                            debug_log(f"[video] *** REAL KEYFRAME (flag=0) at frame#{frame_count}, len={len(payload)} md5={digest} - {tag} ***")
                        # Track runs of byte-identical consecutive frames without logging every
                        # single frame - only log when a run of 2+ duplicates ends, with its length.
                        digest = hashlib.md5(payload).hexdigest()[:12]
                        if digest == last_frame_hash:
                            dup_run_len += 1
                        else:
                            if dup_run_len >= 2:
                                debug_log(f"[video] {dup_run_len} consecutive byte-identical frames ended at frame#{frame_count - 1}")
                            dup_run_len = 1
                            last_frame_hash = digest
                        # STUCK ENCODER DETECTOR: verified live that this camera's encoder can
                        # silently stall (after ~70-90s of continuous streaming) and keep
                        # re-transmitting the SAME encoded frame indefinitely at a trickle rate
                        # (not a true "no data" stall, so the 5s-idle stall-retry logic below never
                        # fires) - NOT motion-triggered (confirmed live: 20s of deliberate motion in
                        # front of the camera during a stall did not recover it). Ordinary legitimate
                        # static-scene duplicate runs seen so far have been short (2-4 frames), so a
                        # much longer run is treated as abnormal and worth force-reconnecting over.
                        if dup_run_len == 15:
                            log(f"[video] {dup_run_len} consecutive byte-identical frames - encoder "
                                f"appears stuck, forcing reconnect to see if a fresh session recovers it")
                            self.reconnect_until_success(resolution=resolution)
                            reassembler = frame_reassembler.FrameReassembler(self.session_key)
                            packet_count = 0
                            frame_count = 0
                            byte_count = 0
                            stall_retries = 0
                            last_video_data = time.time()
                            self.last_video_data_time = last_video_data
                            last_packet_time = time.time()
                            last_frame_hash = None
                            dup_run_len = 0
                            last_heartbeat_time = time.time()
                            expected_data_seq = None
                            reorder_buffer.clear()
                            last_seq_advance_time = time.time()
                            with ack_lock:
                                pending_ack_ids.clear()
                            continue
                        # Forward the camera's ORIGINAL, unmodified bytes - no fix_h264_slice() -
                        # since those exact bytes are proven to work on a real H.264 decoder.
                        # NOTE: do NOT gate this on seen_real_keyframe - tried that, but it starves
                        # the RTP stream (nothing but the one-time SPS/PPS primer ever flows) and
                        # real clients (VLC) time out and disconnect after ~10-15s of silence,
                        # before the keyframe (often ~10s+ in) ever arrives. Verified live: just
                        # forwarding everything unconditionally (as before) DOES work - the client
                        # shows garbage until the real keyframe arrives, then self-corrects/resets
                        # its own decoder state on the genuine IDR and becomes visually clear.
                        server.push_access_unit(payload)
        finally:
            stop_keepalive.set()
            stop_ack_flush.set()
