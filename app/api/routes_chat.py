"""
Chat endpoint que consulta VLM sobre lo que ven las cámaras.

- POST /api/chat
  Body: { "message": str, "history": [{role, content}] }
  Response: { "response": str, "context_cameras": int, "ms": int }

El sistema incluye en el system prompt el contexto en vivo de
todas las cámaras (último análisis VLM) para que el modelo
pueda responder con conocimiento de la escena actual.

VLM corre en DGX Spark con GPU — esta ruta solo orquesta
el request HTTP, no hace inferencia local.
"""
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import uuid
from typing import List, Optional

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────
# Detector: ¿la pregunta requiere histórico?
#
# Por default NO incluimos histórico — solo el estado actual va al prompt.
# Esto evita saturar VLM con eventos pasados cuando la pregunta es
# sobre "ahora". El histórico solo se inyecta cuando hay señales claras
# de que la pregunta es sobre el pasado o pide una agregación temporal.
# ─────────────────────────────────────────────────────────────────────────
_HISTORY_RE = re.compile(
    r"\b("
    # Adverbios temporales pasados
    r"hoy|ayer|anteayer|antes|anteriormente|previamente|"
    r"anoche|esta\s+(?:mañana|tarde|noche|madrugada)|"
    r"hace\s+\d+|hace\s+(?:un|una|unos|unas|dos|tres|cuatro|cinco|diez|veinte|"
    r"medi[oa]|much[oa]s?)\s+(?:segund|minut|hor|d[ií]a|semana|mes|año)|"
    # Tiempos verbales pasados (pretérito + imperfecto)
    r"pas[oó]|pasaron|pasaba|pasaban|"
    r"sucedi[oó]|sucedieron|ocurri[oó]|ocurrieron|"
    r"estuv[oe]|estuvieron|estaba|estaban|"
    r"hub[oe]|hab[ií]a|hab[ií]an|"
    r"vin[oe]|vinieron|ven[ií]a|ven[ií]an|"
    r"entr[oó]|entraron|sali[oó]|salieron|lleg[oó]|llegaron|"
    r"vio|vieron|ve[ií]a|ve[ií]an|detect[oó]|detectaron|registr[oó]|registraron|"
    # Perfecto compuesto: "ha/han + participio"
    r"ha[ns]?\s+(?:visto|habido|pasado|ocurrido|entrado|salido|llegado|estado|detectado|registrado)|"
    r"se\s+(?:vio|vieron|ve[ií]a|ve[ií]an|registr[oó]|registraron|detect[oó]|detectaron)|"
    # Preguntas sobre cuándo
    r"cu[aá]ndo|desde\s+cu[aá]ndo|hasta\s+cu[aá]ndo|cu[aá]nto\s+tiempo|"
    r"[uú]ltim[oa]\s+(?:vez|momento|hora|d[ií]a)|"
    # Agregaciones / históricos
    r"resumen|resumir|historial|hist[oó]ric|registro|reporte|"
    r"cu[aá]nt[ao]s?\s+veces|"
    r"tendencia|evoluci[oó]n|patr[oó]n|pico\s+de|"
    # Disparadores explícitos
    r"qu[eé]\s+pas[oó]|qu[eé]\s+ha\s+pasado|qu[eé]\s+ocurri[oó]"
    r")\b",
    re.IGNORECASE,
)


_PEOPLE_RE = re.compile(
    r"\b(personas?|gente|qui[eé]n(es)?|visitas?|visitante|identidad|sujetos?|nombre|"
    r"conocid[oa]s?|desconocid[oa]s?|recurrente|lleg[oó]|entr[oó]|sali[oó])\b",
    re.IGNORECASE,
)


def _question_needs_history(question: str) -> bool:
    """True si la pregunta hace referencia al pasado, pide agregación o pregunta por personas."""
    q = question or ""
    return bool(_HISTORY_RE.search(q) or _PEOPLE_RE.search(q))


