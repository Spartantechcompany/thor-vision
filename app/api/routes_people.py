import logging
from typing import Optional

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_JPEG = {"Cache-Control": "public, max-age=3600"}


def _mgr(request: Request):
    return getattr(request.app.state, "visits", None)


@router.get("/api/subjects")
def list_subjects(request: Request, since: Optional[float] = None, limit: int = 100):
    return {"subjects": request.app.state.db.list_subjects(since=since, limit=limit)}


@router.get("/api/subjects/stats")
def subjects_stats(request: Request):
    m = _mgr(request)
    return m.snapshot() if m else {"enabled": False}


@router.patch("/api/subjects/{sid}")
def rename_subject(sid: int, request: Request, payload: dict = Body(...)):
    name = " ".join(str(payload.get("name") or "").split())[:60]
    m = _mgr(request)
    if not name or m is None or not m.rename_subject(sid, name):
        return JSONResponse(status_code=400, content={"error": "nombre invalido o sujeto inexistente"})
    return {"ok": True, "name": name}


@router.post("/api/subjects/{sid}/merge")
def merge_subject(sid: int, request: Request, payload: dict = Body(...)):
    m = _mgr(request)
    try:
        dst = int(payload.get("into"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "into requerido"})
    if m is None or not m.merge_subjects(sid, dst):
        return JSONResponse(status_code=400, content={"error": "no se pudo fusionar"})
    return {"ok": True, "into": dst}


@router.delete("/api/subjects/{sid}")
def delete_subject(sid: int, request: Request):
    m = _mgr(request)
    if m is None or not m.delete_subject(sid):
        return JSONResponse(status_code=404, content={"error": "sujeto inexistente"})
    return {"ok": True}


@router.get("/api/subjects/{sid}/face")
def subject_face(sid: int, request: Request):
    b = request.app.state.db.get_subject_blob(sid, "face")
    return Response(content=b, media_type="image/jpeg", headers=_JPEG) if b else \
        JSONResponse(status_code=404, content={"error": "sin imagen"})


@router.get("/api/subjects/{sid}/body")
def subject_body(sid: int, request: Request):
    b = request.app.state.db.get_subject_blob(sid, "body")
    return Response(content=b, media_type="image/jpeg", headers=_JPEG) if b else \
        JSONResponse(status_code=404, content={"error": "sin imagen"})


@router.get("/api/person-visits")
def list_visits(request: Request, cam_id: Optional[str] = None,
                subject_id: Optional[int] = None, since: Optional[float] = None,
                until: Optional[float] = None, limit: int = 100,
                include_hidden: bool = False, unassigned: bool = False):
    return {"visits": request.app.state.db.query_person_visits(
        cam_id=cam_id, subject_id=subject_id, since=since, until=until, limit=limit,
        include_hidden=include_hidden, unassigned=unassigned)}


@router.get("/api/person-visits/{vid}/face")
def visit_face(vid: int, request: Request):
    b = request.app.state.db.get_visit_blob(vid, "face")
    return Response(content=b, media_type="image/jpeg", headers=_JPEG) if b else \
        JSONResponse(status_code=404, content={"error": "sin imagen"})


@router.get("/api/person-visits/{vid}/body")
def visit_body(vid: int, request: Request):
    b = request.app.state.db.get_visit_blob(vid, "body")
    return Response(content=b, media_type="image/jpeg", headers=_JPEG) if b else \
        JSONResponse(status_code=404, content={"error": "sin imagen"})
