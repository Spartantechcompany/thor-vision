"""Linea de tiempo de alertas: eventos con tipo y captura para el carril general y uno por camara."""
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Request

router = APIRouter()

_MAX_EVENTS = 4000
_MAX_SPAN_S = 8 * 86400


@router.get("/api/timeline")
def timeline(request: Request, since: Optional[float] = None, until: Optional[float] = None, cam_id: Optional[str] = None):
    until = until or time.time()
    since = since or until - 86400
    since = max(since, until - _MAX_SPAN_S)
    db = getattr(request.app.state, "db", None)
    config = request.app.state.config
    cams = [{"id": c.id, "name": c.name, "zone": c.zone} for c in config.cameras]
    if db is None:
        return {"since": since, "until": until, "cams": cams, "events": [], "truncated": False}
    SEL = ("SELECT e.id, e.ts, e.cam_id, e.people, e.data, e.review_label, "
           "(SELECT s.id FROM snapshots s WHERE s.event_id = e.id ORDER BY s.id DESC LIMIT 1) FROM events e ")
    WH = "WHERE e.type='nemotron' AND e.has_alert=1 AND e.ts>=? AND e.ts<=? "
    group_n: dict = {}
    with db._lock:
        total = db._conn.execute("SELECT COUNT(*) FROM events e " + WH, (since, until)).fetchone()[0]
        if total <= _MAX_EVENTS:
            rows = db._conn.execute(SEL + WH + "ORDER BY e.ts DESC LIMIT ?", (since, until, _MAX_EVENTS + 1)).fetchall()
        else:
            # ventana muy ancha: en vez de quedarse con los ultimos N eventos (y dejar vacio lo mas viejo), se agrupa por camara, tipo y tramo de tiempo
            # y se devuelve un evento representativo de cada grupo con su cantidad (n)
            bucket = max(60.0, (until - since) / 500)
            grp = db._conn.execute("SELECT MIN(e.id), COUNT(*) FROM events e " + WH +
                                   "GROUP BY e.cam_id, json_extract(e.data,'$.alert_types[0]'), CAST((e.ts-?)/? AS INTEGER)", (since, until, since, bucket)).fetchall()
            group_n = {i: n for i, n in grp}
            ids = list(group_n)
            rows = []
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                rows += db._conn.execute(SEL + f"WHERE e.id IN ({','.join('?' * len(chunk))}) ORDER BY e.ts DESC", chunk).fetchall()
    truncated = total > _MAX_EVENTS
    events = []
    _iot = getattr(request.app.state, "iot", None)
    iot_alias = {i: d.get("alias") for i, d in (_iot.devices.items() if _iot is not None else []) if d.get("alias")}
    for eid, ts, cam, people, data, label, sid in (rows if group_n else rows[:_MAX_EVENTS]):
        try:
            d = json.loads(data or "{}")
        except ValueError:
            d = {}
        if d.get("source") == "iot":                  # las alertas de sensores y camaras Tuya van a su propio carril, no al de la camara de captura
            cam = (iot_alias.get((d.get("device_id") or (d.get("iot") or {}).get("device_id"))) or cam)
        events.append({"id": eid, "ts": ts, "cam": cam, "people": people or 0,
                       "type": _effective_type(d),
                       "sev": d.get("severity") or "", "text": (d.get("activity") or "")[:160],
                       "review": label, "img": f"/api/snapshots/file/{sid}" if sid else None, **({"n": group_n[eid]} if group_n.get(eid, 1) > 1 else {})})
    _fill_ring_images(db, events)
    _mark_known_night(db, events)
    events += _street_traffic(db, config, since, until)
    iot = getattr(request.app.state, "iot", None)
    rel = set()
    if cam_id and iot is not None:            # en la vista de una camara tambien salen los dispositivos Tuya relacionados (sensores y camaras de esa zona)
        rel = {d["alias"] for d in iot.devices.values() if d.get("alias") and (cam_id in (d.get("related") or []) or d.get("cam") == cam_id)}
    if cam_id:
        events = [e for e in events if e["cam"] == cam_id or e["cam"] in rel]
        cams = [c for c in cams if c["id"] == cam_id]
    events.sort(key=lambda e: e["ts"])
    if iot is not None:
        cams += [{"id": d["alias"], "name": d["name"], "zone": "tuya"} for d in iot.devices.values() if d.get("alias") and (not cam_id or d["alias"] == cam_id or d["alias"] in rel)]
    return {"since": since, "until": until, "cams": cams, "events": events, "truncated": truncated, "total": total, "thinned": bool(group_n)}


