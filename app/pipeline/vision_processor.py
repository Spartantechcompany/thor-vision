import base64
import logging
import os
import time
from typing import Optional

import cv2
import numpy as np

from app.vision.detection_store import CameraDetection, FaceDetection
from app.vision.face_db import FaceDB
from app.vision import zones

logger = logging.getLogger(__name__)

_INFER_THREADS = int(os.environ.get("INFER_THREADS", "4"))


def _apply_thread_limits() -> None:
    try:
        import torch
        torch.set_num_threads(_INFER_THREADS)
        logger.info("torch threads → %d", _INFER_THREADS)
    except ImportError:
        pass
    try:
        cv2.setNumThreads(_INFER_THREADS)
    except Exception:
        pass


def _face_sharpness(crop) -> float:
    from app.vision.visits import sharpness, face_yaw
    return sharpness(crop, 64) if crop is not None and crop.size else 0.0


# Clases COCO que importan en THOR (el resto se ignora). Misma pasada que las personas: costo marginal.
_OBJECT_NAMES = {1: "bicicleta", 2: "auto", 3: "moto", 5: "autobus", 7: "camion", 15: "gato", 16: "perro",
                 24: "mochila", 26: "bolso", 28: "maleta"}
_OBJECTS_ENABLED = os.environ.get("YOLO_OBJECTS_ENABLED", "true").lower() == "true"
_OBJECT_MIN_CONF = float(os.environ.get("YOLO_OBJECT_CONF", "0.35"))

