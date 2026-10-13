"""Alertas de vehiculos estacionados: llegada, cada hora y salida, con metadato del auto y el tiempo."""
import sqlite3
import threading

from app.vision import parked


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self.events = []

    def insert_event(self, type, cam_id, payload, people=None, has_alert=False):
        self.events.append((type, cam_id, payload, has_alert))
        return len(self.events)


def tracker():
    db = FakeDB()
    t = parked.ParkedTracker(db)
    t.cam_names = {"cam-189": "Exterior 1"}
    return t, db


def add_vehicle(t, rec=7):
    t.db._conn.execute("INSERT INTO parked_vehicles (id, cam_id, cls, first_ts, last_ts, state) VALUES (?, 'cam-189', 'auto', 1000, 1000, 'parked')", (rec,))


def test_alert_payload_is_a_standard_alert_with_parked_metadata():
    t, db = tracker()
    add_vehicle(t)
    t._alert("cam-189", 7, "auto", "hourly", 1000.0, 1000.0 + 3 * 3600 + 120)
    typ, cam, p, has_alert = db.events[0]
    assert typ == "nemotron" and has_alert and cam == "cam-189"
    assert p["alert_types"] == ["vehiculo_detenido"] and p["severity"] == "medium" and p["schema"] == 2
    assert "lleva 3 h 2 min" in p["activity"] and "Exterior 1" in p["activity"]
    assert p["parked"]["state"] == "hourly" and p["parked"]["duration_s"] == 3 * 3600 + 120


def test_arrival_and_departure_messages():
    t, db = tracker()
    add_vehicle(t)
    t._alert("cam-189", 7, "auto", "arrived", 1000.0, 1000.0)
    t._alert("cam-189", 7, "auto", "left", 1000.0, 1000.0 + 25 * 60)
    assert "estacionado en Exterior 1" in db.events[0][2]["activity"] and db.events[0][2]["severity"] == "low"
    assert "se fue de Exterior 1 tras 25 min" in db.events[1][2]["activity"]


def test_plate_goes_into_the_alert_when_known():
    t, db = tracker()
    add_vehicle(t)
    t.db._conn.execute("UPDATE parked_vehicles SET plate='ABC123' WHERE id=7")
    t._alert("cam-189", 7, "auto", "arrived", 1000.0, 1010.0)
    assert db.events[0][2]["parked"]["plate"] == "ABC123" and "ABC123" in db.events[0][2]["alerts"][0]
