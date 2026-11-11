"""Contraseñas con scrypt (biblioteca estándar). Nunca se guarda ni se registra la contraseña en claro."""

import base64
import hashlib
import hmac
import secrets

SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return "scrypt:" + base64.urlsafe_b64encode(salt).decode() + ":" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split(":")
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT), expected)
