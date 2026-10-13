"""Registro continuo de presencia (fase 1): cuantas visitas quedan identificadas por rostro, sugeridas por cuerpo/ropa, y estado de la validacion por esqueleto."""
import threading
import time

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/api/presence/stats")
def stats(request: Request, hours: float = 24.0):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    names = {c.id: c.name for c in request.app.state.config.cameras}
    since = time.time() - min(max(hours, 1), 168) * 3600
    with db._lock:
        rows = db._conn.execute(
            "SELECT cam_id, COUNT(*), SUM(subject_id IS NOT NULL), SUM(subject_id IS NULL AND json_extract(attrs,'$.guess.id') IS NOT NULL), SUM(attrs IS NOT NULL), "
            "SUM(face IS NOT NULL) FROM person_visits WHERE fp=0 AND static=0 AND start_ts>=? GROUP BY cam_id ORDER BY 2 DESC", (since,)).fetchall()
    cams = [{"cam": names.get(c, c), "visits": n, "with_identity": i or 0, "with_guess": g or 0, "with_body": b or 0, "with_face": f or 0,
             "pct_identified": round(100 * (i or 0) / n, 1) if n else 0, "pct_identified_or_guess": round(100 * ((i or 0) + (g or 0)) / n, 1) if n else 0}
            for c, n, i, g, b, f in rows]
    tot = {k: sum(c[k] for c in cams) for k in ("visits", "with_identity", "with_guess", "with_body", "with_face")}
    bid = getattr(getattr(request.app.state, "visits", None), "bodyid", None)
    models = getattr(request.app.state, "vision_models", None)
    return {"hours": hours, "total": tot, "cams": cams, "bodyid": bid.stats if bid else None, "pose": getattr(models, "pose_stats", None)}


@router.post("/api/presence/backfill")
def backfill(request: Request, hours: float = 24.0):
    bid = getattr(getattr(request.app.state, "visits", None), "bodyid", None)
    if bid is None:
        raise HTTPException(503, "reconocimiento de cuerpo apagado")
    threading.Thread(target=lambda: bid.backfill(hours), daemon=True, name="bodyid-backfill").start()
    return {"ok": True, "hours": hours, "note": "procesando en segundo plano; consulta /api/presence/stats"}


@router.get("/api/presence/capture-quality")
def capture_quality(request: Request, hours: float = 168.0, grid_x: int = 8, grid_y: int = 6):
    """Donde salen las mejores capturas de rostro: por camara, calidad (mediana y p90) y un mapa de calor del encuadre (calidad media por celda)."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    names = {c.id: c.name for c in request.app.state.config.cameras}
    since = time.time() - min(max(hours, 1), 24 * 30) * 3600
    with db._lock:
        rows = db._conn.execute("SELECT cam_id, face IS NOT NULL, face_score, face_xy FROM person_visits WHERE fp=0 AND static=0 AND start_ts>=?", (since,)).fetchall()
    cams: dict = {}
    for cam, has, q, xy in rows:
        c = cams.setdefault(cam, {"visits": 0, "with_face": 0, "q": [], "cells": {}})
        c["visits"] += 1
        if has and q:
            c["with_face"] += 1
            c["q"].append(q)
            if xy:
                try:
                    x, y = (float(v) for v in xy.split(","))
                except ValueError:
                    continue
                k = (min(grid_x - 1, int(x * grid_x)), min(grid_y - 1, int(y * grid_y)))
                cell = c["cells"].setdefault(k, [0, 0.0])
                cell[0] += 1
                cell[1] += q
    out = []
    for cam, c in cams.items():
        qs = sorted(c["q"])
        pct = lambda p: round(qs[min(len(qs) - 1, int(len(qs) * p))], 1) if qs else None
        cells = [{"x": k[0], "y": k[1], "n": v[0], "q": round(v[1] / v[0], 1)} for k, v in c["cells"].items()]
        best = sorted(cells, key=lambda z: -(z["q"] * min(z["n"], 10)))[:3]
        out.append({"cam_id": cam, "cam": names.get(cam, cam), "visits": c["visits"], "with_face": c["with_face"],
                    "pct_face": round(100 * c["with_face"] / c["visits"], 1) if c["visits"] else 0, "q_median": pct(0.5), "q_p90": pct(0.9),
                    "cells": cells, "best_cells": best, "mapped": sum(z["n"] for z in cells)})
    out.sort(key=lambda z: -z["with_face"])
    return {"hours": hours, "grid": [grid_x, grid_y], "cams": out,
            "note": "El mapa se llena con las visitas nuevas (la posicion no se guardaba antes)."}


@router.get("/api/gait/eval")
def gait_eval(request: Request, min_per_id: int = 4):
    """Mide si el vector de marcha separa a las personas: AUC entre pares de la misma identidad y de identidades distintas (visitas con rostro confirmado)."""
    import json as _json

    import numpy as np

    from app.vision import gait
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    with db._lock:
        rows = db._conn.execute("SELECT g.feat, v.subject_id, g.cam_id, g.ts FROM gait_samples g JOIN person_visits v ON v.id=g.visit_id "
                                "JOIN subjects s ON s.id=v.subject_id WHERE s.named=1 AND g.quality>=0.5").fetchall()
        total = db._conn.execute("SELECT COUNT(*), AVG(quality), AVG(fps) FROM gait_samples").fetchone()
    by: dict = {}
    for f, sid, cam, ts in rows:
        by.setdefault(sid, []).append((np.array(_json.loads(f)), ts))
    keep = {s: v for s, v in by.items() if len(v) >= min_per_id}
    if len(keep) < 2:
        return {"samples": total[0], "quality_avg": round(total[1] or 0, 2), "fps_avg": round(total[2] or 0, 1), "identities_usable": len(keep),
                "note": "Aun no hay suficientes muestras de personas identificadas (se necesitan >= %d por persona y 2 personas)." % min_per_id}
    allv = np.stack([x for v in keep.values() for x, _ in v])
    sd = allv.std(axis=0) + 1e-6
    items = [(sid, (x - allv.mean(axis=0)) / sd, ts) for sid, v in keep.items() for x, ts in v]
    pos, neg, pos_day, neg_day = [], [], [], []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            d = float(np.linalg.norm(items[i][1] - items[j][1]))
            same_day = abs(items[i][2] - items[j][2]) < 86400
            (pos if items[i][0] == items[j][0] else neg).append(d)
            if same_day:
                (pos_day if items[i][0] == items[j][0] else neg_day).append(d)
    return {"samples": total[0], "quality_avg": round(total[1] or 0, 2), "fps_avg": round(total[2] or 0, 1), "identities_usable": len(keep), "labeled_samples": len(items),
            "auc": round(gait.auc(pos, neg), 3), "auc_same_day": round(gait.auc(pos_day, neg_day), 3) if pos_day and neg_day else None, "features": gait.NAMES,
            "decision": "usar solo si el AUC supera claramente 0.8 (el cuerpo/ReID dio 0.73)"}
