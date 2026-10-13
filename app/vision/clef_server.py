"""Servidor local del modelo de decision Clef-flash (Cloudflare, Apache-2.0, 9B sobre Qwen3.5).

Corre en su propio contenedor (misma imagen que thor-vision) para no mezclar memoria ni caidas con el pipeline de video.
No genera texto: recibe un estado (texto + imagenes) y preguntas tipadas (noul / choice / score) y devuelve una probabilidad
por opcion en un solo pase. Escucha solo en 127.0.0.1 (network_mode host).

  POST /decide   {"state": str|obj, "images": [<jpeg en base64>], "questions": {...}}  ->  respuesta tipo Jev/SystemOne
  GET  /health   estado del modelo y estadisticas de latencia
"""
import base64
import io
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_DIR = os.environ.get("CLEF_MODEL_DIR", "/app/data/models/clef-flash")
PORT = int(os.environ.get("CLEF_PORT", "20140"))
HOST = os.environ.get("CLEF_HOST", "127.0.0.1")
MAX_SIDE = int(os.environ.get("CLEF_IMG_MAX_SIDE", "448"))        # lado mayor de cada imagen: acota los tokens de imagen
MAX_BODY = 8 * 1024 * 1024

logger = logging.getLogger("clef")
_lock = threading.Lock()
_state = {"model": None, "processor": None, "fn": None, "loaded_s": None, "calls": 0, "errors": 0, "ms": []}


def load() -> None:
    import torch
    sys.path.insert(0, MODEL_DIR)
    from joint_schema_model import load_release_model, systemone          # codigo del modelo, revisado: sin exec/eval/red
    t0 = time.time()
    model, processor = load_release_model(MODEL_DIR, device="cuda", dtype=torch.bfloat16)
    _state.update(model=model, processor=processor, fn=systemone, loaded_s=round(time.time() - t0, 1))
    logger.info("Clef-flash cargado en %.1f s", _state["loaded_s"])


def _image(b64: str):
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    k = MAX_SIDE / max(im.size)
    return im.resize((max(28, int(im.width * k)), max(28, int(im.height * k)))) if k < 1 else im


def decide(req: dict) -> dict:
    rec = {"model": "clef-flash", "state": req.get("state", ""), "questions": req["questions"]}
    if req.get("images"):
        rec["images"] = [_image(x) for x in req["images"][:4]]
    t0 = time.time()
    with _lock:                                                     # un pase a la vez en la GPU
        import torch
        with torch.inference_mode():
            out = _state["fn"](_state["model"], _state["processor"], rec)
    ms = (time.time() - t0) * 1000
    _state["calls"] += 1
    _state["ms"] = (_state["ms"] + [ms])[-200:]
    out["ms"] = round(ms)
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "no existe"})
        ms = sorted(_state["ms"])
        self._send(200, {"ready": _state["model"] is not None, "loaded_s": _state["loaded_s"], "calls": _state["calls"], "errors": _state["errors"],
                         "p50_ms": round(ms[len(ms) // 2]) if ms else None, "p95_ms": round(ms[int(len(ms) * .95)]) if ms else None})

    def do_POST(self):
        if self.path != "/decide":
            return self._send(404, {"error": "no existe"})
        if _state["model"] is None:
            return self._send(503, {"error": "modelo cargando"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "tamano invalido"})
            self._send(200, decide(json.loads(self.rfile.read(n))))
        except (ValueError, KeyError, TypeError) as exc:
            self._send(400, {"error": str(exc)[:200]})
        except Exception as exc:                                     # noqa: BLE001 - el servidor no debe caerse por una peticion
            _state["errors"] += 1
            logger.exception("decide")
            self._send(500, {"error": str(exc)[:200]})

    def log_message(self, *a):                                       # sin una linea por peticion
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    load()
    threading.Event().wait()


if __name__ == "__main__":
    main()
