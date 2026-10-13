"""Rutas de vehiculos estacionados, etiquetado de mascotas y version/notas de cada release."""
import json
import os
import sqlite3
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app import version as ver
from app.vision import parked as parked_mod
from app.vision import pets as pets_mod

router = APIRouter()
_PLATE_RE = re.compile(r"^[A-Z0-9\- ]{3,12}$")


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    return db


# ---------------- version y releases
@router.get("/api/version")
def version():
    return {"version": ver.VERSION, "released": ver.RELEASED, "name": ver.NAME, "build": ver.build_info()}


@router.get("/api/releases")
def releases():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "release_notes.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        raise HTTPException(404, "release_notes.json no disponible")
    return {"current": ver.VERSION, "releases": data}


# ---------------- vehiculos estacionados
@router.get("/api/vehicles")
def vehicles(request: Request, state: Optional[str] = None, cam_id: Optional[str] = None,
             since: Optional[float] = None, limit: int = 100):
    names = {c.id: c.name for c in request.app.state.config.cameras}
    rows = parked_mod.list_vehicles(_db(request), state=state, cam=cam_id, since=since, limit=limit)
    for r in rows:
        r["cam"] = names.get(r["cam_id"], r["cam_id"])
    return {"vehicles": rows, "tracked_cams": sorted(parked_mod.CAMS), "min_parked_s": parked_mod.MIN_PARKED_S}


@router.get("/api/vehicles/{vid}/snaps")
def vehicle_snaps(vid: int, request: Request):
    """Capturas de la llegada, de cada hora y de la salida del vehiculo (las alertas que generaron captura)."""
    db = _db(request)
    with db._lock:
        rows = db._conn.execute(
            "SELECT e.ts, json_extract(e.data,'$.parked.state'), (SELECT s.id FROM snapshots s WHERE s.event_id=e.id ORDER BY s.id DESC LIMIT 1) "
            "FROM events e WHERE json_extract(e.data,'$.parked.vehicle_id')=? AND e.has_alert=1 ORDER BY e.ts", (vid,)).fetchall()
    label = {"arrived": "Llegada", "hourly": "Sigue estacionado", "left": "Se fue"}
    return {"snaps": [{"ts": t, "kind": label.get(st, st), "url": f"/api/snapshots/file/{sid}"} for t, st, sid in rows if sid]}


