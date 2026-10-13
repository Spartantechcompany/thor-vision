"""Identidad por cuerpo (fase 1): embedding ReID en la GPU + color de ropa de cada visita, y una sugerencia de quien podria ser.

El embedding sale de un modelo de re-identificacion de personas (ONNX, onnxruntime-gpu) sobre la mejor captura de cuerpo de la visita. Con las camaras
de THOR (vista alta, baja resolucion, noche en infrarrojo) la senal es moderada (AUC ~0.73 en visitas con rostro confirmado), por eso solo SUGIERE:
nunca asigna ni fusiona identidades. La sugerencia exige parecido alto con alguien con nombre visto en las ultimas horas y ropa compatible.
"""
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import urllib.request
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = os.environ.get("REID_MODEL", "/app/data/models/youtureid.onnx")
MODEL_URL = "https://huggingface.co/opencv/person_reid_youtureid/resolve/main/person_reid_youtu_2021nov.onnx"
ENABLED = os.environ.get("BODYID_ENABLED", "true").lower() == "true"
GUESS_SIM = float(os.environ.get("BODYID_GUESS_SIM", "0.55"))
WINDOW_H = float(os.environ.get("BODYID_WINDOW_H", "12"))
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
ACHROMATIC = {"negro", "blanco", "gris"}


def prep(bgr: np.ndarray) -> np.ndarray:
    """Recorte -> tensor 3x256x128 con relleno (sin deformar la persona)."""
    h, w = bgr.shape[:2]
    k = min(128 / w, 256 / h)
    nw, nh = max(1, int(w * k)), max(1, int(h * k))
    r = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
    can = np.full((256, 128, 3), 114, np.uint8)
    y0, x0 = (256 - nh) // 2, (128 - nw) // 2
    can[y0:y0 + nh, x0:x0 + nw] = r
    return (((can[:, :, ::-1].astype(np.float32) / 255.0) - _MEAN) / _STD).transpose(2, 0, 1)


def color_name(patch: np.ndarray) -> Optional[str]:
    """Color dominante de una region (BGR) en palabras; None si no hay datos."""
    if patch is None or patch.size == 0:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    h, s, v = (float(np.median(hsv[:, i])) for i in range(3))
    if v < 55:
        return "negro"
    if s < 45:
        return "blanco" if v > 185 else "gris"
    if h < 8 or h >= 172:
        return "rojo"
    if h < 20:
        return "cafe" if v < 150 else "naranja"
    if h < 35:
        return "amarillo"
    if h < 85:
        return "verde"
    if h < 132:
        return "azul"
    if h < 160:
        return "morado"
    return "rosa"


def clothing(body: np.ndarray) -> dict:
    """Color de la parte de arriba y de abajo del cuerpo (zona central, sin cabeza ni pies)."""
    h, w = body.shape[:2]
    cx0, cx1 = int(w * 0.28), int(w * 0.72)
    return {"upper": color_name(body[int(h * 0.22):int(h * 0.50), cx0:cx1]), "lower": color_name(body[int(h * 0.56):int(h * 0.88), cx0:cx1])}


def compatible(a: dict, b: dict) -> bool:
    """Misma ropa: algun color coincide y ninguno de los dos pares es claramente distinto. De noche (infrarrojo) todo es gris: se tolera."""
    ok = False
    for k in ("upper", "lower"):
        x, y = a.get(k), b.get(k)
        if not x or not y:
            continue
        if x == y or (x in ACHROMATIC and y in ACHROMATIC):
            ok = True
        else:
            return False
    return ok


def describe(c: dict) -> str:
    parts = []
    if c.get("upper"):
        parts.append(f"arriba {c['upper']}")
    if c.get("lower"):
        parts.append(f"abajo {c['lower']}")
    return ", ".join(parts)


