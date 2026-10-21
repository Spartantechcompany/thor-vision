"""Eventos de dispositivos Tuya (puertas, ventanas, garage) por MQTT, SOLO LECTURA.

El puente tuya_bridge (en oapc-devops) publica cada cambio de estado en <prefijo>/<id>/status y el ultimo estado retenido en
<prefijo>/<id>/last. Aqui se guardan en `iot_events`, y cuando una puerta o ventana se abre se genera una alerta con la captura de la
camara asociada; tambien si sigue abierta mucho tiempo y cuando la bateria del sensor esta baja. Nunca se publica nada al broker.

Credenciales: data/mqtt.json (permisos 600, fuera de git): {"host","port","user","password","topic"}.
"""
import base64
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

import yaml

logger = logging.getLogger(__name__)

MQTT_PATH = os.environ.get("MQTT_CONFIG", "/app/data/mqtt.json")
IOT_YML = os.environ.get("IOT_CONFIG", "/app/config/iot.yml")
LIBS = os.environ.get("PLATES_LIBS", "/app/data/pylibs")        # paho-mqtt se instala junto a fast-alpr (scripts/setup_mqtt.sh)

TUYA_SERVICE = os.environ.get("TUYA_SERVICE_URL", "http://192.168.0.194:8765")
MOTION_CODES = {"ipc_motion", "ipc_bang"}
CAMERA_ALERT_COOLDOWN_S = float(os.environ.get("TUYA_ALERT_COOLDOWN_S", "120"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS iot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, device_id TEXT NOT NULL, name TEXT, kind TEXT,
  code TEXT NOT NULL, value TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS idx_iot_ts ON iot_events(ts);
CREATE INDEX IF NOT EXISTS idx_iot_dev ON iot_events(device_id, ts);
"""


def load_map(path: str = IOT_YML) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("iot: no se pudo leer %s: %s", path, exc)
        return {}


def is_open(dev: dict, changes: dict):
    """True/False si el cambio trae el estado abierto/cerrado del dispositivo; None si no lo trae."""
    code = dev.get("open_code", "doorcontact_state")
    if code not in changes:
        return None
    return changes[code] == dev.get("open_value", True)


class IotListener:
    def __init__(self, db, snapshots, buffers: dict, cam_names: dict):
        self.db, self.snapshots, self.buffers, self.cam_names = db, snapshots, buffers, cam_names
        self.cfg = load_map()
        self.devices: dict = self.cfg.get("devices", {})
        self.devices_by_alias: dict = {d["alias"]: d.get("name", "").replace(" (Tuya)", "") for d in self.devices.values() if d.get("alias")}
        self.state: dict = {}            # device_id -> {"open": bool|None, "since": ts, "battery": int|None, "last_ts": ts}
        self._alerted_open: dict = {}    # device_id -> ts de la ultima alerta de "sigue abierto"
        self._low_batt: dict = {}
        self._alerted_usage: set = set()
        self._recent: dict = {}          # (device, codigo, valor) -> ts: el mismo mensaje llega duplicado desde la nube
        self._last_motion: dict = {}     # device_id -> ts de la ultima alerta de movimiento/ruido
        self.detections = None           # DetectionStore (lo fija main.py): sirve para elegir la camara que de verdad ve a la persona
        self.clip_cb = None              # callable(event_id, cam_ids): pide un clip de video de las camaras de la zona
        self.on_light = None             # callable(dev, ts): alguien movio un interruptor; pide captura a las camaras de esa zona
        self.on_motion = None            # callable(alias, ts): aviso de movimiento de una camara Tuya (viajes y rafaga en la camara de entrada)
        self._stop = threading.Event()
        self._retry = threading.Event()      # se activa si el broker rechaza la conexion: se vuelve a leer data/mqtt.json y se reintenta
        self.status = {"connected": False, "error": "", "messages": 0}
        with db._lock:
            db._conn.executescript(_SCHEMA)

    # ---- mensajes
    def handle(self, topic: str, payload: bytes, retained: bool = False) -> None:
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if topic.endswith("/_usage"):
            self._usage_alert(msg, retained)
            return
        dev_id = msg.get("device_id") or topic.split("/")[-2]
        changes = msg.get("changes") or {}
        dev = self.devices.get(dev_id)
        if dev is not None and not changes and "online" in msg:         # gateways y equipos sin sensores: solo informan si estan en linea (sincronizacion de la nube)
            st0 = self.state.setdefault(dev_id, {"open": None, "since": None, "battery": None, "last_ts": None})
            st0["online"], st0["last_ts"] = bool(msg["online"]), float(msg.get("ts") or time.time())
            return
        if dev is None or not changes:
            return
        self.status["messages"] += 1
        ts = float(msg.get("ts") or time.time())
        st = self.state.setdefault(dev_id, {"open": None, "since": ts, "battery": None, "last_ts": ts})
        st["last_ts"] = ts
        if "online" in msg:
            st["online"] = bool(msg["online"])
        retained = retained or msg.get("source") == "cloud-sync"      # el puente publica el estado de la nube cada 10 min: se registra como sincronizacion, nunca como evento ni alerta
        source = "sync" if retained else "event"
        for code, value in changes.items():
            if code == "initiative_message":
                value = self._decode_initiative(value)
                if value is None:
                    continue
                code = value["cmd"]
            self._store(ts, dev_id, dev, code, value, source)
            if dev.get("kind") == "camera" and not retained and code in MOTION_CODES:
                self._camera_alert(dev_id, dev, ts, code)
        if "battery_percentage" in changes:
            st["battery"] = changes["battery_percentage"]
            self._check_battery(dev_id, dev, st["battery"])
        if dev.get("kind") == "curtain":
            for c in ("percent_control", "percent_state"):
                if c in changes:
                    st["percent"] = changes[c]
            return
        if dev.get("kind") == "light":
            sw = st.setdefault("sw", {})
            sw.update({c: bool(v) for c, v in changes.items() if re.fullmatch(r"switch(_\d+|_led)?", c)})
            if not any(re.fullmatch(r"switch(_\d+|_led)?", c) for c in changes):
                return
            if self.on_light is not None and not retained and any(re.fullmatch(r"switch(_\d+|_led)?", c) and c not in (dev.get("ignore_codes") or []) for c in changes):
                try:
                    self.on_light(dev, ts)
                except (TypeError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
                    logger.warning("iot: on_light: %s", exc)
            op = any(sw.values())
        else:
            op = is_open(dev, changes)
        if op is None:
            return
        opened_at = st.get("since") or ts
        changed = st["open"] is not None and st["open"] != op
        first = st["open"] is None
        if op != st["open"]:
            st["open"], st["since"] = op, ts
        if retained or first:
            return                       # estado al arrancar: solo se registra, no alerta
        if dev.get("kind") == "light":
            if changed:
                self._store(ts, dev_id, dev, "encendida" if op else "apagada", "true", "derived")
            return                       # las luces no generan alertas
        if changed and op:
            self._alert_opened(dev_id, dev, ts)
        elif changed and not op:
            self._store(ts, dev_id, dev, "cerrada", "true", "derived")
            self._alert_closed(dev_id, dev, ts, opened_at)

    @staticmethod
    def _decode_initiative(value):
        """Mensaje de la camara Tuya (base64 de un JSON): comando (ipc_motion, ipc_bang...), hora y tipo. La imagen queda cifrada en la nube de Tuya."""
        try:
            m = json.loads(base64.b64decode(value))
        except (ValueError, TypeError):
            return None
        cmd = str(m.get("cmd") or "")
        return {"cmd": cmd, "time": m.get("time"), "type": m.get("type"), "alarm": m.get("alarm")} if cmd else None

    def _camera_alert(self, dev_id: str, dev: dict, ts: float, code: str) -> None:
        """Movimiento (ipc_motion) o ruido fuerte (ipc_bang) de una camara Tuya: una alerta con captura por camara cada CAMERA_ALERT_COOLDOWN_S."""
        if self.on_motion is not None and code == "ipc_motion":
            try:
                self.on_motion(dev.get("alias") or dev_id, ts)
            except (TypeError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
                logger.warning("iot: on_motion: %s", exc)
        if ts - self._last_motion.get(dev_id, 0) < CAMERA_ALERT_COOLDOWN_S:
            return
        self._last_motion[dev_id] = ts
        sound = code == "ipc_bang"
        name = dev["name"].replace(" (Tuya)", "")
        eid = self._insert_alert(dev.get("alias") or dev_id, f"{'Ruido fuerte' if sound else 'Movimiento'} en {name}", f"{'Ruido' if sound else 'Movimiento'} detectado por {name}",
                                 "ruido_tuya" if sound else "movimiento_tuya", "low", {"device_id": dev_id, "name": dev["name"], "code": code})
        threading.Thread(target=self._grab_frame, args=(eid, dev_id, dev), daemon=True, name="iot-grab").start()

    def _grab_frame(self, event_id: int, dev_id: str, dev: dict) -> None:
        """Una captura de la camara Tuya por el servicio de video (consume un poco de la cuota; el servicio la bloquea al llegar al tope)."""
        import cv2
        import numpy as np
        try:
            with urllib.request.urlopen(f"{TUYA_SERVICE}/frame/{dev_id}", timeout=25) as r:
                jpg = r.read()
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None and self.snapshots is not None:
                self.snapshots.save(dev.get("alias") or dev_id, frame, "tuya", event_id=event_id)
        except (OSError, urllib.error.URLError, ValueError, cv2.error) as exc:
            logger.warning("iot: sin captura de %s: %s", dev.get("name"), exc)

    def _store(self, ts, dev_id, dev, code, value, source) -> None:
        if source == "event":
            key = (dev_id, code, json.dumps(value))
            if ts - self._recent.get(key, 0) < 5:
                return
            self._recent[key] = ts
            if len(self._recent) > 500:
                self._recent = {k: t for k, t in self._recent.items() if ts - t < 60}
        try:
            with self.db._lock:
                self.db._conn.execute("INSERT INTO iot_events (ts, device_id, name, kind, code, value, source) VALUES (?,?,?,?,?,?,?)",
                                      (ts, dev_id, dev.get("name"), dev.get("kind"), code, json.dumps(value), source))
                self.db._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("iot: no se pudo guardar el evento: %s", exc)

    # ---- alertas
    def _insert_alert(self, cam_id, activity: str, alert: str, alert_type: str, severity: str, extra: dict) -> int:
        payload = {"schema": 2, "source": "iot", "people": 0, "persons": [], "vehicles": 0, "activity": activity, "scene": "",
                   "relevant": True, "alerts": [alert], "alert_types": [alert_type], "severity": severity, "confidence": "high", "iot": extra}
        return self.db.insert_event("nemotron", cam_id, payload, people=0, has_alert=True)

    def _best_cam(self, dev: dict):
        """Camara de la zona que ve a una persona justo ahora (la puerta puede no estar en el campo de la camara asignada); si ninguna, la asignada."""
        base = dev.get("cam")
        if self.detections is None:
            return base
        cands = [c for c in ([base] + list(dev.get("related") or [])) if c and str(c).startswith("cam-")]
        now, best = time.time(), None
        for c in dict.fromkeys(cands):
            d = self.detections.get(c)
            if d is not None and now - (getattr(d, "yolo_ts", 0) or 0) < 5 and (getattr(d, "yolo_persons", 0) or getattr(d, "person_count", 0) or 0) > 0:
                n = getattr(d, "yolo_persons", 0) or getattr(d, "person_count", 0)
                if best is None or n > best[0]:
                    best = (n, c)
        return best[1] if best else base

    def _zone_cams(self, dev: dict) -> list:
        cams = [c for c in ([dev.get("cam")] + list(dev.get("related") or [])) if c and str(c).startswith("cam-")]
        return list(dict.fromkeys(cams))

    def _frame_of(self, cam_id):
        buf = self.buffers.get(cam_id)
        frame = buf.peek_latest() if buf is not None and hasattr(buf, "peek_latest") else None
        frame = getattr(frame, "frame", frame)
        return frame[0] if isinstance(frame, tuple) else frame

    def _mosaic(self, cams: list):
        """Una sola imagen con la vista actual de todas las camaras de la zona (borde verde = ven a una persona), para ver donde paso algo."""
        import cv2
        import numpy as np
        tw, th = 640, 360
        tiles = []
        for c in cams[:4]:
            fr = self._frame_of(c)
            tile = np.zeros((th, tw, 3), np.uint8)
            if fr is not None:
                h, w = fr.shape[:2]
                k = min(tw / w, th / h)
                small = cv2.resize(fr, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
                y0, x0 = (th - small.shape[0]) // 2, (tw - small.shape[1]) // 2
                tile[y0:y0 + small.shape[0], x0:x0 + small.shape[1]] = small
            d = self.detections.get(c) if self.detections is not None else None
            seen = d is not None and time.time() - (getattr(d, "yolo_ts", 0) or 0) < 5 and (getattr(d, "yolo_persons", 0) or 0) > 0
            if seen:
                cv2.rectangle(tile, (0, 0), (tw - 1, th - 1), (0, 220, 0), 6)
            label = self.cam_names.get(c) or c
            cv2.rectangle(tile, (0, th - 30), (tw, th), (0, 0, 0), -1)
            cv2.putText(tile, label, (8, th - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            tiles.append(tile)
        while len(tiles) < 2 or len(tiles) % 2:
            tiles.append(np.zeros((th, tw, 3), np.uint8))
        rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
        return np.vstack(rows)

    def _snapshot_zone(self, event_id: int, dev: dict, trigger: str) -> None:
        """Con varias camaras en la zona (garage: frontal, posterior y exteriores) guarda un mosaico de todas; con una sola, la camara que ve a la persona."""
        cams = self._zone_cams(dev)
        if len(cams) >= 2 and self.snapshots is not None:
            try:
                self.snapshots.save(dev.get("cam") or cams[0], self._mosaic(cams), trigger, event_id=event_id)
                return
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("iot: mosaico: %s", exc)
        self._snapshot(event_id, self._best_cam(dev), trigger)

    def _snapshot(self, event_id: int, cam_id, trigger: str) -> None:
        buf = self.buffers.get(cam_id) if cam_id else None
        frame = buf.peek_latest() if buf is not None and hasattr(buf, "peek_latest") else None
        frame = getattr(frame, "frame", frame)          # peek_latest devuelve un FrameEntry
        if isinstance(frame, tuple):
            frame = frame[0]
        if self.snapshots is not None and frame is not None:
            try:
                self.snapshots.save(cam_id, frame, trigger, event_id=event_id)
            except (OSError, ValueError) as exc:
                logger.warning("iot: no se pudo guardar la captura: %s", exc)

    def _night(self, ts: float) -> bool:
        h = time.localtime(ts).tm_hour
        return h >= 22 or h < 6

    def _alert_opened(self, dev_id: str, dev: dict, ts: float) -> None:
        what = {"door": "puerta", "window": "ventana", "garage": "garage"}.get(dev.get("kind"), "dispositivo")
        sev = "medium" if self._night(ts) else "low"
        cam = dev.get("cam")
        eid = self._insert_alert(cam, f"Se abrio {what}: {dev['name']}", f"{dev['name']} abierta" + (" de noche" if sev == "medium" else ""),
                                 "puerta_abierta", sev, {"device_id": dev_id, "name": dev["name"], "kind": dev.get("kind"), "state": "open",
                                                          "cam_confirmed": bool(dev.get("confirmed"))})
        self._alerted_open[dev_id] = ts
        self._snapshot_zone(eid, dev, "puerta")
        if self.clip_cb is not None:
            try:
                self.clip_cb(eid, self._zone_cams(dev))
            except (TypeError, ValueError, OSError) as exc:
                logger.warning("iot: clip: %s", exc)
        if self.on_motion is not None and dev.get("alias"):
            try:
                self.on_motion(dev["alias"], ts)         # la apertura anticipa una entrada en la camara asociada (viajes)
            except (TypeError, ValueError, KeyError, OSError, sqlite3.Error) as exc:
                logger.warning("iot: on_motion: %s", exc)
        logger.info("iot: %s abierta -> alerta %d (cam %s)", dev["name"], eid, cam)

    def _alert_closed(self, dev_id: str, dev: dict, ts: float, opened_at: float) -> None:
        """Cierre de una puerta/ventana: queda en la linea de tiempo (sin severidad) para ver cuanto estuvo abierta."""
        what = {"door": "puerta", "window": "ventana", "garage": "garage"}.get(dev.get("kind"), "dispositivo")
        mins = max(0, int((ts - opened_at) // 60))
        secs = max(0, int(ts - opened_at))
        dur = f"{mins // 60} h {mins % 60} min" if mins >= 60 else (f"{mins} min {secs % 60} s" if mins >= 1 else f"{secs} s")
        eid = self._insert_alert(dev.get("cam"), f"Se cerro {what}: {dev['name']} (estuvo abierta {dur})", f"{dev['name']} cerrada", "puerta_abierta", "none",
                                 {"device_id": dev_id, "name": dev["name"], "kind": dev.get("kind"), "state": "closed", "minutes": mins})
        self._snapshot_zone(eid, dev, "puerta_cierre")
        self._alerted_open.pop(dev_id, None)

    def _usage_alert(self, msg: dict, retained: bool) -> None:
        """Aviso del servicio de video Tuya: se llego a un porcentaje de la cuota mensual de la nube."""
        key = (msg.get("threshold"), time.strftime("%Y-%m", time.localtime(float(msg.get("ts") or time.time()))))
        if retained and key in self._alerted_usage:
            return
        if key in self._alerted_usage:
            return
        self._alerted_usage.add(key)
        if retained:
            return                       # al arrancar solo se recuerda el aviso viejo; no se vuelve a alertar
        pct = msg.get("pct")
        self._insert_alert(None, f"Consumo de video de las camaras Tuya al {pct}% de la cuota mensual", f"Video Tuya: {pct}% de {msg.get('quota_gb')} GB usados",
                           "consumo_video_tuya", "medium" if (msg.get("threshold") or 0) >= 80 else "low", {"usage": msg})

    def _check_battery(self, dev_id: str, dev: dict, pct) -> None:
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            return
        if pct > int(self.cfg.get("low_battery_pct", 15)) or time.time() - self._low_batt.get(dev_id, 0) < 86400:
            return
        self._low_batt[dev_id] = time.time()
        logger.info("iot: bateria baja (%s%%) del sensor %s (se muestra en su tarjeta, no es alerta)", pct, dev["name"])

    def check_open_too_long(self, now: float | None = None) -> None:
        """Llamar periodicamente: alerta de nuevo si una puerta o ventana sigue abierta mas de alert_open_minutes."""
        now = now or time.time()
        limit = float(self.cfg.get("alert_open_minutes", 0)) * 60
        if limit <= 0:
            return                       # apagado: solo se avisa en cada cambio de estado (al cerrar queda cuanto duro)
        for dev_id, st in self.state.items():
            dev = self.devices.get(dev_id, {})
            if not st.get("open") or dev.get("kind") == "camera":
                continue
            if now - st["since"] >= limit and now - self._alerted_open.get(dev_id, 0) >= limit:
                self._alerted_open[dev_id] = now
                mins = int((now - st["since"]) // 60)
                eid = self._insert_alert(dev.get("cam"), f"{dev['name']} lleva {mins} min abierta", f"{dev['name']} sigue abierta ({mins} min)",
                                         "puerta_abierta", "medium", {"device_id": dev_id, "name": dev["name"], "state": "open", "minutes": mins})
                self._snapshot(eid, dev.get("cam"), "puerta")

    def _safe(self, fn, *args) -> None:
        """Un fallo al procesar un mensaje o al revisar puertas no debe matar el hilo MQTT (una vez dejo de recibir eventos por esto)."""
        try:
            fn(*args)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            logger.warning("iot: error procesando %s", getattr(fn, "__name__", "?"), exc_info=True)

    # ---- conexion MQTT
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="iot-mqtt").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                with open(MQTT_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                self.status.update(connected=False, error="sin data/mqtt.json (scripts/setup_mqtt.sh)")
                self._stop.wait(60)
                continue
            try:
                if LIBS not in sys.path:
                    sys.path.append(LIBS)
                import paho.mqtt.client as mqtt
            except ImportError:
                self.status.update(connected=False, error="falta paho-mqtt (scripts/setup_mqtt.sh)")
                self._stop.wait(60)
                continue
            prefix = str(cfg.get("topic", "oxm/tuya/#")).rstrip("#").rstrip("/")
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="thor-vision-iot")
            c.username_pw_set(cfg["user"], cfg["password"])

            def on_connect(cl, u, f, rc, p=None, _prefix=prefix):
                if rc.is_failure:
                    self.status.update(connected=False, error=str(rc))
                    self._retry.set()
                    return
                self.status.update(connected=True, error="")
                cl.subscribe([(f"{_prefix}/+/status", 1), (f"{_prefix}/+/last", 1), (f"{_prefix}/_usage", 1)])

            c.on_connect = on_connect
            c.on_disconnect = lambda cl, u, f, rc, p=None: self.status.update(connected=False)
            c.on_message = lambda cl, u, m: self._safe(self.handle, m.topic, m.payload, bool(m.retain))
            try:
                c.connect(cfg.get("host", "192.168.0.101"), int(cfg.get("port", 1883)), 30)
                c.loop_start()
                backoff = 5
                self._retry.clear()
                while not self._stop.wait(5):
                    if self._retry.is_set():
                        break
                    self._safe(self.check_open_too_long)
                c.loop_stop()
                c.disconnect()
                if self._retry.is_set() and not self._stop.is_set():
                    self._stop.wait(60)           # clave o usuario rechazados: se espera y se reintenta con el archivo actual
            except OSError as exc:
                self.status.update(connected=False, error=f"sin conexion: {exc}"[:120])
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def snapshot_state(self) -> list:
        out = []
        for dev_id, dev in self.devices.items():
            st = self.state.get(dev_id, {})
            out.append({"device_id": dev_id, "name": dev.get("name"), "kind": dev.get("kind"), "cam": dev.get("cam"),
                        "cam_name": self.cam_names.get(dev.get("cam") or "", None), "confirmed": bool(dev.get("confirmed")),
                        "open": st.get("open"), "since": st.get("since"), "battery": st.get("battery"), "last_ts": st.get("last_ts"), "online": st.get("online"),
                        "alias": dev.get("alias"), "role": dev.get("role"), "switches": st.get("sw"), "percent": st.get("percent"),
                        "type": dev.get("type"), "gangs": dev.get("gangs"),
                        "related": [{"id": c, "name": self.cam_names.get(c) or (self.devices_by_alias.get(c) or c)} for c in (dev.get("related") or [])]})
        return out

    def recent(self, device_id: str = "", limit: int = 100, sync: bool = True) -> list:
        sql, args = "SELECT id, ts, device_id, name, kind, code, value, source FROM iot_events WHERE 1=1" + ("" if sync else " AND source!='sync'"), []
        if device_id:
            sql += " AND device_id=?"
            args.append(device_id)
        sql += "" if sync else " AND code NOT LIKE 'switch%' AND code NOT IN ('countdown_1','relay_status','light_mode','random_time','cycle_time','child_lock')"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        with self.db._lock:
            rows = self.db._conn.execute(sql, args).fetchall()
        return [{"id": i, "ts": t, "device_id": d, "name": n, "kind": k, "code": c, "value": json.loads(v) if v else None, "source": s}
                for i, t, d, n, k, c, v, s in rows]
