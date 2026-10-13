"""Resuelve URLs `onvif://<equipo>/<perfil>` a la URL RTSP real con el usuario ONVIF del registro.

Asi cameras.yml no lleva claves para ese equipo y la rotacion del usuario desde /onvif aplica sola.
"""
import threading
import time
import urllib.parse

from app.onvif import client as oc
from app.onvif import registry

SCHEME = "onvif://"
_TTL = 300.0
_lock = threading.Lock()
_cache: dict = {}


def is_onvif(url: str) -> bool:
    return isinstance(url, str) and url.startswith(SCHEME)


def host_of(url: str):
    """Host del equipo ONVIF (para mostrar), sin resolver ni exponer nada."""
    if not is_onvif(url):
        return urllib.parse.urlparse(url).hostname
    ep = registry.get_custom(url[len(SCHEME):].split("/", 1)[0])
    return ep["host"] if ep else None


def resolve(url: str) -> str:
    """Devuelve la URL RTSP con credenciales inyectadas; lanza oc.OnvifError si no se puede."""
    if not is_onvif(url):
        return url
    key = (url, registry.VERSION[0])
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    ep_id, _, token = url[len(SCHEME):].partition("/")
    ep = registry.get_custom(ep_id)
    user, pw = registry.auth(ep_id)
    if ep is None or not user:
        raise oc.OnvifError(f"equipo ONVIF '{ep_id}' sin usuario configurado")
    host, port = ep["host"], int(ep.get("port", 80))
    off = oc.get_datetime(host, port)["skew_s"]
    services = oc.get_services(host, port, user, pw, off)
    media = oc._rewrite(services["media"], host, port)
    if not token:
        token = oc.get_profiles(media, user, pw, off)[0]["token"]
    uri = oc.get_stream_uri(media, token, user, pw, off)
    full = uri.replace("rtsp://", f"rtsp://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@", 1)
    with _lock:
        _cache[key] = (time.time(), full)
    return full
