"""El middleware se monta como lo hace Starlette (cls(app=app)) y solo traga el error de conexion cerrada."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils.quiet import QuietClosed


def test_middleware_builds_in_a_real_app_and_swallows_only_closed_handler():
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    app.add_middleware(QuietClosed)
    assert TestClient(app).get("/ok").json() == {"ok": True}          # la pila se construye (antes fallaba con TypeError)

    async def closed(scope, receive, send):
        raise RuntimeError("unable to perform operation on <TCPTransport>; the handler is closed")

    async def other(scope, receive, send):
        raise RuntimeError("otro error")

    asyncio.run(QuietClosed(closed)({"type": "http"}, None, None))     # no propaga
    try:
        asyncio.run(QuietClosed(other)({"type": "http"}, None, None))
        assert False, "debia propagar"
    except RuntimeError as exc:
        assert str(exc) == "otro error"
