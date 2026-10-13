"""Indice vectorial (EmbeddingGemma 2) de descripciones de eventos y recortes de personas. Busqueda semantica y mezcla con FTS (RRF).
Tabla emb_index(kind, ref_id, vec float16). Los vectores se cargan a memoria al buscar (decenas de miles x 256 dim: trivial)."""
import io
import json
import logging
import threading
import time

import numpy as np

from app.storage import textindex
from app.vision import embedder

logger = logging.getLogger(__name__)
DDL = "CREATE TABLE IF NOT EXISTS emb_index (kind TEXT NOT NULL, ref_id INTEGER NOT NULL, ts REAL, vec BLOB NOT NULL, PRIMARY KEY (kind, ref_id))"
_cache: dict = {}


def ensure(conn) -> None:
    conn.execute(DDL)
    conn.commit()


def _pack(v) -> bytes:
    return np.asarray(v, np.float16).tobytes()


def _matrix(db, kind: str):
    """(ids, ts, matriz) en memoria; solo se leen las filas nuevas desde la ultima consulta (rowid mayor)."""
    last, ids, ts, mat = _cache.get(kind, (0, np.zeros(0, np.int64), np.zeros(0, np.float64), np.zeros((0, embedder.DIM), np.float32)))
    with db._lock:
        rows = db._conn.execute("SELECT rowid, ref_id, ts, vec FROM emb_index WHERE kind=? AND rowid>? ORDER BY rowid", (kind, last)).fetchall()
    if rows:
        ids = np.concatenate([ids, np.array([r[1] for r in rows], np.int64)])
        ts = np.concatenate([ts, np.array([r[2] or 0 for r in rows], np.float64)])
        mat = np.concatenate([mat, np.frombuffer(b"".join(r[3] for r in rows), np.float16).reshape(len(rows), -1).astype(np.float32)])
        _cache[kind] = (rows[-1][0], ids, ts, mat)
    return ids, ts, mat


def search_vec(db, kind: str, qvec, since: float = 0, until: float = 1e12, k: int = 20) -> list:
    ids, ts, mat = _matrix(db, kind)
    if not len(ids):
        return []
    sim = mat @ np.asarray(qvec, np.float32)
    sim = np.where((ts >= since) & (ts <= until), sim, -9)
    top = np.argsort(-sim)[:k]
    return [(int(ids[i]), float(sim[i])) for i in top if sim[i] > -9]


def search_text(db, query: str, since: float, until: float, k: int = 20, kind: str = "event") -> list:
    if not embedder.available():
        return []
    q = embedder.embed_texts([query], query=True)
    return search_vec(db, kind, q[0], since, until, k) if len(q) else []


def rrf(rank_lists: list, k: int = 20, c: int = 60) -> list:
    """Mezcla de rankings (reciprocal rank fusion): los mejores de cada lista suben, sin comparar puntajes distintos."""
    score: dict = {}
    for lst in rank_lists:
        for r, x in enumerate(lst):
            score[x] = score.get(x, 0.0) + 1.0 / (c + r + 1)
    return [x for x, _ in sorted(score.items(), key=lambda t: -t[1])[:k]]


def index_events(db, days: float = 7, limit: int = 500) -> int:
    """Vectoriza las descripciones que el FTS ya considera nuevas (ev_fts) y aun no estan en emb_index."""
    since = time.time() - days * 86400
    with db._lock:
        rows = db._conn.execute("SELECT e.id, e.ts, e.data FROM events e JOIN ev_fts f ON f.rowid=e.id "
                                "WHERE e.ts>? AND NOT EXISTS (SELECT 1 FROM emb_index x WHERE x.kind='event' AND x.ref_id=e.id) ORDER BY e.id DESC LIMIT ?", (since, limit)).fetchall()
    items = []
    for eid, ts, data in rows:
        try:
            items.append((eid, ts, textindex.event_text(json.loads(data))))
        except (ValueError, TypeError):
            continue
    if not items:
        return 0
    vecs = embedder.embed_texts([t for _, _, t in items])
    if not len(vecs):
        return 0
    with db._lock:
        db._conn.executemany("INSERT OR REPLACE INTO emb_index(kind, ref_id, ts, vec) VALUES ('event',?,?,?)", [(i, ts, _pack(v)) for (i, ts, _), v in zip(items, vecs)])
        db._conn.commit()
    return len(items)


def index_visits(db, days: float = 3, limit: int = 150) -> int:
    """Vectoriza el cuerpo (o la escena) de las visitas reales: permite buscar personas por como se ven."""
    from PIL import Image
    since = time.time() - days * 86400
    with db._lock:
        rows = db._conn.execute("SELECT v.id, v.start_ts, v.body FROM person_visits v WHERE v.start_ts>? AND v.fp=0 AND v.static=0 AND v.body IS NOT NULL "
                                "AND NOT EXISTS (SELECT 1 FROM emb_index x WHERE x.kind='visit' AND x.ref_id=v.id) ORDER BY v.id DESC LIMIT ?", (since, limit)).fetchall()
    ids, ts, imgs = [], [], []
    for vid, t, blob in rows:
        try:
            imgs.append(Image.open(io.BytesIO(blob)).convert("RGB"))
            ids.append(vid)
            ts.append(t)
        except (OSError, ValueError):
            continue
    if not imgs:
        return 0
    vecs = embedder.embed_images(imgs)
    if not len(vecs):
        return 0
    with db._lock:
        db._conn.executemany("INSERT OR REPLACE INTO emb_index(kind, ref_id, ts, vec) VALUES ('visit',?,?,?)", [(i, t, _pack(v)) for i, t, v in zip(ids, ts, vecs)])
        db._conn.commit()
    return len(ids)


def start_indexer(db, every_s: float = 120.0) -> threading.Event:
    stop = threading.Event()
    ensure(db._conn)

    def loop():
        stop.wait(60)
        while not stop.is_set():
            try:
                if embedder.available():
                    a, b = index_events(db), index_visits(db)
                    if a or b:
                        logger.info("embindex: +%d eventos, +%d visitas", a, b)
                        if a == 500:
                            stop.wait(3)                   # hay mas pendiente: pausa corta para que las consultas del usuario pasen
                            continue
            except Exception as exc:                       # noqa: BLE001 - el indexador no debe morir por un lote malo
                logger.warning("embindex: %s", exc, exc_info=True)
            stop.wait(every_s)
    threading.Thread(target=loop, daemon=True, name="embindex").start()
    return stop


def hybrid_events(db, query: str, since: float, until: float, cams=None, limit: int = 10) -> list:
    """FTS + embeddings mezclados (RRF). Si el modelo no esta disponible, queda solo el FTS (como antes)."""
    fts = textindex.search(db, query, since, until, cams, limit=max(limit * 3, 30))
    ranked = [h["id"] for h in fts]
    by = {h["id"]: h for h in fts}
    sem = [i for i, _ in search_text(db, query, since, until, k=max(limit * 3, 30))] if embedder.available() else []
    if sem:
        by.update({i: h for i, h in textindex.fetch_events(db, [i for i in sem if i not in by], cams).items()})
        sem = [i for i in sem if i in by]
        ranked = rrf([ranked, sem], limit)
    return [by[i] for i in ranked[:limit] if i in by]
