"""Reglas de alertas: estadisticas por tipo con etiquetas de revision."""
import json
import sqlite3
import threading
import time

from app.api import routes_rules as rr


class DB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, type TEXT, cam_id TEXT, has_alert INT, review_label TEXT, data TEXT)")


def test_stats_group_by_type_cam_and_review():
    db, now = DB(), time.time()
    rows = [("cam-1", "noise"), ("cam-1", "important"), ("cam-2", None), ("cam-1", None)]
    for cam, lab in rows:
        db._conn.execute("INSERT INTO events (ts, type, cam_id, has_alert, review_label, data) VALUES (?,?,?,?,?,?)",
                         (now, "nemotron", cam, 1, lab, json.dumps({"alert_types": ["merodeo"]})))
    s = rr._stats(db)["merodeo"]
    assert s["total"] == 4 and s["noise"] == 1 and s["important"] == 1 and s["sin"] == 2 and s["cams"] == {"cam-1": 3, "cam-2": 1}


def test_known_pets_suppress_animal_alert():
    from app.vision import scenes
    sc = scenes.SceneContext(cam_id="c", name="Cocina", where="", focus=[], ignore=[], alert_types=frozenset({"animal"}), night_person="note",
                             vehicles=False, catalog={"animal": "x"})
    res = {"activity": "Un perro cafe en la cocina", "alerts": ["Animal en la cocina"], "alert_types": ["animal"], "people": 0, "relevant": True}
    scenes.PETS_KNOWN = lambda cam: True
    try:
        out = scenes.normalize_result(dict(res), sc)
        assert out["alert_types"] == [] and out["alerts"] == [] and out["pet_known"] is True
        scenes.PETS_KNOWN = lambda cam: False
        assert scenes.normalize_result(dict(res), sc)["alert_types"] == ["animal"]
    finally:
        scenes.PETS_KNOWN = None


def test_person_with_bike_is_not_lying_on_the_ground():
    from app.vision import scenes
    sc = scenes.SceneContext(cam_id="c", name="Exterior 1", where="", focus=[], ignore=[], alert_types=frozenset({"merodeo", "persona_en_suelo"}), night_person="note",
                             vehicles=True, catalog={"merodeo": "x", "persona_en_suelo": "y"})
    res = {"activity": "Un hombre con ropa oscura yace en el suelo en la banqueta.", "alerts": ["Persona en la banqueta"], "alert_types": ["merodeo"], "people": 1,
           "relevant": True, "yolo_objects": {"bicicleta": 1}}
    out = scenes.normalize_result(res, sc, 1)
    assert out["alert_types"] == [] and out["alerts"] == [] and out["bike_in_scene"] is True


def test_parked_in_zone_majority_and_no_polygon():
    from app.vision.parked import ParkedTracker
    assert ParkedTracker._in_zone({"hits": 10, "zin": 6})
    assert not ParkedTracker._in_zone({"hits": 10, "zin": 2})
    assert ParkedTracker._in_zone({"hits": 10})            # sin dato de zona (sin poligono dibujado): cuenta como dentro


def test_visit_records_where_the_best_face_appeared():
    from types import SimpleNamespace
    from app.vision.visits import Track, VisitManager
    tr = Track(1, (100, 100, 200, 300), 0.0)
    tr.dims = (1000, 500)
    f = SimpleNamespace(bbox=(120, 110, 160, 160), area=2000.0, det_score=0.9, sharpness=80.0, yaw=0.0, name=None, confidence=None,
                        embedding=None, thumb_b64="AAAA")
    VisitManager._face_obs(tr, f)
    assert tr.face_xy == "0.150,0.400"
