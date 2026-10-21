"""Menos llamadas y mas escena: clip de 16 s, escena igual sin llamada, rutina fuera cuando el gateway esta saturado, prompt fijo antes del medio."""
import time
from collections import deque
from types import SimpleNamespace

import numpy as np

from app.vision import scenes, vlm_analyzer as va, vlm_worker as vw


def _worker(scene=None, cam="cam-x"):
    w = vw.VLMWorker.__new__(vw.VLMWorker)
    w.cam_id, w.scene, w._last_sig = cam, scene, None
    w._ring, w._ring_t = deque(maxlen=12), 0.0
    return w


def _det(n=1, x=100):
    return SimpleNamespace(yolo_ts=time.time(), yolo_persons=n, person_bboxes=[(x, 100, x + 50, 300)] * n, objects=[], faces=[])


def test_misma_escena_se_salta_y_un_cambio_no():
    w = _worker()
    fr = np.full((360, 640, 3), 90, np.uint8)
    w._last_sig = w._signature(fr, _det(1))
    assert w._same_scene(w._signature(fr, _det(1, x=110)))           # se movio 10 px: sigue siendo la misma escena
    assert not w._same_scene(w._signature(fr, _det(2)))              # llego otra persona
    assert not w._same_scene(w._signature(fr, _det(1, x=400)))       # cruzo el cuadro
    other = fr.copy(); other[:, :320] = 200
    assert not w._same_scene(w._signature(other, _det(1)))           # la imagen cambio
    w._last_sig = dict(w._last_sig, t=time.time() - vw.SAME_STALE_S - 1)
    assert not w._same_scene(w._signature(fr, _det(1)))              # demasiado viejo: se vuelve a mirar


def test_prioridad_videoportero_calle_con_persona_y_noche():
    street = SimpleNamespace(night_alert=lambda: False, vehicles=True)
    office = SimpleNamespace(night_alert=lambda: False, vehicles=False)
    night = SimpleNamespace(night_alert=lambda: True, vehicles=False)
    assert _worker(cam="cam-vto")._high_priority(None)
    assert _worker(street)._high_priority(_det(1)) and not _worker(street)._high_priority(_det(0))
    assert not _worker(office)._high_priority(_det(2))
    assert _worker(night)._high_priority(_det(0))


def test_clip_de_escena_cubre_los_ultimos_segundos():
    w = _worker()
    now = time.time()
    for i in range(9):
        w._ring.append((now - 16 + 2 * i, np.zeros((270, 480, 3), np.uint8)))
    got = {}
    w.analyzer = SimpleNamespace(analyze_video=lambda frames, fps, ctx, **k: got.update(n=len(frames), fps=fps, ctx=ctx) or {"activity": "x"})
    w.buffer = None
    res, kind = w._analyze_as_video(np.zeros((1080, 1920, 3), np.uint8), {"person_count": 0})
    assert kind == "video" and got["n"] == vw.SCENE_FRAMES and got["ctx"]["_clip_s"] >= 14 and got["fps"] < 1


def test_rutina_no_llama_si_el_gateway_esta_saturado(monkeypatch):
    a = va.VLMAnalyzer("http://x")
    calls = []
    monkeypatch.setattr(a, "_request_once", lambda *x, **k: calls.append(1) or {"activity": "ok"})
    va._BUSY["until"] = time.time() + 30
    try:
        with va.priority(False):
            assert a._request({}, "t", 5).get("_busy_skip")
        with va.priority(True):
            assert a._request({}, "t", 5)["activity"] == "ok"
        assert calls == [1] and not va.gateway_busy()                     # la respuesta buena cierra la pausa
    finally:
        va._BUSY["until"] = 0.0


def test_503_de_capacidad_abre_la_pausa_y_la_rutina_no_reintenta(monkeypatch):
    a = va.VLMAnalyzer("http://x")
    calls = []

    def busy(*x, **k):
        calls.append(1)
        va._mark_busy("Chat admission capacity is temporarily unavailable")
        return {"activity": "error", "_retryable": True, "_busy503": True}
    monkeypatch.setattr(a, "_request_once", busy)
    try:
        with va.priority(False):
            a._request({}, "t", 5)
        assert calls == [1] and va.gateway_busy()
    finally:
        va._BUSY.update(until=0.0, n=0)


def test_texto_fijo_no_cambia_con_la_hora():
    ctx = scenes.SceneContext(cam_id="c", name="Cowork", where="oficina", focus=["a"], ignore=["b"], alert_types=frozenset(), vehicles=False,
                              night_person="alert")
    s1, d1 = ctx.render_parts(time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1)))
    s2, d2 = ctx.render_parts(time.mktime((2026, 10, 7, 23, 30, 0, 0, 0, -1)))
    assert s1 == s2 and "HORA LOCAL" not in s1 and "12:00" in d1 and "REGLA NOCTURNA" in d2
