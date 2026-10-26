"""Guardia de acceso en ASGI puro: cubre HTTP y WebSocket. Sin sesión válida, nada pasa salvo las rutas públicas.

Las máquinas (eventos, NOC) entran solo con un token de máquina y solo a las rutas de detección y cámaras.
"""

import hmac
import os
from http.cookies import SimpleCookie

from app.auth import sessions

PUBLIC = {"/login", "/codigo", "/enrolar", "/enrolar/qr.svg", "/logout"}
MACHINE_PREFIXES = ("/api/detections", "/api/cameras")
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


def _machine_ok(scope) -> bool:
    token = os.environ.get("THOR_MACHINE_TOKEN", "")
    return (len(token) >= 32 and scope["path"].startswith(MACHINE_PREFIXES)
            and hmac.compare_digest(_bearer(scope).encode(), token.encode()))


def _full_session(scope) -> bool:
    v = sessions.verify(_cookies(scope).get(COOKIE_FULL))
    return bool(v and v[1] == sessions.FULL)


class AuthGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in PUBLIC or _full_session(scope) or _machine_ok(scope):
            return await self.app(scope, receive, send)
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        if scope.get("method") == "GET" and "text/html" in accept and not path.startswith("/api/"):
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/login"), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        body = b'{"detail":"Inicie sesion"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
