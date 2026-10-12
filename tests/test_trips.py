"""Viajes disparados por eventos: direccion, union en un solo viaje y silencio del timbre sin entrada."""
import sqlite3
import threading
import time

from app.vision.trips import Trips

TOPO = {"street": ["cam-vto", "cam-189", "cam-191"], "trips": {"window_s": 300, "triggers": {
    "garage-abre": {"label": "Se abrió el garage", "cams": ["cam-189", "cam-228", "cam-113"], "inner": ["cam-228", "cam-113"]},
    "vto-visita": {"label": "Visita", "cams": ["cam-vto", "cam-120"], "inner": ["cam-120"], "quiet_if_empty": True, "min_gap_s": 90}}}}


class FakeDB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript("""CREATE TABLE person_visits (id INTEGER PRIMARY KEY, cam_id TEXT, start_ts REAL, end_ts REAL, body BLOB, scene BLOB,
            subject_id INTEGER, known_name TEXT, fp INTEGER DEFAULT 0, static INTEGER DEFAULT 0, journey_id INTEGER);
            CREATE TABLE journeys (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL, last_ts REAL, last_cam TEXT, subject_id INTEGER, origin TEXT, tuya_lead_s REAL,
            n_visits INTEGER, status TEXT);""")
        self.events = []

    def insert_event(self, typ, cam, data, people=0, has_alert=False):
        self.events.append((cam, data))
        return len(self.events)

    def visit(self, i, cam, t0, t1, jid=None):
        with self._lock:
            self._conn.execute("INSERT INTO person_visits (id, cam_id, start_ts, end_ts, journey_id) VALUES (?,?,?,?,?)", (i, cam, t0, t1, jid))


def make():
    db = FakeDB()
    return db, Trips(db, None, TOPO, {"cam-189": "Exterior 1", "cam-228": "Garage frontal", "cam-113": "Escaleras entrada", "cam-vto": "Videoportero", "cam-120": "Acceso Site"})


def test_entrada_calle_garage_escaleras_es_un_solo_viaje():
    db, t = make()
    t0 = time.time()
    sid = t.start("garage-abre", t0)
    db.visit(1, "cam-189", t0 - 40, t0 - 20, 7)
    db.visit(2, "cam-228", t0 + 5, t0 + 20, 7)
    db.visit(3, "cam-113", t0 + 40, t0 + 70, 7)
    t.sweep(t0 + 1000)
    cam, data = db.events[0]
    assert data["trip"]["direction"] == "entrada" and data["trip"]["visits"] == [1, 2, 3] and cam == "cam-189"
    assert "Exterior 1 > Garage frontal > Escaleras entrada" in data["activity"]
    assert sid == 1


def test_salida_termina_en_la_calle():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    db.visit(1, "cam-113", t0 - 10, t0 + 20, 3)
    db.visit(2, "cam-228", t0 + 30, t0 + 45, 3)
    db.visit(3, "cam-189", t0 + 60, t0 + 80, 3)
    t.sweep(t0 + 1000)
    assert db.events[0][1]["trip"]["direction"] == "salida"


def test_garage_sin_nadie_alerta_pero_timbre_sin_entrada_no():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    t.start("vto-visita", t0)
    db.visit(5, "cam-vto", t0, t0 + 8)
    t.sweep(t0 + 1000)
    assert len(db.events) == 1 and "ninguna cámara vio a alguien" in db.events[0][1]["activity"]


def test_mismo_evento_repetido_no_abre_otra_sesion():
    _, t = make()
    t0 = time.time()
    assert t.start("vto-visita", t0) == t.start("vto-visita", t0 + 30)
    assert t.start("otro", t0) is None and t.attending("cam-vto") and not t.attending("cam-215")


def test_dos_personas_a_la_vez_no_se_funden():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    db.visit(1, "cam-189", t0 - 30, t0 - 10, 1)
    db.visit(2, "cam-228", t0 + 5, t0 + 20, 1)
    db.visit(3, "cam-113", t0 + 10, t0 + 40, 2)             # otra persona, ya adentro, saliendo
    db.visit(4, "cam-228", t0 + 50, t0 + 60, 2)
    db.visit(5, "cam-189", t0 + 70, t0 + 90, 2)
    t.sweep(t0 + 1000)
    d = db.events[0][1]
    assert d["trip"]["direction"] == "varios" and d["trip"]["paths"] == [[1, 2], [3, 4, 5]] and d["people"] == 2


def test_gente_de_calle_sin_entrar_no_es_recorrido():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    db.visit(1, "cam-189", t0, t0 + 20, 9)
    t.sweep(t0 + 1000)
    assert "ninguna cámara vio a alguien" in db.events[0][1]["activity"]


def test_parpadeo_de_una_camara_no_cuenta():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    db.visit(1, "cam-228", t0 + 5, t0 + 7, 4)               # 2 s en una sola camara
    t.sweep(t0 + 1000)
    assert "ninguna cámara vio a alguien" in db.events[0][1]["activity"]


def test_entrada_va_primero_y_el_movimiento_interno_se_cuenta():
    db, t = make()
    t0 = time.time()
    t.start("garage-abre", t0)
    db.visit(1, "cam-228", t0 + 1, t0 + 15, 5)               # movimiento interno
    db.visit(2, "cam-113", t0 + 2, t0 + 20, 6)               # otro
    db.visit(3, "cam-189", t0 - 20, t0 - 5, 8)
    db.visit(4, "cam-228", t0 + 30, t0 + 45, 8)              # entrada real
    t.sweep(t0 + 1000)
    d = db.events[0][1]
    assert d["activity"].startswith("Se abrió el garage: entrada: Exterior 1 > Garage frontal") and "+2 movimientos más dentro" in d["activity"]
    assert d["trip"]["direction"] == "entrada"
