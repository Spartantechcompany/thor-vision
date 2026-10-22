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
    if os.environ.get("EVAL_SAMPLER", "true").lower() == "true":
        from app.api.routes_eval import start_sampler
        app.state.eval_stop = start_sampler(app)

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
    if os.environ.get("EMBED_ENABLED", "true").lower() == "true":                 # busqueda semantica (EmbeddingGemma 2); si el modelo no esta, el indexador se queda quieto
        from app.storage.embindex import start_indexer
        app.state.embed_stop = start_indexer(db)

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
    vision_models.hires_cams = {c.id for c in config.cameras if getattr(c, "zone", "") == "exterior"}
    app.state.vision_models = vision_models

    visit_manager = None
    if os.environ.get("TRACKER_ENABLED", "true").lower() == "true":
        from app.vision.visits import VisitManager
        visit_manager = VisitManager(db)
        try:
            from app.vision.bodyid import BodyID
            visit_manager.bodyid = BodyID(db)
        except (ImportError, OSError) as exc:
            logger.warning("BodyID apagado: %s", exc)
        visit_manager.zones = {c.id: c.zone for c in config.cameras}
    app.state.visits = visit_manager
    app.state.clef = None
    if visit_manager is not None and os.environ.get("CLEF_ENABLED", "true").lower() == "true":
        from app.vision.clef_client import ClefClient
        app.state.clef = ClefClient()
        visit_manager.set_verifier(app.state.clef)
        from app.vision import consolidate
        app.state.consolidate_stop = consolidate.start_nightly(db, visit_manager, app.state.clef)    # une de noche los "Persona #N" que son la misma

    vision_queue = VisionQueue(
        store    = detection_store,
        face_db  = face_db,
        models   = vision_models,
        db       = db,
        visits   = visit_manager,
    )
    from app.vision.parked import ParkedTracker
    from app.vision.pets import PetCollector
    app.state.parked = ParkedTracker(db)
    app.state.parked.cam_names = {c.id: c.name for c in config.cameras}
    app.state.pets = PetCollector(db)
    vision_queue.hooks = [app.state.parked.update, app.state.pets.update]

    def _pet_event(kind, pet, cam_id, conf, crop, _db=db, _names={c.id: c.name for c in config.cameras}):
        """Una mascota conocida salio de la casa o regreso: alerta informativa con la foto del recorte."""
        import cv2
        from app.vision.pet_presence import KNOWN
        cam = _names.get(cam_id, cam_id)
        txt = f"{KNOWN[pet]} {'salió de la casa' if kind == 'salida' else 'regresó a la casa'} (visto en {cam})"
        eid = _db.insert_event("nemotron", cam_id, {"schema": 2, "source": "mascotas", "people": 0, "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                                    "relevant": True, "alerts": [txt], "alert_types": ["mascota_" + kind], "severity": "low", "confidence": "medium",
                                                    "pet": {"name": KNOWN[pet], "kind": kind, "conf": round(conf, 2)}}, people=0, has_alert=True)
        snaps = getattr(app.state, "snapshots", None)
        if crop is not None and snaps is not None:
            try:
                snaps.save(cam_id, crop, "mascota", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("mascotas: sin captura: %s", exc)
    from app.vision.pet_presence import PetPresence
    _street = set((app.state.journeys.topo.get("street") if getattr(app.state, "journeys", None) else None) or ["cam-189", "cam-191", "cam-vto"])
    app.state.pet_presence = PetPresence(db, _street | {"cam-189", "cam-191", "cam-228"}, {c.id: c.name for c in config.cameras}, _pet_event)
    app.state.pets.presence = app.state.pet_presence
    if os.environ.get("EVAL_TRACK_LOG", "true").lower() == "true":
        from app.api.routes_eval import make_track_recorder
        vision_queue.hooks.append(make_track_recorder())          # cajas de persona con su tiempo, para comparar trackers fuera de linea
    vision_queue.start()
    if visit_manager is not None:
        from app.vision.gait import GaitCollector
        visit_manager.gait = GaitCollector(db, vision_models, manager._buffers)

    if visit_manager is not None and os.environ.get("PREROLL_ENABLED", "true").lower() == "true":
        from app.capture.preroll import PreRoll
        for cam in config.enabled_cameras:
            _buf = manager._buffers.get(cam.id)
            if _buf is not None and cam.mode != "faces":
                _pr = PreRoll(cam.id, _buf)
                _pr.start()
                vision_queue.preroll[cam.id] = _pr
        logger.info("Pre-roll: %d camaras", len(vision_queue.preroll))
        if vision_queue.preroll and os.environ.get("CLIPS_ENABLED", "true").lower() == "true":
            from app.storage.clips import ClipStore
            app.state.clips = ClipStore(db, os.environ.get("CLIPS_DIR", "/app/data/clips"), vision_queue.preroll)
            app.state.clips_stop = app.state.clips.start_purger()

    app.state.face_db       = face_db
    if visit_manager is not None:
        visit_manager.set_models(vision_models, face_db,
                                 {c.id: getattr(c, "rtsp_url", None) for c in config.cameras if getattr(c, "mode", "full") != "faces"})
    app.state.vision_models = vision_models
    vision_models.faces_only = {c.id for c in config.cameras if getattr(c, "mode", "full") == "faces"}

    # ── Movimiento nativo de las camaras (SUNAPI) como disparador de Qwen ──
    motion_monitors = {}
    if os.environ.get("NATIVE_MOTION_ENABLED", "true").lower() == "true":
        from app.capture.camera_motion import CameraMotionMonitor
        for cam in config.enabled_cameras:
            if cam.mode == "faces":
                continue
            m = CameraMotionMonitor(cam.id, cam.rtsp_url)
            m.start()
            motion_monitors[cam.id] = m
    app.state.motion_monitors = motion_monitors

    def _line_cross(cam_id, _names={c.id: c.name for c in config.cameras}):
        """La linea virtual IVA de la camara se cruzo: si YOLO ve una persona en los segundos siguientes, alerta con captura y clip."""
        import cv2
        import time
        time.sleep(3.0)                                         # YOLO sube a 2 fps con el movimiento; se le da tiempo de confirmar
        det = detection_store.get(cam_id)
        if det is None or not (det.yolo_persons and time.time() - (det.yolo_ts or 0) < 8):
            return
        h = time.localtime().tm_hour
        txt = f"Cruce de la línea virtual en {_names.get(cam_id, cam_id)}: {det.yolo_persons} persona(s)"
        eid = db.insert_event("nemotron", cam_id, {"schema": 2, "source": "iva", "people": det.yolo_persons, "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                                    "relevant": True, "alerts": [txt], "alert_types": ["cruce_linea"], "severity": "medium" if (h >= 22 or h < 6) else "low",
                                                    "confidence": "medium"}, people=det.yolo_persons, has_alert=True)
        buf = manager._buffers.get(cam_id)
        fe = buf.peek_latest() if buf is not None else None
        frame = getattr(fe, "frame", fe)
        if frame is not None:
            try:
                snapshots.save(cam_id, frame, "linea", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("cruce de linea: sin captura: %s", exc)
        _cl = getattr(app.state, "clips", None)
        if _cl is not None:
            _cl.request(eid, [cam_id])
    for _m in motion_monitors.values():
        _m.cross_cb = _line_cross

    yolo_workers = []
    for cam in config.enabled_cameras:
        buf = manager._buffers.get(cam.id)
        if buf is not None:
            w = VisionWorker(
                cam_id       = cam.id,
                buffer       = buf,
                vision_queue = vision_queue,
                fps          = 2.0 if cam.mode == "faces" else 0.5,
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
    if getattr(app.state, "parked", None) is not None:
        app.state.parked.analyzer = analyzer          # respaldo del OCR de placas
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
    if os.environ.get("ONVIF_ENABLED", "true").lower() == "true":
        from app.onvif.service import OnvifManager
        app.state.onvif = OnvifManager(db, config)
        app.state.onvif.start()
    app.state.parked.snapshots = snapshots
    from app.vision.doorbell import DoorAlerts
    _door_cams = [c.strip() for c in os.environ.get("DOORBELL_CAMS", "cam-vto").split(",") if c.strip()]
    def _armed_alert(cam_id, v, _snap=snapshots, _db=db, _names={c.id: c.name for c in config.cameras}):
        """Armado absoluto: cada visita que cierra es una alerta con su captura (escena, cuerpo o cara)."""
        _yr = getattr(app.state, "yolo_rules", None)
        if _yr is not None:
            try:
                _yr.on_visit(cam_id, v)                      # modo sin VLM: persona nocturna y merodeo por reglas sobre la visita confirmada
            except Exception as exc:                         # noqa: BLE001 - una regla con error no debe frenar el armado ni los viajes
                logger.warning("yolo_rules: %s", exc)
        from app.vision import arming
        if not arming.is_absolute():
            return
        import cv2
        import numpy as np
        dur = int(v["last_ts"] - v["first_ts"])
        who = f" ({v['name']})" if v.get("name") else ""
        txt = f"Armado absoluto: persona{who} en {_names.get(cam_id, cam_id)} durante {dur} s"
        eid = _db.insert_event("nemotron", cam_id, {"schema": 2, "source": "arming", "people": 1, "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                                    "relevant": True, "alerts": [txt], "alert_types": ["actividad_armado"], "severity": "high", "confidence": "high",
                                                    "visit": {"visit_id": v["visit_id"], "subject_id": v.get("subject_id")}}, people=1, has_alert=True)
        img = v.get("scene") or v.get("body") or v.get("face")
        frame = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR) if img else None
        if frame is not None and _snap is not None:
            try:
                _snap.save(cam_id, frame, "armado", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("armado: no se pudo guardar la captura: %s", exc)
    if visit_manager is not None:
        visit_manager.any_cb = _armed_alert
        from app.vision.scenes import Scenes as _Scenes
        from app.vision.yolo_rules import YoloRules
        app.state.yolo_rules = YoloRules(db, snapshots, _Scenes(), {c.id: c.name for c in config.cameras}, visit_manager.zones,
                                         lambda: getattr(getattr(app.state, "vlm_analyzer", None), "last_ok_ts", 0.0))

        def _door_loiter(cam_id, v, _snap=snapshots, _db=db, _names={c.id: c.name for c in config.cameras}):
            """Merodeo frente a la puerta peatonal (zona dibujada): alerta con captura; el personal conocido no alerta."""
            if v.get("name"):
                return
            import cv2
            import time
            import numpy as np
            h = time.localtime().tm_hour
            night = h >= 22 or h < 6
            txt = f"Merodeo frente a la puerta peatonal ({_names.get(cam_id, cam_id)}): persona {v['dwell_s']} s en la zona"
            eid = _db.insert_event("nemotron", cam_id, {"schema": 2, "source": "zona", "people": 1, "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                                        "relevant": True, "alerts": [txt], "alert_types": ["merodeo_puerta"], "severity": "high" if night else "medium",
                                                        "confidence": "high", "visit": {"visit_id": v.get("visit_id"), "subject_id": v.get("subject_id")}}, people=1, has_alert=True)
            img = v.get("scene") or v.get("body") or v.get("face")
            frame = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR) if isinstance(img, (bytes, bytearray)) else img
            _cl = getattr(app.state, "clips", None)
            if _cl is not None:
                _cl.request(eid, [cam_id])                    # el clip trae los 6 s previos y 6 s despues
            if frame is not None and _snap is not None:
                try:
                    _snap.save(cam_id, frame, "merodeo", event_id=eid)
                except (OSError, ValueError, cv2.error) as exc:
                    logger.warning("merodeo puerta: sin captura: %s", exc)
        visit_manager.zone_cb = _door_loiter
    app.state.doorbell = None
    for _dc in _door_cams:
        _cfg = next((c for c in config.cameras if c.id == _dc), None)
        if _cfg is None:
            continue
        app.state.doorbell = DoorAlerts(db, snapshots, manager._buffers, _dc, _cfg.name)
        if app.state.visits is not None:
            app.state.visits.visit_cb[_dc] = app.state.doorbell.visitor_alert
        if getattr(app.state, "onvif", None) is not None:
            app.state.onvif.dahua_cb = app.state.doorbell.ring_alert
            def _vto_grab(_ep=_dc, _on=app.state.onvif):
                from app.onvif import dahua_events as _de, registry as _reg
                ep = next((e for e in _reg.endpoints(config) if e.get("kind") == "vto"), None)
                u, p = _on._creds(ep["id"])
                return _de.snapshot(ep["host"], u, p)
            app.state.doorbell.grab = _vto_grab
    if os.environ.get("IOT_ENABLED", "true").lower() == "true":
        from app.iot.listener import IotListener
        app.state.iot = IotListener(db, snapshots, manager._buffers, {c.id: c.name for c in config.cameras})
        app.state.iot.detections = detection_store
        app.state.iot.clip_cb = lambda eid, cams: getattr(app.state, "clips", None) and app.state.clips.request(eid, cams)
        app.state.iot.start()
        from app.vision.journeys import JourneyLinker
        app.state.journeys = JourneyLinker(db, snapshots)
        app.state.journeys.clef = getattr(app.state, "clef", None)          # Clef juzga el cuerpo al unir visitas en un recorrido
        app.state.journeys.cam_names = {c.id: c.name for c in config.cameras}
        app.state.journeys.start()

        def _tuya_motion(alias, ts, _j=app.state.journeys, _vq=vision_queue):
            _j.tuya_event(alias, ts)
            for cam in _j._entry_cams(alias):
                _vq.submit_burst(cam, "tuya")          # reanaliza el pre-roll de las camaras de entrada: la persona puede estar entrando
        app.state.trips = None
        if os.environ.get("TRIPS_ENABLED", "true").lower() == "true":
            from app.vision.trips import Trips
            app.state.trips = Trips(db, snapshots, app.state.journeys.topo, app.state.journeys.cam_names)
            app.state.trips.analyzer = getattr(app.state, "vlm_analyzer", None)          # resumen de cada viaje con video
            if getattr(app.state, "doorbell", None) is not None:
                app.state.doorbell.on_ring = lambda ts, _t=app.state.trips: _t.start("vto-timbre", ts)   # el viaje del videoportero lo abre el timbre, no quien pasa
            app.state.trips_stop = app.state.trips.start_sweeper()
            if visit_manager is not None:
                visit_manager.trips_attend = app.state.trips.attending

        def _trip_trigger(alias, ts, _tr=lambda: app.state.trips, _vq=vision_queue):
            tr = _tr()
            if tr is not None and tr.start(alias, ts) is not None:
                for cam in tr.cams_for(alias):
                    _vq.submit_burst(cam, "viaje")          # el recorrido puede haber empezado ya: se reanalizan los ultimos segundos
        _motion_only = _tuya_motion

        def _tuya_motion(alias, ts, _m=_motion_only):
            _m(alias, ts)
            _trip_trigger(alias, ts)
        app.state.iot.on_motion = _tuya_motion

        def _light_moved(dev, ts, _vq=vision_queue):
            for cam in dev.get("cams") or []:
                _vq.submit_burst(cam, "switch")        # alguien movio un interruptor: la persona esta ahi, se reanalizan los ultimos segundos de las camaras de la zona
        app.state.iot.on_light = _light_moved
    from app.devices.manager import DeviceManager
    app.state.devices = DeviceManager(db, config)
    from app.api.chat_agent import start_chat_probe
    app.state.chat_probe_stop = start_chat_probe(app)

    native_min_s  = float(_env("VLM_NATIVE_MIN_INTERVAL_S", "NEMOTRON_NATIVE_MIN_INTERVAL_S", "30"))
    idle_beat_s   = float(_env("VLM_IDLE_HEARTBEAT_S", "NEMOTRON_IDLE_HEARTBEAT_S",     "900"))

    from app.vision import scenes as scenes_mod
    from app.vision.scenes import Scenes
    scenes = Scenes()
    if getattr(app.state, "visits", None) is not None:
        scenes_mod.KNOWN_PRESENT = app.state.visits.known_present
    if getattr(app.state, "pets", None) is not None:
        scenes_mod.PETS_KNOWN = app.state.pets.known_now
    require_person = os.environ.get("VLM_REQUIRE_PERSON", "true").lower() == "true"
    sustain_s      = float(os.environ.get("VLM_MOTION_SUSTAIN_S", "10"))

    vlm_workers = []
    for cam in config.enabled_cameras:
        buf = manager._buffers.get(cam.id)
        if buf is not None and cam.mode != "faces":
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
    getattr(app.state, "chat_probe_stop", None) and app.state.chat_probe_stop.set()
    getattr(app.state, "onvif", None) and app.state.onvif.stop()
    getattr(app.state, "iot", None) and app.state.iot.stop()
    for w in yolo_workers:
        w.stop()
    for nw in vlm_workers:
        nw.stop()
    manager.stop()
    vision_models.teardown()
    db.close()


app = FastAPI(title="THOR Vision", lifespan=lifespan)


from app.utils.quiet import QuietClosed
app.add_middleware(QuietClosed)
from app.utils.gzip_mw import SelectiveGZip
app.add_middleware(SelectiveGZip)
from app.utils.nocache_mw import NoCacheHTML
app.add_middleware(NoCacheHTML)

app.include_router(stream_router)
app.include_router(status_router)
app.include_router(ws_router)
app.include_router(vision_router)
app.include_router(chat_router)
app.include_router(history_router)
app.include_router(reports_router)
from app.api.chat_agent import router as chat_agent_router  # noqa: E402
app.include_router(chat_agent_router)
from app.api.routes_timeline import router as timeline_router  # noqa: E402
app.include_router(timeline_router)
from app.api.routes_searxng import router as searxng_router  # noqa: E402
app.include_router(searxng_router)
from app.api.routes_extras import router as extras_router  # noqa: E402
app.include_router(extras_router)
from app.api.routes_people_admin import router as people_admin_router  # noqa: E402
app.include_router(people_admin_router)
from app.api.routes_zones import router as zones_router
from app.api.routes_wifi import router as wifi_router
app.include_router(wifi_router)
app.include_router(zones_router)
from app.api.routes_onvif import router as onvif_router
app.include_router(onvif_router)
from app.api.routes_devices import router as devices_router
app.include_router(devices_router)
from app.api.routes_knowledge import router as knowledge_router
app.include_router(knowledge_router)
from app.api.routes_iot import router as iot_router
app.include_router(iot_router)
app.include_router(people_router)
from app.api.routes_rules import router as rules_router
app.include_router(rules_router)
from app.api.routes_eval import router as eval_router
app.include_router(eval_router)
from app.api.routes_arming import router as arming_router
app.include_router(arming_router)
from app.api.routes_semantic import router as semantic_router
app.include_router(semantic_router)
from app.api.routes_presence import router as presence_router
app.include_router(presence_router)
from app.api.routes_journeys import router as journeys_router
app.include_router(journeys_router)


@app.get("/buscar", response_class=HTMLResponse)
async def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/evaluacion", response_class=HTMLResponse)
async def eval_page(request: Request):
    return templates.TemplateResponse("eval.html", {"request": request})


@app.get("/reglas", response_class=HTMLResponse)
async def rules_page(request: Request):
    return templates.TemplateResponse("rules.html", {"request": request})


@app.get("/iot", response_class=HTMLResponse)
async def iot_page(request: Request):
    return templates.TemplateResponse("iot.html", {"request": request})


@app.get("/conocimiento", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    return templates.TemplateResponse("knowledge.html", {"request": request})


@app.get("/dispositivos", response_class=HTMLResponse)
async def devices_page(request: Request):
    return templates.TemplateResponse("devices.html", {"request": request})


@app.get("/onvif", response_class=HTMLResponse)
async def onvif_page(request: Request):
    return templates.TemplateResponse("onvif.html", {"request": request})


@app.get("/zonas", response_class=HTMLResponse)
async def zones_page(request: Request):
    return templates.TemplateResponse("zones.html", {"request": request})


@app.get("/presencia", response_class=HTMLResponse)
async def presence_page(request: Request):
    return templates.TemplateResponse("presence.html", {"request": request})


@app.get("/wifi", response_class=HTMLResponse)
async def wifi_page(request: Request):
    return templates.TemplateResponse("wifi.html", {"request": request})


@app.get("/tv", response_class=HTMLResponse)
async def tv_page(request: Request):
    return templates.TemplateResponse("tv.html", {"request": request, "umami_src": os.environ.get("UMAMI_SCRIPT_URL", ""), "umami_id": os.environ.get("UMAMI_WEBSITE_ID", "")})


@app.get("/camara/{cam_id}", response_class=HTMLResponse)
async def camera_page(request: Request, cam_id: str):
    return templates.TemplateResponse("camera.html", {"request": request, "umami_src": os.environ.get("UMAMI_SCRIPT_URL", ""), "umami_id": os.environ.get("UMAMI_WEBSITE_ID", "")})


@app.get("/galeria", response_class=HTMLResponse)
async def gallery_page(request: Request):
    return templates.TemplateResponse("gallery.html", {"request": request})


@app.get("/identidades", response_class=HTMLResponse)
async def identities_page(request: Request):
    return templates.TemplateResponse("identities.html", {"request": request})


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
