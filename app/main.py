import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request

from app.config import load_config
from app.capture.manager import CaptureManager
from app.api.routes_stream  import router as stream_router
from app.api.routes_status  import router as status_router
from app.api.routes_ws      import router as ws_router
from app.api.routes_vision  import router as vision_router
from app.api.routes_chat    import router as chat_router
from app.api.routes_history import router as history_router
from app.api.routes_reports import router as reports_router
from app.api.routes_people  import router as people_router
from app.utils.logger import setup_logging

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

def _env(new, old, default=None):
    """VLM_* tiene prioridad; NEMOTRON_* se conserva como respaldo para despliegues existentes."""
    return os.environ.get(new) or os.environ.get(old) or default

logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("THOR Vision starting...")
    config  = load_config()
    manager = CaptureManager(config)
    manager.start()

    app.state.config          = config
    app.state.capture_manager = manager

    # ── DetectionStore — compartido por YOLO y VLM ──────────────────
    from app.vision.detection_store import DetectionStore
    detection_store = DetectionStore()
    app.state.detection_store = detection_store

    # ── Persistencia: SQLite + snapshots ──────────────────────────────────
    from app.storage import EventDB, SnapshotManager, start_retention_thread, start_report_thread

    db = EventDB("/app/data/events.db")
    app.state.db = db

    snapshots = SnapshotManager(
        base_path   = "/app/data/snapshots",
        db          = db,
        frame_width = config.global_cfg.frame_width,
    )
    app.state.snapshots = snapshots

    retention_stop = start_retention_thread(
        db=db, snapshots=snapshots,
        interval_s          = float(os.environ.get("RETENTION_INTERVAL_S",        "3600")),
        max_events          = int(  os.environ.get("RETENTION_MAX_EVENTS",        "200000")),
        max_snapshot_bytes  = int(  os.environ.get("RETENTION_MAX_SNAPSHOT_GB",   "50")) * 1024**3,
        max_age_days        = int(  os.environ.get("RETENTION_MAX_AGE_DAYS",      "30")),
        max_face_sightings  = int(  os.environ.get("RETENTION_MAX_FACE_SIGHTINGS","100000")),
    )
    app.state.retention_stop = retention_stop

    # ── YOLO + InsightFace — bboxes rápidos (paralelo a VLM) ─────────
    from app.vision.face_db            import FaceDB
    from app.pipeline.vision_processor import VisionModels
    from app.vision.worker             import VisionQueue, VisionWorker

    face_db = FaceDB(
        db_path                = "/app/data/metadata/faces.json",
        recognition_threshold  = 0.40,
    )
    vision_models = VisionModels(device="cuda:0", yolo_conf=0.25)
    vision_models.setup()

    visit_manager = None
    if os.environ.get("TRACKER_ENABLED", "true").lower() == "true":
        from app.vision.visits import VisitManager
        visit_manager = VisitManager(db)
    app.state.visits = visit_manager

    vision_queue = VisionQueue(
        store    = detection_store,
        face_db  = face_db,
        models   = vision_models,
        db       = db,
        visits   = visit_manager,
    )
    vision_queue.start()

    if visit_manager is not None and os.environ.get("PREROLL_ENABLED", "true").lower() == "true":
        from app.capture.preroll import PreRoll
        for cam in config.enabled_cameras:
            _buf = manager._buffers.get(cam.id)
            if _buf is not None:
                _pr = PreRoll(cam.id, _buf)
                _pr.start()
                vision_queue.preroll[cam.id] = _pr
        logger.info("Pre-roll: %d camaras", len(vision_queue.preroll))

    app.state.face_db       = face_db
    if visit_manager is not None:
        visit_manager.set_models(vision_models, face_db,
                                 {c.id: getattr(c, "rtsp_url", None) for c in config.cameras})
    app.state.vision_models = vision_models

    # ── Movimiento nativo de las camaras (SUNAPI) como disparador de Qwen ──
    motion_monitors = {}
    if os.environ.get("NATIVE_MOTION_ENABLED", "true").lower() == "true":
        from app.capture.camera_motion import CameraMotionMonitor
        for cam in config.enabled_cameras:
            m = CameraMotionMonitor(cam.id, cam.rtsp_url)
            m.start()
            motion_monitors[cam.id] = m
    app.state.motion_monitors = motion_monitors

    yolo_workers = []
    for cam in config.enabled_cameras:
        buf = manager._buffers.get(cam.id)
        if buf is not None:
            w = VisionWorker(
                cam_id       = cam.id,
                buffer       = buf,
                vision_queue = vision_queue,
                fps          = 0.5,
                motion_source = motion_monitors.get(cam.id),
                boost_fps    = float(os.environ.get("YOLO_BOOST_FPS", "2")),
                hunt_source  = visit_manager,
                hunt_fps     = float(os.environ.get("FACE_HUNT_FPS", "4")),
            )
            w.start()
            yolo_workers.append(w)

    app.state.yolo_workers = yolo_workers
    logger.info("YOLO pipeline: %d workers, 1 serial queue", len(yolo_workers))

    # ── VLM workers — análisis semántico motion-gated ────────────────
    from app.vision.vlm_analyzer import VLMAnalyzer
    from app.vision.vlm_worker   import VLMWorker

    vlm_endpoint = _env("VLM_ENDPOINT", "NEMOTRON_ENDPOINT",  "http://localhost:8003")
    vlm_model    = _env("VLM_MODEL", "NEMOTRON_MODEL", "thor-vision")
    vlm_api_key  = _env("VLM_API_KEY", "NEMOTRON_API_KEY")
    vlm_min_s    = float(_env("VLM_MIN_INTERVAL_S", "NEMOTRON_MIN_INTERVAL_S",   "30"))
    vlm_max_s    = float(_env("VLM_MAX_INTERVAL_S", "NEMOTRON_MAX_INTERVAL_S",  "120"))
    vlm_motion   = float(_env("VLM_MOTION_THRESHOLD", "NEMOTRON_MOTION_THRESHOLD", "0.04"))
    snapshot_periodic = float(os.environ.get("SNAPSHOT_PERIODIC_S",       "600"))
    video_window_s    = float(_env("VLM_VIDEO_WINDOW_S", "NEMOTRON_VIDEO_WINDOW_S",  "3"))

    analyzer = VLMAnalyzer(
        endpoint      = vlm_endpoint,
        model         = vlm_model,
        max_tokens    = 420,
        timeout       = 25,
        video_timeout = 60,
        api_key       = vlm_api_key,
        max_width     = int(os.environ.get("VLM_MAX_WIDTH", "1280")),
        max_height    = int(os.environ.get("VLM_MAX_HEIGHT", "720")),
    )
    app.state.vlm_analyzer = analyzer
    if visit_manager is not None and os.environ.get("VISIT_VLM_ENABLED", "true").lower() == "true":
        visit_manager.set_analyzer(analyzer)

    # ── Reportes periódicos (flywheel de triage de alertas, opt-in) ────────
    # El generador necesita `db` (agregados) y `analyzer` (redacción vía
    # LLM) — arranca aquí, después de que ambos ya existen.
    report_stop = start_report_thread(
        db=db, analyzer=analyzer,
        interval_s   = float(os.environ.get("REPORT_INTERVAL_HOURS", "24")) * 3600,
        period_hours = float(os.environ.get("REPORT_PERIOD_HOURS",   "24")),
        kind         = os.environ.get("REPORT_KIND", "daily"),
        enabled      = os.environ.get("REPORT_ENABLED", "false").lower() == "true",
    )
    app.state.report_stop = report_stop

    native_min_s  = float(_env("VLM_NATIVE_MIN_INTERVAL_S", "NEMOTRON_NATIVE_MIN_INTERVAL_S", "30"))
    idle_beat_s   = float(_env("VLM_IDLE_HEARTBEAT_S", "NEMOTRON_IDLE_HEARTBEAT_S",     "900"))

    from app.vision.scenes import Scenes
    scenes = Scenes()
    require_person = os.environ.get("VLM_REQUIRE_PERSON", "true").lower() == "true"
    sustain_s      = float(os.environ.get("VLM_MOTION_SUSTAIN_S", "10"))

    vlm_workers = []
    for cam in config.enabled_cameras:
        buf = manager._buffers.get(cam.id)
        if buf is not None:
            nw = VLMWorker(
                motion_source         = motion_monitors.get(cam.id),
                native_min_interval_s = native_min_s,
                idle_heartbeat_s      = idle_beat_s,
                scene                 = scenes.get(cam.id),
                require_person        = require_person,
                sustain_s             = sustain_s,
                cam_id              = cam.id,
                buffer              = buf,
                store               = detection_store,
                analyzer            = analyzer,
                min_interval_s      = vlm_min_s,
                max_interval_s      = vlm_max_s,
                motion_threshold    = vlm_motion,
                db                  = db,
                snapshots           = snapshots,
                snapshot_periodic_s = snapshot_periodic,
                video_window_s      = video_window_s,
            )
            nw.start()
            vlm_workers.append(nw)

    # ── Pipeline de detalle para cam-cowork (experimento, opt-in) ──────────
    # Segundo worker sobre la MISMA cámara/buffer, prompt especializado en
    # tipo de contenido por monitor. Escribe bajo un cam_id sintético para
    # no pisar el resultado del pipeline general en DetectionStore/events
    # (ver CLAUDE.md / plan de esta sesión). No toca config/cameras.yml.
    cowork_detail_enabled = os.environ.get("COWORK_DETAIL_ENABLED", "false").lower() == "true"
    if cowork_detail_enabled:
        cowork_buf = manager._buffers.get("cam-cowork")
        if cowork_buf is not None:
            from app.vision.vlm_analyzer import MONITOR_DETAIL_PROMPT

            detail_analyzer = VLMAnalyzer(
                endpoint      = vlm_endpoint,
                model         = vlm_model,
                max_tokens    = 300,
                timeout       = 25,
                video_timeout = 60,
                api_key       = vlm_api_key,
                user_prompt   = MONITOR_DETAIL_PROMPT,
                max_width     = 960,
                # cam-cowork es 16:9 nativo (1920x1080) — sin este max_height
                # explicito, el nuevo cap simetrico de VLMAnalyzer usaria
                # el default (360) y encogeria esto a 640x360, deshaciendo el
                # punto de subir max_width a 960 para mas detalle de pantallas.
                max_height    = 540,
            )
            detail_worker = VLMWorker(
                cam_id           = "cam-cowork-detail",
                context_cam_id   = "cam-cowork",
                buffer           = cowork_buf,
                store            = detection_store,
                analyzer         = detail_analyzer,
                min_interval_s   = 120.0,
                max_interval_s   = 120.0,
                # Alto a propósito: desactiva el disparo por movimiento.
                # No tiene sentido mandar clips de video por movimiento en
                # pantalla — un frame cada 2 min (heartbeat) alcanza y es
                # más barato para un gateway ya con fallas intermitentes.
                motion_threshold = 1.0,
                db               = db,
                snapshots        = None,
                event_type       = "nemotron_detail",
            )
            detail_worker.start()
            vlm_workers.append(detail_worker)
            logger.info("Cowork detail pipeline: activo (cam-cowork-detail, heartbeat=120s)")
        else:
            logger.warning("COWORK_DETAIL_ENABLED=true pero no hay buffer para cam-cowork")

    app.state.vlm_workers = vlm_workers

    logger.info(
        "Started — %d cams | YOLO 0.5fps | VLM motion-gated min=%.0fs max=%.0fs | endpoint=%s",
        len(config.enabled_cameras),
        vlm_min_s, vlm_max_s, vlm_endpoint,
    )
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("THOR Vision shutting down...")
    retention_stop.set()
    report_stop.set()
    for w in yolo_workers:
        w.stop()
    for nw in vlm_workers:
        nw.stop()
    manager.stop()
    vision_models.teardown()
    db.close()


app = FastAPI(title="THOR Vision", lifespan=lifespan)

app.include_router(stream_router)
app.include_router(status_router)
app.include_router(ws_router)
app.include_router(vision_router)
app.include_router(chat_router)
app.include_router(history_router)
app.include_router(reports_router)
app.include_router(people_router)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ip = request.client.host if request.client else "-"
    ua = request.headers.get('user-agent', '-')
    logger.info(f"dashboard visit ip={ip} ua={ua}")
    db = getattr(request.app.state, "db", None)
    if db is not None:
        db.insert_dashboard_visit(ip=ip, user_agent=ua, path="/")
    return templates.TemplateResponse("index.html", {"request": request,
        "umami_src": os.environ.get("UMAMI_SCRIPT_URL", ""), "umami_id": os.environ.get("UMAMI_WEBSITE_ID", "")})
