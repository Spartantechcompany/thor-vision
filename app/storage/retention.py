"""
Background thread que corre `db.rotate()` + `snapshots.rotate()` periódicamente.
"""
import logging
import threading
import time

from app.storage.db                import EventDB
from app.storage.snapshot_manager  import SnapshotManager

logger = logging.getLogger(__name__)


def start_retention_thread(
    db: EventDB,
    snapshots: SnapshotManager,
    interval_s: float          = 3600.0,
    max_events: int            = 200_000,
    max_chat_per_session: int  = 1_000,
    max_snapshot_bytes: int    = 50 * 1024 * 1024 * 1024,
    max_age_days: int          = 30,
    max_face_sightings: int    = 100_000,
) -> threading.Event:
    """
    Arranca un thread daemon que corre retention cada `interval_s` segundos.
    Retorna un threading.Event que, si se setea, detiene el loop.
    """
    stop = threading.Event()

    def _run():
        # Delay inicial corto — para no correr exactamente al arranque
        stop.wait(60)
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                db_res   = db.rotate(
                    max_events           = max_events,
                    max_chat_per_session = max_chat_per_session,
                    max_face_sightings   = max_face_sightings,
                )
                snap_res = snapshots.rotate(
                    max_bytes    = max_snapshot_bytes,
                    max_age_days = max_age_days,
                )
                logger.info(
                    "Retention done in %.1fs — db=%s snapshots=%s",
                    time.monotonic() - t0,
                    db_res or "no-op",
                    snap_res,
                )
            except Exception as e:
                logger.warning("Retention error: %s", e)

            stop.wait(interval_s)

    t = threading.Thread(target=_run, daemon=True, name="retention")
    t.start()
    logger.info("Retention thread started — interval=%.0fs", interval_s)
    return stop
