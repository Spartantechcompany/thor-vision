import logging
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, Form, Request, Response, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Detection endpoints ────────────────────────────────────────────────

@router.get("/api/detections")
def get_all_detections(request: Request):
    store = request.app.state.detection_store
    return {
        "total_persons": store.total_persons(),
        "cameras": store.as_api(),
    }


@router.get("/api/detections/{cam_id}")
def get_camera_detection(cam_id: str, request: Request):
    store = request.app.state.detection_store
    d = store.get(cam_id)
    if d is None:
        return {"person_count": 0, "faces": [], "updated_at": None}
    return {
        "person_count": d.person_count,
        "faces": [
            {"name": f.name, "confidence": round(f.confidence, 3), "thumb": f.thumb_b64}
            for f in d.faces
        ],
        "updated_at": d.updated_at,
        "inference_ms": round(d.inference_ms, 1),
        # cajas de persona de YOLO (rectangulos, sin imagen) para analisis de trayectorias
        "person_bboxes": [[int(v) for v in b] for b in (d.person_bboxes or [])],
        "yolo_persons": d.yolo_persons,
        "yolo_ts": d.yolo_ts,
        "frame_w": d.frame_w,
        "frame_h": d.frame_h,
    }


# ── Face management endpoints ──────────────────────────────────────────

@router.get("/api/faces")
def list_faces(request: Request):
    face_db = request.app.state.face_db
    return {"faces": face_db.list_faces(), "total": len(face_db)}


@router.post("/api/faces")
async def register_face(
    request: Request,
    name: str = Form(...),
    image: UploadFile = File(...),
):
    vision_models = request.app.state.vision_models
    face_db = request.app.state.face_db

    if vision_models._face_app is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Face recognition model not loaded"},
        )

    data = await image.read()
    arr = np.frombuffer(data, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse(status_code=400, content={"error": "Invalid image"})

    embedding, thumb_jpeg = vision_models.extract_embedding_and_thumb(frame)
    if embedding is None:
        return JSONResponse(status_code=422, content={"error": "No face detected in image"})

    face_id = face_db.add(name, embedding, thumb_jpeg=thumb_jpeg)
    logger.info("Registered face '%s' id=%s", name, face_id)
    return {"id": face_id, "name": name}


@router.delete("/api/faces/{face_id}")
def delete_face(face_id: str, request: Request):
    face_db = request.app.state.face_db
    if face_db.remove(face_id):
        return {"deleted": True, "id": face_id}
    return JSONResponse(status_code=404, content={"error": "Face not found"})


@router.get("/api/faces/{face_id}/image")
def face_image(face_id: str, request: Request):
    face_db = request.app.state.face_db
    path = face_db.thumb_path(face_id)
    if path is None:
        return JSONResponse(status_code=404, content={"error": "No thumbnail"})
    return FileResponse(path, media_type="image/jpeg",
                         headers={"Cache-Control": "public, max-age=86400"})


# ── Historial de avistamientos (log persistente, distinto de /api/detections
#    que es momentáneo — se pierde en cuanto la persona sale de cuadro) ─────

@router.get("/api/face-sightings")
def list_face_sightings(
    request: Request,
    cam_id: Optional[str] = None,
    name: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 100,
):
    db = request.app.state.db
    return {"sightings": db.query_face_sightings(
        cam_id=cam_id, name=name, since=since, until=until, limit=limit,
    )}


@router.get("/api/face-sightings/{sighting_id}/body")
def face_sighting_body(sighting_id: int, request: Request):
    body = request.app.state.db.get_face_sighting_body(sighting_id)
    if body is None:
        return JSONResponse(status_code=404, content={"error": "No body image"})
    return Response(content=body, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/face-sightings/{sighting_id}/image")
def face_sighting_image(sighting_id: int, request: Request):
    db = request.app.state.db
    thumb = db.get_face_sighting_thumb(sighting_id)
    if thumb is None:
        return JSONResponse(status_code=404, content={"error": "No thumbnail"})
    return Response(content=thumb, media_type="image/jpeg",
                     headers={"Cache-Control": "public, max-age=86400"})
