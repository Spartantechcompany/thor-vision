"""Acceso al panel: sin sesión no se entra; contraseña y código en pasos separados; el QR solo la primera vez.

Se prueba con una app mínima que usa el mismo guardia y las mismas rutas de acceso, sin cargar los modelos de visión.
"""
import importlib
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "s" * 40
MACHINE = "m" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("THOR_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("THOR_SESSION_SECRET", SECRET)
    monkeypatch.setenv("THOR_MACHINE_TOKEN", MACHINE)
    for mod in ("app.auth.store", "app.auth.sessions", "app.auth.limiter", "app.auth.guard", "app.auth.routes"):
        importlib.reload(importlib.import_module(mod))
    from app.auth import limiter, passwords, store, totp
    from app.auth.guard import AuthGuard
    from app.auth.routes import router

    app = FastAPI()
    app.include_router(router)

    @app.get("/")
    def panel():
        return {"ok": "panel"}

    @app.get("/api/detections/cam1")
    def det():
        return {"person_count": 0}

    @app.get("/api/zonas")
    def zonas():
        return {"zonas": []}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "detalle": "interno"}

    app.add_middleware(AuthGuard)
    store.create_user("Diego.Reyes", passwords.hash_password("clave-larga-123"))
    limiter.ip_blocked  # noqa: B018
    return TestClient(app, follow_redirects=False), store, totp


BROWSER = {"accept": "text/html,application/xhtml+xml"}


def test_no_session_means_no_access(env):
    c, _, _ = env
    assert c.get("/", headers=BROWSER).status_code == 303
    assert c.get("/", headers=BROWSER).headers["location"] == "/login"
    assert c.get("/api/zonas", headers={"accept": "application/json"}).status_code == 401
    assert c.get("/api/health").status_code == 401, "la salud interna no es pública"


def test_login_page_is_public_and_asks_only_user_and_password(env):
    c, _, _ = env
    r = c.get("/login")
    assert r.status_code == 200 and 'name="usuario"' in r.text and 'name="password"' in r.text
    assert 'name="codigo"' not in r.text, "el código no se pide en el primer paso"


def test_wrong_password_is_refused_without_saying_which_part(env):
    c, _, _ = env
    r = c.post("/login", data={"usuario": "Diego.Reyes", "password": "mala"}, headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 401 and "incorrectos" in r.text


def test_first_login_goes_to_qr_enrollment_then_grants_access(env):
    c, store, totp = env
    r = c.post("/login", data={"usuario": "diego.reyes", "password": "clave-larga-123"},
               headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 303 and r.headers["location"] == "/enrolar"
    assert c.get("/", headers=BROWSER).status_code == 303, "todavía sin sesión completa"
    assert c.get("/enrolar").status_code == 200  # la página genera el secreto y muestra el QR
    qr = c.get("/enrolar/qr.svg")
    assert qr.status_code == 200 and "<svg" in qr.text
    secreto = store.get_user("Diego.Reyes")["totp_secret"]
    assert c.post("/enrolar", data={"codigo": "000000"}, headers={"sec-fetch-site": "same-origin"}).status_code == 401
    ok = c.post("/enrolar", data={"codigo": totp.totp_at(secreto, int(time.time()) // 30)},
                headers={"sec-fetch-site": "same-origin"})
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert c.get("/", headers=BROWSER).status_code == 200


def test_later_logins_need_password_and_code_on_a_separate_screen(env):
    c, store, totp = env
    store.set_pending_secret("Diego.Reyes", totp.new_secret())
    store.enable_totp("Diego.Reyes")
    r = c.post("/login", data={"usuario": "Diego.Reyes", "password": "clave-larga-123"},
               headers={"sec-fetch-site": "same-origin"})
    assert r.headers["location"] == "/codigo"
    assert c.get("/codigo").status_code == 200
    assert c.get("/", headers=BROWSER).status_code == 303
    secreto = store.get_user("Diego.Reyes")["totp_secret"]
    ok = c.post("/codigo", data={"codigo": totp.totp_at(secreto, int(time.time()) // 30)},
                headers={"sec-fetch-site": "same-origin"})
    assert ok.status_code == 303 and c.get("/", headers=BROWSER).status_code == 200


def test_password_alone_does_not_open_the_panel_after_enrollment(env):
    c, store, totp = env
    store.set_pending_secret("Diego.Reyes", totp.new_secret())
    store.enable_totp("Diego.Reyes")
    c.post("/login", data={"usuario": "Diego.Reyes", "password": "clave-larga-123"},
           headers={"sec-fetch-site": "same-origin"})
    assert c.get("/", headers=BROWSER).status_code == 303


def test_brute_force_on_codes_is_blocked_by_ip_limit(env):
    c, store, totp = env
    store.set_pending_secret("Diego.Reyes", totp.new_secret())
    store.enable_totp("Diego.Reyes")
    h = {"sec-fetch-site": "same-origin"}
    c.post("/login", data={"usuario": "Diego.Reyes", "password": "clave-larga-123"}, headers=h)
    codigos = [c.post("/codigo", data={"codigo": f"{n:06d}"}, headers=h).status_code for n in range(7)]
    # 429 = limite de intentos; 403 = la IP ya quedo bloqueada en la base (version de produccion con ip_blocks)
    assert codigos[-1] in (403, 429) or 429 in codigos


def test_cross_site_post_is_refused(env):
    c, _, _ = env
    r = c.post("/login", data={"usuario": "x", "password": "y"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_machine_token_reaches_only_detection_and_camera_routes(env):
    c, _, _ = env
    bearer = {"authorization": f"Bearer {MACHINE}"}
    assert c.get("/api/detections/cam1", headers=bearer).status_code == 200
    assert c.get("/api/zonas", headers=bearer).status_code == 401, "la máquina no puede usar otras rutas"
    assert c.get("/api/detections/cam1", headers={"authorization": "Bearer otro"}).status_code == 401


def test_disabled_account_cannot_log_in(env):
    c, store, _ = env
    store.set_active("Diego.Reyes", False)
    r = c.post("/login", data={"usuario": "Diego.Reyes", "password": "clave-larga-123"},
               headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 401


def test_without_secret_everything_is_closed(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setenv("THOR_SESSION_SECRET", "")
    assert c.get("/login").status_code == 503
    assert c.get("/", headers=BROWSER).status_code == 303
