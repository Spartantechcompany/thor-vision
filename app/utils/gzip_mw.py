"""Compresion gzip de paginas y JSON (el panel pesa 220 KB y la linea de tiempo 640 KB sin comprimir). No comprime imagenes, video, flujos ni websockets."""
from starlette.middleware.gzip import GZipMiddleware

SKIP_PREFIXES = ("/api/snapshot", "/api/snapshots/file", "/api/clips/file", "/api/stream", "/api/pets/crops/", "/api/faces/", "/api/subjects/", "/api/person-visits/",
                 "/api/face-sightings/", "/api/vehicles/", "/api/eval/image", "/ws", "/api/debug/payload", "/api/chat/stream", "/api/search/stream")


class SelectiveGZip:
    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 5):
        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("path", "").startswith(SKIP_PREFIXES):
            return await self.gz(scope, receive, send)
        return await self.app(scope, receive, send)
