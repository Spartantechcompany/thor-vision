"""Cliente del servidor local Clef-flash (app/vision/clef_server.py). Decisiones tipadas con probabilidad, sin texto libre.

Si el servidor no responde, `decide` devuelve None y el pipeline sigue con sus reglas de siempre (interruptor automatico:
tras CLEF_FAILS fallos seguidos se deja de llamar CLEF_PAUSE_S segundos)."""
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)
URL = os.environ.get("CLEF_URL", "http://127.0.0.1:20140")
TIMEOUT_S = float(os.environ.get("CLEF_TIMEOUT_S", "20"))
FAILS = int(os.environ.get("CLEF_FAILS", "3"))
PAUSE_S = float(os.environ.get("CLEF_PAUSE_S", "30"))

PERSON_QUESTIONS = {
    "is_person": {"type": "noul", "instructions": (
        "Does the image show a real human being (a body, head or limbs of a person)? "
        "Answer false for trees, tree trunks, poles, posts, plants, parked cars, signs, shadows, reflections or any other object.")},
    "what": {"type": "choice", "instructions": "What is the main subject of the image?", "criteria": {
        "person": "A human being, walking, standing, sitting or riding something",
        "tree_or_pole": "A tree, trunk, pole, post, fence, plant or wall",
        "vehicle": "A car, motorcycle, bus, truck or bicycle",
        "animal": "A dog, cat or other animal",
        "shadow_or_light": "A shadow, reflection, glare, headlights or noise",
        "other": "Anything else, or too unclear to tell"}},
}


SAME_FACE_Q = {"same": {"type": "noul", "instructions": "Are these two face photos the same person? Judge only facial features (eyes, nose, mouth, face shape, "
                                                    "hairline); ignore lighting, angle, image quality and clothing."}}


FACE_CLEAR_Q = {"clear": {"type": "noul", "instructions": "Is this face photo clear enough to recognize the person? Answer false if it is blurry, smeared, "
                                                      "streaked by video compression, pixelated, too small, too dark, the face is mostly hidden or turned away, "
                                                      "or it is not a face at all."}}


class ClefClient:
    def __init__(self, url: str = URL):
        self.url = url.rstrip("/")
        self._fails, self._paused_until = 0, 0.0
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "errors": 0, "skipped": 0}

    def available(self) -> bool:
        return time.time() >= self._paused_until

    def decide(self, state, images: list, questions: dict, timeout: float = TIMEOUT_S) -> Optional[dict]:
        """images: JPEG en bytes. Devuelve la respuesta del servidor (answers por pregunta) o None."""
        if not self.available():
            self.stats["skipped"] += 1
            return None
        body = json.dumps({"state": state, "images": [base64.b64encode(i).decode() for i in images], "questions": questions}).encode()
        req = urllib.request.Request(self.url + "/decide", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read())
            with self._lock:
                self._fails = 0
            self.stats["calls"] += 1
            return out
        except (OSError, urllib.error.URLError, ValueError) as exc:
            with self._lock:
                self._fails += 1
                if self._fails >= FAILS:
                    self._paused_until, self._fails = time.time() + PAUSE_S, 0
                    logger.warning("Clef: %d fallos seguidos, pausa de %.0f s (%s)", FAILS, PAUSE_S, exc)
            self.stats["errors"] += 1
            return None

    def same_person(self, face_a: bytes, face_b: bytes) -> Optional[float]:
        """Probabilidad de que dos rostros sean la misma persona (solo rasgos de la cara), o None si no hay respuesta."""
        out = self.decide({"task": "Two face crops from security cameras taken at different times."}, [face_a, face_b], SAME_FACE_Q)
        try:
            return float(out["answers"]["same"]["noul"]) if out else None
        except (KeyError, TypeError, ValueError):
            return None

    def face_clear(self, face: bytes) -> Optional[float]:
        """Probabilidad de que el recorte sea una cara nitida y reconocible (sirve de miniatura), o None si no hay respuesta."""
        out = self.decide({"task": "Face crop from a security camera, to decide if it is good enough to show as the person's thumbnail."}, [face], FACE_CLEAR_Q)
        try:
            return float(out["answers"]["clear"]["noul"]) if out else None
        except (KeyError, TypeError, ValueError):
            return None

    def verify_person(self, crop_jpg: bytes, where: str = "") -> Optional[dict]:
        """El detector YOLO marco este recorte como persona: {'p_person', 'what', 'p_what', 'ms'} o None si no hay respuesta."""
        out = self.decide({"source": "security camera crop flagged as a person by an object detector", "camera": where}, [crop_jpg], PERSON_QUESTIONS)
        if not out:
            return None
        try:
            a = out["answers"]
            noul = a["is_person"]
            p = float(noul["noul"] if "noul" in noul else noul.get("probability", 0.0))      # noul: probabilidad de "verdadero"
            ch = a["what"]
            return {"p_person": p, "what": ch.get("choice"), "p_what": float((ch.get("probabilities") or {}).get(ch.get("choice"), 0.0)), "ms": out.get("ms")}
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Clef: respuesta inesperada (%s): %s", exc, str(out)[:200])
            return None
