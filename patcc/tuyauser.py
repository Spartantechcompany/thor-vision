"""API de usuario final de Tuya (la que usan los «OpenClaw skills» de Tuya), con una API Key `sk-…`.

A diferencia del proyecto de la nube (que solo controla los equipos de su «cupo»), esta API actúa como el usuario de la app:
lista las casas y equipos de la cuenta y los controla sin ese cupo. Documentación: github.com/tuya/tuya-openclaw-skills.

- Autenticación: `Authorization: Bearer sk-…`. La región sale de las dos letras tras `sk-` (AZ = EE. UU., AY = China, EU, IN, UE, WE, SG).
- La clave vive en data/tuya_user.json (600) y nunca se devuelve al navegador ni se escribe en la consola.
- Casa: solo se usan las casas cuyo id es PATCC_TUYA_HOME o cuyo nombre es PATCC_TUYA_HOME_NAME (Patricio 10); nunca «todos los equipos».
- Las órdenes no se reintentan solas (un reintento podría repetir el comando). Los valores se validan contra el modelo del equipo.
"""
import json
import os
import threading
import time

import httpx

from patcc import consola, files

CFG = "tuya_user.json"
HOME_ID = os.environ.get("PATCC_TUYA_HOME", "153519480")
HOME_NAME = os.environ.get("PATCC_TUYA_HOME_NAME", "Patricio 10").strip().lower()
TIMEOUT = 12.0
LIST_TTL = 120.0
BASE = {"AY": "https://openapi.tuyacn.com", "AZ": "https://openapi.tuyaus.com", "EU": "https://openapi.tuyaeu.com", "IN": "https://openapi.tuyain.com",
        "UE": "https://openapi-ueaz.tuyaus.com", "WE": "https://openapi-weaz.tuyaeu.com", "SG": "https://openapi-sg.iotbing.com"}
# códigos que el panel puede enviar por esta vía (los focos siguen por la red local)
USER_CODES = ("switch", "switch_1", "switch_2", "switch_3", "switch_4", "switch_5", "switch_6", "switch_led", "control", "percent_control")
ERRORS = {1010: "la API Key es inválida o venció", 10011: "falta un contacto vinculado en la app de Tuya (teléfono o correo)",
          40000901: "el equipo no existe en esta cuenta", 40000903: "el equipo no tiene modelo"}


class TuyaUserError(Exception):
    pass


def region_url(key: str) -> str:
    k = (key or "").strip()
    if not k.startswith("sk-") or len(k) < 20:
        raise ValueError("la clave debe empezar con sk- y estar completa")
    url = BASE.get(k[3:5].upper())
    if not url:
        raise ValueError(f"región desconocida en la clave: {k[3:5]}")
    return url


