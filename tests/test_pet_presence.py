"""Presencia de mascotas: en casa / fuera y avisos de salida y regreso."""
import sqlite3
import threading

from app.vision.pet_presence import PetPresence


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)


def make():
    ev = []
    return PetPresence(DB(), {"cam-189", "cam-191"}, {"cam-215": "Cocina", "cam-189": "Exterior 1"}, lambda *a: ev.append(a[:3])), ev


def test_salida_y_regreso_se_avisan_una_vez():
    p, ev = make()
    assert p.seen("akamaru", "cam-215", 0.9, 1000.0) is None
    assert p.seen("akamaru", "cam-189", 0.9, 1100.0) == "salida"
    assert p.seen("akamaru", "cam-189", 0.9, 1150.0) is None                  # sigue fuera
    assert p.seen("akamaru", "cam-215", 0.9, 1500.0) == "regreso"
    assert [e[0] for e in ev] == ["salida", "regreso"]
    st = {s["pet"]: s for s in p.status(1600.0)}
    assert st["akamaru"]["status"] == "en casa" and st["akamaru"]["cam"] == "Cocina" and st["gigi"]["status"] == "sin datos"


def test_poca_confianza_o_perro_desconocido_no_cuentan():
    p, ev = make()
    assert p.seen("akamaru", "cam-215", 0.4, 1000.0) is None and p.seen("firulais", "cam-215", 0.99, 1000.0) is None
    assert p.status(1001.0)[0]["status"] == "sin datos"


def test_cambio_de_lado_tras_mucho_tiempo_no_es_salida():
    p, ev = make()
    p.seen("gigi", "cam-215", 0.9, 1000.0)
    assert p.seen("gigi", "cam-189", 0.9, 1000.0 + 5000) is None and ev == []   # no se sabe cuando cruzo: solo cambia el estado
    assert {s["pet"]: s for s in p.status(7000.0)}["gigi"]["status"] == "fuera"
