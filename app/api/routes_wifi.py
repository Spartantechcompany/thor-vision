"""Presencia digital por WiFi (UniFi): huella de empleados que existe aunque la camara no los vea.

Un lector de solo lectura en oapc-devops envia cada 3 min los clientes del UDM (nombre, IP, MAC, hora de conexion). Aqui se guardan sesiones por
dispositivo; el usuario asigna cada MAC a un empleado (una sola vez) y la asistencia suma esa presencia a la de las camaras.
Solo se asignan dispositivos de empleados que firmaron el aviso de privacidad; los demas pueden marcarse como ignorados."""
import datetime
import json
import logging
import os
import re
import time
import sqlite3
import unicodedata
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()
GAP_S = float(os.environ.get("WIFI_GAP_S", "600"))          # un dispositivo que desaparece menos de esto sigue en la misma sesion
_SCHEMA = """
CREATE TABLE IF NOT EXISTS wifi_devices (mac TEXT PRIMARY KEY, name TEXT, ip TEXT, kind TEXT, subject_id INTEGER, ignore INTEGER NOT NULL DEFAULT 0,
  first_seen REAL, last_seen REAL, role TEXT, suggest TEXT);
CREATE TABLE IF NOT EXISTS wifi_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, mac TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_wifi_sess ON wifi_sessions(mac, end_ts);
"""
_ready = False
ROLES = ("telefono", "laptop", "otro")
_PHONE = re.compile(r"iphone|\bios\b|android|galaxy|pixel|redmi|xiaomi|huawei|oppo|motorola|celular|phone", re.I)
_LAPTOP = re.compile(r"macbook|laptop|thinkpad|desktop|windows|surface|\bmbp\b|\bmac\b|notebook|lenovo|dell|latitude|elitebook", re.I)
_OTHER = re.compile(r"watch|alexa|apple ?tv|camara|\bcam\b|tuya|panel|foco|\btv\b|impresora|printer|echo|kl\d{3}|lwip|wlan0|smart", re.I)
_GENERIC = re.compile(r"(^|\s)([0-9a-f]{2}:[0-9a-f]{2})$|^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.I)       # nombres por defecto: 'iPhone 49:56', 'wlan0 a6:1f'


def guess_role(name: str) -> str:
    """Telefono / laptop / otro segun el nombre del cliente en UniFi (solo una pista; el usuario la corrige)."""
    n = name or ""
    if _OTHER.search(n):
        return "otro"
    if _PHONE.search(n):
        return "telefono"
    if _LAPTOP.search(n):
        return "laptop"
    return "otro"


def norm_name(name: str) -> str:
    """Nombre sin acentos ni mayusculas; vacio si es un nombre generico con la cola de la MAC (no identifica a nadie)."""
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower().strip()
    return "" if (not n or _GENERIC.search(n)) else re.sub(r"\s+", " ", n)


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    global _ready
    if not _ready:
        with db._lock:
            db._conn.executescript(_SCHEMA)
            for col in ("role TEXT", "suggest TEXT"):
                try:
                    db._conn.execute(f"ALTER TABLE wifi_devices ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass                                      # la columna ya existe
            db._conn.commit()
        _ready = True
    return db


def _ts(iso: str, now: float) -> float:
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        return t if now - 86400 <= t <= now else now
    except (ValueError, TypeError):
        return now


def _classify_device(db, mac: str, name: str) -> None:
    """Rol por nombre y, si el equipo cambio de MAC (direccion privada) pero conserva su nombre, hereda al empleado y el rol del que ya se asigno."""
    sid, role = db._conn.execute("SELECT subject_id, role FROM wifi_devices WHERE mac=?", (mac,)).fetchone()
    new_role = role or guess_role(name)
    new_sid = sid
    key = norm_name(name)
    if sid is None and key:
        for osid, oname, orole in db._conn.execute("SELECT subject_id, name, role FROM wifi_devices WHERE subject_id IS NOT NULL AND mac!=?", (mac,)).fetchall():
            if norm_name(oname) == key:
                new_sid, new_role = osid, role or orole or new_role
                break
    if (new_sid, new_role) != (sid, role):
        db._conn.execute("UPDATE wifi_devices SET subject_id=?, role=? WHERE mac=?", (new_sid, new_role, mac))


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
            _classify_device(db, mac, str(c.get("name") or ""))
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
    role: Optional[str] = None


@router.post("/api/wifi/clients")
def post_clients(request: Request, payload: dict = Body(...)):
    return {"ok": True, "n": ingest(_db(request), list(payload.get("clients") or [])[:500])}


@router.get("/api/wifi/devices")
def devices(request: Request):
    db = _db(request)
    now = time.time()
    with db._lock:
        rows = db._conn.execute(
            "SELECT d.mac, d.name, d.ip, d.subject_id, s.name, d.ignore, d.last_seen, d.role, d.suggest FROM wifi_devices d LEFT JOIN subjects s ON s.id=d.subject_id "
            "ORDER BY (d.subject_id IS NULL), d.last_seen DESC LIMIT 500").fetchall()
    out = []
    for m, n, ip, sid, en, ig, ls, role, sug in rows:
        try:
            sg = json.loads(sug) if sug else None
        except ValueError:
            sg = None
        out.append({"mac": m, "name": n, "ip": ip, "subject_id": sid, "employee": en, "ignore": bool(ig), "last_seen": ls, "online": now - (ls or 0) < GAP_S,
                    "role": role or guess_role(n), "suggest": sg})
    return {"devices": out}


KIND_Q = {"type": "choice", "instructions": "What kind of device is this, judging only by its network name?", "criteria": {
    "phone": "A smartphone (iPhone, iOS, Android, Galaxy, Pixel...)", "laptop": "A laptop or desktop computer (MacBook, Windows PC...)",
    "tablet": "A tablet (iPad...)", "other": "A watch, TV, speaker, camera, light, printer, access point or any other non-personal device"}}


def suggest_owners(db, clef, limit: int = 60, now: Optional[float] = None) -> int:
    """Clef lee el nombre de cada equipo sin duenio y sugiere de que tipo es y de que empleado parece; solo guarda la sugerencia (nunca asigna solo)."""
    now = now or time.time()
    with db._lock:
        emps = db._conn.execute("SELECT id, name FROM subjects WHERE category='empleado' AND name IS NOT NULL AND TRIM(name)!='' ORDER BY name").fetchall()
        cands = db._conn.execute("SELECT mac, name FROM wifi_devices WHERE subject_id IS NULL AND ignore=0 AND last_seen>? ORDER BY last_seen DESC LIMIT ?",
                                 (now - 14 * 86400, limit * 3)).fetchall()
    if not emps:
        return 0
    owner_q = {"type": "choice", "instructions": "Whose personal device is this, judging only by its network name? Choose 'none' if the name does not point to one of the listed people.",
               "criteria": {**{f"e{i}": f"{n} (employee)" for i, n in emps}, "none": "No listed person, or the name does not say"}}
    n = 0
    for mac, name in [c for c in cands if norm_name(c[1])][:limit]:
        out = clef.decide({"wifi_client_name": name, "context": "Client names come from a company WiFi network"}, [], {"kind": KIND_Q, "owner": owner_q})
        if not out:
            continue
        try:
            a = out["answers"]
            kind, owner = a["kind"]["choice"], a["owner"]["choice"]
            p_owner = float((a["owner"].get("probabilities") or {}).get(owner, 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        sug = {"kind": kind, "owner_id": int(owner[1:]) if owner.startswith("e") and owner[1:].isdigit() else None, "p": round(p_owner, 2)}
        with db._lock:
            db._conn.execute("UPDATE wifi_devices SET suggest=? WHERE mac=? AND subject_id IS NULL", (json.dumps(sug), mac))
            db._conn.commit()
        n += 1
    return n


@router.post("/api/wifi/suggest")
def suggest(request: Request):
    clef = getattr(request.app.state, "clef", None)
    if clef is None:
        raise HTTPException(503, "Clef no esta activo")
    return {"ok": True, "suggested": suggest_owners(_db(request), clef)}


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
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, f"rol invalido; usa uno de {list(ROLES)}")
            sets.append("role=?")
            args.append(body.role)
        if "subject_id" in body.model_fields_set:
            sets.append("suggest=NULL")                       # ya se decidio: la sugerencia deja de aplicar
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
            "SELECT d.subject_id, d.mac, d.name, s.start_ts, s.end_ts, d.role FROM wifi_sessions s JOIN wifi_devices d ON d.mac=s.mac "
            "WHERE d.subject_id IS NOT NULL AND d.ignore=0 AND s.end_ts>=? AND s.start_ts<?", (t0, t1)).fetchall()
        has_phone = {r[0] for r in db._conn.execute("SELECT subject_id FROM wifi_devices WHERE subject_id IS NOT NULL AND ignore=0 AND role='telefono'").fetchall()}
    for sid, mac, name, a, b, role in rows:
        if sid in has_phone and role != "telefono":
            continue
        e = out.setdefault(sid, {"segments": [], "devices": set()})
        e["segments"].append((max(a, t0), min(b, t1)))
        e["devices"].add(name or mac)
    for e in out.values():
        segs = sorted(e["segments"])
        merged: list = []
        for a, b in segs:                                 # une dispositivos del mismo empleado que se traslapan
            if merged and a <= merged[-1][1] + GAP_S:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        e.update(first=merged[0][0], last=merged[-1][1], minutes=round(sum(b - a for a, b in merged) / 60), devices=sorted(e["devices"]), segments=merged)
    return out


def online_employees(db, now: float) -> set:
    """Empleados con un dispositivo conectado ahora que cuenta como presencia: su telefono; si no tiene uno asignado, cualquiera de sus equipos.
    Una laptop olvidada en la oficina no cuenta mientras su telefono este fuera."""
    with db._lock:
        rows = db._conn.execute("SELECT subject_id, role, last_seen FROM wifi_devices WHERE subject_id IS NOT NULL AND ignore=0").fetchall()
    has_phone = {sid for sid, role, _ in rows if role == "telefono"}
    return {sid for sid, role, seen in rows if now - (seen or 0) < GAP_S and (role == "telefono" or sid not in has_phone)}


@router.get("/api/presence/now")
def presence_now(request: Request):
    """Quien esta ahora: empleados (camara o WiFi), invitados del dia y mascotas, para el tablero /presencia."""
    from app.api.routes_people_admin import build_attendance
    db = _db(request)
    now = time.time()
    att = build_attendance(request, None)
    online = online_employees(db, now)
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
