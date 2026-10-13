"""Busqueda semantica: mezcla RRF, busqueda vectorial, respaldo a FTS sin modelo, indexado incremental (con un embedder falso)."""
import json
import sqlite3
import threading
import time

import numpy as np

from app.storage import embindex, textindex
from app.vision import embedder

WORDS = ["perro", "auto", "persona", "laptop", "capucha", "bolsa"]


def fake_texts(texts, query=False, batch=64):
    out = np.zeros((len(texts), embedder.DIM), np.float32)
    for i, t in enumerate(texts):
        for j, w in enumerate(WORDS):
            if w in t.lower():
                out[i, j] = 1.0
        out[i, 20] = 0.01
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, cam_id TEXT, type TEXT, data TEXT)")
        self._conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, event_id INTEGER)")
        textindex.ensure(self._conn)
        embindex.ensure(self._conn)

    def add(self, eid, text, cam="cam-1"):
        d = {"activity": text + " en la entrada de la casa", "persons": [], "alerts": []}
        self._conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (eid, time.time(), cam, "nemotron", json.dumps(d)))
        self._conn.execute("INSERT INTO ev_fts(rowid, t) VALUES (?,?)", (eid, " ".join(textindex.stems(text + " en la entrada de la casa"))))


def test_rrf_promotes_items_ranked_high_in_both_lists():
    assert embindex.rrf([[1, 2, 3], [3, 1, 4]], 3)[:2] == [1, 3]


def test_index_search_and_incremental_indexing(monkeypatch):
    monkeypatch.setattr(embedder, "embed_texts", fake_texts)
    monkeypatch.setattr(embedder, "available", lambda: True)
    db = DB()
    db.add(1, "Un perro camina por el pasillo")
    db.add(2, "Un auto blanco estacionado frente al acceso")
    db.add(3, "Una persona con capucha mira su laptop")
    assert embindex.index_events(db) == 3 and embindex.index_events(db) == 0         # no repite lo ya indexado
    top = embindex.search_text(db, "perro", 0, time.time() + 10, k=2)
    assert top[0][0] == 1
    db.add(4, "Otro perro duerme junto a la puerta")
    assert embindex.index_events(db) == 1
    assert {i for i, _ in embindex.search_text(db, "perro", 0, time.time() + 10, k=2)} == {1, 4}


def test_hybrid_falls_back_to_fts_without_the_model(monkeypatch):
    monkeypatch.setattr(embedder, "available", lambda: False)
    db = DB()
    db.add(1, "Un perro camina por el pasillo")
    hits = embindex.hybrid_events(db, "perro", 0, time.time() + 10, None, limit=5)
    assert [h["id"] for h in hits] == [1]
