"""Anillo de pre-roll por camara: JPEGs a resolucion nativa cada ~250 ms durante ~6 s."""
import logging
import os
import threading
import time
from collections import deque

import cv2

logger = logging.getLogger(__name__)

SECONDS = float(os.environ.get("PREROLL_SECONDS", "6"))
INTERVAL_S = float(os.environ.get("PREROLL_INTERVAL_S", "0.25"))
QUALITY = int(os.environ.get("PREROLL_JPEG_QUALITY", "85"))


class PreRoll(threading.Thread):
    def __init__(self, cam_id: str, buffer):
        super().__init__(daemon=True, name=f"preroll-{cam_id}")
        self.cam_id = cam_id
        self.buffer = buffer
        self._ring: deque = deque(maxlen=int(SECONDS / INTERVAL_S) + 2)
        self._lock = threading.Lock()
        self._last_mono = 0.0
        self._stop_ev = threading.Event()
        self.enc_ms = 0.0
        self.encoded = 0
        self._last_log = time.monotonic()

    def run(self) -> None:
        while not self._stop_ev.is_set():
            t0 = time.monotonic()
            try:
                e = self.buffer.peek_latest()
                if e is not None and e.timestamp != self._last_mono:
                    self._last_mono = e.timestamp
                    ok, buf = cv2.imencode(".jpg", e.frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
                    if ok:
                        wall = time.time() - (time.monotonic() - e.timestamp)
                        with self._lock:
                            self._ring.append((wall, buf.tobytes()))
                        ms = (time.monotonic() - t0) * 1000
                        self.enc_ms = ms if not self.encoded else self.enc_ms * 0.9 + ms * 0.1
                        self.encoded += 1
            except Exception as exc:
                logger.debug("preroll %s: %s", self.cam_id, exc)
            if time.monotonic() - self._last_log > 600:
                self._last_log = time.monotonic()
                with self._lock:
                    n, kb = len(self._ring), sum(len(j) for _, j in self._ring) // 1024
                logger.info("PREROLL_RING cam=%s frames=%d %dKB enc=%.0fms", self.cam_id, n, kb, self.enc_ms)
            self._stop_ev.wait(max(0.0, INTERVAL_S - (time.monotonic() - t0)))

    def frames_since(self, after_ts: float) -> list:
        with self._lock:
            return [(t, j) for t, j in self._ring if t > after_ts]

    def stop(self) -> None:
        self._stop_ev.set()
