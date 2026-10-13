"""Clasificacion de personas en tres bases (por revisar / empleados / invitados), parecidos y asistencia."""
import csv
import io
import os
import sqlite3
import time
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()
CATEGORIES = ("revisar", "empleado", "invitado")
ATTENDANCE_EXTRA = {c.strip() for c in os.environ.get("ATTENDANCE_EXTRA_CAMS", "cam-228").split(",") if c.strip()}   # acceso peatonal: Garage frontal
DOORBELL_CAMS = {c.strip() for c in os.environ.get("DOORBELL_CAMS", "cam-vto").split(",") if c.strip()}
GAP_S = 600   # huecos menores a 10 min dentro de una cámara interna cuentan como presencia continua


def _street(request: Request) -> list:
    """Camaras de la calle: exteriores y videoportero. Su trafico queda como registro, no cuenta en ocupacion ni asistencia ni en 'por revisar'."""
    return [c.id for c in request.app.state.config.cameras if getattr(c, "zone", "") == "exterior" or c.id in DOORBELL_CAMS]


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    return db


def ensure_schema(db) -> None:
    with db._lock:
        try:
            db._conn.execute("ALTER TABLE subjects ADD COLUMN category TEXT NOT NULL DEFAULT 'revisar'")
        except Exception:
            pass
        db._conn.execute("CREATE INDEX IF NOT EXISTS idx_subjects_cat ON subjects(category)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS subject_rejects (a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY (a, b))")
        db._conn.commit()


def _embeddings(db) -> tuple:
    with db._lock:
        rows = db._conn.execute("SELECT id, embedding FROM subjects WHERE embedding IS NOT NULL AND n_emb > 0").fetchall()
    ids, vecs = [], []
    for sid, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        if v.size == 512 and np.isfinite(v).all():
            n = float(np.linalg.norm(v))
            if n > 0:
                ids.append(sid)
                vecs.append(v / n)
    return ids, (np.stack(vecs) if vecs else np.zeros((0, 512), np.float32))


def _subject_rows(db, where="", args=(), limit=200) -> list:
    sql = ("SELECT s.id, COALESCE(s.name, 'Persona #' || s.id), s.named, s.category, s.created_ts, s.last_ts, "
           "(SELECT COUNT(*) FROM person_visits v WHERE v.subject_id=s.id AND v.fp=0 AND v.static=0), "
           "(SELECT group_concat(DISTINCT v.cam_id) FROM person_visits v WHERE v.subject_id=s.id AND v.fp=0 AND v.static=0), "
           "(SELECT COUNT(*) FROM person_visits v WHERE v.subject_id=s.id AND v.face IS NOT NULL) "
           "FROM subjects s WHERE 1=1 " + where + " ORDER BY s.last_ts DESC LIMIT ?")
    with db._lock:
        rows = db._conn.execute(sql, (*args, min(limit, 500))).fetchall()
    return rows


@router.get("/api/people/overview")
def overview(request: Request):
    db = _db(request)
    with db._lock:
        rows = db._conn.execute("SELECT category, COUNT(*), SUM(named) FROM subjects GROUP BY category").fetchall()
    d = {c: {"total": 0, "named": 0} for c in CATEGORIES}
    for cat, n, nm in rows:
        d.setdefault(cat, {"total": 0, "named": 0}).update(total=n, named=int(nm or 0))
    return {"categories": d}


