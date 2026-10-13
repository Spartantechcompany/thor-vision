"""Gestion de camaras Hanwha por SUNAPI, construida sobre el catalogo que publica cada equipo (attributes.cgi/cgis).

El catalogo declara cada funcion (grupo > submenu > accion), su nivel de acceso y el tipo de cada parametro (enum, rango, bool,
texto), asi que la validacion y los formularios salen del propio equipo y sirven para cualquier modelo/firmware.
"""
import logging
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import urllib3
from requests.auth import HTTPDigestAuth

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

TIMEOUT = 12
CATALOG_TTL = 3600.0
_NAME = re.compile(r"^[A-Za-z0-9_.]{1,64}$")
_READ_ACTIONS = ("view", "check", "monitor", "monitordiff")
_SECRET = re.compile(r"pass|pwd|secret|key|token|community", re.I)

# --- politica -------------------------------------------------------------------------------------------------
DENY = {("system", "firmwareupdate"), ("system", "factoryreset"), ("system", "configrestore"), ("system", "configbackup"),
        ("security", "ssl"), ("security", "802Dot1x"), ("security", "rsa"), ("opensdk", None)}
STRONG_GROUPS = {"network", "security"}
STRONG = {("system", "power"), ("media", "videoprofile"), ("media", "videoprofilepolicy"), ("media", "videoencoderinstances")}


class SunapiError(Exception):
    pass


def classify(cgi: str, submenu: str, action: str) -> str:
    """read | write | strong | deny. strong = confirmacion escrita; deny = nunca desde aqui."""
    if action in _READ_ACTIONS:
        return "read"
    if (cgi, submenu) in DENY or (cgi, None) in DENY:
        return "deny"
    if cgi in STRONG_GROUPS or (cgi, submenu) in STRONG:
        return "strong"
    return "write"


def redact(params: dict) -> dict:
    return {k: ("***" if _SECRET.search(k) else v) for k, v in params.items()}


def _dtype(p: ET.Element) -> dict:
    dt = next(iter(p.iter("dataType")), None)
    if dt is None or not len(dt):
        return {"type": "string"}
    t = dt[0]
    if t.tag == "enum":
        return {"type": "enum", "options": [e.get("value") for e in t.iter("entry")]}
    if t.tag == "int":
        return {"type": "int", "min": int(t.get("min", "0")), "max": int(t.get("max", "2147483647"))}
    if t.tag == "bool":
        return {"type": "bool", "true": t.get("true", "True"), "false": t.get("false", "False")}
    if t.tag == "csv":
        return {"type": "csv"}
    return {"type": "string", "maxlen": int(t.get("maxlen", "0") or 0)}


def parse_catalog(xml_text: str) -> dict:
    """{cgi: {submenu: {action: {"access": str, "params": [{name, request, response, type, ...}]}}}}"""
    root = ET.fromstring(xml_text)
    out: dict = {}
    for cgi in root.iter("cgi"):
        for sm in cgi.iter("submenu"):
            for a in sm.findall("action"):
                params = [dict(name=p.get("name"), request=p.get("request") == "true", response=p.get("response") == "true", **_dtype(p))
                          for p in a.findall("parameter")]
                out.setdefault(cgi.get("name"), {}).setdefault(sm.get("name"), {})[a.get("name")] = {"access": a.get("accesslevel"), "params": params}
    return out


def validate_params(spec: dict, params: dict) -> dict:
    """Valida contra el tipo declarado por el equipo; devuelve los valores como texto listos para la URL."""
    by = {p["name"]: p for p in spec["params"] if p["request"]}
    out = {}
    for k, v in params.items():
        if not _NAME.match(str(k)):
            raise SunapiError(f"nombre de parametro invalido: {k!r}")
        p = by.get(k)
        if p is None:
            raise SunapiError(f"el equipo no declara el parametro {k}")
        s = str(v)
        if p["type"] == "enum" and s not in p["options"]:
            raise SunapiError(f"{k}: valor fuera de las opciones {p['options']}")
        if p["type"] == "int":
            try:
                n = int(s)
            except ValueError:
                raise SunapiError(f"{k}: debe ser un numero entero") from None
            if not p["min"] <= n <= p["max"]:
                raise SunapiError(f"{k}: fuera del rango {p['min']}-{p['max']}")
        if p["type"] == "bool" and s not in (p["true"], p["false"]):
            raise SunapiError(f"{k}: debe ser {p['true']} o {p['false']}")
        if p["type"] == "string" and p.get("maxlen") and len(s) > p["maxlen"]:
            raise SunapiError(f"{k}: maximo {p['maxlen']} caracteres")
        if "\n" in s or "\r" in s or "&" in s:
            raise SunapiError(f"{k}: caracteres no permitidos")
        out[k] = s
    return out


def parse_kv(text: str) -> dict:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv


class Sunapi:
    _catalogs: dict = {}
    _lock = threading.Lock()

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self._s = requests.Session()
        self._s.auth = HTTPDigestAuth(urllib.parse.unquote(user), urllib.parse.unquote(password))
        self._s.verify = False

    def _get(self, path: str, timeout: float = TIMEOUT) -> requests.Response:
        try:
            return self._s.get(f"http://{self.host}{path}", timeout=timeout)
        except requests.RequestException as exc:
            raise SunapiError(f"sin respuesta: {type(exc).__name__}") from exc

    def catalog(self, refresh: bool = False) -> dict:
        with self._lock:
            hit = self._catalogs.get(self.host)
            if hit and not refresh and time.time() - hit[0] < CATALOG_TTL:
                return hit[1]
        r = self._get("/stw-cgi/attributes.cgi/cgis", timeout=20)
        if r.status_code != 200:
            raise SunapiError(f"catalogo no disponible (HTTP {r.status_code})")
        cat = parse_catalog(r.text)
        with self._lock:
            self._catalogs[self.host] = (time.time(), cat)
        return cat

    def spec(self, cgi: str, submenu: str, action: str) -> dict:
        try:
            return self.catalog()[cgi][submenu][action]
        except KeyError:
            raise SunapiError(f"el equipo no soporta {cgi}.{submenu}.{action}") from None

    def call(self, cgi: str, submenu: str, action: str, params: dict | None = None) -> dict:
        for part in (cgi, submenu, action.split("/")[0]):
            if not _NAME.match(part):
                raise SunapiError("nombre invalido")
        q = {"msubmenu": submenu, "action": action.split("/")[0], **(params or {})}
        r = self._get(f"/stw-cgi/{cgi}.cgi?" + urllib.parse.urlencode(q))
        return {"status": r.status_code, "kv": parse_kv(r.text) if r.status_code == 200 else {}, "raw": r.text[:2000]}
