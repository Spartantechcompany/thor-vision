"""Calidad de las caras: sin agrandar recortes chicos, minimo de tamano y Clef quitando las miniaturas barridas o borrosas."""
import sqlite3
import threading

import numpy as np

from app.pipeline.vision_processor import VisionModels
from app.vision import visits as vm


def _side(jpg):
    import cv2
    return cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR).shape[:2]


def test_recorte_chico_no_se_agranda():
    frame = np.random.randint(0, 255, (1080, 1920, 3), np.uint8)
    assert _side(VisionModels._crop_thumb(frame, (100, 100, 140, 150), size=160, margin=0.35)) == (85, 85)       # 50 px x 1.7 de margen, sin agrandar a 160


def test_recorte_grande_se_reduce_al_maximo():
    frame = np.random.randint(0, 255, (1080, 1920, 3), np.uint8)
    assert _side(VisionModels._crop_thumb(frame, (100, 100, 300, 320), size=160, margin=0.35)) == (160, 160)


def test_cara_chica_o_desenfocada_no_es_miniatura():
    assert vm.thumb_ok((0, 0, 60, 70), 500)
    assert not vm.thumb_ok((0, 0, 30, 40), 500)          # 30 px de ancho en el cuadro original: se veria borrosa
    assert not vm.thumb_ok((0, 0, 60, 70), 50)           # muy desenfocada


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.executescript("CREATE TABLE person_visits (id INTEGER PRIMARY KEY, face BLOB); INSERT INTO person_visits VALUES (1, x'01'), (2, x'02');")


class Clef:
    def __init__(self, p): self.p = p
    def face_clear(self, face): return self.p


def _mgr(p):
    m = vm.VisitManager.__new__(vm.VisitManager)
    m.db, m.stats, m.verifier = DB(), {}, Clef(p)
    return m


def test_clef_quita_la_cara_que_no_se_ve_bien():
    m = _mgr(0.05)
    m._face_check(1, b"x")
    assert m.db._conn.execute("SELECT face FROM person_visits WHERE id=1").fetchone()[0] is None
    assert m.db._conn.execute("SELECT face FROM person_visits WHERE id=2").fetchone()[0] is not None
    assert m.stats["face_clef_drop"] == 1


def test_clef_conserva_la_cara_clara_y_sin_respuesta_no_toca_nada():
    m = _mgr(0.6)
    m._face_check(1, b"x")
    assert m.db._conn.execute("SELECT face FROM person_visits WHERE id=1").fetchone()[0] is not None
    m.verifier = Clef(None)
    assert m._face_check(2, b"x") is None
    assert m.db._conn.execute("SELECT face FROM person_visits WHERE id=2").fetchone()[0] is not None