@router.get("/api/vehicles/{vid}/image")
def vehicle_image(vid: int, request: Request):
    db = _db(request)
    with db._lock:
        row = db._conn.execute("SELECT thumb FROM parked_vehicles WHERE id=?", (vid,)).fetchone()
    if not row or not row[0]:
        raise HTTPException(404)
    return Response(content=row[0], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


class VehiclePatch(BaseModel):
    plate: Optional[str] = None
    note: Optional[str] = None


@router.patch("/api/vehicles/{vid}")
def vehicle_patch(vid: int, body: VehiclePatch, request: Request):
    db = _db(request)
    sets, args = [], []
    if body.plate is not None:
        p = body.plate.strip().upper()
        if p and not _PLATE_RE.match(p):
            raise HTTPException(400, "placa invalida")
        sets.append("plate=?")
        args.append(p or None)
        sets.append("plate_conf=?")
        args.append(1.0 if p else None)
        sets.append("plate_src=?")
        args.append("manual" if p else None)
    if body.note is not None:
        sets.append("note=?")
        args.append(body.note.strip()[:200] or None)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    with db._lock:
        cur = db._conn.execute(f"UPDATE parked_vehicles SET {', '.join(sets)} WHERE id=?", (*args, vid))
        db._conn.commit()
    if not cur.rowcount:
        raise HTTPException(404)
    return {"ok": True}


@router.get("/api/vehicles/{vid}/plate")
def vehicle_plate_image(vid: int, request: Request):
    db = _db(request)
    with db._lock:
        row = db._conn.execute("SELECT plate_img FROM parked_vehicles WHERE id=?", (vid,)).fetchone()
    if not row or not row[0]:
        raise HTTPException(404)
    return Response(content=row[0], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ---------------- mascotas
@router.get("/api/pets/stats")
def pets_stats(request: Request):
    db = _db(request)
    with db._lock:
        rows = db._conn.execute("SELECT COALESCE(label,'sin_etiqueta'), COUNT(*) FROM pet_crops GROUP BY 1").fetchall()
    d = dict(rows)
    return {"counts": d, "total": sum(d.values()), "labels": list(pets_mod.LABELS),
            "names": pets_mod.NAMES, "ready_to_train": all(d.get(k, 0) >= 15 for k in ("akamaru", "mojo", "gigi"))}


@router.get("/api/pets/crops")
def pets_crops(request: Request, unlabeled: int = 1, label: Optional[str] = None, limit: int = 24,
               order: str = "random", pred: Optional[str] = None, offset: int = 0):
    db = _db(request)
    sql, args = "SELECT id, cam_id, ts, cls, conf, label, pred_label, pred_conf FROM pet_crops WHERE ", []
    if label:
        sql += "label=?"
        args.append(label)
    elif unlabeled:
        sql += "label IS NULL"
    else:
        sql += "1=1"
    if pred:
        sql += " AND pred_label=?"
        args.append(pred)
    sql += {"confident": " ORDER BY pred_conf DESC", "recent": " ORDER BY ts DESC"}.get(order, " ORDER BY RANDOM()")
    sql += " LIMIT ? OFFSET ?"
    args.extend([min(limit, 60), max(0, offset)])
    names = {c.id: c.name for c in request.app.state.config.cameras}
    with db._lock:
        rows = db._conn.execute(sql, args).fetchall()
    return {"crops": [{"id": i, "cam": names.get(c, c), "ts": t, "cls": k, "conf": round(cf or 0, 2), "label": lb,
                       "pred": pl, "pred_conf": round(pc, 3) if pc is not None else None}
                      for i, c, t, k, cf, lb, pl, pc in rows]}


@router.post("/api/pets/suggest")
def pets_suggest(request: Request):
    from app.vision import pet_model
    return pet_model.train_and_suggest(_db(request))


@router.get("/api/pets/model")
def pets_model():
    from app.vision import pet_model
    m = pet_model.load_model()
    return {"trained": bool(m), "n": m["n"] if m else 0, "ts": m["ts"] if m else None, "report": m["report"] if m else None}


@router.get("/api/pets/crops/{cid}/image")
def pets_image(cid: int, request: Request):
    db = _db(request)
    with db._lock:
        row = db._conn.execute("SELECT file FROM pet_crops WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404)
    path = os.path.realpath(os.path.join(pets_mod.ROOT, row[0]))
    if not path.startswith(os.path.realpath(pets_mod.ROOT) + os.sep) or not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


class PetLabel(BaseModel):
    label: Optional[str] = None


@router.post("/api/pets/crops/{cid}/label")
def pets_label(cid: int, body: PetLabel, request: Request):
    if body.label is not None and body.label not in pets_mod.LABELS:
        raise HTTPException(400, f"etiqueta invalida; usa una de {list(pets_mod.LABELS)}")
    db = _db(request)
    with db._lock:
        cur = db._conn.execute("UPDATE pet_crops SET label=?, labeled_ts=? WHERE id=?",
                               (body.label, time.time() if body.label else None, cid))
        db._conn.commit()
    if not cur.rowcount:
        raise HTTPException(404)
    return {"ok": True}


@router.get("/api/clef")
def clef_status(request: Request):
    """Estado del modelo de decision Clef-flash (servidor local) y lo que ha decidido el tracker con el."""
    import json as _json
    import urllib.request as _rq
    cl, vm = getattr(request.app.state, "clef", None), getattr(request.app.state, "visits", None)
    out = {"enabled": cl is not None, "client": cl.stats if cl else None,
           "tracker": {k: v for k, v in (vm.stats.items() if vm else []) if k.startswith("clef_")}}
    try:
        with _rq.urlopen((cl.url if cl else "http://127.0.0.1:20140") + "/health", timeout=3) as r:
            out["server"] = _json.loads(r.read())
    except (OSError, ValueError):
        out["server"] = None
    return out


@router.get("/api/trips")
def trips_list(request: Request, limit: int = 30):
    """Viajes disparados por eventos (puerta del garage, videoportero): direccion, visitas y alerta asociada."""
    db = _db(request)
    with db._lock:
        rows = db._conn.execute("SELECT id, start_ts, end_ts, alias, label, direction, n_visits, journey_id, event_id, status FROM trips ORDER BY id DESC LIMIT ?",
                                (min(max(limit, 1), 200),)).fetchall()
    keys = ("id", "start_ts", "end_ts", "alias", "label", "direction", "n_visits", "journey_id", "event_id", "status")
    return {"trips": [dict(zip(keys, r)) for r in rows]}


@router.get("/api/clips/recent")
def clips_recent(request: Request, hours: float = 48.0):
    """event_id -> clips de video de ese evento (para el boton ▶ del historial)."""
    db = _db(request)
    with db._lock:
        try:
            rows = db._conn.execute("SELECT id, event_id, cam_id, secs FROM clips WHERE ts>? ORDER BY ts DESC LIMIT 500", (time.time() - min(hours, 720) * 3600,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    names = {c.id: c.name for c in request.app.state.config.cameras}
    out: dict = {}
    for cid, eid, cam, secs in rows:
        out.setdefault(str(eid), []).append({"id": cid, "cam": names.get(cam, cam), "secs": round(secs or 0)})
    return {"clips": out}


@router.get("/api/clips/file/{cid}")
def clip_file(cid: int, request: Request):
    db = _db(request)
    cs = getattr(request.app.state, "clips", None)
    with db._lock:
        row = db._conn.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()
    if not row or cs is None:
        raise HTTPException(404)
    path = os.path.realpath(os.path.join(cs.root, row[0]))
    if not path.startswith(os.path.realpath(cs.root) + os.sep) or not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "max-age=86400"})


@router.post("/api/clips/now")
def clip_now(request: Request, cam: str):
    """Graba un clip de una camara ahora mismo (6 s previos + 6 s despues), sin alerta asociada; util para probar y para guardar un momento."""
    cs = getattr(request.app.state, "clips", None)
    if cs is None or cam not in cs.preroll:
        raise HTTPException(404, "camara sin pre-roll o clips apagados")
    cs.request(0, [cam])
    return {"ok": True, "espera_s": 6}


@router.get("/api/yolo-rules")
def yolo_rules_status(request: Request):
    """Modo sin VLM: si esta activo, por que y cuantas alertas ha generado."""
    yr = getattr(request.app.state, "yolo_rules", None)
    if yr is None:
        return {"enabled": False}
    last = yr.vlm_last_ok() or 0.0
    return {"enabled": True, "mode": __import__("app.vision.yolo_rules", fromlist=["MODE"]).MODE, "active": yr.active(), "vlm_last_ok_age_s": round(time.time() - last) if last else None,
            **yr.stats}


@router.get("/api/pets/presence")
def pets_presence(request: Request):
    """Estado de cada mascota conocida: en casa / fuera / sin datos, con su ultima camara y hora."""
    pp = getattr(request.app.state, "pet_presence", None)
    return {"pets": pp.status() if pp else []}
