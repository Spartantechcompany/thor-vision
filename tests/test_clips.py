"""Clips de alertas: se arma un MP4 con los cuadros del anillo, se registra y caduca."""
import os
import sqlite3
import threading
import time

import cv2
import numpy as np

from app.storage import clips as cl


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)


class Ring:
    def __init__(self, t0, n):
        ok = lambda i: cv2.imencode(".jpg", np.full((90, 160, 3), i * 5 % 255, np.uint8))[1].tobytes()
        self.items = [(t0 + i * 0.25, ok(i)) for i in range(n)]

    def frames_since(self, after):
        return [(t, j) for t, j in self.items if t > after]


def test_clip_se_escribe_y_se_registra(tmp_path):
    db, now = DB(), time.time()
    cs = cl.ClipStore(db, str(tmp_path), {"cam-189": Ring(now - 5, 24)})
    cid = cs._write(7, "cam-189", now, cs.preroll["cam-189"].frames_since(0))
    path = tmp_path / db._conn.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()[0]
    cap = cv2.VideoCapture(str(path))
    assert path.exists() and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 24


def test_pocos_cuadros_no_hacen_clip(tmp_path):
    db = DB()
    cs = cl.ClipStore(db, str(tmp_path), {"cam-189": Ring(time.time(), 2)})
    assert cs._write(7, "cam-189", time.time(), cs.preroll["cam-189"].frames_since(0)) is None and cs.stats["empty"] == 1


def test_camaras_sin_preroll_se_ignoran_y_los_viejos_caducan(tmp_path):
    db, now = DB(), time.time()
    cs = cl.ClipStore(db, str(tmp_path), {"cam-189": Ring(now - 5, 24)})
    cs.request(1, ["cam-vto"])
    assert cs.stats["requested"] == 0
    cid = cs._write(9, "cam-189", now - 200 * 86400, cs.preroll["cam-189"].frames_since(0))
    assert cid and cs.purge(now) == 1 and not os.listdir(tmp_path / time.strftime("%Y%m%d", time.localtime(now - 200 * 86400)))
