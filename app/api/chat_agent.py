"""Vision Agent: chat en streaming al estilo Morphic (proceso, evidencia citada, capturas, preguntas relacionadas)."""
import collections
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api import routes_chat as rc

logger = logging.getLogger(__name__)
router = APIRouter()

_RELATED_RE = re.compile(r"```related\s*(\[.*?\])\s*```", re.S)
_ALERT_Q_RE = re.compile(r"\balert\w*|incident\w*|evento\w*|pas[oó]\b|ocurri", re.I)
_REL_TIME_RE = re.compile(r"\bhace\s+\d+\s*(?:s|seg\w*|min\w*|h|horas?)\b", re.I)
_DENY_IMG_RE = re.compile(r"no (tengo|puedo).{0,40}(acceso|enviar|recuperar).{0,40}(im[aá]gen|captur)", re.I)
_EVIDENCE_MAX = 12

# Contrato de formato de la interfaz (no es el skill entrenable de config/skills/chat.md).
AGENT_RULES = """## Formato de respuesta (obligatorio)
- Empieza directo con la respuesta. Sin saludos, sin repetir la pregunta, sin despedidas.
- Breve: unas 150 palabras salvo que pidan detalle. Viñetas para listas; **negritas** solo para cifras o nombres clave.
- Encabezados (###) solo si la respuesta tiene 3 partes o más.
- Cita la evidencia: al final de cada oración que use un evento agrega su etiqueta después del punto, por ejemplo: "Hubo 14 alertas nocturnas en Sala de Juntas. [E1]". Varias juntas: [E1][E3]. Usa solo etiquetas que existan.
- Horas: usa la hora local HH:MM de la evidencia o de los hechos. Los turnos anteriores pueden traer tiempos relativos viejos: nunca los copies, recalcula con los datos actuales.
- "Visitas" no son personas distintas; personas distintas = sujetos identificados por rostro.
- El usuario puede elegir un rango exacto en la línea de tiempo del panel; si los hechos dicen "PERIODO: X a Y", responde solo sobre ese periodo y menciónalo.

## Preguntas relacionadas
Después de tu conclusión genera exactamente 3 preguntas de seguimiento en un bloque:
```related
["pregunta 1", "pregunta 2", "pregunta 3"]
```
- Una para profundizar en el punto más importante, una para actuar o decidir, y una para ampliar o comparar.
- Ancladas a datos concretos de ESTA respuesta (cámaras, horas, personas, tipos de alerta). Máximo 12 palabras cada una, en español.
- OMITE el bloque en saludos o agradecimientos, respuestas de un solo dato, cuando no pudiste responder y cuando serían genéricas.
"""


class StreamRequest(BaseModel):
    message: str
    history: list = []
    since: Optional[float] = None    # rango exacto elegido en la linea de tiempo
    until: Optional[float] = None
    cam_id: Optional[str] = None


_CLOCK_RANGE_RE = re.compile(r"\b(?:entre(?: las?)?|de(?: las?)?|desde(?: las?)?)\s*(\d{1,2})(?::(\d{2}))?\s*(?:h|hrs)?\s*"
                             r"(?:y(?: las?)?|a(?: las?)?|hasta(?: las?)?|-)\s*(\d{1,2})(?::(\d{2}))?", re.I)


def _clock_range(msg: str, now: float) -> Optional[tuple]:
    """'entre 21:30 y 22:15' -> (since, until) de hoy; si el rango aun no ocurre hoy, es de ayer."""
    m = _CLOCK_RANGE_RE.search(msg or "")
    if not m:
        return None
    h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    lt = time.localtime(now)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    a, b = day0 + h1 * 3600 + m1 * 60, day0 + h2 * 3600 + m2 * 60
    if b <= a:
        b += 86400
    if a > now:
        a, b = a - 86400, b - 86400
    return a, min(b, now)


