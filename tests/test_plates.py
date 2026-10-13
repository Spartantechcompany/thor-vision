"""Lectura de placas: normalizacion, votacion entre lecturas y que lo manual mande sobre lo automatico."""
import sqlite3
import threading

import numpy as np

from app.vision import parked, plates


def test_normalize():
    assert plates.normalize("abc-123 d") == "ABC123D"
    assert plates.normalize("AB1") == ""                 # demasiado corta
    assert plates.normalize("A" * 12) == ""              # demasiado larga
    assert plates.normalize(None) == ""


def test_vote_needs_agreement_or_a_very_confident_single_read():
    assert plates.vote([("ABC123", .6)]) is None                              # una sola lectura poco segura
    assert plates.vote([("ABC123", .9)])[0] == "ABC123"                       # una lectura muy segura
    assert plates.vote([("ABC123", .6), ("ABC123", .55)])[:2] == ("ABC123", 0.575)
    assert plates.vote([("ABC123", .6), ("ABC128", .6)]) is None              # no coinciden
    w = plates.vote([("ABC123", .6), ("ABC128", .3), ("ABC123", .7), ("ABC128", .3)])
    assert w[0] == "ABC123" and w[2] == 2                                      # gana la que suma mas
    assert plates.vote([]) is None


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")


def make():
    t = parked.ParkedTracker(FakeDB())
    t.db._conn.execute("INSERT INTO parked_vehicles (id, cam_id, cls, first_ts, last_ts, state) VALUES (1, 'cam-189', 'auto', 0, 0, 'parked')")
    c = {"rec": 1, "anchor": (100, 100, 300, 250), "cls": {"auto": 5}}
    return t, c


def row(t):
    return t.db._conn.execute("SELECT plate, plate_conf, plate_src, plate_img IS NOT NULL FROM parked_vehicles WHERE id=1").fetchone()


def test_auto_plate_is_saved_after_agreeing_reads(monkeypatch):
    t, c = make()
    monkeypatch.setattr(plates, "enabled", lambda: True)
    monkeypatch.setattr(plates, "read", lambda crop: {"text": "ABC123", "conf": .8, "det": .9, "jpeg": b"jpg"})
    frame = np.zeros((400, 600, 3), np.uint8)
    t._maybe_read_plate(c, frame, 600, 400, 1000.0)
    assert row(t)[0] is None                                       # una lectura de 0.72 todavia no alcanza
    t._maybe_read_plate(c, frame, 600, 400, 1010.0)               # antes del intervalo: no vuelve a leer
    assert c["plate_n"] == 1
    t._maybe_read_plate(c, frame, 600, 400, 1000.0 + plates.EVERY_S + 1)
    assert row(t)[0] == "ABC123" and row(t)[2] == "auto" and row(t)[3] == 1


def test_manual_plate_is_never_overwritten(monkeypatch):
    t, c = make()
    t.db._conn.execute("UPDATE parked_vehicles SET plate='MANUAL1', plate_src='manual' WHERE id=1")
    monkeypatch.setattr(plates, "enabled", lambda: True)
    monkeypatch.setattr(plates, "read", lambda crop: {"text": "ZZZ999", "conf": .9, "det": .9, "jpeg": b"x"})
    t._maybe_read_plate(c, np.zeros((400, 600, 3), np.uint8), 600, 400, 1000.0)
    assert row(t)[0] == "MANUAL1" and row(t)[2] == "manual"


def test_disabled_reader_reads_nothing(monkeypatch):
    t, c = make()
    monkeypatch.setattr(plates, "enabled", lambda: False)
    t._maybe_read_plate(c, np.zeros((400, 600, 3), np.uint8), 600, 400, 1000.0)
    assert row(t)[0] is None and c.get("plate_n", 0) == 0
