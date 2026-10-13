"""EmbeddingGemma 2 (google/embeddinggemma-2, Apache 2.0): texto e imagenes al mismo espacio de vectores, en la GPU de Thor.
Las dependencias (sentence-transformers, transformers) viven en data/pylibs-emb (scripts/install_embed.sh), no en la imagen:
se agregan al FINAL de sys.path para no pisar nada de la imagen. Vectores de EMBED_DIM dimensiones (Matryoshka, normalizados)."""
import logging
import os
import sys
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)
LIBS = os.environ.get("EMBED_LIBS", "/app/data/pylibs-emb")
MODEL = os.environ.get("EMBED_MODEL", "google/embeddinggemma-2")
DIM = int(os.environ.get("EMBED_DIM", "256"))             # 256 conserva casi toda la calidad (guia del modelo) y ocupa 512 bytes por vector
_lock = threading.Lock()
_waiting = 0                     # consultas de usuario esperando el modelo: el indexador de fondo les cede el turno
_wlock = threading.Lock()
_model = None
_failed = False


def _load():
    global _model, _failed
    if _model is not None or _failed:
        return _model
    with _lock:
        if _model is not None or _failed:
            return _model
        try:
            if LIBS and os.path.isdir(LIBS) and LIBS not in sys.path:
                sys.path.append(LIBS)
            os.environ.setdefault("HF_HOME", os.environ.get("EMBED_HF_HOME", "/app/data/models/hf"))
            if os.path.isdir(os.path.join(os.environ["HF_HOME"], "hub", "models--" + MODEL.replace("/", "--"))):
                os.environ.setdefault("HF_HUB_OFFLINE", "1")          # ya descargado: sin consultas a internet al cargar
            import torch
            from sentence_transformers import SentenceTransformer
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            m = SentenceTransformer(MODEL, device=dev, model_kwargs={"torch_dtype": torch.bfloat16 if dev == "cuda" else torch.float32})
            logger.info("EmbeddingGemma 2 cargado en %s (%d dim)", dev, DIM)
            _model = m
        except Exception as exc:                          # noqa: BLE001 - sin el modelo la busqueda semantica se apaga, el resto del sistema sigue
            logger.warning("EmbeddingGemma 2 no disponible: %s", exc)
            _failed = True
    return _model


def available() -> bool:
    return _load() is not None


def _norm(a) -> np.ndarray:
    a = np.asarray(a, np.float32)[..., :DIM]
    return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-9)


def embed_texts(texts: list, query: bool = False, batch: int = 64) -> np.ndarray:
    m = _load()
    if m is None or not texts:
        return np.zeros((0, DIM), np.float32)
    global _waiting
    if query:
        with _wlock:
            _waiting += 1
    try:
        out = []
        for i in range(0, len(texts), 64):             # el modelo se suelta entre lotes: una consulta del usuario no espera detras de miles de textos
            while not query and _waiting:
                time.sleep(0.05)                       # el indexador espera si hay una consulta de usuario en curso
            with _lock:
                out.append(m.encode(list(texts[i:i + 64]), prompt_name="SearchQuery" if query else "Document", batch_size=batch, convert_to_numpy=True))
        return _norm(np.concatenate(out))
    finally:
        if query:
            with _wlock:
                _waiting -= 1


def embed_images(images: list, batch: int = 16) -> np.ndarray:
    """images: rutas de archivo o imagenes PIL."""
    m = _load()
    if m is None or not images:
        return np.zeros((0, DIM), np.float32)
    out = []
    for i in range(0, len(images), batch):
        while _waiting:
            time.sleep(0.05)
        with _lock:
            out.append(m.encode([{"image": x} for x in images[i:i + batch]], batch_size=batch, convert_to_numpy=True))
    return _norm(np.concatenate(out))