def _range_label(since: float, until: float) -> str:
    fmt = "%d-%b %H:%M"
    same_day = time.strftime("%Y%m%d", time.localtime(since)) == time.strftime("%Y%m%d", time.localtime(until))
    return f"{time.strftime(fmt, time.localtime(since))} a {time.strftime('%H:%M' if same_day else fmt, time.localtime(until))}"


class FeedbackRequest(BaseModel):
    session_id: str
    label: str
    question: str = ""
    answer: str = ""
    comment: str = ""


# ── salud del modelo del chat ─────────────────────────────────────────────────

def _health(app) -> dict:
    h = getattr(app.state, "chat_health", None)
    if h is None:
        h = {"last_ok_ts": 0.0, "last_ms": None, "last_error": None, "last_error_ts": 0.0,
             "ok": collections.deque(maxlen=500), "err": collections.deque(maxlen=500), "last_call_ts": 0.0}
        app.state.chat_health = h
    return h


def _mark(app, ok: bool, ms: Optional[int] = None, error: Optional[str] = None) -> None:
    h = _health(app)
    now = time.time()
    h["last_call_ts"] = now
    if ok:
        h["last_ok_ts"], h["last_ms"] = now, ms
        h["ok"].append(now)
    else:
        h["last_error"], h["last_error_ts"] = (error or "error")[:160], now
        h["err"].append(now)


def chat_health_summary(app) -> dict:
    h = _health(app)
    now = time.time()
    ok_1h = sum(1 for t in h["ok"] if now - t < 3600)
    err_1h = sum(1 for t in h["err"] if now - t < 3600)
    if h["last_ok_ts"] and now - h["last_ok_ts"] < 900 and h["last_ok_ts"] >= h["last_error_ts"]:
        status = "degraded" if err_1h > ok_1h else "ok"
    elif h["last_error_ts"] and h["last_error_ts"] > h["last_ok_ts"]:
        status = "down"
    else:
        status = "unknown"
    return {"status": status, "model": _chat_cfg(app)[1], "model_real": getattr(app.state, "chat_model_real", None),
            "last_ms": h["last_ms"], "last_ok_ts": h["last_ok_ts"], "last_error": h["last_error"],
            "last_error_ts": h["last_error_ts"], "ok_1h": ok_1h, "err_1h": err_1h}


def _chat_cfg(app) -> tuple:
    an = getattr(app.state, "vlm_analyzer", None)
    endpoint = getattr(an, "endpoint", "") if an else ""
    model = os.environ.get("CHAT_MODEL") or (getattr(an, "model", "") if an else "")
    key = os.environ.get("CHAT_API_KEY") or (getattr(an, "api_key", None) if an else None)
    return endpoint, model, key


def _request(endpoint: str, key: Optional[str], payload: dict, timeout: int):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.urlopen(urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                                         headers=headers, method="POST"), timeout=timeout)


def start_chat_probe(app, interval_s: float = 300.0) -> threading.Event:
    """Sondeo ligero del modelo del chat cuando nadie lo usa, para que el panel no mienta."""
    stop = threading.Event()

    def _run():
        stop.wait(45)
        while not stop.is_set():
            h = _health(app)
            if time.time() - h["last_call_ts"] > interval_s:
                endpoint, model, key = _chat_cfg(app)
                if endpoint and model:
                    t0 = time.monotonic()
                    try:
                        with _request(endpoint, key, {"model": model, "max_tokens": 4, "temperature": 0,
                                                      "chat_template_kwargs": {"enable_thinking": False},
                                                      "messages": [{"role": "user", "content": "ok"}]}, 30) as r:
                            d = json.loads(r.read())
                        app.state.chat_model_real = d.get("model") or getattr(app.state, "chat_model_real", None)
                        _mark(app, True, round((time.monotonic() - t0) * 1000))
                    except Exception as e:
                        _mark(app, False, error=f"sondeo: {e}")
            stop.wait(60)

    threading.Thread(target=_run, daemon=True, name="chat-probe").start()
    return stop


# ── contexto ────────────────────────────────────────────────────────────────

