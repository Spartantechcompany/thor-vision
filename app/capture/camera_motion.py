import logging
import threading
import time
import urllib.parse
from collections import deque
from typing import Optional

import requests
import urllib3
from requests.auth import HTTPDigestAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_VA_KEYS = (
    "Channel.0.VideoAnalytics.Passing",
    "Channel.0.VideoAnalytics.Intrusion",
    "Channel.0.VideoAnalytics.Entering",
    "Channel.0.VideoAnalytics.Exiting",
)


class CameraMotionMonitor(threading.Thread):
    """
    Sondea el estado de eventos nativo de la camara (Hanwha SUNAPI,
    eventstatus.cgi, Digest con las mismas credenciales del RTSP) para saber
    en tiempo real si la propia camara esta detectando movimiento.

    `usable()` solo es True si el detector esta encendido en la camara
    (videoanalysis DetectionType != Off) Y el endpoint responde: asi el
    worker de VLM sabe cuando puede confiar en esta senal y cuando debe caer
    al frame-diff por software.
    """

    CFG_REFRESH_S = 300.0
    STALE_S = 10.0

    def __init__(self, cam_id: str, rtsp_url: str, poll_s: float = 1.0):
        super().__init__(daemon=True, name=f"camotion-{cam_id}")
        u = urllib.parse.urlparse(rtsp_url)
        self.cam_id = cam_id
        self.host = u.hostname
        self.poll_s = poll_s
        self._sess = requests.Session()
        self._sess.auth = HTTPDigestAuth(
            urllib.parse.unquote(u.username or ""),
            urllib.parse.unquote(u.password or ""),
        )
        self._sess.verify = False
        self._base: Optional[str] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._reachable = False
        self._enabled = False
        self._detection_type = ""
        self._last_ok_t = 0.0
        self._active = False
        self._since = 0.0
        self._last_rise_ts = 0.0
        self._last_end_ts = 0.0
        self._rises: deque = deque(maxlen=2000)

    def _get(self, path: str, timeout: float = 4.0) -> str:
        r = self._sess.get(f"{self._base}/stw-cgi/{path}", timeout=timeout)
        r.raise_for_status()
        return r.text

    def _detect_base(self) -> bool:
        for scheme in ("http", "https"):
            self._base = f"{scheme}://{self.host}"
            try:
                self._get("eventstatus.cgi?msubmenu=eventstatus&action=check")
                logger.info("CameraMotion cam=%s: SUNAPI via %s", self.cam_id, scheme)
                return True
            except Exception:
                continue
        self._base = None
        return False

    def _refresh_config(self) -> None:
        t = self._get("eventsources.cgi?msubmenu=videoanalysis&action=view")
        dt = ""
        for line in t.splitlines():
            if line.startswith("Channel.0.DetectionType="):
                dt = line.split("=", 1)[1].strip()
                break
        with self._lock:
            self._detection_type = dt
            self._enabled = dt not in ("", "Off")

    def _poll_once(self) -> None:
        t = self._get("eventstatus.cgi?msubmenu=eventstatus&action=check")
        now = time.time()
        active = "Channel.0.MotionDetection=True" in t or any(
            f"{k}=True" in t for k in _VA_KEYS
        )
        with self._lock:
            if active and not self._active:
                self._since = now
                self._last_rise_ts = now
                self._rises.append(now)
            elif not active and self._active:
                self._last_end_ts = now
            self._active = active
            self._reachable = True
            self._last_ok_t = now

    def run(self) -> None:
        last_cfg = 0.0
        fails = 0
        while not self._stop.is_set():
            try:
                if self._base is None and not self._detect_base():
                    raise RuntimeError("SUNAPI no responde")
                if time.time() - last_cfg >= self.CFG_REFRESH_S:
                    self._refresh_config()
                    last_cfg = time.time()
                self._poll_once()
                if fails:
                    logger.info("CameraMotion cam=%s: recuperado", self.cam_id)
                fails = 0
                self._stop.wait(self.poll_s)
            except Exception as e:
                fails += 1
                with self._lock:
                    self._reachable = False
                    if self._active:
                        self._active = False
                        self._last_end_ts = time.time()
                if fails in (1, 30):
                    logger.warning("CameraMotion cam=%s: %s", self.cam_id, str(e)[:80])
                self._base = None if fails % 10 == 0 else self._base
                self._stop.wait(min(5.0, self.poll_s * (1 + fails)))

    def usable(self) -> bool:
        with self._lock:
            return (self._reachable and self._enabled
                    and time.time() - self._last_ok_t < self.STALE_S)

    def active_recently(self, tail_s: float = 5.0) -> bool:
        with self._lock:
            if not (self._reachable and self._enabled):
                return False
            return self._active or (time.time() - self._last_end_ts) < tail_s

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                "reachable": self._reachable,
                "enabled": self._enabled,
                "detection_type": self._detection_type,
                "active": self._active,
                "since": self._since if self._active else 0.0,
                "last_rise_ts": self._last_rise_ts,
                "last_end_ts": self._last_end_ts,
                "rises_1h": sum(1 for t in self._rises if now - t < 3600),
            }

    def stop(self) -> None:
        self._stop.set()
