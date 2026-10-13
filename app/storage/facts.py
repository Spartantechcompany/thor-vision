"""Hechos exactos de un periodo, calculados en SQL: fuente unica para reportes, chat y su evaluacion."""
import json
import logging
import time
from collections import Counter, defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_ALERT_ROWS = 20000
_EXAMPLES_PER_TYPE = 3


def _hhmm(ts: float) -> str:
    return time.strftime("%d-%b %H:%M", time.localtime(ts))


def _cam_names() -> dict:
    try:
        from app.config import load_config
        return {c.id: c.name for c in load_config().cameras}
    except Exception:
        return {}


def _scene_types() -> tuple[dict, dict]:
    try:
        from app.vision.scenes import Scenes
        sc = Scenes()
        cams = {cid: sorted(ctx.alert_types) for cid, ctx in sc._by_cam.items()}
        catalog = next(iter(sc._by_cam.values())).catalog if sc._by_cam else {}
        return cams, dict(catalog)
    except Exception:
        return {}, {}


def build_facts(db, since: float, until: Optional[float] = None) -> dict:
    until = time.time() if until is None else until
    names = _cam_names()
    cam_types, catalog = _scene_types()
    conn, lock = db._conn, db._lock
    with lock:
        q = lambda sql, *a: conn.execute(sql, a).fetchall()
        tot = q("SELECT COUNT(*), COALESCE(SUM(has_alert),0), COALESCE(MAX(people),0) "
                "FROM events WHERE type='nemotron' AND ts>=? AND ts<?", since, until)[0]
        prev = q("SELECT COUNT(*), COALESCE(SUM(has_alert),0) FROM events "
                 "WHERE type='nemotron' AND ts>=? AND ts<?", since - (until - since), since)[0]
        review = dict(q("SELECT COALESCE(review_label,'sin_revisar'), COUNT(*) FROM events "
                        "WHERE type='nemotron' AND has_alert=1 AND ts>=? AND ts<? GROUP BY 1", since, until))
        per_cam = q("SELECT cam_id, COUNT(*), COALESCE(SUM(has_alert),0), COALESCE(MAX(people),0) "
                    "FROM events WHERE type='nemotron' AND ts>=? AND ts<? GROUP BY cam_id "
                    "ORDER BY 2 DESC", since, until)
        alert_rows = q("SELECT id, ts, cam_id, data, review_label FROM events WHERE type='nemotron' "
                       "AND has_alert=1 AND ts>=? AND ts<? ORDER BY ts DESC LIMIT ?",
                       since, until, _MAX_ALERT_ROWS)
        vis = q("SELECT cam_id, COUNT(*), COUNT(DISTINCT subject_id), "
                "COALESCE(SUM(subject_id IS NULL),0), AVG(end_ts-start_ts) FROM person_visits "
                "WHERE static=0 AND fp=0 AND start_ts>=? AND start_ts<? GROUP BY cam_id ORDER BY 2 DESC",
                since, until)
        subj = q("SELECT v.subject_id, COALESCE(s.name,'Persona #'||v.subject_id), COALESCE(s.named,0), "
                 "COUNT(*), group_concat(DISTINCT v.cam_id), MIN(v.start_ts), MAX(v.end_ts) "
                 "FROM person_visits v LEFT JOIN subjects s ON s.id=v.subject_id "
                 "WHERE v.static=0 AND v.fp=0 AND v.subject_id IS NOT NULL AND v.start_ts>=? AND v.start_ts<? "
                 "GROUP BY v.subject_id ORDER BY 4 DESC", since, until)
        first_ev = q("SELECT MIN(ts) FROM events WHERE type='nemotron'")[0][0]
        first_vis = q("SELECT MIN(start_ts) FROM person_visits")[0][0]

    by_type, by_cam_type, examples = Counter(), defaultdict(Counter), defaultdict(list)
    for eid, ts, cam, data, label in alert_rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            d = {}
        types = [t for t in (d.get("alert_types") or []) if t] or ["sin_tipo"]
        for t in types:
            by_type[t] += 1
            by_cam_type[cam][t] += 1
            if len(examples[t]) < _EXAMPLES_PER_TYPE:
                examples[t].append({"id": eid, "hora": _hhmm(ts), "cam": names.get(cam, cam),
                                    "texto": (d.get("activity") or "")[:180],
                                    "severidad": d.get("severity"), "revision": label})

    return {
        "window": {"since": since, "until": until, "hours": round((until - since) / 3600, 1),
                   "desde": _hhmm(since), "hasta": _hhmm(until)},
        "data_from": {"eventos": _hhmm(first_ev) if first_ev else None,
                      "visitas": _hhmm(first_vis) if first_vis else None,
                      "visitas_cubren_periodo": bool(first_vis and first_vis <= since)},
        "totals": {"observaciones": tot[0], "alertas": tot[1], "max_personas": tot[2],
                   "alertas_importantes": review.get("important", 0), "alertas_ruido": review.get("noise", 0),
                   "alertas_sin_revisar": review.get("sin_revisar", 0)},
        "prev": {"observaciones": prev[0], "alertas": prev[1]},
        "cams": [{"id": c, "nombre": names.get(c, c), "observaciones": n, "alertas": a, "max_personas": mp,
                  "alertas_por_tipo": dict(by_cam_type[c].most_common())} for c, n, a, mp in per_cam],
        "alert_types": dict(by_type.most_common()),
        "examples": dict(examples),
        "visits": {"total": sum(r[1] for r in vis), "sin_identidad": sum(r[3] for r in vis),
                   "por_camara": [{"nombre": names.get(c, c), "visitas": n, "sujetos": s, "sin_identidad": u,
                                   "duracion_media_s": round(d or 0)} for c, n, s, u, d in vis]},
        "subjects": _subjects(subj, names),
        "config": {"catalogo": catalog,
                   "tipos_por_camara": {names.get(c, c): t for c, t in cam_types.items()}},
    }


