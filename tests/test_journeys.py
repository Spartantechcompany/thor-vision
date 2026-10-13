"""Viajes y marcha: reglas de enlace entre camaras y rasgos de la postura."""
import numpy as np

from app.vision import gait, journeys as jy

TOPO = {"building": ["cam-228", "cam-120", "cam-215"], "street": ["cam-vto"], "entry_cams": ["cam-228", "cam-vto"], "max_gap_s": 180, "fragment_gap_s": 30}


def vec(i):
    v = np.zeros(8, np.float32)
    v[i] = 1.0
    return v


def journey(**k):
    base = {"id": 1, "last_ts": 100.0, "last_cam": "cam-228", "subject_id": None, "embs": [vec(0)], "attrs": {"upper": "rojo", "lower": "azul"}}
    base.update(k)
    return base


def visit(**k):
    base = {"id": 9, "cam_id": "cam-120", "start_ts": 125.0, "end_ts": 140.0, "subject_id": None, "emb": vec(0), "attrs": {"upper": "rojo", "lower": "azul"}}
    base.update(k)
    return base


def test_same_body_close_in_time_links_and_different_body_does_not():
    assert jy.link_score(TOPO, journey(), visit()) >= jy.LINK_MIN
    assert jy.link_score(TOPO, journey(), visit(emb=vec(3), attrs={"upper": "verde", "lower": "negro"})) < jy.LINK_MIN


def test_impossible_links_are_rejected():
    assert jy.link_score(TOPO, journey(), visit(start_ts=400.0)) is None              # demasiado tarde
    assert jy.link_score(TOPO, journey(), visit(start_ts=50.0)) is None               # empieza antes de que termine el anterior
    assert jy.link_score(TOPO, journey(), visit(cam_id="cam-xyz")) is None            # camara fuera del mapa
    assert jy.link_score(TOPO, journey(subject_id=1), visit(subject_id=2)) is None    # dos identidades por rostro distintas
    assert jy.link_score(TOPO, journey(subject_id=1), visit(subject_id=1)) == 1.0


def test_same_camera_fragments_only_within_the_short_gap():
    assert jy.link_score(TOPO, journey(last_cam="cam-120"), visit(start_ts=120.0)) is not None
    assert jy.link_score(TOPO, journey(last_cam="cam-120"), visit(start_ts=160.0)) is None


def test_gait_features_find_cadence_and_reject_short_sequences():
    fps, f = 12.0, 1.8                                                   # 1.8 pasos por segundo
    seq = []
    for t in range(36):
        s = np.sin(2 * np.pi * f * t / fps)
        k = np.zeros((17, 3), np.float32)
        k[:, 2] = 0.9
        pts = {gait.LSH: (90, 100), gait.RSH: (110, 100), gait.LHI: (92, 160), gait.RHI: (108, 160), gait.LKN: (92, 200), gait.RKN: (108, 200),
               gait.LAN: (90 - 10 * s, 240 + 15 * s), gait.RAN: (110 + 10 * s, 240 - 15 * s), gait.LWR: (85, 150 + 10 * s), gait.RWR: (115, 150 - 10 * s)}
        for i, p in pts.items():
            k[i, :2] = p
        seq.append(k)
    out = gait.features(seq, fps)
    assert out is not None and abs(out[0][0] - f) < 0.35 and out[1] == 1.0
    assert gait.features(seq[:6], fps) is None


def test_auc_orders_distances():
    assert gait.auc([0.1, 0.2], [0.8, 0.9]) == 1.0
    assert gait.auc([0.8, 0.9], [0.1, 0.2]) == 0.0


def test_tuya_entry_matches_visit_already_in_view_and_accepts_several_cams():
    import sqlite3
    import threading
    from types import SimpleNamespace
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("CREATE TABLE person_visits (id INTEGER PRIMARY KEY, cam_id TEXT, start_ts REAL, end_ts REAL, fp INT DEFAULT 0, static INT DEFAULT 0, journey_id INT);")
    db = SimpleNamespace(_conn=conn, _lock=threading.RLock(), insert_event=lambda *a, **k: 1)
    lk = jy.JourneyLinker(db)
    lk.topo = {"tuya_entries": {"tuya-garaje": ["cam-236", "cam-113", "cam-228"], "door-recepcion": "cam-113"}, "tuya_window_s": 60}
    assert lk._entry_cams("tuya-garaje") == ["cam-236", "cam-113", "cam-228"] and lk._entry_cams("door-recepcion") == ["cam-113"] and lk._entry_cams("x") == []
    conn.execute("INSERT INTO person_visits (id, cam_id, start_ts, end_ts) VALUES (1,'cam-113',1000,1040)")          # ya estaba en cuadro cuando abrio la puerta
    lk.tuya_event("door-recepcion", 1008.0)
    lk._resolve_tuya(1100.0)
    assert conn.execute("SELECT state, visit_id FROM tuya_entries").fetchone() == ("matched", 1)
    lk.tuya_event("tuya-garaje", 2000.0)                                                                       # nadie en ninguna de las 3 camaras
    lk._resolve_tuya(2100.0)
    assert conn.execute("SELECT state FROM tuya_entries WHERE alias='tuya-garaje'").fetchone() == ("orphan",)


def test_dedupe_drops_boxes_contained_in_a_bigger_one():
    from app.vision.visits import _dedupe
    big, inner, other = (100, 100, 200, 300), (110, 110, 190, 200), (400, 100, 480, 300)
    assert _dedupe([big, inner, other]) == [big, other]
