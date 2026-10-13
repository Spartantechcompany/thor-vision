"""Endpoint compatible con la API JSON de SearXNG: Morphic busca aqui en vez de la web.

Cada busqueda en texto libre se interpreta como periodo + camaras + tipos + personas y devuelve:
un resultado con los hechos exactos del periodo, uno por grupo de alertas, uno por persona con
nombre, y las capturas de las alertas como resultados de imagen.
"""
import re
import time

from fastapi import APIRouter, Request

from app.api import routes_chat as rc
from app.api.chat_agent import _clock_range, _evidence, _range_label

import logging
import sqlite3
logger = logging.getLogger(__name__)
router = APIRouter()

_TYPE_WORDS = [
    (r"merode", ["merodeo"]),
    (r"tel[eé]fono|celular", ["uso_telefono_exterior"]),
    (r"animal|perro|mascota|gato", ["animal"]),
    (r"acceso|puerta|entrada no", ["acceso_no_autorizado", "puerta_abierta"]),
    (r"veh[ií]culo|auto|carro|coche|moto", ["manipulacion_vehiculo", "vehiculo_detenido"]),
    (r"noct|fuera de horario|de noche|madrugada", ["persona_nocturna"]),
    (r"suelo|ca[ií]d|tirad|desmay", ["persona_en_suelo"]),
    (r"fuego|humo|incendio", ["fuego_humo"]),
]


def _named_subjects(db) -> list:
    with db._lock:
        rows = db._conn.execute("SELECT DISTINCT name FROM subjects WHERE named=1 AND name IS NOT NULL").fetchall()
    return sorted({(n or "").strip() for (n,) in rows if n and n.strip()})


def _person_visits(db, names: dict, person: str, since: float, until: float) -> list:
    with db._lock:
        rows = db._conn.execute(
            "SELECT v.cam_id, v.start_ts, v.end_ts FROM person_visits v JOIN subjects s ON s.id = v.subject_id "
            "WHERE lower(trim(s.name)) = lower(?) AND v.fp = 0 AND v.start_ts >= ? AND v.start_ts <= ? "
            "ORDER BY v.start_ts", (person, since, until)).fetchall()
    out, cur = [], None
    for cam, a, b in rows:
        if cur and cur["cam"] == cam and a - cur["b"] <= 300:
            cur["b"], cur["n"] = max(cur["b"], b or a), cur["n"] + 1
        else:
            cur = {"cam": names.get(cam, cam), "a": a, "b": b or a, "n": 1}
            out.append(cur)
    return out


def _clip(lines, limit=580):
    out, n = [], 0
    for ln in lines:
        if n + len(ln) + 1 > limit:
            break
        out.append(ln)
        n += len(ln) + 1
    return "\n".join(out)


def _fact_chunks(f: dict, label: str) -> list:
    """Hechos exactos en bloques cortos: Morphic recorta cada resultado a 600 caracteres."""
    t, pv, v, sj = f["totals"], f["prev"], f["visits"], f["subjects"]
    chunks = [(f"Totales exactos {label}", _clip([
        f"Periodo {label}. Observaciones {t['observaciones']} (periodo anterior {pv['observaciones']}).",
        f"Alertas {t['alertas']} (periodo anterior {pv['alertas']}).",
        f"Revisión: {t['alertas_importantes']} importantes, {t['alertas_ruido']} ruido, {t['alertas_sin_revisar']} sin revisar.",
        f"Máximo de personas simultáneas en una cámara: {t['max_personas']}.",
        f"Cámaras del sistema: {', '.join(str(c.get('nombre') or c.get('id') or '?') for c in f['cams'])}." if f["cams"] else "",
    ]))]
    cam_lines = [f"{c.get('nombre') or c.get('id')}: {c['alertas']} alertas ({', '.join(f'{k} {n}' for k, n in c['alertas_por_tipo'].items()) or 'ninguna'}); "
                 f"{c['observaciones']} obs; máx {c['max_personas']} pers." for c in f["cams"]]
    i = 0
    while i < len(cam_lines):
        part = _clip(cam_lines[i:])
        k = part.count("\n") + 1
        chunks.append((f"Alertas por cámara {label}" + (f" ({len(chunks)})" if i else ""), part))
        i += k
    chunks.append((f"Alertas por tipo {label}", _clip([f"{k}: {n}" for k, n in f["alert_types"].items()] or ["sin alertas"])))
    ppl = [f"Visitas (tramos de seguimiento, NO personas distintas): {v['total']}; sin identidad: {v['sin_identidad']}.",
           f"Personas distintas identificadas por rostro: {sj['distintos']}."]
    ppl += [f"{p['nombre']}: {p['visitas']} detecciones en {', '.join(p['camaras'])} (primera {p['primera']}, última {p['ultima']})." for p in sj["nombrados"]]
    chunks.append((f"Personas identificadas {label}", _clip(ppl)))
    if sj["recurrentes_sin_nombre"]:
        chunks.append((f"Personas recurrentes sin nombre {label}", _clip(
            [f"{p['nombre']}: {p['visitas']} visitas en {', '.join(p['camaras'])}." for p in sj["recurrentes_sin_nombre"]])))
    return [(a, b) for a, b in chunks if b]


