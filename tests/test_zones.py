"""Zonas por camara: poligonos, filtro de detecciones y validacion."""
import json

import pytest

from app.vision import zones


@pytest.fixture(autouse=True)
def tmp_zones(tmp_path, monkeypatch):
    monkeypatch.setattr(zones, "PATH", str(tmp_path / "zones.json"))
    zones._cache.update(mtime=None, checked=0.0, data={})
    yield


SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]


def test_point_in_polygon():
    assert zones.point_in_polygon(0.3, 0.3, SQUARE)
    assert not zones.point_in_polygon(0.7, 0.3, SQUARE)
    assert not zones.point_in_polygon(0.3, 0.9, SQUARE)


def test_ignore_objects_keeps_persons_and_drops_objects_inside():
    zones.save("cam-1", [{"type": "ignorar_objetos", "name": "palmera", "pts": SQUARE}])
    persons = [(200, 200, 300, 380)]                       # base en (250,380) de un cuadro 1000x800 -> (0.25, 0.47) dentro
    objects = [{"c": "perro", "conf": .7, "b": (200, 200, 300, 380)}, {"c": "auto", "conf": .8, "b": (700, 300, 900, 380)}]
    p, o = zones.filter_detections("cam-1", persons, objects, 1000, 800)
    assert p == persons and [x["c"] for x in o] == ["auto"]


def test_ignore_all_also_drops_persons():
    zones.save("cam-1", [{"type": "ignorar_todo", "pts": SQUARE}])
    p, o = zones.filter_detections("cam-1", [(200, 200, 300, 380)], [], 1000, 800)
    assert p == []


def test_other_cameras_are_untouched_and_parking_helpers():
    zones.save("cam-1", [{"type": "estacionamiento", "pts": SQUARE}])
    assert zones.has("cam-1", "estacionamiento") and not zones.has("cam-2", "estacionamiento")
    assert zones.contains("cam-1", ("estacionamiento",), (200, 200, 300, 380), 1000, 800)
    assert not zones.contains("cam-1", ("estacionamiento",), (700, 300, 900, 380), 1000, 800)
    objs = [{"c": "auto", "conf": .9, "b": (700, 300, 900, 380)}]
    assert zones.filter_detections("cam-2", [], objs, 1000, 800) == ([], objs)


def test_notes_and_roundtrip(tmp_path):
    zones.save("cam-1", [{"type": "nota", "name": "piso", "text": "piso de madera con reflejos", "pts": SQUARE}])
    assert zones.notes("cam-1") == [("piso", "piso de madera con reflejos")]
    assert json.load(open(zones.PATH))["cam-1"][0]["type"] == "nota"
    zones.save("cam-1", [])
    assert zones.get("cam-1") == []


@pytest.mark.parametrize("bad", [
    [{"type": "inventado", "pts": SQUARE}],
    [{"type": "nota", "pts": [[0, 0], [1, 1]]}],
    [{"type": "nota", "pts": [[0, 0], [2, 0], [1, 1]]}],
    [{"type": "nota", "pts": [["a", 0], [1, 0], [1, 1]]}],
])
def test_validation_rejects_bad_zones(bad):
    with pytest.raises(ValueError):
        zones.validate(bad)
