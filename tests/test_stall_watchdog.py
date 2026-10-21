"""Vigilante de cuadros: una captura colgada se libera para forzar la reconexion."""
import threading
import time
import types

from app.capture import rtsp_reader as rr


class Buf:
    def __init__(self, lat): self.lat = lat
    def latency_ms(self): return self.lat


class Cap:
    released = False
    def release(self): self.released = True


def reader(lat):
    r = rr.RTSPReader.__new__(rr.RTSPReader)
    r.cam, r.buffer, r._connected_since = types.SimpleNamespace(id="cam-x"), Buf(lat), time.time() - 600
    r._method = "GStreamer H264 + NVDEC (hardware)"
    return r


def test_sin_cuadros_nuevos_libera_la_captura(monkeypatch):
    monkeypatch.setenv("STREAM_STALL_S", "0.1")
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    cap = Cap()
    reader(25_000.0)._watchdog(cap, threading.Event())          # se libera y el vigilante termina
    assert cap.released


def test_con_cuadros_frescos_no_toca_nada(monkeypatch):
    monkeypatch.setenv("STREAM_STALL_S", "20")
    cap, done = Cap(), threading.Event()
    calls = {"n": 0}
    def fake_wait(self, timeout=None):
        calls["n"] += 1
        return calls["n"] > 2
    monkeypatch.setattr(threading.Event, "wait", fake_wait)
    reader(300.0)._watchdog(cap, done)
    assert not cap.released


def test_con_ffmpeg_no_libera_la_captura_desde_otro_hilo(monkeypatch):
    monkeypatch.setenv("STREAM_STALL_S", "0.1")
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    r, cap = reader(25_000.0), Cap()
    r._method = "OpenCV/FFmpeg (software, fallback)"
    r._watchdog(cap, threading.Event())
    assert not cap.released


def test_la_captura_ffmpeg_lleva_tiempo_maximo_de_lectura():
    src = open("app/capture/rtsp_reader.py", encoding="utf-8").read()
    assert "CAP_PROP_READ_TIMEOUT_MSEC" in src and "CAP_PROP_OPEN_TIMEOUT_MSEC" in src
