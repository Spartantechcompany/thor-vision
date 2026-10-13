"""Registro de endpoints ONVIF: las camaras de config + equipos extra (videoportero...), y el acceso ONVIF de cada uno.

El acceso se guarda en data/onvif.json (permisos 600) y NUNCA sale por la API.
"""
import json
import os
import threading
from urllib.parse import urlparse

PATH = os.environ.get("ONVIF_PATH", "/app/data/onvif.json")
_lock = threading.Lock()
VERSION = [0]      # sube cuando cambia el acceso de algun equipo (invalida las URLs resueltas)


def _read() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("endpoints", [])
    d.setdefault("auth", {})
    d.setdefault("manage", {})
    return d


def _write(d: dict) -> None:
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    os.chmod(PATH, 0o600)


def camera_endpoints(config) -> list:
    out = []
    for c in config.cameras:
        host = urlparse(c.rtsp_url).hostname
        if host and not c.rtsp_url.startswith("onvif://"):         # esos equipos ya son un endpoint propio (registro ONVIF)
            out.append({"id": c.id, "name": c.name, "kind": "camera", "host": host, "port": 80, "listen": False, "zone": c.zone})
    return out


def endpoints(config) -> list:
    with _lock:
        d = _read()
    extra = [dict(e, kind=e.get("kind", "device")) for e in d["endpoints"]]
    return camera_endpoints(config) + extra


def get(config, ep_id: str):
    return next((e for e in endpoints(config) if e["id"] == ep_id), None)


def get_custom(ep_id: str):
    with _lock:
        d = _read()
    return next((dict(e) for e in d["endpoints"] if e["id"] == ep_id), None)


def has_auth(ep_id: str) -> bool:
    with _lock:
        a = _read()["auth"].get(ep_id)
    return bool(a and a.get("user"))


def auth(ep_id: str) -> tuple:
    with _lock:
        a = _read()["auth"].get(ep_id) or {}
    return a.get("user", ""), a.get("password", "")


def set_auth(ep_id: str, user: str, password: str) -> None:
    with _lock:
        d = _read()
        d["auth"][ep_id] = {"user": user, "password": password}
        _write(d)
        VERSION[0] += 1


def clear_auth(ep_id: str) -> None:
    with _lock:
        d = _read()
        d["auth"].pop(ep_id, None)
        _write(d)


def add_endpoint(ep: dict) -> dict:
    with _lock:
        d = _read()
        if any(e["id"] == ep["id"] for e in d["endpoints"]):
            raise ValueError("ya existe un equipo con ese id")
        d["endpoints"].append(ep)
        _write(d)
    return ep


def remove_endpoint(ep_id: str) -> bool:
    with _lock:
        d = _read()
        n = len(d["endpoints"])
        d["endpoints"] = [e for e in d["endpoints"] if e["id"] != ep_id]
        d["auth"].pop(ep_id, None)
        _write(d)
        return len(d["endpoints"]) < n


def set_listen(ep_id: str, listen: bool) -> bool:
    with _lock:
        d = _read()
        for e in d["endpoints"]:
            if e["id"] == ep_id:
                e["listen"] = bool(listen)
                _write(d)
                return True
    return False


def is_manage(dev_id: str) -> bool:
    """Modo gestion: sin el, un equipo solo se puede consultar (default)."""
    with _lock:
        return bool(_read()["manage"].get(dev_id))


def set_manage(dev_id: str, enabled: bool) -> None:
    with _lock:
        d = _read()
        d["manage"][dev_id] = bool(enabled)
        _write(d)
