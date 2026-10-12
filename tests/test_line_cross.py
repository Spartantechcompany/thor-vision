"""Cruce de la linea virtual IVA: flanco de subida de VideoAnalytics.Passing con enfriamiento."""
import time

from app.capture.camera_motion import CameraMotionMonitor


def mon(cb):
    m = CameraMotionMonitor("cam-189", "rtsp://u:p@10.0.0.1/profile1/media.smp")
    m.cross_cb = cb
    return m


def test_el_cruce_dispara_una_vez_por_flanco(monkeypatch):
    calls = []
    m = mon(lambda cam: calls.append(cam))
    seq = iter(["Channel.0.VideoAnalytics.Passing=True", "Channel.0.VideoAnalytics.Passing=True", "Channel.0.VideoAnalytics.Passing=False",
                "Channel.0.VideoAnalytics.Passing=True"])
    monkeypatch.setattr(m, "_get", lambda *a, **k: next(seq))
    for _ in range(4):
        m._poll_once()
    time.sleep(0.2)
    assert calls == ["cam-189"]                      # el segundo cruce cae dentro del enfriamiento de 15 s


def test_sin_callback_no_falla(monkeypatch):
    m = mon(None)
    monkeypatch.setattr(m, "_get", lambda *a, **k: "Channel.0.VideoAnalytics.Passing=True")
    m._poll_once()
    assert m._active
