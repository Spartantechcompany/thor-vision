"""Dispositivos Tuya: estado desde thor-vision (/api/iot/devices, que escucha el MQTT del puente) y control por el
servicio local de oapc-devops (/control/<id>, /bulbs). Ese servicio ya limita qué equipos se pueden mover (los de iot.yml,
solo la casa Patricio 10) y a qué ritmo; aquí se valida además el código de la orden."""
import os
import re
import threading
import time

import httpx

SVC = os.environ.get("TUYA_SERVICE_URL", "http://192.168.0.194:8765").rstrip("/")
VISION = os.environ.get("VISION_URL", "http://127.0.0.1:8080").rstrip("/")
VISION_TOKEN = os.environ.get("VISION_TOKEN", "")          # token de máquina de thor-vision (cuando tenga acceso con login)
CODES = re.compile(r"^(switch|switch_led|switch_[1-6]|brightness_pct|colour_hex|white_pct|control|percent_control)$")
DEV_ID = re.compile(r"^[A-Za-z0-9]{6,40}$")
BULBS_EVERY_S = 90.0           # leer los focos por la red local es lento (algunos no responden): se hace en segundo plano


class Tuya:
    def __init__(self):
        self._lock = threading.Lock()
        self._devices: list = []
        self._devices_ts = 0.0
        self._bulbs: list = []
        self._bulbs_ts = 0.0
        self.last_error = ""
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._bulbs_loop, daemon=True, name="hogar-focos").start()

    # ── estado ─────────────────────────────────────────────────────
    def devices(self, max_age: float = 4.0) -> list:
        with self._lock:
            if time.time() - self._devices_ts < max_age:
                return self._devices
        h = {"Authorization": f"Bearer {VISION_TOKEN}"} if VISION_TOKEN else {}
        try:
            r = httpx.get(VISION + "/api/iot/devices", headers=h, timeout=6)
            r.raise_for_status()
            devs = r.json().get("devices") or []
            self.last_error = ""
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"thor-vision no entrega los dispositivos: {type(exc).__name__}"
            return self._devices
        with self._lock:
            self._devices, self._devices_ts = devs, time.time()
        return devs

    def bulbs(self) -> dict:
        with self._lock:
            return {"bulbs": self._bulbs, "ts": self._bulbs_ts}

    def _bulbs_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = httpx.get(SVC + "/bulbs", timeout=60)
                r.raise_for_status()
                with self._lock:
                    self._bulbs, self._bulbs_ts = r.json().get("bulbs") or [], time.time()
            except (httpx.HTTPError, ValueError):
                pass
            self._stop.wait(BULBS_EVERY_S)

    def refresh_bulbs_soon(self) -> None:
        threading.Thread(target=self._bulbs_once, daemon=True).start()

    def _bulbs_once(self) -> None:
        try:
            r = httpx.get(SVC + "/bulbs", timeout=60)
            r.raise_for_status()
            with self._lock:
                self._bulbs, self._bulbs_ts = r.json().get("bulbs") or [], time.time()
        except (httpx.HTTPError, ValueError):
            pass

    # ── control ────────────────────────────────────────────────────
    def control(self, dev_id: str, code: str, value) -> dict:
        if not DEV_ID.match(dev_id or ""):
            raise ValueError("dispositivo inválido")
        if not CODES.match(code or ""):
            raise ValueError(f"orden no permitida: {code}")
        try:
            r = httpx.post(f"{SVC}/control/{dev_id}", json={"code": code, "value": value}, timeout=15)
            out = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "result": f"servicio Tuya no responde: {type(exc).__name__}"}
        with self._lock:
            self._devices_ts = 0.0
        return out

    def group(self, ids, action: dict) -> dict:
        """Varios focos a la vez por la red local: ids = 'all' o lista; action = {on, brightness, colour, white}."""
        if ids != "all":
            ids = [i for i in (ids or []) if isinstance(i, str) and DEV_ID.match(i)]
        act = {k: action[k] for k in ("on", "brightness", "colour", "white") if k in (action or {})}
        if not act:
            raise ValueError("acción vacía")
        try:
            r = httpx.post(f"{SVC}/bulbs", json={"ids": ids, "action": act}, timeout=60)
            out = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "result": f"servicio Tuya no responde: {type(exc).__name__}"}
        self.refresh_bulbs_soon()
        return out

    # ── vista para el panel ────────────────────────────────────────
    def view(self) -> list:
        out = []
        for d in self.devices():
            if d.get("retired"):
                continue
            out.append({k: d.get(k) for k in ("device_id", "name", "kind", "type", "online", "open", "since", "battery", "switches", "gangs",
                                              "gang_names", "percent", "cam_name", "last_ts")})
        return out