class TuyaUser:
    def __init__(self):
        self._lock = threading.Lock()
        self._list = {"ts": 0.0, "devices": {}, "homes": []}
        self._models: dict = {}
        self.overlay: dict = {}            # device_id -> {"props": {...}, "ts": float}: estado leído tras una orden
        self.last_error = ""

    # ── configuración ──────────────────────────────────────────────
    @staticmethod
    def _cfg() -> dict:
        return files.load(CFG, {}) or {}

    @property
    def configured(self) -> bool:
        c = self._cfg()
        return bool(c.get("key"))

    def info(self) -> dict:
        c = self._cfg()
        k = c.get("key") or ""
        return {"configured": bool(k), "key_hint": ("…" + k[-4:]) if k else "", "endpoint": c.get("endpoint", ""), "saved_by": c.get("saved_by"),
                "saved_ts": c.get("saved_ts"), "last_error": self.last_error, "home": HOME_NAME.title(),
                "devices": len(self._list["devices"]), "homes": [h["name"] for h in self._list["homes"]]}

    def save(self, key: str, endpoint: str, who: str) -> dict:
        """Prueba la clave (casas y equipos, solo lectura) antes de guardar; si falla no se guarda nada."""
        key = (key or "").strip()
        base = (endpoint or "").strip().rstrip("/") or region_url(key)
        if not base.startswith("https://"):
            raise ValueError("el endpoint debe ser https://")
        homes, devs = self._fetch(key, base)
        files.save(CFG, {"key": key, "endpoint": base, "saved_by": who, "saved_ts": time.time()}, secret=True)
        with self._lock:
            self._list = {"ts": time.time(), "devices": devs, "homes": homes}
            self._models.clear()
        return {"homes": [h["name"] for h in homes], "devices": len(devs), "endpoint": base}

    def forget(self) -> None:
        files.save(CFG, {}, secret=True)
        with self._lock:
            self._list = {"ts": 0.0, "devices": {}, "homes": []}
            self._models.clear()
            self.overlay.clear()

    # ── HTTP ───────────────────────────────────────────────────────
    @staticmethod
    def _call(base: str, key: str, method: str, path: str, body=None):
        t0 = time.time()
        try:
            r = httpx.request(method, base + path, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            consola.log("error", "tuya-usuario", f"{method} {path} sin respuesta: {type(exc).__name__}")
            raise TuyaUserError(f"la API de usuario de Tuya no responde: {type(exc).__name__}") from exc
        ms = round((time.time() - t0) * 1000)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code >= 400 or not j.get("success", False):
            code = j.get("code")
            msg = ERRORS.get(code) or str(j.get("msg") or f"HTTP {r.status_code}")[:120]
            consola.log("warn", "tuya-usuario", f"{method} {path} → {r.status_code} código {code}: {msg} ({ms} ms)")
            raise TuyaUserError(msg)
        consola.log("debug", "tuya-usuario", f"{method} {path} → ok {ms} ms")
        return j.get("result")

    def _conn(self):
        c = self._cfg()
        if not c.get("key"):
            raise TuyaUserError("API de usuario de Tuya sin configurar")
        return c.get("endpoint") or region_url(c["key"]), c["key"]

    def _fetch(self, key: str, base: str):
        homes = (self._call(base, key, "GET", "/v1.0/end-user/homes/all") or {}).get("homes") or []
        mine = [h for h in homes if str(h.get("home_id")) == HOME_ID or str(h.get("name") or "").strip().lower() == HOME_NAME]
        if not mine:
            raise ValueError(f"la cuenta no tiene la casa «{HOME_NAME.title()}»; casas vistas: {', '.join(str(h.get('name')) for h in homes) or 'ninguna'}")
        devs: dict = {}
        for h in mine:
            res = self._call(base, key, "GET", f"/v1.0/end-user/homes/{h['home_id']}/devices") or {}
            for d in res.get("devices") or []:
                devs[d["device_id"]] = {"name": d.get("name"), "online": bool(d.get("online")), "category": d.get("category"), "home": h.get("name")}
        return mine, devs

    # ── equipos ────────────────────────────────────────────────────
    def devices(self, max_age: float = LIST_TTL) -> dict:
        """device_id -> {name, online, category, home} de la casa; vacío (y last_error) si no se pudo leer."""
        with self._lock:
            if time.time() - self._list["ts"] < max_age or not self.configured:
                return self._list["devices"]
        base, key = self._conn()
        try:
            homes, devs = self._fetch(key, base)
            self.last_error = ""
        except (TuyaUserError, ValueError) as exc:
            self.last_error = str(exc)
            return self._list["devices"]
        with self._lock:
            self._list = {"ts": time.time(), "devices": devs, "homes": homes}
        return devs

    def has(self, dev_id: str) -> bool:
        return self.configured and dev_id in self.devices()

    def model(self, dev_id: str) -> dict:
        """code -> {access, type, min, max, range} del modelo del equipo (se guarda 1 h)."""
        with self._lock:
            hit = self._models.get(dev_id)
            if hit and time.time() - hit[0] < 3600:
                return hit[1]
        base, key = self._conn()
        res = self._call(base, key, "GET", f"/v1.0/end-user/devices/{dev_id}/model") or {}
        raw = res.get("model")
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        out = {}
        for svc in data.get("services") or [data]:
            for p in svc.get("properties") or []:
                ts = p.get("typeSpec") or {}
                if isinstance(ts, str):
                    try:
                        ts = json.loads(ts)
                    except ValueError:
                        ts = {}
                out[p.get("code")] = {"access": p.get("accessMode"), "type": ts.get("type"), "min": ts.get("min"), "max": ts.get("max"), "range": ts.get("range")}
        with self._lock:
            self._models[dev_id] = (time.time(), out)
        return out

    def detail(self, dev_id: str) -> dict:
        base, key = self._conn()
        res = self._call(base, key, "GET", f"/v1.0/end-user/devices/{dev_id}/detail") or {}
        return {"online": bool(res.get("online")), "properties": res.get("properties") or {}}

    def issue(self, dev_id: str, code: str, value) -> str:
        """Envía una propiedad. Devuelve «ok» o lanza TuyaUserError con el motivo. El valor se valida contra el modelo del equipo."""
        if code not in USER_CODES:
            raise TuyaUserError(f"orden no permitida por esta vía: {code}")
        if not self.has(dev_id):
            raise TuyaUserError("el equipo no está en la casa de la cuenta de usuario")
        spec = self.model(dev_id).get(code)
        if spec is None:
            raise TuyaUserError(f"el equipo no tiene la propiedad «{code}»")
        if spec["access"] == "ro":
            raise TuyaUserError(f"«{code}» es de solo lectura")
        t, v = spec["type"], value
        if t == "bool" and not isinstance(v, bool):
            raise TuyaUserError(f"«{code}» espera verdadero o falso")
        if t == "enum" and v not in (spec.get("range") or []):
            raise TuyaUserError(f"«{code}» acepta: {', '.join(map(str, spec.get('range') or []))}")
        if t == "value":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (spec.get("min", v) <= v <= spec.get("max", v)):
                raise TuyaUserError(f"«{code}» espera un número entre {spec.get('min')} y {spec.get('max')}")
        base, key = self._conn()
        self._call(base, key, "POST", f"/v1.0/end-user/devices/{dev_id}/shadow/properties/issue", {"properties": json.dumps({code: v})})
        threading.Thread(target=self._confirm, args=(dev_id,), daemon=True).start()
        return "ok"

    def _confirm(self, dev_id: str) -> None:
        """Dos segundos después de una orden se lee el estado del equipo para mostrarlo."""
        time.sleep(2.0)
        try:
            d = self.detail(dev_id)
            self.overlay[dev_id] = {"props": d["properties"], "online": d["online"], "ts": time.time()}
        except TuyaUserError:
            pass
