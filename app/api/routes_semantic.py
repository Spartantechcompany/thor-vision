"""Busqueda semantica (EmbeddingGemma 2): descripciones de eventos y recortes de personas."""
import time

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.storage import embindex, textindex
from app.vision import embedder

router = APIRouter()


def _json(obj) -> Response:
    """JSON con la codificacion declarada: algunos visores leian los acentos como Latin-1."""
    return Response(json.dumps(obj, ensure_ascii=False), media_type="application/json; charset=utf-8")


@router.get("/api/search/semantic")
def semantic(request: Request, q: str, kind: str = "event", hours: float = 168, k: int = 12, mode: str = "hybrid"):
    """kind=event (descripciones) o visit (recortes de personas por apariencia). mode=hybrid|semantic|fts (solo eventos)."""
    db = getattr(request.app.state, "db", None)
    if db is None or not q.strip():
        raise HTTPException(400, "consulta vacia o sin base de datos")
    now = time.time()
    since = now - min(max(hours, 1), 24 * 30) * 3600
    k = min(max(k, 1), 50)
    if kind == "visit":
        hits = embindex.search_text(db, q, since, now, k=k, kind="visit")
        return _json({"kind": kind, "model_ready": embedder.available(), "results": [{"visit_id": i, "score": round(s, 3), "image": f"/api/person-visits/{i}/body"} for i, s in hits]})
    if mode == "fts":
        res = textindex.search(db, q, since, now, None, limit=k)
    elif mode == "semantic":
        res = list(textindex.fetch_events(db, [i for i, _ in embindex.search_text(db, q, since, now, k=k)]).values())
    else:
        res = embindex.hybrid_events(db, q, since, now, None, limit=k)
    return _json({"kind": "event", "mode": mode, "model_ready": embedder.available(), "results": res})


@router.get("/api/search/status")
def status(request: Request):
    db = request.app.state.db
    with db._lock:
        c = dict(db._conn.execute("SELECT kind, COUNT(*) FROM emb_index GROUP BY kind").fetchall()) if db._conn.execute("SELECT name FROM sqlite_master WHERE name='emb_index'").fetchone() else {}
    return {"model": embedder.MODEL, "dim": embedder.DIM, "loaded": embedder._model is not None, "failed": embedder._failed, "indexed": c}
