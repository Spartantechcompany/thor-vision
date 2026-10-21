"""Recuadro de la escena en el verde del panel y filtro de cajas diminutas sin castigar a quien se aleja."""
import cv2
import numpy as np

from app.vision.visits import Track, VisitManager, _scene_with_box


def _mgr():
    m = VisitManager.__new__(VisitManager)
    m.zones, m._ghosts = {"cam-x": "exterior"}, {}
    return m


def _track(heights):
    tr = Track(1, (100, 100, 140, 100 + heights[0]), 0.0)
    tr.dims = (1280, 720)
    for i, h in enumerate(heights[1:], 1):
        tr.observe((100 + i * 12, 100, 140 + i * 12, 100 + h), float(i))      # camina: no es un objeto fijo
    return tr


def test_persona_que_se_aleja_no_es_fantasma():
    tr = _track([300 - 8 * i for i in range(30)])           # termina con ~70 px... y la ultima caja ya es chica
    tr.observe((600, 100, 620, 100 + 30), 31.0)             # ultimo cuadro: 30 px de alto (<7% de 720)
    assert tr.bbox[3] - tr.bbox[1] < 0.07 * 720
    assert not _mgr()._is_ghost("cam-x", tr)


def test_caja_minuscula_toda_la_vida_si_es_fantasma():
    assert _mgr()._is_ghost("cam-x", _track([30] * 12))


def test_escena_con_dos_personas_dibuja_las_dos_en_verde_delgado():
    frame = np.zeros((720, 1280, 3), np.uint8)
    jpg = _scene_with_box(frame, (100, 100, 300, 500), [(700, 100, 900, 500)], width=1280)
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
    edge = img[300, 96:105].astype(int)                      # cruza el borde izquierdo de la caja principal
    b, g, r = edge[edge[:, 1].argmax()]
    assert g > 120 and r > b + 50                            # #76B900 (R>B), no el turquesa de antes
    assert (img[300, 696:705].astype(int)[:, 1] > 50).any()  # la otra persona tambien
    assert (img[300, 106:290] < 25).all()                    # nada dibujado dentro de la caja
    assert (img[300, 99:102].astype(int)[:, 1] > 60).sum() <= 2   # 1 px de grosor (JPEG deja algo de halo)


def test_visita_sin_recorte_de_cuerpo_guarda_la_escena():
    import types
    import numpy as np
    from app.vision.visits import VisitManager
    m = VisitManager.__new__(VisitManager)
    import threading
    m._lock, m._tracks, m._next_tid = threading.RLock(), {}, {}
    m.gait = m.verifier = m.trips_attend = m.burst_cb = m.zone_cb = None
    m._grabbers, m.zones, m.stats = {}, {}, {}
    m._housekeeping = lambda *a, **k: None
    m._maybe_snapshot = m._maybe_verify = lambda *a, **k: None
    frame = np.zeros((720, 1280, 3), np.uint8)
    res = types.SimpleNamespace(person_bboxes=[(10, 10, 20, 30)], faces=[], frame_w=1280, frame_h=720)      # caja minuscula: no genera recorte de cuerpo
    m.update("cam-228", res, frame)
    tr = m._tracks["cam-228"][0]
    assert tr.body is None and tr.scene
