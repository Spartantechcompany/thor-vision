"""Presencia digital por WiFi (UniFi): huella de empleados que existe aunque la camara no los vea.

Un lector de solo lectura en oapc-devops envia cada 3 min los clientes del UDM (nombre, IP, MAC, hora de conexion). Aqui se guardan sesiones por
dispositivo; el usuario asigna cada MAC a un empleado (una sola vez) y la asistencia suma esa presencia a la de las camaras.
Solo se asignan dispositivos de empleados que firmaron el aviso de privacidad; los demas pueden marcarse como ignorados."""
import datetime
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()
GAP_S = float(os.environ.get("WIFI_GAP_S", "600"))          # un dispositivo que desaparece menos de esto sigue en la misma sesion
_SCHEMA = """
CREATE TABLE IF NOT EXISTS wifi_devices (mac TEXT PRIMARY KEY, name TEXT, ip TEXT, kind TEXT, subject_id INTEGER, ignore INTEGER NOT NULL DEFAULT 0,
  first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS wifi_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, mac TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_wifi_sess ON wifi_sessions(mac, end_ts);
"""
_ready = False


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    global _ready
    if not _ready:
        with db._lock:
            db._conn.executescript(_SCHEMA)
            db._conn.commit()
        _ready = True
    return db


def _ts(iso: str, now: float) -> float:
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        return t if now - 86400 <= t <= now else now
    except (ValueError, TypeError):
        return now


def ingest(db, clients: list, now: Optional[float] = None) -> int:
    now = now or time.time()
    n = 0
    with db._lock:
        for c in clients:
            mac = str(c.get("mac") or c.get("macAddress") or "").lower().strip()
            if len(mac) != 17 or str(c.get("type") or "WIRELESS").upper() == "WIRED":
                continue                                  # telefonos y laptops por WiFi; lo cableado no es presencia de una persona
            db._conn.execute(
                "INSERT INTO wifi_devices (mac, name, ip, kind, first_seen, last_seen) VALUES (?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET "
                "name=excluded.name, ip=excluded.ip, last_seen=excluded.last_seen",
                (mac, str(c.get("name") or "")[:80], str(c.get("ip") or c.get("ipAddress") or "")[:40], str(c.get("type") or "")[:12], now, now))
            row = db._conn.execute("SELECT id FROM wifi_sessions WHERE mac=? AND end_ts>=? ORDER BY end_ts DESC LIMIT 1", (mac, now - GAP_S)).fetchone()
            if row:
                db._conn.execute("UPDATE wifi_sessions SET end_ts=? WHERE id=?", (now, row[0]))
            else:
                db._conn.execute("INSERT INTO wifi_sessions (mac, start_ts, end_ts) VALUES (?,?,?)", (mac, _ts(c.get("connectedAt"), now), now))
            n += 1
        db._conn.commit()
    return n


class Assign(BaseModel):
    subject_id: Optional[int] = None
    ignore: Optional[bool] = None


@router.post("/api/wifi/clients")
def post_clients(request: Request, payload: dict = Body(...)):
    return {"ok": True, "n": ingest(_db(request), list(payload.get("clients") or [])[:500])}


@router.get("/api/wifi/devices")
def devices(request: Request):
    db = _db(request)
    now = time.time()
    with db._lock:
        rows = db._conn.execute(
            "SELECT d.mac, d.name, d.ip, d.subject_id, s.name, d.ignore, d.last_seen FROM wifi_devices d LEFT JOIN subjects s ON s.id=d.subject_id "
            "ORDER BY (d.subject_id IS NULL), d.last_seen DESC LIMIT 500").fetchall()
    return {"devices": [{"mac": m, "name": n, "ip": ip, "subject_id": sid, "employee": en, "ignore": bool(ig), "last_seen": ls, "online": now - (ls or 0) < GAP_S}
                        for m, n, ip, sid, en, ig, ls in rows]}


@router.get("/api/wifi/employees")
def employees(request: Request):
    db = _db(request)
    with db._lock:
        rows = db._conn.execute("SELECT id, name FROM subjects WHERE category='empleado' AND name IS NOT NULL ORDER BY name").fetchall()
    return {"employees": [{"id": i, "name": n} for i, n in rows]}