# Validacion de personas con esqueleto (YOLO26-pose): una persona real tiene puntos del cuerpo visibles; un sofa, una bolsa o un reflejo no.
_POSE_CAMS = {c.strip() for c in os.environ.get("POSE_CAMS", "cam-sala-juntas,cam-215,cam-120,cam-113,cam-118,cam-228,cam-236,cam-cowork").split(",") if c.strip()}
_POSE_DROP = {c.strip() for c in os.environ.get("POSE_DROP_CAMS", "cam-sala-juntas,cam-215").split(",") if c.strip()}   # en el resto solo se mide
_VTO_DET_SIZE = int(os.environ.get("VTO_FACE_DET_SIZE", "960"))
_VTO_DET_THRESH = float(os.environ.get("VTO_FACE_DET_THRESH", "0.45"))
_HIRES_IMGSZ = int(os.environ.get("YOLO_HIRES_IMGSZ", "1280"))     # camaras exteriores de 5 MP: la GPU de Thor esta casi libre
_POSE_MIN_KP = int(os.environ.get("POSE_MIN_KP", "6"))
_POSE_PATH = os.environ.get("POSE_MODEL", "/app/data/models/yolo26n-pose.pt")
_POSE_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n-pose.pt"


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / float((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


class VisionModels:
    def __init__(self, device: str = "cpu", yolo_conf: float = 0.25):
        self.device = device
        self.yolo_conf = yolo_conf
        self._yolo = None
        self.faces_only: set = set()      # camaras en modo solo-rostros (videoportero)
        self.hires_cams: set = set()      # camaras con mucha resolucion (5 MP): YOLO a mayor tamano para ver personas lejanas
        self._face_app = None
        self._face_app_vto = None         # detector mas sensible para el videoportero (camara solo-rostros)
        self._pose = None
        self._pose_failed = False
        self.pose_stats = {"checked": 0, "confirmed": 0, "rejected": 0, "dropped": 0, "by_cam": {}}

    def _pose_model(self):
        if self._pose is not None or self._pose_failed:
            return self._pose
        try:
            from ultralytics import YOLO
            if not os.path.exists(_POSE_PATH):
                import urllib.request
                os.makedirs(os.path.dirname(_POSE_PATH), exist_ok=True)
                urllib.request.urlretrieve(_POSE_URL, _POSE_PATH)
            self._pose = YOLO(_POSE_PATH)
            self._pose(np.zeros((64, 64, 3), dtype=np.uint8), verbose=False, imgsz=640, device=self.device)
            logger.info("YOLO26-pose cargado (%s) para validar personas en %s; descarta en %s", _POSE_PATH, sorted(_POSE_CAMS), sorted(_POSE_DROP))
        except (OSError, ImportError, RuntimeError, ValueError) as exc:
            logger.warning("YOLO26-pose no disponible (sin validacion por esqueleto): %s", exc)
            self._pose_failed = True
            self._pose = None
        return self._pose

    def _validate_persons(self, frame: np.ndarray, cam_id: str, persons: list) -> list:
        """Marca cada persona de YOLO con la cantidad de puntos del cuerpo visibles; en las camaras de _POSE_DROP descarta las que no tienen esqueleto."""
        model = self._pose_model()
        if model is None or not persons:
            return persons
        try:
            res = model(frame, imgsz=640, conf=0.25, verbose=False, device=self.device)
        except (RuntimeError, ValueError) as exc:
            logger.debug("pose error: %s", exc)
            return persons
        dets = []
        for r in res:
            if r.keypoints is None or r.boxes is None:
                continue
            kc = r.keypoints.conf.cpu().numpy() if r.keypoints.conf is not None else None
            for i, box in enumerate(r.boxes):
                dets.append((tuple(box.xyxy[0].tolist()), int((kc[i] > 0.4).sum()) if kc is not None else 0))
        keep = []
        st = self.pose_stats
        cs = st["by_cam"].setdefault(cam_id, {"checked": 0, "rejected": 0, "dropped": 0})
        for p in persons:
            best = max(((_iou(p, d[0]), d[1]) for d in dets), default=(0.0, 0), key=lambda t: t[0])
            ok = best[0] >= 0.25 and best[1] >= _POSE_MIN_KP
            st["checked"] += 1
            cs["checked"] += 1
            if ok:
                st["confirmed"] += 1
            else:
                st["rejected"] += 1
                cs["rejected"] += 1
            if ok or cam_id not in _POSE_DROP:
                keep.append(p)
            else:
                st["dropped"] += 1
                cs["dropped"] += 1
        return keep

    def setup(self) -> None:
        _apply_thread_limits()
        self._load_yolo()
        self._load_insightface()

    def _load_yolo(self) -> None:
        try:
            from ultralytics import YOLO
            self._yolo = YOLO("yolov8s.pt")   # yolov8s: +15% precisión vs nano
            dummy = np.zeros((64, 64, 3), dtype=np.uint8)
            self._yolo(dummy, verbose=False, imgsz=640)
            logger.info("YOLOv8s loaded (device=%s, imgsz=640, conf=%.2f, threads=%d)",
                        self.device, self.yolo_conf, _INFER_THREADS)
        except Exception as exc:
            logger.error("YOLO load failed: %s", exc)

    def _load_insightface(self) -> None:
        try:
            from insightface.app import FaceAnalysis
            self._face_app = FaceAnalysis(
                name="buffalo_sc",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
            # det_thresh sube de 0.5 (default) a 0.6 — confirmado en vivo que
            # con el default se aceptaban "caras" que eran ruido/reflejos en
            # camaras exteriores de noche (ej. cam-189), sin ningun filtro
            # de confianza de deteccion (distinto del umbral de RECONOCIMIENTO
            # de FaceDB, que solo decide a QUIEN se parece, no SI es una cara).
            self._face_app.prepare(ctx_id=0, det_size=(640, 640), det_thresh=0.6)
            logger.info("InsightFace buffalo_sc loaded (det_size=640, det_thresh=0.6)")
            try:      # videoportero: el cuadro ya viene centrado en la cara, el umbral de 0.6 descartaba ~40% (medido: 54% -> 88% con 960/0.45)
                self._face_app_vto = FaceAnalysis(name="buffalo_sc", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
                self._face_app_vto.prepare(ctx_id=0, det_size=(_VTO_DET_SIZE, _VTO_DET_SIZE), det_thresh=_VTO_DET_THRESH)
                logger.info("InsightFace videoportero: det_size=%d det_thresh=%.2f", _VTO_DET_SIZE, _VTO_DET_THRESH)
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("detector del videoportero no disponible, se usa el general: %s", exc)
                self._face_app_vto = None
        except Exception as exc:
            logger.warning("InsightFace load failed: %s", exc)

    def process(self, frame: np.ndarray, cam_id: str, face_db: FaceDB) -> CameraDetection:
        t0 = time.monotonic()
        if cam_id in self.faces_only:
            return self._process_faces_only(frame, cam_id, face_db, t0)
        person_bboxes, objects = self._detect(frame, _HIRES_IMGSZ if cam_id in self.hires_cams else 640)
        person_bboxes, objects = zones.filter_detections(cam_id, person_bboxes, objects, int(frame.shape[1]), int(frame.shape[0]))
        if cam_id in _POSE_CAMS and person_bboxes:
            person_bboxes = self._validate_persons(frame, cam_id, person_bboxes)
        faces = self._detect_faces(frame, face_db, person_bboxes)
        return CameraDetection(
            cam_id=cam_id,
            person_count=len(person_bboxes),
            person_bboxes=person_bboxes,
            objects=objects,
            faces=faces,
            updated_at=time.time(),
            inference_ms=(time.monotonic() - t0) * 1000,
            frame_w=int(frame.shape[1]),
            frame_h=int(frame.shape[0]),
        )

    def _process_faces_only(self, frame: np.ndarray, cam_id: str, face_db: FaceDB, t0: float) -> CameraDetection:
        """Videoportero: solo rostros, sin YOLO. La cara ampliada hace de 'persona' para el seguimiento de visitas."""
        faces = self._detect_faces(frame, face_db, None, sensitive=True)
        h, w = frame.shape[:2]
        boxes = []
        for f in faces:
            x1, y1, x2, y2 = f.bbox
            cx, cy, bw, bh = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) * 2.2, (y2 - y1) * 3.0
            boxes.append((max(0, int(cx - bw / 2)), max(0, int(cy - bh / 4)), min(w, int(cx + bw / 2)), min(h, int(cy + bh * 3 / 4))))
        return CameraDetection(cam_id=cam_id, person_count=len(boxes), person_bboxes=boxes, objects=[], faces=faces,
                               updated_at=time.time(), inference_ms=(time.monotonic() - t0) * 1000, frame_w=int(w), frame_h=int(h))

    def _detect(self, frame: np.ndarray, imgsz: int = 640) -> tuple:
        """(personas [(x1,y1,x2,y2)], objetos [{c, conf, b}]) en una sola pasada."""
        if self._yolo is None:
            return [], []
        try:
            classes = [0] + (list(_OBJECT_NAMES) if _OBJECTS_ENABLED else [])
            results = self._yolo(
                frame,
                classes=classes,
                conf=self.yolo_conf,
                imgsz=imgsz,
                verbose=False,
                device=self.device,
            )
            persons, objects = [], []
            for r in results:
                for box in r.boxes:
                    cls = int(box.cls[0])
                    bb = tuple(map(int, box.xyxy[0].tolist()))
                    if cls == 0:
                        persons.append(bb)
                    elif cls in _OBJECT_NAMES:
                        conf = float(box.conf[0])
                        if conf >= _OBJECT_MIN_CONF:
                            objects.append({"c": _OBJECT_NAMES[cls], "conf": round(conf, 2), "b": bb})
            return persons, objects
        except Exception as exc:
            logger.debug("YOLO error: %s", exc)
            return [], []

    # Defensa en profundidad ademas del det_thresh de prepare(): si algo
    # cambia esa config mas adelante, esto sigue filtrando falsos positivos
    # de baja confianza (ruido/reflejos detectados como "cara").
    MIN_DET_SCORE = 0.6

    # Margen para tolerar desalineacion entre el bbox de YOLO (cuerpo
    # completo) y el de InsightFace (solo cara) — persona agachada, brazo
    # fuera de cuadro, etc. No exige overlap exacto, solo que el centro de
    # la cara caiga dentro de un bbox de persona "inflado" un 25% por lado.
    _PERSON_BBOX_MARGIN = 0.25

    @classmethod
    def _face_has_person(cls, face_bbox: tuple, person_bboxes: list) -> bool:
        fx = (face_bbox[0] + face_bbox[2]) / 2
        fy = (face_bbox[1] + face_bbox[3]) / 2
        for (x1, y1, x2, y2) in person_bboxes:
            mx = (x2 - x1) * cls._PERSON_BBOX_MARGIN
            my = (y2 - y1) * cls._PERSON_BBOX_MARGIN
            if (x1 - mx) <= fx <= (x2 + mx) and (y1 - my) <= fy <= (y2 + my):
                return True
        return False

    def _detect_faces(self, frame: np.ndarray, face_db: FaceDB,
                       person_bboxes: Optional[list] = None, sensitive: bool = False) -> list:
        app = (self._face_app_vto if sensitive else None) or self._face_app
        if app is None:
            return []
        min_score = _VTO_DET_THRESH if (sensitive and app is self._face_app_vto) else self.MIN_DET_SCORE
        try:
            from app.vision.visits import face_yaw
            detected = app.get(frame)
            faces = []
            for face in detected:
                score = float(face.det_score) if face.det_score is not None else 1.0
                if score < min_score:
                    logger.debug("Cara descartada por baja confianza de deteccion: %.3f", score)
                    continue
                bbox = tuple(map(int, face.bbox.tolist()))
                # Si se pasan bboxes de personas (pipeline en vivo, no
                # enrolamiento), exigir que la cara este sobre una persona
                # que YOLO ya confirmo — descarta cuadros/reflejos/texturas
                # que InsightFace ve como cara pero donde no hay un humano.
                if person_bboxes is not None and not self._face_has_person(bbox, person_bboxes):
                    logger.debug("Cara descartada: sin persona YOLO cerca (bbox=%s)", bbox)
                    continue
                name, conf = face_db.recognize(face.embedding)
                thumb = self._crop_thumb(frame, bbox, size=128, margin=0.35)
                thumb_b64 = base64.b64encode(thumb).decode() if thumb else None
                fx1, fy1 = max(0, bbox[0]), max(0, bbox[1])
                fcrop = frame[fy1:max(fy1 + 1, bbox[3]), fx1:max(fx1 + 1, bbox[2])]
                faces.append(FaceDetection(
                    bbox=bbox, name=name, confidence=conf, thumb_b64=thumb_b64,
                    embedding=face.embedding, det_score=score,
                    sharpness=_face_sharpness(fcrop),
                    area=float(max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])),
                    yaw=face_yaw(getattr(face, 'kps', None))))
            return faces
        except Exception as exc:
            logger.debug("InsightFace error: %s", exc)
            return []

    @staticmethod
    def _crop_thumb(frame: np.ndarray, bbox: tuple, size: int = 72,
                    margin: float = 0.0) -> Optional[bytes]:
        """Recorta el bbox de la cara y lo codifica como JPEG chico (avatar).
        No falla nunca hacia arriba — un thumb roto no debe tumbar la detección."""
        try:
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox
            side = max(x2 - x1, y2 - y1) * (1 + 2 * margin)      # siempre cuadrado: nunca se estira la cara
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            x1, x2 = int(cx - side / 2), int(cx + side / 2)
            y1, y2 = int(cy - side / 2), int(cy + side / 2)
            if x2 <= x1 or y2 <= y1 or x2 <= 0 or y2 <= 0 or x1 >= w or y1 >= h:
                return None
            pl, pt, pr, pb = max(0, -x1), max(0, -y1), max(0, x2 - w), max(0, y2 - h)       # borde fuera del cuadro: se rellena en vez de deformar
            crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            if pl or pt or pr or pb:
                crop = cv2.copyMakeBorder(crop, pt, pb, pl, pr, cv2.BORDER_REPLICATE)
            crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA if size <= 72 else cv2.INTER_CUBIC)
            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80 if margin else 70])
            return buf.tobytes() if ok else None
        except Exception as exc:
            logger.debug("Thumb crop error: %s", exc)
            return None

    def extract_embedding(self, frame: np.ndarray) -> Optional[np.ndarray]:
        if self._face_app is None:
            return None
        try:
            detected = self._face_app.get(frame)
            if not detected:
                return None
            best = max(detected, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
            return best.embedding
        except Exception as exc:
            logger.debug("Embedding error: %s", exc)
            return None

    def extract_embedding_and_thumb(
        self, frame: np.ndarray
    ) -> tuple[Optional[np.ndarray], Optional[bytes]]:
        """Igual que extract_embedding(), pero además devuelve el recorte JPEG
        de la cara más grande (para guardarlo como miniatura de enrolamiento)."""
        if self._face_app is None:
            return None, None
        try:
            detected = self._face_app.get(frame)
            if not detected:
                return None, None
            best = max(detected, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
            bbox = tuple(map(int, best.bbox.tolist()))
            thumb = self._crop_thumb(frame, bbox)
            return best.embedding, thumb
        except Exception as exc:
            logger.debug("Embedding+thumb error: %s", exc)
            return None, None

    def teardown(self) -> None:
        self._yolo = None
        self._face_app = None
