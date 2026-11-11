"""Guardia de acceso en ASGI puro: cubre HTTP y WebSocket. Sin sesión válida, nada pasa salvo las rutas públicas.

Cada solicitud revisa que la cuenta siga activa y que la IP no tenga un bloqueo vigente. Las máquinas (eventos, NOC)
entran solo con un token de máquina y solo a las rutas de detección y cámaras.
"""

import hmac
import os
from http.cookies import SimpleCookie

from app.auth import sessions, store

PUBLIC = {"/login", "/codigo", "/enrolar", "/enrolar/qr.svg", "/logout"}
MACHINE_PREFIXES = ("/api/detections", "/api/cameras")
ADMIN_PREFIX = "/admin"
LOOPBACK = {"127.0.0.1", "::1"}
COOKIE_FULL = "thor_sesion"


def _cookies(scope) -> dict:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            c = SimpleCookie()
            c.load(v.decode("latin-1"))
            return {name: m.value for name, m in c.items()}
    return {}


def _bearer(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            t = v.decode("latin-1")
            return t[7:] if t.lower().startswith("bearer ") else ""
    return ""


def _ip(scope) -> str:
    cliente = scope.get("client")
    return cliente[0] if cliente else "desconocida"


def _machine_ok(scope) -> bool:
    token = os.environ.get("THOR_MACHINE_TOKEN", "")
    return (len(token) >= 32 and scope["path"].startswith(MACHINE_PREFIXES)
            and hmac.compare_digest(_bearer(scope).encode(), token.encode()))


def _cuenta_con_sesion(scope) -> dict | None:
    """La cuenta de la sesión completa, si sigue existiendo y activa. Desactivar una cuenta corta sus sesiones."""
    v = sessions.verify(_cookies(scope).get(COOKIE_FULL))
    if not v or v[1] != sessions.FULL:
        return None
    cuenta = store.get_user(v[0])
    return cuenta if cuenta and cuenta["active"] else None


class AuthGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if _machine_ok(scope):
            return await self.app(scope, receive, send)
        ip = _ip(scope)
        if ip not in LOOPBACK and store.ip_block(ip):
            return await self._negar(scope, send, 403, "Acceso bloqueado. Contacte a TI.")
        if path in PUBLIC:
            return await self.app(scope, receive, send)
        cuenta = _cuenta_con_sesion(scope)
        if cuenta is None:
            return await self._sin_sesion(scope, send)
        if path.startswith(ADMIN_PREFIX) and cuenta["role"] != "admin":
            return await self._negar(scope, send, 403, "Solo para administradores.")
        return await self.app(scope, receive, send)

    async def _sin_sesion(self, scope, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        if scope.get("method") == "GET" and "text/html" in accept and not scope["path"].startswith("/api/"):
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/login"), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await self._json(send, 401, b'{"detail":"Inicie sesion"}')

    async def _negar(self, scope, send, status: int, mensaje: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._json(send, status, ('{"detail":"%s"}' % mensaje).encode("utf-8"))

    @staticmethod
    async def _json(send, status: int, body: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