def compose_chat_system(skill: str, total_cams: int, context_block: str,
                        facts_text: str = "", facts_label: str = "",
                        history_block: str = "", hist_kind: str = "",
                        images: Optional[list] = None) -> str:
    parts = [skill, f"Cámaras en el sistema: {total_cams}.",
             "Capacidades: TÚ entregas las capturas de las alertas. Cuando el usuario las pide, el sistema las "
             "adjunta a TU respuesta y se ven debajo de ella. Nunca digas que no tienes acceso a imágenes ni "
             "que el usuario debe buscarlas en otro sistema.",
             "## Estado actual por cámara:", context_block]
    if images is not None:
        if images:
            lst = "\n".join(f"- {i['hora']} · {i['cam']} · {i['texto']}"
                            + (f" · personas conocidas presentes: {', '.join(i['personas'])}" if i.get("personas") else "")
                            for i in images)
            parts += [f"## Capturas que adjuntas en esta respuesta ({len(images)}). Di 'Aquí están las capturas' "
                      "y resume cámara, hora y quién aparece si se sabe:", lst]
        else:
            parts += ["## Capturas adjuntas a esta respuesta", "(no hay alertas con captura en el periodo pedido)"]
    if facts_text:
        parts += [f"## Hechos exactos del periodo ({facts_label})", facts_text]
    if history_block:
        parts += [f"## {hist_kind} ({facts_label})", history_block]
    if not facts_text and not history_block:
        parts.append("(La pregunta es sobre el presente: no se cargó histórico.)")
    return "\n\n".join(parts)


_DEFAULT_CHAT_SKILL = (
    "Eres el asistente de THOR Vision, un sistema de vigilancia con cámaras IP. "
    "Responde en español, conciso y profesional, solo con los datos provistos; no inventes."
)


class ChatMessage(BaseModel):
    role: str        # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []


