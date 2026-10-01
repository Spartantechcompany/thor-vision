import urllib.parse
import logging
import threading
import time
from enum import Enum
from typing import Optional

import cv2
import numpy as np

from app.capture.frame_buffer import FrameBuffer
from app.config import CameraConfig, GlobalConfig

logger = logging.getLogger(__name__)


class CameraState(str, Enum):
    CONNECTING   = "connecting"
    STREAMING    = "streaming"
    RECONNECTING = "reconnecting"
    DISABLED     = "disabled"
    STOPPED      = "stopped"


class RTSPReader(threading.Thread):
    """
    Thread dedicado por cámara.
    Máquina de estados: CONNECTING → STREAMING → RECONNECTING → CONNECTING
    Reconexión con backoff exponencial: 5s → 10s → 20s → máx 60s

    Mejoras de latencia:
    - GStreamer latency=0, max-buffers=1 drop=true → sin acumulación
    - Read loop sin throttle → siempre lee el frame más reciente
    - FrameBuffer maxlen=2 → máx ~130ms de lag
    """

    def __init__(self, cam: CameraConfig, global_cfg: GlobalConfig, buffer: FrameBuffer):
        super().__init__(name=f"rtsp-{cam.id}", daemon=True)
        self.cam = cam
        self.global_cfg = global_cfg
        self.buffer = buffer

        self.state: CameraState = CameraState.CONNECTING
        self._stop_event = threading.Event()
        self._backoff: float = 5.0
        self._connect_attempts: int = 0
        self._total_frames: int = 0
        self._error_msg: str = ""
        self._mask_polys = self.cam.privacy_mask or []
        self._mask_px = None
        self._mask_size = None
        self._method = ""
        self._connected_since = 0.0
        self._native_wh = None

    # ── Public ──────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def is_alive_and_streaming(self) -> bool:
        return self.state == CameraState.STREAMING

    def stats(self) -> dict:
        return {
            "id": self.cam.id,
            "name": self.cam.name,
            "state": self.state.value,
            "fps": self.buffer.fps,
            "latency_ms": round(self.buffer.latency_ms(), 1),
            "total_frames": self._total_frames,
            "connect_attempts": self._connect_attempts,
            "error": self._error_msg,
            "zone": self.cam.zone,
            "priority": self.cam.priority,
        }

    def connection(self) -> dict:
        u = urllib.parse.urlparse(self.cam.rtsp_url)
        gst = self._method.startswith("GStreamer")
        return {
            "host": u.hostname,
            "port": u.port or 554,
            "scheme": u.scheme,
            "path": u.path,
            "transport": "TCP (interleaved)" if gst else "auto (FFmpeg)",
            "method": self._method or "-",
            "connected_since": self._connected_since if self.state == CameraState.STREAMING else 0.0,
            "native_wh": self._native_wh,
            "cfg_resolution": self.cam.resolution,
            "rotate": self.cam.rotate,
            "frames": self._total_frames,
        }

    # ── Threading ────────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("[%s] Reader started", self.cam.id)
        while not self._stop_event.is_set():
            self.state = CameraState.CONNECTING
            cap = self._open_capture()
            if cap is None:
                logger.warning("[%s] Connection failed (attempt %d), retry in %.0fs",
                               self.cam.id, self._connect_attempts, self._backoff)
                self._stop_event.wait(self._backoff)
                self._backoff = min(self._backoff * 2, 60.0)
                continue

            self._backoff = 5.0
            self._error_msg = ""
            self.state = CameraState.STREAMING
            self._connected_since = time.time()
            self._native_wh = None
            logger.info("[%s] Streaming started", self.cam.id)

            self._read_loop(cap)
            cap.release()

            if not self._stop_event.is_set():
                self.state = CameraState.RECONNECTING
                logger.warning("[%s] Stream lost, reconnecting...", self.cam.id)
                self._stop_event.wait(2.0)

        self.state = CameraState.STOPPED
        logger.info("[%s] Reader stopped", self.cam.id)

    # ── Internals ────────────────────────────────────────────────────────────

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        self._connect_attempts += 1

        # Intento 1: GStreamer H.264 — latency=0, sin buffering
        cap = self._try_gstreamer_h264()
        if cap is None and self._connect_attempts > 1:
            # la camara suele tardar unos segundos en liberar la sesion RTSP anterior
            self._stop_event.wait(4.0)
            cap = self._try_gstreamer_h264()
        if cap is not None:
            return cap

        # Intento 2: GStreamer H.265 — para cámaras con HEVC
        cap = self._try_gstreamer_h265()
        if cap is not None:
            return cap

        # Intento 3: OpenCV/FFMPEG directo — fallback universal
        cap = self._try_opencv_direct()
        if cap is not None:
            return cap

        self._error_msg = "No se pudo conectar (GST H264/H265 ni OpenCV)"
        return None

    def _capture_dims(self) -> tuple[int, int]:
        """(width, height) para las caps del pipeline: usa la resolucion
        nativa de la camara si se declaro en cameras.yml (ej. cam-113,
        nativo 1080x1920 vertical — forzar 1280x720 ahi la distorsionaba
        en diagonal), si no cae al tamano global de siempre."""
        if self.cam.resolution and len(self.cam.resolution) == 2:
            return self.cam.resolution[0], self.cam.resolution[1]
        return self.global_cfg.frame_width, self.global_cfg.frame_height

    def _try_gstreamer_h264(self) -> Optional[cv2.VideoCapture]:
        """GStreamer H.264 con NVDEC (nvv4l2decoder) — decode por hardware,
        no CPU. latency=0, resize a 1280x720 BGR, un solo frame en sink."""
        url = self.cam.rtsp_url
        w, h = self._capture_dims()
        pipeline = (
            f"rtspsrc location={url} "
            f"latency=0 protocols=tcp do-retransmission=false tcp-timeout=5000000 "
            f"! rtph264depay ! h264parse ! nvv4l2decoder "
            f"! nvvidconv ! video/x-raw,format=BGRx,width={w},height={h} "
            f"! videoconvert ! video/x-raw,format=BGR "
            f"! appsink max-buffers=1 drop=true sync=false emit-signals=false"
        )
        return self._open_gst(pipeline, "H264")

    def _try_gstreamer_h265(self) -> Optional[cv2.VideoCapture]:
        """GStreamer H.265/HEVC con NVDEC — para cámaras que usan HEVC."""
        url = self.cam.rtsp_url
        w, h = self._capture_dims()
        pipeline = (
            f"rtspsrc location={url} "
            f"latency=0 protocols=tcp do-retransmission=false tcp-timeout=5000000 "
            f"! rtph265depay ! h265parse ! nvv4l2decoder "
            f"! nvvidconv ! video/x-raw,format=BGRx,width={w},height={h} "
            f"! videoconvert ! video/x-raw,format=BGR "
            f"! appsink max-buffers=1 drop=true sync=false emit-signals=false"
        )
        return self._open_gst(pipeline, "H265")

    def _open_gst(self, pipeline: str, label: str) -> Optional[cv2.VideoCapture]:
        try:
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    # info (no debug): antes esto era invisible y las 10 camaras
                    # llevaban meses cayendo al fallback de FFmpeg sin que se notara.
                    logger.info("[%s] conectado vía GStreamer %s + NVDEC (hardware decode)",
                               self.cam.id, label)
                    self._method = f"GStreamer {label} + NVDEC (hardware)"
                    return cap
            cap.release()
        except Exception as e:
            logger.debug("[%s] GStreamer %s failed: %s", self.cam.id, label, e)
        return None

    def _try_opencv_direct(self) -> Optional[cv2.VideoCapture]:
        """OpenCV/FFMPEG directo — buffersize=1 para mínima latencia."""
        url = self.cam.rtsp_url
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    logger.info("[%s] conectado vía OpenCV/FFmpeg fallback (sin GStreamer)",
                               self.cam.id)
                    self._method = "OpenCV/FFmpeg (software, fallback)"
                    return cap
            cap.release()
        except Exception as e:
            self._error_msg = str(e)
            logger.debug("[%s] OpenCV failed: %s", self.cam.id, e)
        return None

    _ROTATE_FLAGS = {
        90:  cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }

    def _apply_privacy_mask(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        if self._mask_px is None or self._mask_size != (w, h):
            self._mask_px = [
                np.array([[int(x * w), int(y * h)] for x, y in poly], dtype=np.int32)
                for poly in self._mask_polys
            ]
            self._mask_size = (w, h)
        cv2.fillPoly(frame, self._mask_px, (0, 0, 0))

    def _read_loop(self, cap: cv2.VideoCapture) -> None:
        """
        Lee frames a máxima velocidad sin throttle.
        FrameBuffer(maxlen=2) descarta frames antiguos → latencia mínima.
        """
        consecutive_errors = 0
        rotate_flag = self._ROTATE_FLAGS.get(self.cam.rotate)

        while not self._stop_event.is_set():
            if (self._method.startswith("OpenCV") and self._connected_since
                    and time.time() - self._connected_since > 300):
                logger.info("[%s] en fallback FFmpeg >5 min, reintentando GStreamer+NVDEC", self.cam.id)
                break
            ret, frame = cap.read()

            if not ret or frame is None or frame.size == 0:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    logger.warning("[%s] %d errores consecutivos, reconectando",
                                   self.cam.id, consecutive_errors)
                    break
                time.sleep(0.05)
                continue

            if self._native_wh is None:
                self._native_wh = [int(frame.shape[1]), int(frame.shape[0])]

            if rotate_flag is not None:
                frame = cv2.rotate(frame, rotate_flag)
                # Tras rotar 90/270, dimensiones quedan invertidas — reescalar
                # al tamano global para que el resto del pipeline (YOLO,
                # snapshots, JPEG del dashboard) vea un tamano consistente.
                h, w = frame.shape[:2]
                gw, gh = self.global_cfg.frame_width, self.global_cfg.frame_height
                if (w, h) != (gw, gh):
                    frame = cv2.resize(frame, (gw, gh), interpolation=cv2.INTER_LINEAR)

            consecutive_errors = 0
            self._total_frames += 1

            if self._mask_polys:
                self._apply_privacy_mask(frame)
            self.buffer.put(frame)
