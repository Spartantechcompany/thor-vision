"""
Reportes periódicos generados por el VLM a partir de eventos agregados —
ver app/storage/report_generator.py. Endpoints separados de routes_history.py
para no mezclar "eventos de cámara" con "texto sintetizado por el modelo".
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.storage.report_generator import generate_report

logger = logging.getLogger(__name__)
router = APIRouter()


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Storage not initialized")
    return db


@router.get("/api/reports")
def list_reports(request: Request, limit: int = 30):
    db = _db(request)
    return {"reports": db.query_reports(limit=limit)}


@router.post("/api/reports/generate")
def generate_report_now(request: Request, period_hours: float = 24.0):
    """Genera un reporte de inmediato — funciona aunque REPORT_ENABLED esté
    apagado (el kill-switch solo controla el hilo periódico automático)."""
    db = _db(request)
    analyzer = getattr(request.app.state, "vlm_analyzer", None)
    if analyzer is None:
        raise HTTPException(503, "VLM no inicializado")

    try:
        report_id = generate_report(db, analyzer, period_hours=period_hours, kind="manual")
    except Exception as e:
        logger.warning("generate_report_now: fallo inesperado: %s", e)
        raise HTTPException(503, "Error generando el reporte — intenta de nuevo en un momento")
    if report_id is None:
        raise HTTPException(
            422, "Sin observaciones en el periodo pedido, o el modelo/gateway no respondió a tiempo"
        )
    return {"id": report_id}
