"""Cliente ONVIF y registro de endpoints: sin secretos en salidas y firma WS-Security correcta."""
import base64
import hashlib
import os
import re
import stat
from types import SimpleNamespace

import pytest

from app.onvif import client, registry


def test_clean_uri_strips_credentials():
    assert client.clean_uri("rtsp://user:pa55@192.168.0.3:554/cam?x=1") == "rtsp://192.168.0.3:554/cam?x=1"
    assert client.clean_uri("rtsp://192.168.0.3/cam") == "rtsp://192.168.0.3/cam"


def test_envelope_password_digest_is_verifiable():
    xml = client._envelope("<tds:GetDeviceInformation/>", "gigi", "secreto", offset=0).decode()
    nonce = base64.b64decode(re.search(r"<Nonce[^>]*>([^<]+)</Nonce>", xml).group(1))
    created = re.search(r"<u:Created>([^<]+)</u:Created>", xml).group(1)
    digest = re.search(r"<Password[^>]*>([^<]+)</Password>", xml).group(1)
    assert digest == base64.b64encode(hashlib.sha1(nonce + created.encode() + b"secreto").digest()).decode()
    assert "secreto" not in xml                                   # la clave nunca viaja en claro
    assert "<Security" not in client._envelope("<x/>").decode()    # sin usuario, sin cabecera de seguridad


def test_xml_special_chars_in_user_are_escaped():
    assert "&lt;" in client._envelope("<x/>", "a<b", "p").decode()


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "PATH", str(tmp_path / "onvif.json"))
    return registry


def test_registry_auth_is_private_and_persistent(reg):
    reg.set_auth("vto", "gigi", "x")
    assert reg.has_auth("vto") and reg.auth("vto") == ("gigi", "x")
    assert stat.S_IMODE(os.stat(reg.PATH).st_mode) == 0o600
    reg.clear_auth("vto")
    assert not reg.has_auth("vto")


def test_registry_endpoints_merge_cameras_without_credentials(reg):
    cfg = SimpleNamespace(cameras=[SimpleNamespace(id="cam-1", name="Cocina", rtsp_url="rtsp://u:p@192.168.0.50:554/x", zone="interior")])
    reg.add_endpoint({"id": "vto", "name": "Videoportero", "kind": "vto", "host": "192.168.0.3", "port": 80, "listen": True})
    eps = reg.endpoints(cfg)
    assert [e["id"] for e in eps] == ["cam-1", "vto"] and eps[0]["host"] == "192.168.0.50"
    assert "p" not in str(eps[0].values()).split("192.168.0.50")[0][-3:] and "rtsp_url" not in eps[0]
    with pytest.raises(ValueError):
        reg.add_endpoint({"id": "vto", "name": "x", "host": "h"})
    assert reg.set_listen("vto", False) and not reg.get(cfg, "vto")["listen"]
    assert reg.remove_endpoint("vto") and reg.get(cfg, "vto") is None


def test_resolve_injects_credentials_and_follows_rotation(reg, monkeypatch):
    from app.onvif import resolve
    reg.add_endpoint({"id": "vto", "name": "V", "kind": "vto", "host": "192.168.0.3", "port": 80})
    reg.set_auth("vto", "gigi", "p@ss:1")
    monkeypatch.setattr(client, "get_datetime", lambda h, p=80: {"skew_s": 0})
    monkeypatch.setattr(client, "get_services", lambda h, p, u, pw, o=0: {"media": "http://10.0.0.9/onvif/media_service"})
    monkeypatch.setattr(client, "get_stream_uri", lambda m, t, u, pw, o=0: "rtsp://192.168.0.3:554/cam/realmonitor?channel=1&subtype=1")
    resolve._cache.clear()
    url = resolve.resolve("onvif://vto/MediaProfile00001")
    assert url == "rtsp://gigi:p%40ss%3A1@192.168.0.3:554/cam/realmonitor?channel=1&subtype=1"
    assert resolve.host_of("onvif://vto/x") == "192.168.0.3" and resolve.host_of("rtsp://u:p@1.2.3.4/x") == "1.2.3.4"
    reg.set_auth("vto", "gigi", "nueva")                            # rotacion: la URL cacheada ya no vale
    assert "nueva" in resolve.resolve("onvif://vto/MediaProfile00001")
    assert resolve.resolve("rtsp://a/b") == "rtsp://a/b"


def test_resolve_without_user_fails_cleanly(reg):
    from app.onvif import resolve
    reg.add_endpoint({"id": "vto2", "name": "V", "kind": "vto", "host": "1.1.1.1"})
    with pytest.raises(client.OnvifError):
        resolve.resolve("onvif://vto2/x")


def test_pull_waits_longer_than_the_device_holds_the_request(monkeypatch):
    seen = {}

    def fake_soap(url, body, user="", password="", offset=0.0, timeout=client.TIMEOUT):
        seen["timeout"] = timeout
        return client.ET.fromstring("<a/>")
    monkeypatch.setattr(client, "soap", fake_soap)
    client.pull("http://x/pp", "u", "p", seconds=10)
    assert seen["timeout"] > 10
