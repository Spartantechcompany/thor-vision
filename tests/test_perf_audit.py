"""Correcciones de la auditoria: gzip selectivo, limpieza del volcado de depuracion y cache de la linea de tiempo."""
import asyncio
import gzip

from app.utils.gzip_mw import SelectiveGZip


async def _call(app, path, headers=(("accept-encoding", "gzip"),)):
    sent = []
    scope = {"type": "http", "method": "GET", "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers], "query_string": b"", "scheme": "http", "server": ("x", 80)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict((k.decode(), v.decode()) for k, v in start["headers"]), b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _app(ctype):
    async def app(scope, receive, send):
        body = b"x" * 5000
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
    return app


def test_json_y_html_se_comprimen():
    h, b = asyncio.run(_call(SelectiveGZip(_app("application/json")), "/api/timeline"))
    assert h.get("content-encoding") == "gzip" and gzip.decompress(b) == b"x" * 5000 and len(b) < 200


def test_imagenes_y_clips_no_se_comprimen():
    for path in ("/api/snapshot/cam-189", "/api/clips/file/3", "/api/snapshots/file/9"):
        h, b = asyncio.run(_call(SelectiveGZip(_app("image/jpeg")), path))
        assert "content-encoding" not in h and len(b) == 5000


def test_la_limpieza_del_volcado_ignora_carpetas(tmp_path, monkeypatch):
    from app.vision import vlm_analyzer as va
    (tmp_path / "latest").mkdir()
    for i in range(5):
        (tmp_path / f"f{i}.jpg").write_bytes(b"x")
    monkeypatch.setattr(va, "DEBUG_DIR", tmp_path)
    monkeypatch.setattr(va, "DEBUG_MAX_FILES", 3)
    va._dump_debug("tag", b"media", ".jpg", "{}")
    assert (tmp_path / "latest").is_dir() and len([f for f in tmp_path.iterdir() if f.is_file()]) <= 3 + 2
