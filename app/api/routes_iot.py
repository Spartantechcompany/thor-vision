"""Estado y eventos de los dispositivos IoT (Tuya por MQTT, solo lectura)."""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _l(request: Request):
    m = getattr(request.app.state, "iot", None)
    if m is None:
        raise HTTPException(503, "integracion IoT apagada")
    return m


@router.get("/api/iot/devices")
def devices(request: Request):
    m = _l(request)
    return {"status": m.status, "devices": m.snapshot_state()}


@router.get("/api/iot/config")
def config(request: Request):
    """Direccion del servicio de video de Tuya (en oapc-devops) para que la pagina /iot lo consulte."""
    import os
    return {"tuya_service": os.environ.get("TUYA_SERVICE_URL", "http://192.168.0.194:8765")}


@router.get("/api/iot/events")
def events(request: Request, device_id: str = "", limit: int = 100, sync: bool = False):
    """Registro de eventos de los dispositivos Tuya. sync=true incluye las sincronizaciones de la nube y todos los codigos internos."""
    return {"events": _l(request).recent(device_id, limit, sync=sync)}


_PATCC = {"ts": 0.0, "data": None}


@router.get("/api/patcc/resumen")
def patcc_resumen():
    """Reflejo ligero de PAT Control Center (panel aparte de luces y dispositivos): se consulta con su token de máquina y se guarda 15 s."""
    import os
    import time

    import httpx
    url = os.environ.get("PATCC_URL", "http://127.0.0.1:8095").rstrip("/")
    public = os.environ.get("PATCC_PUBLIC_URL", "http://192.168.0.10:8095")
    tok = os.environ.get("PATCC_TOKEN", "")          # solo hace falta si PAT·CC corre con PATCC_AUTH=on
    if time.time() - _PATCC["ts"] < 15 and _PATCC["data"] is not None:
        return _PATCC["data"]
    try:
        r = httpx.get(url + "/api/resumen", headers={"Authorization": f"Bearer {tok}"} if tok else {}, timeout=4)
        r.raise_for_status()
        data = {"ok": True, "url": public, **r.json()}
    except (httpx.HTTPError, ValueError) as exc:
        data = {"ok": False, "url": public, "error": type(exc).__name__}
    _PATCC.update(ts=time.time(), data=data)
    return data
