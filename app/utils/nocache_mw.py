"""Las paginas HTML (panel, camara, identidades...) no se guardan en cache del navegador: Safari conservaba copias viejas al volver a una pestana."""


class NoCacheHTML:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def _send(msg):
            if msg["type"] == "http.response.start":
                hs = msg.get("headers") or []
                if any(k.lower() == b"content-type" and v.startswith(b"text/html") for k, v in hs) and not any(k.lower() == b"cache-control" for k, v in hs):
                    msg["headers"] = [*hs, (b"cache-control", b"no-cache")]
            await send(msg)
        return await self.app(scope, receive, _send)