@router.get("/api/people")
def people(request: Request, category: str = "revisar", q: Optional[str] = None, only_named: int = 0, limit: int = 200,
           scope: str = "interior"):
    if category not in CATEGORIES:
        raise HTTPException(400, f"categoria invalida; usa {list(CATEGORIES)}")
    db = _db(request)
    names = {c.id: c.name for c in request.app.state.config.cameras}
    where, args = " AND s.category=?", [category]
    if only_named:
        where += " AND s.named=1"
    ext = _street(request)
    if scope in ("interior", "calle") and ext:
        ph = ",".join("?" * len(ext))
        if scope == "interior":       # entraron: alguna visita en camara interior (o ya tienen nombre)
            where += f" AND (s.named=1 OR EXISTS (SELECT 1 FROM person_visits v WHERE v.subject_id=s.id AND v.fp=0 AND v.static=0 AND v.cam_id NOT IN ({ph})))"
        else:                          # solo pasaron por la calle / videoportero: registro, sin revisar a mano
            where += f" AND s.named=0 AND NOT EXISTS (SELECT 1 FROM person_visits v WHERE v.subject_id=s.id AND v.fp=0 AND v.static=0 AND v.cam_id NOT IN ({ph}))"
        args.extend(ext)
    if q:
        where += " AND lower(COALESCE(s.name,'')) LIKE ?"
        args.append(f"%{q.lower()}%")
    out = []
    sug = _suggestions(db) if category == "revisar" else {}
    rows = _subject_rows(db, where, tuple(args), limit)
    desc: dict = {}
    if rows:
        with db._lock:
            for sid, d in db._conn.execute(
                    f"SELECT subject_id, vlm_desc FROM person_visits WHERE subject_id IN ({','.join('?' * len(rows))}) AND vlm_desc IS NOT NULL AND vlm_desc<>'' "
                    "ORDER BY start_ts ASC", [r[0] for r in rows]):
                desc[sid] = d                      # queda la mas reciente: ropa y rasgos que el VLM vio en su ultima visita
    for sid, name, named, cat, created, last, visits, cams, faces in rows:
        out.append({"id": sid, "name": name, "named": bool(named), "category": cat, "created_ts": created, "last_ts": last,
                    "visits": visits, "cams": [names.get(c, c) for c in (cams or "").split(",") if c],
                    "cam_ids": [c for c in (cams or "").split(",") if c], "last_desc": (desc.get(sid) or "")[:200],
                    "has_face": faces > 0, "suggested": "empleado" if (named and cat == "revisar") else None,
                    "match": sug.get(sid)})
    return {"category": category, "people": out}


def _rejected(db) -> set:
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS subject_rejects (a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY (a, b))")
        return {(a, b) for a, b in db._conn.execute("SELECT a, b FROM subject_rejects")}


def _suggestions(db, min_sim: float = 0.38) -> dict:
    """Para cada persona sin nombre, la persona con nombre (empleado o invitado) a la que mas se parece: {id: {id,name,category,sim}}."""
    ids, E = _embeddings(db)
    if len(ids) < 2:
        return {}
    with db._lock:
        meta = {r[0]: r for r in db._conn.execute("SELECT id, COALESCE(name,'Persona #'||id), named, category FROM subjects").fetchall()}
    tgt = [j for j, i in enumerate(ids) if i in meta and meta[i][2] and meta[i][3] in ("empleado", "invitado")]
    if not tgt:
        return {}
    S = E @ E[tgt].T
    rej = _rejected(db)
    out = {}
    for j, i in enumerate(ids):
        if i not in meta or meta[i][2]:
            continue
        masked = np.where(np.array(ids)[tgt] == i, -1, S[j])
        for k_, t_ in enumerate(tgt):                 # descarta a quienes el usuario ya dijo "no es"
            if (i, ids[t_]) in rej:
                masked[k_] = -1
        k = int(np.argmax(masked))
        sim = float(masked[k])
        if sim >= min_sim:
            t = meta[ids[tgt[k]]]
            out[i] = {"id": t[0], "name": t[1], "category": t[3], "sim": round(sim, 3)}
    return out


class AcceptBody(BaseModel):
    min_sim: float = 0.5
    dry: bool = True


@router.post("/api/people/accept-suggestions")
def accept_suggestions(body: AcceptBody, request: Request):
    """Fusiona las personas sin nombre que se parecen >= min_sim a una persona con nombre (dry=true solo lista)."""
    db = _db(request)
    m = getattr(request.app.state, "visits", None)
    if m is None:
        raise HTTPException(503, "tracker apagado")
    street = set(_street(request))
    pairs = []
    for sid, s in _suggestions(db, max(0.3, body.min_sim)).items():
        with db._lock:
            inside = db._conn.execute("SELECT 1 FROM person_visits WHERE subject_id=? AND fp=0 AND static=0 AND cam_id NOT IN (%s) LIMIT 1" % ",".join("?" * len(street) or "NULL"),
                                      (sid, *street)).fetchone()
        if inside:                                 # solo quienes entraron; la gente de la calle no se mezcla con empleados
            pairs.append((sid, s))
    done = 0
    if not body.dry:
        for sid, s in pairs:
            if m.merge_subjects(sid, s["id"]):
                done += 1
    return {"dry": body.dry, "candidates": [{"from": sid, **s} for sid, s in pairs], "merged": done}


