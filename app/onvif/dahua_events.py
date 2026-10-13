"""Eventos nativos de Dahua (timbre, llamada, puerta) por HTTP: GET eventManager.cgi?action=attach.

Cada evento llega como `Code=<codigo>;action=<Start|Stop|Pulse>;index=<n>;data={...json...}` (el JSON puede ocupar varias lineas).
Con la cuenta admin del equipo. El ONVIF del videoportero no anuncia el timbre.
"""
import json
import re
import time
import urllib.parse

import requests
import urllib3
from requests.auth import HTTPDigestAuth

urllib3.disable_warnings()
_HEAD = re.compile(r"Code=(\w+);action=(\w+);index=(\d+)(?:;data=(.*))?$")


class DahuaError(Exception):
    pass


def snapshot(host: str, user: str, password: str, channel: int = 1, timeout: float = 6.0) -> bytes:
    """JPEG actual del videoportero (snapshot.cgi, Digest). Sirve cuando el flujo RTSP aun no tiene cuadro."""
    r = requests.get(f"http://{host}/cgi-bin/snapshot.cgi?channel={channel}", auth=HTTPDigestAuth(user, password), timeout=timeout)
    if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
        raise DahuaError(f"snapshot HTTP {r.status_code}")
    return r.content


def parse_event(block: str):
    """Texto de un evento -> (code, action, index, data_dict) o None (latidos y bloques sin evento)."""
    text = " ".join(l.strip() for l in block.replace("\r", "").splitlines() if l.strip() and not l.startswith(("Content-", "--")))
    m = _HEAD.search(text)
    if not m:
        return None
    code, action, index, raw = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"raw": raw[:200]}
    return code, action, index, data


def stream_events(host: str, user: str, password: str, should_stop, on_event, timeout: float = 30.0) -> None:
    """Se queda leyendo eventos hasta que should_stop() sea verdadero o se corte la conexion (DahuaError)."""
    s = requests.Session()
    s.auth = HTTPDigestAuth(urllib.parse.unquote(user), urllib.parse.unquote(password))
    try:
        r = s.get(f"http://{host}/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5", stream=True, timeout=(5, timeout))
    except requests.RequestException as exc:
        raise DahuaError(f"sin respuesta: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise DahuaError(f"HTTP {r.status_code}")
    block: list = []
    try:
        for raw in r.iter_lines():
            if should_stop():
                return
            if raw is None:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw      # el equipo no declara charset: llegan bytes
            if line.startswith("--"):
                ev = parse_event("\n".join(block)) if block else None
                block = []
                if ev:
                    on_event(*ev)
                continue
            block.append(line)
    except requests.RequestException as exc:
        raise DahuaError(f"conexion cortada: {type(exc).__name__}") from exc
    finally:
        r.close()
    raise DahuaError("el equipo cerro la conexion")
