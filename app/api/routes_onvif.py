"""Operador ONVIF: gestion de los endpoints (camaras y videoportero), sondeo, eventos y pagina /onvif."""
import re

from fastapi import APIRouter, Body, HTTPException, Request

from app.onvif import client as oc
from app.onvif import registry

router = APIRouter()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,120}$")


def _mgr(request: Request):
    m = getattr(request.app.state, "onvif", None)
    if m is None:
        raise HTTPException(503, "operador ONVIF apagado")
    return m


def _view(request: Request, ep: dict) -> dict:
    m = _mgr(request)
    pr = m.last_probe(ep["id"])
    return {"id": ep["id"], "name": ep["name"], "kind": ep["kind"], "host": ep["host"], "port": ep.get("port", 80),
            "listen": bool(ep.get("listen")), "zone": ep.get("zone"), "has_user": registry.has_auth(ep["id"]),
            "listener": m.status(ep["id"]), "probe": pr}


@router.get("/api/onvif/endpoints")
def endpoints(request: Request):
    return {"endpoints": [_view(request, e) for e in registry.endpoints(request.app.state.config)]}


@router.post("/api/onvif/endpoints")
def add_endpoint(request: Request, payload: dict = Body(...)):
    ep_id, name, host = str(payload.get("id", "")), str(payload.get("name", ""))[:60], str(payload.get("host", ""))
    if not _ID.match(ep_id) or not _HOST.match(host) or not name:
        raise HTTPException(400, "id (letras, numeros, guion), nombre y host son obligatorios")
    try:
        registry.add_endpoint({"id": ep_id, "name": name, "kind": str(payload.get("kind", "device"))[:20], "host": host,
                               "port": int(payload.get("port", 80)), "listen": bool(payload.get("listen", False))})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@router.delete("/api/onvif/endpoints/{ep_id}")
def delete_endpoint(ep_id: str, request: Request):
    if any(e["id"] == ep_id for e in registry.camera_endpoints(request.app.state.config)):
        raise HTTPException(400, "las camaras vienen de la configuracion; solo se quitan los equipos agregados")
    if not registry.remove_endpoint(ep_id):
        raise HTTPException(404, "no existe")
    return {"ok": True}


@router.put("/api/onvif/endpoints/{ep_id}/auth")
def set_auth(ep_id: str, request: Request, payload: dict = Body(...)):
    ep = registry.get(request.app.state.config, ep_id)
    if ep is None:
        raise HTTPException(404, "no existe")
    user, pw = str(payload.get("user", "")).strip(), str(payload.get("password", ""))
    if not user or not pw:
        raise HTTPException(400, "usuario y clave son obligatorios")
    try:                                            # solo se guarda si el equipo lo acepta
        off = oc.get_datetime(ep["host"], int(ep.get("port", 80)))["skew_s"]
        info = oc.get_device_info(ep["host"], int(ep.get("port", 80)), user, pw, off)
    except oc.OnvifError as exc:
        raise HTTPException(400, f"el equipo no lo acepta: {exc}") from exc
    registry.set_auth(ep_id, user, pw)
    return {"ok": True, "device": info}


@router.delete("/api/onvif/endpoints/{ep_id}/auth")
def clear_auth(ep_id: str, request: Request):
    registry.clear_auth(ep_id)
    return {"ok": True}


@router.put("/api/onvif/endpoints/{ep_id}/listen")
def set_listen(ep_id: str, request: Request, payload: dict = Body(...)):
    if not registry.set_listen(ep_id, bool(payload.get("listen"))):
        raise HTTPException(404, "solo los equipos agregados escuchan eventos de forma continua")
    return {"ok": True}


@router.post("/api/onvif/endpoints/{ep_id}/probe")
def probe(ep_id: str, request: Request):
    try:
        return _mgr(request).probe(ep_id)
    except KeyError:
        raise HTTPException(404, "no existe") from None


@router.get("/api/onvif/endpoints/{ep_id}/live")
def live(ep_id: str, request: Request, seconds: int = 10):
    if not registry.has_auth(ep_id):
        raise HTTPException(400, "sin usuario ONVIF")
    try:
        return {"events": _mgr(request).live_events(ep_id, seconds)}
    except (oc.OnvifError, KeyError) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/api/onvif/events")
def events(request: Request, endpoint: str = "", limit: int = 100, since: float = 0.0):
    return {"events": _mgr(request).recent_events(endpoint or None, limit, since)}
