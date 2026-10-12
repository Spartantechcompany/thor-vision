"""Presencia por WiFi, asignacion a empleados y poda de fotos de empleados."""
import sqlite3
import threading

import pytest

from app.api import routes_wifi
from app.storage.db import prune_employee_images


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript(routes_wifi._SCHEMA)


def clients(*macs):
    return [{"name": f"tel-{m[-2:]}", "ip": "192.168.0.5", "mac": m, "type": "WIRELESS", "connectedAt": "2026-10-12T14:00:00Z"} for m in macs]


def test_sesiones_se_extienden_y_se_parten_por_huecos():
    db, mac = DB(), "aa:bb:cc:dd:ee:01"
    routes_wifi.ingest(db, clients(mac), now=1_000_000.0)
    routes_wifi.ingest(db, clients(mac), now=1_000_180.0)               # mismo bloque
    routes_wifi.ingest(db, clients(mac), now=1_000_180.0 + 5000)         # volvio despues de mucho: otra sesion
    rows = db._conn.execute("SELECT start_ts, end_ts FROM wifi_sessions ORDER BY id").fetchall()
    assert len(rows) == 2 and rows[0][1] == 1_000_180.0


def test_lo_cableado_y_macs_invalidas_no_cuentan():
    db = DB()
    n = routes_wifi.ingest(db, [{"mac": "aa:bb:cc:dd:ee:02", "type": "WIRED"}, {"mac": "x"}] + clients("aa:bb:cc:dd:ee:03"), now=1_000_000.0)
    assert n == 1


def test_presencia_por_empleado_une_dispositivos():
    db = DB()
    db._conn.executescript("CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT, category TEXT);")
    db._conn.execute("INSERT INTO subjects VALUES (1, 'Ana', 'empleado')")
    for i, mac in enumerate(("aa:bb:cc:dd:ee:10", "aa:bb:cc:dd:ee:11")):
        for t in range(0, 3601, 300):                                    # el lector envia cada pocos minutos
            routes_wifi.ingest(db, clients(mac), now=1_000_000.0 + i * 60 + t if t < 3600 else 1_003_600.0)
    db._conn.execute("UPDATE wifi_devices SET subject_id=1")
    r = routes_wifi.presence_by_employee(db, 999_000.0, 1_010_000.0)[1]
    assert r["minutes"] == 60 and len(r["devices"]) == 2 and r["first"] == 1_000_000.0 and r["last"] == 1_003_600.0


def test_poda_conserva_las_mejores_fotos_por_camara():
    c = sqlite3.connect(":memory:")
    c.executescript("""CREATE TABLE subjects (id INTEGER PRIMARY KEY, category TEXT);
        CREATE TABLE person_visits (id INTEGER PRIMARY KEY, subject_id INT, cam_id TEXT, start_ts REAL, face BLOB, body BLOB, scene BLOB, face_score REAL, body_score REAL);
        INSERT INTO subjects VALUES (1, 'empleado'), (2, 'invitado');""")
    for i in range(8):
        c.execute("INSERT INTO person_visits VALUES (?,1,'cam-a',100,x'01',x'02',x'03',?,1)", (i + 1, float(i)))        # mejores: 8..4
    c.execute("INSERT INTO person_visits VALUES (20,1,'cam-b',100,NULL,x'02',NULL,NULL,5)")
    c.execute("INSERT INTO person_visits VALUES (30,2,'cam-a',100,x'01',x'02',x'03',1,1)")                             # invitado: no se toca
    c.execute("INSERT INTO person_visits VALUES (40,1,'cam-a',900,x'01',x'02',x'03',0,0)")                              # reciente: no se toca
    assert prune_employee_images(c, 500.0, 5) == 3
    kept = {r[0] for r in c.execute("SELECT id FROM person_visits WHERE face IS NOT NULL OR body IS NOT NULL")}
    assert kept == {4, 5, 6, 7, 8, 20, 30, 40}
