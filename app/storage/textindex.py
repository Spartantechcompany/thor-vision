"""Indice de texto (FTS5) sobre las descripciones del VLM: permite buscar "persona con bolsa naranja" en el historial.

Se guarda solo la raiz de 5 letras de cada palabra (sin acentos): ligero y tolera plurales/generos.
El texto original se lee del evento por rowid. Prueba 2026-10-03: P@5=0.80 contra 0.62 de embeddings e5.
"""
import json
import re
import time
import unicodedata

STOP = set("de la el en con un una por que los las y a al del se su alguien persona personas quien andaba hay "
           "camara camaras ayer hoy anoche semana mes dia dias hora horas ultimas ultimos paso fue estuvo donde cuando cuantas cuantos".split())


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def stems(text: str) -> list:
    return [w[:5] for w in re.findall(r"\w+", _norm(text)) if len(w) > 2]


def query_stems(q: str) -> list:
    return list(dict.fromkeys(w[:5] for w in re.findall(r"\w+", _norm(q)) if len(w) > 2 and w not in STOP))


def event_text(payload: dict) -> str:
    pers = "; ".join(f"{p.get('desc', '')} {p.get('action', '')}" for p in (payload.get("persons") or []) if isinstance(p, dict))
    return f"{payload.get('activity', '')}. {pers}. {' '.join(payload.get('alerts') or [])}".strip()


def _load(data):
    try:
        return json.loads(data)
    except (ValueError, TypeError):
        return None


def ensure(conn) -> None:
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ev_fts USING fts5(t, tokenize='unicode61')")


def index_event(conn, eid: int, payload: dict, last: dict, cam) -> None:
    """Indexa la descripcion si cambio respecto a la ultima de la camara (las repeticiones no aportan)."""
    text = event_text(payload)
    if len(text) < 30 or last.get(cam) == text:
        return
    last[cam] = text
    conn.execute("INSERT INTO ev_fts(rowid, t) VALUES (?, ?)", (eid, " ".join(stems(text))))


def backfill(db, days: int = 30, batch: int = 2000) -> int:
    since = time.time() - days * 86400
    last: dict = {}
    n = 0
    cur_id = 0
    while True:
        with db._lock:
            rows = db._conn.execute(
                "SELECT id, cam_id, data FROM events WHERE type='nemotron' AND ts>? AND id>? ORDER BY id LIMIT ?",
                (since, cur_id, batch)).fetchall()
            if not rows:
                break
            for eid, cam, data in rows:
                cur_id = eid
                payload = _load(data)
                if payload is not None:
                    index_event(db._conn, eid, payload, last, cam)
                    n += 1
            db._conn.commit()
        time.sleep(0.05)
    return n


def search(db, query: str, since: float, until: float, cams=None, limit: int = 10) -> list:
    ws = query_stems(query)
    if not ws:
        return []
    sql = ("SELECT e.id, e.cam_id, e.ts, e.data FROM ev_fts JOIN events e ON e.id=ev_fts.rowid "
           "WHERE ev_fts MATCH ? AND e.ts BETWEEN ? AND ?")
    args = [" OR ".join(ws), since, until]
    if cams:
        sql += f" AND e.cam_id IN ({','.join('?' * len(cams))})"
        args += list(cams)
    sql += " ORDER BY bm25(ev_fts), e.ts DESC LIMIT ?"
    args.append(limit)
    out = []
    with db._lock:
        for eid, cam, ts, data in db._conn.execute(sql, args).fetchall():
            payload = _load(data)
            if payload is not None:
                snap = db._conn.execute("SELECT id FROM snapshots WHERE event_id=? ORDER BY id LIMIT 1", (eid,)).fetchone()
                out.append({"id": eid, "cam": cam, "ts": ts, "text": event_text(payload), "snapshot_id": snap[0] if snap else None})
    return out


def fetch_events(db, ids: list, cams=None) -> dict:
    """{id: hit} con el mismo formato que search(), para ids que vienen de otro buscador (embeddings)."""
    out = {}
    with db._lock:
        for eid in ids:
            row = db._conn.execute("SELECT id, cam_id, ts, data FROM events WHERE id=?", (eid,)).fetchone()
            if not row or (cams and row[1] not in cams):
                continue
            payload = _load(row[3])
            if payload is None:
                continue
            snap = db._conn.execute("SELECT id FROM snapshots WHERE event_id=? ORDER BY id LIMIT 1", (eid,)).fetchone()
            out[eid] = {"id": eid, "cam": row[1], "ts": row[2], "text": event_text(payload), "snapshot_id": snap[0] if snap else None}
    return out
