"""OXM Hogar: una sola página con luces, medios, sensores, escenas y horarios de toda la casa (Home Assistant + Tuya).

Arranque:  uvicorn hogar.main:app --host 0.0.0.0 --port 8095
Variables: HOGAR_DATA, THOR_AUTH_DB, THOR_SESSION_SECRET, THOR_COOKIE_PREFIX=hogar, THOR_APP_NAME="OXM Hogar",
           THOR_MACHINE_TOKEN + THOR_MACHINE_EXTRA=/api/resumen (para el reflejo en thor-vision), TUYA_SERVICE_URL, VISION_URL.
Acceso: por defecto SIN login (red local de confianza, HOGAR_AUTH=off); con HOGAR_AUTH=on vuelve el login de app/auth y los roles
(operador y admin controlan y crean escenas; solo admin cambia la conexión con Home Assistant).
"""
import os
import pathlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import sessions, store
from app.auth.guard import COOKIE_FULL, AuthGuard
from app.auth.panel import router as admin_auth_router
from app.auth.routes import _same_origin
from app.auth.routes import router as auth_router
from hogar import automation, files
from hogar.ha import HA, HAError
from hogar.tuya import Tuya

PAGE = pathlib.Path(__file__).parent / "static" / "index.html"
AUTH = os.environ.get("HOGAR_AUTH", "off").lower() in ("on", "1", "true", "si")

ha = HA()
tuya = Tuya()
auto = automation.Automations(ha, tuya)


@asynccontextmanager
async def _lifespan(_app):
    if os.environ.get("HOGAR_NO_THREADS") != "1":
        tuya.start()
        auto.start()
    yield