def _clean_history(history: list) -> list:
    """Quita turnos sin respuesta, negaciones de imagenes y tiempos relativos viejos; acota el largo."""
    out = []
    for m in history:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = (m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) or ""
        if role not in ("user", "assistant") or not content.strip():
            continue
        if role == "assistant" and _DENY_IMG_RE.search(content):
            continue
        if role == "user" and out and out[-1]["role"] == "user":
            out[-1] = {"role": "user", "content": content}
            continue
        if role == "assistant":
            content = _RELATED_RE.sub("", content)
            content = _REL_TIME_RE.sub("", content)[:700]
        out.append({"role": role, "content": content})
    while out and out[-1]["role"] == "user":
        out.pop()
    return out[-10:]


def _evidence(db, names: dict, since: float, until: float, cams: Optional[list] = None) -> list:
    """Alertas agrupadas por camara y tipo (huecos <= 10 min), las mas recientes primero, etiquetadas E1..En."""
    sql = ("SELECT e.id, e.ts, e.cam_id, e.people, e.data, "
           "(SELECT s.id FROM snapshots s WHERE s.event_id = e.id ORDER BY s.id DESC LIMIT 1) "
           "FROM events e WHERE e.type='nemotron' AND e.has_alert=1 AND e.ts>=? AND e.ts<=?")
    args = [since, until]
    if cams:
        sql += " AND e.cam_id IN (%s)" % ",".join("?" * len(cams))
        args += cams
    sql += " ORDER BY e.ts ASC LIMIT 4000"
    with db._lock:
        rows = db._conn.execute(sql, args).fetchall()
    groups, open_by_key = [], {}
    for eid, ts, cam, people, data, sid in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            d = {}
        tipo = (d.get("alert_types") or ["sin_tipo"])[0]
        key = (cam, tipo)
        g = open_by_key.get(key)
        if g and ts - g["hasta_ts"] <= 600:
            g["n"] += 1
            g["hasta_ts"] = ts
            g["max_personas"] = max(g["max_personas"], people or 0)
            g["texto"] = (d.get("activity") or g["texto"])[:160]
            if sid:
                g["snapshot_id"], g["snap_ts"], g["event_id"] = sid, ts, eid
        else:
            g = {"cam_id": cam, "cam": names.get(cam, cam), "tipo": tipo, "n": 1, "desde_ts": ts, "hasta_ts": ts,
                 "max_personas": people or 0, "texto": (d.get("activity") or "")[:160],
                 "snapshot_id": sid, "snap_ts": ts, "event_id": eid}
            groups.append(g)
            open_by_key[key] = g
    groups.sort(key=lambda g: g["hasta_ts"], reverse=True)
    groups = groups[:_EVIDENCE_MAX]
    known = rc._known_people(db, [(g["snapshot_id"], g["snap_ts"], g["cam_id"], None) for g in groups])
    for i, g in enumerate(groups, 1):
        g["ref"] = f"E{i}"
        g["desde"] = time.strftime("%H:%M", time.localtime(g["desde_ts"]))
        g["hasta"] = time.strftime("%H:%M", time.localtime(g["hasta_ts"]))
        g["personas"] = known.get((g["cam_id"], g["snap_ts"]), [])
        g["url"] = f"/api/snapshots/file/{g['snapshot_id']}" if g["snapshot_id"] else None
    return groups


