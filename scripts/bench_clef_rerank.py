"""Clef reordenando el buscador: mismas consultas y misma verdad que bench_embed.py (docs/BENCHMARK-embeddinggemma2.md).
TEXTO: el hibrido (FTS + EmbeddingGemma) da 20 candidatos; Clef los reordena (a) leyendo la descripcion y (b) mirando la captura del evento.
COLOR: el embedding de imagen da 20 recortes de cuerpo; Clef los reordena mirando el recorte.
Verdad = el tipo de alerta que asigno el VLM (texto) y el color calculado por bodyid (color): ambas son etiquetas automaticas, no humanas.
Uso (dentro del contenedor): PYTHONPATH=/app/data/pylibs-emb python3 /tmp/bench_clef_rerank.py"""
import base64
import io
import json
import os
import random
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import bench_embed as be
from app.storage import embindex, textindex
from app.vision import embedder

CLEF = os.environ.get("CLEF_URL", "http://127.0.0.1:20140")
K = int(os.environ.get("RERANK_K", "20"))
SNAP_ROOT = "/app/data/snapshots"


def clef(state, images, question):
    req = {"state": state, "images": [base64.b64encode(i).decode() for i in images], "questions": {"q": {"type": "noul", "instructions": question}}}
    r = urllib.request.urlopen(urllib.request.Request(CLEF + "/decide", json.dumps(req).encode(), {"Content-Type": "application/json"}), timeout=60)
    return json.loads(r.read())["answers"]["q"]["noul"]


def snap(eid):
    row = be.conn.execute("SELECT path FROM snapshots WHERE event_id=? ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    if not row:
        return None
    for p in (row[0], os.path.join(SNAP_ROOT, row[0])):
        try:
            from PIL import Image
            im = Image.open(p).convert("RGB")
            im.thumbnail((640, 640))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=80)
            return b.getvalue()
        except OSError:
            continue
    return None


def texto():
    since = be.now - be.DAYS * 86400
    rows = be.conn.execute("SELECT e.id, e.data FROM events e JOIN ev_fts f ON f.rowid=e.id WHERE e.ts>?", (since,)).fetchall()
    typ, txt = {}, {}
    for eid, data in rows:
        try:
            d = json.loads(data)
            typ[eid] = (d.get("alert_types") or [None])[0]
            txt[eid] = textindex.event_text(d)
        except (ValueError, TypeError):
            pass
    ids = np.array(list(txt))
    vec = embedder.embed_texts([txt[i] for i in ids], batch=128)
    agg = {"hibrido": [], "clef_texto": [], "clef_imagen": []}
    calls, t0 = 0, time.time()
    for t, qs in be.TEXT_Q.items():
        rel = {i for i, ty in typ.items() if ty == t}
        if len(rel) < 30:
            continue
        for q in qs:
            fts = [h["id"] for h in textindex.search(be.db, q, since, be.now, None, limit=50)]
            qv = embedder.embed_texts([q], query=True)[0]
            emb = [int(ids[i]) for i in np.argsort(-(vec @ qv))[:50]]
            hyb = embindex.rrf([fts, emb], 50)
            top = hyb[:K]
            st = [(clef({"camera_event_description": txt.get(e, "")}, [], f"Does this security camera event match the search: '{q}'?"), e) for e in top]
            imgs = {e: snap(e) for e in top}
            si = [(clef({"task": "Security camera snapshot of an event", "search": q}, [imgs[e]], f"Does this image show: {q}?") if imgs[e] else -1, e) for e in top]
            calls += 2 * len(top)
            r_txt = [e for _, e in sorted(st, key=lambda x: -x[0])] + hyb[K:]
            r_img = [e for _, e in sorted(si, key=lambda x: -x[0])] + hyb[K:]
            for name, r in (("hibrido", hyb), ("clef_texto", r_txt), ("clef_imagen", r_img)):
                agg[name].append((be.p_at(rel, r), be.mrr(rel, r)))
    return {"consultas": len(agg["hibrido"]), "llamadas_clef": calls, "segundos": round(time.time() - t0),
            "promedio": {n: {"P@10": round(float(np.mean([a for a, _ in v])), 3), "MRR@10": round(float(np.mean([b for _, b in v])), 3)} for n, v in agg.items() if v}}


def color():
    from PIL import Image
    since = be.now - be.VDAYS * 86400
    rows = be.conn.execute("SELECT id, attrs, body FROM person_visits WHERE start_ts>? AND fp=0 AND static=0 AND body IS NOT NULL AND attrs IS NOT NULL", (since,)).fetchall()
    random.Random(7).shuffle(rows)
    rows = rows[:be.MAXV]
    up, imgs, ids, raw = {}, [], [], {}
    for vid, attrs, body in rows:
        try:
            up[vid] = json.loads(attrs).get("upper")
            imgs.append(Image.open(io.BytesIO(body)).convert("RGB"))
            ids.append(vid)
            raw[vid] = body
        except (ValueError, TypeError, OSError):
            pass
    vec = embedder.embed_images(imgs)
    ids = np.array(ids)
    agg = {"emb_imagen": [], "clef_imagen": []}
    for key, word in be.COL.items():
        rel = {i for i, u in up.items() if u == key}
        if len(rel) < 15:
            continue
        q = f"persona con camiseta de color {word}"
        qv = embedder.embed_texts([q], query=True)[0]
        emb = [int(ids[i]) for i in np.argsort(-(vec @ qv))[:50]]
        sc = [(clef({"task": "Crop of a person from a security camera"}, [raw[v]], f"Is this person wearing an upper-body garment (shirt, jacket, hoodie) that is mainly {word}?"), v) for v in emb[:K]]
        r = [v for _, v in sorted(sc, key=lambda x: -x[0])] + emb[K:]
        agg["emb_imagen"].append(be.p_at(rel, emb))
        agg["clef_imagen"].append(be.p_at(rel, r))
    return {"colores": len(agg["emb_imagen"]), "promedio_P@10": {n: round(float(np.mean(v)), 3) for n, v in agg.items() if v}}


if __name__ == "__main__":
    embedder.available()
    out = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "rerank_k": K, "texto": texto(), "color": color()}
    print(json.dumps(out, ensure_ascii=False, indent=1))