app = FastAPI(title="OXM Hogar", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


# ── sesión y permisos ──────────────────────────────────────────────
def _user(request: Request) -> dict | None:
    if not AUTH:        # red local de confianza: cualquiera que llegue es administrador; la bitácora anota desde qué dirección
        ip = request.client.host if request.client else "desconocida"
        return {"username": f"red local ({ip})", "role": "admin", "active": 1}
    v = sessions.verify(request.cookies.get(COOKIE_FULL))
    if not v or v[1] != sessions.FULL:
        return None
    u = store.get_user(v[0])
    return u if u and u["active"] else None


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def _who(request: Request, write: bool = False, admin: bool = False):
    """(usuario, None) o (None, respuesta de error). Escribir exige mismo origen (evita que otra página ordene por ti)."""
    u = _user(request)
    if u is None:
        return None, _err(401, "Inicie sesión")
    if write and not _same_origin(request):
        return None, _err(403, "Origen no permitido")
    if admin and u.get("role") != "admin":
        return None, _err(403, "Solo para administradores")
    return u, None


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        return {}
    return b if isinstance(b, dict) else {}


# ── página ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


# ── estado ─────────────────────────────────────────────────────────
@app.get("/api/estado")
def estado(request: Request):
    u, e = _who(request)
    if e:
        return e
    out = {"ts": time.time(), "auth": AUTH, "user": {"name": u["username"], "role": u.get("role")}}
    try:
        out["ha"] = {"ok": True, "entities": ha.view()}
    except HAError as exc:
        out["ha"] = {"ok": False, "error": str(exc), "entities": [], "configured": ha.configured}
    devs = tuya.view()
    out["tuya"] = {"ok": not tuya.last_error, "error": tuya.last_error, "devices": devs, **tuya.bulbs()}
    return out


@app.get("/api/resumen")
def resumen():
    """Reflejo ligero para thor-vision (token de máquina o sesión): cuántas luces hay prendidas y qué sigue."""
    lights_on = lights = 0
    ha_ok = True
    try:
        for x in ha.view():
            if x["domain"] in ("light", "switch"):
                lights += 1
                lights_on += x["state"] == "on"
    except HAError:
        ha_ok = False
    offline = 0
    for d in tuya.view():
        if d.get("online") is False:
            offline += 1
        if d.get("kind") == "light" and isinstance(d.get("switches"), dict):
            lights += 1
            lights_on += d.get("online") is not False and any(d["switches"].values())
    return {"lights_on": lights_on, "lights": lights, "offline": offline, "ha_ok": ha_ok, "tuya_ok": not tuya.last_error,
            "scenes": len(auto.scenes()), "next": auto.next_run()}


# ── control ────────────────────────────────────────────────────────
@app.post("/api/ha/accion")
async def ha_accion(request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    b = await _json(request)
    data = dict(b.get("data") or {})
    data["entity_id"] = b.get("entity_id")
    try:
        ha.call(str(b.get("domain") or ""), str(b.get("service") or ""), data)
    except ValueError as exc:
        return _err(400, str(exc))
    except HAError as exc:
        return _err(502, str(exc))
    files.log(u["username"], f'{b.get("domain")}.{b.get("service")} {b.get("entity_id")}', "ok")
    return {"ok": True}


@app.post("/api/tuya/control")
async def tuya_control(request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    b = await _json(request)
    try:
        r = tuya.control(str(b.get("device_id") or ""), str(b.get("code") or ""), b.get("value"))
    except ValueError as exc:
        return _err(400, str(exc))
    files.log(u["username"], f'tuya {b.get("device_id")} {b.get("code")}={b.get("value")}', str(r.get("result"))[:120])
    return r


@app.post("/api/tuya/focos")
async def tuya_focos(request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    b = await _json(request)
    try:
        r = tuya.group(b.get("ids", "all"), b.get("action") or {})
    except ValueError as exc:
        return _err(400, str(exc))
    files.log(u["username"], "focos " + str(b.get("action")), str(r.get("result"))[:120])
    return r


# ── escenas y horarios ─────────────────────────────────────────────
@app.get("/api/escenas")
def escenas(request: Request):
    u, e = _who(request)
    return e or {"scenes": auto.scenes(), "schedules": auto.schedules(), "next": auto.next_run()}


@app.post("/api/escenas")
async def escena_guardar(request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    try:
        s = auto.save_scene(await _json(request))
    except (ValueError, TypeError) as exc:
        return _err(400, str(exc))
    files.log(u["username"], f"guardó la escena «{s['name']}»", "ok")
    return {"ok": True, "scene": s}


@app.delete("/api/escenas/{sid}")
def escena_borrar(sid: str, request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    return {"ok": auto.delete_scene(sid)}


@app.post("/api/escenas/{sid}/ejecutar")
def escena_ejecutar(sid: str, request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    try:
        res = auto.run_scene(sid, u["username"])
    except KeyError:
        return _err(404, "la escena no existe")
    return {"ok": any(r["ok"] for r in res) if res else True, "results": res}


@app.post("/api/horarios")
async def horario_guardar(request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    try:
        h = auto.save_schedule(await _json(request))
    except (ValueError, TypeError) as exc:
        return _err(400, str(exc))
    files.log(u["username"], f"guardó el horario «{h['name']}» ({h['when']})", "ok")
    return {"ok": True, "schedule": h}


@app.delete("/api/horarios/{hid}")
def horario_borrar(hid: str, request: Request):
    u, e = _who(request, write=True)
    if e:
        return e
    return {"ok": auto.delete_schedule(hid)}


# ── ajustes ────────────────────────────────────────────────────────
@app.get("/api/ajustes")
def ajustes(request: Request):
    u, e = _who(request)
    if e:
        return e
    info = ha.info()
    if u.get("role") != "admin":
        info = {"configured": info["configured"], "url": info["url"]}
    from hogar import tuya as tmod
    return {"ha": info, "tuya_service": tmod.SVC, "vision": tmod.VISION, "role": u.get("role"), "auth": AUTH}


@app.post("/api/ajustes/ha")
async def ajustes_ha(request: Request):
    u, e = _who(request, write=True, admin=True)
    if e:
        return e
    b = await _json(request)
    try:
        r = ha.save(str(b.get("url") or ""), str(b.get("token") or ""), u["username"])
    except ValueError as exc:
        return _err(400, str(exc))
    except HAError as exc:
        return _err(502, f"no se guardó: {exc}")
    files.log(u["username"], "conectó Home Assistant", f"{r.get('location')} {r.get('version')}")
    return {"ok": True, **r}


@app.delete("/api/ajustes/ha")
def ajustes_ha_borrar(request: Request):
    u, e = _who(request, write=True, admin=True)
    if e:
        return e
    ha.forget()
    files.log(u["username"], "borró el token de Home Assistant", "ok")
    return {"ok": True}


@app.get("/api/bitacora")
def bitacora(request: Request):
    u, e = _who(request)
    return e or {"items": files.recent_log(150)}


# Acceso (solo con HOGAR_AUTH=on): mismas pantallas y reglas que thor-vision, con cuentas y cookies propias. Al final = capa externa.
if AUTH:
    app.include_router(auth_router)
    app.include_router(admin_auth_router)
    app.add_middleware(AuthGuard)
