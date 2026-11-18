"""Cliente de Home Assistant (API REST). El token vive en data/ha.json (600) y nunca se devuelve al navegador.

Solo se permiten acciones de control de dispositivos (luces, enchufes, medios, cortinas...). Nada de reiniciar Home Assistant,
botones de equipos (el NAS tiene "reboot"/"shutdown") ni cambios de configuración.
"""
import json
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

from patcc import consola, files

CFG = "ha.json"
TIMEOUT = 8.0

# dominio -> acciones permitidas
ALLOWED = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "media_player": {"turn_on", "turn_off", "media_play", "media_pause", "media_play_pause", "media_next_track", "media_previous_track",
                     "volume_set", "volume_mute", "play_media", "select_source"},
    "remote": {"turn_on", "turn_off", "send_command"},
    "scene": {"turn_on"},
    "script": {"turn_on"},
    "automation": {"trigger", "turn_on", "turn_off"},
    "tts": {"speak"},
}
# dominios que se muestran en el panel
SHOWN = {"light", "switch", "fan", "cover", "media_player", "remote", "scene", "script", "automation", "binary_sensor", "sensor",
         "person", "device_tracker", "weather", "camera", "tts", "sun"}
SENSOR_CLASSES = {"temperature", "humidity", "battery", "power", "energy", "illuminance", "carbon_dioxide", "pm25", "voltage", "current"}


# switches de Home Assistant que NO son iluminación y sí se muestran (lista de entity_id separada por comas); por defecto ninguno
SWITCH_ALLOW = {x.strip() for x in os.environ.get("PATCC_HA_SWITCHES", "").split(",") if x.strip()}
_DEVINFO_TPL = """{% set o = namespace(r=[]) %}{% for s in (states.light | list) + (states.switch | list) %}{% set e = s.entity_id %}
{% set o.r = o.r + [{'id': e, 'dev': device_attr(e,'name'), 'ids': (device_attr(e,'identifiers') | list), 'area': area_name(e), 'tuya': e in integration_entities('tuya')}] %}{% endfor %}{{ o.r | tojson }}"""


class HAError(Exception):
    pass


def _check_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("la dirección debe ser http(s)://host:puerto")
    return url


