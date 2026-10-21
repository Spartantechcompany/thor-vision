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
from app.vision import vlm_analyzer
from app.vision.vlm_analyzer import VLMAnalyzer

logger = logging.getLogger(__name__)

# Set de "alerts" sintéticos del propio analyzer cuando algo falla
# — no son alertas reales, no deben disparar snapshots
_FAKE_ALERTS = {"error", "empty response", "bad json", "encode failed",
                "unreachable", ""}


import os

SCENE_FRAMES = int(os.environ.get("VLM_SCENE_FRAMES", "8"))          # cuadros del clip de escena (el servidor junta pares: 8 cuadros = 4 grupos)
SCENE_WINDOW_S = float(os.environ.get("VLM_SCENE_WINDOW_S", "16"))   # segundos de escena que cubre el clip (antes ~1 s: el buffer de 30 cuadros a 10-30 fps)
SCENE_STEP_S = float(os.environ.get("VLM_SCENE_STEP_S", "2"))        # un cuadro del anillo de escena cada tanto
SCENE_W, SCENE_H = int(os.environ.get("VLM_SCENE_WIDTH", "480")), int(os.environ.get("VLM_SCENE_HEIGHT", "270"))   # 8 cuadros a 480x270 = ~530 tokens
RISE_DELAY_S = float(os.environ.get("VLM_RISE_DELAY_S", "4"))        # al empezar el movimiento se espera para que la accion entre en el clip
SAME_STALE_S = float(os.environ.get("VLM_SAME_STALE_S", "300"))      # una escena que no cambia se vuelve a mirar a lo mucho cada tanto
SAME_PIX = float(os.environ.get("VLM_SAME_PIX", "0.06"))             # fraccion de pixeles (64x36) que cambiaron: por debajo es la misma escena
SAME_MOVE = float(os.environ.get("VLM_SAME_MOVE", "0.08"))           # cuanto se puede mover (fraccion del cuadro) una persona y seguir siendo "la misma escena"
SAME_GATE = os.environ.get("VLM_SAME_GATE", "true").lower() == "true"
IDLE_W, IDLE_H = int(os.environ.get("VLM_IDLE_WIDTH", "640")), int(os.environ.get("VLM_IDLE_HEIGHT", "360"))   # foto de "sin movimiento": 235 tokens contra 895 a 1280x720


