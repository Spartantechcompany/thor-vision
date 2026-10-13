"""Viajes entre camaras: lista, sugerencias de identidad por recorrido y aceptacion en bloque."""
import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    return db


@router.get("/api/journeys")
def journeys(request: Request, hours: float = 12.0, pending: int = 0, limit: int = 60):
    """Viajes recientes con sus visitas; pending=1 solo los que tienen visitas por confirmar (identidad conocida por el recorrido)."""
    db = _db(request)
    names = {c.id: c.name for c in request.app.state.config.cameras}
    since = time.time() - min(max(hours, 0.5), 168) * 3600
    with db._lock:
        js = db._conn.execute("SELECT j.id, j.start_ts, j.last_ts, j.origin, j.tuya_lead_s, j.subject_id, COALESCE(s.name,'Persona #'||s.id), s.named FROM journeys j "
                              "LEFT JOIN subjects s ON s.id=j.subject_id WHERE j.last_ts>=? AND j.status='open' AND j.n_visits>=2 ORDER BY j.last_ts DESC LIMIT ?",
                              (since, min(limit, 200))).fetchall()
        out = []
        for jid, a, b, origin, lead, sid, name, named in js:
            vs = db._conn.execute("SELECT id, cam_id, start_ts, subject_id, face IS NOT NULL, attrs FROM person_visits WHERE journey_id=? ORDER BY start_ts", (jid,)).fetchall()
            todo = [v for v in vs if v[3] is None]
            if pending and (not todo or sid is None):
                continue
            vis = []
            for vid, cam, ts, vsid, face, at in vs:
                try:
                    a_ = json.loads(at) if at else {}
                except ValueError:
                    a_ = {}
                vis.append({"id": vid, "cam": names.get(cam, cam), "ts": ts, "identified": vsid is not None, "face": bool(face), "ropa": {k: a_.get(k) for k in ("upper", "lower")}})
            out.append({"id": jid, "start_ts": a, "last_ts": b, "origin": origin, "tuya_lead_s": lead, "subject": {"id": sid, "name": name, "named": bool(named)} if sid else None,
                        "visits": vis, "to_confirm": len(todo) if sid else 0})
    return {"journeys": out}


@router.post("/api/journeys/{jid}/accept")
def accept(jid: int, request: Request):
    """Confirma el recorrido: las visitas sin identidad pasan a la persona del viaje (se recalcula su rostro promedio)."""
    from app.api.routes_people_admin import _recompute
    db = _db(request)
    with db._lock:
        row = db._conn.execute("SELECT subject_id FROM journeys WHERE id=?", (jid,)).fetchone()
        if not row or row[0] is None:
            raise HTTPException(404, "viaje sin identidad")
        cur = db._conn.execute("UPDATE person_visits SET subject_id=? WHERE journey_id=? AND subject_id IS NULL", (row[0], jid))
        db._conn.commit()
    _recompute(db, row[0])
    m = getattr(request.app.state, "visits", None)
    if m is not None:
        m.reload_subjects()
    return {"ok": True, "assigned": cur.rowcount}


@router.post("/api/journeys/{jid}/reject")
def reject(jid: int, request: Request):
    """No es esa persona: se quita la sugerencia y el viaje deja de proponerla."""
    db = _db(request)
    with db._lock:
        db._conn.execute("UPDATE journeys SET subject_id=NULL, status='rechazado' WHERE id=?", (jid,))
        for vid, at in db._conn.execute("SELECT id, attrs FROM person_visits WHERE journey_id=? AND attrs IS NOT NULL", (jid,)).fetchall():
            try:
                a = json.loads(at)
            except ValueError:
                continue
            if a.pop("journey", None) is not None:
                db._conn.execute("UPDATE person_visits SET attrs=? WHERE id=?", (json.dumps(a, ensure_ascii=False), vid))
        db._conn.commit()
    return {"ok": True}


@router.post("/api/journeys/backfill")
def backfill(request: Request, hours: float = 24.0):
    """Enlaza en orden cronologico las visitas de las ultimas horas que aun no tienen viaje (para medir el efecto con datos reales)."""
    import threading
    lk = getattr(request.app.state, "journeys", None)
    if lk is None:
        raise HTTPException(503, "viajes apagados")
    threading.Thread(target=lambda: lk.run_once(window_s=min(hours, 168) * 3600), daemon=True, name="journeys-backfill").start()
    return {"ok": True, "hours": hours}


@router.get("/api/journeys/stats")
def stats(request: Request, hours: float = 24.0):
    db = _db(request)
    since = time.time() - hours * 3600
    with db._lock:
        j = db._conn.execute("SELECT COUNT(*), SUM(n_visits>=2), SUM(subject_id IS NOT NULL), SUM(origin='tuya'), SUM(origin='entrada') FROM journeys WHERE start_ts>=?", (since,)).fetchone()
        vis = db._conn.execute("SELECT COUNT(*), SUM(journey_id IS NOT NULL), SUM(subject_id IS NOT NULL), SUM(subject_id IS NULL AND json_extract(attrs,'$.journey.sid') IS NOT NULL) "
                               "FROM person_visits WHERE start_ts>=? AND fp=0 AND static=0", (since,)).fetchone()
        tu = dict(db._conn.execute("SELECT state, COUNT(*) FROM tuya_entries WHERE ts>=? GROUP BY state", (since,)).fetchall())
    lk = getattr(request.app.state, "journeys", None)
    return {"hours": hours, "journeys": {"total": j[0], "multi_camera": j[1] or 0, "with_identity": j[2] or 0, "started_by_tuya": j[3] or 0, "entries": j[4] or 0},
            "visits": {"total": vis[0], "in_journey": vis[1] or 0, "identified_by_face": vis[2] or 0, "recovered_by_journey": vis[3] or 0},
            "tuya_entries": tu, "linker": lk.stats if lk else None}