class HA:
    def __init__(self):
        self._lock = threading.Lock()
        self._states: list = []
        self._states_ts = 0.0
        self.last_error = ""
        self._dev = {"ts": 0.0, "map": {}}

    # ── configuración ──────────────────────────────────────────────
    @staticmethod
    def _cfg() -> dict:
        return files.load(CFG, {}) or {}

    @property
    def configured(self) -> bool:
        c = self._cfg()
        return bool(c.get("url") and c.get("token"))

    def info(self) -> dict:
        c = self._cfg()
        tok = c.get("token") or ""
        return {"configured": self.configured, "url": c.get("url", ""), "token_hint": ("…" + tok[-4:]) if tok else "",
                "saved_by": c.get("saved_by"), "saved_ts": c.get("saved_ts"), "last_error": self.last_error}

    def token_info(self) -> dict:
        """Fechas de creación y vencimiento leídas del propio token (JWT) solo para mostrarlas; el token nunca sale del servidor."""
        import base64
        import json
        from datetime import datetime
        tok = self._cfg().get("token") or ""
        try:
            part = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (IndexError, ValueError):
            return {}
        out = {}
        for k, name in (("iat", "created"), ("exp", "expires")):
            if isinstance(claims.get(k), (int, float)):
                out[name] = datetime.fromtimestamp(claims[k]).strftime("%Y-%m-%d")
        if isinstance(claims.get("exp"), (int, float)):
            out["days_left"] = round((claims["exp"] - time.time()) / 86400)
        return out

    def save(self, url: str, token: str, who: str) -> dict:
        """Prueba la conexión antes de guardar; si falla no se guarda nada."""
        url = _check_url(url)
        token = (token or "").strip()
        if len(token) < 20:
            raise ValueError("token incompleto")
        r = self._get(url, token, "/api/config")
        files.save(CFG, {"url": url, "token": token, "saved_by": who, "saved_ts": time.time()}, secret=True)
        with self._lock:
            self._states_ts = 0.0
        return {"version": r.get("version"), "location": r.get("location_name")}

    def forget(self) -> None:
        files.save(CFG, {}, secret=True)
        with self._lock:
            self._states, self._states_ts = [], 0.0

    # ── HTTP ───────────────────────────────────────────────────────
    @staticmethod
    def _get(url: str, token: str, p: str):
        t0 = time.time()
        try:
            r = httpx.get(url + p, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            consola.log("error", "ha", f"GET {p} sin respuesta: {type(exc).__name__}")
            raise HAError(f"Home Assistant no responde: {type(exc).__name__}") from exc
        ms = round((time.time() - t0) * 1000)
        if r.status_code == 401:
            consola.log("error", "ha", f"GET {p} → 401 (token rechazado) {ms} ms")
            raise HAError("Home Assistant rechazó el token (401)")
        if r.status_code != 200:
            consola.log("error", "ha", f"GET {p} → {r.status_code} {ms} ms")
            raise HAError(f"Home Assistant respondió {r.status_code}")
        consola.log("debug", "ha", f"GET {p} → 200 {ms} ms")
        return r.json()

    def _conn(self):
        c = self._cfg()
        if not (c.get("url") and c.get("token")):
            raise HAError("Home Assistant sin configurar: agrega la dirección y el token en Ajustes")
        return c["url"], c["token"]

    def config(self) -> dict:
        url, tok = self._conn()
        return self._get(url, tok, "/api/config")

    def states(self, max_age: float = 2.0) -> list:
        with self._lock:
            if time.time() - self._states_ts < max_age:
                return self._states
        url, tok = self._conn()
        try:
            st = self._get(url, tok, "/api/states")
            self.last_error = ""
        except HAError as exc:
            self.last_error = str(exc)
            raise
        with self._lock:
            self._states, self._states_ts = st, time.time()
        return st

    def state(self, entity_id: str):
        return next((e for e in self.states() if e.get("entity_id") == entity_id), None)

    def call(self, domain: str, service: str, data: dict) -> list:
        """Llama una acción permitida. Valida que las entidades existan y sean del dominio correcto."""
        if service not in ALLOWED.get(domain, set()):
            raise ValueError(f"acción no permitida: {domain}.{service}")
        data = dict(data or {})
        ents = data.get("entity_id")
        ents = [ents] if isinstance(ents, str) else list(ents or [])
        if not ents:
            raise ValueError("falta entity_id")
        known = {e["entity_id"] for e in self.states(max_age=10)}
        for e in ents:
            if e not in known:
                raise ValueError(f"entidad desconocida: {e}")
            if e.split(".")[0] != domain:
                raise ValueError(f"{e} no es del dominio {domain}")
        if domain == "tts":
            mp = data.get("media_player_entity_id")
            if not (isinstance(mp, str) and mp in known and mp.startswith("media_player.")):
                raise ValueError("falta el reproductor donde anunciar")
            data["message"] = str(data.get("message") or "")[:300]
        url, tok = self._conn()
        t0 = time.time()
        try:
            r = httpx.post(f"{url}/api/services/{domain}/{service}", json=data, headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            consola.log("error", "ha", f"{domain}.{service} {ents} sin respuesta: {type(exc).__name__}")
            raise HAError(f"Home Assistant no responde: {type(exc).__name__}") from exc
        ms = round((time.time() - t0) * 1000)
        if r.status_code not in (200, 201):
            consola.log("error", "ha", f"{domain}.{service} {ents} → {r.status_code} {ms} ms")
            raise HAError(f"Home Assistant respondió {r.status_code}: {r.text[:120]}")
        consola.log("info", "ha", f"{domain}.{service} {ents} → {r.status_code} {ms} ms")
        with self._lock:
            self._states_ts = 0.0                       # el siguiente sondeo trae el estado nuevo
        return r.json() if r.content else []

    def devinfo(self, max_age: float = 60.0) -> dict:
        """entity_id -> {dev, tuya_id, area} de las luces e interruptores, según el registro de dispositivos de Home Assistant."""
        with self._lock:
            if time.time() - self._dev["ts"] < max_age:
                return self._dev["map"]
        url, tok = self._conn()
        t0 = time.time()
        try:
            r = httpx.post(url + "/api/template", json={"template": _DEVINFO_TPL}, headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
            r.raise_for_status()
            rows = json.loads(r.text)
        except (httpx.HTTPError, ValueError) as exc:
            consola.log("warn", "ha", f"no se pudo leer el registro de dispositivos: {type(exc).__name__}")
            return self._dev["map"]
        out = {}
        for x in rows:
            tid = next((i[1] for i in x.get("ids") or [] if len(i) == 2 and i[0] == "tuya"), None)
            out[x["id"]] = {"dev": (x.get("dev") or "").strip(), "tuya_id": tid, "area": x.get("area")}
        with self._lock:
            self._dev = {"ts": time.time(), "map": out}
        consola.log("debug", "ha", f"registro de dispositivos: {len(out)} entidades ({round((time.time() - t0) * 1000)} ms)")
        return out

    # ── vista para el panel ────────────────────────────────────────
    def view(self) -> list:
        out = []
        for e in self.states():
            eid = e.get("entity_id", "")
            dom = eid.split(".")[0]
            if dom not in SHOWN:
                continue
            a = e.get("attributes") or {}
            if dom == "sensor" and a.get("device_class") not in SENSOR_CLASSES:
                continue
            item = {"id": eid, "domain": dom, "name": a.get("friendly_name") or eid, "state": e.get("state"),
                    "class": a.get("device_class"), "unit": a.get("unit_of_measurement")}
            if dom in ("light", "switch"):
                di = self.devinfo().get(eid) if self.configured else None
                if di:
                    item["dev"], item["tuya_id"], item["area"] = di["dev"], di["tuya_id"], di["area"]
            if dom == "light":
                item.update(brightness=a.get("brightness"), rgb=a.get("rgb_color"), modes=a.get("supported_color_modes"),
                            kelvin=a.get("color_temp_kelvin"), min_k=a.get("min_color_temp_kelvin"), max_k=a.get("max_color_temp_kelvin"))
            elif dom == "media_player":
                item.update(volume=a.get("volume_level"), muted=a.get("is_volume_muted"), title=a.get("media_title"),
                            artist=a.get("media_artist"), source=a.get("source"), sources=(a.get("source_list") or [])[:40],
                            app=a.get("app_name"), features=a.get("supported_features"))
            elif dom == "cover":
                item.update(position=a.get("current_position"))
            elif dom == "sun":
                item.update(next_setting=a.get("next_setting"), next_rising=a.get("next_rising"))
            elif dom == "weather":
                item.update(temperature=a.get("temperature"), humidity=a.get("humidity"))
            out.append(item)
        return out
