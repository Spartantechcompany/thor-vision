import base64
import json
import logging
import os
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from app.vision.scenes import normalize_result

logger = logging.getLogger(__name__)
VIDEO_MAX_W = int(os.environ.get("VLM_VIDEO_MAX_WIDTH", "640"))
VIDEO_MAX_H = int(os.environ.get("VLM_VIDEO_MAX_HEIGHT", "360"))

SYSTEM_PROMPT = (
    "Eres un analista de cámaras de seguridad. Describes solo lo que es "
    "visible en la imagen, con precisión y sin especular. "
    "Responde ÚNICAMENTE con un objeto JSON válido. "
    "Sin texto adicional, sin markdown, sin explicación."
)

USER_PROMPT = (
    'Analiza el frame y responde con este JSON exacto:\n'
    '{"people": 0, "activity": "descripcion", "alerts": [], "scene": "descripcion"}\n'
    'Usa enteros reales para people, strings reales para los demas campos.'
)

VIDEO_USER_PROMPT = (
    'Analiza esta secuencia de video (varios segundos, no un solo instante) y '
    'responde con este JSON exacto:\n'
    '{"people": 0, "activity": "descripcion", "alerts": [], "scene": "descripcion"}\n'
    'Describe la actividad como algo que ocurre a lo largo del clip (quien entra, '
    'sale, se mueve, qué dirección), no solo el contenido de un frame estático. '
    'Usa enteros reales para people, strings reales para los demas campos.'
)

# Prompt especializado para el pipeline de detalle de cam-cowork: pide tipo
# de contenido por monitor, NO transcripción literal de texto — un experimento
# manual (crop+zoom sobre un frame real) confirmó que a esta resolución no se
# puede leer texto con confianza, solo distinguir el tipo de contenido.
MONITOR_DETAIL_PROMPT = (
    'Esta cámara ve un área de trabajo con monitores/pantallas de computadora. '
    'Responde ÚNICAMENTE con este JSON exacto:\n'
    '{"monitor_count": 0, "monitors": ["tipo de contenido"], "people": 0}\n'
    'Para "monitors", da un array con una entrada breve por cada monitor visible '
    'describiendo el TIPO de contenido (por ejemplo: "editor de código", '
    '"navegador web", "terminal", "videollamada", "documento/hoja de cálculo", '
    '"apagado o sin contenido visible"). NO intentes transcribir texto literal '
    'ni leer contenido específico — a esta resolución no es confiable, solo '
    'clasifica el tipo de actividad visible en cada pantalla. '
    'Usa enteros reales para monitor_count y people.'
)


DEBUG_DIR = Path("/app/data/debug/nemotron")
DEBUG_MAX_FILES = 40  # ~20 pares (payload + respuesta), rota los mas viejos


