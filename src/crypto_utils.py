#!/usr/bin/env python3
"""AES-128-ECB helpers shared by the auth handshake and the AV frame reassembler.

The session key/password are always used with AES-128-ECB, 16-byte blocks - see
wire_protocol.py's module docstring for how the password-derived key fits into the auth
handshake.
"""
try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    from Cryptodome.Cipher import AES  # python < 3.14


def derive_password_key(password: str) -> bytes:
    """The view password, UTF-8 encoded and zero-padded (or truncated) to 16 bytes."""
    key = password.encode("utf-8")
    if len(key) > 16:
        return key[:16]
    return key.ljust(16, b"\x00")


def encrypt_block(password: str, block16: bytes) -> bytes:
    return AES.new(derive_password_key(password), AES.MODE_ECB).encrypt(block16)


def decrypt_block(password: str, block16: bytes) -> bytes:
    return AES.new(derive_password_key(password), AES.MODE_ECB).decrypt(block16)


def new_ecb_cipher(key: bytes):
    """A ready-to-use AES-128-ECB cipher for an already-known key (e.g. the session key,
    as opposed to encrypt_block()/decrypt_block()'s password-derived key)."""
    return AES.new(key, AES.MODE_ECB)