def _subjects(subj, names) -> dict:
    named = {}
    for _, nm, is_named, n, cams, f, l in subj:
        if not is_named:
            continue
        key = nm.strip().lower()
        e = named.setdefault(key, {"nombre": nm.strip(), "visitas": 0, "camaras": set(), "primera": f, "ultima": l})
        e["visitas"] += n
        e["camaras"].update(names.get(x, x) for x in (cams or "").split(",") if x)
        e["primera"], e["ultima"] = min(e["primera"], f), max(e["ultima"], l)
    out = sorted(named.values(), key=lambda e: -e["visitas"])
    for e in out:
        e["camaras"] = sorted(e["camaras"]); e["primera"], e["ultima"] = _hhmm(e["primera"]), _hhmm(e["ultima"])
    return {"distintos": len(subj),
            "nombrados": out,
            "recurrentes_sin_nombre": [{"nombre": nm, "visitas": n, "camaras": [names.get(x, x) for x in (cams or "").split(",") if x]}
                                       for _, nm, is_named, n, cams, f, l in subj if not is_named and n >= 3][:8]}



def render_facts(f: dict) -> str:
    t, w = f["totals"], f["window"]
    L = [f"PERIODO: {w['desde']} a {w['hasta']} ({w['hours']} h, hora local).",
         f"DATOS DISPONIBLES: eventos desde {f['data_from']['eventos']}; visitas de personas desde {f['data_from']['visitas']}"
         + ("" if f["data_from"]["visitas_cubren_periodo"] else " (las visitas NO cubren todo el periodo)") + ".",
         "",
         "TOTALES (exactos):",
         f"- Observaciones: {t['observaciones']} (periodo anterior equivalente: {f['prev']['observaciones']})",
         f"- Alertas: {t['alertas']} (periodo anterior: {f['prev']['alertas']})",
         f"- Revision de alertas: {t['alertas_importantes']} importantes, {t['alertas_ruido']} ruido, {t['alertas_sin_revisar']} sin revisar",
         f"- Maximo de personas simultaneas en una camara: {t['max_personas']}",
         "",
         "POR CAMARA (observaciones / alertas / max personas / alertas por tipo):"]
    for c in f["cams"]:
        tipos = ", ".join(f"{k} {v}" for k, v in c["alertas_por_tipo"].items()) or "-"
        L.append(f"- {c['nombre']}: {c['observaciones']} / {c['alertas']} / {c['max_personas']} / {tipos}")
    L += ["", "ALERTAS POR TIPO (codigo: total):"]
    L += [f"- {k}: {v}" for k, v in f["alert_types"].items()] or ["- ninguna"]
    L += ["", "EJEMPLOS POR TIPO (mas recientes):"]
    for k, exs in f["examples"].items():
        for e in exs:
            rv = f" [revisada: {e['revision']}]" if e["revision"] else ""
            L.append(f"- {k} | {e['hora']} | {e['cam']} | {e['texto']}{rv}")
    v, s = f["visits"], f["subjects"]
    L += ["", "PERSONAS (visitas = una persona seguida de entrada a salida en una camara):",
          f"- Visitas: {v['total']} ({v['sin_identidad']} sin identidad por falta de rostro)",
          f"- Sujetos distintos identificados por rostro: {s['distintos']}"]
    for c in v["por_camara"]:
        L.append(f"- {c['nombre']}: {c['visitas']} visitas, {c['sujetos']} sujetos, {c['sin_identidad']} sin identidad, "
                 f"duracion media {c['duracion_media_s']} s")
    if s["nombrados"]:
        L.append("- Personas con nombre vistas:")
        L += [f"  - {p['nombre']}: {p['visitas']} visitas en {', '.join(p['camaras'])} (primera {p['primera']}, ultima {p['ultima']})"
              for p in s["nombrados"]]
    if s["recurrentes_sin_nombre"]:
        L.append("- Recurrentes sin nombre (3+ visitas):")
        L += [f"  - {p['nombre']}: {p['visitas']} visitas en {', '.join(p['camaras'])}" for p in s["recurrentes_sin_nombre"]]
    L += ["", "CONFIGURACION: tipos de alerta habilitados por camara (fueron configurados a proposito como relevantes):"]
    L += [f"- {cam}: {', '.join(ts)}" for cam, ts in f["config"]["tipos_por_camara"].items()]
    L += ["Catalogo de tipos:"] + [f"- {k}: {d}" for k, d in f["config"]["catalogo"].items()]
    return "\n".join(L)
