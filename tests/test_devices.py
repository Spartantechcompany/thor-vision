"""Gestion de dispositivos: catalogo SUNAPI, validacion de tipos, politica de seguridad y bitacora."""
import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.devices import manager as dm
from app.devices import sunapi as sn
from app.onvif import registry

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sunapi_catalog_sample.xml")


@pytest.fixture(scope="module")
def cat():
    return sn.parse_catalog(open(FIX, encoding="utf-8").read())


def test_catalog_parses_groups_actions_and_types(cat):
    assert {"system", "network", "media", "security"} <= set(cat)
    date_set = cat["system"]["date"]["set"]
    assert date_set["access"] == "admin"
    sync = next(p for p in date_set["params"] if p["name"] == "SyncType")
    assert sync["type"] == "enum" and sync["options"] == ["NTP", "Manual"]
    month = next(p for p in date_set["params"] if p["name"] == "Month")
    assert (month["type"], month["min"], month["max"]) == ("int", 1, 12)
    assert next(p for p in date_set["params"] if p["name"] == "DSTEnable")["type"] == "bool"
    assert "view" in cat["system"]["deviceinfo"]


def test_classification_policy():
    assert sn.classify("system", "deviceinfo", "view") == "read"
    assert sn.classify("system", "date", "set") == "write"
    assert sn.classify("system", "power", "control") == "strong"                 # reiniciar
    assert sn.classify("network", "interface", "set") == "strong"                # puede cortar el acceso
    assert sn.classify("security", "users", "add/update") == "strong"
    assert sn.classify("media", "videoprofile", "set") == "strong"               # Thor lee por esos perfiles
    for sm in ("factoryreset", "firmwareupdate", "configrestore", "configbackup"):
        assert sn.classify("system", sm, "control") == "deny"
    assert sn.classify("opensdk", "install", "install") == "deny"
    assert sn.classify("security", "ssl", "install") == "deny"


def test_param_validation_uses_the_types_declared_by_the_device(cat):
    spec = cat["system"]["date"]["set"]
    assert sn.validate_params(spec, {"SyncType": "NTP", "Month": 5}) == {"SyncType": "NTP", "Month": "5"}
    for bad in ({"SyncType": "Magia"}, {"Month": 13}, {"Month": "x"}, {"DSTEnable": "quiza"}, {"NoExiste": 1}, {"NTPURLList": "a&Reboot=1"}):
        with pytest.raises(sn.SunapiError):
            sn.validate_params(spec, bad)


def test_redaction_masks_secrets():
    assert sn.redact({"Password": "x", "UserName": "a", "Key": "k", "Name": "n"}) == {"Password": "***", "UserName": "a", "Key": "***", "Name": "n"}


def test_parse_kv():
    assert sn.parse_kv("Model=QNO-8010R\nChannel.0.Name=Cam=1\nvacio\n") == {"Model": "QNO-8010R", "Channel.0.Name": "Cam=1"}


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "PATH", str(tmp_path / "onvif.json"))
    cfg = SimpleNamespace(cameras=[SimpleNamespace(id="cam-1", name="Cocina", rtsp_url="rtsp://u:p@10.0.0.5/x", zone="interior")])
    return dm.DeviceManager(FakeDB(), cfg)


def test_read_only_by_default_and_confirmation_levels(mgr):
    with pytest.raises(dm.PolicyError, match="solo lectura"):
        mgr._authorize("cam-1", "write", "si")
    registry.set_manage("cam-1", True)
    mgr._authorize("cam-1", "write", "si")
    with pytest.raises(dm.PolicyError, match="confirma"):
        mgr._authorize("cam-1", "write", "")
    with pytest.raises(dm.PolicyError, match="escribe el id"):
        mgr._authorize("cam-1", "strong", "otra")
    mgr._authorize("cam-1", "strong", "cam-1")
    with pytest.raises(dm.PolicyError, match="no se permite"):
        mgr._authorize("cam-1", "deny", "cam-1")


def test_audit_records_and_masks(mgr):
    mgr.audit("cam-1", "write", "system.date.set", {"SyncType": "NTP", "Password": "secreto"}, True, "ok", "10.0.0.9")
    a = mgr.audit_list("cam-1")[0]
    assert a["action"] == "system.date.set" and a["params"]["Password"] == "***" and a["brand"] == "hanwha" and a["ok"]


def test_blocked_action_is_audited_and_never_reaches_the_device(mgr, monkeypatch):
    class Boom:
        def spec(self, *a):
            raise AssertionError("no debe consultar al equipo")
    monkeypatch.setattr(mgr, "_sunapi", lambda dev_id: Boom())
    with pytest.raises(dm.PolicyError):
        mgr.sunapi_act("cam-1", "system", "date", "set", {"SyncType": "NTP"}, "si", "10.0.0.9")          # sin modo gestion
    with pytest.raises(dm.PolicyError):
        mgr.sunapi_act("cam-1", "system", "factoryreset", "control", {}, "cam-1", "10.0.0.9")             # denegada siempre
