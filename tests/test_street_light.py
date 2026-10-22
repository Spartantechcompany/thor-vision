"""Camaras de calle en modo ligero: solo quien se acerca a la puerta abre una visita; el resto se cuenta como transeunte."""
import sqlite3
import threading

from app.vision import visits as vm, zones


def test_cerca_de_la_puerta_con_y_sin_zona_de_seguimiento(monkeypatch):
    door = [{"type": "puerta_peatonal", "pts": [(0.40, 0.60), (0.50, 0.60), (0.50, 0.80), (0.40, 0.80)]}]
    monkeypatch.setattr(zones, "get", lambda cam: door)
    assert zones.near_door("cam-191", (430, 500, 470, 700), 1000, 1000) is True          # en la puerta
    assert zones.near_door("cam-191", (330, 520, 370, 760), 1000, 1000) is True          # al lado: dentro del margen
    assert zones.near_door("cam-191", (850, 100, 890, 300), 1000, 1000) is False         # al fondo de la calle
    monkeypatch.setattr(zones, "get", lambda cam: door + [{"type": "seguimiento_calle", "pts": [(0.7, 0.0), (1.0, 0.0), (1.0, 0.4), (0.7, 0.4)]}])
    assert zones.near_door("cam-191", (850, 100, 890, 300), 1000, 1000) is True          # la zona dibujada manda
    monkeypatch.setattr(zones, "get", lambda cam: [])
    assert zones.near_door("cam-191", (850, 100, 890, 300), 1000, 1000) is None          # sin zonas: no se filtra


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)


def test_transeunte_se_cuenta_y_no_abre_visita(monkeypatch):
    m = vm.VisitManager.__new__(vm.VisitManager)
    m.db, m.stats = DB(), {}
    tr = vm.Track(1, (850, 100, 890, 300), 1000.0)
    assert not m._full("cam-191", tr) and m._full("cam-215", tr)
    tr.near = True
    assert m._full("cam-191", tr)
    m._passerby("cam-191", 3600 * 5 + 10)
    m._passerby("cam-191", 3600 * 5 + 900)
    assert m.db._conn.execute("SELECT hour, n FROM street_counts").fetchall() == [(18000.0, 2)]
