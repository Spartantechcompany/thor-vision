"""Gestion de dispositivos por marca (Hanwha por SUNAPI, Dahua/ONVIF por ONVIF) y pagina /dispositivos."""
from fastapi import APIRouter, Body, HTTPException, Request

from app.devices import onvif_ops
from app.devices import sunapi as sn
from app.devices.manager import PolicyError
from app.onvif import client as oc
from app.onvif import registry

router = APIRouter()


def _m(request: Request):
    m = getattr(request.app.state, "devices", None)
    if m is None:
        raise HTTPException(503, "gestion de dispositivos apagada")
    return m


def _actor(request: Request) -> str:
    return request.client.host if request.client else "-"


def _known(request: Request, dev_id: str):
    if registry.get(request.app.state.config, dev_id) is None:
        raise HTTPException(404, "equipo inexistente")


def _wrap(fn):
    try:
        return fn()
    except PolicyError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (sn.SunapiError, oc.OnvifError) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/api/devices")
def devices(request: Request):
    return {"devices": _m(request).devices()}


@router.get("/api/devices/audit")
def audit(request: Request, device: str = "", limit: int = 100):
    return {"actions": _m(request).audit_list(device, limit)}


@router.get("/api/devices/{dev_id}")
def device(dev_id: str, request: Request, refresh: int = 0):
    _known(request, dev_id)
    m = _m(request)
    s = {k: v for k, v in m.summary(dev_id, bool(refresh)).items() if not k.startswith("_")}
    return {"id": dev_id, "brand": m.brand(dev_id), "manage": registry.is_manage(dev_id), "summary": s,
            "onvif_ops": {"reads": list(onvif_ops.READS), "actions": {k: v[1] for k, v in onvif_ops.ACTIONS.items()}}
            if m.brand(dev_id) != "hanwha" else None}


@router.put("/api/devices/{dev_id}/manage")
def set_manage(dev_id: str, request: Request, payload: dict = Body(...)):
    _known(request, dev_id)
    registry.set_manage(dev_id, bool(payload.get("enabled")))
    _m(request).audit(dev_id, "config", "modo_gestion", {"enabled": bool(payload.get("enabled"))}, True, "ok", _actor(request))
    return {"ok": True, "manage": registry.is_manage(dev_id)}


# ---- Hanwha / SUNAPI
@router.get("/api/devices/{dev_id}/sunapi/tree")
def sunapi_tree(dev_id: str, request: Request):
    _known(request, dev_id)
    return _wrap(lambda: {"tree": _m(request).sunapi_tree(dev_id)})


@router.get("/api/devices/{dev_id}/sunapi/spec")
def sunapi_spec(dev_id: str, request: Request, cgi: str, submenu: str, action: str):
    _known(request, dev_id)
    return _wrap(lambda: _m(request).sunapi_spec(dev_id, cgi, submenu, action))


@router.get("/api/devices/{dev_id}/sunapi/view")
def sunapi_view(dev_id: str, request: Request, cgi: str, submenu: str, channel: str = ""):
    _known(request, dev_id)
    return _wrap(lambda: _m(request).sunapi_view(dev_id, cgi, submenu, {"Channel": channel} if channel != "" else None))


@router.post("/api/devices/{dev_id}/sunapi/action")
def sunapi_action(dev_id: str, request: Request, payload: dict = Body(...)):
    _known(request, dev_id)
    return _wrap(lambda: _m(request).sunapi_act(dev_id, str(payload.get("cgi", "")), str(payload.get("submenu", "")), str(payload.get("action", "")),
                                                dict(payload.get("params") or {}), str(payload.get("confirm", "")), _actor(request)))


# ---- ONVIF (Dahua y otros)
@router.get("/api/devices/{dev_id}/onvif/{op}")
def onvif_read(dev_id: str, op: str, request: Request):
    _known(request, dev_id)
    return _wrap(lambda: _m(request).onvif_read(dev_id, op))


@router.post("/api/devices/{dev_id}/onvif-action")
def onvif_action(dev_id: str, request: Request, payload: dict = Body(...)):
    _known(request, dev_id)
    return _wrap(lambda: _m(request).onvif_act(dev_id, str(payload.get("op", "")), dict(payload.get("params") or {}),
                                               str(payload.get("confirm", "")), _actor(request)))