def _render_evidence(groups: list) -> str:
    if not groups:
        return "(sin alertas en el periodo)"
    lines = []
    for g in groups:
        rango = g["desde"] if g["desde"] == g["hasta"] else f"{g['desde']}-{g['hasta']}"
        extra = f" · conocidos: {', '.join(g['personas'])}" if g["personas"] else ""
        lines.append(f"- [{g['ref']}] {rango} · {g['cam']} · {g['tipo']} x{g['n']} · max {g['max_personas']} personas{extra} · {g['texto']}")
    return "\n".join(lines)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _prepare(req: StreamRequest, request: Request, session_id: str) -> dict:
    app = request.app
    db = getattr(app.state, "db", None)
    store = getattr(app.state, "detection_store", None)
    config = app.state.config
    names = {c.id: c.name for c in config.cameras}
    msg = req.message.strip()

    history = req.history or []
    if not history and db is not None:
        try:
            history = db.query_chat_history(session_id, limit=20)
        except Exception as e:
            logger.warning("Vision Agent: no se pudo cargar la memoria: %s", e)
    history = _clean_history(history)

    prev_user = [m["content"] for m in history if m["role"] == "user"]
    hours = rc._explicit_hours(msg)
    if hours is None and re.search(r"\b(esas?|esos?|eso|ah[ií]|anterior)\b", msg, re.I):
        for q in reversed(prev_user):
            hours = rc._explicit_hours(q)
            if hours is not None:
                break
    now = time.time()
    window = None
    if req.since and req.until and req.until > req.since:
        window = (float(req.since), min(float(req.until), now))
    else:
        window = _clock_range(msg, now)
    needs = (rc._question_needs_history(msg) or bool(rc._IMG_RE.search(msg)) or hours is not None
             or window is not None)
    hours = (hours or rc._infer_history_hours(msg)) if needs else 0.0
    if window:
        hours = max((window[1] - window[0]) / 3600.0, 1 / 60)
    since, until = window if window else (now - hours * 3600, now)
    nq = rc._norm_txt(msg + " " + (prev_user[-1] if prev_user and hours else ""))
    cams = [cid for cid, nm in names.items() if rc._norm_txt(nm) in nq]
    if req.cam_id and req.cam_id in names and req.cam_id not in cams:
        cams = [req.cam_id]
    if "exterior" in nq and not cams:
        cams = [cid for cid, nm in names.items() if "exterior" in rc._norm_txt(nm)]

    steps = ["Estado en vivo de las cámaras"]
    facts_text, groups, label = "", [], ""
    if needs and db is not None:
        label = _range_label(since, until) if window else rc._human_window(hours)
        try:
            from app.storage.facts import build_facts, render_facts
            facts_text = render_facts(build_facts(db, since, until))
            steps.append(f"Hechos exactos · {label}")
        except Exception as e:
            logger.warning("Vision Agent: fallo construyendo hechos: %s", e)
        try:
            groups = _evidence(db, names, since, until, cams or None)
            steps.append(f"Alertas agrupadas · {len(groups)} grupos" + (f" · {', '.join(names[c] for c in cams)}" if cams else ""))
        except Exception as e:
            logger.warning("Vision Agent: fallo construyendo evidencia: %s", e)

    want_imgs = bool(rc._IMG_RE.search(msg))
    images = None
    if want_imgs or (groups and _ALERT_Q_RE.search(msg)):
        images = [{"url": g["url"], "cam": g["cam"], "hora": g["hasta"], "texto": g["texto"],
                   "personas": g["personas"], "ref": g["ref"]} for g in groups if g["url"]][: (8 if want_imgs else 4)]
        steps.append(f"Capturas · {len(images)}")

    context_block, n_ctx = rc._build_camera_context(store, config)
    from app.storage.report_generator import load_skill
    system = rc.compose_chat_system(
        load_skill("chat", rc._DEFAULT_CHAT_SKILL), len(config.cameras), context_block,
        facts_text=facts_text, facts_label=label,
        history_block=_render_evidence(groups) if needs else "",
        hist_kind="Evidencia: alertas agrupadas, cítalas como [E#]", images=images)
    system += "\n\n" + AGENT_RULES
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": msg}]
    sources = [{k: g[k] for k in ("ref", "cam", "tipo", "n", "desde", "hasta", "max_personas", "personas", "texto", "url")}
               for g in groups]
    return {"messages": messages, "steps": steps, "sources": sources, "images": images or [],
            "n_ctx": n_ctx, "hours": hours, "since": since if needs else None, "until": until if needs else None}


