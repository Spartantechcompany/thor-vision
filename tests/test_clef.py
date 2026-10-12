"""Cliente de Clef (respuesta -> probabilidad) y regla de verificacion del tracker."""
from app.vision import clef_client
from app.vision.visits import Track, VisitManager


def _answers(p, choice="tree_or_pole"):
    return {"answers": {"is_person": {"type": "noul", "noul": p}, "what": {"choice": choice, "probabilities": {choice: 0.9}}}, "ms": 120}


def test_verify_person_parsea_la_respuesta(monkeypatch):
    c = clef_client.ClefClient("http://x")
    monkeypatch.setattr(c, "decide", lambda *a, **k: _answers(0.08))
    r = c.verify_person(b"jpg", "Exterior 2")
    assert r["p_person"] == 0.08 and r["what"] == "tree_or_pole" and r["ms"] == 120


def test_sin_respuesta_no_decide(monkeypatch):
    c = clef_client.ClefClient("http://127.0.0.1:1")          # nadie escucha
    assert c.verify_person(b"jpg") is None and c.stats["errors"] == 1


def test_pausa_tras_fallos_seguidos():
    c = clef_client.ClefClient("http://127.0.0.1:1")
    for _ in range(clef_client.FAILS):
        c.decide("x", [], {})
    assert not c.available() and c.decide("x", [], {}) is None and c.stats["skipped"] == 1


def test_maybe_verify_solo_sin_cara_y_con_cuerpo():
    m = VisitManager.__new__(VisitManager)
    import queue
    m.verifier, m._ver_q = object(), queue.Queue()
    tr = Track(1, (0, 0, 50, 100), 0.0)
    tr.hits, tr.body, tr.body_q = 6, b"x", 40.0
    m._maybe_verify("cam-191", tr)
    assert m._ver_q.qsize() == 1 and tr.ver_pending
    m._maybe_verify("cam-191", tr)                                # ya pendiente
    assert m._ver_q.qsize() == 1
    tr2 = Track(2, (0, 0, 50, 100), 0.0)
    tr2.hits, tr2.body, tr2.n_face = 6, b"x", 1                   # con cara: ya es una persona
    m._maybe_verify("cam-191", tr2)
    assert m._ver_q.qsize() == 1
