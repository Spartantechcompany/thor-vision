import time
import cv2
import numpy as np
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

router = APIRouter()

# Colores BGR — paleta OXM Tech
_C_BLUE    = (194, 120, 30)    # oxm-blue #1E78C2
_C_ALERT   = (73,  81,  248)   # red-alert #F85149
_C_TEXT    = (200, 200, 200)   # texto suave
_C_GREEN   = (80,  185, 63)    # verde personas conocidas
_NEMOTRON_STALE_S = 25         # segundos antes de considerar resultado viejo


def _get_manager(request: Request):
    return request.app.state.capture_manager


def _get_store(request: Request):
    return getattr(request.app.state, "detection_store", None)


def _jpeg_encode(frame: np.ndarray, quality: int = 75) -> bytes:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def _offline_frame(w: int = 1280, h: int = 720) -> bytes:
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = (36, 27, 26)
    text  = "Sin señal"
    font  = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 1.2, 2
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    cv2.putText(frame, text, ((w - tw) // 2, (h + th) // 2),
                font, scale, (89, 89, 89), thick, cv2.LINE_AA)
    return _jpeg_encode(frame)


def _draw_overlay(frame: np.ndarray, detection) -> np.ndarray:
    """
    Dibuja overlay con datos de VLM:
    - Conteo de personas (badge superior-izquierdo)
    - Actividad / descripción (barra inferior)
    - Alertas reales (esquina superior-derecha)
    """
    if detection is None or detection.nemotron is None:
        return frame

    nem = detection.nemotron

    # Saltar si el resultado es un error o está desactualizado
    if nem.get("activity") == "error":
        return frame
    if time.time() - nem.get("_ts", 0) > _NEMOTRON_STALE_S:
        return frame

    out   = frame.copy()
    h, w  = out.shape[:2]
    font  = cv2.FONT_HERSHEY_SIMPLEX

    # ── Badge de personas (top-left) ──────────────────────────────────────
    people = nem.get("people", 0)
    if isinstance(people, int) and people > 0:
        label = f"  {people} persona{'s' if people != 1 else ''}  "
        sc, tk = 0.52, 1
        (bw, bh), _ = cv2.getTextSize(label, font, sc, tk)
        cv2.rectangle(out, (6, 6), (10 + bw, 14 + bh), (15, 12, 10), -1)
        cv2.putText(out, label, (8, 8 + bh), font, sc, _C_BLUE, tk, cv2.LINE_AA)

    # ── Barra de actividad (bottom) ───────────────────────────────────────
    activity = str(nem.get("activity", "")).strip()
    if activity and activity not in ("error", ""):
        # Truncar si es muy largo
        max_chars = max(30, w // 11)
        if len(activity) > max_chars:
            activity = activity[:max_chars - 1] + "…"
        sc, tk = 0.46, 1
        (tw2, th2), _ = cv2.getTextSize(activity, font, sc, tk)
        y_bot = h - 10
        cv2.rectangle(out, (4, y_bot - th2 - 8), (10 + tw2, y_bot + 4),
                      (10, 10, 10), -1)
        cv2.putText(out, activity, (7, y_bot - 2), font, sc, _C_TEXT, tk, cv2.LINE_AA)

    # ── Alertas reales (top-right) ────────────────────────────────────────
    _SKIP_ALERTS = {"error", "empty response", "bad json", "encode failed",
                    "unreachable", ""}
    alerts = [a for a in nem.get("alerts", []) if str(a).strip() not in _SKIP_ALERTS]
    for i, alert in enumerate(alerts[:2]):
        alert_str = str(alert)[:52]
        sc, tk = 0.44, 1
        (aw, ah), _ = cv2.getTextSize(alert_str, font, sc, tk)
        y_a = 8 + i * (ah + 12)
        x_a = w - aw - 12
        cv2.rectangle(out, (x_a - 4, y_a - 2), (w - 6, y_a + ah + 4),
                      (25, 15, 50), -1)
        cv2.putText(out, alert_str, (x_a, y_a + ah),
                    font, sc, _C_ALERT, tk, cv2.LINE_AA)

    return out


def _mjpeg_generator(cam_id: str, manager, store, target_fps: int = 15,
                     quality: int = 65, max_width: int = 1280):
    """
    Genera frames MJPEG con Content-Length y headers correctos.
    Resize de seguridad si el frame viene en resolución mayor (OpenCV fallback).
    """
    interval    = 1.0 / max(target_fps, 1)
    offline_jpg = _offline_frame()

    while True:
        try:
            t0    = time.monotonic()
            frame = manager.get_frame(cam_id)

            if frame is not None:
                # Safety: resize si el frame es más grande que max_width
                h, w = frame.shape[:2]
                if w > max_width:
                    scale = max_width / w
                    frame = cv2.resize(frame, (max_width, int(h * scale)),
                                       interpolation=cv2.INTER_LINEAR)
                if store is not None:
                    frame = _draw_overlay(frame, store.get(cam_id))
                jpeg = _jpeg_encode(frame, quality)
            else:
                jpeg = offline_jpg

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                b"\r\n" + jpeg + b"\r\n"
            )

            elapsed = time.monotonic() - t0
            sleep   = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

        except GeneratorExit:
            break
        except Exception:
            time.sleep(0.1)


@router.get("/stream/{cam_id}")
def stream(cam_id: str, request: Request):
    manager = _get_manager(request)
    store   = _get_store(request)
    cfg     = request.app.state.config.global_cfg
    # Calidad MJPEG más baja que snapshots — prioriza fluidez sobre detalle
    stream_quality = min(cfg.jpeg_quality, 65)
    return StreamingResponse(
        _mjpeg_generator(cam_id, manager, store, cfg.stream_fps,
                         stream_quality, cfg.frame_width),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control":     "no-cache, no-store, must-revalidate",
            "Pragma":            "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _stamp(frame: np.ndarray, name: str) -> np.ndarray:
    """Nombre de la camara y fecha/hora (la captura HD de la camara no trae el rotulo del video)."""
    out = frame.copy()
    h, w = out.shape[:2]
    sc = max(0.6, w / 1280 * 0.7)
    txt = f"{name}  {time.strftime('%d-%m-%Y %H:%M:%S')}"
    (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, sc, 2)
    cv2.rectangle(out, (0, 0), (tw + 20, th + 18), (0, 0, 0), -1)
    cv2.putText(out, txt, (10, th + 8), cv2.FONT_HERSHEY_SIMPLEX, sc, (255, 255, 255), 2, cv2.LINE_AA)
    return out


@router.get("/api/snapshot/{cam_id}")
def snapshot(cam_id: str, request: Request, hd: int = 0, native: int = 0, w: int = 0):
    """
    Snapshot JPEG estático. Usado por el dashboard como polling
    (evita límite de 6 conexiones HTTP/1.1 concurrentes del navegador).
    Resize a frame_width para limitar bandwidth.
    """
    manager   = _get_manager(request)
    store     = _get_store(request)
    cfg       = request.app.state.config.global_cfg
    big       = bool(hd or native)
    max_w     = 2800 if big else cfg.frame_width       # nativa/HD: sin reducir (hasta 2800 px de ancho; las de 5 MP traen 2592)
    quality   = 85 if big else min(cfg.jpeg_quality, 70)
    if 160 <= w < max_w:                                 # miniatura ligera (modo TV)
        max_w, quality = w, min(quality, 60)

    frame = manager.get_frame(cam_id)
    if hd and not native:                           # captura a resolucion completa de la propia camara (SUNAPI), si la soporta
        vm = getattr(request.app.state, "visits", None)
        g = (getattr(vm, "_grabbers", None) or {}).get(cam_id)
        full = g.grab() if g is not None else None
        if full is not None:
            h, w = full.shape[:2]
            if w > 1920:
                full = cv2.resize(full, (1920, int(h * 1920 / w)), interpolation=cv2.INTER_AREA)
            if store is not None:
                full = _draw_overlay(full, store.get(cam_id))

            return Response(content=_jpeg_encode(full, 85), media_type="image/jpeg", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    if frame is None:
        return Response(
            content=_offline_frame(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # Safety resize
    h, w = frame.shape[:2]
    if w > max_w:
        scale = max_w / w
        frame = cv2.resize(frame, (max_w, int(h * scale)),
                           interpolation=cv2.INTER_LINEAR)
    if store is not None:
        frame = _draw_overlay(frame, store.get(cam_id))
    return Response(
        content=_jpeg_encode(frame, quality),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
