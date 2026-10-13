"""
Endpoints para consultar la persistencia: eventos, snapshots, chat history.
"""
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()


# Payloads que escribe VLMAnalyzer en cada llamada al VLM.
_DEBUG_LATEST_DIR = Path("/app/data/debug/nemotron/latest")

# cam_id llega por URL y se usa para construir rutas de archivo: whitelist
# estricta, no basta con sanear separadores.
_CAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_cam_id(cam_id: str) -> str:
    if not _CAM_ID_RE.match(cam_id or ""):
        raise HTTPException(400, "cam_id inválido")
    return cam_id


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Storage not initialized")
    return db


def _snapshots(request: Request):
    sm = getattr(request.app.state, "snapshots", None)
    if sm is None:
        raise HTTPException(503, "Snapshot manager not initialized")
    return sm


# ── Events ───────────────────────────────────────────────────────────────

@router.get("/api/events")
def list_events(
    request: Request,
    cam_id:    Optional[str]   = None,
    type:      Optional[str]   = None,
    since:     Optional[float] = None,
    until:     Optional[float] = None,
    has_alert: Optional[int]   = None,    # 0 | 1
    limit:     int             = 200,
):
    db = _db(request)
    rows = db.query_events(
        cam_id    = cam_id,
        type      = type,
        since     = since,
        until     = until,
        has_alert = bool(has_alert) if has_alert is not None else None,
        limit     = limit,
    )
    return {"count": len(rows), "events": rows}


@router.patch("/api/events/{event_id}/review")
async def review_event(event_id: int, request: Request):
    """Triage manual de una alerta: {"label": "important"|"noise"|null}."""
    db = _db(request)
    body = await request.json()
    label = body.get("label")
    if label is not None and label not in ("important", "noise"):
        raise HTTPException(400, "label debe ser 'important', 'noise' o null")
    ok = db.set_event_review(event_id, label)
    if not ok:
        raise HTTPException(404, "Evento no encontrado")
    return {"id": event_id, "review_label": label}


# ── Snapshots ────────────────────────────────────────────────────────────

@router.get("/api/snapshots")
def list_snapshots(
    request: Request,
    cam_id:  Optional[str]   = None,
    since:   Optional[float] = None,
    until:   Optional[float] = None,
    trigger: Optional[str]   = None,
    limit:   int             = 100,
):
    db = _db(request)
    rows = db.query_snapshots(
        cam_id  = cam_id,
        since   = since,
        until   = until,
        trigger = trigger,
        limit   = limit,
    )
    return {"count": len(rows), "snapshots": rows}


@router.get("/api/snapshots/file/{snapshot_id}")
def serve_snapshot(snapshot_id: int, request: Request, w: int = 0):
    db = _db(request)
    sm = _snapshots(request)

    row = db.get_snapshot(snapshot_id)
    if row is None:
        raise HTTPException(404, "Snapshot not found in index")

    abs_path = sm.absolute_path(row["path"])
    if not abs_path.exists():
        raise HTTPException(404, f"Snapshot file missing: {row['path']}")

    try:
        data = abs_path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"Read error: {e}")

    if w:
        from app.utils.thumb import shrink
        data = shrink(data, w)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/api/snapshots/{cam_id}")
def trigger_snapshot(cam_id: str, request: Request):
    """Trigger manual de snapshot — guarda el frame actual."""
    manager = request.app.state.capture_manager
    sm      = _snapshots(request)

    frame = manager.get_frame(cam_id)
    if frame is None:
        raise HTTPException(404, f"No frame available for {cam_id}")

    rel_path = sm.save(cam_id, frame, trigger="api", event_id=None)
    if rel_path is None:
        raise HTTPException(500, "Snapshot save failed")

    abs_path = sm.absolute_path(rel_path)
    try:
        size = abs_path.stat().st_size
    except OSError:
        size = 0

    return {
        "cam_id":     cam_id,
        "path":       rel_path,
        "size_bytes": size,
    }


# ── Chat history ─────────────────────────────────────────────────────────

@router.get("/api/chat/history")
def chat_history(request: Request, session_id: str, limit: int = 50):
    db = _db(request)
    rows = db.query_chat_history(session_id=session_id, limit=limit)
    return {"session_id": session_id, "count": len(rows), "messages": rows}


# ── Dashboard visits (quien consulta el sitio) ──────────────────────────

@router.get("/api/visits")
def list_visits(
    request: Request,
    ip:    Optional[str]   = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int             = 200,
):
    db = _db(request)
    rows = db.query_dashboard_visits(ip=ip, since=since, until=until, limit=limit)
    return {"count": len(rows), "visits": rows}


