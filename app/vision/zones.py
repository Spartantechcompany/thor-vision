"""Zonas por camara dibujadas por el usuario (poligonos normalizados 0..1 sobre el cuadro).

Tipos:
  ignorar_objetos  YOLO descarta autos/perros/bolsas... cuyo punto base cae dentro (falsos positivos fijos: una palmera, una sombra)
  ignorar_todo     igual, pero tambien personas
  estacionamiento  solo se registran vehiculos estacionados dentro de estas zonas (si la camara tiene alguna)
  puerta_peatonal  frente a la puerta peatonal: una persona real que se queda ahi DOOR_DWELL_S segundos es merodeo (alerta con captura)
  nota             texto fijo para el VLM: "esto siempre esta ahi, no lo describas ni alertes por ello"

Se guardan en data/zones.json (se relee solo cuando cambia el archivo).
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

PATH = os.environ.get("ZONES_PATH", "/app/data/zones.json")
TYPES = ("ignorar_objetos", "ignorar_todo", "estacionamiento", "nota", "puerta_peatonal")
MAX_ZONES_PER_CAM = 30
MAX_POINTS = 60

_lock = threading.Lock()
_cache: dict = {"mtime": None, "checked": 0.0, "data": {}}


def _load() -> dict:
    now = time.time()
    with _lock:
        if now - _cache["checked"] < 2.0:
            return _cache["data"]
        _cache["checked"] = now
        try:
            m = os.path.getmtime(PATH)
        except OSError:
            _cache.update(mtime=None, data={})
            return _cache["data"]
        if m != _cache["mtime"]:
            try:
                with open(PATH, encoding="utf-8") as f:
                    raw = json.load(f)
                _cache.update(mtime=m, data=raw if isinstance(raw, dict) else {})
            except (OSError, ValueError) as exc:
                logger.warning("zones: no se pudo leer %s: %s", PATH, exc)
        return _cache["data"]


def get(cam_id: str) -> list:
    return list(_load().get(cam_id) or [])


def all_zones() -> dict:
    return dict(_load())


def validate(zones: list) -> list:
    """Normaliza y valida; lanza ValueError con un mensaje claro."""
    if not isinstance(zones, list) or len(zones) > MAX_ZONES_PER_CAM:
        raise ValueError(f"maximo {MAX_ZONES_PER_CAM} zonas por camara")
    out = []
    for i, z in enumerate(zones):
        if not isinstance(z, dict) or z.get("type") not in TYPES:
            raise ValueError(f"zona {i + 1}: tipo invalido; usa {list(TYPES)}")
        pts = z.get("pts")
        if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POINTS:
            raise ValueError(f"zona {i + 1}: el poligono necesita de 3 a {MAX_POINTS} puntos")
        clean = []
        for p in pts:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                raise ValueError(f"zona {i + 1}: punto invalido") from None
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(f"zona {i + 1}: los puntos van de 0 a 1")
            clean.append([round(x, 4), round(y, 4)])
        out.append({"id": str(z.get("id") or f"z{i + 1}")[:20], "type": z["type"], "name": str(z.get("name") or "")[:60],
                    "text": str(z.get("text") or "")[:240], "pts": clean})
    return out


def save(cam_id: str, zones: list) -> list:
    clean = validate(zones)
    with _lock:
        try:
            with open(PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if clean:
            data[cam_id] = clean
        else:
            data.pop(cam_id, None)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
        _cache.update(mtime=None, checked=0.0)
    return clean


def point_in_polygon(x: float, y: float, pts: list) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _base_point(box, w: int, h: int) -> tuple:
    """Punto base normalizado (centro inferior de la caja), como en Frigate."""
    return ((box[0] + box[2]) / 2.0 / max(1, w), box[3] / max(1, h))


def overlap_frac(box, pts: list, w: int, h: int) -> float:
    """Fraccion del area de la caja que cae dentro del poligono (pts normalizados), con una rejilla chica."""
    import cv2
    import numpy as np
    gw, gh = 160, 120
    x1, y1, x2, y2 = (max(0, int(box[0] / w * gw)), max(0, int(box[1] / h * gh)), min(gw, int(box[2] / w * gw) + 1), min(gh, int(box[3] / h * gh) + 1))
    if x2 <= x1 or y2 <= y1:
        return 0.0
    mask = np.zeros((gh, gw), np.uint8)
    cv2.fillPoly(mask, [np.array([[int(px * gw), int(py * gh)] for px, py in pts], np.int32)], 1)
    return float(mask[y1:y2, x1:x2].sum()) / float((x2 - x1) * (y2 - y1))


def contains(cam_id: str, types: tuple, box, w: int, h: int, min_overlap: float = 0.35) -> bool:
    x, y = _base_point(box, w, h)
    for z in get(cam_id):
        if z["type"] in types and (point_in_polygon(x, y, z["pts"]) or overlap_frac(box, z["pts"], w, h) >= min_overlap):
            return True
    return False


def has(cam_id: str, ztype: str) -> bool:
    return any(z["type"] == ztype for z in get(cam_id))


def filter_detections(cam_id: str, persons: list, objects: list, w: int, h: int) -> tuple:
    """Quita de las detecciones de YOLO lo que cae en zonas de ignorar."""
    zs = [z for z in get(cam_id) if z["type"] in ("ignorar_objetos", "ignorar_todo")]
    if not zs:
        return persons, objects
    obj_zs = [z["pts"] for z in zs]
    all_zs = [z["pts"] for z in zs if z["type"] == "ignorar_todo"]

    def inside(box, polys):
        x, y = _base_point(box, w, h)
        return any(point_in_polygon(x, y, p) for p in polys)

    return ([b for b in persons if not inside(b, all_zs)] if all_zs else persons,
            [o for o in objects if not inside(o["b"], obj_zs)])


def notes(cam_id: str) -> list:
    return [(z["name"], z["text"]) for z in get(cam_id) if z["type"] == "nota" and z["text"]]
