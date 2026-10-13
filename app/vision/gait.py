"""Marcha (fase de recoleccion): vector de rasgos del caminar a partir de la postura (YOLO26-pose) de una rafaga corta de una persona seguida.

Solo se guarda el vector en cada visita; NO se usa para identificar hasta medir (AUC con visitas de rostro confirmado, /api/gait/eval).
Rasgos (todos normalizados por el largo del torso, para no depender de la distancia a la camara): cadencia, amplitud vertical y horizontal de la
separacion de tobillos, largo de pierna, ancho de hombros, inclinacion media y variacion del torso, balanceo de brazos.
"""
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("GAIT_ENABLED", "true").lower() == "true"
CAMS = {c.strip() for c in os.environ.get("GAIT_CAMS", "cam-228,cam-120,cam-113,cam-236,cam-215,cam-sala-juntas,cam-cowork").split(",") if c.strip()}
SECONDS = float(os.environ.get("GAIT_SECONDS", "3"))
FPS = float(os.environ.get("GAIT_FPS", "12"))
MIN_BODY_H = float(os.environ.get("GAIT_MIN_BODY_H", "140"))      # alto de la persona en pixeles del cuadro original
NAMES = ["cadencia_hz", "paso_vertical", "paso_horizontal", "largo_pierna", "ancho_hombros", "inclinacion", "inclinacion_var", "balanceo_brazos"]
# indices COCO
LSH, RSH, LEL, REL, LWR, RWR, LHI, RHI, LKN, RKN, LAN, RAN = 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gait_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, visit_id INTEGER UNIQUE, cam_id TEXT, ts REAL, n_frames INTEGER, fps REAL, quality REAL, feat TEXT);
CREATE INDEX IF NOT EXISTS idx_gait_visit ON gait_samples(visit_id);
"""


def _pt(k: np.ndarray, i: int, thr: float = 0.3):
    return k[i, :2] if k[i, 2] >= thr else None


def features(seq: list, fps: float) -> Optional[tuple]:
    """seq: lista de arreglos (17,3) [x, y, conf] ordenados en el tiempo. Devuelve (vector de 8 rasgos, calidad 0..1) o None si no alcanza."""
    if len(seq) < 10 or fps < 5:
        return None
    L, sep_y, sep_x, leg, shw, lean, arm = [], [], [], [], [], [], []
    for k in seq:
        ls, rs, lh, rh = _pt(k, LSH), _pt(k, RSH), _pt(k, LHI), _pt(k, RHI)
        if ls is None or rs is None or lh is None or rh is None:
            continue
        ms, mh = (ls + rs) / 2, (lh + rh) / 2
        torso = float(np.linalg.norm(ms - mh))
        if torso < 8:
            continue
        L.append(torso)
        la, ra = _pt(k, LAN), _pt(k, RAN)
        if la is not None and ra is not None:
            sep_y.append(float(la[1] - ra[1]) / torso)
            sep_x.append(float(abs(la[0] - ra[0])) / torso)
        for h, kn, an in ((lh, LKN, LAN), (rh, RKN, RAN)):
            kp, ap = _pt(k, kn), _pt(k, an)
            if kp is not None and ap is not None:
                leg.append((float(np.linalg.norm(h - kp)) + float(np.linalg.norm(kp - ap))) / torso)
        shw.append(float(np.linalg.norm(ls - rs)) / torso)
        v = ms - mh
        lean.append(float(np.arctan2(v[0], -v[1])))
        for sh, wr in ((ls, LWR), (rs, RWR)):
            w = _pt(k, wr)
            if w is not None:
                arm.append(float(w[1] - sh[1]) / torso)
    quality = len(L) / len(seq)
    if len(L) < 8 or len(sep_y) < 8 or not leg:
        return None
    s = np.asarray(sep_y) - np.mean(sep_y)
    spec = np.abs(np.fft.rfft(s * np.hanning(len(s))))
    freqs = np.fft.rfftfreq(len(s), d=1.0 / fps)
    band = (freqs >= 0.6) & (freqs <= 4.0)
    cad = float(freqs[band][np.argmax(spec[band])]) if band.any() and spec[band].max() > 0 else 0.0
    vec = [cad, float(np.std(sep_y)), float(np.mean(sep_x)), float(np.mean(leg)), float(np.mean(shw)), float(np.mean(lean)), float(np.std(lean)),
           float(np.std(arm)) if len(arm) >= 4 else 0.0]
    return vec, float(quality)


class GaitCollector:
    def __init__(self, db, models, buffers: dict):
        self.db, self.models, self.buffers = db, models, buffers
        self.stats = {"samples": 0, "skipped": 0, "errors": 0}
        self._q: queue.Queue = queue.Queue(maxsize=6)
        self._last: dict = {}
        with db._lock:
            db._conn.executescript(_SCHEMA)
            db._conn.commit()
        if ENABLED:
            threading.Thread(target=self._loop, daemon=True, name="gait").start()

    def submit(self, cam_id: str, visit_id: int, bbox: tuple, dims: tuple) -> bool:
        if not ENABLED or cam_id not in CAMS or visit_id is None or time.time() - self._last.get(cam_id, 0) < SECONDS + 2:
            return False
        self._last[cam_id] = time.time()
        try:
            self._q.put_nowait((cam_id, visit_id, bbox, dims))
            return True
        except queue.Full:
            return False

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            try:
                self._run(*job)
            except (RuntimeError, ValueError, TypeError, sqlite3.Error, cv2.error, OSError) as exc:
                self.stats["errors"] += 1
                logger.warning("gait: %s", exc)

    def _run(self, cam_id: str, visit_id: int, bbox: tuple, dims: tuple) -> None:
        buf = self.buffers.get(cam_id)
        model = self.models._pose_model() if self.models is not None else None
        if buf is None or model is None:
            return
        frames, stamps = [], []
        t_end = time.monotonic() + SECONDS
        last = None
        while time.monotonic() < t_end:                                     # muestreo a ~FPS desde el buffer (sin pedir nada mas a la camara)
            e = buf.peek_latest()
            if e is not None and e.timestamp != last:
                last = e.timestamp
                f = e.frame
                k = min(1.0, 960.0 / f.shape[1])
                frames.append(cv2.resize(f, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else f)
                stamps.append(e.timestamp)
            time.sleep(max(0.0, 1.0 / FPS - 0.005))
        if len(frames) < 10:
            self.stats["skipped"] += 1
            return
        h0, w0 = frames[0].shape[:2]
        sx, sy = (w0 / dims[0], h0 / dims[1]) if dims and dims[0] and dims[1] else (1.0, 1.0)
        target = (bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)
        if (target[3] - target[1]) * (1 / max(sy, 1e-6)) < MIN_BODY_H:
            self.stats["skipped"] += 1
            return
        res = model(frames, imgsz=640, conf=0.25, verbose=False, device=self.models.device)
        seq, prev = [], target
        for r in reversed(res):                                              # del cuadro mas nuevo al mas viejo, encadenando la caja
            if r.boxes is None or r.keypoints is None or len(r.boxes) == 0:
                seq.append(None)
                continue
            boxes = r.boxes.xyxy.cpu().numpy()
            cx, cy = (prev[0] + prev[2]) / 2, (prev[1] + prev[3]) / 2
            d = [np.hypot((b[0] + b[2]) / 2 - cx, (b[1] + b[3]) / 2 - cy) for b in boxes]
            i = int(np.argmin(d))
            if d[i] > max(prev[2] - prev[0], prev[3] - prev[1]):
                seq.append(None)
                continue
            prev = tuple(boxes[i])
            seq.append(np.concatenate([r.keypoints.xy[i].cpu().numpy(), r.keypoints.conf[i].cpu().numpy()[:, None]], axis=1))
        seq = [s for s in reversed(seq) if s is not None]
        fps = (len(frames) - 1) / max(1e-3, stamps[-1] - stamps[0])
        out = features(seq, fps)
        if out is None:
            self.stats["skipped"] += 1
            return
        vec, q = out
        with self.db._lock:
            self.db._conn.execute("INSERT OR REPLACE INTO gait_samples (visit_id, cam_id, ts, n_frames, fps, quality, feat) VALUES (?,?,?,?,?,?,?)",
                                  (visit_id, cam_id, time.time(), len(seq), round(fps, 1), round(q, 2), json.dumps([round(x, 4) for x in vec])))
            self.db._conn.commit()
        self.stats["samples"] += 1


def auc(pos: list, neg: list) -> float:
    """Probabilidad de que un par de la misma persona sea mas parecido (menor distancia) que uno de personas distintas."""
    if not pos or not neg:
        return float("nan")
    a = np.concatenate([pos, neg])
    ranks = a.argsort().argsort() + 1
    # menor distancia = mas parecido: AUC = P(d_pos < d_neg)
    rp = ranks[:len(pos)].sum()
    u = rp - len(pos) * (len(pos) + 1) / 2
    return float(1.0 - u / (len(pos) * len(neg)))