def _fill_ring_images(db, events: list) -> None:
    """Timbres viejos que quedaron sin captura: se usa la persona que el videoportero registro a menos de 90 s del timbre."""
    for e in events:
        if e["type"] != "timbre_videoportero" or e.get("img"):
            continue
        with db._lock:
            row = db._conn.execute(
                "SELECT id, scene IS NOT NULL, face IS NOT NULL FROM person_visits WHERE cam_id=? AND start_ts BETWEEN ? AND ? AND (face IS NOT NULL OR body IS NOT NULL) "
                "ORDER BY ABS(start_ts - ?) LIMIT 1", (e["cam"], e["ts"] - 90, e["ts"] + 90, e["ts"])).fetchone()
        if row:
            e["img"] = f"/api/person-visits/{row[0]}/{'scene' if row[1] else ('face' if row[2] else 'body')}"
            e["text"] = (e["text"] + " (captura del visitante cercano)")[:200]


def _mark_known_night(db, events: list) -> None:
    """'Persona nocturna' cuya visita cercana (+-15 s, misma camara) ya es un empleado con nombre: advertencia amarilla, no alerta roja."""
    from app.vision import arming
    cand = [e for e in events if e["type"] == "persona_nocturna"]
    if not cand or arming.is_absolute():
        return
    with db._lock:
        rows = db._conn.execute("SELECT v.cam_id, v.start_ts, v.end_ts FROM person_visits v JOIN subjects s ON s.id=v.subject_id "
                                "WHERE s.named=1 AND s.category='empleado' AND v.fp=0 AND v.end_ts>=? AND v.start_ts<=?",
                                (min(e["ts"] for e in cand) - 15, max(e["ts"] for e in cand) + 15)).fetchall()
    for e in cand:
        if any(c == e["cam"] and a - 15 <= e["ts"] <= b + 15 for c, a, b in rows):
            e["type"] = "persona_conocida"


def _effective_type(d: dict) -> str:
    """Tipo para la linea de tiempo: separa el sensor de la deteccion por camara y el transito del videoportero de las visitas reales."""
    t = (d.get("alert_types") or ["sin_tipo"])[0]
    src = d.get("source")
    if src == "iot" and t == "puerta_abierta":
        return "sensor_puerta"
    if src == "doorbell" and t == "visita_videoportero":
        db_ = d.get("doorbell") or {}
        if (db_.get("duration_s") or 0) < 15 and "(" not in (d.get("activity") or ""):
            return "trafico_calle"
    return t


