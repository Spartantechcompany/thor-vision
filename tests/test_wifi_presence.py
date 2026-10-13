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


def _emp_db():
    db = DB()
    db._conn.executescript("CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT, category TEXT); INSERT INTO subjects VALUES (1, 'Ana', 'empleado');")
    return db


def named(name, mac, t=1_000_000.0):
    return [{"name": name, "ip": "1.1.1.1", "mac": mac, "type": "WIRELESS", "connectedAt": "2026-10-12T14:00:00Z"}]


def test_el_rol_se_adivina_por_el_nombre():
    g = routes_wifi.guess_role
    assert g("iOS Oswaldo Pérez") == "telefono" and g("MacBook Air Armando Zeferino") == "laptop" and g("Alexa Cocina") == "otro" and g("Watch 08:ed") == "otro"


def test_laptop_olvidada_no_cuenta_si_el_telefono_esta_fuera():
    db = _emp_db()
    routes_wifi.ingest(db, named("iOS Ana", "aa:bb:cc:dd:00:01") + named("MacBook Air Ana", "aa:bb:cc:dd:00:02"), now=1_000_000.0)
    db._conn.execute("UPDATE wifi_devices SET subject_id=1")
    routes_wifi.ingest(db, named("MacBook Air Ana", "aa:bb:cc:dd:00:02"), now=1_001_000.0)          # el telefono lleva mas de 10 min sin verse; la laptop sigue
    assert routes_wifi.online_employees(db, 1_001_000.0) == set()
    routes_wifi.ingest(db, named("iOS Ana", "aa:bb:cc:dd:00:01"), now=1_001_020.0)
    assert routes_wifi.online_employees(db, 1_001_020.0) == {1}


def test_sin_telefono_asignado_la_laptop_si_cuenta():
    db = _emp_db()
    routes_wifi.ingest(db, named("MacBook Air Ana", "aa:bb:cc:dd:00:02"), now=1_000_000.0)
    db._conn.execute("UPDATE wifi_devices SET subject_id=1")
    assert routes_wifi.online_employees(db, 1_000_010.0) == {1}


def test_cambio_de_mac_con_el_mismo_nombre_hereda_al_duenio():
    db = _emp_db()
    routes_wifi.ingest(db, named("iOS Oswaldo Pérez", "02:fe:83:bf:73:4a"), now=1_000_000.0)
    db._conn.execute("UPDATE wifi_devices SET subject_id=1, role='telefono'")
    routes_wifi.ingest(db, named("iOS Oswaldo Perez", "22:ca:a7:58:d0:a7"), now=1_000_100.0)            # direccion privada nueva, mismo nombre sin acentos
    assert db._conn.execute("SELECT subject_id, role FROM wifi_devices WHERE mac='22:ca:a7:58:d0:a7'").fetchone() == (1, "telefono")
    routes_wifi.ingest(db, named("iPhone 49:56", "aa:47:35:cc:49:56"), now=1_000_100.0)               # nombre generico: no hereda a nadie
    assert db._conn.execute("SELECT subject_id FROM wifi_devices WHERE mac='aa:47:35:cc:49:56'").fetchone()[0] is None
