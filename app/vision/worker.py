import base64
import cv2
import logging
import numpy as np
import os
import queue
import threading
import time

from app.vision.detection_store import DetectionStore
from app.vision.face_db import FaceDB
from app.capture.frame_buffer import FrameBuffer

logger = logging.getLogger(__name__)

PRE_ENABLED = os.environ.get("PREROLL_ENABLED", "true").lower() == "true"
PRE_WINDOW_S = float(os.environ.get("PREROLL_SECONDS", "6"))
PRE_MAX_FRAMES = int(os.environ.get("PREROLL_BURST_MAX", "16"))
PRE_MIN_FRAMES = 3
PRE_COOLDOWN_S = float(os.environ.get("PREROLL_COOLDOWN_S", "3"))
PRE_MAX_WAIT_S = 3.0
PRE_MOTION_DELAY_S = float(os.environ.get("PREROLL_MOTION_DELAY_S", "2"))


class VisionQueue:
    """
    Single inference thread — processes one frame at a time serially.
    Prevents multiple cameras from saturating all CPU cores in parallel.
    """

    # Tope de "avistamientos" persistidos por cámara: sin esto, a 0.5fps con
    # alguien en cuadro se generaría una fila cada 2s — demasiado. Como
    # máximo 1 tanda (todas las caras de ese pase) por cámara cada 30s.
    SIGHT_COOLDOWN_S = 30.0
    BODY_COOLDOWN_S = 120.0     # personas sin rostro: 1 tanda por camara cada 2 min
    BODY_STATIC_S = 600.0       # misma caja casi identica en 10 min = objeto estatico

    def __init__(self, store: DetectionStore, face_db: FaceDB, models,
                 db=None, visits=None):
        self.visits = visits   # VisitManager | None — tracker + visitas + sujetos
        self.store = store
        self.face_db = face_db
        self.models = models
        self.db = db   # EventDB | None — historial de avistamientos (opcional)
        self._last_sight_ts: dict[str, float] = {}
        self._last_body: dict[str, tuple] = {}     # cam -> (ts, bbox)
        self._q: queue.Queue = queue.Queue(maxsize=5)  # bounded: drop if full
        self._thread = threading.Thread(target=self._run, daemon=True, name="vision-queue")
        self.preroll: dict = {}                      # cam_id -> PreRoll
        self._pre_q: queue.Queue = queue.Queue(maxsize=8)
        self._burst_upto: dict[str, float] = {}      # cam -> ts del ultimo frame ya reprocesado
        self._last_burst: dict[str, float] = {}
        if visits is not None and PRE_ENABLED:
            visits.burst_cb = self.submit_burst

    def start(self) -> None:
        self._thread.start()
        logger.info("VisionQueue started")

    def submit(self, cam_id: str, frame) -> None:
        try:
            self._q.put_nowait((cam_id, frame))
        except queue.Full:
            pass  # drop frame — better than building a backlog

    def submit_burst(self, cam_id: str, reason: str) -> bool:
        """Encola el reanalisis del pre-roll (baja prioridad). Devuelve True si se encolo."""
        pr = self.preroll.get(cam_id)
        if not PRE_ENABLED or pr is None or self.visits is None:
            return False
        now = time.time()
        if now - self._last_burst.get(cam_id, 0.0) < PRE_COOLDOWN_S:
            return False
        after = max(now - PRE_WINDOW_S, self._burst_upto.get(cam_id, 0.0))
        frames = pr.frames_since(after)
        if len(frames) < PRE_MIN_FRAMES:
            return False
        if len(frames) > PRE_MAX_FRAMES:
            idx = sorted(set(np.linspace(0, len(frames) - 1, PRE_MAX_FRAMES).round().astype(int).tolist()))
            frames = [frames[i] for i in idx]
        try:
            self._pre_q.put_nowait((now, cam_id, reason, frames))
        except queue.Full:
            return False
        self._last_burst[cam_id] = now
        self._burst_upto[cam_id] = frames[-1][0]
        return True

    def _next(self):
        pq = self._pre_q.queue
        if pq and (self._q.empty() or time.time() - pq[0][0] > PRE_MAX_WAIT_S):
            try:
                return "pre", self._pre_q.get_nowait()
            except queue.Empty:
                pass
        return "live", self._q.get(timeout=0.3)

    def _run_burst(self, item) -> None:
        _, cam_id, reason, frames = item
        t0, n = time.monotonic(), 0
        for ts, jpg in reversed(frames):      # del mas nuevo al mas viejo: encadena con el track vivo
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            result = self.models.process(frame, cam_id, self.face_db)
            self.visits.update(cam_id, result, frame, ts=ts, src="pre")
            n += 1
        self.visits.burst_end(cam_id)
        self.visits.stats["pre_bursts"] += 1
        self.visits.stats["pre_frames"] += n
        logger.info("PREROLL cam=%s motivo=%s frames=%d %.0fms", cam_id, reason, n,
                    (time.monotonic() - t0) * 1000)

    def _run(self) -> None:
        while True:
            try:
                kind, item = self._next()
                if kind == "pre":
                    self._run_burst(item)
                    continue
                cam_id, frame = item
                t0 = time.monotonic()
                result = self.models.process(frame, cam_id, self.face_db)
                self.store.update(result)
                if self.visits is not None:
                    self.visits.update(cam_id, result, frame)
                else:
                    self._log_sightings(cam_id, result, frame)
                logger.debug("inference cam=%s  %.0fms", cam_id, (time.monotonic()-t0)*1000)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.warning("VisionQueue error: %s", exc)

    @staticmethod
    def _body_jpeg(frame, bbox, max_side: int = 288):
        try:
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox
            bw, bh = x2 - x1, y2 - y1
            x1, x2 = max(0, int(x1 - bw * .12)), min(w, int(x2 + bw * .12))
            y1, y2 = max(0, int(y1 - bh * .06)), min(h, int(y2 + bh * .06))
            if x2 <= x1 or y2 <= y1:
                return None
            crop = frame[y1:y2, x1:x2]
            k = max_side / max(crop.shape[:2])
            if k < 1.0:
                crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
            return buf.tobytes() if ok else None
        except Exception:
            return None

    @staticmethod
    def _iou(a, b) -> float:
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
        return inter / ua if ua > 0 else 0.0

    def _log_sightings(self, cam_id: str, result, frame=None) -> None:
        """Persiste avistamientos: una fila por cara (con zoom de la cara y el
        cuerpo de su persona) y, aparte, filas 'Sin rostro' con solo el cuerpo
        para personas cuya cara no se vio. Cara: 1 tanda por camara cada 30 s."""
        persons = list(getattr(result, "person_bboxes", None) or [])
        if self.db is None or (not result.faces and not persons):
            return
        now = time.time()
        face_turn = bool(result.faces) and now - self._last_sight_ts.get(cam_id, 0.0) >= self.SIGHT_COOLDOWN_S
        matched: set = set()

        def person_for(fb):
            cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
            for i, pb in enumerate(persons):
                mx, my = (pb[2]-pb[0]) * .1, (pb[3]-pb[1]) * .1
                if pb[0]-mx <= cx <= pb[2]+mx and pb[1]-my <= cy <= pb[3]+my:
                    return i
            return None

        if face_turn:
            self._last_sight_ts[cam_id] = now
        for face in result.faces:
            i = person_for(face.bbox)
            if i is not None:
                matched.add(i)
            if not face_turn:
                continue
            try:
                thumb = base64.b64decode(face.thumb_b64) if face.thumb_b64 else None
                body = self._body_jpeg(frame, persons[i]) if (frame is not None and i is not None) else None
                self.db.insert_face_sighting(cam_id, face.name, face.confidence, thumb, body)
            except Exception as exc:
                logger.warning("No se pudo guardar avistamiento cam=%s: %s", cam_id, exc)

        if frame is None or not persons or len(matched) == len(persons):
            return
        last_ts, last_bb = self._last_body.get(cam_id, (0.0, None))
        if now - last_ts < self.BODY_COOLDOWN_S:
            return
        for i, pb in enumerate(persons):
            if i in matched:
                continue
            if last_bb is not None and now - last_ts < self.BODY_STATIC_S and self._iou(pb, last_bb) > 0.85:
                continue
            body = self._body_jpeg(frame, pb)
            if not body:
                continue
            try:
                self.db.insert_face_sighting(cam_id, "Sin rostro", None, None, body)
                self._last_body[cam_id] = (now, pb)
            except Exception as exc:
                logger.warning("No se pudo guardar persona cam=%s: %s", cam_id, exc)
            break


