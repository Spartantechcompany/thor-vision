import base64
import logging
import os
import time
from typing import Optional

import cv2
import numpy as np

from app.vision.detection_store import CameraDetection, FaceDetection
from app.vision.face_db import FaceDB

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


class VisionModels:
    def __init__(self, device: str = "cpu", yolo_conf: float = 0.25):
        self.device = device
        self.yolo_conf = yolo_conf
        self._yolo = None
        self._face_app = None

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
        except Exception as exc:
            logger.warning("InsightFace load failed: %s", exc)

    def process(self, frame: np.ndarray, cam_id: str, face_db: FaceDB) -> CameraDetection:
        t0 = time.monotonic()
        person_bboxes = self._detect_persons(frame)
        faces = self._detect_faces(frame, face_db, person_bboxes)
        return CameraDetection(
            cam_id=cam_id,
            person_count=len(person_bboxes),
            person_bboxes=person_bboxes,
            faces=faces,
            updated_at=time.time(),
            inference_ms=(time.monotonic() - t0) * 1000,
            frame_w=int(frame.shape[1]),
            frame_h=int(frame.shape[0]),
        )

    def _detect_persons(self, frame: np.ndarray) -> list:
        if self._yolo is None:
            return []
        try:
            results = self._yolo(
                frame,
                classes=[0],          # class 0 = person
                conf=self.yolo_conf,
                imgsz=640,
                verbose=False,
                device=self.device,
            )
            bboxes = []
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    bboxes.append((x1, y1, x2, y2))
            return bboxes
        except Exception as exc:
            logger.debug("YOLO error: %s", exc)
            return []

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
                       person_bboxes: Optional[list] = None) -> list:
        if self._face_app is None:
            return []
        try:
            from app.vision.visits import face_yaw
            detected = self._face_app.get(frame)
            faces = []
            for face in detected:
                score = float(face.det_score) if face.det_score is not None else 1.0
                if score < self.MIN_DET_SCORE:
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
            if margin:
                side = max(x2 - x1, y2 - y1) * (1 + 2 * margin)
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                x1, x2 = int(cx - side / 2), int(cx + side / 2)
                y1, y2 = int(cy - side / 2), int(cy + side / 2)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                return None
            crop = cv2.resize(frame[y1:y2, x1:x2], (size, size),
                              interpolation=cv2.INTER_AREA if size <= 72 else cv2.INTER_CUBIC)
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