def _format_relative_time(seconds_ago: float) -> str:
    """1h23min, 45min, 12s — texto humano de cuánto hace."""
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s"
    if seconds_ago < 3600:
        return f"{int(seconds_ago // 60)}min"
    h = int(seconds_ago // 3600)
    m = int((seconds_ago % 3600) // 60)
    return f"{h}h{m:02d}min" if m else f"{h}h"


# ─────────────────────────────────────────────────────────────────────────
# Ventana del histórico
#
# Antes esto estaba fijo en 6h, pero además `query_events(limit=400)` ordena
# por ts DESC: con el ritmo real (~8.5k eventos/día) esas 400 filas cubrían
# ~1 hora, mientras el prompt afirmaba "últimas 6 horas". Ahora la ventana se
# infiere de la pregunta y el filtrado se resuelve en SQL.
# ─────────────────────────────────────────────────────────────────────────

_HISTORY_DEFAULT_HOURS = float(os.environ.get("CHAT_HISTORY_HOURS", "6"))
_HISTORY_MAX_HOURS     = 720.0    # 30 días — tope de retención práctico

# Arriba de esta ventana se cambia a resumen agregado: listar evento por
# evento no cabe en el contexto del modelo (ya provocó un
# context_length_exceeded de 186k tokens contra un límite de 128k).
_AGGREGATE_THRESHOLD_HOURS = 12.0

# ~3 chars/token es conservador para español con tokenizador Qwen, así que
# 18k chars ≈ 6k tokens. Es el cinturón de seguridad, no el caso normal.
_HISTORY_MAX_CHARS = 18_000

# Los turnos previos tampoco tenían tope: 10 respuestas largas del modelo
# solas podían comerse el contexto.
_CHAT_HISTORY_MAX_CHARS = 12_000

_RANGE_RES = [
    (re.compile(r"\b[uú]ltim[oa]s?\s+(\d{1,3})\s*(?:h|hr|hrs|horas?)\b", re.I),
     lambda m: float(m.group(1))),
    (re.compile(r"\bhace\s+(\d{1,3})\s*(?:h|hr|hrs|horas?)\b", re.I),
     lambda m: float(m.group(1))),
    (re.compile(r"\b[uú]ltim[oa]s?\s+(\d{1,2})\s*d[ií]as?\b", re.I),
     lambda m: float(m.group(1)) * 24),
    (re.compile(r"\bhace\s+(\d{1,2})\s*d[ií]as?\b", re.I),
     lambda m: float(m.group(1)) * 24),
    (re.compile(r"\b[uú]ltim[oa]s?\s+(\d{1,2})\s*semanas?\b", re.I),
     lambda m: float(m.group(1)) * 168),
    (re.compile(r"\b[uú]ltim[oa]s?\s+(\d{1,3})\s*(?:min|mins|minutos?)\b", re.I),
     lambda m: float(m.group(1)) / 60),
    (re.compile(r"\b(?:anteayer|antier)\b", re.I),                 lambda m: 72.0),
    (re.compile(r"\bayer\b", re.I),                                 lambda m: 48.0),
    (re.compile(r"\b(?:esta|[uú]ltima)\s+semana\b|\bsemanal\b|\b7\s*d[ií]as\b", re.I),
     lambda m: 168.0),
    (re.compile(r"\b(?:este|[uú]ltimo)\s+mes\b|\bmensual\b|\b30\s*d[ií]as\b", re.I),
     lambda m: 720.0),
    (re.compile(r"\b(?:hoy|[uú]ltimo\s+d[ií]a|24\s*h(?:oras)?|del\s+d[ií]a|diario)\b", re.I),
     lambda m: 24.0),
    (re.compile(r"\banoche\b|\bmadrugada\b", re.I),               lambda m: 14.0),
    (re.compile(r"\besta\s+ma[nñ]ana\b", re.I),                    lambda m: 10.0),
    (re.compile(r"\b(?:esta\s+tarde|hace\s+un\s+rato|recientemente|[uú]ltimas\s+horas)\b", re.I),
     lambda m: 6.0),
    (re.compile(r"\b(?:ahorita|justo\s+ahora|en\s+este\s+momento)\b", re.I),
     lambda m: 1.0),
]


_IMG_RE = re.compile(r"\b(captur\w*|imag\w*|fotos?|snapshots?|mu[eé]str\w*|ens[eé][nñ]\w*|ver\s+(?:la|las|el|los)\s+alertas?)\b", re.I)


def _explicit_hours(question: str) -> Optional[float]:
    for rx, fn in _RANGE_RES:
        m = rx.search(question or "")
        if m:
            return fn(m)
    return None


def _norm_txt(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _alert_images(db, config, question: str, history: list, limit: int = 8) -> list:
    """Capturas de las alertas del periodo y camaras a las que se refiere la pregunta (o el turno anterior)."""
    prev_user = [m.content for m in history if m.role == "user"]
    hours = _explicit_hours(question)
    ref = question
    if hours is None:
        for q in reversed(prev_user):
            hours = _explicit_hours(q)
            if hours is not None:
                ref = q
                break
    hours = hours or 1.0
    names = {c.id: c.name for c in config.cameras}
    nq = _norm_txt(question + " " + ref)
    cams = [cid for cid, nm in names.items() if _norm_txt(nm) in nq]
    until = time.time(); since = until - hours * 3600
    sql = ("SELECT s.id, e.ts, e.cam_id, e.data FROM events e JOIN snapshots s ON s.event_id = e.id "
           "WHERE e.type='nemotron' AND e.has_alert=1 AND e.ts>=? AND e.ts<=?")
    args = [since, until]
    if cams:
        sql += " AND e.cam_id IN (%s)" % ",".join("?" * len(cams)); args += cams
    sql += " ORDER BY e.ts DESC LIMIT ?"; args.append(limit)
    with db._lock:
        rows = db._conn.execute(sql, args).fetchall()
    out = []
    known = _known_people(db, rows)
    for sid, ts, cam, data in rows:
        try:
            act = (json.loads(data or "{}").get("activity") or "")[:140]
        except Exception:
            act = ""
        out.append({"url": f"/api/snapshots/file/{sid}", "cam": names.get(cam, cam),
                    "hora": time.strftime("%H:%M", time.localtime(ts)), "texto": act,
                    "personas": known.get((cam, ts), [])})
    return out


def _known_people(db, rows, pad_s: float = 60.0) -> dict:
    """Personas con nombre cuya visita en esa camara cubre la hora de la alerta."""
    out = {}
    with db._lock:
        for _sid, ts, cam, _data in rows:
            names = db._conn.execute(
                "SELECT DISTINCT s.name FROM person_visits v JOIN subjects s ON s.id = v.subject_id "
                "WHERE v.cam_id=? AND s.named=1 AND v.fp=0 AND v.start_ts<=? AND v.end_ts>=?",
                (cam, ts + pad_s, ts - pad_s)).fetchall()
            out[(cam, ts)] = sorted({(n or "").strip() for (n,) in names if n})
    return out


def _infer_history_hours(question: str) -> float:
    """
    Deduce la ventana pedida. Gana la primera coincidencia: los patrones
    numéricos van antes que los nombrados para que "últimas 3 horas" no caiga
    en la regla genérica de "últimas horas".
    """
    q = question or ""
    for rx, fn in _RANGE_RES:
        m = rx.search(q)
        if not m:
            continue
        try:
            return max(0.25, min(fn(m), _HISTORY_MAX_HOURS))
        except (TypeError, ValueError):
            continue
    return _HISTORY_DEFAULT_HOURS


def _human_window(hours: float) -> str:
    """Etiqueta en español para meter en el prompt; evita el '6 horas' fijo."""
    if hours <= 1.0:
        return "última hora"
    if hours < 24:
        return f"últimas {int(round(hours))} horas"
    days = hours / 24.0
    if days <= 1.0:
        return "último día"
    if 6.5 <= days <= 7.5:
        return "última semana"
    if 29 <= days <= 31:
        return "último mes"
    return f"últimos {int(round(days))} días"


def _build_timeline_block(db, config, since: float, until: float,
                          hours: float, max_events: int) -> tuple[str, int]:
    """Nivel A: timeline evento-por-evento para ventanas cortas."""
    rows = db.query_significant_events(since=since, until=until, limit=max_events)
    if not rows:
        return (f"Sin cambios significativos en {_human_window(hours)} "
                f"(escenas estáticas)."), 0

    cam_names   = {c.id: c.name for c in config.cameras}
    now         = time.time()
    alert_count = sum(1 for r in rows if r["has_alert"])

    lines = []
    for ev in reversed(rows):          # cronológico, más viejo primero
        ago    = _format_relative_time(now - ev["ts"])
        name   = cam_names.get(ev["cam_id"], ev["cam_id"])
        prefix = "[ALERTA] " if ev["has_alert"] else ""
        desc   = (ev["activity"] or "")[:70] or "(sin descripción)"
        line   = f"- hace {ago} · {name} · {ev['people']}p · {desc}"
        if ev["has_alert"] and ev["alerts"]:
            line += "  ALERTAS: " + ", ".join(str(a) for a in ev["alerts"][:2])
        lines.append(prefix + line)

    header = f"({len(rows)} eventos relevantes"
    if alert_count:
        header += f", {alert_count} con alerta"
    header += "):"
    return header + "\n" + "\n".join(lines), len(rows)


def _build_aggregate_block(db, config, since: float, until: float,
                           hours: float) -> tuple[str, int]:
    """Nivel B: rollup agregado, tamaño acotado sin importar el rango."""
    agg       = db.aggregate_events(since=since, until=until)
    cam_names = {c.id: c.name for c in config.cameras}
    tot, win  = agg["totals"], agg["window"]
    now       = time.time()

    lines = [f"({tot['events']} observaciones, {tot['alerts']} con alerta, "
             f"máx {tot['max_people']} persona(s) simultáneas):"]

    first_ts = tot.get("data_first_ts")
    if first_ts and first_ts > since:
        lines.append(f"NOTA: solo hay datos desde hace "
                     f"{_format_relative_time(now - first_ts)} (retención); "
                     f"el rango pedido excede lo disponible.")

    lines.append("\nPor cámara:")
    for c in agg["per_cam"]:
        name  = cam_names.get(c["cam_id"], c["cam_id"])
        parts = [f"{c['n']} obs"]
        if c["n_with_people"]:
            parts.append(f"{c['n_with_people']} con gente (máx {c['max_people']})")
        else:
            parts.append("sin gente")
        if c["alerts"]:
            parts.append(f"{c['alerts']} ALERTA(S)")
        lines.append(f"- {name}: " + ", ".join(parts))

    con_gente = [b for b in agg["buckets"] if (b["sum_people"] or 0) > 0]
    if con_gente:
        por_hora = win["bucket"] == "hour"
        con_gente.sort(key=lambda b: b["sum_people"], reverse=True)
        fmt = "%d/%m %Hh" if por_hora else "%d/%m"
        lines.append(f"\nFranjas con más presencia ("
                     f"{'por hora' if por_hora else 'por día'}):")
        for b in con_gente[:8]:
            inicio = b["b"] * win["bucket_width_s"] - win["tz_offset_s"]
            etiqueta = time.strftime(fmt, time.localtime(inicio))
            extra = f", {b['alerts']} alerta(s)" if b["alerts"] else ""
            lines.append(f"- {etiqueta}: máx {b['max_people']}p, "
                         f"{b['n']} obs{extra}")

    if agg["alerts"]:
        lines.append("\nAlertas registradas:")
        for a in agg["alerts"][:12]:
            name = cam_names.get(a["cam_id"], a["cam_id"])
            txt  = ", ".join(str(x) for x in a["alerts"][:2]) or "(sin texto)"
            lines.append(f"- hace {_format_relative_time(now - a['ts'])} · "
                         f"{name} · {a['people']}p · {txt}")

    if agg["peaks"]:
        top  = agg["peaks"][0]
        name = cam_names.get(top["cam_id"], top["cam_id"])
        lines.append(f"\nPico máximo: {top['people']} persona(s) en {name}, "
                     f"hace {_format_relative_time(now - top['ts'])}.")

    return "\n".join(lines), tot["events"]


def _build_recent_history(db, config, hours: float = _HISTORY_DEFAULT_HOURS,
                          max_events: int = 60) -> tuple[str, int]:
    """
    Devuelve (texto, n_eventos). Dos niveles según la ventana: detalle para lo
    reciente, agregado para rangos largos.
    """
    if db is None:
        return "(persistencia no disponible)", 0

    now   = time.time()
    since = now - hours * 3600

    try:
        if hours > _AGGREGATE_THRESHOLD_HOURS:
            text, n = _build_aggregate_block(db, config, since, now, hours)
        else:
            text, n = _build_timeline_block(db, config, since, now,
                                            hours, max_events)
    except Exception as e:
        logger.warning("Histórico: fallo construyendo bloque (%.1fh): %s", hours, e)
        return "(error consultando histórico)", 0

    if len(text) > _HISTORY_MAX_CHARS:
        kept, total = [], 0
        for line in text.split("\n"):
            if total + len(line) + 1 > _HISTORY_MAX_CHARS:
                break
            kept.append(line)
            total += len(line) + 1
        logger.info("Histórico recortado: %d → %d chars (ventana %.1fh)",
                    len(text), total, hours)
        text = "\n".join(kept) + "\n(… recortado por tamaño)"

    return text, n


def _build_camera_context(store, config) -> tuple[str, int]:
    """Construye el contexto en vivo de todas las cámaras."""
    if store is None:
        return "Sin sistema de análisis disponible.", 0

    cam_names = {c.id: c.name for c in config.cameras}
    lines = []
    now = time.time()

    for cam_id, det in store.get_all().items():
        if cam_id not in cam_names:
            continue
        nem = det.nemotron
        if not nem or nem.get("activity") == "error":
            continue
        name     = cam_names.get(cam_id, cam_id)
        people   = nem.get("people", 0)
        activity = (nem.get("activity") or "").strip()
        scene    = (nem.get("scene") or "").strip()
        alerts   = [a for a in (nem.get("alerts") or [])
                    if str(a).strip() not in
                    {"", "error", "empty response", "bad json", "unreachable"}]
        age_s = int(now - nem.get("_ts", now))

        parts = [f"{name}: {people} persona(s)"]
        if activity: parts.append(f"actividad: {activity}")
        if scene and scene != activity: parts.append(f"escena: {scene}")
        if alerts: parts.append(f"ALERTAS: {', '.join(alerts)}")
        parts.append(f"hace {age_s}s")
        lines.append("- " + " | ".join(parts))

    if not lines:
        return "Sin observaciones recientes de las cámaras.", 0

    return "\n".join(lines), len(lines)


@router.post("/api/chat")
def chat(req: ChatRequest, request: Request,
         x_session_id: Optional[str] = Header(default=None)):
    t0 = time.monotonic()

    analyzer = getattr(request.app.state, "vlm_analyzer", None)
    if analyzer is None:
        return {"response": "VLM no está configurado en el servidor.",
                "error": True, "ms": 0}

    store  = getattr(request.app.state, "detection_store", None)
    config = request.app.state.config
    db     = getattr(request.app.state, "db", None)

    # Asignar session_id si no vino
    session_id = (x_session_id or "").strip() or str(uuid.uuid4())

    history = list(req.history or [])
    if not history and db is not None and x_session_id:
        try:
            history = [ChatMessage(role=m["role"], content=m["content"])
                       for m in db.query_chat_history(session_id, limit=20)
                       if m.get("role") in ("user", "assistant") and m.get("content")]
        except Exception as e:
            logger.warning("Chat: no se pudo cargar la memoria de la sesion: %s", e)

    # turnos viejos donde el asistente negaba poder enviar imagenes contaminan las respuestas nuevas
    history = [m for m in history if not (m.role == "assistant" and re.search(
        r"no (tengo|puedo).{0,40}(acceso|enviar|recuperar).{0,40}(im[aá]gen|captur)", m.content or "", re.I))]

    images = None
    if db is not None and _IMG_RE.search(req.message or ""):
        try:
            images = _alert_images(db, config, req.message, history)
        except Exception as e:
            logger.warning("Chat: fallo buscando capturas: %s", e)
            images = []

    context_block, n_ctx = _build_camera_context(store, config)
    total_cams = len(config.cameras)

    # Solo cargamos el histórico si la pregunta lo necesita
    # — esto reduce el contexto enviado a VLM y baja la carga GPU.
    needs_history = _question_needs_history(req.message)
    if needs_history:
        hist_hours = _infer_history_hours(req.message)
        history_block, n_hist = _build_recent_history(db, config, hours=hist_hours)
        hist_label = _human_window(hist_hours)
        hist_kind  = ("Resumen estadístico agregado (no es una lista exhaustiva)"
                      if hist_hours > _AGGREGATE_THRESHOLD_HOURS
                      else "Histórico de eventos significativos")
    else:
        hist_hours    = 0.0
        history_block = ""
        hist_label    = ""
        hist_kind     = ""
        n_hist        = 0

    facts_text = ""
    if needs_history and db is not None:
        try:
            from app.storage.facts import build_facts, render_facts
            now_ts = time.time()
            facts_text = render_facts(build_facts(db, now_ts - hist_hours * 3600, now_ts))
        except Exception as e:
            logger.warning("Chat: fallo construyendo hechos: %s", e)
        if hist_hours > _AGGREGATE_THRESHOLD_HOURS and facts_text:
            history_block = ""  # el rollup agregado repite lo que ya dan los hechos

    from app.storage.report_generator import load_skill
    system_prompt = compose_chat_system(
        load_skill("chat", _DEFAULT_CHAT_SKILL), total_cams, context_block,
        facts_text=facts_text, facts_label=hist_label,
        history_block=history_block, hist_kind=hist_kind, images=images)

    # Construir mensajes — system + últimos turnos (acotados) + nuevo.
    # Se recorre del más nuevo al más viejo insertando en la posición 1, así
    # que al agotarse el presupuesto se descartan los turnos más antiguos.
    messages = [{"role": "system", "content": system_prompt}]
    hist_chars = 0
    for m in reversed(history[-10:]):
        if m.role not in ("user", "assistant") or not m.content:
            continue
        if hist_chars + len(m.content) > _CHAT_HISTORY_MAX_CHARS:
            break
        messages.insert(1, {"role": m.role, "content": m.content})
        hist_chars += len(m.content)
    messages.append({"role": "user", "content": req.message})

    payload = json.dumps({
        "model":                os.environ.get("CHAT_MODEL") or analyzer.model,
        "max_tokens":           600,
        "temperature":          0.3,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages":             messages,
    }).encode()

    # El gateway hoy acepta requests sin auth, pero sin la key no puede
    # atribuir el consumo a este proyecto. Se reusa la del analyzer.
    chat_headers = {"Content-Type": "application/json"}
    chat_key = os.environ.get("CHAT_API_KEY") or getattr(analyzer, "api_key", None)
    if chat_key:
        chat_headers["Authorization"] = f"Bearer {chat_key}"

    http_req = urllib.request.Request(
        analyzer.endpoint,
        data    = payload,
        headers = chat_headers,
        method  = "POST",
    )

    # Un único reintento corto para los 503/504 intermitentes del gateway.
    # No más: hay un humano esperando, y fallar rápido es mejor que
    # encadenar timeouts de 90s.
    try:
        try:
            with urllib.request.urlopen(http_req, timeout=90) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e_first:
            if e_first.code not in (429, 502, 503, 504):
                raise
            logger.info("Chat: HTTP %s del gateway, reintento único en 2s",
                        e_first.code)
            time.sleep(2)
            with urllib.request.urlopen(http_req, timeout=90) as resp:
                data = json.loads(resp.read())

        request.app.state.chat_model_real = data.get("model")
        choice = data["choices"][0]["message"]
        text = (choice.get("content") or "").strip()

        if not text:
            # Fallback al reasoning si el modelo no produce content
            reasoning = (choice.get("reasoning") or "").strip()
            # Tomar las primeras 2-3 líneas significativas
            if reasoning:
                text = reasoning.split("\n\n")[-1].strip()

        if not text:
            text = "(sin respuesta del modelo)"

        ms = round((time.monotonic() - t0) * 1000)
        logger.info(
            "Chat OK %dms ctx=%d cams hist=%s(%d, %.1fh) | session=%s Q=%s",
            ms, n_ctx,
            "yes" if needs_history else "no",
            n_hist, hist_hours,
            session_id[:8],
            req.message[:60].replace("\n", " "),
        )

        # Persistir conversación (user + assistant) si hay DB
        if db is not None:
            try:
                db.insert_chat(session_id, "user",      req.message,
                               context_cams=n_ctx)
                db.insert_chat(session_id, "assistant", text,
                               context_cams=n_ctx, ms=ms)
            except Exception as e:
                logger.warning("Chat DB insert error: %s", e)

        return {
            "response":         text,
            "images":           images or [],
            "context_cameras":  n_ctx,
            "history_events":   n_hist,
            "history_hours":    hist_hours,
            "used_history":     needs_history,
            "ms":               ms,
            "session_id":       session_id,
        }

    except urllib.error.URLError as e:
        logger.warning("Chat unreachable: %s", e)
        return {"response": f"No se puede contactar a VLM: {e}",
                "error": True, "session_id": session_id,
                "ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        logger.warning("Chat error: %s", e)
        return {"response": f"Error procesando la pregunta: {e}",
                "error": True, "session_id": session_id,
                "ms": round((time.monotonic() - t0) * 1000)}
