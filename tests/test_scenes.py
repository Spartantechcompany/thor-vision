"""Contrato del saneador de respuestas del VLM: lo que ya fallo en produccion no debe volver."""
import time

import pytest

from app.vision import scenes
from app.vision.scenes import SceneContext, normalize_result

CATALOG = {"persona_nocturna": "x", "persona_en_suelo": "x", "animal": "x", "merodeo": "x"}


def ctx(**kw):
    base = dict(cam_id="cam-t", name="T", where="w", focus=[], ignore=[], alert_types=frozenset(CATALOG),
                night_person="alert", vehicles=False, catalog=CATALOG)
    base.update(kw)
    return SceneContext(**base)


def night_ts():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 2, 0, 0, 0, 0, -1))


def test_error_result_passes_through():
    r = {"activity": "error"}
    assert normalize_result(r, ctx()) is r


def test_activity_never_empty_or_placeholder():
    r = normalize_result({"people": 0, "activity": "Sin actividad relevante.", "scene": "luz tenue"}, ctx())
    assert r["activity"].startswith("Sin personas ni movimiento en cuadro")
    assert "luz tenue" in r["activity"]


def test_alert_about_detectors_is_dropped():
    r = normalize_result({"people": 1, "relevant": True, "alerts": ["el detector YOLO ve otra cosa"],
                          "alert_types": ["merodeo"], "severity": "high"}, ctx())
    assert r["alerts"] == [] and r["severity"] == "none"


def test_person_alert_without_person_is_removed():
    r = normalize_result({"people": 0, "relevant": True, "alerts": ["alguien merodea"],
                          "alert_types": ["merodeo"]}, ctx())
    assert r["alert_types"] == [] and r["alerts"] == []


def test_floor_alert_needs_yolo_person():
    base = {"people": 1, "relevant": True, "alerts": ["persona en el suelo"], "alert_types": ["persona_en_suelo"],
            "severity": "high"}
    assert normalize_result(dict(base), ctx(), yolo_people=0)["alerts"] == []
    assert normalize_result(dict(base), ctx(), yolo_people=1, posture={"max_aspect": 0.4})["alerts"] == []
    ok = normalize_result(dict(base), ctx(), yolo_people=1, posture={"max_aspect": 1.4})
    assert ok["alert_types"] == ["persona_en_suelo"] and ok["severity"] == "high"


def test_known_pet_suppresses_animal_alert():
    r = normalize_result({"people": 0, "relevant": True, "alerts": ["Akamaru en el patio"], "alert_types": ["animal"]},
                         ctx(known_pets=["Akamaru"]))
    assert r["alert_types"] == [] and r["alerts"] == []


def test_types_outside_camera_catalog_are_dropped():
    r = normalize_result({"people": 1, "relevant": True, "alerts": ["x"], "alert_types": ["inventado", "merodeo"]},
                         ctx(alert_types=frozenset({"merodeo"})))
    assert r["alert_types"] == ["merodeo"]


def test_severity_is_coherent_with_alerts():
    r = normalize_result({"people": 1, "relevant": True, "alerts": ["x"], "alert_types": ["merodeo"], "severity": "none"}, ctx())
    assert r["severity"] == "medium"
    r2 = normalize_result({"people": 0, "alerts": [], "severity": "high", "confidence": "raro"}, ctx())
    assert r2["severity"] == "none" and r2["confidence"] == "medium"


def test_fields_are_clipped_and_schema_marked():
    r = normalize_result({"people": 500, "activity": "a" * 999, "persons": [{"desc": "d" * 300}] * 20}, ctx(), yolo_people=2)
    assert r["people"] == 0 and len(r["activity"]) <= 240 and len(r["persons"]) <= 6
    assert all(len(p["desc"]) <= 90 for p in r["persons"])
    assert r["schema"] == 2 and r["yolo_people"] == 2 and r["people_mismatch"] is True


def test_night_rule_by_hour():
    c = ctx(night_hours=(22, 6), night_person="alert")
    assert c.night_alert(night_ts()) is True
    assert ctx(night_person="note").night_alert(night_ts()) is False


def test_real_scenes_file_loads():
    s = scenes.Scenes()
    assert s.get("cam-189") is not None
    assert all(c.alert_types <= frozenset(c.catalog) for c in s._by_cam.values())
