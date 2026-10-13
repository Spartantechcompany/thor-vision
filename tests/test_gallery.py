"""Galeria de identidades: recalculo del rostro promedio al mover o borrar miniaturas."""
import sqlite3
import threading

import numpy as np

from app.api import routes_people_admin as pa


class DB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE subjects (id INTEGER PRIMARY KEY, embedding BLOB, n_emb INTEGER)")
        self._conn.execute("CREATE TABLE person_visits (id INTEGER PRIMARY KEY, subject_id INT, embedding BLOB, n_emb INT)")


def _vec(i):
    v = np.zeros(512, np.float32)
    v[i] = 1.0
    return v.tobytes()


def test_recompute_averages_remaining_visits_and_clears_when_empty():
    db = DB()
    db._conn.execute("INSERT INTO subjects VALUES (1, NULL, 0), (2, NULL, 0)")
    db._conn.executemany("INSERT INTO person_visits VALUES (?,?,?,?)", [(1, 1, _vec(0), 3), (2, 1, _vec(1), 3), (3, 2, None, 0)])
    pa._recompute(db, 1)
    emb, n = db._conn.execute("SELECT embedding, n_emb FROM subjects WHERE id=1").fetchone()
    v = np.frombuffer(emb, np.float32)
    assert n == 6 and abs(float(np.linalg.norm(v)) - 1) < 1e-5 and v[0] > 0.7 and v[1] > 0.7
    db._conn.execute("DELETE FROM person_visits WHERE id=2")
    pa._recompute(db, 1)
    assert np.frombuffer(db._conn.execute("SELECT embedding FROM subjects WHERE id=1").fetchone()[0], np.float32)[1] == 0
    pa._recompute(db, 2)
    assert db._conn.execute("SELECT embedding, n_emb FROM subjects WHERE id=2").fetchone() == (None, 0)
