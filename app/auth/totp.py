"""Segundo factor TOTP (RFC 6238, SHA-1, 30 s) con biblioteca estándar."""

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

STEP = 30


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret_b32: str, counter: int, digits: int = 6) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def verify(secret_b32: str, code: str, now: float, window: int = 1) -> bool:
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()) or not secret_b32:
        return False
    counter = int(now // STEP)
    return any(hmac.compare_digest(totp_at(secret_b32, counter + d), code) for d in range(-window, window + 1))


def provisioning_uri(username: str, secret_b32: str, issuer: str = "THOR Vision") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret_b32}&issuer={quote(issuer)}"
