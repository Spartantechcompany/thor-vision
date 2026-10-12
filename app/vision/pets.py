"""Recolector de recortes de perros/gatos para etiquetar las mascotas de la casa."""
import json
import logging
import os
import threading
import time

import cv2

logger = logging.getLogger(__name__)

ROOT = os.environ.get("PET_DIR", "/app/data/pets")
ENABLED = os.environ.get("PET_COLLECT", "true").lower() == "true"
MIN_CONF = float(os.environ.get("PET_MIN_CONF", "0.45"))
MIN_SIDE = int(os.environ.get("PET_MIN_SIDE", "36"))
COOLDOWN_S = float(os.environ.get("PET_COOLDOWN_S", "40"))
KNOWN_LABELS = {"akamaru", "mojo", "gigi"}
KNOWN_MIN_P = float(os.environ.get("PET_KNOWN_MIN_P", "0.6"))
MAX_TOTAL = int(os.environ.get("PET_MAX_TOTAL", "4000"))
LABELS = ("akamaru", "mojo", "gigi", "otro", "descartar")
NAMES = {"akamaru": "Akamaru", "mojo": "Mojo-jojo", "gigi": "Gigi"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_crops (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  file       TEXT NOT NULL,
  cam_id     TEXT NOT NULL,
  ts         REAL NOT NULL,
  cls        TEXT NOT NULL,
  conf       REAL,
  w          INTEGER,
  h          INTEGER,
  label      TEXT,
  labeled_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_pet_label ON pet_crops(label);
CREATE INDEX IF NOT EXISTS idx_pet_ts ON pet_crops(ts);
"""


class PetCollector:
    def __init__(self, db):
        self.db = db
        self._last: dict = {}
        self.presence = None             # PetPresence: estado en casa / fuera de cada mascota conocida
        self._seen: dict = {}            # cam_id -> (ts, [(etiqueta, prob)]) de los perros/gatos vistos por ultima vez
        self._model = None
        self._model_ts = 0.0
        self._lock = threading.Lock()
        os.makedirs(ROOT, exist_ok=True)
        with db._lock:
            db._conn.executescript(_SCHEMA)
            for col, typ in (("pred_label", "TEXT"), ("pred_conf", "REAL")):
                try:
                    db._conn.execute(f"ALTER TABLE pet_crops ADD COLUMN {col} {typ}")
                except Exception:
                    pass
            db._conn.commit()
            n = db._conn.execute("SELECT COUNT(*) FROM pet_crops").fetchone()[0]
        if n == 0:
            self._import_seed()
        logger.info("PetCollector: %s, %d recortes en la base", "activo" if ENABLED else "apagado", n)

    def _import_seed(self) -> None:
        meta_path = "/app/data/skillopt/dogs/meta.json"
        if not os.path.exists(meta_path):
            return
        try:
            meta = json.load(open(meta_path))
        except Exception:
            return
        day = os.path.join(ROOT, "seed")
        os.makedirs(day, exist_ok=True)
        k = 0
        with self.db._lock:
            for m in meta:
                src = m.get("f", "")
                if not os.path.exists(src):
                    continue
                dst = os.path.join(day, os.path.basename(src))
                try:
                    os.replace(src, dst) if os.path.dirname(src) == "/app/data/skillopt/dogs" else None
                except OSError:
                    continue
                w, h = (m.get("wh") or [0, 0])[:2]
                self.db._conn.execute("INSERT INTO pet_crops (file, cam_id, ts, cls, conf, w, h) VALUES (?,?,?,?,?,?,?)",
                                      (os.path.relpath(dst, ROOT), m["cam"], m["ts"], m["cls"], m.get("conf"), w, h))
                k += 1
            self.db._conn.commit()
        logger.info("PetCollector: importados %d recortes semilla", k)

    def known_now(self, cam_id: str, max_age_s: float = 150.0) -> bool:
        """True si los perros/gatos vistos hace poco en la camara son mascotas de la casa segun el clasificador (sin desconocidos)."""
        seen = self._seen.get(cam_id)
        return bool(seen and time.time() - seen[0] <= max_age_s and seen[1] and all(l in KNOWN_LABELS and p >= KNOWN_MIN_P for l, p in seen[1]))

    def _classify(self, crop):
        try:
            from app.vision import pet_model
            if self._model is None or time.time() - self._model_ts > 300:
                self._model, self._model_ts = pet_model.load_model(), time.time()
            return pet_model.classify(crop, model=self._model) if self._model else (None, 0.0)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            logger.debug("pets: sin clasificacion: %s", exc)
            return None, 0.0

    def update(self, cam_id, result, frame, ts=None) -> None:
        if not ENABLED:
            return
        now = ts or time.time()
        if now - self._last.get(cam_id, 0) < COOLDOWN_S:
            return
        objs = [o for o in (getattr(result, "objects", None) or []) if o["c"] in ("perro", "gato") and o["conf"] >= MIN_CONF]
        if not objs:
            return
        fh, fw = frame.shape[:2]
        rw, rh = getattr(result, "frame_w", 0) or fw, getattr(result, "frame_h", 0) or fh
        sx, sy = fw / rw, fh / rh
        saved = 0
        labs: list = []
        for o in sorted(objs, key=lambda o: -o["conf"])[:3]:
            x1, y1, x2, y2 = o["b"]
            x1, x2, y1, y2 = int(x1 * sx), int(x2 * sx), int(y1 * sy), int(y2 * sy)
            if min(x2 - x1, y2 - y1) < MIN_SIDE:
                continue
            pad = 6
            crop = frame[max(0, y1 - pad):min(fh, y2 + pad), max(0, x1 - pad):min(fw, x2 + pad)]
            k = 384 / max(crop.shape[:2])
            if k < 1.0:
                crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            day = time.strftime("%Y%m%d", time.localtime(now))
            os.makedirs(os.path.join(ROOT, day), exist_ok=True)
            rel = f"{day}/{cam_id}_{int(now * 1000)}_{saved}.jpg"
            cv2.imwrite(os.path.join(ROOT, rel), crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
            with self.db._lock:
                self.db._conn.execute("INSERT INTO pet_crops (file, cam_id, ts, cls, conf, w, h) VALUES (?,?,?,?,?,?,?)",
                                      (rel, cam_id, now, o["c"], o["conf"], x2 - x1, y2 - y1))
                self.db._conn.commit()
            saved += 1
            lab = self._classify(crop)
            labs.append(lab)
            if self.presence is not None and lab[0]:
                self.presence.seen(lab[0], cam_id, lab[1], now, crop)
        if labs:
            self._seen[cam_id] = (now, labs)
        if saved:
            self._last[cam_id] = now
            self._trim()

    def _trim(self) -> None:
        with self.db._lock:
            n = self.db._conn.execute("SELECT COUNT(*) FROM pet_crops").fetchone()[0]
            if n <= MAX_TOTAL:
                return
            rows = self.db._conn.execute("SELECT id, file FROM pet_crops WHERE label IS NULL ORDER BY ts ASC LIMIT ?", (n - MAX_TOTAL,)).fetchall()
            for rid, f in rows:
                try:
                    os.remove(os.path.join(ROOT, f))
                except OSError:
                    pass
                self.db._conn.execute("DELETE FROM pet_crops WHERE id=?", (rid,))
            self.db._conn.commit()
