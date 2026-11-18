"""Cliente de Home Assistant (API REST). El token vive en data/ha.json (600) y nunca se devuelve al navegador.

Solo se permiten acciones de control de dispositivos (luces, enchufes, medios, cortinas...). Nada de reiniciar Home Assistant,
botones de equipos (el NAS tiene "reboot"/"shutdown") ni cambios de configuración.
"""
import threading
import time
from urllib.parse import urlsplit

import httpx

from hogar import files

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
        try:
            r = httpx.get(url + p, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant no responde: {type(exc).__name__}") from exc
        if r.status_code == 401:
            raise HAError("Home Assistant rechazó el token (401)")
        if r.status_code != 200:
            raise HAError(f"Home Assistant respondió {r.status_code}")
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
        try:
            r = httpx.post(f"{url}/api/services/{domain}/{service}", json=data, headers={"Authorization": f"Bearer {tok}"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant no responde: {type(exc).__name__}") from exc
        if r.status_code not in (200, 201):
            raise HAError(f"Home Assistant respondió {r.status_code}: {r.text[:120]}")
        with self._lock:
            self._states_ts = 0.0                       # el siguiente sondeo trae el estado nuevo
        return r.json() if r.content else []

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
