"""Snapshot JPEG bajo demanda de una camara Hanwha por SUNAPI (Digest, mismas credenciales del RTSP)."""
import logging
import re
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
_PROFILES = "/stw-cgi/media.cgi?msubmenu=videoprofile&action=view"
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
        self._profile: Optional[int] = None      # perfil MJPEG de la camara: el snapshot SUNAPI lo necesita (error 604 si no existe)
        self._profile_checked = False

    def _retry(self, base: str) -> Optional[np.ndarray]:
        r = self._sess.get(base + _PATH + f"&Profile={self._profile}", timeout=6)
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR) if r.content[:2] == b"\xff\xd8" else None
        if img is not None:
            self._base, self._fails = base, 0
            self.grabs += 1
        return img

    def usable(self) -> bool:
        return time.time() >= self._off_until

    def _fail(self, why: str) -> None:
        self._fails += 1
        if self._fails >= FAIL_LIMIT:
            self._off_until = time.time() + COOLDOWN_S
            self._fails = 0
            self._base = None
            self._profile_checked = False            # tras la pausa se vuelve a buscar un perfil MJPEG (puede haberse creado en la camara)
            logger.info("Snapshot cam=%s: sin soporte/acceso (%s); pausa %.0f min",
                        self.cam_id, why, COOLDOWN_S / 60)

    def _find_mjpeg_profile(self, base: str) -> Optional[int]:
        """Camaras como la QNO-8010R solo traen perfiles H.264/H.265: el snapshot falla con 604 hasta que se crea uno MJPEG. Si existe, se usa."""
        self._profile_checked = True
        try:
            t = self._sess.get(base + _PROFILES, timeout=6).text
        except requests.RequestException as exc:
            logger.debug("Snapshot cam=%s: no se pudieron leer los perfiles: %s", self.cam_id, exc)
            return None
        enc = {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"Channel\.0\.Profile\.(\d+)\.EncodingType=(\S+)", t)}
        found = sorted(k for k, v in enc.items() if v.upper() == "MJPEG")
        if found:
            logger.info("Snapshot cam=%s: usando el perfil MJPEG %d", self.cam_id, found[0])
            return found[0]
        logger.info("Snapshot cam=%s: sin perfil MJPEG (perfiles %s); crea uno en la camara para tener snapshots a resolucion completa", self.cam_id, enc)
        return None

    def grab(self) -> Optional[np.ndarray]:
        if not self.usable():
            return None
        with self._lock:
            t0 = time.monotonic()
            bases = [self._base] if self._base else [f"http://{self.host}", f"https://{self.host}"]
            for base in bases:
                try:
                    r = self._sess.get(base + _PATH + (f"&Profile={self._profile}" if self._profile else ""), timeout=6)
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
                    if r.status_code == 200 and b"604" in r.content[:60] and not self._profile and not self._profile_checked:
                        self._profile = self._find_mjpeg_profile(base)          # una sola vez por arranque; si hay perfil MJPEG se reintenta de inmediato
                        if self._profile:
                            return self._retry(base)
            self._fail(err if bases else "sin base")
            return None
