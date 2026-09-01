#!/usr/bin/env python3
"""GStreamer-backed RTSP server that DECODES this camera's H.264 (software) and RE-ENCODES
it into a clean, spec-compliant stream before serving it over RTSP.

Background: earlier sessions blamed ffmpeg/libavcodec for outright rejecting this camera's
bitstream (zero real keyframes, num_ref_idx exceeding the SPS's own max, malformed
ref_pic_list_modification) and required a VAAPI GPU decoder to tolerate it. Confirmed live
(2026-09) that was wrong: once the actual bugs in the UDP reassembly/reordering/decryption
pipeline upstream of this module were fixed (see abus-protocol.md), plain software decode
(avdec_h264/openh264dec) handles this stream fine - the bitstream quirks are real but not
severe enough that standard libraries choke on them; what looked like a decoder-tolerance
problem was actually corrupted input reaching the decoder. No GPU/VAAPI requirement anymore.

So instead of passing the camera's raw NAL units straight through to whatever decoder the
RTSP client uses, this module decodes once, server-side, and re-encodes to a normal,
standards-compliant H.264 stream any ordinary player (VLC, ffplay, etc.) can play.

Pipeline: appsrc -> h264parse -> avdec_h264/openh264dec -> videoconvert ->
          x264enc -> h264parse -> rtph264pay (pay0)
          appsrc (asrc) -> audioconvert -> rtpL16pay (pay1)  [audio, opt-in --enable-audio;
          camera sends raw 8kHz mono PCM despite the AAC we request via AUDIO_START]

Public surface: start()/push_access_unit()/push_audio_access_unit(),
on_first_client_connect/on_last_client_disconnect callbacks.

Requires (Debian/Ubuntu): gstreamer1.0-tools gstreamer1.0-plugins-base
gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav (avdec_h264)
gir1.2-gst-rtsp-server-1.0 python3-gi, plus gstreamer1.0-plugins-ugly for x264enc if using
the software encoder option.

Optional RTSP Basic auth (auth_username/auth_password) uses gst-rtsp-server's own
GstRTSPAuth/permissions mechanism (the same pattern as upstream's examples/test-auth.c, ported
to this GI binding's non-variadic equivalents). Confirmed live (2026-09) that two of the C
API's variadic calls aren't exposed here: `RTSPToken.new()` takes zero arguments (role is set
afterwards via `token.set_string(...)`), and `RTSPMediaFactory.add_role(...)` doesn't exist at
all (use a `RTSPPermissions` object with `add_permission_for_role(role, permission, allowed)`,
attached via `factory.set_permissions(...)`). If it's still wrong for your GStreamer version,
__init__ raises immediately rather than silently serving unauthenticated - test explicitly
(both a rejected plain `rtsp://host/abus` and an accepted `rtsp://user:pass@host/abus`) after
deploying.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtsp", "1.0")
    gi.require_version("GstRtspServer", "1.0")
    from gi.repository import GLib, Gst, GstRtsp, GstRtspServer
    _IMPORT_ERROR: Optional[Exception] = None
except (ImportError, ValueError) as _exc:  # pragma: no cover - depends on system packages
    GLib = Gst = GstRtsp = GstRtspServer = None  # type: ignore
    _IMPORT_ERROR = _exc

def _factory_exists(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None

class GstAbusRtspServer:
    """Decode (VAAPI) + re-encode + serve over RTSP via gst-rtsp-server."""

    def __init__(self, sps_nal: bytes, pps_nal: bytes, host: str = "0.0.0.0", port: int = 8554,
                 path: str = "abus", enable_audio: bool = True,
                 auth_username: Optional[str] = None, auth_password: Optional[str] = None,
                 on_first_client_connect: Optional[Callable[[], None]] = None,
                 on_last_client_disconnect: Optional[Callable[[], None]] = None):
        if _IMPORT_ERROR is not None:
            raise RuntimeError(
                "GStreamer + gst-rtsp-server + PyGObject are required for gst_rtsp_server "
                "(see this module's docstring for the packages to install)"
            ) from _IMPORT_ERROR
        Gst.init(None)
        self.host = host
        self.port = port
        self.path = path
        self._sps_au = sps_nal if sps_nal.startswith(b"\x00\x00\x00\x01") else b"\x00\x00\x00\x01" + sps_nal
        self._pps_au = pps_nal if pps_nal.startswith(b"\x00\x00\x00\x01") else b"\x00\x00\x00\x01" + pps_nal
        self._on_first_client_connect = on_first_client_connect
        self._on_last_client_disconnect = on_last_client_disconnect

        self._lock = threading.Lock()
        self._appsrc: Optional["Gst.Element"] = None
        self._audio_appsrc: Optional["Gst.Element"] = None
        self._snapshot_sink: Optional["Gst.Element"] = None
        self._active = False

        pipeline_desc = self._build_pipeline(enable_audio)

        self._server = GstRtspServer.RTSPServer()
        self._server.set_address(host)
        self._server.set_service(str(port))
        self._server.connect("client-connected", self._on_client_connected)

        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(pipeline_desc)
        # Force RTP-over-TCP (RTSP-interleaved) only - refuses UDP SETUP requests entirely.
        # These are large (tens of KB) 1080p frames, FU-A-fragmented into many RTP/UDP
        # packets each; losing even one fragment over a lossy WiFi hop mid-frame corrupts
        # everything after it in that frame - reproduced live as "structured on one side,
        # pure static on the other" garbage on both Android and iOS VLC. TCP guarantees
        # in-order, lossless delivery, ruling this out as a cause of that corruption.
        factory.set_protocols(GstRtsp.RTSPLowerTrans.TCP)
        # Decode+encode is real GPU/CPU work - transcode ONCE and fan the result out to every
        # connected client, instead of running a separate transcode per client.
        factory.set_shared(True)
        factory.connect("media-configure", self._on_media_configure)
        if auth_username and auth_password:
            self._configure_auth(factory, auth_username, auth_password)
        self._server.get_mount_points().add_factory(f"/{path}", factory)

        self._loop = GLib.MainLoop()
        self._loop_thread: Optional[threading.Thread] = None

    def _configure_auth(self, factory, username: str, password: str) -> None:
        """RTSP Basic auth restricting this factory to one role, via gst-rtsp-server's own
        GstRTSPAuth/permissions mechanism. A security feature must fail closed: if this API
        isn't available/working as expected on the installed GStreamer version, raise
        immediately rather than silently starting the server unauthenticated."""
        try:
            auth = GstRtspServer.RTSPAuth()
            # RTSPToken.new() takes no args in this GI binding (unlike the variadic C API) -
            # build it empty and set the role field separately.
            token = GstRtspServer.RTSPToken.new()
            token.set_string(GstRtspServer.RTSP_TOKEN_MEDIA_FACTORY_ROLE, username)
            basic = GstRtspServer.RTSPAuth.make_basic(username, password)
            auth.add_basic(basic, token)
            # factory.add_role(...) is the variadic C API and isn't exposed by this GI
            # binding - build a GstRTSPPermissions object (non-variadic per-role setter) and
            # attach it to the factory instead.
            permissions = GstRtspServer.RTSPPermissions.new()
            permissions.add_permission_for_role(username, GstRtspServer.RTSP_PERM_MEDIA_FACTORY_ACCESS, True)
            permissions.add_permission_for_role(username, GstRtspServer.RTSP_PERM_MEDIA_FACTORY_CONSTRUCT, True)
            factory.set_permissions(permissions)
            self._server.set_auth(auth)
        except Exception as exc:
            raise RuntimeError(
                "Failed to configure RTSP Basic auth via gst-rtsp-server's GstRTSPAuth "
                f"({exc}) - refusing to start unauthenticated since auth was requested. "
                "Pass auth_username/auth_password=None to run without RTSP auth."
            ) from exc

    def _on_client_connected(self, server, client) -> None:
        # Fires on every raw RTSP TCP connection, before DESCRIBE/SETUP/PLAY - confirms the
        # GLib main loop is actually dispatching gst-rtsp-server's own events at all.
        print("[rtsp-gst] RTSP client TCP-connected", flush=True)

    def _build_pipeline(self, enable_audio: bool) -> str:
        # Prefer avdec_h264 (ffmpeg backend inside gstreamer); fall back to openh264dec
        if _factory_exists("avdec_h264"):
            decoder = "avdec_h264"
        elif _factory_exists("openh264dec"):
            decoder = "openh264dec"
        else:
            raise RuntimeError("No software H.264 decoding element found (looked for avdec_h264/openh264dec).")

        pre_tee = [
            # block=false + a bounded, leaky (drop-oldest) queue means a slow/stalled RTSP
            # client can never block the caller of push_access_unit() (the camera's own
            # UDP-receive loop). leaky-type on appsrc requires GStreamer >= 1.20.
            "appsrc name=src is-live=true format=time do-timestamp=true block=false "
            "max-bytes=4000000 leaky-type=downstream "
            "caps=video/x-h264,stream-format=byte-stream,alignment=au",
            "h264parse",
            decoder,
        ]
        # NOTE: this used to force `video/x-raw,format=NV12` right here, because VAAPI
        # decoders output GPU-memory NV12 surfaces and downstream negotiation needed an
        # explicit system-memory format to land on. Now that decode is software
        # (avdec_h264/openh264dec, which natively produce I420, not NV12), that forced caps
        # string - with no videoconvert in between to actually perform the conversion - would
        # very likely fail caps negotiation outright. A plain videoconvert lets the decoder's
        # real native output format negotiate normally; both branches below already have
        # their own videoconvert/explicit format right after the tee anyway.
        pre_tee.append("videoconvert")
        # Split the decoded raw video here: one branch re-encodes for RTSP (as before), the
        # other keeps a standing JPEG copy of the latest frame for the /snapshot endpoint -
        # see get_snapshot_jpeg(). Each branch gets its own queue so a stall in one (e.g. a
        # slow snapshot request) can never back up into the other via the shared tee.
        pre_tee.append("tee name=vtee")
        pre_tee_chain = " ! ".join(pre_tee)

        video_branch = " ! ".join([
            "vtee.", "queue",
            "videoconvert",
            "video/x-raw,format=I420",
            "x264enc tune=zerolatency speed-preset=veryfast key-int-max=60",
            "h264parse config-interval=-1",
            "rtph264pay name=pay0 pt=96 config-interval=-1",
        ])
        # drop=true + max-buffers=1 means this branch can never accumulate a backlog - it
        # always holds just the single most recent frame, encoded to JPEG on arrival.
        # get_snapshot_jpeg() reads the appsink's "last-sample" property on demand; no
        # signal/pull-sample round-trip needed for an occasional snapshot request.
        snapshot_branch = (
            "vtee. ! queue leaky=downstream max-size-buffers=1 ! videoconvert ! jpegenc ! "
            "appsink name=snapshot_sink sync=false max-buffers=1 drop=true "
            "emit-signals=false enable-last-sample=true"
        )
        video_chain = f"{pre_tee_chain} {video_branch} {snapshot_branch}"
        if not enable_audio:
            return f"( {video_chain} )"

        # Confirmed live: despite requesting AAC via IOCTRL_TYPE_AUDIO_START, this camera
        # always sends raw PCM instead (8kHz mono 16-bit, AudioTrack's native little-endian
        # format - see CODECID_A_PCM/PCM_SAMPLE_RATE_HZ in abus_rtsp_bridge.py). Unlike the
        # ADTS-AAC approach tried first, these caps are FULLY specified up front (no need to
        # inspect any real frame to derive rate/channels), so audioconvert/rtpL16pay can
        # negotiate immediately at pipeline-preroll time - this should avoid the caps-
        # negotiation stall that broke the whole session (video included) when the audio
        # branch had no real ADTS data to derive caps from. rtpL16pay requires big-endian
        # samples per RFC 3551; audioconvert does the endianness swap.
        audio_chain = (
            "appsrc name=asrc is-live=true format=time do-timestamp=true block=false "
            "max-bytes=1000000 leaky-type=downstream "
            "caps=audio/x-raw,format=S16LE,rate=8000,channels=1,layout=interleaved ! "
            "audioconvert ! audio/x-raw,format=S16BE ! rtpL16pay name=pay1 pt=98"
        )
        return f"( {video_chain} {audio_chain} )"

    def _on_media_configure(self, factory, media) -> None:
        print("[rtsp-gst] media-configure fired (client is describing/setting up)", flush=True)
        appsrc = media.get_element().get_child_by_name("src")
        audio_appsrc = media.get_element().get_child_by_name("asrc")
        snapshot_sink = media.get_element().get_child_by_name("snapshot_sink")
        with self._lock:
            self._appsrc = appsrc
            self._audio_appsrc = audio_appsrc
            self._snapshot_sink = snapshot_sink
        # The camera never sends real SPS/PPS NALs - prime the decoder with the known,
        # hardcoded ones before any real frame so h264parse/the VAAPI decoder can configure
        # themselves. The RE-ENCODER downstream produces its OWN, fully compliant SPS/PPS for
        # whatever plays the served stream - this priming is only needed for our own decode.
        appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(self._sps_au + self._pps_au))
        media.connect("new-state", self._on_media_new_state)
        media.connect("unprepared", self._on_media_unprepared)
        # Trigger the camera start HERE (at DESCRIBE time), not on new-state->PLAYING: our
        # decoder/encoder are NOT live sources, so the pipeline can only preroll/negotiate
        # caps once real camera frames start flowing through appsrc - but gst-rtsp-server
        # needs that same negotiation to succeed before DESCRIBE can even return an SDP and
        # advance the media towards PLAYING at all. Waiting for PLAYING here is a deadlock:
        # confirmed live - "new-state" never fired, DESCRIBE never completed, VLC timed out.
        with self._lock:
            if self._active:
                return
            self._active = True
        if self._on_first_client_connect:
            self._on_first_client_connect()

    def _on_media_new_state(self, media, state) -> None:
        # Purely diagnostic now - the camera-start trigger lives in _on_media_configure()
        # (see its comment for why waiting for PLAYING here deadlocks).
        print(f"[rtsp-gst] media new-state: {Gst.Element.state_get_name(state)} ({int(state)})", flush=True)

    def _on_media_unprepared(self, media) -> None:
        print("[rtsp-gst] media unprepared (all clients gone)", flush=True)
        with self._lock:
            was_active = self._active
            self._active = False
            self._appsrc = None
            self._audio_appsrc = None
            self._snapshot_sink = None
        if was_active and self._on_last_client_disconnect:
            self._on_last_client_disconnect()

    def start(self) -> None:
        if self._server.attach(None) == 0:
            raise OSError(f"gst-rtsp-server failed to bind {self.host}:{self.port}")
        self._loop_thread = threading.Thread(target=self._loop.run, daemon=True)
        self._loop_thread.start()
        print(f"[rtsp-gst] listening on rtsp://{self.host}:{self.port}/{self.path}", flush=True)

    def push_access_unit(self, annexb_payload: bytes) -> None:
        """Forward one already-camera-encoded access unit (raw Annex-B bytes, unmodified)
        into the shared decode/re-encode pipeline, if a client is currently connected."""
        with self._lock:
            appsrc = self._appsrc
        if appsrc is None:
            return
        appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(annexb_payload)))

    def push_audio_access_unit(self, pcm_frame: bytes) -> None:
        """Forward one chunk of raw camera PCM audio (8kHz mono S16LE, unmodified) into the
        audio branch, if a client is currently connected."""
        with self._lock:
            audio_appsrc = self._audio_appsrc
        if audio_appsrc is None:
            return
        audio_appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(pcm_frame)))

    def get_snapshot_jpeg(self) -> Optional[bytes]:
        """Return the most recently decoded video frame, JPEG-encoded, or None if no client
        is currently connected / no frame has been decoded yet. Cheap - just reads the
        snapshot appsink's already-encoded last sample, no extra pipeline work triggered."""
        with self._lock:
            sink = self._snapshot_sink
        if sink is None:
            return None
        sample = sink.get_property("last-sample")
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