def _street_traffic(db, config, since, until) -> list:
    """Personas que pasan por las camaras de la calle (y el videoportero): quedan como registro en la linea de tiempo, no en la ocupacion interior."""
    door = [c.strip() for c in os.environ.get("DOORBELL_CAMS", "cam-vto").split(",") if c.strip()]
    ext = [c.id for c in config.cameras if getattr(c, "zone", "") == "exterior"] + [c for c in door if any(x.id == c for x in config.cameras)]
    if not ext:
        return []
    with db._lock:
        rows = db._conn.execute(
            "SELECT v.id, v.start_ts, v.cam_id, COALESCE(s.name, 'Persona #'||s.id), s.named, v.vlm_desc, v.attrs, v.scene IS NOT NULL FROM person_visits v LEFT JOIN subjects s ON s.id=v.subject_id "
            f"WHERE v.fp=0 AND v.static=0 AND v.hits>=6 AND v.start_ts>=? AND v.start_ts<=? AND v.cam_id IN ({','.join('?' * len(ext))}) "
            "AND (s.named=1 OR v.end_ts - v.start_ts >= 8 OR v.cam_id IN ('cam-vto')) ORDER BY v.start_ts DESC LIMIT 3000", (since, until, *ext)).fetchall()
    out = []
    for vid, ts, cam, name, named, desc, at, has_scene in rows:
        who = name if named else "Persona sin identificar"
        try:
            a = json.loads(at) if at else {}
        except ValueError:
            a = {}
        cl = ", ".join(f"{k} {a[v]}" for k, v in (("arriba", "upper"), ("abajo", "lower")) if a.get(v))
        g = a.get("guess")
        jy = a.get("journey")
        extra = ((f" · ropa: {cl}" if cl else "") + (f" · ¿{g['name']}? {round(g['sim'] * 100)}%" if g and not named else "")
                 + (f" · probable {jy['name']} (por su recorrido)" if jy and not named else ""))
        out.append({"id": f"v{vid}", "ts": ts, "cam": cam, "people": 1, "type": "trafico_calle", "sev": "none",
                    "text": (f"{who} en la calle" + (f": {desc}" if desc else "") + extra)[:220], "review": None,
                    "img": f"/api/person-visits/{vid}/{'scene' if has_scene else 'body'}"})
    return out



@router.get("/api/correlation")
def correlation(request: Request, ts: Optional[float] = None, event_id: Optional[int] = None, before: int = 120, after: int = 300):
    """Todo lo que paso alrededor de un momento (o de una alerta): timbre, visitas con o sin nombre, alertas, puertas y camaras Tuya, en orden."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"items": []}
    names = {c.id: c.name for c in request.app.state.config.cameras}
    with db._lock:
        if event_id is not None:
            row = db._conn.execute("SELECT ts FROM events WHERE id=?", (event_id,)).fetchone()
            ts = row[0] if row else ts
    if ts is None:
        return {"items": [], "note": "indica ts o event_id"}
    t0, t1 = ts - min(before, 1800), ts + min(after, 3600)
    items = []
    with db._lock:
        c = db._conn
        for eid, t, cam, data in c.execute("SELECT id, ts, cam_id, data FROM events WHERE type='nemotron' AND has_alert=1 AND ts BETWEEN ? AND ?", (t0, t1)):
            try:
                d = json.loads(data or "{}")
            except ValueError:
                d = {}
            items.append({"ts": t, "kind": "alerta", "cam": names.get(cam, cam), "text": d.get("activity") or "", "type": (d.get("alert_types") or [""])[0], "event_id": eid})
        for vid, a, b, cam, nm, named in c.execute(
                "SELECT v.id, v.start_ts, v.end_ts, v.cam_id, COALESCE(s.name,'Persona #'||s.id), s.named FROM person_visits v LEFT JOIN subjects s ON s.id=v.subject_id "
                "WHERE v.fp=0 AND v.static=0 AND v.start_ts BETWEEN ? AND ?", (t0, t1)):
            items.append({"ts": a, "kind": "persona", "cam": names.get(cam, cam), "text": (nm if named else "persona sin identificar") + f" ({int(b - a)} s)", "visit_id": vid,
                          "img": f"/api/person-visits/{vid}/body"})
        for t, ep, topic, state in c.execute("SELECT ts, endpoint, topic, state FROM onvif_events WHERE topic LIKE 'dahua/%' AND ts BETWEEN ? AND ?", (t0, t1)):
            items.append({"ts": t, "kind": "videoportero", "cam": "Videoportero", "text": topic.split("/", 1)[1] + (f" ({state})" if state else "")})
        for t, name, kind, code, val in c.execute("SELECT ts, name, kind, code, value FROM iot_events WHERE source!='sync' AND ts BETWEEN ? AND ? "
                                                  "AND code IN ('doorcontact_state','cerrada','ipc_motion','ipc_bang')", (t0, t1)):
            items.append({"ts": t, "kind": "sensor", "cam": name, "text": f"{code} = {val}"[:80]})
    items.sort(key=lambda i: i["ts"])
    return {"ts": ts, "window": [t0, t1], "items": items[:300]}
