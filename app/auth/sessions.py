"""Sesiones firmadas (HMAC-SHA256). Dos etapas: «pwd» (contraseña bien, falta el código) y «full» (sesión completa)."""

import hashlib
import hmac
import os
import time

FULL_TTL_S = 8 * 3600
STAGE_TTL_S = 10 * 60
PWD_ENROL = "enrol"   # contraseña correcta y sin autenticador todavía: ir a registrarlo
PWD_MFA = "mfa"       # contraseña correcta: falta el código
FULL = "full"


def secret() -> str:
    return os.environ.get("THOR_SESSION_SECRET", "")


def configured() -> bool:
    return len(secret()) >= 32


def sign(username: str, stage: str, ttl: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    cuerpo = f"{username}|{stage}|{int(now + ttl)}"
    firma = hmac.new(secret().encode(), cuerpo.encode(), hashlib.sha256).hexdigest()
    return f"{cuerpo}|{firma}"


def verify(token: str | None, now: float | None = None) -> tuple[str, str] | None:
    """(usuario, etapa) si el token es válido y vigente; None si no."""
    if not token or not configured():
        return None
    now = time.time() if now is None else now
    try:
        username, stage, exp, firma = token.rsplit("|", 3)
    except ValueError:
        return None
    esperada = hmac.new(secret().encode(), f"{username}|{stage}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(firma, esperada) or now > int(exp):
        return None
    return username, stage
