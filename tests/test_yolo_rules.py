"""Modo sin VLM: persona nocturna y merodeo por reglas sobre visitas confirmadas."""
import sqlite3
import threading
import time

from app.vision import scenes as sc
from app.vision import yolo_rules as yr


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript("""CREATE TABLE subjects (id INTEGER PRIMARY KEY, category TEXT);
            CREATE TABLE person_visits (id INTEGER PRIMARY KEY, attrs TEXT);
            INSERT INTO subjects VALUES (1, 'empleado'), (2, 'revisar');
            INSERT INTO person_visits VALUES (10, '{"upper": "gris", "lower": "azul"}');""")
        self.events = []

    def insert_event(self, typ, cam, data, people=0, has_alert=False):
        self.events.append((cam, data))
        return len(self.events)


class Scenes:
    def __init__(self):
        mk = lambda mode: sc.SceneContext(cam_id="x", name="x", where="", focus=[], ignore=[], alert_types=frozenset(), night_person=mode, vehicles=False, night_hours=(22, 6))
        self.by = {"cam-120": mk("alert"), "cam-191": mk("note")}

    def get(self, cam):
        return self.by.get(cam)


def at(h):
    return time.mktime(time.struct_time((2026, 10, 13, h, 0, 0, 1, 286, -1)))            # martes


def make(vlm_ok=0.0, started=0.0):
    db = DB()
    return db, yr.YoloRules(db, None, Scenes(), {"cam-120": "Acceso Site", "cam-191": "Exterior 2"}, {"cam-120": "interior", "cam-191": "exterior"}, lambda: vlm_ok, started=started)


def visit(t, dur=10, **k):
    return {"visit_id": 10, "subject_id": None, "name": None, "first_ts": t, "last_ts": t + dur, **k}


def test_solo_actua_si_el_vlm_esta_en_silencio():
    t = at(2)
    db, r = make(vlm_ok=t - 10, started=t - 9999)
    assert r.on_visit("cam-120", visit(t), now=t + 20) is None                         # el VLM respondio hace 10 s
    db, r = make(vlm_ok=t - 3600, started=t - 9999)
    assert r.on_visit("cam-120", visit(t), now=t + 20) == "persona_nocturna"
    assert "ropa: arriba gris, abajo azul" in db.events[0][1]["activity"] and db.events[0][1]["source"] == "yolo"


def test_no_alerta_de_dia_ni_a_personal_conocido():
    t = at(14)
    _, r = make(vlm_ok=0.0, started=0.0)
    assert r.on_visit("cam-120", visit(t), now=t + 20) is None
    t = at(2)
    assert r.on_visit("cam-120", visit(t, name="Ana"), now=t + 20) is None
    assert r.on_visit("cam-120", visit(t, subject_id=1), now=t + 20) is None and r.stats["skipped_known"] == 2


def test_calle_solo_si_se_queda_y_con_enfriamiento():
    t = at(15)
    _, r = make()
    assert r.on_visit("cam-191", visit(t, dur=20), now=t + 30) is None                  # peaton que pasa
    assert r.on_visit("cam-191", visit(t, dur=200), now=t + 300) == "merodeo"
    assert r.on_visit("cam-191", visit(t + 10, dur=200), now=t + 310) is None           # enfriamiento
    t = at(1)
    _, r = make()
    assert r.on_visit("cam-191", visit(t, dur=60), now=t + 70) is None                  # esa camara no tiene night_person: alert
