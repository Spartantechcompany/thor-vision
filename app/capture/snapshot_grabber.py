"""Snapshot JPEG bajo demanda de una camara Hanwha por SUNAPI (Digest, mismas credenciales del RTSP)."""
import logging
import threading
import time
import urllib.parse
from typing import Optional

import cv2
import numpy as np
import requests
import urllib3
from requests.auth import HTTPDigestAuth

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

_PATH = "/stw-cgi/video.cgi?msubmenu=snapshot&action=view&Channel=0"
FAIL_LIMIT = 3
COOLDOWN_S = 1800.0


class SnapshotGrabber:
    def __init__(self, cam_id: str, rtsp_url: str):
        u = urllib.parse.urlparse(rtsp_url)
        self.cam_id = cam_id
        self.host = u.hostname
        self._sess = requests.Session()
        self._sess.auth = HTTPDigestAuth(urllib.parse.unquote(u.username or ""),
                                         urllib.parse.unquote(u.password or ""))
        self._sess.verify = False
        self._base: Optional[str] = None
        self._lock = threading.Lock()
        self._fails = 0
        self._off_until = 0.0
        self.grabs = 0
        self.last_ms = 0.0

    def usable(self) -> bool:
        return time.time() >= self._off_until

    def _fail(self, why: str) -> None:
        self._fails += 1
        if self._fails >= FAIL_LIMIT:
            self._off_until = time.time() + COOLDOWN_S
            self._fails = 0
            self._base = None
            logger.info("Snapshot cam=%s: sin soporte/acceso (%s); pausa %.0f min",
                        self.cam_id, why, COOLDOWN_S / 60)

    def grab(self) -> Optional[np.ndarray]:
        if not self.usable():
            return None
        with self._lock:
            t0 = time.monotonic()
            bases = [self._base] if self._base else [f"http://{self.host}", f"https://{self.host}"]
            for base in bases:
                try:
                    r = self._sess.get(base + _PATH, timeout=6)
                except Exception as exc:
                    err = type(exc).__name__
                    continue
                if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        self._base, self._fails = base, 0
                        self.grabs += 1
                        self.last_ms = (time.monotonic() - t0) * 1000
                        return img
                    err = "decode"
                else:
                    err = f"http{r.status_code}:{r.content[:12]!r}"
            self._fail(err if bases else "sin base")
            return None
