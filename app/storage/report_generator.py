"""
Genera reportes periódicos en texto natural (español) usando el propio VLM
como "redactor" — parte del flywheel de triage de alertas: el prompt le dice
al modelo qué alertas ya se marcaron importante/ruido (si el usuario las
revisó) para que el reporte sea más útil con el tiempo, y agrupa el ruido
recurrente aunque todavía no se haya triageado, para que sea visible sin
tener que revisar cada alerta una por una.
"""
import logging
import os
import threading
import time
from typing import Optional

from app.storage.db import EventDB
from app.storage.facts import build_facts, render_facts
from app.vision.llm_text import complete_text

SKILLS_DIR = os.environ.get("SKILLS_DIR", "/app/config/skills")


def report_llm(analyzer):
    """Modelo del reporte: REPORT_MODEL (con REPORT_API_KEY o CHAT_API_KEY) o, si no, el del VLM."""
    import types
    model = os.environ.get("REPORT_MODEL")
    if not model:
        return analyzer
    key = os.environ.get("REPORT_API_KEY") or os.environ.get("CHAT_API_KEY") or getattr(analyzer, "api_key", None)
    return types.SimpleNamespace(endpoint=analyzer.endpoint, model=model, api_key=key)


def load_skill(name: str, default: str) -> str:
    try:
        with open(os.path.join(SKILLS_DIR, f"{name}.md"), encoding="utf-8") as f:
            return f.read().strip() or default
    except OSError:
        return default

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Eres un analista de seguridad que redacta reportes breves y claros en "
    "español, a partir de datos ya agregados de un sistema de cámaras. "
    "Nunca inventes datos que no estén en el resumen que se te da. Sé "
    "concreto, usa viñetas cuando ayude a la lectura, y no repitas números "
    "que ya están en el resumen salvo para darles contexto."
)


def generate_report(db: EventDB, analyzer, period_hours: float = 24.0,
                    kind: str = "daily") -> Optional[int]:
    """Genera un reporte y lo guarda. Devuelve el id del reporte, o None si
    no había nada que reportar (periodo sin observaciones)."""
    until = time.time()
    since = until - period_hours * 3600

    facts = build_facts(db, since, until)
    if not facts["totals"]["observaciones"]:
        logger.info("report_generator: sin observaciones en el periodo, no se genera reporte")
        return None
    data = render_facts(facts)
    messages = [
        {"role": "system", "content": load_skill("report", _SYSTEM_PROMPT)},
        {"role": "user", "content": data + "\n\nRedacta el reporte del periodo."},
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
        text = complete_text(report_llm(analyzer), messages, max_tokens=1600, temperature=0.2, timeout=120)
    except Exception as e:
        logger.warning("report_generator: fallo llamando al gateway: %s", e)
        text = ""
    if not text:
        logger.warning("report_generator: sin redacción del modelo, se guarda resumen de datos")
        body = data
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
        # sin esto, cada reinicio del contenedor generaba un reporte nuevo
        try:
            with db._lock:
                last = db._conn.execute("SELECT MAX(generated_at) FROM reports WHERE kind=?", (kind,)).fetchone()[0]
            if last and time.time() - last < interval_s:
                stop.wait(interval_s - (time.time() - last))
        except Exception as e:
            logger.warning("report_generator: no se pudo leer el ultimo reporte: %s", e)
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
