"""Armado absoluto: el estado persiste, manda sobre el horario y las exenciones, y el cambio queda en la linea de tiempo."""
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_arming import router
from app.vision import arming, scenes


import pytest


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    arming.reset_for_tests()                                    # que el estado no se filtre a otras pruebas


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(arming, "PATH", str(tmp_path / "arming.json"))
    arming.reset_for_tests()


def test_state_persists_and_rejects_unknown_modes(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert arming.get()["mode"] == "normal" and not arming.is_absolute()
    arming.set_mode("absoluto", "1.2.3.4")
    arming.reset_for_tests()                                  # como tras un reinicio
    assert arming.is_absolute() and arming.get()["by"] == "1.2.3.4"
    try:
        arming.set_mode("loco")
        assert False
    except ValueError:
        pass


def test_absolute_mode_overrides_schedule_and_weekend(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ctx = scenes.SceneContext(cam_id="c", name="Cowork", where="", focus=[], ignore=[], alert_types=frozenset(), vehicles=False, night_person="note")
    noon = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))          # miercoles al mediodia, camara en 'note'
    assert not ctx.night_alert(noon)
    arming.set_mode("absoluto")
    assert ctx.night_alert(noon)
    arming.set_mode("normal")
    assert not ctx.night_alert(noon)


def test_api_toggles_and_logs_an_alert_event(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    events = []
    app = FastAPI()
    app.state.db = type("D", (), {"insert_event": lambda self, t, c, p, people=None, has_alert=False: events.append((t, c, p, has_alert)) or 1})()
    app.include_router(router)
    c = TestClient(app)
    assert c.post("/api/arming", json={"mode": "xx"}).status_code == 400
    assert c.post("/api/arming", json={"mode": "absoluto"}).json()["mode"] == "absoluto"
    c.post("/api/arming", json={"mode": "absoluto"})                  # repetir no genera otro evento
    assert len(events) == 1 and events[0][2]["alert_types"] == ["estado_armado"] and events[0][3]
    assert c.get("/api/arming").json()["mode"] == "absoluto"