@router.get("/searxng/search")
def searxng_search(request: Request, q: str = "", format: str = "json"):
    app = request.app
    db = getattr(app.state, "db", None)
    config = app.state.config
    names = {c.id: c.name for c in config.cameras}
    base = str(request.base_url).rstrip("/")
    query = (q or "").strip()
    now = time.time()
    if db is None:
        return {"query": query, "number_of_results": 0, "results": []}

    window = _clock_range(query, now)
    if window is None:
        hours = rc._explicit_hours(query) or rc._infer_history_hours(query)
        window = (now - hours * 3600, now)
    since, until = window
    nq = rc._norm_txt(query)
    cams = [cid for cid, nm in names.items() if rc._norm_txt(nm) in nq]
    if not cams and "exterior" in nq:
        cams = [cid for cid, nm in names.items() if "exterior" in rc._norm_txt(nm)]
    types = [t for rx, ts in _TYPE_WORDS if re.search(rx, nq) for t in ts]
    people = [p for p in _named_subjects(db) if re.search(r"\b" + re.escape(rc._norm_txt(p)) + r"\b", nq)]
    label = _range_label(since, until)

    results = []
    try:
        from app.api import routes_knowledge as kn
        from app.storage import textindex as _ti
        qs = set(_ti.query_stems(query))
        asks_pets = bool(re.search(r"perr|mascot|gigi|akamaru|mojo|shiba|shar", nq))
        for title, text in kn.sections(request):
            ts = set(_ti.stems(title + " " + text))
            if (asks_pets and "mascot" in title.lower()) or (qs & ts and len(qs & ts) >= 2):
                results.append({"title": f"Conocimiento de la casa · {title}", "url": f"{base}/conocimiento", "content": re.sub(r"\s+", " ", text.replace("## ", ""))[:556]})
    except (ImportError, OSError) as exc:
        logger.warning("conocimiento: %s", exc)
    from app.storage.facts import build_facts
    f = build_facts(db, since, until)
    purl = f"{base}/?periodo={int(since)}-{int(until)}"
    for title, content in _fact_chunks(f, label):
        results.append({"title": title, "url": purl, "content": content})

    # busqueda por descripcion (ej. "persona con bolsa naranja"): indice de texto de las descripciones del VLM
    try:
        from app.storage import embindex
        explicit = rc._explicit_hours(query) is not None or _clock_range(query, now) is not None
        t_since = since if explicit else now - 7 * 86400
        hits = embindex.hybrid_events(db, query, t_since, until if explicit else now, cams or None, limit=10)      # FTS + EmbeddingGemma 2 (solo FTS si el modelo no esta)
        for h in hits:
            cam_name = names.get(h["cam"], h["cam"])
            when = time.strftime("%d-%b %H:%M", time.localtime(h["ts"]))
            item = {"title": f"{cam_name} · {when} · coincidencia en descripción",
                    "url": f"{base}/", "content": f"{h['text'][:380]}" + ("" if h.get("snapshot_id") else " (sin captura guardada de este momento)")}
            if h.get("snapshot_id"):
                item["url"] = item["img_src"] = f"{base}/api/snapshots/file/{h['snapshot_id']}"
            results.append(item)
    except (sqlite3.Error, ImportError) as exc:
        logger.warning("fts search: %s", exc)

    # personas por como se ven ("persona con chamarra roja"): busqueda de imagen sobre los recortes de cuerpo
    try:
        if re.search(r"persona|hombre|mujer|alguien|gente", nq) and re.search(r"camiseta|playera|chamarra|chaqueta|sudadera|gorra|capucha|pantal|ropa|vest|lleva|con ", nq):
            from app.storage import embindex as _ei
            for vid, sim in _ei.search_text(db, query, since, until, k=4, kind="visit"):
                row = db._conn.execute("SELECT cam_id, start_ts FROM person_visits WHERE id=?", (vid,)).fetchone()
                if row:
                    results.append({"title": f"{names.get(row[0], row[0])} · {time.strftime('%d-%b %H:%M', time.localtime(row[1]))} · coincidencia por imagen",
                                    "url": f"{base}/api/person-visits/{vid}/body", "img_src": f"{base}/api/person-visits/{vid}/body",
                                    "content": f"Recorte de una persona que se parece a «{query}» (similitud {sim:.2f}); es una coincidencia visual, no una identificación."})
    except (sqlite3.Error, ImportError) as exc:
        logger.warning("busqueda por imagen: %s", exc)

    for p in people:
        visits = _person_visits(db, names, p, since, until)
        if visits:
            desc = "; ".join(f"{v['cam']} {time.strftime('%H:%M', time.localtime(v['a']))}-{time.strftime('%H:%M', time.localtime(v['b']))}"
                             for v in visits[:20])
            content = f"{p} fue identificado por rostro {sum(v['n'] for v in visits)} veces en {label}: {desc}."
        else:
            content = f"{p} no fue identificado por rostro en {label}."
        results.append({"title": f"{p} · recorrido {label}", "url": f"{base}/?persona={p}", "content": content})

    groups = _evidence(db, names, since, until, cams or None)
    if types:
        groups = [g for g in groups if g["tipo"] in types] or groups
    for g in groups:
        rango = g["desde"] if g["desde"] == g["hasta"] else f"{g['desde']}-{g['hasta']}"
        quien = f" Personas conocidas presentes: {', '.join(g['personas'])}." if g["personas"] else ""
        results.append({
            "title": f"{g['cam']} · {g['tipo'].replace('_', ' ')} ×{g['n']} · {rango}",
            "url": f"{base}{g['url']}" if g["url"] else f"{base}/",
            "content": f"{g['n']} alertas de tipo {g['tipo']} en {g['cam']} entre {g['desde']} y {g['hasta']}; "
                       f"hasta {g['max_personas']} personas. Descripción: {g['texto']}.{quien}"})

    if re.search(r"veh[ií]culo|auto|carro|coche|moto|camion|camioneta|estacion|placa|aparcad", nq):
        from app.vision.parked import list_vehicles
        for v in list_vehicles(db, limit=12):
            dur = v["duration_s"]
            tiempo = f"{dur // 3600} h {dur % 3600 // 60} min" if dur >= 3600 else f"{dur // 60} min"
            llegada = time.strftime("%d-%b %H:%M", time.localtime(v["first_ts"]))
            estado = "sigue estacionado" if v["state"] == "parked" else "ya se fue"
            placa = f" Placa: {v['plate']}." if v.get("plate") else " Placa sin registrar."
            results.append({"title": f"Vehículo {v['cls']} en {names.get(v['cam_id'], v['cam_id'])} · {estado}",
                            "url": f"{base}/api/vehicles/{v['id']}/image" if v["has_thumb"] else f"{base}/",
                            "content": f"{v['cls']} estacionado en {names.get(v['cam_id'], v['cam_id'])} desde {llegada} ({tiempo}); {estado}.{placa}"})
    for g in groups:
        if g["url"]:
            results.append({"title": f"{g['cam']} {g['hasta']} · {g['tipo'].replace('_', ' ')}",
                            "url": f"{base}{g['url']}", "img_src": f"{base}{g['url']}",
                            "content": g["texto"]})

    return {"query": query, "number_of_results": len(results), "results": results}
