"""El analisis de video manda los cuadros como imagenes cuando el gateway descarta video_url."""
import numpy as np

from app.vision import vlm_analyzer as va


def analyzer(captured):
    a = va.VLMAnalyzer.__new__(va.VLMAnalyzer)
    a.video_timeout, a.video_user_prompt = 30, "Analiza la secuencia."
    a._request = lambda part, text, timeout, **k: captured.update(part=part, text=text) or {"people": 0}
    a._finish = lambda res, scene, ctx: res
    a._person_crop_part = lambda *x, **k: None
    a._build_context_prefix = lambda ctx: ""
    a._target_dims = lambda w, h: (w, h)
    return a


def test_el_video_se_envia_como_cuadros_en_orden(monkeypatch):
    monkeypatch.setattr(va, "VIDEO_AS_FRAMES", True)
    got = {}
    frames = [np.full((360, 640, 3), i * 10, np.uint8) for i in range(20)]
    analyzer(got).analyze_video(frames, 10.0)
    assert [p["type"] for p in got["part"]] == ["image_url"] * va.VIDEO_FRAMES
    assert "cronologico" in got["text"] and "~2 s" in got["text"]


def test_con_la_opcion_apagada_sigue_mandando_video(monkeypatch):
    monkeypatch.setattr(va, "VIDEO_AS_FRAMES", False)
    got = {}
    a = analyzer(got)
    a._encode_mp4 = lambda fr, fps: b"mp4"
    a.analyze_video([np.zeros((36, 64, 3), np.uint8)] * 4, 10.0)
    assert got["part"]["type"] == "video_url"
