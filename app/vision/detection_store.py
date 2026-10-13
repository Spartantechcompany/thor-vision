import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# VLM results older than this are considered stale for count merging
_NEMOTRON_STALE_S = 45.0


@dataclass
class FaceDetection:
    bbox: tuple        # (x1, y1, x2, y2)
    name: str
    confidence: float
    thumb_b64: Optional[str] = None   # crop JPEG chico (base64), None si el crop falló
    embedding: object = field(default=None, repr=False)   # InsightFace 512-d (solo uso interno)
    det_score: float = 1.0
    sharpness: float = 0.0
    area: float = 0.0
    yaw: float = 0.0


def _count_objects(objects) -> dict:
    out: dict = {}
    for o in objects or []:
        out[o["c"]] = out.get(o["c"], 0) + 1
    return out


@dataclass
class CameraDetection:
    cam_id: str
    person_count: int = 0
    person_bboxes: list = field(default_factory=list)
    objects: list = field(default_factory=list)   # [{c, conf, b}] autos, perros, bolsas...
    faces: list = field(default_factory=list)
    updated_at: float = 0.0
    inference_ms: float = 0.0
    nemotron: Optional[dict] = None
    yolo_persons: int = 0     # conteo YOLO puro (sin mezclar el del VLM)
    yolo_ts: float = 0.0
    frame_w: int = 0          # tamano del fotograma sobre el que YOLO dio person_bboxes
    frame_h: int = 0


class DetectionStore:
    """Thread-safe store para los últimos resultados de análisis por cámara."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, CameraDetection] = {}

    def update(self, result: CameraDetection) -> None:
        """
        YOLO update: preserva nemotron y toma el mayor conteo entre
        YOLO y VLM (VLM es más semántico, YOLO da bboxes).
        """
        result.yolo_persons = result.person_count
        result.yolo_ts = time.time()
        with self._lock:
            existing = self._data.get(result.cam_id)
            if existing and existing.nemotron is not None:
                result.nemotron = existing.nemotron
                # Si VLM tiene un conteo reciente y mayor, lo respeta
                nem_ts = existing.nemotron.get("_ts", 0)
                nem_people = existing.nemotron.get("people", 0)
                if (isinstance(nem_people, int)
                        and nem_people > 0
                        and (time.time() - nem_ts) < _NEMOTRON_STALE_S
                        and nem_people > result.person_count):
                    result.person_count = nem_people
            self._data[result.cam_id] = result

    def update_nemotron(self, cam_id: str, result: dict) -> None:
        """
        VLM update: actualiza semántica y person_count.
        Preserva bboxes de YOLO si existen.
        """
        with self._lock:
            people = result.get("people", 0)
            if not isinstance(people, int) or people < 0:
                people = 0

            if cam_id in self._data:
                existing = self._data[cam_id]
                existing.nemotron     = result
            else:
                d = CameraDetection(
                    cam_id=cam_id,
                    person_count=people,
                    updated_at=result.get("_ts", time.time()),
                )
                d.nemotron = result
                self._data[cam_id] = d

    def get(self, cam_id: str) -> Optional[CameraDetection]:
        with self._lock:
            return self._data.get(cam_id)

    def get_all(self) -> dict[str, CameraDetection]:
        with self._lock:
            return dict(self._data)

    def total_persons(self) -> int:
        with self._lock:
            return sum(v.person_count for v in self._data.values())

    def as_api(self) -> dict:
        with self._lock:
            return {
                cam_id: {
                    "person_count": d.person_count,
                    "faces": [
                        {"name": f.name, "confidence": round(f.confidence, 3),
                         "thumb": f.thumb_b64}
                        for f in d.faces
                    ],
                    "vlm": d.nemotron,
                    "yolo_persons": d.yolo_persons,
                    "objects": _count_objects(d.objects),
                    "yolo_ts": d.yolo_ts,
                    "updated_at": d.updated_at,
                    "inference_ms": round(d.inference_ms, 1),
                }
                for cam_id, d in self._data.items()
            }