@router.post("/api/chat/stream")
def chat_stream(req: StreamRequest, request: Request, x_session_id: Optional[str] = Header(default=None)):
    if not (req.message or "").strip():
        raise HTTPException(400, "mensaje vacío")
    app = request.app
    session_id = (x_session_id or "").strip() or str(uuid.uuid4())
    endpoint, model, key = _chat_cfg(app)
    db = getattr(app.state, "db", None)

    def gen():
        t0 = time.monotonic()
        try:
            ctx = _prepare(req, request, session_id)
        except Exception as e:
            logger.warning("Vision Agent: fallo preparando contexto: %s", e)
            yield _sse("error", {"message": "No pude preparar el contexto; intenta de nuevo."})
            return
        yield _sse("process", {"steps": ctx["steps"], "hours": ctx["hours"], "since": ctx["since"], "until": ctx["until"]})
        if ctx["images"]:
            yield _sse("images", ctx["images"])
        if ctx["sources"]:
            yield _sse("sources", ctx["sources"])

        payload = {"model": model, "max_tokens": 900, "temperature": 0.3, "stream": True,
                   "chat_template_kwargs": {"enable_thinking": False}, "messages": ctx["messages"]}
        text, real = [], None
        for attempt in (1, 2):
            try:
                with _request(endpoint, key, payload, 120) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        real = obj.get("model") or real
                        for ch in obj.get("choices") or []:
                            piece = (ch.get("delta") or {}).get("content")
                            if piece:
                                text.append(piece)
                                yield _sse("delta", {"t": piece})
                break
            except urllib.error.HTTPError as e:
                if attempt == 1 and not text and e.code in (429, 502, 503, 504):
                    time.sleep(2)
                    continue
                _mark(app, False, error=f"HTTP {e.code}")
                yield _sse("error", {"message": f"El modelo no respondió (HTTP {e.code}). Intenta de nuevo."})
                return
            except Exception as e:
                if attempt == 1 and not text:
                    time.sleep(2)
                    continue
                _mark(app, False, error=str(e))
                yield _sse("error", {"message": "No se pudo contactar al modelo. Intenta de nuevo."})
                return

        full = "".join(text).strip()
        related = []
        m = _RELATED_RE.search(full)
        if m:
            try:
                related = [str(q).strip() for q in json.loads(m.group(1)) if str(q).strip()][:3]
            except ValueError:
                related = []
        clean = _RELATED_RE.sub("", full).strip() or "(sin respuesta)"
        ms = round((time.monotonic() - t0) * 1000)
        if full:
            _mark(app, True, ms)
            app.state.chat_model_real = real or getattr(app.state, "chat_model_real", None)
        if db is not None and full:
            try:
                db.insert_chat(session_id, "user", req.message, context_cams=ctx["n_ctx"])
                db.insert_chat(session_id, "assistant", clean, context_cams=ctx["n_ctx"], ms=ms)
            except Exception as e:
                logger.warning("Vision Agent: no se guardó el historial: %s", e)
        logger.info("Vision Agent OK %dms model=%s fuentes=%d imgs=%d Q=%s", ms, real, len(ctx["sources"]),
                    len(ctx["images"]), req.message[:60].replace("\n", " "))
        yield _sse("done", {"text": clean, "related": related, "ms": ms, "model": real, "session_id": session_id})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/chat/feedback")
def chat_feedback(fb: FeedbackRequest, request: Request):
    if fb.label not in ("up", "down"):
        raise HTTPException(400, "label debe ser up o down")
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "sin base de datos")
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS chat_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                         "session_id TEXT, label TEXT NOT NULL, question TEXT, answer TEXT, comment TEXT)")
        cur = db._conn.execute("INSERT INTO chat_feedback (ts, session_id, label, question, answer, comment) VALUES (?,?,?,?,?,?)",
                               (time.time(), fb.session_id[:64], fb.label, fb.question[:2000], fb.answer[:6000], fb.comment[:1000]))
        db._conn.commit()
    return {"id": cur.lastrowid}
