"""Consolidacion de identidades: fusiona solo con Clef e InsightFace de acuerdo, nunca dos personas con nombre, una fusion por sujeto por pasada."""
import sqlite3
import threading

import numpy as np

from app.vision import consolidate as C


class DB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")


def _s(i, named, v, n=1):
    e = np.asarray(v, np.float32)
    return {"id": i, "named": named, "emb": e / np.linalg.norm(e), "n": n, "face": bytes([i])}


class Clef:
    def __init__(self, p): self.p = p
    def same_person(self, a, b): return self.p.get(tuple(sorted((a[0], b[0]))), 0.0)


def test_reglas(monkeypatch):
    subs = [_s(1, True, [1, 0, 0]), _s(2, False, [0.9, 0.3, 0]), _s(3, True, [0.95, 0.2, 0.1]), _s(4, False, [0.6, 0.0, 0.8]), _s(5, False, [0.62, 0.02, 0.78])]
    monkeypatch.setattr(C, "_subjects", lambda db: subs)
    merged = []
    mgr = type("M", (), {"merge_subjects": lambda self, a, b: merged.append((a, b)) or True})()
    clef = Clef({(1, 2): 0.9, (2, 3): 0.9, (1, 4): 0.9, (4, 5): 0.9})
    db = DB()
    st = C.run(db, mgr, clef, dry=False)
    assert db._conn.execute("SELECT COUNT(*) FROM identity_checks").fetchone()[0] == 2
    assert sum(1 for m in merged if m[0] == 2 and m[1] in (1, 3)) == 1     # sin nombre -> a UNA persona con nombre (la mas parecida)
    assert not any(set(m) == {1, 3} for m in merged)        # dos con nombre nunca
    assert (4, 5) in merged or (5, 4) in merged
    assert st["merged"] == 2 and st["skipped"] >= 1


def test_coseno_bajo_queda_como_sugerencia(monkeypatch):
    subs = [_s(1, False, [1, 0]), _s(2, False, [0.35, 0.937])]         # coseno 0.35: Clef dice que si, InsightFace no
    monkeypatch.setattr(C, "_subjects", lambda db: subs)
    mgr = type("M", (), {"merge_subjects": lambda self, a, b: True})()
    st = C.run(None, mgr, Clef({(1, 2): 0.95}), dry=True)
    assert st["merged"] == 0 and st["suggested"] == 1