class VisionWorker(threading.Thread):
    """
    Lightweight per-camera thread — just grabs the latest frame and
    submits it to the shared VisionQueue at `fps` rate.
    """

    def __init__(
        self,
        cam_id: str,
        buffer: FrameBuffer,
        vision_queue: VisionQueue,
        fps: float = 0.5,
        motion_source=None,
        boost_fps: float = 2.0,
        hunt_source=None,
        hunt_fps: float = 4.0,
    ):
        super().__init__(daemon=True, name=f"vsrc-{cam_id}")
        self.motion_source = motion_source
        self.boost_interval = 1.0 / max(boost_fps, 0.05)
        self.hunt_source = hunt_source
        self.hunt_interval = 1.0 / max(hunt_fps, 0.05)
        self.hunting = False
        self.boosted = False
        self.cam_id = cam_id
        self.buffer = buffer
        self.vision_queue = vision_queue
        self.interval = 1.0 / max(fps, 0.05)
        self._stop = threading.Event()

    def run(self) -> None:
        logger.info("VisionWorker started — cam=%s  interval=%.1fs", self.cam_id, self.interval)
        while not self._stop.is_set():
            t0 = time.monotonic()
            frame = self.buffer.get_latest()
            if frame is not None:
                self.vision_queue.submit(self.cam_id, frame)
            elapsed = time.monotonic() - t0
            was_boosted = self.boosted
            self.boosted = bool(self.motion_source and self.motion_source.active_recently())
            if self.boosted and not was_boosted and PRE_ENABLED:
                tm = threading.Timer(PRE_MOTION_DELAY_S, self.vision_queue.submit_burst,
                                     (self.cam_id, "motion"))
                tm.daemon = True
                tm.start()
            try:
                self.hunting = bool(self.hunt_source and self.hunt_source.needs_faces(self.cam_id))
            except Exception:
                self.hunting = False
            base = self.interval
            if self.boosted:
                base = self.boost_interval
            if self.hunting:
                base = min(base, self.hunt_interval)
            sleep = base - elapsed
            if sleep > 0:
                self._stop.wait(sleep)

    def stop(self) -> None:
        self._stop.set()
