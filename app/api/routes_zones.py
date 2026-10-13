"""Zonas por camara: leer, guardar y la pagina /zonas donde se dibujan."""
from fastapi import APIRouter, Body, HTTPException, Request

from app.vision import zones

router = APIRouter()


def _cam_ids(request: Request) -> set:
    return {c.id for c in request.app.state.config.cameras}


@router.get("/api/zones")
def list_zones(request: Request):
    names = {c.id: c.name for c in request.app.state.config.cameras}
    return {"types": list(zones.TYPES), "cameras": [{"id": i, "name": n, "zones": zones.get(i)} for i, n in names.items()]}


@router.get("/api/zones/{cam_id}")
def get_zones(cam_id: str, request: Request):
    if cam_id not in _cam_ids(request):
        raise HTTPException(404, "camara inexistente")
    return {"cam_id": cam_id, "zones": zones.get(cam_id)}


@router.put("/api/zones/{cam_id}")
def put_zones(cam_id: str, request: Request, payload: dict = Body(...)):
    if cam_id not in _cam_ids(request):
        raise HTTPException(404, "camara inexistente")
    try:
        saved = zones.save(cam_id, payload.get("zones", []))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "cam_id": cam_id, "zones": saved}
