"""
Genera reportes periódicos en texto natural (español) usando el propio VLM
como "redactor" — parte del flywheel de triage de alertas: el prompt le dice
al modelo qué alertas ya se marcaron importante/ruido (si el usuario las
revisó) para que el reporte sea más útil con el tiempo, y agrupa el ruido
recurrente aunque todavía no se haya triageado, para que sea visible sin
tener que revisar cada alerta una por una.
"""
import logging
import threading
import time
from typing import Optional

from app.storage.db import EventDB
from app.vision.llm_text import complete_text

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Eres un analista de seguridad que redacta reportes breves y claros en "
    "español, a partir de datos ya agregados de un sistema de cámaras. "
    "Nunca inventes datos que no estén en el resumen que se te da. Sé "
    "concreto, usa viñetas cuando ayude a la lectura, y no repitas números "
    "que ya están en el resumen salvo para darles contexto."
)


def _build_prompt(agg: dict, alerts: list[dict], period_hours: float) -> str:
    totals = agg.get("totals", {})
    per_cam = agg.get("per_cam", [])

    important = [a for a in alerts if a.get("review_label") == "important"]
    noise     = [a for a in alerts if a.get("review_label") == "noise"]
    unreviewed = [a for a in alerts if not a.get("review_label")]

    lines = [
        f"Periodo: últimas {period_hours:.0f} horas.",
        f"Observaciones totales: {totals.get('events', 0)}.",
        f"Alertas totales: {totals.get('alerts', 0)} "
        f"({len(important)} marcadas importantes, {len(noise)} marcadas como "
        f"ruido, {len(unreviewed)} sin revisar por el usuario).",
        f"Máximo de personas detectadas simultáneas: {totals.get('max_people', 0)}.",
        "",
        "Por cámara (observaciones / alertas):",
    ]
    for c in per_cam[:10]:
        lines.append(f"- {c.get('cam_id')}: {c.get('n')} obs, {c.get('alerts') or 0} alertas")

    if important:
        lines.append("\nAlertas marcadas IMPORTANTES por el usuario:")
        for a in important[:20]:
            data = a.get("data") or {}
            lines.append(f"- [{a.get('cam_id')}] {data.get('activity', '')} "
                         f"| alertas: {data.get('alerts')}")

    if unreviewed:
        lines.append(f"\nAlertas sin revisar aún ({len(unreviewed)} en total, muestra):")
        for a in unreviewed[:20]:
            data = a.get("data") or {}
            lines.append(f"- [{a.get('cam_id')}] {data.get('activity', '')} "
                         f"| alertas: {data.get('alerts')}")

    if noise:
        lines.append(f"\nAlertas ya marcadas como ruido por el usuario ({len(noise)}):")
        for a in noise[:10]:
            data = a.get("data") or {}
            lines.append(f"- [{a.get('cam_id')}] alertas: {data.get('alerts')}")

    lines.append(
        "\nRedacta un reporte con: (1) un resumen general de la actividad del "
        "periodo, (2) las alertas importantes con su contexto si las hay, "
        "(3) un párrafo señalando patrones de ruido recurrente (agrupa "
        "alertas de texto similar, ej. iluminación/lens flare/personas no "
        "reconocidas) para que el usuario sepa qué categorías de alerta "
        "probablemente no necesitan revisión, y (4) cuántas alertas siguen "
        "sin revisar. No uses JSON, escribe texto natural."
    )
    return "\n".join(lines)


def generate_report(db: EventDB, analyzer, period_hours: float = 24.0,
                    kind: str = "daily") -> Optional[int]:
    """Genera un reporte y lo guarda. Devuelve el id del reporte, o None si
    no había nada que reportar (periodo sin observaciones)."""
    until = time.time()
    since = until - period_hours * 3600

    agg = db.aggregate_events(since=since, until=until)
    if not agg.get("totals", {}).get("events"):
        logger.info("report_generator: sin observaciones en el periodo, no se genera reporte")
        return None

    alerts = db.query_events(type="nemotron", has_alert=True, since=since, until=until, limit=200)
    prompt = _build_prompt(agg, alerts, period_hours)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    # El gateway falla de forma intermitente (401/503/504, ver CLAUDE.md) —
    # mismo criterio que el resto del pipeline: nunca dejar que una falla
    # transitoria del LLM tumbe la llamada, solo no generar el reporte esta
    # vez (el trigger manual ya reintenta una vez adentro de complete_text).
    try:
        # Timeout generoso a propósito: 900 tokens de texto libre tarda
        # bastante más que una respuesta corta de chat (confirmado: el chat
        # responde en <1s, pero un reporte de este largo agotó el timeout de
        # 90s dos veces seguidas con el gateway sano) — esto no bloquea
        # ninguna UI en vivo, corre en background o bajo demanda explícita.
        text = complete_text(analyzer, messages, max_tokens=900, temperature=0.4, timeout=60)
    except Exception as e:
        logger.warning("report_generator: fallo llamando al gateway: %s", e)
        text = ""
    if not text:
        logger.warning("report_generator: sin redacción del modelo, se guarda resumen de datos")
        body = prompt.split("\nRedacta un reporte")[0]
        text = "[Resumen automático de datos — el modelo no redactó este periodo]\n\n" + body

    report_id = db.insert_report(kind=kind, period_start=since, period_end=until, content=text)
    logger.info("report_generator: reporte #%d generado (%s, %.0fh, %d chars)",
               report_id, kind, period_hours, len(text))
    return report_id


def start_report_thread(db: EventDB, analyzer, interval_s: float = 86400.0,
                        period_hours: float = 24.0, kind: str = "daily",
                        enabled: bool = False) -> threading.Event:
    """Mismo esqueleto que start_retention_thread — thread daemon que corre
    generate_report() cada `interval_s`. Si `enabled` es False no arranca
    nada (kill-switch explícito, mismo patrón ya usado en esta sesión)."""
    stop = threading.Event()
    if not enabled:
        logger.info("report_generator: deshabilitado (REPORT_ENABLED=false)")
        return stop

    def _run():
        stop.wait(60)
        while not stop.is_set():
            try:
                generate_report(db, analyzer, period_hours=period_hours, kind=kind)
            except Exception as e:
                logger.warning("report_generator error: %s", e)
            stop.wait(interval_s)

    t = threading.Thread(target=_run, daemon=True, name="report-generator")
    t.start()
    logger.info("report_generator thread started — interval=%.0fs period=%.0fh kind=%s",
               interval_s, period_hours, kind)
    return stop
