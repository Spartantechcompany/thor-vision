"""Identidad en dos etapas: Clef decide la zona gris con candados."""
import sqlite3
import threading

import numpy as np

from app.vision import visits as vm


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript("""CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT, named INTEGER);
            CREATE TABLE person_visits (id INTEGER PRIMARY KEY, subject_id INTEGER, face BLOB, face_score REAL);
            INSERT INTO subjects VALUES (1, 'Ana', 1), (2, NULL, 0), (3, 'Neto', 1);
            INSERT INTO person_visits VALUES (10, 1, x'01', 0.9), (20, 2, x'02', 0.5), (30, 3, x'03', 0.8);""")

    def update_person_visit(self, vid, **f):
        with self._lock:
            self._conn.execute("UPDATE person_visits SET subject_id=? WHERE id=?", (f["subject_id"], vid))


class Clef:
    def __init__(self, p): self.p = p
    def same_person(self, a, b): return self.p


def mgr(p):
    m = vm.VisitManager.__new__(vm.VisitManager)
    m.db, m._lock, m.stats, m.verifier = DB(), threading.RLock(), {}, Clef(p)
    m._subjects = {1: {"name": "Ana", "named": True}, 2: {"name": None, "named": False}, 3: {"name": "Neto", "named": True}}
    return m


def test_clef_alto_mueve_la_visita_y_borra_el_sujeto_vacio():
    m = mgr(0.93)
    assert m._id_check(20, 2, 1, 0.33, b"face") == "auto"
    assert m.db._conn.execute("SELECT subject_id FROM person_visits WHERE id=20").fetchone()[0] == 1
    assert m.db._conn.execute("SELECT COUNT(*) FROM subjects WHERE id=2").fetchone()[0] == 0 and 2 not in m._subjects


def test_clef_medio_solo_sugiere():
    m = mgr(0.7)
    assert m._id_check(20, 2, 1, 0.33, b"face") == "sugerencia"
    assert m.db._conn.execute("SELECT subject_id FROM person_visits WHERE id=20").fetchone()[0] == 2
    assert m.db._conn.execute("SELECT decision FROM identity_checks").fetchone()[0] == "sugerencia"


def test_nunca_mueve_la_visita_de_alguien_con_nombre():
    m = mgr(0.99)
    assert m._id_check(30, 3, 1, 0.40, b"face") == "bloqueado"           # Neto -> Ana: bloqueado aunque Clef diga 0.99
    assert m.db._conn.execute("SELECT subject_id FROM person_visits WHERE id=30").fetchone()[0] == 3