class CategoryBody(BaseModel):
    category: str


class BulkBody(BaseModel):
    ids: list
    category: str


@router.patch("/api/people/{sid}/category")
def set_category(sid: int, body: CategoryBody, request: Request):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"categoria invalida; usa {list(CATEGORIES)}")
    db = _db(request)
    with db._lock:
        cur = db._conn.execute("UPDATE subjects SET category=? WHERE id=?", (body.category, sid))
        db._conn.commit()
    if not cur.rowcount:
        raise HTTPException(404, "sujeto inexistente")
    return {"ok": True}


@router.post("/api/people/bulk-category")
def bulk_category(body: BulkBody, request: Request):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"categoria invalida; usa {list(CATEGORIES)}")
    ids = [int(i) for i in body.ids][:500]
    if not ids:
        raise HTTPException(400, "ids vacio")
    db = _db(request)
    with db._lock:
        cur = db._conn.execute(f"UPDATE subjects SET category=? WHERE id IN ({','.join('?' * len(ids))})", (body.category, *ids))
        db._conn.commit()
    return {"ok": True, "updated": cur.rowcount}


@router.get("/api/people/{sid}/similar")
def similar(sid: int, request: Request, min_sim: float = 0.32, limit: int = 12):
    db = _db(request)
    ids, E = _embeddings(db)
    if sid not in ids:
        return {"similar": [], "note": "este sujeto aun no tiene embedding facial"}
    i = ids.index(sid)
    sims = E @ E[i]
    order = [j for j in np.argsort(-sims) if ids[j] != sid and sims[j] >= min_sim][:limit]
    rows = {r[0]: r for r in _subject_rows(db, f" AND s.id IN ({','.join('?' * len(order))})" if order else " AND 0", tuple(ids[j] for j in order), 500)} if order else {}
    out = []
    for j in order:
        r = rows.get(ids[j])
        if r:
            out.append({"id": ids[j], "name": r[1], "named": bool(r[2]), "category": r[3], "visits": r[6], "last_ts": r[5],
                        "similarity": round(float(sims[j]), 3)})
    return {"similar": out, "auto_merge_threshold": 0.45}


@router.get("/api/people/duplicates")
def duplicates(request: Request, min_sim: float = 0.38, limit: int = 40):
    """Pares de sujetos que se parecen entre si (candidatos a fusionar)."""
    db = _db(request)
    ids, E = _embeddings(db)
    if len(ids) < 2:
        return {"pairs": []}
    S = E @ E.T
    iu = np.triu_indices(len(ids), 1)
    vals = S[iu]
    keep = np.where(vals >= min_sim)[0]
    keep = keep[np.argsort(-vals[keep])][:limit]
    with db._lock:
        meta = {r[0]: r for r in db._conn.execute("SELECT id, COALESCE(name,'Persona #'||id), named, category FROM subjects").fetchall()}
    rej = _rejected(db)
    out = []
    for k in keep:
        a, b = ids[iu[0][k]], ids[iu[1][k]]
        ma, mb = meta.get(a), meta.get(b)
        if not ma or not mb or (a, b) in rej or (b, a) in rej:
            continue
        if ma[2] and mb[2] and ma[1].strip().lower() != mb[1].strip().lower():
            continue                      # dos personas con nombre distinto son personas distintas: no se ofrece unirlas
        out.append({"a": a, "b": b, "similarity": round(float(vals[k]), 3), "a_name": ma[1], "b_name": mb[1], "a_cat": ma[3], "b_cat": mb[3]})
    return {"pairs": out}


def _merge(intervals, gap=GAP_S):
    out = []
    for a, b in sorted(intervals):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _day_bounds(date: Optional[str]) -> tuple:
    lt = time.localtime() if not date else time.strptime(date, "%Y-%m-%d")
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return day0, day0 + 86400


