"""Indice de texto de descripciones del VLM (alimenta la busqueda del Vision Agent)."""
import json
import sqlite3
import threading

from app.storage import textindex as ti


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, type TEXT, cam_id TEXT, data TEXT)")
        self._conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, event_id INTEGER)")
        ti.ensure(self._conn)
        self.last = {}

    def add(self, eid, cam, ts, activity, desc=""):
        p = {"activity": activity, "persons": [{"desc": desc, "action": ""}] if desc else []}
        self._conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (eid, ts, "nemotron", cam, json.dumps(p)))
        ti.index_event(self._conn, eid, p, self.last, cam)


def test_stems_ignore_accents_and_plurals():
    assert ti.stems("Camiseta NARANJA, bolsas") == ["camis", "naran", "bolsa"]
    assert ti.query_stems("alguien con bolsa naranja ayer") == ["bolsa", "naran"]
    assert ti.query_stems("persona en la cocina hoy") == ["cocin"]


def test_search_finds_by_description_and_respects_window_and_cam():
    db = FakeDB()
    db.add(1, "cam-191", 1000, "Una mujer camina por la banqueta cargando una bolsa naranja", "blusa negra")
    db.add(2, "cam-215", 1010, "Un hombre sentado en la cocina usando una laptop", "camiseta azul")
    db.add(3, "cam-191", 5000, "Una persona con bolsa naranja pasa frente al muro", "gorra")
    r = ti.search(db, "persona con bolsa naranja", 0, 2000)
    assert [x["id"] for x in r] == [1]
    assert {x["id"] for x in ti.search(db, "bolsa naranja", 0, 9999)} == {1, 3}
    assert ti.search(db, "bolsa naranja", 0, 9999, cams=["cam-215"]) == []
    assert ti.search(db, "alguien ayer", 0, 9999) == []          # solo palabras vacias: sin busqueda


def test_consecutive_duplicates_are_not_reindexed():
    db = FakeDB()
    for i in range(5):
        db.add(i + 1, "cam-1", 100 + i, "Sala vacia con luces encendidas y sillas ordenadas")
    assert db._conn.execute("SELECT count(*) FROM ev_fts").fetchone()[0] == 1


def test_hit_carries_real_snapshot_id_only_when_it_exists():
    db = FakeDB()
    db.add(1, "cam-1", 1000, "Persona con bolsa naranja en la banqueta", "blusa")
    db.add(2, "cam-1", 1100, "Otra persona con bolsa naranja junto al muro", "chamarra")
    db._conn.execute("INSERT INTO snapshots (id, event_id) VALUES (77, 2)")
    by_id = {h["id"]: h for h in ti.search(db, "bolsa naranja", 0, 2000)}
    assert by_id[1]["snapshot_id"] is None and by_id[2]["snapshot_id"] == 77
