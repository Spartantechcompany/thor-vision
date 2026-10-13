"""Alertas del videoportero: visitas (alguien con rostro frente a la puerta) y llamadas (timbre, eventos nativos de Dahua)."""
import logging
import os
import sqlite3
import time

import cv2
import numpy as np
import requests

from app.onvif.dahua_events import DahuaError

logger = logging.getLogger(__name__)

RING_CODES = {c.strip() for c in os.environ.get("DOORBELL_CODES", "Invite,CallNoAnswered,BackKeyLight").split(",") if c.strip()}
MIN_VISIT_S = float(os.environ.get("DOORBELL_MIN_VISIT_S", "15"))       # menos que esto sin timbre ni nombre = transito (trafico), no visita
RING_WINDOW_S = float(os.environ.get("DOORBELL_RING_WINDOW_S", "90"))
DEBOUNCE_S = float(os.environ.get("DOORBELL_DEBOUNCE_S", "10"))


class DoorAlerts:
    def __init__(self, db, snapshots, buffers: dict, cam_id: str = "cam-vto", cam_name: str = "Videoportero"):
        self.db, self.snapshots, self.buffers, self.cam_id, self.cam_name = db, snapshots, buffers, cam_id, cam_name
        self._last_ring: dict = {}
        self.transit = 0            # visitas que solo pasaron (sin alerta)
        self.grab = None            # callable() -> bytes JPEG; lo fija main.py (snapshot.cgi del videoportero)

    def _save_snapshot(self, event_id: int, trigger: str, frame) -> None:
        if self.snapshots is None or frame is None:
            return
        try:
            self.snapshots.save(self.cam_id, frame, trigger, event_id=event_id)
        except (OSError, cv2.error) as exc:
            logger.warning("doorbell: no se pudo guardar la captura: %s", exc)

    def _insert(self, activity: str, alert: str, alert_type: str, extra: dict, severity: str = "low") -> int:
        payload = {"schema": 2, "source": "doorbell", "people": 1, "persons": [], "vehicles": 0, "activity": activity, "scene": "",
                   "relevant": True, "alerts": [alert], "alert_types": [alert_type], "severity": severity, "confidence": "high", **extra}
        return self.db.insert_event("nemotron", self.cam_id, payload, people=1, has_alert=True)

    def visitor_alert(self, v: dict) -> None:
        """Se llama al cerrarse una visita en el videoportero. v: visit_id, subject_id, name, first_ts, last_ts, scene, face."""
        dur = v["last_ts"] - v["first_ts"]
        rang = any(v["first_ts"] - RING_WINDOW_S <= t <= v["last_ts"] + RING_WINDOW_S for t in self._last_ring.values())
        if dur < MIN_VISIT_S and not v.get("name") and not rang:
            self.transit += 1
            return                    # alguien que paso frente al videoportero: queda como trafico (person_visits), sin alerta
        who = f" ({v['name']})" if v.get("name") else ""
        try:
            eid = self._insert(f"Visita frente al videoportero{who}: {int(dur)} s", f"Alguien se detuvo frente al videoportero{who}",
                               "visita_videoportero", {"doorbell": {"kind": "visit", "visit_id": v["visit_id"], "subject_id": v.get("subject_id"), "duration_s": round(dur, 1)}})
        except sqlite3.Error as exc:
            logger.warning("doorbell: no se pudo guardar la alerta de visita: %s", exc)
            return
        img = v.get("scene") or v.get("face")
        frame = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR) if img else None
        self._save_snapshot(eid, "visita", frame)
        logger.info("doorbell: visita %s alerta=%d", v["visit_id"], eid)

    def ring_alert(self, code: str, action: str, data: dict) -> None:
        """Evento nativo de Dahua. Solo los codigos de timbre/llamada generan alerta (DOORBELL_CODES); el resto queda en onvif_events."""
        if code not in RING_CODES or action in ("Stop",):
            return
        now = time.time()
        if now - self._last_ring.get(code, 0) < DEBOUNCE_S:
            return
        self._last_ring[code] = now
        try:
            eid = self._insert("Alguien toco el timbre del videoportero", "Llamada al videoportero", "timbre_videoportero",
                               {"doorbell": {"kind": "ring", "code": code, "action": action, "data": {k: v for k, v in list(data.items())[:6]}}}, "medium")
        except sqlite3.Error as exc:
            logger.warning("doorbell: no se pudo guardar la alerta de timbre: %s", exc)
            return
        buf = self.buffers.get(self.cam_id)
        frame = buf.peek_latest() if buf is not None and hasattr(buf, "peek_latest") else None
        frame = getattr(frame, "frame", frame)          # peek_latest devuelve un FrameEntry
        if isinstance(frame, tuple):
            frame = frame[0]
        if frame is None and self.grab is not None:
            try:
                frame = cv2.imdecode(np.frombuffer(self.grab(), np.uint8), cv2.IMREAD_COLOR)
            except (OSError, ValueError, requests.RequestException, DahuaError) as exc:
                logger.warning("doorbell: sin captura del timbre: %s", exc)
        self._save_snapshot(eid, "timbre", frame)
        logger.info("doorbell: timbre (%s/%s) alerta=%d", code, action, eid)
