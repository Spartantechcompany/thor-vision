"""Dispositivos Tuya: estado desde thor-vision (/api/iot/devices, que escucha el MQTT del puente) y control por el
servicio local de oapc-devops (/control/<id>, /bulbs). Ese servicio ya limita qué equipos se pueden mover (los de iot.yml,
solo la casa Patricio 10) y a qué ritmo; aquí se valida además el código de la orden."""
import os
import re
import threading
import time

import httpx

from patcc import consola
from patcc import registro as reg_mod

SVC = os.environ.get("TUYA_SERVICE_URL", "http://192.168.0.194:8765").rstrip("/")
VISION = os.environ.get("VISION_URL", "http://127.0.0.1:8080").rstrip("/")
VISION_TOKEN = os.environ.get("VISION_TOKEN", "")          # token de máquina de thor-vision (cuando tenga acceso con login)
RETIRED_IDS = {i.strip() for i in os.environ.get("PATCC_RETIRED_IDS", "").split(",") if i.strip()}      # respaldo hasta que thor-vision exponga `retired`
CODES = re.compile(r"^(switch|switch_led|switch_[1-6]|brightness_pct|colour_hex|white_pct|control|percent_control)$")
DEV_ID = re.compile(r"^[A-Za-z0-9]{6,40}$")
BULBS_EVERY_S = 120.0          # leer los focos por la red local es lento (los que no responden tardan): se hace en segundo plano
BULBS_TIMEOUT_S = 240.0


class Tuya:
    def __init__(self):
        self._lock = threading.Lock()
        self._devices: list = []
        self._devices_ts = 0.0
        self._bulbs: list = []
        self._bulbs_ts = 0.0
        self.last_error = ""
        self.registro = None               # patcc.registro.Registro: estado de nube por equipo y filtro por casa
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._bulbs_loop, daemon=True, name="patcc-focos").start()

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
                r = httpx.get(SVC + "/bulbs", timeout=BULBS_TIMEOUT_S)
                r.raise_for_status()
                with self._lock:
                    self._bulbs, self._bulbs_ts = r.json().get("bulbs") or [], time.time()
                ok = sum(1 for b in self._bulbs if b.get("reachable"))
                consola.log("debug", "tuya", f"focos por red local: {ok} de {len(self._bulbs)} alcanzables")
            except (httpx.HTTPError, ValueError) as exc:
                consola.log("warn", "tuya", f"lectura de focos por red local falló: {type(exc).__name__}")
            self._stop.wait(BULBS_EVERY_S)

    def refresh_bulbs_soon(self) -> None:
        threading.Thread(target=self._bulbs_once, daemon=True).start()

    def _bulbs_once(self) -> None:
        try:
            r = httpx.get(SVC + "/bulbs", timeout=BULBS_TIMEOUT_S)
            r.raise_for_status()
            with self._lock:
                self._bulbs, self._bulbs_ts = r.json().get("bulbs") or [], time.time()
        except (httpx.HTTPError, ValueError) as exc:
            consola.log("warn", "tuya", f"lectura de focos por red local falló: {type(exc).__name__}")

    # ── control ────────────────────────────────────────────────────
    def _dev(self, dev_id: str):
        return next((d for d in self.view(all_=True) if d["device_id"] == dev_id), None)

    def control(self, dev_id: str, code: str, value) -> dict:
        if not DEV_ID.match(dev_id or ""):
            raise ValueError("dispositivo inválido")
        if not CODES.match(code or ""):
            raise ValueError(f"orden no permitida: {code}")
        dev = self._dev(dev_id)
        name = (dev or {}).get("name", dev_id)
        if dev is None:
            consola.log("warn", "tuya", f"orden a un equipo que no es de esta casa o no existe: {dev_id}")
            return {"ok": False, "result": "ese equipo no pertenece a Patricio 10"}
        if dev["type"] != "foco" and dev["nube"] in ("sin_cupo", "retirado"):
            why = ("ya no existe en la nube de Tuya (retirado)" if dev["nube"] == "retirado"
                   else "no está dentro del cupo de control de la nube de Tuya, que rechaza sus órdenes («quota is insufficient» o «permission deny»)")
            consola.log("warn", "tuya", f"{name} {code}={value} no enviada: {why}")
            return {"ok": False, "result": f"«{name}» {why}. Hace falta controlarlo por la red local."}
        t0 = time.time()
        try:
            r = httpx.post(f"{SVC}/control/{dev_id}", json={"code": code, "value": value}, timeout=15)
            out = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            consola.log("error", "tuya", f"{name} {code}={value}: servicio Tuya no responde ({type(exc).__name__})")
            return {"ok": False, "result": f"servicio Tuya no responde: {type(exc).__name__}"}
        consola.log("info" if out.get("ok") else "warn", "tuya", f"{name} {code}={value} → {out.get('result')} ({round((time.time() - t0) * 1000)} ms)")
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
        t0 = time.time()
        try:
            r = httpx.post(f"{SVC}/bulbs", json={"ids": ids, "action": act}, timeout=120)
            out = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            consola.log("error", "tuya", f"focos {act}: servicio Tuya no responde ({type(exc).__name__})")
            return {"ok": False, "result": f"servicio Tuya no responde: {type(exc).__name__}"}
        consola.log("info" if out.get("ok") else "warn", "tuya", f"focos {act} → {out.get('result')} ({round((time.time() - t0) * 1000)} ms)")
        self.refresh_bulbs_soon()
        return out

    # ── vista para el panel ────────────────────────────────────────
    def view(self, all_: bool = False) -> list:
        """Equipos de la casa con su estado de nube. Sin all_, se omiten los retirados y cualquiera de otra casa."""
        states = self.registro.states() if self.registro else {}
        out = []
        for d in self.devices():
            st = states.get(d.get("device_id"), {})
            casa = st.get("casa")
            if casa and str(casa) != reg_mod.HOME:
                continue                                              # filtro por casa: Coworking540 nunca entra
            nube = st.get("nube") or ("desconocido")
            if d.get("retired") or d.get("device_id") in RETIRED_IDS:
                nube = "retirado"
            if nube == "retirado" and not all_:
                continue
            item = {k: d.get(k) for k in ("device_id", "name", "kind", "type", "online", "open", "since", "battery", "switches", "gangs",
                                          "gang_names", "percent", "cam_name", "last_ts", "cloud")}
            item["retired"] = bool(d.get("retired")) or d.get("device_id") in RETIRED_IDS
            item["nube"] = nube
            out.append(item)
        return out

    def stats(self) -> dict:
        with self._lock:
            b = self._bulbs
            return {"focos": len(b), "alcanzables": sum(1 for x in b if x.get("reachable")), "ts": self._bulbs_ts, "error": self.last_error}
