"""Merodeo frente a la puerta peatonal: zona dibujada + permanencia + persona verificada."""
import types

from app.vision import zones
from app.vision.visits import Track, VisitManager

ZONE = [{"id": "z1", "type": "puerta_peatonal", "name": "puerta", "text": "", "pts": [[0.2, 0.2], [0.3, 0.2], [0.3, 0.6], [0.2, 0.6]]}]
RES = types.SimpleNamespace(frame_w=1000, frame_h=1000)


def mgr(verifier=None):
    m = VisitManager.__new__(VisitManager)
    m.zone_cb, m.verifier, m.fired = None, verifier, []
    m.zone_cb = lambda cam, info: m.fired.append((cam, info))
    return m


def track(person_p=None, hits=6):
    tr = Track(1, (210, 400, 260, 580), 0.0)
    tr.hits, tr.person_p, tr.body = hits, person_p, b"x"
    return tr


def test_el_tipo_de_zona_existe_y_valida():
    assert "puerta_peatonal" in zones.TYPES and zones.validate(ZONE)[0]["type"] == "puerta_peatonal"


def test_permanencia_en_la_zona_alerta_una_sola_vez(monkeypatch):
    monkeypatch.setattr(zones, "get", lambda cam: ZONE)
    m, tr = mgr(), track()
    for t in (0, 8, 16, 24, 40):
        m._door_dwell("cam-189", tr, (210, 400, 260, 580), None, [], float(t), RES)
    assert len(m.fired) == 1 and m.fired[0][1]["dwell_s"] >= 20


def test_fuera_de_la_zona_o_poco_tiempo_no_alerta(monkeypatch):
    monkeypatch.setattr(zones, "get", lambda cam: ZONE)
    m, tr = mgr(), track()
    m._door_dwell("cam-189", tr, (600, 400, 650, 580), None, [], 0.0, RES)
    m._door_dwell("cam-189", tr, (600, 400, 650, 580), None, [], 60.0, RES)
    m._door_dwell("cam-189", tr, (210, 400, 260, 580), None, [], 100.0, RES)
    m._door_dwell("cam-189", tr, (210, 400, 260, 580), None, [], 105.0, RES)
    assert m.fired == []


def test_un_arbol_que_clef_no_confirma_no_merodea(monkeypatch):
    monkeypatch.setattr(zones, "get", lambda cam: ZONE)
    m = mgr(verifier=object())
    for p in (None, 0.1):
        tr = track(person_p=p)
        for t in (0, 30):
            m._door_dwell("cam-189", tr, (210, 400, 260, 580), None, [], float(t), RES)
    assert m.fired == []
    tr = track(person_p=0.9)
    for t in (0, 30):
        m._door_dwell("cam-189", tr, (210, 400, 260, 580), None, [], float(t), RES)
    assert len(m.fired) == 1
