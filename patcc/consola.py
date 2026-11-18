"""Registro técnico de PAT·CC para devops: buffer en memoria (lo que ve /consola) y archivo rotativo en data/consola.log.

Distinto de la bitácora (files.log), que es de negocio: quién ordenó qué. Aquí van llamadas a Home Assistant y a Tuya con su
tiempo, errores, sincronizaciones y horarios. Nunca se escriben tokens, claves ni cuerpos de respuesta completos.
"""
import itertools
import logging
import logging.handlers
import os
import threading
import time
from collections import deque

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_buf: deque = deque(maxlen=4000)
_ids = itertools.count(1)
_lock = threading.Lock()
_file = None


def _logger():
    global _file
    if _file is None:
        _file = logging.getLogger("patcc.consola")
        _file.setLevel(logging.DEBUG)
        _file.propagate = False
        try:
            d = os.environ.get("PATCC_DATA", "/srv/data")
            os.makedirs(d, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(os.path.join(d, "consola.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            _file.addHandler(h)
        except OSError:
            pass
    return _file


def log(level: str, src: str, msg: str) -> None:
    level = level if level in LEVELS else "info"
    msg = str(msg).replace("\n", " ")[:400]
    with _lock:
        e = {"id": next(_ids), "ts": time.time(), "level": level, "src": src, "msg": msg}
        _buf.append(e)
    _logger().log(LEVELS[level], "%s %-5s %-8s %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), level, src, msg)


def entries(since: int = 0, level: str = "debug", src: str = "", q: str = "", limit: int = 500) -> list:
    floor = LEVELS.get(level, 10)
    q = q.lower()
    with _lock:
        rows = [e for e in _buf if e["id"] > since]
    out = [e for e in rows if LEVELS[e["level"]] >= floor and (not src or e["src"] == src) and (not q or q in e["msg"].lower() or q in e["src"])]
    return out[-limit:]


def sources() -> list:
    with _lock:
        return sorted({e["src"] for e in _buf})