@router.put("/api/wifi/devices/{mac}")
def assign(mac: str, body: Assign, request: Request):
    db = _db(request)
    mac = mac.lower()
    with db._lock:
        if body.subject_id is not None:
            ok = db._conn.execute("SELECT 1 FROM subjects WHERE id=? AND category='empleado'", (body.subject_id,)).fetchone()
            if not ok:
                raise HTTPException(400, "solo se asignan dispositivos a personas de la base de empleados")
        sets, args = [], []
        if "subject_id" in body.model_fields_set:
            sets.append("subject_id=?")
            args.append(body.subject_id)
        if body.ignore is not None:
            sets.append("ignore=?")
            args.append(int(body.ignore))
        if not sets:
            raise HTTPException(400, "nada que cambiar")
        cur = db._conn.execute(f"UPDATE wifi_devices SET {', '.join(sets)} WHERE mac=?", (*args, mac))
        db._conn.commit()
    if not cur.rowcount:
        raise HTTPException(404, "dispositivo desconocido")
    return {"ok": True}


def day_bounds(date: Optional[str]) -> tuple:
    d = datetime.date.fromisoformat(date) if date else datetime.date.today()
    t0 = time.mktime(d.timetuple())
    return t0, t0 + 86400


def presence_by_employee(db, t0: float, t1: float) -> dict:
    """subject_id -> {first, last, minutes, devices[]} con la presencia por WiFi de sus dispositivos asignados en [t0, t1)."""
    out: dict = {}
    with db._lock:
        rows = db._conn.execute(
            "SELECT d.subject_id, d.mac, d.name, s.start_ts, s.end_ts FROM wifi_sessions s JOIN wifi_devices d ON d.mac=s.mac "
            "WHERE d.subject_id IS NOT NULL AND d.ignore=0 AND s.end_ts>=? AND s.start_ts<?", (t0, t1)).fetchall()
    for sid, mac, name, a, b in rows:
        e = out.setdefault(sid, {"segments": [], "devices": set()})
        e["segments"].append((max(a, t0), min(b, t1)))
        e["devices"].add(name or mac)
    for e in out.values():
        segs = sorted(e["segments"])
        merged: list = []
        for a, b in segs:                                 # une dispositivos del mismo empleado que se traslapan (celular + laptop)
            if merged and a <= merged[-1][1] + GAP_S:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        e.update(first=merged[0][0], last=merged[-1][1], minutes=round(sum(b - a for a, b in merged) / 60), devices=sorted(e["devices"]), segments=merged)
    return out


@router.get("/api/wifi/presence")
def presence(request: Request, date: Optional[str] = None):
    db = _db(request)
    t0, t1 = day_bounds(date)
    with db._lock:
        names = dict(db._conn.execute("SELECT id, name FROM subjects WHERE category='empleado'").fetchall())
    res = presence_by_employee(db, t0, t1)
    return {"date": datetime.date.fromtimestamp(t0).isoformat(), "employees": [
        {"subject_id": sid, "name": names.get(sid), "first": v["first"], "last": v["last"], "minutes": v["minutes"], "devices": v["devices"]}
        for sid, v in sorted(res.items(), key=lambda kv: names.get(kv[0]) or "")]}


@router.get("/api/presence/now")
def presence_now(request: Request):
    """Quien esta ahora: empleados (camara o WiFi), invitados del dia y mascotas, para el tablero /presencia."""
    from app.api.routes_people_admin import build_attendance
    db = _db(request)
    now = time.time()
    att = build_attendance(request, None)
    with db._lock:
        online = {r[0] for r in db._conn.execute(
            "SELECT DISTINCT subject_id FROM wifi_devices WHERE subject_id IS NOT NULL AND ignore=0 AND last_seen>=?", (now - GAP_S,)).fetchall()}
    emps = []
    for e in att["employees"]:
        wifi_on = e["id"] in online
        cam_on = bool(e["present"])
        status = "en oficina" if (cam_on or wifi_on) else ("salió" if e.get("left") else ("sin ver hoy" if not e.get("attended_any") else "fuera"))
        emps.append({"id": e["id"], "name": e["name"], "status": status, "by_camera": cam_on, "by_wifi": wifi_on, "first_ts": e.get("first_ts") or e.get("wifi_first_ts"),
                     "last_ts": e.get("last_ts") or e.get("wifi_last_ts"), "minutes": max(e.get("minutes") or 0, e.get("wifi_minutes") or 0), "cams": e.get("cams", []),
                     "usual_in": e.get("usual_in"), "alert": e.get("status")})
    emps.sort(key=lambda x: (x["status"] != "en oficina", x["name"] or ""))
    guests = [{**g, "now": now - (g["last_ts"] or 0) < 900} for g in att["guests"]["people"]]
    pp = getattr(request.app.state, "pet_presence", None)
    return {"ts": now, "employees": emps, "guests": guests, "pets": pp.status(now) if pp else [],
            "counts": {"in_office": sum(1 for e in emps if e["status"] == "en oficina"), "employees": len(emps), "guests_now": sum(1 for g in guests if g["now"]),
                       "pets_home": sum(1 for p in (pp.status(now) if pp else []) if p["status"] == "en casa")}}