# ── Debug: último payload del VLM por cámara ────────────────────────────

@router.get("/api/debug/payload/{cam_id}")
def debug_payload(cam_id: str, request: Request):
    """
    Último request/response del VLM para una cámara.

    Los archivos los escribe `VLMAnalyzer` en cada llamada (éxito o
    error), así que esto refleja exactamente lo último que se mandó y lo que
    contestó el modelo — el equivalente al panel de debug de live-vlm-webui.
    """
    cam       = _safe_cam_id(cam_id)
    resp_file = _DEBUG_LATEST_DIR / f"{cam}.response.json"

    if not resp_file.exists():
        raise HTTPException(404, f"Sin payload registrado para {cam}")

    try:
        raw = resp_file.read_text(encoding="utf-8", errors="replace")
        stat = resp_file.stat()
    except OSError as e:
        raise HTTPException(500, f"Error leyendo payload: {e}")

    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        # Los errores del gateway no siempre vienen en JSON
        response = {"_raw": raw[:4000]}

    media = None
    for ext in (".jpg", ".mp4"):
        f = _DEBUG_LATEST_DIR / f"{cam}{ext}"
        if not f.exists():
            continue
        st = f.stat()
        media = {
            "kind":  "video" if ext == ".mp4" else "frame",
            "bytes": st.st_size,
            "ts":    st.st_mtime,
            "url":   f"/api/debug/payload/{cam}/media",
        }
        break

    analyzer = getattr(request.app.state, "vlm_analyzer", None)
    return {
        "cam_id": cam,
        "ts":     stat.st_mtime,
        "request": {
            "endpoint":   getattr(analyzer, "endpoint", None),
            "model":      getattr(analyzer, "model", None),
            "max_tokens": getattr(analyzer, "max_tokens", None),
            "auth":       "bearer" if getattr(analyzer, "api_key", None) else "none",
            "media":      media,
        },
        "response": response,
    }


@router.get("/api/debug/payload/{cam_id}/media")
def debug_payload_media(cam_id: str):
    """Sirve el frame/clip exacto que se le mandó al VLM en la última llamada."""
    cam = _safe_cam_id(cam_id)
    for ext, mime in ((".jpg", "image/jpeg"), (".mp4", "video/mp4")):
        f = _DEBUG_LATEST_DIR / f"{cam}{ext}"
        if not f.exists():
            continue
        try:
            data = f.read_bytes()
        except OSError as e:
            raise HTTPException(500, f"Error leyendo media: {e}")
        # no-store: siempre se quiere el último, no una copia cacheada
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "no-store"})
    raise HTTPException(404, f"Sin media para {cam}")


# ── Histórico agregado (misma fuente que usa el chat) ───────────────────

@router.get("/api/events/aggregate")
def events_aggregate(request: Request,
                     hours:  float           = 24.0,
                     since:  Optional[float] = None,
                     until:  Optional[float] = None,
                     bucket: Optional[str]   = None):
    """
    Rollup agregado de eventos, resuelto en SQL. Es la misma función que
    alimenta al chat en ventanas largas, para que la UI y el modelo no puedan
    contar historias distintas.
    """
    db  = _db(request)
    now = time.time()
    if since is None:
        since = now - max(hours, 0.1) * 3600
    if bucket not in (None, "hour", "day"):
        raise HTTPException(400, "bucket debe ser 'hour' o 'day'")
    return db.aggregate_events(since=since, until=until, bucket=bucket)


# ── Storage stats ────────────────────────────────────────────────────────

@router.get("/api/storage/stats")
def storage_stats(request: Request):
    db = _db(request)
    sm = _snapshots(request)
    stats = db.stats()

    # Calcular tamaño total de snapshots en disco
    snap_bytes = 0
    try:
        for cam_dir in sm.base.iterdir():
            if not cam_dir.is_dir(): continue
            for day_dir in cam_dir.iterdir():
                if not day_dir.is_dir(): continue
                for f in day_dir.iterdir():
                    try:
                        snap_bytes += f.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass

    stats["snapshots_gb"] = round(snap_bytes / (1024 ** 3), 2)
    return stats


# ── Admin: trigger retention manual ──────────────────────────────────────

@router.post("/api/admin/rotate")
def admin_rotate(request: Request):
    """Trigger manual de retention (no espera 1h)."""
    db = _db(request)
    sm = _snapshots(request)
    t0 = time.monotonic()
    db_res   = db.rotate()
    snap_res = sm.rotate()
    return {
        "ms":         round((time.monotonic() - t0) * 1000),
        "db":         db_res or "no-op",
        "snapshots":  snap_res,
    }
