"""Miniaturas al vuelo: las listas cargaban cientos de capturas completas (varios cientos de MB); con ?w= se sirven reducidas."""
import cv2
import numpy as np


def shrink(data: bytes, w: int, quality: int = 72) -> bytes:
    """JPEG reducido a `w` px de ancho (si ya es mas chico o falla la decodificacion, se devuelve igual)."""
    if not 32 <= w <= 1600:
        return data
    im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if im is None or im.shape[1] <= w:
        return data
    k = w / im.shape[1]
    out = cv2.resize(im, (w, max(1, int(im.shape[0] * k))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else data
