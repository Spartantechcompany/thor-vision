"""Middleware ASGI: el navegador cancela peticiones de imagenes (pestanas, miniaturas perezosas); escribir a esa conexion ya cerrada no es un error de la aplicacion."""


class QuietClosed:
    def __init__(self, app, **kwargs):          # Starlette lo construye como cls(app=app, ...)
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        except RuntimeError as exc:
            if "handler is closed" not in str(exc):
                raise