def _dump_debug(tag: str, media_bytes: bytes, media_ext: str, response_body: str) -> None:
    """Guarda el payload (imagen/video) exacto + la respuesta cruda cuando una
    llamada a VLM falla, para poder inspeccionar el caso real despues."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        base = DEBUG_DIR / f"{ts}_{tag}"
        base.with_suffix(media_ext).write_bytes(media_bytes)
        base.with_suffix(".response.json").write_text(response_body, encoding="utf-8")

        files = sorted(DEBUG_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        excess = len(files) - DEBUG_MAX_FILES
        for f in files[:max(excess, 0)]:
            f.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("No se pudo guardar debug dump: %s", e)


LATEST_DIR = DEBUG_DIR / "latest"


def _dump_latest(tag: str, media_bytes: bytes, media_ext: str, response_body: str) -> None:
    """Guarda SIEMPRE (exito o error) el ultimo payload por camara — vista
    tipo 'live-vlm-webui' de que se mando/recibio ahora mismo, sin rotacion
    (un solo par por tag, se sobreescribe)."""
    try:
        LATEST_DIR.mkdir(parents=True, exist_ok=True)
        base = LATEST_DIR / tag
        base.with_suffix(media_ext).write_bytes(media_bytes)
        base.with_suffix(".response.json").write_text(response_body, encoding="utf-8")
    except Exception as e:
        logger.warning("No se pudo guardar latest payload: %s", e)


class VLMAnalyzer:
    """
    Envía frames (o clips de video cortos) a el VLM (vLLM) para
    análisis semántico. Deshabilita thinking mode con chat_template_kwargs
    para obtener JSON directo en content sin razonamiento.
    """

    def __init__(self, endpoint: str, model: str = "thor-vision",
                 max_tokens: int = 200, timeout: int = 30,
                 video_timeout: int = 60, api_key: Optional[str] = None,
                 user_prompt: str = USER_PROMPT,
                 video_user_prompt: str = VIDEO_USER_PROMPT,
                 max_width: int = 640, max_height: int = 360):
        self.endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.video_timeout = video_timeout
        self.api_key = api_key
        # Permiten instancias especializadas (ej. detalle de pantallas en
        # cam-cowork) sin tocar el comportamiento de las 10 cámaras que usan
        # los defaults de módulo.
        self.user_prompt = user_prompt
        self.video_user_prompt = video_user_prompt
        # max_width solo (sin tope de alto) asumia 16:9 implicitamente. En
        # cuanto una camara con otro aspect ratio entro al pipeline (cam-189
        # 4:3 nativo 2592x1944, cam-113 vertical 1080x1920) el resultado tras
        # escalar por ancho quedaba MUCHO mas alto de lo esperado (ej. 640x480
        # o 640x1139) — mas pixeles = mas tokens de imagen, y en video (varios
        # frames por request) esto empujo el request por completo sobre el
        # limite de contexto del modelo (context_length_exceeded, 400, sin
        # reintento). Confirmado en logs: cam-189 con analisis por movimiento
        # (via=video) fallando el 100% de las veces desde que se piloteo su
        # resolucion nativa. max_height acota el otro eje tambien — el mismo
        # presupuesto de pixeles (640x360) para cualquier aspect ratio.
        self.max_width = max_width
        self.max_height = max_height
        # Modelo que de verdad responde. `self.model` es lo que se PIDE
        # (hoy un alias del gateway); esto es lo que el backend REPORTA.
        self.last_model = None
        logger.info("VLMAnalyzer init — endpoint=%s model=%s auth=%s",
                    self.endpoint, self.model, "bearer" if api_key else "none")

    def _finish(self, result: dict, scene, context: Optional[dict]) -> dict:
        if scene is None:
            return result
        yolo = (context or {}).get("person_count")
        return normalize_result(result, scene, yolo if isinstance(yolo, int) else None)

    def analyze(self, frame: np.ndarray, context: dict = None,
                tag: str = "frame", scene=None) -> dict:
        jpeg = self._encode_jpeg(frame)
        if not jpeg:
            return self._error("encode failed")

        b64 = base64.b64encode(jpeg).decode()
        crop_part = self._person_crop_part(frame, context, tag)
        user_text = self._build_context_prefix(context) + (
            scene.render() if scene else self.user_prompt)

        content_part = {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{b64}"
        }}
        if crop_part:
            content_part = [content_part, crop_part]
        return self._finish(self._request(content_part, user_text, self.timeout,
                              debug_tag=tag, debug_media=jpeg, debug_ext=".jpg"),
                            scene, context)

    def describe_visit(self, cam_id: str, body: Optional[bytes], face: Optional[bytes],
                       scene: Optional[bytes]) -> dict:
        """Descripcion de UNA persona a partir de sus mejores recortes (se llama al
        cerrar la visita, no por cada movimiento)."""
        parts, names = [], []
        for label, blob in (("recorte del cuerpo", body), ("recorte del rostro", face),
                            ("escena completa", scene)):
            if blob:
                b64 = base64.b64encode(blob).decode()
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
                names.append(label)
        if not parts:
            return self._error("sin imagenes")
        text = (
            f"Camara de seguridad {cam_id}. Las imagenes, en este orden ({', '.join(names)}), "
            "muestran a UNA misma persona detectada. Describela para poder reconocerla despues. "
            'Responde SOLO este JSON: {"visible": <true si hay una persona real; false si es un '
            'objeto, reflejo o falsa deteccion>, "desc": "genero aparente, edad aproximada, ropa '
            '(prendas y colores), accesorios u objetos que carga; maximo 25 palabras", '
            '"action": "que hace o hacia donde va; maximo 12 palabras", '
            '"confidence": "low|medium|high"}. No inventes: si algo no se distingue escribe '
            '"no distinguible".')
        return self._request(parts, text, self.timeout, debug_tag=f"visit-{cam_id}")

    def analyze_video(self, frames: List[np.ndarray], fps: float,
                       context: dict = None, tag: str = "video", scene=None,
                       crop_frame: Optional[np.ndarray] = None) -> dict:
        """
        Igual que `analyze`, pero envía una secuencia corta de frames como
        clip de video (content type `video_url`) en vez de un frame único.
        El modelo ve movimiento/temporalidad real, no solo un instante.

        `frames` debe venir en orden cronológico (más viejo primero).
        Si el clip tiene <2 frames, cae a `analyze` sobre el último frame —
        un "video" de 1 frame no aporta nada sobre la vía de imagen normal.
        """
        if not frames:
            return self._error("no frames")
        if len(frames) < 2:
            return self.analyze(frames[-1], context, scene=scene)

        mp4 = self._encode_mp4(frames, fps)
        if not mp4:
            return self._error("video encode failed")

        b64 = base64.b64encode(mp4).decode()
        crop_part = self._person_crop_part(
            crop_frame if crop_frame is not None else frames[-1], context, tag)
        user_text = self._build_context_prefix(context) + (
            scene.render(video=True) if scene else self.video_user_prompt)

        content_part = {"type": "video_url", "video_url": {
            "url": f"data:video/mp4;base64,{b64}"
        }}
        if crop_part:
            content_part = [content_part, crop_part]
        return self._finish(self._request(content_part, user_text, self.video_timeout,
                              debug_tag=tag, debug_media=mp4, debug_ext=".mp4"),
                            scene, context)

    @staticmethod
    def _union_box(context: dict):
        bbs = context.get("person_bboxes") or []
        fw, fh = context.get("frame_wh") or (0, 0)
        if not bbs or not fw or not fh:
            return None
        return (min(b[0] for b in bbs), min(b[1] for b in bbs),
                max(b[2] for b in bbs), max(b[3] for b in bbs), fw, fh)

    @staticmethod
    def _person_hint(context: Optional[dict]) -> str:
        u = VLMAnalyzer._union_box(context or {})
        if not u:
            return ""
        x1, y1, x2, y2, fw, fh = u
        cx, cy = (x1 + x2) / 2 / fw, (y1 + y2) / 2 / fh
        horiz = "izquierda" if cx < .34 else ("centro" if cx < .67 else "derecha")
        vert = "arriba" if cy < .34 else ("en medio" if cy < .67 else "abajo")
        size = round((y2 - y1) / fh * 100)
        n = context.get("person_count")
        crop = ("La 2a imagen es un recorte ampliado de esa zona: revisala primero; "
                "si hay una persona cuentala y describe su ropa y que hace. "
                if context.get("_has_crop") else "")
        return (f"Un detector de personas (YOLO) marco {n} persona(s) en la zona {horiz}-{vert} "
                f"del encuadre (alto ~{size}% de la imagen; puede ser pequena o lejana). "
                f"{crop}Si de verdad no hay ninguna persona (p. ej. es un objeto), "
                f"pon people=0.\n")

    def _person_crop_part(self, frame: np.ndarray, context: Optional[dict],
                          tag: str) -> Optional[dict]:
        """Recorte ampliado de la(s) persona(s) que vio YOLO, tomado del frame de
        resolucion nativa: una persona lejana mide ~30 px tras el downscale a
        640x360 y el VLM no la ve; en el recorte si."""
        try:
            u = self._union_box(context or {})
            if not u:
                return None
            x1, y1, x2, y2, fw, fh = u
            h, w = frame.shape[:2]
            sx, sy = w / fw, h / fh
            x1, x2, y1, y2 = x1 * sx, x2 * sx, y1 * sy, y2 * sy
            bw, bh = x2 - x1, y2 - y1
            side = max(bw * 1.7, bh * 1.35, 160.0)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            ax, ay = int(max(0, cx - side * .5)), int(max(0, cy - side * .5))
            bx, by = int(min(w, cx + side * .5)), int(min(h, cy + side * .5))
            crop = frame[ay:by, ax:bx]
            if crop.size == 0:
                return None
            ch, cw = crop.shape[:2]
            k = 448.0 / max(ch, cw)
            k = min(k, 3.0)
            crop = cv2.resize(crop, (max(1, int(cw * k)), max(1, int(ch * k))),
                              interpolation=cv2.INTER_CUBIC)
            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return None
            jpg = buf.tobytes()
            context["_has_crop"] = True
            try:
                LATEST_DIR.mkdir(parents=True, exist_ok=True)
                (LATEST_DIR / f"{tag}.crop.jpg").write_bytes(jpg)
            except Exception:
                pass
            return {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}
        except Exception as e:
            logger.debug("person crop error: %s", e)
            return None

    @staticmethod
    def _build_context_prefix(context: Optional[dict]) -> str:
        if not context:
            return ""
        parts = []
        hint = VLMAnalyzer._person_hint(context)
        if hint:
            return hint
        if context.get("person_count") is not None:
            parts.append(f"YOLO contó {context['person_count']} persona(s)")
        if context.get("faces"):
            names = [f.get("name", "desconocido") for f in context["faces"]]
            parts.append(f"Caras reconocidas: {', '.join(names)}")
        if not parts:
            return ""
        return ("Referencia de un detector de personas (puede fallar, sobre todo de "
                "noche; verifica tú en la imagen): " + "; ".join(parts) + ".\n")

    # El gateway devuelve 503 (chat_admission_busy) y 504 de forma
    # intermitente; esos sí valen reintento. 401/400 son permanentes:
    # reintentarlos solo gasta GPU y retrasa el error real.
    _RETRY_HTTP_CODES = frozenset({429, 502, 503, 504})
    _MAX_ATTEMPTS     = 3
    _RETRY_BASE_DELAY = 1.0

    def _request(self, media_content_part: dict, user_text: str, timeout: int,
                 debug_tag: str = "req", debug_media: Optional[bytes] = None,
                 debug_ext: str = ".bin") -> dict:
        """Reintenta solo los fallos transitorios, con backoff exponencial."""
        delay = self._RETRY_BASE_DELAY
        for intento in range(1, self._MAX_ATTEMPTS + 1):
            result = self._request_once(media_content_part, user_text, timeout,
                                        debug_tag, debug_media, debug_ext)
            if not result.pop("_retryable", False):
                return result
            if intento == self._MAX_ATTEMPTS:
                logger.warning("VLM cam=%s: agotados %d intentos",
                               debug_tag, self._MAX_ATTEMPTS)
                return result
            motivo = (result.get("alerts") or ["?"])[0]
            logger.info("VLM cam=%s reintento %d/%d en %.0fs (%s)",
                        debug_tag, intento, self._MAX_ATTEMPTS - 1, delay,
                        str(motivo)[:80])
            time.sleep(delay)
            delay *= 2
        return result

    def _request_once(self, media_content_part: dict, user_text: str, timeout: int,
                      debug_tag: str = "req", debug_media: Optional[bytes] = None,
                      debug_ext: str = ".bin") -> dict:
        t0 = time.monotonic()
        payload = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            # Deshabilitar thinking mode — fuerza respuesta directa en content
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        *(media_content_part if isinstance(media_content_part, list)
                          else [media_content_part]),
                        {"type": "text", "text": user_text},
                    ]
                }
            ]
        }).encode()

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_body = resp.read()
            data = json.loads(raw_body)
            if debug_media is not None:
                _dump_latest(debug_tag, debug_media, debug_ext,
                             raw_body.decode(errors="replace"))

            choice = data["choices"][0]["message"]
            text = (choice.get("content") or "").strip()

            if not text:
                # Fallback: intentar extraer JSON del reasoning
                reasoning = (choice.get("reasoning") or "").strip()
                # Buscar último bloque JSON completo en el reasoning
                start = reasoning.rfind("{")
                end   = reasoning.rfind("}") + 1
                if start >= 0 and end > start:
                    candidate = reasoning[start:end]
                    try:
                        json.loads(candidate)  # validar antes de usar
                        text = candidate
                        logger.debug("VLM: JSON extraído de reasoning")
                    except json.JSONDecodeError:
                        pass

            if not text:
                logger.warning("VLM empty content+reasoning for response: %s",
                               json.dumps(choice)[:200])
                return self._error("empty response")

            # Limpiar fences markdown si los hay
            if "```" in text:
                for block in text.split("```"):
                    block = block.strip().lstrip("json").strip()
                    if block.startswith("{"):
                        text = block
                        break

            # Extraer primer objeto JSON
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]

            result = json.loads(text)
            result["_ms"] = round((time.monotonic() - t0) * 1000)
            result["_ts"] = time.time()
            served = data.get("model")
            if served:
                self.last_model = served
                result["_model"] = served
            logger.info("VLM OK %.0fms people=%s activity=%s",
                        result["_ms"], result.get("people"), str(result.get("activity", ""))[:50])
            return result

        except urllib.error.HTTPError as e:
            body = "?"
            try:
                body = e.read().decode(errors="replace")[:2000]
            except Exception:
                pass
            logger.warning("VLM HTTP %s: %s | body=%s", e.code, e.reason, body)
            if debug_media is not None:
                _dump_debug(debug_tag, debug_media, debug_ext, body)
                _dump_latest(debug_tag, debug_media, debug_ext, body)
            err = self._error(f"http {e.code}: {body[:200]}")
            err["_retryable"] = e.code in self._RETRY_HTTP_CODES
            return err
        except urllib.error.URLError as e:
            logger.warning("VLM unreachable: %s", e)
            if debug_media is not None:
                _dump_debug(debug_tag, debug_media, debug_ext, str(e))
                _dump_latest(debug_tag, debug_media, debug_ext, str(e))
            err = self._error(f"unreachable: {e}")
            err["_retryable"] = True     # red/timeout: transitorio
            return err
        except json.JSONDecodeError as e:
            snippet = text[:120] if 'text' in locals() else "?"
            logger.warning("VLM bad JSON: %s | snippet=%s", e, snippet)
            return self._error("bad json")
        except Exception as e:
            logger.warning("VLM error: %s", e)
            return self._error(str(e))

    def _target_dims(self, w: int, h: int) -> tuple[int, int]:
        """Dimensiones finales respetando max_width Y max_height (nunca solo
        uno), preservando aspect ratio, sin agrandar si ya entra en el
        presupuesto. Mismo presupuesto de pixeles para cualquier orientacion
        o relacion de aspecto — evita que una camara 4:3 o vertical mande
        mas pixeles/tokens que una 16:9 estandar."""
        scale = min(self.max_width / w, self.max_height / h, 1.0)
        return max(1, int(w * scale)), max(1, int(h * scale))

    def _encode_jpeg(self, frame: np.ndarray, quality: int = 70) -> Optional[bytes]:
        try:
            h, w = frame.shape[:2]
            tw, th = self._target_dims(w, h)
            if (tw, th) != (w, h):
                frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            return buf.tobytes()
        except Exception as e:
            logger.error("JPEG encode error: %s", e)
            return None

    # Tope de frames por clip, independiente de cuantos entregue el buffer.
    # Antes del fix de GStreamer+NVDEC (2026-09-15) las camaras entregaban
    # frames de forma poco confiable (~5-15 utiles en una ventana de 3s), asi
    # que el limite real nunca se probo. Con NVDEC decodificando a la fps
    # real de la camara (15-31fps confirmado en vivo), el buffer de 30 slots
    # se llena COMPLETO en ~1-1.5s y get_recent_frames(3s) siempre devuelve
    # los 30 — 3x mas frames que antes, y aunque cada frame ya viene acotado
    # por _target_dims(), 30 frames de video volvio a empujar el request
    # sobre el limite de contexto del modelo (confirmado en logs: 237
    # context_length_exceeded en 6h, hasta 180K tokens estimados). Submuestreo
    # uniforme a 10 frames — cubre la misma duracion real del clip (se ajusta
    # el fps de salida proporcionalmente, no se acelera el video), suficiente
    # para describir direccion/entrada/salida de movimiento.
    MAX_VIDEO_FRAMES = 10

    def _subsample_frames(self, frames: List[np.ndarray], fps: float) -> tuple[List[np.ndarray], float]:
        n = len(frames)
        if n <= self.MAX_VIDEO_FRAMES:
            return frames, fps
        idx = [round(i * (n - 1) / (self.MAX_VIDEO_FRAMES - 1)) for i in range(self.MAX_VIDEO_FRAMES)]
        sampled = [frames[i] for i in idx]
        # fps de salida reducido en la misma proporcion para no acelerar
        # el clip — misma duracion real, menos cuadros por segundo mostrados.
        adjusted_fps = max(fps * len(sampled) / n, 1.0) if fps > 0 else fps
        return sampled, adjusted_fps

    def _encode_mp4(self, frames: List[np.ndarray], fps: float) -> Optional[bytes]:
        """
        Codifica una lista de frames a un MP4 corto en un archivo temporal
        (cv2.VideoWriter no soporta escribir a memoria directamente) y
        devuelve los bytes. Reescala igual que _encode_jpeg para no mandar
        video a resolución completa de cámara.
        """
        if not frames:
            return None
        try:
            frames, fps = self._subsample_frames(frames, fps)
            h, w = frames[0].shape[:2]
            w, h = self._target_dims(w, h)
            vs = min(VIDEO_MAX_W / w, VIDEO_MAX_H / h, 1.0)
            w, h = max(2, int(w * vs) // 2 * 2), max(2, int(h * vs) // 2 * 2)

            # fps mínimo de 1 — VideoWriter no acepta 0 o negativos, y un
            # buffer recién arrancado puede reportar fps=0 antes de estabilizar.
            safe_fps = max(fps, 1.0)

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as tmp:
                writer = cv2.VideoWriter(
                    tmp.name, cv2.VideoWriter_fourcc(*"mp4v"), safe_fps, (w, h)
                )
                if not writer.isOpened():
                    logger.error("VideoWriter no pudo abrir %s", tmp.name)
                    return None
                try:
                    for frame in frames:
                        resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR) \
                            if frame.shape[:2] != (h, w) else frame
                        writer.write(resized)
                finally:
                    writer.release()
                tmp.seek(0)
                return tmp.read()
        except Exception as e:
            logger.error("MP4 encode error: %s", e)
            return None

    @staticmethod
    def _error(msg: str) -> dict:
        return {
            "people": -1,
            "activity": "error",
            "alerts": [msg],
            "scene": "error",
            "_ts": time.time(),
            "_ms": 0,
        }
