"""Las paginas HTML no se guardan en cache; las imagenes y el JSON no se tocan."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.testclient import TestClient

from app.utils.nocache_mw import NoCacheHTML


def test_solo_el_html_lleva_no_cache():
    app = FastAPI()
    app.add_middleware(NoCacheHTML)
    app.get("/p", response_class=HTMLResponse)(lambda: "<html></html>")
    app.get("/j")(lambda: JSONResponse({"a": 1}))
    app.get("/i")(lambda: Response(b"x", media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"}))
    c = TestClient(app)
    assert c.get("/p").headers["cache-control"] == "no-cache"
    assert "cache-control" not in c.get("/j").headers
    assert c.get("/i").headers["cache-control"] == "max-age=3600"