class BodyID:
    def __init__(self, db):
        self.db = db
        self.stats = {"embedded": 0, "guessed": 0, "errors": 0, "ms": 0.0}
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._sess = None
        self._in = None
        self._lock = threading.Lock()
        with db._lock:
            for col, typ in (("body_emb", "BLOB"), ("attrs", "TEXT")):
                try:
                    db._conn.execute(f"ALTER TABLE person_visits ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass                       # ya existia
            db._conn.commit()
        threading.Thread(target=self._loop, daemon=True, name="bodyid").start()

    # ---- modelo (carga perezosa en la GPU)
    def _load(self) -> bool:
        if self._sess is not None:
            return True
        try:
            if not os.path.exists(MODEL_PATH):
                os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
                urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            import onnxruntime as ort
            self._sess = ort.InferenceSession(MODEL_PATH, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            self._in = self._sess.get_inputs()[0].name
            logger.info("BodyID: modelo %s en %s", os.path.basename(MODEL_PATH), self._sess.get_providers()[0])
            return True
        except (OSError, ImportError, RuntimeError, ValueError) as exc:   # sin modelo o sin GPU: la fase 1 se apaga sola, el resto sigue
            logger.warning("BodyID: no se pudo cargar el modelo: %s", exc)
            self._sess = None
            return False

    def embed(self, bgr: np.ndarray) -> Optional[np.ndarray]:
        if not self._load():
            return None
        with self._lock:
            out = self._sess.run(None, {self._in: prep(bgr)[None]})[0].reshape(1, -1)[0]
        n = float(np.linalg.norm(out))
        return (out / n).astype(np.float32) if n > 0 and np.isfinite(out).all() else None

    # ---- cola
    def submit(self, vid: int, jpeg: bytes) -> None:
        if not ENABLED or vid is None or not jpeg:
            return
        try:
            self._q.put_nowait((vid, jpeg))
        except queue.Full:
            pass

    def _loop(self) -> None:
        while True:
            vid, jpeg = self._q.get()
            try:
                self.process(vid, jpeg)
            except (ValueError, TypeError, sqlite3.Error, cv2.error, OSError) as exc:
                self.stats["errors"] += 1
                logger.warning("BodyID visita %s: %s", vid, exc)

    def process(self, vid: int, jpeg: bytes) -> Optional[dict]:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.shape[0] < 60:
            return None
        t0 = time.monotonic()
        emb = self.embed(img)
        attrs = clothing(img)
        if emb is None:
            return None
        guess = None
        with self.db._lock:
            sid = self.db._conn.execute("SELECT subject_id, start_ts FROM person_visits WHERE id=?", (vid,)).fetchone()
        if sid is not None and sid[0] is None:                        # sin identidad por rostro: se busca un parecido
            guess = self._guess(emb, attrs, sid[1] or time.time())
        if guess:
            attrs["guess"] = guess
            self.stats["guessed"] += 1
        self.db.update_person_visit(vid, body_emb=emb.tobytes(), attrs=json.dumps(attrs, ensure_ascii=False))
        with self.db._lock:
            self.db._conn.commit()
        self.stats["embedded"] += 1
        self.stats["ms"] = round((time.monotonic() - t0) * 1000, 1)
        return attrs

    def _guess(self, emb: np.ndarray, attrs: dict, ts: float) -> Optional[dict]:
        """El conocido (con nombre) al que mas se parece entre quienes se vieron en las ultimas horas."""
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT v.subject_id, COALESCE(s.name,''), v.body_emb, v.attrs FROM person_visits v JOIN subjects s ON s.id=v.subject_id "
                "WHERE s.named=1 AND v.body_emb IS NOT NULL AND v.fp=0 AND v.start_ts BETWEEN ? AND ? ORDER BY v.start_ts DESC LIMIT 600",
                (ts - WINDOW_H * 3600, ts)).fetchall()
        best: dict = {}
        for sid, name, blob, at in rows:
            v = np.frombuffer(blob, np.float32)
            if v.size != emb.size:
                continue
            sim = float(v @ emb)
            try:
                other = json.loads(at or "{}")
            except ValueError:
                other = {}
            if sim >= GUESS_SIM and compatible(attrs, other) and sim > best.get(sid, (0, ""))[0]:
                best[sid] = (sim, name)
        if not best:
            return None
        sid, (sim, name) = max(best.items(), key=lambda kv: kv[1][0])
        return {"id": sid, "name": name, "sim": round(sim, 3)}

    def backfill(self, hours: float = 24.0, limit: int = 20000) -> int:
        """Procesa visitas recientes que aun no tienen embedding (mide el efecto en datos reales)."""
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT id, body FROM person_visits WHERE body IS NOT NULL AND body_emb IS NULL AND fp=0 AND static=0 AND start_ts>=? ORDER BY start_ts ASC LIMIT ?",
                (time.time() - hours * 3600, limit)).fetchall()
        n = 0
        for vid, body in rows:
            try:
                if self.process(vid, body):
                    n += 1
            except (ValueError, TypeError, sqlite3.Error, cv2.error, OSError):
                self.stats["errors"] += 1
        return n
