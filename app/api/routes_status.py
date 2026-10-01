import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.utils.gpu_probe import get_system_metrics

router = APIRouter()


def _get_manager(request: Request):
    return request.app.state.capture_manager


@router.get("/api/health")
def health(request: Request):
    manager = _get_manager(request)
    return {"status": "ok", **manager.summary()}


@router.get("/api/cameras")
def cameras(request: Request):
    manager = _get_manager(request)
    return {"cameras": manager.get_all_stats()}


@router.get("/api/cameras/{cam_id}")
def camera_detail(cam_id: str, request: Request):
    manager = _get_manager(request)
    return manager.get_camera_stats(cam_id)


@router.get("/api/stats")
def stats(request: Request):
    manager = _get_manager(request)
    return {
        "summary": manager.summary(),
        "cameras": manager.get_all_stats(),
        "system": get_system_metrics(),
    }


@router.get("/api/system")
def system_metrics():
    return get_system_metrics()


@router.get("/api/vlm")
def vlm_health(request: Request):
    """
    Salud del modelo de visión-lenguaje que analiza los frames.
    Existe porque el analizador puede quedarse mudo sin que nada lo note:
    en julio 2026 estuvo caído 3 días y el dashboard seguía viéndose normal.
    """
    endpoint = (os.environ.get("VLM_ENDPOINT") or os.environ.get("NEMOTRON_ENDPOINT", ""))
    # Lo que se PIDE al endpoint. Con gateway de por medio suele ser un
    # alias (ej. "thor-vision"), no el modelo real que atiende.
    model    = (os.environ.get("VLM_MODEL") or os.environ.get("NEMOTRON_MODEL", ""))
    store    = getattr(request.app.state, "detection_store", None)
    analyzer = getattr(request.app.state, "vlm_analyzer", None)
    model_real = getattr(analyzer, "last_model", None) if analyzer else None
    manager  = _get_manager(request)
    # DetectionStore tambien guarda el pipeline de detalle de Cowork bajo el
    # cam_id sintetico "cam-cowork-detail" (ver COWORK_DETAIL_ENABLED en
    # main.py) — sin este filtro se contaba como una 11a "camara" en
    # cams_total/cams_ok/cams_fresh, aunque solo hay 10 camaras reales.
    real_cam_ids = {c["id"] for c in manager.get_all_stats()} if manager else None

    now, cams = time.time(), []
    if store:
        for cam_id, det in store.get_all().items():
            if real_cam_ids is not None and cam_id not in real_cam_ids:
                continue
            r = getattr(det, "nemotron", None) or {}
            if not r:
                continue
            ts = r.get("_ts") or 0
            cams.append({
                "cam_id":   cam_id,
                "ok":       r.get("activity") != "error",
                "age_s":    round(now - ts, 1) if ts else None,
                "ms":       r.get("_ms") or 0,
                "activity": str(r.get("activity") or "")[:80],
            })

    ok  = [c for c in cams if c["ok"]]
    lat = [c["ms"] for c in ok if c["ms"]]
    # Fresco = analizado hace menos de 5 min (el worker corre cada 30-120s)
    fresh = [c for c in ok if c["age_s"] is not None and c["age_s"] < 300]

    if not cams:
        status = "unknown"
    elif not ok:
        status = "down"
    elif len(fresh) < max(1, len(cams) // 2):
        status = "degraded"
    else:
        status = "ok"

    return {
        "status":      status,
        "model":       model,
        "model_real":  model_real or None,
        "endpoint":    endpoint,
        "cams_total":  len(cams),
        "cams_ok":     len(ok),
        "cams_fresh":  len(fresh),
        "latency_ms":  round(sum(lat) / len(lat)) if lat else None,
        "last_ok_s":   round(min((c["age_s"] for c in ok if c["age_s"] is not None), default=0), 1) if ok else None,
        "cameras":     sorted(cams, key=lambda c: c["cam_id"]),
    }


@router.get("/api/cowork/detail")
def cowork_detail(request: Request):
    """
    Resultado del pipeline de detalle de pantallas para cam-cowork
    (experimento, opt-in vía COWORK_DETAIL_ENABLED). 404 si está apagado
    o todavía no corrió el primer heartbeat — el frontend lo trata como
    "no hay nada que mostrar", nunca como error.
    """
    store = getattr(request.app.state, "detection_store", None)
    det = store.get("cam-cowork-detail") if store else None
    result = getattr(det, "nemotron", None) if det else None
    if not result or result.get("activity") == "error":
        return JSONResponse({"available": False}, status_code=404)

    return {
        "available":      True,
        "monitor_count":  result.get("monitor_count"),
        "monitors":       result.get("monitors") or [],
        "people":         result.get("people"),
        "age_s":          round(time.time() - (result.get("_ts") or 0), 1),
    }


@router.get("/api/pipeline")
def pipeline(request: Request):
    """Estado por camara de las 3 etapas: movimiento -> YOLO -> Qwen (VLM)."""
    st = request.app.state
    monitors = getattr(st, "motion_monitors", {}) or {}
    workers = {w.cam_id: w for w in (getattr(st, "vlm_workers", []) or [])
               if w.event_type == "nemotron"}
    store = getattr(st, "detection_store", None)
    yw = {w.cam_id: w for w in (getattr(st, "yolo_workers", []) or [])}
    cams = {}
    totals: dict = {}
    for cam_id, w in workers.items():
        m = monitors.get(cam_id)
        ms = m.snapshot() if m else None
        native = bool(m and m.usable())
        det = store.get(cam_id) if store else None
        calls = w.calls_last_hour()
        for k, v in calls.items():
            totals[k] = totals.get(k, 0) + v
        cams[cam_id] = {
            "motion": {
                "source": "camera" if native else "software",
                "native": ms,
                "native_usable": native,
            },
            "yolo": {
                "last_ts": det.yolo_ts if det else 0,
                "persons": det.yolo_persons if det else 0,
                "boosted": bool(getattr(yw.get(cam_id), "boosted", False)),
                "ms": round(det.inference_ms) if det else 0,
            },
            "vlm": {**(w.last_info or {}), "calls_1h": calls,
                     "skips_1h": w.skips_last_hour()},
        }
    skips = sum(c["vlm"]["skips_1h"] for c in cams.values())
    return {"ts": time.time(), "cameras": cams, "vlm_calls_1h": totals,
            "vlm_skips_1h": skips}


def _tcp_rtt(host, port, timeout=1.0):
    import socket
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return None


@router.get("/api/connections")
def connections(request: Request):
    """Como esta conectada cada camara (sin credenciales): RTSP, decodificador, SUNAPI."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse
    st = request.app.state
    mgr = getattr(st, "capture_manager", None)
    if mgr is None:
        return {"cameras": []}
    monitors = getattr(st, "motion_monitors", {}) or {}
    vm = getattr(st, "visits", None)
    grabbers = getattr(vm, "_grabbers", {}) or {}
    stats = {x["id"]: x for x in mgr.get_all_stats()}
    items = []
    for cam_id, r in mgr._readers.items():
        c = r.connection()
        c.update({"id": cam_id, "name": r.cam.name, "zone": r.cam.zone})
        sx = stats.get(cam_id, {})
        c.update({"state": sx.get("state"), "fps": sx.get("fps"), "latency_ms": sx.get("latency_ms"),
                  "attempts": sx.get("connect_attempts"), "error": sx.get("error")})
        m = monitors.get(cam_id)
        if m:
            base = getattr(m, "_base", None)
            pu = urlparse(base) if base else None
            ms = m.snapshot()
            c["sunapi"] = {"base": (f"{pu.scheme}://{pu.hostname}:{pu.port or (443 if pu.scheme == 'https' else 80)}" if pu else None),
                           "motion_usable": bool(m.usable()), "detector": ms.get("detection_type"),
                           "reachable": ms.get("reachable"), "rises_1h": ms.get("rises_1h")}
        g = grabbers.get(cam_id)
        if g:
            c["snapshot"] = {"usable": bool(g.usable()), "grabs": g.grabs, "last_ms": round(g.last_ms),
                             "base": g._base, "paused_s": max(0, round(g._off_until - time.time()))}
        items.append(c)
    with ThreadPoolExecutor(max_workers=10) as ex:
        rtts = list(ex.map(lambda c: _tcp_rtt(c["host"], c["port"]), items))
    for c, rtt in zip(items, rtts):
        c["rtt_ms"] = rtt
    return {"ts": time.time(), "cameras": items}


@router.get("/spot")
def spot_animation():
    from fastapi.responses import FileResponse
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "static", "spot.html")
    if not os.path.exists(path):
        return JSONResponse({"detail": "no disponible"}, status_code=404)
    return FileResponse(path, media_type="text/html")