def _fit(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    fh, fw = frame.shape[:2]
    k = min(w / fw, h / fh, 1.0)
    return cv2.resize(frame, (max(2, int(fw * k) // 2 * 2), max(2, int(fh * k) // 2 * 2)), interpolation=cv2.INTER_AREA) if k < 1.0 else frame


def _summarize_objects(objects) -> dict:
    out: dict = {}
    for o in objects or []:
        out[o["c"]] = out.get(o["c"], 0) + 1
    return out


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
        self._ring: deque      = deque(maxlen=int(SCENE_WINDOW_S / max(0.5, SCENE_STEP_S)) + 2)   # (ts, cuadro chico) para el clip de escena
        self._ring_t           = 0.0
        self._last_sig         = None     # estado de la escena del ultimo analisis (personas, objetos, miniatura)
        self.same_skips: deque = deque(maxlen=3000)
        self.busy_skips: deque = deque(maxlen=3000)

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

    # ── Escena igual / prioridad ───────────────────────────────────────────

    def _signature(self, frame: np.ndarray, det) -> dict:
        fresh = det is not None and time.time() - det.yolo_ts < 10.0
        fh, fw = frame.shape[:2]
        bbs = (det.person_bboxes or []) if fresh else []
        sx, sy = fw, fh
        if fresh and getattr(det, "frame_wh", None):
            sx, sy = det.frame_wh
        return {"t": time.time(), "people": det.yolo_persons if fresh else 0,
                "centers": sorted(((b[0] + b[2]) / 2 / max(1, sx), (b[1] + b[3]) / 2 / max(1, sy)) for b in bbs[:8]),
                "objs": _summarize_objects(det.objects) if fresh else {},
                "thumb": cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)}

    def _same_scene(self, sig: dict) -> bool:
        """Misma escena que el ultimo analisis: mismas personas en casi el mismo lugar, mismos objetos y la imagen casi igual."""
        last = self._last_sig
        if not SAME_GATE or last is None or sig["t"] - last["t"] > SAME_STALE_S:
            return False
        if sig["people"] != last["people"] or sig["objs"] != last["objs"] or len(sig["centers"]) != len(last["centers"]):
            return False
        for (x0, y0), (x1, y1) in zip(sig["centers"], last["centers"]):
            if abs(x0 - x1) > SAME_MOVE or abs(y0 - y1) > SAME_MOVE:
                return False
        return float((cv2.absdiff(sig["thumb"], last["thumb"]) > 25).mean()) < SAME_PIX

    def _high_priority(self, det) -> bool:
        """Prioritaria si una persona aqui puede ser alerta ahora: horario nocturno de alerta (o armado), videoportero, o persona en una camara de calle."""
        if self.cam_id == "cam-vto":
            return True
        sc = self.scene
        if sc is not None and sc.night_alert():
            return True
        people = det.yolo_persons if det is not None and time.time() - det.yolo_ts < 10.0 else 0
        return bool(sc is not None and sc.vehicles and people > 0)

    def _feed_ring(self, frame: np.ndarray) -> None:
        t = time.time()
        if t - self._ring_t >= SCENE_STEP_S:
            self._ring.append((t, _fit(frame, SCENE_W, SCENE_H)))
            self._ring_t = t

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

            self._feed_ring(frame)
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
                    rising = st["last_rise_ts"] > self._handled_rise
                    if rising and time.time() - st["last_rise_ts"] < RISE_DELAY_S:
                        pass                                    # recien empezo: se espera a que la accion entre en el clip
                    elif st["active"] or rising:
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
                sig = self._signature(frame, detection)
                high = self._high_priority(detection)
                if reason != "idle" and self._same_scene(sig):
                    # nada cambio desde el ultimo analisis: no se gasta otra llamada (se vuelve a mirar a los SAME_STALE_S)
                    self._handled_rise = time.time()
                    self._last_analysis_t = now
                    self.same_skips.append(time.time())
                    self.last_info = dict(self.last_info, skipped="igual", skipped_ts=time.time())
                    self._stop.wait(self.check_interval_s)
                    continue
                if not high and vlm_analyzer.gateway_busy():
                    self.busy_skips.append(time.time())        # gateway saturado: la rutina espera (no cuenta como analisis)
                    self._stop.wait(self.check_interval_s)
                    continue
                context = None
                if detection and time.time() - detection.yolo_ts < 10.0:
                    context = {
                        "person_count": detection.yolo_persons,
                        "person_bboxes": [tuple(b) for b in (detection.person_bboxes or [])[:4]],
                        "objects": _summarize_objects(detection.objects),
                        "aspects": [round((b[2] - b[0]) / max(1, b[3] - b[1]), 2) for b in (detection.person_bboxes or [])[:6]],
                        "frame_wh": (int(frame.shape[1]), int(frame.shape[0])),
                        "faces": [
                            {"name": f.name, "confidence": round(f.confidence, 2)}
                            for f in detection.faces
                        ],
                    }

                is_motion_trigger = reason.startswith("motion=") or reason == "cam-motion"
                self._handled_rise = time.time()
                media_kind = "frame"
                with vlm_analyzer.priority(high):
                    if is_motion_trigger:
                        result, media_kind = self._analyze_as_video(frame, context)
                    else:
                        result = self.analyzer.analyze(_fit(frame, IDLE_W, IDLE_H) if reason == "idle" else frame, context,
                                                       tag=self.cam_id, scene=self.scene)
                if result.get("_busy_skip"):
                    self.busy_skips.append(time.time())
                    self._stop.wait(self.check_interval_s)
                    continue
                if result.get("activity") != "error":
                    self._last_sig = sig

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
        ring = [(t, f) for t, f in self._ring if time.time() - t <= SCENE_WINDOW_S]
        if len(ring) >= 3:
            # clip de escena completa: un cuadro cada ~SCENE_STEP_S de los ultimos SCENE_WINDOW_S, mas el actual
            if time.time() - ring[-1][0] > 0.5:
                ring.append((time.time(), _fit(current_frame, SCENE_W, SCENE_H)))
            n = len(ring)
            k = min(SCENE_FRAMES, n)
            sel = [ring[round(i * (n - 1) / max(k - 1, 1))] for i in range(k)]
            span = max(0.5, sel[-1][0] - sel[0][0])
            ctx = dict(context or {})
            ctx["_clip_s"], ctx["_clip_n"] = span, len(sel)
            res = self.analyzer.analyze_video([f for _, f in sel], (len(sel) - 1) / span, ctx, tag=self.cam_id,
                                              scene=self.scene, crop_frame=current_frame)
            if context is not None and ctx.get("_has_crop"):
                context["_has_crop"] = True
            return res, "video"
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
