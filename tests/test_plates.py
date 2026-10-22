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
    assert plates.vote([("ABC123", .6), ("ABC123", .55)]) is None             # dos lecturas de confianza media ya no bastan (entraban placas mal leidas)
    assert plates.vote([("ABC123", .75), ("ABC123", .72)])[:2] == ("ABC123", 0.735)
    assert plates.vote([("ABC123", .62)] * 3)[0] == "ABC123"                   # tres lecturas iguales si
    assert plates.vote([("ABC123", .8), ("ABC128", .8)]) is None              # no coinciden
    w = plates.vote([("ABC123", .8), ("ABC128", .3), ("ABC123", .75), ("ABC128", .3)])
    assert w[0] == "ABC123" and w[2] == 2                                      # gana la que suma mas
    assert plates.vote([]) is None
    assert plates.guess([("ABC123", .6), ("ABC123", .55)])[0] == "ABC123"      # queda por confirmar
    assert plates.guess([("ABC123", .2)]) is None


def test_similar_tolera_confusiones_del_ocr():
    assert plates.similar("RAG051E", "RAC451E")            # G/C y 0/4 en placas reales de Exterior 1 y 2
    assert plates.similar("761ZJP", "761ZJP") and plates.similar("MLT588A", "WLT588A")
    assert not plates.similar("RAG051E", "PZB203J") and not plates.similar("ABC123", "ABC1234") and not plates.similar(None, "ABC123")
    assert not plates.similar("ABC123", "AXY123")          # dos letras distintas que el OCR no confunde
    assert not plates.similar("ABC12", "ABX1Z")            # placas cortas: dos diferencias no bastan


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")

    def insert_event(self, *a, **k):
        return 1


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


def _rec(t, rid, cam, first, last, plate, conf, src="auto", state="left"):
    t.db._conn.execute("INSERT INTO parked_vehicles (id, cam_id, cls, first_ts, last_ts, left_ts, state, plate, plate_conf, plate_src) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (rid, cam, "auto", first, last, last, state, plate, conf, src))


def test_mismo_auto_en_la_misma_camara_se_fusiona_y_en_la_otra_queda_como_gemelo():
    import time
    t = parked.ParkedTracker(FakeDB())
    b = time.time() - 10 * 86400
    _rec(t, 10, "cam-191", b + 1000, b + 5000, "RAG051E", .83)
    _rec(t, 11, "cam-189", b + 2000, b + 6000, "RAC451E", .75)          # a la vez, la otra camara, lectura parecida
    _rec(t, 12, "cam-191", b + 5200, b + 9000, "RAG051E", .9)           # misma camara, pegado: registro partido
    _rec(t, 13, "cam-191", b + 300000, b + 305000, "RAG051E", .9)       # dias despues: visita repetida
    t.relink_all()
    q = lambda rid: t.db._conn.execute("SELECT plate, merged_into, twin_id, first_ts FROM parked_vehicles WHERE id=?", (rid,)).fetchone()
    assert q(12)[1] == 10 and q(10)[3] == b + 1000             # 12 se fusiona en 10
    assert q(11)[0] == "RAG051E" and q(11)[2] == 10            # la lectura mas segura gana y quedan como gemelos
    assert q(13)[1] is None and q(13)[2] is None               # la de otro dia no se toca
    v = {x["id"]: x for x in parked.list_vehicles(t.db, limit=50)}
    assert 12 not in v and v[13]["other_visits"] == 1          # el fusionado no se lista; el gemelo no cuenta como visita aparte


def test_lecturas_poco_seguras_viejas_pasan_a_por_confirmar():
    db = FakeDB()
    parked.ParkedTracker(db)
    db._conn.execute("INSERT INTO parked_vehicles (id, cam_id, cls, first_ts, last_ts, state, plate, plate_conf, plate_src) VALUES (1,'cam-191','auto',0,0,'left','MB5939A',.62,'auto')")
    db._conn.execute("INSERT INTO parked_vehicles (id, cam_id, cls, first_ts, last_ts, state, plate, plate_conf, plate_src) VALUES (2,'cam-191','auto',0,0,'left','579BSV',.83,'auto')")
    parked.ParkedTracker(db)                                   # al arrancar
    r = dict((i, (p, g)) for i, p, g in db._conn.execute("SELECT id, plate, plate_guess FROM parked_vehicles"))
    assert r[1] == (None, "MB5939A") and r[2] == ("579BSV", None)


def test_se_sigue_leyendo_despacio_despues_de_las_primeras_lecturas(monkeypatch):
    t, c = make()
    monkeypatch.setattr(plates, "enabled", lambda: True)
    monkeypatch.setattr(plates, "read", lambda crop: None)
    c["plate_n"] = plates.MAX_READS
    t._maybe_read_plate(c, np.zeros((400, 600, 3), np.uint8), 600, 400, 1000.0)
    assert c["plate_n"] == plates.MAX_READS + 1 and c["plate_next"] == 1000.0 + parked.PLATE_SLOW_S


def test_carril_con_trafico_no_se_registra_como_estacionado():
    t = parked.ParkedTracker(FakeDB())
    frame = np.zeros((400, 600, 3), np.uint8)
    box = (100, 100, 300, 250)
    res = lambda objs: type("R", (), {"objects": objs, "frame_w": 600, "frame_h": 400})()
    car = [{"c": "auto", "b": box}]
    for i in range(240):                                       # 4 min a 1 cuadro/s; el auto solo aparece 1 de cada 6 cuadros (pasan autos)
        t.update("cam-191", res(car if i % 6 == 0 else []), frame, ts=1000.0 + i)
    assert t.db._conn.execute("SELECT COUNT(*) FROM parked_vehicles").fetchone()[0] == 0
    t2 = parked.ParkedTracker(FakeDB())
    for i in range(240):                                       # auto quieto: aparece en casi todos los cuadros
        t2.update("cam-191", res(car if i % 10 else []), frame, ts=1000.0 + i)
    assert t2.db._conn.execute("SELECT COUNT(*) FROM parked_vehicles").fetchone()[0] == 1