def _habits(db, sids: list, internal: list, street: set, days: int = 30) -> dict:
    """Horario habitual aprendido del historial: mediana de la primera y la ultima vez vistos por dia (entre semana / fin de semana)."""
    if not sids:
        return {}
    since = time.time() - days * 86400
    ph_s, ph_c = ",".join("?" * len(sids)), ",".join("?" * len(internal)) or "NULL"
    with db._lock:
        rows = db._conn.execute(
            f"SELECT subject_id, cam_id, start_ts, COALESCE(end_ts, start_ts) FROM person_visits WHERE fp=0 AND static=0 AND start_ts>=? AND subject_id IN ({ph_s})",
            (since, *sids)).fetchall()
    per: dict = {}                       # (grupo, dia) -> [primera, ultima] en segundos desde medianoche
    for sid, cam, a, b in rows:
        lt = time.localtime(a)
        day = (lt.tm_year, lt.tm_yday)
        lo = lambda t: time.localtime(t).tm_hour * 3600 + time.localtime(t).tm_min * 60
        if cam in internal or cam in street:
            e = per.setdefault((lt.tm_wday >= 5, day), [lo(a), lo(b)])
            e[0], e[1] = min(e[0], lo(a)), max(e[1], lo(b))
    out: dict = {}
    for weekend in (False, True):
        ds = [v for (we, _), v in per.items() if we == weekend and v[1] - v[0] >= 1800]     # dias con al menos media hora de presencia
        if len(ds) >= 3:
            ds_in, ds_out = sorted(v[0] for v in ds), sorted(v[1] for v in ds)
            out["weekend" if weekend else "weekday"] = {"in": ds_in[len(ds_in) // 2], "out": ds_out[len(ds_out) // 2], "days": len(ds)}
    return out


def _hm(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def build_attendance(request: Request, date: Optional[str]) -> dict:
    db = _db(request)
    try:
        t0, t1 = _day_bounds(date)
    except ValueError:
        raise HTTPException(400, "fecha invalida; usa YYYY-MM-DD")
    cams = request.app.state.config.cameras
    names = {c.id: c.name for c in cams}
    street = set(_street(request))
    internal = [c.id for c in cams if (getattr(c, "zone", "interior") == "interior" or c.id in ATTENDANCE_EXTRA) and c.id not in street]
    now = time.time()
    with db._lock:
        emp = db._conn.execute("SELECT id, COALESCE(name,'Persona #'||id) FROM subjects WHERE category='empleado' ORDER BY name").fetchall()
        vis = db._conn.execute(
            "SELECT v.subject_id, v.cam_id, v.start_ts, COALESCE(v.end_ts, v.start_ts) FROM person_visits v JOIN subjects s ON s.id=v.subject_id "
            "WHERE s.category='empleado' AND v.fp=0 AND v.static=0 AND v.start_ts>=? AND v.start_ts<? "
            f"AND v.cam_id IN ({','.join('?' * len(internal)) or 'NULL'})", (t0, t1, *internal)).fetchall()
        gst = db._conn.execute(
            "SELECT v.subject_id, COALESCE(s.name,'Invitado #'||s.id), MIN(v.start_ts), MAX(COALESCE(v.end_ts,v.start_ts)), COUNT(*), group_concat(DISTINCT v.cam_id) "
            "FROM person_visits v JOIN subjects s ON s.id=v.subject_id WHERE s.category='invitado' AND v.fp=0 AND v.static=0 "
            "AND v.start_ts>=? AND v.start_ts<? GROUP BY v.subject_id ORDER BY MIN(v.start_ts)", (t0, t1)).fetchall()
        out_vis = db._conn.execute(
            "SELECT v.subject_id, MAX(COALESCE(v.end_ts, v.start_ts)) FROM person_visits v JOIN subjects s ON s.id=v.subject_id "
            "WHERE s.category='empleado' AND v.fp=0 AND v.static=0 AND v.start_ts>=? AND v.start_ts<? "
            f"AND v.cam_id IN ({','.join('?' * len(street)) or 'NULL'}) GROUP BY v.subject_id", (t0, t1, *street)).fetchall()
        pend = db._conn.execute(
            "SELECT COUNT(DISTINCT v.subject_id) FROM person_visits v JOIN subjects s ON s.id=v.subject_id WHERE s.category='revisar' "
            "AND v.fp=0 AND v.static=0 AND v.start_ts>=? AND v.start_ts<?", (t0, t1)).fetchone()[0]
    from app.api import routes_wifi
    routes_wifi._db(request)                                       # crea las tablas de WiFi si aun no existen
    wifi = routes_wifi.presence_by_employee(db, t0, t1)
    by: dict = {}
    for sid, cam, a, b in vis:
        by.setdefault(sid, []).append((a, b, cam))
    rows = []
    groups: dict = {}
    for sid, name in emp:
        groups.setdefault(name.strip().lower() if not name.startswith("Persona #") else f"#{sid}", []).append((sid, name))
    for _, members in groups.items():
        sid, name = members[0]
        v = [x for m_sid, _ in members for x in by.get(m_sid, [])]
        segs = _merge([(a, b) for a, b, _ in v])
        present = sum(b - a for a, b in segs)
        last_in = max((b for _, b, _ in v), default=None)
        last_out = max((t for m_sid, t in out_vis if m_sid in {x for x, _ in members}), default=None)
        left = bool(last_out and last_in and last_out >= last_in)           # lo vieron en la calle despues de su ultima vez adentro
        inside = bool(v) and not left and (not (t0 <= now < t1) or now - last_in < 900)
        hab = _habits(db, [x for x, _ in members], set(internal), street)
        usual = hab.get("weekend" if time.localtime(t0).tm_wday >= 5 else "weekday")
        status = ""
        if usual and t0 <= now < t1:
            sec = time.localtime(now).tm_hour * 3600 + time.localtime(now).tm_min * 60
            if not inside and not left and usual["in"] + 1800 <= sec <= usual["out"] - 1800 and not v:
                status = "deberia estar"
            elif inside and sec > usual["out"] + 3600:
                status = "se quedo"
        wf = [wifi[x] for x, _ in members if x in wifi]
        rows.append({"wifi_first_ts": min((w["first"] for w in wf), default=None), "wifi_last_ts": max((w["last"] for w in wf), default=None),
                     "wifi_minutes": max((w["minutes"] for w in wf), default=0), "wifi_devices": sorted({d for w in wf for d in w["devices"]}),
                     "attended_any": bool(v) or bool(wf),
                     "id": sid, "name": name, "usual_in": _hm(usual["in"]) if usual else None, "usual_out": _hm(usual["out"]) if usual else None,
                     "usual_days": usual["days"] if usual else 0, "status": status,
                     "present": inside, "attended": bool(v), "left": left, "exit_ts": last_out if left else None,
                     "first_ts": min((a for a, _, _ in v), default=None),
                     "last_ts": max(last_in or 0, last_out or 0) or None, "minutes": round(present / 60),
                     "segments": len(segs), "cams": sorted({names.get(c, c) for _, _, c in v})})
    return {"date": time.strftime("%Y-%m-%d", time.localtime(t0)), "internal_cams": [names[c] for c in internal],
            "partial_day": t0 <= now < t1,
            "employees": rows,
            "guests": {"count": len(gst), "people": [{"id": g[0], "name": g[1], "first_ts": g[2], "last_ts": g[3], "visits": g[4],
                                                      "cams": [names.get(c, c) for c in (g[5] or "").split(",") if c],
                                                      "snapshot": f"/api/subjects/{g[0]}/face"} for g in gst]},
            "pending_review": pend}


@router.get("/api/attendance")
def attendance(request: Request, date: Optional[str] = None):
    return build_attendance(request, date)


@router.get("/api/attendance.csv")
def attendance_csv(request: Request, date: Optional[str] = None):
    d = build_attendance(request, date)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fecha", "empleado", "presente", "primera_vez", "ultima_vez", "minutos_en_oficina", "camaras", "wifi_primera", "wifi_ultima", "wifi_minutos"])
    hm = lambda ts: time.strftime("%H:%M", time.localtime(ts)) if ts else ""
    for e in d["employees"]:
        w.writerow([d["date"], e["name"], "si" if e["present"] else "no", hm(e["first_ts"]), hm(e["last_ts"]), e["minutes"], "; ".join(e["cams"]), hm(e.get("wifi_first_ts")), hm(e.get("wifi_last_ts")), e.get("wifi_minutes", 0)])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="asistencia_{d["date"]}.csv"'})


@router.get("/api/people/{sid}/visits")
def person_visits(sid: int, request: Request, limit: int = 24):
    db = _db(request)
    names = {c.id: c.name for c in request.app.state.config.cameras}
    with db._lock:
        rows = db._conn.execute(
            "SELECT id, cam_id, start_ts, COALESCE(end_ts,start_ts), (face IS NOT NULL), (body IS NOT NULL) FROM person_visits "
            "WHERE subject_id=? AND fp=0 AND static=0 ORDER BY start_ts DESC LIMIT ?", (sid, min(limit, 100))).fetchall()
    return {"visits": [{"id": i, "cam": names.get(c, c), "start": a, "end": b, "has_face": bool(f), "has_body": bool(bd)}
                       for i, c, a, b, f, bd in rows]}


@router.get("/api/people/today")
def people_today(request: Request):
    """Resumen del dia: cuantos empleados presentes, invitados vistos y personas por revisar."""
    d = build_attendance(request, None)
    emp = d["employees"]
    return {"date": d["date"], "employees_present": sum(1 for e in emp if e["present"]), "employees_total": len(emp),
            "guests": d["guests"]["count"], "pending_review": d["pending_review"]}


def _same_name_groups(db) -> list:
    with db._lock:
        rows = db._conn.execute(
            "SELECT s.id, lower(trim(s.name)), s.category, (SELECT COUNT(*) FROM person_visits v WHERE v.subject_id=s.id) "
            "FROM subjects s WHERE s.named=1 AND s.name IS NOT NULL AND trim(s.name)<>''").fetchall()
    g: dict = {}
    for sid, nm, cat, n in rows:
        g.setdefault(nm, []).append((sid, cat, n))
    return [v for v in g.values() if len(v) > 1]


@router.get("/api/people/same-name")
def same_name(request: Request):
    return {"groups": [[{"id": i, "category": c, "visits": n} for i, c, n in g] for g in _same_name_groups(_db(request))]}


@router.post("/api/people/consolidate")
def consolidate(request: Request):
    """Une los sujetos con el mismo nombre en uno solo (el que tiene mas visitas; empleado gana sobre invitado y por revisar)."""
    db = _db(request)
    m = getattr(request.app.state, "visits", None)
    if m is None:
        raise HTTPException(503, "tracker apagado")
    rank = {"empleado": 0, "invitado": 1, "revisar": 2}
    merged = 0
    for g in _same_name_groups(db):
        g.sort(key=lambda t: (rank.get(t[1], 3), -t[2], t[0]))
        keep = g[0][0]
        for sid, _, _ in g[1:]:
            if m.merge_subjects(sid, keep):
                merged += 1
    return {"ok": True, "merged": merged}


@router.get("/api/occupancy")
def occupancy(request: Request, date: Optional[str] = None):
    """Ocupacion de las camaras interiores (y Garage frontal): ahora, por hora, pico y permanencia. Sin exteriores."""
    db = _db(request)
    t0, t1 = _day_bounds(date)
    cams = request.app.state.config.cameras
    names = {c.id: c.name for c in cams}
    street = set(_street(request))
    ids = [c.id for c in cams if (getattr(c, "zone", "interior") == "interior" or c.id in ATTENDANCE_EXTRA) and c.id not in street]
    ph = ",".join("?" * len(ids)) or "NULL"
    now = time.time()
    with db._lock:
        vis = db._conn.execute(
            "SELECT v.subject_id, COALESCE(s.category,'revisar'), v.cam_id, v.start_ts, COALESCE(v.end_ts, v.start_ts), v.status "
            "FROM person_visits v LEFT JOIN subjects s ON s.id=v.subject_id WHERE v.fp=0 AND v.static=0 AND v.start_ts<? "
            f"AND COALESCE(v.end_ts, v.start_ts)>=? AND v.cam_id IN ({ph})", (t1, t0, *ids)).fetchall()
    hours = [{"h": h, "empleado": set(), "invitado": set(), "revisar": set()} for h in range(24)]
    now_cams: dict = {}
    dwell = {"empleado": [], "invitado": []}
    for sid, cat, cam, a, b, st in vis:
        if cat != "empleado" and b - a < 8:
            continue            # visitas sin identidad y de pocos segundos: ruido de deteccion
        key = sid if sid is not None else f"v{a:.0f}{cam}"
        for h in range(24):
            hs = t0 + h * 3600
            if a < hs + 3600 and b >= hs:
                hours[h][cat if cat in ("empleado", "invitado") else "revisar"].add(key)
        if b >= now - 120:      # visitas "open" huerfanas tras un reinicio no cuentan
            now_cams.setdefault(cam, set()).add(key)
        if cat in dwell:
            dwell[cat].append(b - a)
    series = [{"h": x["h"], "empleado": len(x["empleado"]), "invitado": len(x["invitado"]), "revisar": len(x["revisar"])} for x in hours]
    tot = [x["empleado"] + x["invitado"] + x["revisar"] for x in series]
    peak = max(range(24), key=lambda i: tot[i]) if any(tot) else None
    att = build_attendance(request, date)
    mins = [e["minutes"] for e in att["employees"] if e["present"]]
    return {"date": att["date"], "now": {names.get(c, c): len(v) for c, v in sorted(now_cams.items())}, "now_total": len({k for v in now_cams.values() for k in v}),
            "hourly": series, "peak_hour": peak, "peak_count": tot[peak] if peak is not None else 0,
            "employees_present": sum(1 for e in att["employees"] if e["present"]), "employees_total": len(att["employees"]),
            "guests": att["guests"]["count"], "avg_employee_minutes": round(sum(mins) / len(mins)) if mins else 0,
            "avg_guest_visit_min": round(sum(dwell["invitado"]) / len(dwell["invitado"]) / 60, 1) if dwell["invitado"] else 0,
            "cams": [names[c] for c in ids]}


class RejectBody(BaseModel):
    a: int
    b: int


@router.post("/api/people/reject")
def reject_pair(body: RejectBody, request: Request):
    """'No son la misma persona': esa sugerencia no vuelve a aparecer."""
    db = _db(request)
    _rejected(db)                                        # crea la tabla si no existe
    with db._lock:
        db._conn.execute("INSERT OR IGNORE INTO subject_rejects (a, b) VALUES (?, ?)", (body.a, body.b))
        db._conn.commit()
    return {"ok": True}


@router.get("/api/people/suggestions")
def suggestions(request: Request, min_sim: float = 0.4, limit: int = 120):
    """Personas sin nombre que se parecen a una identificada (solo quienes entraron), de mayor a menor parecido."""
    db = _db(request)
    street = set(_street(request))
    out = []
    for sid, s in sorted(_suggestions(db, max(0.3, min_sim)).items(), key=lambda kv: -kv[1]["sim"]):
        with db._lock:
            row = db._conn.execute("SELECT 1 FROM person_visits WHERE subject_id=? AND fp=0 AND static=0 AND cam_id NOT IN (%s) LIMIT 1" % ",".join("?" * len(street) or "NULL"),
                                   (sid, *street)).fetchone()
            nm = db._conn.execute("SELECT COALESCE(name,'Persona #'||id), (SELECT COUNT(*) FROM person_visits WHERE subject_id=subjects.id) FROM subjects WHERE id=?", (sid,)).fetchone()
        if row and nm:
            out.append({"from": sid, "from_name": nm[0], "visits": nm[1], "into": s["id"], "into_name": s["name"], "category": s["category"], "sim": s["sim"]})
        if len(out) >= limit:
            break
    return {"pairs": out}


class MergeMany(BaseModel):
    pairs: list


@router.post("/api/people/merge-many")
def merge_many(body: MergeMany, request: Request):
    m = getattr(request.app.state, "visits", None)
    if m is None:
        raise HTTPException(503, "tracker apagado")
    done = 0
    for p in body.pairs[:200]:
        try:
            if m.merge_subjects(int(p["from"]), int(p["into"])):
                done += 1
        except (KeyError, TypeError, ValueError):
            continue
    return {"ok": True, "merged": done}


def consolidate_db(db, m) -> int:
    rank = {"empleado": 0, "invitado": 1, "revisar": 2}
    merged = 0
    for g in _same_name_groups(db):
        g.sort(key=lambda t: (rank.get(t[1], 3), -t[2], t[0]))
        for sid, _, _ in g[1:]:
            if m.merge_subjects(sid, g[0][0]):
                merged += 1
    return merged


# ---------------- galeria de una identidad: validar / mover / borrar sus miniaturas
@router.get("/api/people/{sid}/gallery")
def gallery(sid: int, request: Request, limit: int = 120, offset: int = 0):
    db = _db(request)
    names = {c.id: c.name for c in request.app.state.config.cameras}
    with db._lock:
        s = db._conn.execute("SELECT COALESCE(name,'Persona #'||id), category FROM subjects WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "sujeto inexistente")
        tot = db._conn.execute("SELECT COUNT(*) FROM person_visits WHERE subject_id=? AND fp=0", (sid,)).fetchone()[0]
        rows = db._conn.execute(
            "SELECT id, cam_id, start_ts, face IS NOT NULL, body IS NOT NULL, COALESCE(vlm_desc,''), attrs FROM person_visits "
            "WHERE subject_id=? AND fp=0 ORDER BY start_ts DESC LIMIT ? OFFSET ?", (sid, min(limit, 300), offset)).fetchall()
    return {"id": sid, "name": s[0], "category": s[1], "total": tot,
            "visits": [{"id": i, "cam": names.get(c, c), "cam_id": c, "ts": t, "face": bool(f), "body": bool(b), "desc": d[:160], "attrs": _attrs(at)} for i, c, t, f, b, d, at in rows]}


class MoveBody(BaseModel):
    ids: list
    to: Optional[int] = None          # None = identidad nueva
    name: Optional[str] = None


def _attrs(raw) -> dict:
    import json
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _recompute(db, sid: int) -> None:
    with db._lock:
        rows = db._conn.execute("SELECT embedding, n_emb FROM person_visits WHERE subject_id=? AND embedding IS NOT NULL AND n_emb>0", (sid,)).fetchall()
    acc, n = None, 0
    for blob, k in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        if v.size != 512 or not np.isfinite(v).all() or not float(np.linalg.norm(v)):
            continue
        v = v / float(np.linalg.norm(v)) * min(int(k), 5)
        acc = v if acc is None else acc + v
        n += int(k)
    with db._lock:
        if acc is None:
            db._conn.execute("UPDATE subjects SET embedding=NULL, n_emb=0 WHERE id=?", (sid,))
        else:
            acc = acc / float(np.linalg.norm(acc))
            db._conn.execute("UPDATE subjects SET embedding=?, n_emb=? WHERE id=?", (acc.astype(np.float32).tobytes(), n, sid))
        db._conn.commit()


@router.post("/api/person-visits/move")
def move_visits(body: MoveBody, request: Request):
    """Mueve miniaturas/visitas a otra identidad (o a una nueva) cuando no son de esa persona. Recalcula el rostro promedio de ambas."""
    db = _db(request)
    m = getattr(request.app.state, "visits", None)
    ids = [int(i) for i in body.ids][:500]
    if not ids:
        raise HTTPException(400, "ids vacio")
    ph = ",".join("?" * len(ids))
    with db._lock:
        src = [r[0] for r in db._conn.execute(f"SELECT DISTINCT subject_id FROM person_visits WHERE id IN ({ph}) AND subject_id IS NOT NULL", ids)]
        now = time.time()
        if body.to is None:
            to = db._conn.execute("INSERT INTO subjects (name, named, created_ts, last_ts, category) VALUES (?,?,?,?,?)",
                                  (body.name.strip() if body.name and body.name.strip() else None, 1 if body.name and body.name.strip() else 0, now, now, "revisar")).lastrowid
        else:
            to = body.to
            if not db._conn.execute("SELECT 1 FROM subjects WHERE id=?", (to,)).fetchone():
                raise HTTPException(404, "identidad destino inexistente")
        db._conn.execute(f"UPDATE person_visits SET subject_id=? WHERE id IN ({ph})", (to, *ids))
        db._conn.commit()
    for sid in set(src) | {to}:
        _recompute(db, sid)
    if m is not None:
        m.reload_subjects()
    return {"ok": True, "to": to, "moved": len(ids)}


@router.post("/api/person-visits/delete")
def delete_visits(body: MoveBody, request: Request):
    db = _db(request)
    m = getattr(request.app.state, "visits", None)
    ids = [int(i) for i in body.ids][:500]
    if not ids:
        raise HTTPException(400, "ids vacio")
    ph = ",".join("?" * len(ids))
    with db._lock:
        src = [r[0] for r in db._conn.execute(f"SELECT DISTINCT subject_id FROM person_visits WHERE id IN ({ph}) AND subject_id IS NOT NULL", ids)]
        db._conn.execute(f"DELETE FROM person_visits WHERE id IN ({ph})", ids)
        db._conn.commit()
    for sid in src:
        _recompute(db, sid)
    if m is not None:
        m.reload_subjects()
    return {"ok": True, "deleted": len(ids)}
