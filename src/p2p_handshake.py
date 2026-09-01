#!/usr/bin/env python3
"""P2P Handshake helper implementing the CS2 / iLnkP2P protocol layer."""
import struct

# Payload-obfuscation table used by this protocol family's XOR/substitution scheme (a real
# constant, not derived/guessed) - kept for whatever packet type actually uses it; NOT the F9
# payload below, see generate_authenticated_f9_payload()'s docstring for why.
_TABLE_MASK = 0x5A
_MASKED_TABLE = (
    19, 3, 25, 103, 239, 229, 55, 249, 29, 9, 21, 59,
    63, 185, 43, 179, 61, 37, 88, 89, 81, 247, 233, 211,
    113, 117, 111, 155, 49, 209, 207, 205, 75, 191, 253, 87,
    181, 171, 95, 93, 217, 161, 199, 97, 159, 157, 73, 77,
    71, 69, 127, 115, 137, 133,
)
_OBFUSCATION_TABLE = tuple(v ^ _TABLE_MASK for v in _MASKED_TABLE)

RENDEZVOUS_SERVERS = ["23.21.195.143", "122.248.232.207", "176.34.104.236"]
PORT_START = 32100

def pppp_encode_payload(payload: bytes) -> bytes:
    out = bytearray()
    for pos, byte in enumerate(payload):
        xor_val = 0x39
        for b in out:
            xor_val ^= b
        encoded_byte = (byte ^ xor_val ^ _OBFUSCATION_TABLE[pos % len(_OBFUSCATION_TABLE)]) & 0xFF
        out.append(encoded_byte)
    return bytes(out)

def build_cs2_packet(cmd_id: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xF1, cmd_id, len(payload)) + payload

def generate_authenticated_f9_payload() -> bytes:
    """The F9 "client verification" packet sent to a rendezvous directory server.

    CORRECTED (2026-09-01): this used to AES-encrypt a "challenge_token_raw" block with a
    password-derived key and run the whole 84-byte result through pppp_encode_payload()'s
    XOR/substitution obfuscation, on the assumption this was a per-session, password-bound
    challenge like the LAN AES handshake. Proven wrong by diffing two independent real
    captures (capture.pcap, capture1.pcap) byte-for-byte: the real wire bytes are sent
    completely in the clear - no encryption, no obfuscation. The first 29 bytes (header +
    what was called challenge_token_raw + the start of the trailing block) are
    byte-IDENTICAL across both captures, confirming they're fixed protocol constants, not a
    real per-session challenge - the password plays no role in this packet at all. The
    remaining ~55 bytes DO differ between the two captures in a way not yet understood (not
    the DID, not an obvious timestamp) - but the rendezvous server evidently doesn't
    validate them strictly, since the old (needlessly encrypted, provably-wrong-format)
    version of this function still got a real "verification cleared" response from a live
    server. Kept as the exact bytes from a known-working real capture (capture.pcap) until/
    unless a server is found that rejects them - see abus-protocol.md for the full analysis.
    """
    return bytes.fromhex(
        "036c0b166151951eb669e8ae5a6d87621bbdd943f44ec2f44f2b5f9400b"
        "f96ec033e3ce2acc28c9d897fde36307a14a25dfd546068154af528fa75"
        "8e5e5a28b727e12235f44c7f9dca9f4d6f51951eb669dfaa37"
    )

