"""Presencia de las mascotas de la casa (Akamaru, Gigi y Mojo-jojo): en casa / fuera, ultima camara y hora, y avisos de salida y regreso.

Las identificaciones salen del clasificador de recortes (pet_model) que ya corre sobre cada perro visto por YOLO; aqui solo se lleva el estado.
Camara de calle (topology.street: Exterior 1 y 2, videoportero) = fuera de la casa; cualquier otra = dentro. Una salida o un regreso se avisa cuando la
mascota se ve en el otro lado hasta TRANS_S segundos despues de su ultimo avistamiento. Una mascota dormida dentro sigue 'en casa' aunque no se vea."""
import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)
KNOWN = {"akamaru": "Akamaru", "mojo": "Mojo-jojo", "gigi": "Gigi"}
MIN_P = float(os.environ.get("PET_PRESENCE_MIN_P", "0.7"))
TRANS_S = float(os.environ.get("PET_TRANSITION_S", "900"))
LOG_EVERY_S = float(os.environ.get("PET_SIGHT_LOG_S", "60"))
STALE_S = float(os.environ.get("PET_STALE_S", "172800"))          # sin avistamientos en 2 dias: 'sin datos'
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, pet TEXT NOT NULL, cam_id TEXT NOT NULL, conf REAL, outside INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_pet_sight ON pet_sightings(pet, ts);
"""


class PetPresence:
    def __init__(self, db, outside_cams: set, cam_names: dict, on_event: Optional[Callable] = None):
        self.db, self.outside, self.names, self.on_event = db, set(outside_cams), cam_names, on_event
        self._lock = threading.Lock()
        self._state: dict = {}                   # pet -> {ts, cam, outside, conf}
        self._logged: dict = {}
        with db._lock:
            db._conn.executescript(_SCHEMA)
            db._conn.commit()
            rows = db._conn.execute("SELECT pet, ts, cam_id, outside, conf FROM pet_sightings WHERE id IN (SELECT MAX(id) FROM pet_sightings GROUP BY pet)").fetchall()
        for pet, ts, cam, out, conf in rows:
            self._state[pet] = {"ts": ts, "cam": cam, "outside": bool(out), "conf": conf}

    def seen(self, pet: str, cam_id: str, conf: float, ts: Optional[float] = None, crop=None) -> Optional[str]:
        """Registra un avistamiento confiable; devuelve 'salida' / 'regreso' si cambio de lado, si no None."""
        if pet not in KNOWN or conf < MIN_P:
            return None
        ts = ts or time.time()
        out = cam_id in self.outside
        with self._lock:
            prev = self._state.get(pet)
            self._state[pet] = {"ts": ts, "cam": cam_id, "outside": out, "conf": conf}
            change = None
            if prev is not None and prev["outside"] != out and ts - prev["ts"] <= TRANS_S:
                change = "salida" if out else "regreso"
            log = ts - self._logged.get((pet, cam_id), 0.0) >= LOG_EVERY_S or change is not None
            if log:
                self._logged[(pet, cam_id)] = ts
        if log:
            with self.db._lock:
                self.db._conn.execute("INSERT INTO pet_sightings (ts, pet, cam_id, conf, outside) VALUES (?,?,?,?,?)", (ts, pet, cam_id, conf, int(out)))
                self.db._conn.commit()
        if change and self.on_event is not None:
            try:
                self.on_event(change, pet, cam_id, conf, crop)
            except Exception as exc:          # noqa: BLE001 - un aviso fallido no debe afectar al clasificador
                logger.warning("pet_presence: %s", exc)
        return change

    def status(self, now: Optional[float] = None) -> list:
        now = now or time.time()
        out = []
        for pet, name in KNOWN.items():
            s = self._state.get(pet)
            if s is None or now - s["ts"] > STALE_S:
                out.append({"pet": pet, "name": name, "status": "sin datos", "cam": None, "ts": None, "age_s": None, "conf": None})
                continue
            out.append({"pet": pet, "name": name, "status": "fuera" if s["outside"] else "en casa", "cam": self.names.get(s["cam"], s["cam"]), "cam_id": s["cam"],
                        "ts": s["ts"], "age_s": round(now - s["ts"]), "conf": round(s["conf"], 2)})
        return out
