"""PAT Control Center: acceso con cuentas y cookies propias, token de Home Assistant que nunca sale del servidor, acciones permitidas,
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
def patcc(tmp_path, monkeypatch):
    monkeypatch.setenv("THOR_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("THOR_SESSION_SECRET", SECRET)
    monkeypatch.setenv("THOR_MACHINE_TOKEN", MACHINE)
    monkeypatch.setenv("THOR_MACHINE_EXTRA", "/api/resumen")
    monkeypatch.setenv("THOR_COOKIE_PREFIX", "patcc")
    monkeypatch.setenv("PATCC_AUTH", "on")
    monkeypatch.setenv("THOR_APP_NAME", "PAT Control Center")
    monkeypatch.setenv("PATCC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PATCC_NO_THREADS", "1")
    _reload_auth()
    for mod in ("patcc.files", "patcc.ha", "patcc.tuya", "patcc.automation", "patcc.main"):
        importlib.reload(importlib.import_module(mod))
    from app.auth import guard, passwords, sessions, store
    from patcc import main

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


def test_sin_sesion_no_entra_y_la_cookie_es_propia(patcc):
    main, client, guard = patcc
    assert guard.COOKIE_FULL == "patcc_sesion"                 # no pisa la sesión de thor-vision en el mismo host
    c = client()
    assert c.get("/api/estado").status_code == 401
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code in (302, 303) and "/login" in r.headers["location"]
    assert "PAT Control Center" in c.get("/login").text


def test_estado_con_sesion(patcc):
    main, client, _ = patcc
    j = client("Beto.Op").get("/api/estado").json()
    assert j["user"] == {"name": "Beto.Op", "role": "operador"}
    assert j["ha"]["entities"][0]["id"] == "light.p1" and j["tuya"]["devices"][0]["name"] == "Cocina"


def test_resumen_con_token_de_maquina(patcc):
    main, client, _ = patcc
    c = client()
    assert c.get("/api/resumen").status_code == 401
    j = c.get("/api/resumen", headers={"Authorization": f"Bearer {MACHINE}"}).json()
    assert j["lights"] == 3 and j["lights_on"] == 2           # bomba (HA) + cocina (Tuya, un botón prendido)
    assert c.get("/api/estado", headers={"Authorization": f"Bearer {MACHINE}"}).status_code == 401   # la máquina solo ve el resumen


def test_token_de_ha_solo_admin_y_nunca_vuelve(patcc, monkeypatch):
    main, client, _ = patcc
    from patcc import files
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


def test_ha_solo_acciones_de_control(patcc, monkeypatch):
    from patcc.ha import HA
    h = HA()
    monkeypatch.setattr(h, "states", lambda max_age=2.0: [{"entity_id": "light.p1"}, {"entity_id": "button.oxmtech_drive_reboot"},
                                                          {"entity_id": "media_player.sala"}, {"entity_id": "tts.google"}])
    for dom, svc, ent in (("button", "press", "button.oxmtech_drive_reboot"), ("homeassistant", "restart", "light.p1"),
                          ("light", "turn_on", "light.nope"), ("light", "turn_on", "media_player.sala"), ("tts", "speak", "tts.google")):
        with pytest.raises(ValueError):
            h.call(dom, svc, {"entity_id": ent})


def test_escena_y_horario(patcc):
    from patcc import automation as A
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


def test_horario_corre_una_sola_vez(patcc, monkeypatch):
    from patcc import automation as A

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
    monkeypatch.setenv("PATCC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PATCC_NO_THREADS", "1")
    monkeypatch.delenv("PATCC_AUTH", raising=False)
    for mod in ("patcc.files", "patcc.ha", "patcc.tuya", "patcc.automation", "patcc.main"):
        importlib.reload(importlib.import_module(mod))
    from patcc import files, main
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
    assert "t" * 40 not in c.get("/api/ajustes").text and "claves" not in c.get("/api/ajustes").text          # solo hint y fechas, nunca el valor
    c.post("/api/escenas", json={"name": "x", "actions": [{"t": "espera", "s": 1}]}, headers=SAME)
    assert files.recent_log() == [] or files.recent_log()[0]["who"].startswith("red local")
    monkeypatch.undo()
    _reload_auth()


# ── registro de la nube, filtro por casa, consola ─────────────────────
CASA, OTRA = "153519480", "195128033"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PATCC_NO_THREADS", "1")
    for mod in ("patcc.files", "patcc.consola", "patcc.registro"):
        importlib.reload(importlib.import_module(mod))
    from patcc import registro
    r = registro.Registro()
    yield r, registro, monkeypatch
    monkeypatch.undo()


def _fake_cloud(registro, monkeypatch, devices, error=""):
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"ts": 1.0, "devices": devices, "error": error}
    monkeypatch.setattr(registro.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(registro.httpx, "post", lambda *a, **k: Resp())


def test_registro_marca_cupo_sin_cupo_y_retirado_y_detecta_cambios(reg):
    r, registro, mp = reg
    casa = [{"device_id": "aaa111", "name": "Cocina", "type": "interruptor"},
            {"device_id": "bbb222", "name": "Pasillo", "type": "interruptor"},
            {"device_id": "ccc333", "name": "Garage viejo", "type": "interruptor", "cloud": False, "retired": True}]
    nube = [{"id": "aaa111", "name": "Cocina", "owner_id": CASA, "online": True, "category": "kg"},
            {"id": "ddd444", "name": "Pasillo 2", "owner_id": CASA, "online": True, "category": "kg"},
            {"id": "eee555", "name": "Barra", "owner_id": OTRA, "online": True, "category": "kg"}]
    _fake_cloud(registro, mp, nube)
    assert r.sync(casa)["ok"]
    st = r.states()
    assert st["aaa111"]["nube"] == "cupo" and st["bbb222"]["nube"] == "sin_cupo" and st["ccc333"]["nube"] == "retirado"
    assert st["ddd444"]["nube"] == "fuera_de_lista" and st["eee555"]["nube"] == "otra_casa"       # de Patricio 10 pero sin agregar / de Coworking540
    tipos = {c["tipo"] for c in r.summary()["cambios"]}
    assert {"nuevo", "detectado", "otra_casa"} <= tipos
    # segunda sincronización: el equipo sale de la nube, otro se renombra y otro se desconecta
    nube2 = [{"id": "aaa111", "name": "Cocina 2", "owner_id": CASA, "online": False, "category": "kg"}]
    casa[0]["name"] = "Cocina 2"
    _fake_cloud(registro, mp, nube2)
    r.sync(casa)
    tipos2 = [c["tipo"] for c in r.summary()["cambios"]]
    assert "renombrado" in tipos2 and "sin_linea" in tipos2
    assert r.summary()["counts"]["otra_casa"] == 1


def test_registro_si_la_nube_falla_lo_dice_y_no_borra_nada(reg):
    r, registro, mp = reg
    _fake_cloud(registro, mp, [{"id": "aaa111", "name": "A", "owner_id": CASA, "online": True}])
    r.sync([{"device_id": "aaa111", "name": "A", "type": "interruptor"}])
    _fake_cloud(registro, mp, [], error="sign invalid")
    out = r.sync([{"device_id": "aaa111", "name": "A", "type": "interruptor"}])
    assert out["ok"] is False and "sign invalid" in out["detalle"]
    assert r.states()["aaa111"]["nube"] == "cupo"


def test_tuya_no_muestra_otra_casa_ni_ordena_a_equipos_sin_cupo(reg):
    r, registro, mp = reg
    importlib.reload(importlib.import_module("patcc.tuya"))
    from patcc import tuya as tmod
    t = tmod.Tuya()
    t.registro = r
    devs = [{"device_id": "aaa111", "name": "Cocina", "kind": "light", "type": "interruptor", "online": True},
            {"device_id": "bbb222", "name": "Pasillo", "kind": "light", "type": "interruptor", "online": True},
            {"device_id": "ccc333", "name": "Garage viejo", "kind": "light", "type": "interruptor", "cloud": False, "retired": True},
            {"device_id": "ggg777", "name": "Cortina", "kind": "curtain", "type": "cortina", "cloud": False},
            {"device_id": "fff666", "name": "Foco", "kind": "light", "type": "foco", "online": True},
            {"device_id": "eee555", "name": "Barra (otra casa)", "kind": "light", "type": "interruptor", "online": True}]
    mp.setattr(t, "devices", lambda max_age=4.0: devs)
    _fake_cloud(registro, mp, [{"id": "aaa111", "name": "Cocina", "owner_id": CASA, "online": True},
                               {"id": "eee555", "name": "Barra", "owner_id": OTRA, "online": True}])
    r.sync(t.view(all_=True))                                           # lo que se sincroniza es la vista real, con el indicador cloud
    assert r.states()["ccc333"]["nube"] == "retirado"
    nombres = {d["name"] for d in t.view()}
    assert nombres == {"Cocina", "Pasillo", "Foco", "Cortina"}          # el retirado y el de otra casa no aparecen; la cortina (sin permiso) sí, bloqueada
    llamadas = []
    mp.setattr(tmod.httpx, "post", lambda *a, **k: llamadas.append(a) or type("R", (), {"json": lambda s: {"ok": True, "result": "ok"}})())
    res = t.control("bbb222", "switch_1", True)                         # sin cupo: ni se intenta
    assert res["ok"] is False and "cupo" in res["result"] and not llamadas
    assert t.control("ccc333", "switch_1", True)["ok"] is False and not llamadas
    assert "cupo" in t.control("ggg777", "control", "open")["result"] and not llamadas     # cortina sin permiso en la nube: tampoco se intenta
    assert "no pertenece" in t.control("eee555", "switch_1", True)["result"] and not llamadas
    assert t.control("aaa111", "switch_1", True)["ok"] is True and len(llamadas) == 1
    assert t.control("fff666", "switch_led", True)["ok"] is True and len(llamadas) == 2   # los focos van por red local, siempre se intentan


def test_vigencia_del_token_se_lee_del_jwt_sin_devolverlo(reg):
    import base64, json, time as _t
    r, registro, mp = reg
    importlib.reload(importlib.import_module("patcc.ha"))
    from patcc import files
    from patcc.ha import HA
    claims = {"iat": int(_t.time()) - 86400 * 3, "exp": int(_t.time()) + 86400 * 400}
    tok = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"
    files.save("ha.json", {"url": "http://h:8123", "token": tok}, secret=True)
    ti = HA().token_info()
    assert ti["days_left"] in (399, 400) and ti["created"] and ti["expires"] and tok not in json.dumps(ti)


def test_consola_filtra_y_no_guarda_secretos_en_el_formato(reg):
    from patcc import consola
    consola.log("info", "ha", "light.turn_on ['light.p1'] → 200 90 ms")
    consola.log("error", "tuya", "Pasillo switch_1=True → rechazada")
    consola.log("debug", "ha", "GET /api/states → 200 20 ms")
    assert [e["src"] for e in consola.entries(level="warn")] == ["tuya"]
    assert len(consola.entries(src="ha")) == 2 and len(consola.entries(q="pasillo")) == 1
    last = consola.entries()[-1]["id"]
    consola.log("info", "sistema", "x")
    assert [e["msg"] for e in consola.entries(since=last)] == ["x"]
    import os
    assert os.path.exists(os.path.join(os.environ["PATCC_DATA"], "consola.log"))


def test_consola_y_registro_responden_en_la_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCC_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PATCC_NO_THREADS", "1")
    monkeypatch.delenv("PATCC_AUTH", raising=False)
    for mod in ("patcc.files", "patcc.consola", "patcc.ha", "patcc.registro", "patcc.tuya", "patcc.automation", "patcc.main"):
        importlib.reload(importlib.import_module(mod))
    from patcc import main
    monkeypatch.setattr(main.ha, "view", lambda: [])
    monkeypatch.setattr(main.tuya, "view", lambda all_=False: [])
    c = TestClient(main.app)
    assert c.get("/consola").status_code == 200 and "consola" in c.get("/consola").text
    assert c.get("/api/consola").json()["entries"] == [] or isinstance(c.get("/api/consola").json()["entries"], list)
    assert c.post("/api/consola/cmd", json={"cmd": "rm"}, headers=SAME).status_code == 400
    assert c.post("/api/consola/cmd", json={"cmd": "sync"}, headers={"sec-fetch-site": "cross-site", "origin": "http://x"}).status_code == 403
    j = c.get("/api/conexiones").json()
    assert {i["id"] for i in j["items"]} >= {"ha", "tuya", "vision", "mqtt", "nube"}
    assert c.get("/api/registro").json()["home"] == "153519480"
    monkeypatch.undo()
    _reload_auth()
