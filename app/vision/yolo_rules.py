"""Modo sin VLM: alertas por reglas sobre las visitas que el tracker ya confirmo (YOLO + Clef), mientras el modelo de vision no responde.

Se activa solo cuando el VLM lleva NO_VLM_AFTER_S segundos sin una respuesta valida y se apaga cuando vuelve (YOLO_RULES=always/off lo fuerza).
Reglas (el horario y el modo salen de config/scenes.yml, igual que cuando decide el VLM):
  persona_nocturna  de noche, en una camara con night_person: alert, una persona que no es personal conocido (en calle solo si se queda NIGHT_EXT_MIN_S)
  merodeo           en calle (zona exterior), una persona que permanece 150 s o mas (YOLO_MERODEO_S)
Cada alerta lleva su captura y la marca source='yolo' (el panel la muestra como 'sin VLM'). Hay un enfriamiento por camara y tipo."""
import json
import logging
import os
import time
from typing import Callable, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)
MODE = os.environ.get("YOLO_RULES", "auto").lower()                      # auto | always | off
NO_VLM_AFTER_S = float(os.environ.get("NO_VLM_AFTER_S", "300"))
MERODEO_S = float(os.environ.get("YOLO_MERODEO_S", "150"))
NIGHT_EXT_MIN_S = float(os.environ.get("YOLO_NIGHT_EXT_MIN_S", "30"))
COOLDOWN_S = float(os.environ.get("YOLO_RULES_COOLDOWN_S", "120"))


class YoloRules:
    def __init__(self, db, snapshots, scenes, cam_names: dict, zones: dict, vlm_last_ok: Callable[[], float], started: Optional[float] = None):
        self.db, self.snapshots, self.scenes, self.cam_names, self.zones = db, snapshots, scenes, cam_names, zones
        self.vlm_last_ok = vlm_last_ok
        self.started = time.time() if started is None else started
        self._last: dict = {}
        self.stats = {"alerts": 0, "skipped_known": 0}

    def active(self, now: Optional[float] = None) -> bool:
        if MODE == "off":
            return False
        if MODE == "always":
            return True
        now = now or time.time()
        return now - max(self.vlm_last_ok() or 0.0, self.started) > NO_VLM_AFTER_S

    def _is_staff(self, v: dict) -> bool:
        if v.get("name"):
            return True
        sid = v.get("subject_id")
        if sid is None:
            return False
        with self.db._lock:
            row = self.db._conn.execute("SELECT category FROM subjects WHERE id=?", (sid,)).fetchone()
        return bool(row and row[0] == "empleado")

    def _clothes(self, visit_id) -> str:
        if visit_id is None:
            return ""
        with self.db._lock:
            row = self.db._conn.execute("SELECT attrs FROM person_visits WHERE id=?", (visit_id,)).fetchone()
        try:
            a = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            return ""
        return f" · ropa: arriba {a['upper']}, abajo {a['lower']}" if a.get("upper") and a.get("lower") else ""

    def on_visit(self, cam_id: str, v: dict, now: Optional[float] = None) -> Optional[str]:
        """Evalua una visita cerrada; devuelve el tipo de alerta generada o None."""
        now = now or time.time()
        if not self.active(now):
            return None
        sc = self.scenes.get(cam_id)
        if sc is None:
            return None
        if self._is_staff(v):
            self.stats["skipped_known"] += 1
            return None
        dur = max(0.0, (v.get("last_ts") or now) - (v.get("first_ts") or now))
        night, mode, _ = sc._rule(v.get("last_ts") or now)
        exterior = self.zones.get(cam_id) == "exterior"
        kind = None
        if night and mode == "alert" and (not exterior or dur >= NIGHT_EXT_MIN_S):
            kind = "persona_nocturna"
        elif exterior and dur >= MERODEO_S:
            kind = "merodeo"
        if kind is None or now - self._last.get((cam_id, kind), 0.0) < COOLDOWN_S:
            return None
        self._last[(cam_id, kind)] = now
        cam = self.cam_names.get(cam_id, cam_id)
        hm = time.strftime("%H:%M", time.localtime(v.get("first_ts") or now))
        txt = (f"Persona sin identificar en {cam} a las {hm} ({int(dur)} s) · modo sin VLM" if kind == "persona_nocturna"
               else f"Merodeo en {cam}: una persona lleva {int(dur)} s en el lugar · modo sin VLM") + self._clothes(v.get("visit_id"))
        eid = self.db.insert_event("nemotron", cam_id, {"schema": 2, "source": "yolo", "people": 1, "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                                        "relevant": True, "alerts": [txt], "alert_types": [kind], "severity": "medium" if kind == "persona_nocturna" else "low",
                                                        "confidence": "medium", "visit": {"visit_id": v.get("visit_id"), "subject_id": v.get("subject_id")}},
                                   people=1, has_alert=True)
        img = v.get("scene") or v.get("body") or v.get("face")
        frame = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR) if isinstance(img, (bytes, bytearray)) else None
        if frame is not None and self.snapshots is not None:
            try:
                self.snapshots.save(cam_id, frame, "yolo", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("modo sin VLM: sin captura: %s", exc)
        self.stats["alerts"] += 1
        logger.info("YOLO_RULE %s cam=%s dur=%.0fs evento=%s", kind, cam_id, dur, eid)
        return kind
