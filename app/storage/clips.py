"""Clips de video de las alertas: los segundos previos (anillo de pre-roll) mas unos segundos despues, en MP4 H.264 que abre cualquier navegador.

El anillo guarda un cuadro cada 0.25 s a resolucion nativa, asi que el clip va a 4 cuadros por segundo: suficiente para ver quien llego y por donde se fue,
sin gastar decodificacion extra. Se pide con `request(event_id, [cam_ids])` y se genera en segundo plano."""
import logging
import os
import threading
import time
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)
PRE_S = float(os.environ.get("CLIP_PRE_S", "6"))
POST_S = float(os.environ.get("CLIP_POST_S", "6"))
WIDTH = int(os.environ.get("CLIP_WIDTH", "1280"))
TTL_DAYS = float(os.environ.get("CLIP_TTL_DAYS", os.environ.get("CAPTURE_TTL_DAYS", "90")))
FPS = 1.0 / float(os.environ.get("PREROLL_INTERVAL_S", "0.25"))
MAX_PER_EVENT = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, cam_id TEXT, ts REAL NOT NULL, path TEXT NOT NULL,
  secs REAL, bytes INTEGER);
CREATE INDEX IF NOT EXISTS idx_clips_event ON clips(event_id);
CREATE INDEX IF NOT EXISTS idx_clips_ts ON clips(ts);
"""


class ClipStore:
    def __init__(self, db, root: str, preroll: dict):
        self.db, self.root, self.preroll = db, root, preroll
        os.makedirs(root, exist_ok=True)
        with db._lock:
            db._conn.executescript(_SCHEMA)
            db._conn.commit()
        self._sem = threading.Semaphore(2)               # a lo mas dos codificaciones a la vez
        self.stats = {"requested": 0, "saved": 0, "empty": 0}

    def request(self, event_id: int, cam_ids: list, pre_s: float = PRE_S, post_s: float = POST_S) -> None:
        for cam in [c for c in dict.fromkeys(cam_ids) if c in self.preroll][:MAX_PER_EVENT]:
            self.stats["requested"] += 1
            threading.Thread(target=self._build, args=(event_id, cam, pre_s, post_s), daemon=True, name=f"clip-{cam}").start()

    def _build(self, event_id: int, cam: str, pre_s: float, post_s: float) -> None:
        t0 = time.time()
        pr = self.preroll[cam]
        frames = dict(pr.frames_since(t0 - pre_s))
        time.sleep(post_s)
        frames.update(dict(pr.frames_since(t0 - 0.01)))
        with self._sem:
            try:
                self._write(event_id, cam, t0, sorted(frames.items()))
            except Exception as exc:                       # noqa: BLE001 - un clip fallido no debe afectar la alerta
                logger.warning("clip evento %s cam %s: %s", event_id, cam, exc)

    def _write(self, event_id: int, cam: str, t0: float, frames: list) -> Optional[int]:
        if len(frames) < 4:
            self.stats["empty"] += 1
            return None
        day = time.strftime("%Y%m%d", time.localtime(t0))
        rel = f"{day}/{event_id}_{cam}.mp4"
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writer = None
        for _, jpg in frames:
            im = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if im is None:
                continue
            h, w = im.shape[:2]
            if w > WIDTH:
                im = cv2.resize(im, (WIDTH, int(h * WIDTH / w)), interpolation=cv2.INTER_AREA)
                h, w = im.shape[:2]
            if writer is None:
                size = (w - w % 2, h - h % 2)
                for fourcc in ("avc1", "mp4v"):                 # avc1 (H.264) abre en cualquier navegador; mp4v solo como respaldo
                    writer = cv2.VideoWriter(path, cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*fourcc), FPS, size)
                    if writer.isOpened():
                        if fourcc != "avc1":
                            logger.warning("clips: sin codificador H.264, se usa %s (puede no reproducirse en el navegador)", fourcc)
                        break
                if not writer.isOpened():
                    raise RuntimeError("no se pudo abrir ningun codificador de video")
            writer.write(im[:size[1], :size[0]])
        if writer is None:
            return None
        writer.release()
        secs = len(frames) / FPS
        with self.db._lock:
            cur = self.db._conn.execute("INSERT INTO clips (event_id, cam_id, ts, path, secs, bytes) VALUES (?,?,?,?,?,?)",
                                        (event_id, cam, t0, rel, secs, os.path.getsize(path)))
            self.db._conn.commit()
        self.stats["saved"] += 1
        logger.info("CLIP evento=%s cam=%s %.0f s %d KB", event_id, cam, secs, os.path.getsize(path) // 1024)
        return cur.lastrowid

    def purge(self, now: Optional[float] = None) -> int:
        """Borra los clips mas viejos que CLIP_TTL_DAYS (archivo y fila)."""
        if TTL_DAYS <= 0:
            return 0
        cut = (now or time.time()) - TTL_DAYS * 86400
        with self.db._lock:
            rows = self.db._conn.execute("SELECT id, path FROM clips WHERE ts<?", (cut,)).fetchall()
            self.db._conn.execute("DELETE FROM clips WHERE ts<?", (cut,))
            self.db._conn.commit()
        for _, rel in rows:
            try:
                os.remove(os.path.join(self.root, rel))
            except OSError:
                pass
        return len(rows)

    def start_purger(self) -> threading.Event:
        stop = threading.Event()

        def loop():
            while not stop.wait(3600):
                try:
                    self.purge()
                except Exception as exc:     # noqa: BLE001
                    logger.warning("clips purge: %s", exc)
        threading.Thread(target=loop, daemon=True, name="clips-purge").start()
        return stop
