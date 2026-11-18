"""OXM Hogar: acceso con cuentas y cookies propias, token de Home Assistant que nunca sale del servidor, acciones permitidas,
escenas y horarios (hora fija y atardecer)."""
import importlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
MACHINE = "q" * 40
AUTH_MODS = ("app.auth.store", "app.auth.sessions", "app.auth.limiter", "app.auth.guard", "app.auth.routes", "app.auth.panel")
SAME = {"sec-fetch-site": "same-origin"}


def _reload_auth():
    for mod in AUTH_MODS:
        importlib.reload(importlib.import_module(mod))


@pytest.fixture
def hogar(tmp_path, monkeypatch):
    monkeypatch.setenv("THOR_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("THOR_SESSION_SECRET", SECRET)
    monkeypatch.setenv("THOR_MACHINE_TOKEN", MACHINE)
    monkeypatch.setenv("THOR_MACHINE_EXTRA", "/api/resumen")
    monkeypatch.setenv("THOR_COOKIE_PREFIX", "hogar")
    monkeypatch.setenv("HOGAR_AUTH", "on")
    monkeypatch.setenv("THOR_APP_NAME", "OXM Hogar")
    monkeypatch.setenv("HOGAR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HOGAR_NO_THREADS", "1")
    _reload_auth()
    for mod in ("hogar.files", "hogar.ha", "hogar.tuya", "hogar.automation", "hogar.main"):
        importlib.reload(importlib.import_module(mod))
    from app.auth import guard, passwords, sessions, store
    from hogar import main

    store.create_user("Ana.Admin", passwords.hash_password("clave-larga-123"), "admin")
    store.create_user("Beto.Op", passwords.hash_password("clave-larga-456"), "operador")
    ents = [{"id": "light.p1", "domain": "light", "name": "P1", "state": "off"},
            {"id": "switch.bomba", "domain": "switch", "name": "Bomba", "state": "on"}]
    monkeypatch.setattr(main.ha, "view", lambda: ents)
    monkeypatch.setattr(main.tuya, "view", lambda: [{"device_id": "abc123", "name": "Cocina", "kind": "light", "type": "interruptor",
                                                      "online": True, "switches": {"switch_1": True, "switch_2": False}}])

    def client(user=None):
        c = TestClient(main.app)
        if user:
            c.cookies.set(guard.COOKIE_FULL, sessions.sign(user, sessions.FULL, sessions.FULL_TTL_S))
        return c

    yield main, client, guard
    monkeypatch.undo()
    _reload_auth()


def test_sin_sesion_no_entra_y_la_cookie_es_propia(hogar):
    main, client, guard = hogar
    assert guard.COOKIE_FULL == "hogar_sesion"                 # no pisa la sesión de thor-vision en el mismo host
    c = client()
    assert c.get("/api/estado").status_code == 401
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code in (302, 303) and "/login" in r.headers["location"]
    assert "OXM Hogar" in c.get("/login").text


def test_estado_con_sesion(hogar):
    main, client, _ = hogar
    j = client("Beto.Op").get("/api/estado").json()
    assert j["user"] == {"name": "Beto.Op", "role": "operador"}
    assert j["ha"]["entities"][0]["id"] == "light.p1" and j["tuya"]["devices"][0]["name"] == "Cocina"


def test_resumen_con_token_de_maquina(hogar):
    main, client, _ = hogar
    c = client()
    assert c.get("/api/resumen").status_code == 401
    j = c.get("/api/resumen", headers={"Authorization": f"Bearer {MACHINE}"}).json()
    assert j["lights"] == 3 and j["lights_on"] == 2           # bomba (HA) + cocina (Tuya, un botón prendido)
    assert c.get("/api/estado", headers={"Authorization": f"Bearer {MACHINE}"}).status_code == 401   # la máquina solo ve el resumen


def test_token_de_ha_solo_admin_y_nunca_vuelve(hogar, monkeypatch):
    main, client, _ = hogar
    from hogar import files
    monkeypatch.setattr(main.ha, "_get", staticmethod(lambda url, tok, p: {"version": "2026.11.0", "location_name": "Casa"}))
    op = client("Beto.Op")
    assert op.post("/api/ajustes/ha", json={"url": "http://192.168.0.110:8123", "token": "x" * 40}, headers=SAME).status_code == 403
    ad = client("Ana.Admin")
    assert ad.post("/api/ajustes/ha", json={"url": "http://192.168.0.110:8123", "token": "x" * 40},
                   headers={"sec-fetch-site": "cross-site", "origin": "http://otra.pagina"}).status_code == 403   # otra página no ordena
    r = ad.post("/api/ajustes/ha", json={"url": "http://192.168.0.110:8123", "token": "secreto-" + "x" * 40}, headers=SAME)
    assert r.status_code == 200 and r.json()["location"] == "Casa"
    import os
    assert oct(os.stat(files.path("ha.json")).st_mode)[-3:] == "600"
    for c in (ad, op):
        txt = c.get("/api/ajustes").text
        assert "secreto-" not in txt
    assert ad.get("/api/ajustes").json()["ha"]["token_hint"] == "…xxxx"
    assert "token_hint" not in op.get("/api/ajustes").json()["ha"]
    assert ad.post("/api/ajustes/ha", json={"url": "ftp://x", "token": "y" * 40}, headers=SAME).status_code == 400


def test_ha_solo_acciones_de_control(hogar, monkeypatch):
    from hogar.ha import HA
    h = HA()
    monkeypatch.setattr(h, "states", lambda max_age=2.0: [{"entity_id": "light.p1"}, {"entity_id": "button.oxmtech_drive_reboot"},
                                                          {"entity_id": "media_player.sala"}, {"entity_id": "tts.google"}])
    for dom, svc, ent in (("button", "press", "button.oxmtech_drive_reboot"), ("homeassistant", "restart", "light.p1"),
                          ("light", "turn_on", "light.nope"), ("light", "turn_on", "media_player.sala"), ("tts", "speak", "tts.google")):
        with pytest.raises(ValueError):
            h.call(dom, svc, {"entity_id": ent})


def test_escena_y_horario(hogar):
    from hogar import automation as A
    with pytest.raises(ValueError):
        A.validate_scene({"name": "x", "actions": [{"t": "rm -rf"}]})
    s = A.validate_scene({"name": "Noche", "actions": [{"t": "focos", "ids": "all", "action": {"on": True, "brightness": 30}},
                                                       {"t": "espera", "s": 2}]})
    with pytest.raises(ValueError):
        A.validate_schedule({"when": "25:00", "scene_id": s["id"]}, {s["id"]})
    with pytest.raises(ValueError):
        A.validate_schedule({"when": "19:00", "scene_id": "otra"}, {s["id"]})
    h = A.validate_schedule({"when": "atardecer+15", "days": [0, 1, 2, 3, 4], "scene_id": s["id"]}, {s["id"]})
    sun = {"next_setting": "2026-11-18T00:05:00+00:00"}           # 18:05 hora de Ciudad de México
    now = datetime(2026, 11, 17, 18, 21)                          # martes
    t = A.target_today(h["when"], now, sun)
    ev = datetime.fromisoformat(sun["next_setting"]).astimezone().replace(tzinfo=None)
    assert t.date() == now.date() and (t.hour * 60 + t.minute) == (ev.hour * 60 + ev.minute + 15) % 1440
    assert A.target_today("07:30", now, None) == datetime(2026, 11, 17, 7, 30)
    assert A.target_today("amanecer", now, {}) is None            # sin datos del sol no se adivina


def test_horario_corre_una_sola_vez(hogar, monkeypatch):
    from hogar import automation as A

    class FakeTuya:
        calls = []

        def group(self, ids, action):
            self.calls.append(action)
            return {"ok": True, "result": "1 de 1"}

    tu = FakeTuya()
    au = A.Automations(ha=None, tuya=tu)
    monkeypatch.setattr(au, "_sun", lambda: None)
    s = au.save_scene({"name": "Encender", "actions": [{"t": "focos", "ids": "all", "action": {"on": True}}]})
    au.save_schedule({"name": "Mañana", "when": "07:30", "days": list(range(7)), "scene_id": s["id"]})
    assert au.tick(datetime(2026, 11, 17, 7, 29)) == []
    assert len(au.tick(datetime(2026, 11, 17, 7, 30, 20))) == 1
    assert au.tick(datetime(2026, 11, 17, 7, 31)) == []           # ya corrió hoy
    assert au.tick(datetime(2026, 11, 17, 7, 40)) == []           # fuera de la ventana
    assert len(tu.calls) == 1
    assert au.next_run(datetime(2026, 11, 17, 8, 0))["name"] == "Mañana"
    assert au.delete_scene(s["id"]) and au.schedules() == []      # borrar la escena quita sus horarios


def test_sin_login_por_defecto_entra_cualquiera_de_la_red(tmp_path, monkeypatch):
    monkeypatch.setenv("HOGAR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HOGAR_NO_THREADS", "1")
    monkeypatch.delenv("HOGAR_AUTH", raising=False)
    for mod in ("hogar.files", "hogar.ha", "hogar.tuya", "hogar.automation", "hogar.main"):
        importlib.reload(importlib.import_module(mod))
    from hogar import files, main
    monkeypatch.setattr(main.ha, "view", lambda: [])
    monkeypatch.setattr(main.tuya, "view", lambda: [])
    c = TestClient(main.app)
    j = c.get("/api/estado").json()
    assert j["auth"] is False and j["user"]["role"] == "admin" and j["user"]["name"].startswith("red local")
    assert c.get("/", headers={"accept": "text/html"}).status_code == 200            # sin redirección a /login
    assert c.get("/api/resumen").status_code == 200
    assert c.get("/login").status_code == 404
    # sigue exigiendo mismo origen para ordenar y sigue sin devolver el token
    assert c.post("/api/ajustes/ha", json={"url": "http://x:1", "token": "t" * 40},
                  headers={"sec-fetch-site": "cross-site", "origin": "http://otra.pagina"}).status_code == 403
    assert "token" not in c.get("/api/ajustes").text.replace("token_hint", "")
    c.post("/api/escenas", json={"name": "x", "actions": [{"t": "espera", "s": 1}]}, headers=SAME)
    assert files.recent_log() == [] or files.recent_log()[0]["who"].startswith("red local")
    monkeypatch.undo()
    _reload_auth()
