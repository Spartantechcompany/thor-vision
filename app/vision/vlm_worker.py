import logging
import random
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

from app.capture.frame_buffer     import FrameBuffer
from app.vision.detection_store   import DetectionStore
from app.vision.vlm_analyzer import VLMAnalyzer

logger = logging.getLogger(__name__)

# Set de "alerts" sintéticos del propio analyzer cuando algo falla
# — no son alertas reales, no deben disparar snapshots
_FAKE_ALERTS = {"error", "empty response", "bad json", "encode failed",
                "unreachable", ""}


class VLMWorker(threading.Thread):
    """
    Worker por cámara con disparo por movimiento.

    Estrategia para no quemar la GPU del DGX Spark:
    - Cada `check_interval_s` segundos, compara el frame actual contra el
      anterior (diferencia absoluta en escala de grises, sub-muestreada).
    - Solo llama a VLM si:
        a) Hay movimiento significativo, Y han pasado >= min_interval_s
        b) O han pasado >= max_interval_s desde el último análisis (heartbeat)
    - Resultado: escenas estáticas casi no consumen GPU, escenas con
      actividad se actualizan ágilmente.

    Momentos clave como clip de video, no frame único:
    - Cuando el disparo es por movimiento real (no heartbeat), se manda
      el clip reciente del buffer (`video_window_s` segundos) como
      `video_url` en vez de un solo frame — el modelo ve la secuencia
      completa (quién entra, sale, hacia dónde se mueve), no un instante.
    - El heartbeat (sin movimiento, solo por tiempo transcurrido) sigue
      usando un frame único: no tiene sentido gastar tokens/latencia de
      video en confirmar que una escena estática sigue estática.
    """

    def __init__(
        self,
        cam_id: str,
        buffer: FrameBuffer,
        store: DetectionStore,
        analyzer: VLMAnalyzer,
        min_interval_s:      float = 30.0,
        max_interval_s:      float = 120.0,
        motion_threshold:    float = 0.04,
        check_interval_s:    float = 2.0,
        db                         = None,        # EventDB | None
        snapshots                  = None,        # SnapshotManager | None
        snapshot_periodic_s: float = 600.0,
        # Default alineado a la capacidad real del FrameBuffer
        # (buffer_size=30 @ stream_fps=10 en cameras.yml ≈ 3s de historia).
        # Pedir más de lo que el buffer puede dar no falla — get_recent_frames
        # simplemente devuelve lo que haya — pero mantenerlo realista evita
        # logs confusos sobre "ventana pedida" vs "ventana real obtenida".
        video_window_s:      float = 3.0,
        # Solo para pipelines secundarios sobre la misma cámara (ej. detalle
        # de pantallas en cam-cowork): permite escribir bajo un cam_id
        # sintético (sin pisar el resultado del pipeline general en
        # DetectionStore/events) mientras se sigue leyendo el contexto de
        # personas/caras del cam_id real. Default None = usa self.cam_id,
        # cero cambio de comportamiento para los workers existentes.
        context_cam_id: Optional[str] = None,
        event_type: str = "nemotron",
        # Movimiento nativo de la camara (CameraMotionMonitor). Si esta
        # disponible y el detector esta encendido en la camara, Qwen se
        # dispara por ese evento y el heartbeat pasa a idle_heartbeat_s;
        # si no, se conserva el comportamiento anterior (frame-diff + 120s).
        motion_source=None,
        native_min_interval_s: float = 30.0,
        idle_heartbeat_s:      float = 900.0,
        scene=None,
        require_person: bool = False,
        sustain_s: float = 10.0,
    ):
        super().__init__(daemon=True, name=f"vlm-{cam_id}")
        self.cam_id              = cam_id
        self.buffer              = buffer
        self.store               = store
        self.analyzer            = analyzer
        self.min_interval_s      = min_interval_s
        self.max_interval_s      = max_interval_s
        self.motion_threshold    = motion_threshold
        self.check_interval_s    = check_interval_s
        self.db                  = db
        self.snapshots           = snapshots
        self.snapshot_periodic_s = snapshot_periodic_s
        self.video_window_s      = video_window_s
        self.context_cam_id      = context_cam_id or cam_id
        self.event_type          = event_type
        self.motion_source       = motion_source
        self.native_min_interval_s = native_min_interval_s
        self.idle_heartbeat_s    = idle_heartbeat_s
        self.scene               = scene
        self.require_person      = require_person
        self.sustain_s           = sustain_s
        self._skip_log: deque    = deque(maxlen=3000)

        self._stop             = threading.Event()
        self._prev_gray        = None
        self._last_analysis_t  = 0.0
        self._last_people      = 0
        self._last_periodic_t  = 0.0
        self._handled_rise     = 0.0
        self.last_info: dict   = {}
        self._call_log: deque  = deque(maxlen=3000)

    def skips_last_hour(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for ts in list(self._skip_log) if ts >= cutoff)

    def _person_gate(self, st: dict) -> bool:
        """Con movimiento nativo: Qwen solo si YOLO vio personas (<8s) o el
        movimiento se sostiene (vehiculo/animal/fuego, no un destello)."""
        if not self.require_person:
            return True
        det = self.store.get(self.context_cam_id)
        if det and det.yolo_persons > 0 and time.time() - det.yolo_ts < 8.0:
            return True
        return st["active"] and time.time() - st["since"] >= self.sustain_s

    def calls_last_hour(self) -> dict:
        cutoff = time.time() - 3600
        out: dict = {}
        for ts, kind in list(self._call_log):
            if ts >= cutoff:
                out[kind] = out.get(kind, 0) + 1
        return out

    # ── Motion ────────────────────────────────────────────────────────────

    def _compute_motion(self, frame: np.ndarray) -> float:
        """
        Fracción de píxeles con cambio significativo respecto al frame previo.
        Sub-muestrea a 160x90 para que esto cueste casi nada (~0.5ms).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        if self._prev_gray is None:
            self._prev_gray = gray
            return 0.0
        diff = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray
        return float((diff > 25).mean())

    # ── Run loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            "VLMWorker started — cam=%s min=%.0fs max=%.0fs thr=%.3f",
            self.cam_id, self.min_interval_s, self.max_interval_s, self.motion_threshold
        )
        # Stagger inicial para no saturar la GPU con 10 cámaras a la vez
        self._stop.wait(random.uniform(0, min(self.max_interval_s, 15)))
        self._handled_rise = time.time()

        while not self._stop.is_set():
            frame = self.buffer.get_latest()
            if frame is None:
                self._stop.wait(self.check_interval_s)
                continue

            now     = time.monotonic()
            elapsed = now - self._last_analysis_t

            should_analyze = False
            reason         = ""

            native = (self.motion_source is not None
                      and self.motion_source.usable())

            if native:
                # Movimiento nativo de la camara: Qwen solo entra cuando la
                # camara detecta movimiento (o en el heartbeat largo idle).
                if elapsed >= self.idle_heartbeat_s:
                    should_analyze = True
                    reason         = "idle"
                elif elapsed >= self.native_min_interval_s:
                    st = self.motion_source.snapshot()
                    if st["active"] or st["last_rise_ts"] > self._handled_rise:
                        if self._person_gate(st):
                            should_analyze = True
                            reason         = "cam-motion"
                        elif not st["active"]:
                            # movimiento breve y sin personas: descartado
                            self._handled_rise = time.time()
                            self._skip_log.append(time.time())
            # Heartbeat: siempre analizar si pasó demasiado tiempo
            elif elapsed >= self.max_interval_s:
                should_analyze = True
                reason         = "heartbeat"
            # Motion-triggered: solo si pasó el mínimo intervalo
            elif elapsed >= self.min_interval_s:
                motion = self._compute_motion(frame)
                if motion >= self.motion_threshold:
                    should_analyze = True
                    reason         = f"motion={motion:.3f}"
            else:
                # En cooldown — actualizar baseline igualmente para que
                # cuando salgamos del cooldown, motion se mida bien.
                self._compute_motion(frame)

            if should_analyze:
                detection = self.store.get(self.context_cam_id)
                context = None
                if detection and time.time() - detection.yolo_ts < 10.0:
                    context = {
                        "person_count": detection.yolo_persons,
                        "person_bboxes": [tuple(b) for b in (detection.person_bboxes or [])[:4]],
                        "frame_wh": (int(frame.shape[1]), int(frame.shape[0])),
                        "faces": [
                            {"name": f.name, "confidence": round(f.confidence, 2)}
                            for f in detection.faces
                        ],
                    }

                is_motion_trigger = reason.startswith("motion=") or reason == "cam-motion"
                self._handled_rise = time.time()
                media_kind = "frame"
                if is_motion_trigger:
                    result, media_kind = self._analyze_as_video(frame, context)
                else:
                    result = self.analyzer.analyze(frame, context, tag=self.cam_id,
                                                   scene=self.scene)

                self.store.update_nemotron(self.cam_id, result)
                self._last_analysis_t = now

                people    = result.get("people") if isinstance(result.get("people"), int) else 0
                has_alert = self._has_real_alert(result)

                # ── Persistir evento ─────────────────────────────────────
                event_id: Optional[int] = None
                if self.db is not None and result.get("activity") != "error":
                    try:
                        # Adjuntar el trigger del análisis (heartbeat/motion=...)
                        # y qué se mandó de verdad al modelo (video real vs
                        # frame único de fallback) — sin esto solo quedaba en
                        # el log de texto, que rota (max-file=3 en compose).
                        payload = dict(result)
                        payload["_trigger"]    = reason
                        payload["_media_kind"] = media_kind
                        event_id = self.db.insert_event(
                            type      = self.event_type,
                            cam_id    = self.cam_id,
                            payload   = payload,
                            people    = max(people, 0),
                            has_alert = has_alert,
                        )
                    except Exception as e:
                        logger.warning("DB insert error cam=%s: %s", self.cam_id, e)

                # ── Decidir snapshot ─────────────────────────────────────
                if self.snapshots is not None and result.get("activity") != "error":
                    trigger = self._snapshot_trigger(people, has_alert, now)
                    if trigger:
                        try:
                            self.snapshots.save(self.cam_id, frame, trigger, event_id)
                            if trigger == "periodic":
                                self._last_periodic_t = now
                        except Exception as e:
                            logger.warning("Snapshot save error cam=%s: %s",
                                           self.cam_id, e)

                self._last_people = max(people, 0)

                kind = reason.split("=")[0]
                self.last_info = {
                    "ts": time.time(), "trigger": kind, "media": media_kind,
                    "ms": result.get("_ms"), "people": result.get("people"),
                }
                self._call_log.append((time.time(), kind))

                logger.info(
                    "VLM cam=%s [%s] via=%s people=%s activity=%s %.0fms",
                    self.cam_id, reason, media_kind,
                    result.get("people"),
                    str(result.get("activity", ""))[:40],
                    result.get("_ms", 0),
                )

            self._stop.wait(self.check_interval_s)

    # ── Video analysis ───────────────────────────────────────────────────

    def _analyze_as_video(self, current_frame: np.ndarray,
                           context: Optional[dict]) -> tuple[dict, str]:
        """
        Toma la ventana reciente del buffer (`video_window_s`) y la manda
        como clip de video. Si el buffer trae muy pocos frames (arranque
        en frío, cámara recién reconectada), cae a analizar `current_frame`
        solo — nunca bloquea el análisis esperando a que el buffer se llene.

        Devuelve (result, media_kind) — media_kind es "video" o "frame"
        (fallback), para que el log de arriba sea preciso sobre qué se
        mandó de verdad, no solo qué se intentó.
        """
        entries = self.buffer.get_recent_frames(self.video_window_s)
        if len(entries) < 2:
            return self.analyzer.analyze(current_frame, context, tag=self.cam_id,
                                         scene=self.scene), "frame"

        frames = [e.frame for e in entries]
        fps = self.buffer.fps or (len(frames) / self.video_window_s)
        return self.analyzer.analyze_video(frames, fps, context, tag=self.cam_id,
                                           scene=self.scene,
                                           crop_frame=current_frame), "video"

    # ── Snapshot trigger logic ────────────────────────────────────────────

    @staticmethod
    def _has_real_alert(result: dict) -> bool:
        for a in (result.get("alerts") or []):
            if str(a).strip() and str(a).strip() not in _FAKE_ALERTS:
                return True
        return False

    def _snapshot_trigger(self, people: int, has_alert: bool,
                          now: float) -> Optional[str]:
        """
        Decide qué tipo de snapshot guardar (o None).
        Prioridad: alert > people_change > periodic.
        """
        if has_alert:
            return "alert"

        prev = self._last_people
        # Personas aparecieron donde no había, o desaparecieron
        if (prev == 0 and people > 0) or (prev > 0 and people == 0):
            return "people_change"
        # Cambio relativo > 50%
        if prev > 0 and abs(people - prev) / prev > 0.5:
            return "people_change"

        if now - self._last_periodic_t >= self.snapshot_periodic_s:
            return "periodic"

        return None

    def stop(self) -> None:
        self._stop.set()
