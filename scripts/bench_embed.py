"""Benchmark antes/despues de la busqueda semantica con EmbeddingGemma 2, con los datos reales de Thor (corre dentro del contenedor).
TEXTO: consultas en lenguaje natural sobre las descripciones del VLM; verdad = tipo de alerta que el propio sistema asigno al evento.
        antes = FTS5 (textindex, lo que hoy usa el Vision Agent); despues = embeddings; y la mezcla (RRF).
IMAGEN: consultas por color de ropa sobre los recortes de cuerpo; verdad = color de la prenda superior calculado por bodyid.
        antes = FTS sobre la descripcion del VLM de la visita (solo las que tienen descripcion); despues = embedding de la imagen.
Uso: python3 /app/scripts/bench_embed.py   (env: DAYS=7, VISIT_DAYS=3, MAX_VISITS=3000)"""
import io
import json
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
import numpy as np

from app.storage import embindex, textindex
from app.vision import embedder

DAYS, VDAYS, MAXV = float(os.environ.get("DAYS", 7)), float(os.environ.get("VISIT_DAYS", 3)), int(os.environ.get("MAX_VISITS", 3000))
now = time.time()
conn = sqlite3.connect("file:/app/data/events.db?mode=ro", uri=True, check_same_thread=False)


class DB:
    _conn = conn
    _lock = __import__("threading").RLock()


db = DB()
TEXT_Q = {
    "vehiculo_detenido": ["vehículo estacionado que permanece frente al acceso", "auto detenido en la calle junto a la entrada"],
    "manipulacion_vehiculo": ["persona abriendo o forzando un auto", "alguien revisando la puerta de un coche"],
    "animal": ["un perro o gato dentro de la casa", "animal caminando en el interior"],
    "uso_telefono_exterior": ["persona mirando su celular en la banqueta", "alguien usando el teléfono en la calle"],
    "merodeo": ["persona parada frente al muro o la reja mirando hacia adentro", "alguien que se detiene y observa el inmueble"],
    "acceso_no_autorizado": ["persona intentando entrar por una puerta o reja", "alguien forzando una puerta"],
    "grupo_inusual": ["grupo de tres o más personas juntas", "varias personas reunidas"],
    "persona_en_suelo": ["persona tirada o caída en el piso", "alguien acostado en el suelo sin moverse"],
    "persona_nocturna": ["persona de pie en el interior durante la noche", "alguien sentado frente a una laptop de madrugada"],
}


def p_at(rel, ranked, k=10):
    top = ranked[:k]
    return sum(1 for x in top if x in rel) / max(1, len(top))


def mrr(rel, ranked, k=10):
    for i, x in enumerate(ranked[:k]):
        if x in rel:
            return 1.0 / (i + 1)
    return 0.0


def texto():
    since = now - DAYS * 86400
    rows = conn.execute("SELECT e.id, e.data FROM events e JOIN ev_fts f ON f.rowid=e.id WHERE e.ts>?", (since,)).fetchall()
    typ = {}
    for eid, data in rows:
        try:
            typ[eid] = (json.loads(data).get("alert_types") or [None])[0]
        except (ValueError, TypeError):
            pass
    t0 = time.time()
    embindex.ensure(sqlite3.connect(":memory:"))
    texts = []
    for eid, data in rows:
        try:
            texts.append((eid, textindex.event_text(json.loads(data))))
        except (ValueError, TypeError):
            pass
    vec = embedder.embed_texts([t for _, t in texts], batch=128)
    ids = np.array([i for i, _ in texts])
    emb_s = time.time() - t0
    res = {"candidatos": len(texts), "segundos_vectorizar": round(emb_s, 1), "por_tipo": {}}
    agg = {"fts": [], "emb": [], "hibrido": []}
    for t, qs in TEXT_Q.items():
        rel = {i for i, ty in typ.items() if ty == t}
        if len(rel) < 30:
            continue
        for q in qs:
            fts = [h["id"] for h in textindex.search(db, q, since, now, None, limit=50)]
            qv = embedder.embed_texts([q], query=True)[0]
            emb = [int(ids[i]) for i in np.argsort(-(vec @ qv))[:50]]
            hyb = embindex.rrf([fts, emb], 50)
            for name, r in (("fts", fts), ("emb", emb), ("hibrido", hyb)):
                agg[name].append((p_at(rel, r), mrr(rel, r)))
            res["por_tipo"].setdefault(t, []).append({"q": q, "positivos": len(rel), **{n: round(p_at(rel, r), 2) for n, r in (("fts", fts), ("emb", emb), ("hibrido", hyb))}})
    res["promedio"] = {n: {"P@10": round(float(np.mean([a for a, _ in v])), 3), "MRR@10": round(float(np.mean([b for _, b in v])), 3)} for n, v in agg.items() if v}
    return res


COL = {"rojo": "rojo", "negro": "negro", "azul": "azul", "gris": "gris", "blanco": "blanco", "rosa": "rosa", "morado": "morado", "cafe": "café"}


def imagen():
    from PIL import Image
    since = now - VDAYS * 86400
    rows = conn.execute("SELECT id, attrs, body, vlm_desc FROM person_visits WHERE start_ts>? AND fp=0 AND static=0 AND body IS NOT NULL AND attrs IS NOT NULL", (since,)).fetchall()
    random.Random(7).shuffle(rows)
    rows = rows[:MAXV]
    up, desc, imgs, ids = {}, {}, [], []
    for vid, attrs, body, d in rows:
        try:
            up[vid] = json.loads(attrs).get("upper")
            imgs.append(Image.open(io.BytesIO(body)).convert("RGB"))
            ids.append(vid)
            desc[vid] = d or ""
        except (ValueError, TypeError, OSError):
            pass
    t0 = time.time()
    vec = embedder.embed_images(imgs)
    emb_s = time.time() - t0
    ids = np.array(ids)
    res = {"candidatos": len(ids), "con_descripcion_vlm": sum(1 for v in desc.values() if v), "segundos_vectorizar": round(emb_s, 1), "por_color": {}}
    agg = {"fts_descripcion": [], "emb_imagen": []}
    for key, word in COL.items():
        rel = {i for i, u in up.items() if u == key}
        if len(rel) < 15:
            continue
        q = f"persona con camiseta de color {word}"
        stem = textindex.stems(word)[0]
        fts = [i for i, d in desc.items() if d and stem in textindex.stems(d)][:50]                     # lo que hoy se podria buscar (solo visitas con descripcion)
        qv = embedder.embed_texts([q], query=True)[0]
        emb = [int(ids[i]) for i in np.argsort(-(vec @ qv))[:50]]
        base = len(rel) / len(ids)
        res["por_color"][key] = {"positivos": len(rel), "azar": round(base, 3), "fts_descripcion_P@10": round(p_at(rel, fts), 2), "fts_aciertos": len(fts[:10]), "emb_imagen_P@10": round(p_at(rel, emb), 2)}
        agg["fts_descripcion"].append(p_at(rel, fts))
        agg["emb_imagen"].append(p_at(rel, emb))
    res["promedio_P@10"] = {n: round(float(np.mean(v)), 3) for n, v in agg.items() if v}
    return res


if __name__ == "__main__":
    t0 = time.time()
    print("cargando modelo...", flush=True)
    embedder.available()
    out = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "modelo": embedder.MODEL, "dim": embedder.DIM, "texto": texto(), "imagen": imagen(), "segundos_total": round(time.time() - t0)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
