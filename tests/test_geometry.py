"""Geometria y fusion de huecos: base de visitas, vehiculos estacionados y asistencia."""
import numpy as np

from app.api.routes_people_admin import _merge
from app.vision import parked, visits


def test_iou_basics():
    a = (0, 0, 10, 10)
    assert visits._iou(a, a) == 1.0
    assert visits._iou(a, (20, 20, 30, 30)) == 0.0
    assert 0.3 < visits._iou(a, (5, 0, 15, 10)) < 0.4
    assert visits._iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_parked_match_tolerates_yolo_jitter():
    a = (100, 100, 300, 250)
    jitter = (110, 96, 312, 256)           # un auto quieto, caja movida
    assert parked._match(a, jitter) >= parked.MIN_IOU
    assert parked._match(a, (600, 100, 800, 250)) < parked.MIN_IOU


def test_parked_match_rejects_very_different_size_nearby():
    assert parked._match((100, 100, 300, 250), (190, 150, 215, 175)) < parked.MIN_IOU


def test_face_yaw_frontal_and_profile():
    frontal = np.array([[30, 40], [70, 40], [50, 55], [35, 70], [65, 70]], np.float32)
    profile = np.array([[30, 40], [70, 40], [75, 55], [35, 70], [65, 70]], np.float32)
    assert abs(visits.face_yaw(frontal)) < 0.05
    assert visits.face_yaw(profile) > 0.5
    assert visits.face_yaw(None) == 1.0
    assert visits.face_yaw([[1, 1], [1, 1], [1, 1]]) == 1.0


def test_merge_intervals_gap_rule():
    assert _merge([(0, 100), (500, 700)], gap=600) == [[0, 700]]       # hueco < 10 min = presencia continua
    assert _merge([(0, 100), (800, 900)], gap=600) == [[0, 100], [800, 900]]
    assert _merge([(50, 60), (0, 10)], gap=5) == [[0, 10], [50, 60]]    # desordenado
    assert _merge([]) == []
