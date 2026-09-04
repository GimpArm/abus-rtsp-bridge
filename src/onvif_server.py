#!/usr/bin/env python3
"""Minimal ONVIF (Profile S subset) device server: WS-Discovery + Device/Media/PTZ SOAP
services, hand-rolled (no WSDL-generated bindings, no third-party ONVIF/SOAP libraries) -
just stdlib http.server/socket/xml.etree.ElementTree, matching the rest of this project's
dependency-light style.

Implements just enough for common ONVIF clients/NVRs to discover the device, fetch an RTSP
stream URI, and drive PTZ:
  - WS-Discovery (UDP multicast 239.255.255.250:3702): responds to Probe with ProbeMatches.
  - Device service (/onvif/device_service): GetSystemDateAndTime, GetCapabilities,
    GetServices, GetDeviceInformation, GetScopes.
  - Media service (/onvif/media_service): GetProfiles/GetProfile, GetVideoSources,
    GetStreamUri (returns the real RTSP URL this bridge already serves).
  - PTZ service (/onvif/ptz_service): GetNodes/GetNode, GetConfigurations/GetConfiguration,
    ContinuousMove, RelativeMove, Stop, GetStatus, GetPresets, GotoPreset (3 fixed slots,
    matching the app's own 3 saved-position buttons - SetPreset is not implemented).

IMPORTANT LIMITATION: this camera's own protocol only supports a single discrete "move N
steps" command (see move_ptz() in abus_rtsp_bridge.py) - there is no real "start moving,
keep moving until told to stop" primitive to map ONVIF's ContinuousMove onto. Each
ContinuousMove/RelativeMove call is therefore translated into ONE discrete move in the
requested direction; Stop is a no-op. Clients that repeat ContinuousMove while a directional
button is held (common in NVR UIs) will still get a reasonable "keep moving" experience.

Optional auth (start_onvif_server()'s username/password args; unset means anyone on the LAN
can control it) accepts EITHER of the two schemes real ONVIF clients use:
  - WS-Security UsernameToken in the SOAP header (PasswordDigest or PasswordText) - this is
    what NVR/VMS clients built on python-onvif-zeep-async (e.g. Frigate) send by default;
    they do NOT send HTTP Basic auth, so without this a configured username/password made
    every request from them fail with 401 even with correct credentials.
  - HTTP Basic auth (shared with the PTZ REST server) - for simpler tools/manual testing.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import http.server
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional

import http_basic_auth

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)

NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsdd": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}


def _local_name(tag: str) -> str:
    """'{namespace}Foo' -> 'Foo' - dispatch by local name only, ignore namespace/prefix."""
    return tag.rsplit("}", 1)[-1]


def _find(elem: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in elem.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _soap_envelope(body_inner_xml: str, header_inner_xml: str = "") -> bytes:
    header = f"<soap:Header>{header_inner_xml}</soap:Header>" if header_inner_xml else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{header}<soap:Body>{body_inner_xml}</soap:Body></soap:Envelope>"
    )
    return xml.encode("utf-8")


def _soap_fault(reason: str) -> bytes:
    return _soap_envelope(
        "<soap:Fault><soap:Code><soap:Value>soap:Sender</soap:Value></soap:Code>"
        f"<soap:Reason><soap:Text xml:lang=\"en\">{reason}</soap:Text></soap:Reason></soap:Fault>"
    )


def _xesc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --- WS-Security UsernameToken (WSSE) auth, as sent by real ONVIF clients (e.g. Frigate's
# onvif-zeep-async) instead of HTTP Basic auth -------------------------------------------

_WSSE_TIME_WINDOW_SECONDS = 300.0  # ONVIF/WS-Security convention; also bounds the nonce cache below
_seen_nonces_lock = threading.Lock()
_seen_nonces: dict[str, float] = {}  # f"{nonce_b64}|{created}" -> first-seen monotonic time


def _wsse_created_within_window(created: str) -> bool:
    try:
        ts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    age = abs((datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds())
    return age <= _WSSE_TIME_WINDOW_SECONDS


def _wsse_consume_nonce_once(nonce_b64: str, created: str) -> bool:
    """A digest can only ever be replayed if the same (nonce, created) pair is reused -
    reject exact replays within the time window instead of just checking the digest math."""
    key = f"{nonce_b64}|{created}"
    now = time.monotonic()
    with _seen_nonces_lock:
        for stale_key in [k for k, seen_at in _seen_nonces.items() if now - seen_at > _WSSE_TIME_WINDOW_SECONDS]:
            del _seen_nonces[stale_key]
        if key in _seen_nonces:
            return False
        _seen_nonces[key] = now
    return True


def _verify_wsse_username_token(token_elem: ET.Element, expected_username: str, expected_password: str) -> bool:
    username_elem = _find(token_elem, "Username")
    password_elem = _find(token_elem, "Password")
    if username_elem is None or password_elem is None:
        return False
    if not hmac.compare_digest(username_elem.text or "", expected_username):
        return False
    supplied = password_elem.text or ""
    password_type = password_elem.get("Type", "")
    if password_type.endswith("PasswordText"):
        return hmac.compare_digest(supplied, expected_password)
    # PasswordDigest (the default - e.g. onvif-zeep-async's encrypt=True, which is also its
    # own default): Base64(SHA1(nonce_raw_bytes + created_bytes + password_bytes)).
    nonce_elem = _find(token_elem, "Nonce")
    created_elem = _find(token_elem, "Created")
    if nonce_elem is None or created_elem is None:
        return False
    created = created_elem.text or ""
    if not _wsse_created_within_window(created):
        return False
    try:
        nonce_raw = base64.b64decode(nonce_elem.text or "", validate=True)
    except ValueError:
        return False
    expected_digest = base64.b64encode(
        hashlib.sha1(nonce_raw + created.encode("utf-8") + expected_password.encode("utf-8")).digest()
    ).decode("ascii")
    if not hmac.compare_digest(supplied, expected_digest):
        return False
    return _wsse_consume_nonce_once(nonce_elem.text or "", created)


@dataclass
class OnvifConfig:
    """Everything the SOAP handlers need - filled in once by start_onvif_server()."""
    host: str
    http_port: int
    rtsp_url: str
    manufacturer: str = "JSW"
    model: str = "DC-X8"
    firmware_version: str = "1.0.0"
    serial_number: str = "ABUS-BRIDGE"
    move_ptz: Callable[[int, int], None] = field(default=lambda direction, step: None)
    goto_preset: Callable[[int], None] = field(default=lambda index: None)
    device_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: Optional[str] = None
    password: Optional[str] = None
    max_step_per_move: int = 2

    @property
    def device_xaddr(self) -> str:
        return f"http://{self.host}:{self.http_port}/onvif/device_service"

    @property
    def media_xaddr(self) -> str:
        return f"http://{self.host}:{self.http_port}/onvif/media_service"

    @property
    def ptz_xaddr(self) -> str:
        return f"http://{self.host}:{self.http_port}/onvif/ptz_service"


# --- PTZ direction mapping (ONVIF PanTilt x/y velocity/translation -> our PTZ_* constants) ---
# Imported lazily from abus_rtsp_bridge to avoid a hard import-time circular dependency (that
# module imports this one); direction constants are plain ints so this just needs the values.
PTZ_STOP, PTZ_UP, PTZ_DOWN, PTZ_LEFT, PTZ_RIGHT = 0, 1, 2, 3, 4
PTZ_LEFT_UP, PTZ_LEFT_DOWN, PTZ_RIGHT_UP, PTZ_RIGHT_DOWN = 5, 6, 7, 8


def _pan_tilt_to_direction_step(x: float, y: float, max_step: int = 2) -> tuple[int, int]:
    """Map an ONVIF PanTilt vector (x=pan, y=tilt, each roughly -1..1) to one of our
    discrete PTZ_* direction constants + a step size, the same way the real app
    combines simultaneous horizontal+vertical swipe into a single diagonal move.

    NOTE: `max_step` is deliberately small and NOT the camera's raw 1-16 step range - a
    single ONVIF ContinuousMove/RelativeMove call (e.g. one "click" of an NVR's PTZ button)
    apparently always arrives at (or near) full velocity magnitude regardless of how far the
    user actually wanted to move (confirmed live with Frigate: every click saturated the old
    magnitude*16 calculation to the max, moving ~1/4 of the camera's full sweep per click).
    Calibrated against the camera's own manufacturer spec (270 deg horizontal / 90 deg
    vertical) and live testing: 34 raw steps = a full horizontal sweep, 16 raw steps = a full
    vertical sweep - so max_step=2 is a modest ~16 deg horizontal / ~11 deg vertical nudge per
    call, not a big turn. Tune via OnvifConfig.max_step_per_move / --onvif-ptz-step."""
    horiz = "left" if x < -0.05 else "right" if x > 0.05 else None
    vert = "up" if y > 0.05 else "down" if y < -0.05 else None
    step = max(1, min(max_step, round(max(abs(x), abs(y)) * max_step)))
    direction = {
        (None, None): PTZ_STOP,
        ("left", None): PTZ_LEFT, ("right", None): PTZ_RIGHT,
        (None, "up"): PTZ_UP, (None, "down"): PTZ_DOWN,
        ("left", "up"): PTZ_LEFT_UP, ("left", "down"): PTZ_LEFT_DOWN,
        ("right", "up"): PTZ_RIGHT_UP, ("right", "down"): PTZ_RIGHT_DOWN,
    }[(horiz, vert)]
    return direction, step


def _find_pan_tilt(body: ET.Element) -> Optional[tuple[float, float]]:
    for local_name in ("Velocity", "Translation"):
        container = _find(body, local_name)
        if container is None:
            continue
        pan_tilt = _find(container, "PanTilt")
        if pan_tilt is not None:
            try:
                return float(pan_tilt.get("x", "0")), float(pan_tilt.get("y", "0"))
            except ValueError:
                return None
    return None


# --- Device service ---

def _device_get_system_date_and_time(_body: ET.Element, _cfg: OnvifConfig) -> str:
    t = time.gmtime()
    return (
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:UTCDateTime>"
        f"<tt:Time><tt:Hour>{t.tm_hour}</tt:Hour><tt:Minute>{t.tm_min}</tt:Minute><tt:Second>{t.tm_sec}</tt:Second></tt:Time>"
        f"<tt:Date><tt:Year>{t.tm_year}</tt:Year><tt:Month>{t.tm_mon}</tt:Month><tt:Day>{t.tm_mday}</tt:Day></tt:Date>"
        "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    )


def _device_get_device_information(_body: ET.Element, cfg: OnvifConfig) -> str:
    return (
        "<tds:GetDeviceInformationResponse>"
        f"<tds:Manufacturer>{_xesc(cfg.manufacturer)}</tds:Manufacturer>"
        f"<tds:Model>{_xesc(cfg.model)}</tds:Model>"
        f"<tds:FirmwareVersion>{_xesc(cfg.firmware_version)}</tds:FirmwareVersion>"
        f"<tds:SerialNumber>{_xesc(cfg.serial_number)}</tds:SerialNumber>"
        f"<tds:HardwareId>{_xesc(cfg.serial_number)}</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    )


def _device_get_capabilities(_body: ET.Element, cfg: OnvifConfig) -> str:
    return (
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        f"<tt:Device><tt:XAddr>{cfg.device_xaddr}</tt:XAddr>"
        "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
        "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
        "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
        "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
        "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
        "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tt:SupportedVersions>"
        "</tt:System></tt:Device>"
        f"<tt:Media><tt:XAddr>{cfg.media_xaddr}</tt:XAddr>"
        "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
        "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>"
        f"<tt:PTZ><tt:XAddr>{cfg.ptz_xaddr}</tt:XAddr></tt:PTZ>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    )


def _device_get_services(_body: ET.Element, cfg: OnvifConfig) -> str:
    def svc(namespace: str, xaddr: str) -> str:
        return (f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace><tds:XAddr>{xaddr}</tds:XAddr>"
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>")
    return (
        "<tds:GetServicesResponse>"
        + svc("http://www.onvif.org/ver10/device/wsdl", cfg.device_xaddr)
        + svc("http://www.onvif.org/ver10/media/wsdl", cfg.media_xaddr)
        + svc("http://www.onvif.org/ver20/ptz/wsdl", cfg.ptz_xaddr)
        + "</tds:GetServicesResponse>"
    )


def _device_get_scopes(_body: ET.Element, cfg: OnvifConfig) -> str:
    def scope(item: str) -> str:
        return f"<tds:Scopes><tt:ScopeDef>Configurable</tt:ScopeDef><tt:ScopeItem>{item}</tt:ScopeItem></tds:Scopes>"
    return (
        "<tds:GetScopesResponse>"
        + scope("onvif://www.onvif.org/type/video_encoder")
        + scope("onvif://www.onvif.org/type/ptz")
        + scope(f"onvif://www.onvif.org/hardware/{_xesc(cfg.model)}")
        + scope(f"onvif://www.onvif.org/name/{_xesc(cfg.model)}")
        + "</tds:GetScopesResponse>"
    )


DEVICE_ACTIONS: dict = {
    "GetSystemDateAndTime": _device_get_system_date_and_time,
    "GetDeviceInformation": _device_get_device_information,
    "GetCapabilities": _device_get_capabilities,
    "GetServices": _device_get_services,
    "GetScopes": _device_get_scopes,
}


# --- Media service ---

def _profile_xml() -> str:
    return (
        '<trt:Profiles token="profile_0" fixed="true"><tt:Name>MainProfile</tt:Name>'
        '<tt:VideoSourceConfiguration token="vs0"><tt:Name>VideoSourceConfig</tt:Name>'
        '<tt:UseCount>1</tt:UseCount><tt:SourceToken>vsrc0</tt:SourceToken>'
        '<tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>'
        '<tt:VideoEncoderConfiguration token="vec0"><tt:Name>VideoEncoderConfig</tt:Name>'
        '<tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>'
        '<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>'
        '<tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>'
        '<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>'
        '<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>High</tt:H264Profile></tt:H264>'
        # Multicast + SessionTimeout are mandatory (not minOccurs="0") in the ONVIF
        # VideoEncoderConfiguration schema - a strict/validating client can reject the
        # profile without them even though we don't actually support RTP multicast.
        '<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address>'
        '</tt:Address><tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>'
        '<tt:SessionTimeout>PT60S</tt:SessionTimeout>'
        '</tt:VideoEncoderConfiguration>'
        f'<tt:PTZConfiguration token="ptzcfg0">{_ptz_configuration_inner_xml()}</tt:PTZConfiguration>'
        "</trt:Profiles>"
    )


def _media_get_profiles(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return f"<trt:GetProfilesResponse>{_profile_xml()}</trt:GetProfilesResponse>"


def _media_get_profile(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return f"<trt:GetProfileResponse>{_profile_xml()}</trt:GetProfileResponse>"


def _media_get_video_sources(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return (
        '<trt:GetVideoSourcesResponse><trt:VideoSources token="vsrc0">'
        "<tt:Framerate>15</tt:Framerate>"
        "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
        "</trt:VideoSources></trt:GetVideoSourcesResponse>"
    )


def _media_get_stream_uri(_body: ET.Element, cfg: OnvifConfig) -> str:
    return (
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>{_xesc(cfg.rtsp_url)}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT30S</tt:Timeout>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


MEDIA_ACTIONS: dict = {
    "GetProfiles": _media_get_profiles,
    "GetProfile": _media_get_profile,
    "GetVideoSources": _media_get_video_sources,
    "GetStreamUri": _media_get_stream_uri,
}


# --- PTZ service ---

def _ptz_get_nodes(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return (
        '<tptz:GetNodesResponse><tptz:PTZNode token="ptz_node0"><tt:Name>PTZNode</tt:Name>'
        "<tt:SupportedPTZSpaces>"
        # Frigate (and other strict NVR clients) reject a profile as "not PTZ-capable" unless
        # the node declares the same spaces the profile's PTZConfiguration references as its
        # Default*Space - so these three must stay in sync with _ptz_configuration_inner_xml().
        "<tt:AbsolutePanTiltPositionSpace>"
        "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace</tt:URI>"
        "<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>"
        "</tt:AbsolutePanTiltPositionSpace>"
        "<tt:RelativePanTiltTranslationSpace>"
        "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace</tt:URI>"
        "<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>"
        "</tt:RelativePanTiltTranslationSpace>"
        "<tt:ContinuousPanTiltVelocitySpace>"
        "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/ContinuousVelocityGenericSpace</tt:URI>"
        "<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>"
        "</tt:ContinuousPanTiltVelocitySpace>"
        "</tt:SupportedPTZSpaces>"
        "<tt:MaximumNumberOfPresets>3</tt:MaximumNumberOfPresets>"
        "<tt:HomeSupported>false</tt:HomeSupported>"
        "</tptz:PTZNode></tptz:GetNodesResponse>"
    )


def _ptz_get_node(_body: ET.Element, cfg: OnvifConfig) -> str:
    return _ptz_get_nodes(_body, cfg).replace("GetNodesResponse", "GetNodeResponse", 2)


def _ptz_configuration_inner_xml() -> str:
    """Shared by both the standalone PTZ service response and the copy embedded in the
    media profile (GetProfiles) - Frigate specifically validates the LATTER, so it's not
    enough to only set these on the PTZ service's own GetConfigurations response."""
    return (
        "<tt:Name>PTZConfig</tt:Name><tt:UseCount>1</tt:UseCount>"
        "<tt:NodeToken>ptz_node0</tt:NodeToken>"
        "<tt:DefaultAbsolutePantTiltPositionSpace>"
        "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
        "</tt:DefaultAbsolutePantTiltPositionSpace>"
        "<tt:DefaultRelativePanTiltTranslationSpace>"
        "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
        "</tt:DefaultRelativePanTiltTranslationSpace>"
        "<tt:DefaultContinuousPanTiltVelocitySpace>"
        "http://www.onvif.org/ver10/tptz/PanTiltSpaces/ContinuousVelocityGenericSpace"
        "</tt:DefaultContinuousPanTiltVelocitySpace>"
    )


def _ptz_configuration_xml() -> str:
    return f'<tptz:PTZConfiguration token="ptzcfg0">{_ptz_configuration_inner_xml()}</tptz:PTZConfiguration>'


def _ptz_get_configurations(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return f"<tptz:GetConfigurationsResponse>{_ptz_configuration_xml()}</tptz:GetConfigurationsResponse>"


def _ptz_get_configuration(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return f"<tptz:GetConfigurationResponse>{_ptz_configuration_xml()}</tptz:GetConfigurationResponse>"


def _ptz_continuous_move(body: ET.Element, cfg: OnvifConfig) -> str:
    pan_tilt = _find_pan_tilt(body)
    if pan_tilt is not None:
        direction, step = _pan_tilt_to_direction_step(*pan_tilt, max_step=cfg.max_step_per_move)
        if direction != PTZ_STOP:
            cfg.move_ptz(direction, step)
    return "<tptz:ContinuousMoveResponse/>"


def _ptz_relative_move(body: ET.Element, cfg: OnvifConfig) -> str:
    pan_tilt = _find_pan_tilt(body)
    if pan_tilt is not None:
        direction, step = _pan_tilt_to_direction_step(*pan_tilt, max_step=cfg.max_step_per_move)
        if direction != PTZ_STOP:
            cfg.move_ptz(direction, step)
    return "<tptz:RelativeMoveResponse/>"


def _ptz_stop(_body: ET.Element, cfg: OnvifConfig) -> str:
    # Our camera has no real "stop mid-move" primitive (each move is already a one-shot
    # discrete step, done by the time this arrives) - PTZ_STOP is harmless to send anyway.
    cfg.move_ptz(PTZ_STOP, 0)
    return "<tptz:StopResponse/>"


def _ptz_get_status(_body: ET.Element, _cfg: OnvifConfig) -> str:
    return (
        "<tptz:GetStatusResponse><tptz:PTZStatus>"
        '<tt:Position><tt:PanTilt x="0" y="0"/><tt:Zoom x="0"/></tt:Position>'
        "<tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>IDLE</tt:Zoom></tt:MoveStatus>"
        f"<tt:UtcTime>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</tt:UtcTime>"
        "</tptz:PTZStatus></tptz:GetStatusResponse>"
    )


# Fixed 0-2 slots matching the app's own 3 saved-position buttons (see goto_preset() in
# abus_rtsp_bridge.py) - the camera has no LAN-protocol way to report which slots actually
# have a saved position, so all 3 are always advertised as present; GotoPreset on an unset
# slot is just whatever the camera itself does with it (untested - no way to set one here).
PRESET_TOKENS = ("0", "1", "2")


def _ptz_get_presets(_body: ET.Element, _cfg: OnvifConfig) -> str:
    presets = "".join(
        f'<tptz:Preset token="{token}"><tt:Name>Preset {token}</tt:Name></tptz:Preset>'
        for token in PRESET_TOKENS
    )
    return f"<tptz:GetPresetsResponse>{presets}</tptz:GetPresetsResponse>"


def _ptz_goto_preset(body: ET.Element, cfg: OnvifConfig) -> str:
    token_elem = _find(body, "PresetToken")
    if token_elem is not None and token_elem.text and token_elem.text.strip() in PRESET_TOKENS:
        cfg.goto_preset(int(token_elem.text.strip()))
    return "<tptz:GotoPresetResponse/>"


PTZ_ACTIONS: dict = {
    "GetNodes": _ptz_get_nodes,
    "GetNode": _ptz_get_node,
    "GetConfigurations": _ptz_get_configurations,
    "GetConfiguration": _ptz_get_configuration,
    "ContinuousMove": _ptz_continuous_move,
    "RelativeMove": _ptz_relative_move,
    "Stop": _ptz_stop,
    "GetStatus": _ptz_get_status,
    "GetPresets": _ptz_get_presets,
    "GotoPreset": _ptz_goto_preset,
}

SERVICE_PATHS = {
    "/onvif/device_service": DEVICE_ACTIONS,
    "/onvif/media_service": MEDIA_ACTIONS,
    "/onvif/ptz_service": PTZ_ACTIONS,
}


class _OnvifHandler(http.server.BaseHTTPRequestHandler):
    config: OnvifConfig
    server_version = "abus-onvif/1.0"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        if not http_basic_auth.require_basic_auth(self, self.config.username, self.config.password, realm="abus-onvif"):
            return
        # Some clients probe the device service URL with a bare GET first.
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        actions = SERVICE_PATHS.get(self.path.split("?", 1)[0])
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            root = ET.fromstring(raw) if raw else None
        except ET.ParseError:
            root = None
        if not self._check_auth(root):
            return
        if actions is None:
            self._respond(404, _soap_fault(f"unknown ONVIF service path {self.path!r}"))
            return
        body = _find(root, "Body") if root is not None else None
        action_elem = list(body)[0] if body is not None and len(body) else None
        if action_elem is None:
            self._respond(400, _soap_fault("could not parse SOAP request body"))
            return
        action_name = _local_name(action_elem.tag)
        handler = actions.get(action_name)
        if handler is None:
            self._respond(500, _soap_fault(f"Action Not Supported: {action_name}"))
            return
        try:
            response_body = handler(action_elem, self.config)
        except Exception as exc:  # a bad/unexpected request must not crash the server
            self._respond(500, _soap_fault(f"internal error handling {action_name}: {exc}"))
            return
        self._respond(200, _soap_envelope(response_body))

    def _check_auth(self, root: Optional[ET.Element]) -> bool:
        """Accept EITHER a valid WS-Security UsernameToken in the SOAP header (what real
        ONVIF clients like Frigate's onvif-zeep-async send) or HTTP Basic auth. Body must
        already be read off the socket before this is called either way (HTTP/1.0, so an
        unread body doesn't corrupt a subsequent request, but the WSSE check needs it)."""
        if not self.config.username:
            return True
        token = _find(root, "UsernameToken") if root is not None else None
        if token is not None and _verify_wsse_username_token(token, self.config.username, self.config.password or ""):
            return True
        if self.headers.get("Authorization", ""):
            return http_basic_auth.require_basic_auth(self, self.config.username, self.config.password, realm="abus-onvif")
        self._respond(401, _soap_fault("authentication required (WS-Security UsernameToken or HTTP Basic)"))
        return False

    def _respond(self, code: int, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", 'application/soap+xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _ws_discovery_loop(cfg: OnvifConfig, stop_event: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", WS_DISCOVERY_ADDR[1]))
    mreq = struct.pack("4s4s", socket.inet_aton(WS_DISCOVERY_ADDR[0]), socket.inet_aton(cfg.host))
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        # Some container network setups (e.g. host networking absent) can't join multicast -
        # WS-Discovery just won't work then, but direct http://host:port ONVIF calls still do.
        sock.close()
        return
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            root = ET.fromstring(data)
            body = _find(root, "Body")
            probe = _find(body, "Probe") if body is not None else None
            if probe is None:
                continue
            header = _find(root, "Header")
            message_id_elem = _find(header, "MessageID") if header is not None else None
            relates_to = message_id_elem.text if message_id_elem is not None else ""
        except ET.ParseError:
            continue
        reply = _soap_envelope(
            "<wsdd:ProbeMatches><wsdd:ProbeMatch>"
            f"<wsa:EndpointReference><wsa:Address>urn:uuid:{cfg.device_uuid}</wsa:Address></wsa:EndpointReference>"
            "<wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>"
            f"<wsdd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/{_xesc(cfg.model)}</wsdd:Scopes>"
            f"<wsdd:XAddrs>{cfg.device_xaddr}</wsdd:XAddrs>"
            "<wsdd:MetadataVersion>1</wsdd:MetadataVersion>"
            "</wsdd:ProbeMatch></wsdd:ProbeMatches>",
            header_inner_xml=(
                f"<wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>"
                + (f"<wsa:RelatesTo>{_xesc(relates_to)}</wsa:RelatesTo>" if relates_to else "")
                + "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
                "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>"
            ),
        )
        try:
            sock.sendto(reply, addr)
        except OSError:
            pass
    sock.close()


def start_onvif_server(host: str, http_port: int, rtsp_url: str, move_ptz: Callable[[int, int], None],
                        serial_number: str = "ABUS-BRIDGE", ws_discovery: bool = True,
                        username: Optional[str] = None, password: Optional[str] = None,
                        max_step_per_move: int = 2,
                        goto_preset: Callable[[int], None] = lambda index: None,
                        bind_host: str = "0.0.0.0",
                        log: Optional[Callable[[str], None]] = None) -> http.server.ThreadingHTTPServer:
    """Start the ONVIF HTTP/SOAP server (and WS-Discovery responder, unless disabled) in
    background threads. `host` must be a real reachable LAN address (not 0.0.0.0) since it's
    advertised to clients in XAddrs/GetStreamUri/WS-Discovery replies - but the server's own
    listen socket binds to `bind_host` (default 0.0.0.0, i.e. every interface) instead, NOT
    to `host` directly: if address auto-detection ever picks a wrong/unreachable address for
    `host` (e.g. a container-internal address that isn't the real LAN IP), binding the
    listen socket to that same wrong address would make the port completely unreachable from
    anywhere, on top of just being advertised wrong - binding to 0.0.0.0 means the port is
    always actually reachable on every interface regardless, matching how the RTSP/PTZ REST
    servers already behave. HTTP Basic auth is
    enabled iff both username and password are given (note: WS-Discovery itself is always
    unauthenticated - only the SOAP services are gated - since the discovery protocol has no
    such provision). `max_step_per_move` caps how far a single ContinuousMove/RelativeMove
    call (e.g. one NVR PTZ button click) moves the camera - see _pan_tilt_to_direction_step().
    `goto_preset` backs GotoPreset for the camera's 3 fixed preset slots (see PRESET_TOKENS) -
    SetPreset is deliberately not exposed (see goto_preset() in abus_rtsp_bridge.py)."""
    _log = log or (lambda msg: None)
    cfg = OnvifConfig(host=host, http_port=http_port, rtsp_url=rtsp_url,
                       serial_number=serial_number, move_ptz=move_ptz, goto_preset=goto_preset,
                       username=username, password=password, max_step_per_move=max_step_per_move)
    handler_cls = type("_BoundOnvifHandler", (_OnvifHandler,), {"config": cfg})
    server = http.server.ThreadingHTTPServer((bind_host, http_port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"[onvif] device service at {cfg.device_xaddr}{' [auth required]' if username else ''}")
    if ws_discovery:
        stop_event = threading.Event()
        thread = threading.Thread(target=_ws_discovery_loop, args=(cfg, stop_event), daemon=True)
        thread.start()
        _log(f"[onvif] WS-Discovery responder joined {WS_DISCOVERY_ADDR[0]}:{WS_DISCOVERY_ADDR[1]}")
    return server
