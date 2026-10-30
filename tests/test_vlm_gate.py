"""Menos llamadas al VLM: falsos fijos recordados, una descripcion por episodio y alertas agrupadas."""
import time
import types

from app.vision import visits as vm
from app.vision import vlm_worker as vw


def _vm():
    m = vm.VisitManager.__new__(vm.VisitManager)
    m._fp_boxes, m.stats = {}, {}
    return m


def test_falso_fijo_recordado_tras_dos_descartes():
    m = _vm()
    bici = (100, 200, 300, 600)
    assert not m._fp_like("cam-118", bici, 1000.0)
    m._fp_note("cam-118", bici, 900.0)
    assert not m._fp_like("cam-118", bici, 1000.0)            # una vez no basta
    m._fp_note("cam-118", (105, 205, 305, 600), 950.0)
    assert m._fp_like("cam-118", bici, 1000.0)
    assert not m._fp_like("cam-118", (700, 200, 900, 600), 1000.0)   # en otro lugar no
    assert not m._fp_like("cam-118", bici, 1000.0 + vm.FP_MEM_S)     # la memoria caduca
    m._fp_boxes["cam-vto"] = [(950.0, bici)] * 3
    assert not m._fp_like("cam-vto", bici, 1000.0)            # el videoportero nunca se silencia


class _Tracks:
    def __init__(self, tracks):
        self.tracks = tracks

    def active_tracks(self, cam, max_age):
        return self.tracks


def _worker(cam, tracks, objs=()):
    w = vw.VLMWorker.__new__(vw.VLMWorker)
    w.cam_id = w.context_cam_id = cam
    det = types.SimpleNamespace(yolo_ts=time.time(), objects=[{"c": c} for c in objs])
    w.store = types.SimpleNamespace(get=lambda c: det)
    w.tracks_source = _Tracks(tracks)
    w._last_ep = None
    w._alert_last = {}
    w.gate_skips = []
    return w


def test_movimiento_sin_persona_ni_animal_no_se_describe():
    assert _worker("cam-118", [], objs=("auto", "bicicleta"))._struct_gate()[0] == "sin_objeto"
    assert _worker("cam-215", [], objs=("perro",))._struct_gate()[0] is None


def test_calle_solo_quien_se_acerca():
    lejos = [{"id": 1, "near": False}]
    assert _worker("cam-191", lejos)._struct_gate()[0] == "calle_registro"
    assert _worker("cam-191", [{"id": 1, "near": True}])._struct_gate()[0] is None


def test_mismo_episodio_se_describe_una_vez():
    w = _worker("cam-215", [{"id": 4, "near": False}])
    why, ep = w._struct_gate()
    assert why is None
    w._last_ep = (ep[0], ep[1], time.time())
    assert w._struct_gate()[0] == "mismo_episodio"
    w.tracks_source = _Tracks([{"id": 4, "near": False}, {"id": 5, "near": False}])
    assert w._struct_gate()[0] is None                          # llego alguien mas
    w.tracks_source = _Tracks([{"id": 4, "near": False}])
    w._last_ep = (ep[0], ep[1], time.time() - vw.EPISODE_REFRESH_S - 1)
    assert w._struct_gate()[0] is None                          # se refresca tras la ventana


def test_alertas_validas_agrupadas_y_calle_baja():
    w = _worker("cam-215", [])
    assert w._alert_filter({"alert_types": [], "severity": "high"})[0] is False
    assert w._alert_filter({"alert_types": ["merodeo"], "severity": "none"})[0] is False
    ok, g = w._alert_filter({"alert_types": ["merodeo"], "severity": "low"})
    assert ok
    w._alert_last[g["key"]] = (time.time(), g["sev"], 77)
    ok, g2 = w._alert_filter({"alert_types": ["merodeo"], "severity": "low"})
    assert not ok and g2["repeat_of"] == 77
    assert w._alert_filter({"alert_types": ["merodeo"], "severity": "high"})[0]   # sube de severidad: si avisa
    assert w._alert_filter({"alert_types": ["animal"], "severity": "low"})[0]     # otro tipo: si avisa
    calle = _worker("cam-189", [])
    assert calle._alert_filter({"alert_types": ["merodeo"], "severity": "low"})[1]["dropped"] == "calle_baja"
    assert calle._alert_filter({"alert_types": ["merodeo"], "severity": "medium"})[0]
