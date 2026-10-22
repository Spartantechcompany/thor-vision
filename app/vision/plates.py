"""Lectura de placas de vehiculos estacionados (solo dentro de las zonas de estacionamiento).

Se lee el recorte nativo del vehiculo (no el cuadro completo: el detector reduce a ~640 px y la placa desaparece), varias veces
mientras el vehiculo sigue ahi, y se vota entre lecturas. Lo escrito a mano siempre manda sobre lo automatico.

Dependencia: fast-alpr (detector + OCR en ONNX). Se instala fuera de la imagen, en el volumen de datos y sin arrastrar
dependencias (para no pisar el wheel de OpenCV con GStreamer):  scripts/install_alpr.sh
"""
import logging
import os
import re
import sys
import threading
from collections import defaultdict
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("PLATES_ENABLED", "true").lower() == "true"
LIBS = os.environ.get("PLATES_LIBS", "/app/data/pylibs")
CACHE = os.environ.get("PLATES_CACHE", "/app/data/alpr_models")
DETECTOR = os.environ.get("PLATES_DETECTOR", "yolo-v9-s-608-license-plate-end2end")
OCR = os.environ.get("PLATES_OCR", "cct-s-v2-global-model")
EVERY_S = float(os.environ.get("PLATES_EVERY_S", "60"))          # una lectura por minuto por vehiculo
MAX_READS = int(os.environ.get("PLATES_MAX_READS", "20"))        # y se detiene tras estas lecturas
MIN_DET = float(os.environ.get("PLATES_MIN_DET", "0.5"))
_PLATE = re.compile(r"^[A-Z0-9]{5,8}$")

_lock = threading.Lock()
_alpr = None
_failed = False


def normalize(text: Optional[str]) -> str:
    t = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return t if _PLATE.match(t) else ""


def vote(reads: list, min_conf: float = 0.7, min_single: float = 0.85, min_agree: int = 2) -> Optional[tuple]:
    """reads: [(texto, confianza)]. Devuelve (texto, confianza_media, lecturas) del ganador si es confiable, si no None.
    Confiable: 2 lecturas iguales con media >= 0.7, 3 o mas con media >= 0.6, o una sola muy segura (>= 0.85). Lo demas queda "por confirmar"
    (ver guess): con el umbral anterior (2 lecturas >= 0.5) entraron placas mal leidas (MB5939A por MKB543A, RAC451E por RAG051E)."""
    groups = defaultdict(list)
    for t, c in reads:
        if t:
            groups[t].append(float(c))
    if not groups:
        return None
    text, confs = max(groups.items(), key=lambda kv: (sum(kv[1]), len(kv[1])))
    avg = sum(confs) / len(confs)
    if (len(confs) >= min_agree and avg >= min_conf) or (len(confs) >= 3 and avg >= 0.6) or (len(confs) == 1 and avg >= min_single):
        return text, round(avg, 3), len(confs)
    return None


def guess(reads: list, min_conf: float = 0.35) -> Optional[tuple]:
    """La mejor lectura aunque no alcance para darla por buena: (texto, confianza_media, lecturas), para mostrarla como "por confirmar"."""
    groups = defaultdict(list)
    for t, c in reads:
        if t:
            groups[t].append(float(c))
    if not groups:
        return None
    text, confs = max(groups.items(), key=lambda kv: (sum(kv[1]), len(kv[1])))
    avg = sum(confs) / len(confs)
    return (text, round(avg, 3), len(confs)) if avg >= min_conf else None


# Caracteres que el OCR confunde entre si en placas mexicanas (misma forma a baja resolucion)
_CONFUSE = [set("0ODQ"), set("1IL7"), set("5S"), set("8B"), set("2Z"), set("6G"), set("CG"), set("4A"), set("MNWH"), set("UV"), set("EF"), set("PR")]


def _close(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in _CONFUSE)


def similar(a: Optional[str], b: Optional[str]) -> bool:
    """Misma placa con tolerancia de OCR: iguales, mismo largo con 1 caracter distinto, o (6+ caracteres) 2 distintos donde al menos uno
    es una confusion tipica del OCR. Solo se usa para enlazar registros que ya coinciden en lugar y tiempo, asi que el riesgo es bajo."""
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) != len(b) or len(a) < 5:
        return False
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 or (len(diff) == 2 and len(a) >= 6 and any(_close(x, y) for x, y in diff))


def enabled() -> bool:
    return ENABLED and not _failed


def _load():
    global _alpr, _failed
    with _lock:
        if _alpr is not None or _failed:
            return _alpr
        try:
            if LIBS not in sys.path:
                sys.path.append(LIBS)            # al final: numpy/cv2 de la imagen siempre ganan
            os.makedirs(CACHE, exist_ok=True)
            for sub, name in (("open-image-models", "open-image-models"), ("fast-plate-ocr", "fast-plate-ocr")):
                home_dir = os.path.join(os.path.expanduser("~"), ".cache", name)
                target = os.path.join(CACHE, sub)
                os.makedirs(target, exist_ok=True)
                if not os.path.exists(home_dir):
                    os.makedirs(os.path.dirname(home_dir), exist_ok=True)
                    os.symlink(target, home_dir)           # los modelos se descargan una vez y quedan en el volumen de datos
            from fast_alpr import ALPR
            try:          # siempre la GPU de Thor; si no esta disponible para este modelo se cae a CPU
                _alpr = ALPR(detector_model=DETECTOR, ocr_model=OCR, detector_conf_thresh=MIN_DET,
                             detector_providers=["CUDAExecutionProvider", "CPUExecutionProvider"], ocr_device="cuda")
                dev = "GPU"
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("plates: GPU no disponible (%s); usando CPU", exc)
                _alpr = ALPR(detector_model=DETECTOR, ocr_model=OCR, detector_conf_thresh=MIN_DET,
                             detector_providers=["CPUExecutionProvider"], ocr_device="cpu")
                dev = "CPU"
            logger.info("plates: lector cargado (%s + %s) en %s", DETECTOR, OCR, dev)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            _failed = True
            logger.warning("plates: lector no disponible (%s); correr scripts/install_alpr.sh", exc)
        return _alpr


def read(crop_bgr) -> Optional[dict]:
    """Mejor placa del recorte: {text, conf, det, jpeg} o None."""
    a = _load()
    if a is None or crop_bgr is None or crop_bgr.size == 0:
        return None
    best = None
    for r in a.predict(crop_bgr):
        if r.ocr is None:
            continue
        text = normalize(r.ocr.text)
        c = r.ocr.confidence
        conf = float(np.mean(c)) if not isinstance(c, (int, float)) else float(c)
        if not text or r.detection.confidence < MIN_DET:
            continue
        if best is None or conf * r.detection.confidence > best["conf"] * best["det"]:
            bb = r.detection.bounding_box
            pad = 8
            pc = crop_bgr[max(0, bb.y1 - pad):bb.y2 + pad, max(0, bb.x1 - pad):bb.x2 + pad]
            ok, buf = cv2.imencode(".jpg", pc, [cv2.IMWRITE_JPEG_QUALITY, 90]) if pc.size else (False, None)
            best = {"text": text, "conf": conf, "det": float(r.detection.confidence), "jpeg": buf.tobytes() if ok else None}
    return best
