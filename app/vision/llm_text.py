"""
Completado de texto puro (sin imagen/video) contra el mismo gateway LLM que
usa VLMAnalyzer. Usa streaming (SSE): el gateway corta con 504 a ~15s las
peticiones NO-stream, y un reporte de 900 tokens tarda ~40s; con stream la
conexion se mantiene viva y llega completo.
"""
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_RETRY_HTTP_CODES = frozenset({429, 502, 503, 504})


def _stream_once(req, timeout: int) -> str:
    content, reasoning = [], []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0].get("delta", {})
            except Exception:
                continue
            if delta.get("content"):
                content.append(delta["content"])
            r = delta.get("reasoning_content") or delta.get("reasoning")
            if r:
                reasoning.append(r)
    text = "".join(content).strip()
    if not text and reasoning:
        text = "".join(reasoning).strip().split("\n\n")[-1].strip()
    return text


def complete_text(analyzer, messages: list, max_tokens: int = 900,
                  temperature: float = 0.4, timeout: int = 90, thinking: bool = False) -> str:
    payload = json.dumps({
        "model":                analyzer.model,
        "max_tokens":           max_tokens,
        "temperature":          temperature,
        "stream":               True,
        "chat_template_kwargs": {"enable_thinking": thinking},
        "messages":             messages,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if getattr(analyzer, "api_key", None):
        headers["Authorization"] = f"Bearer {analyzer.api_key}"
    req = urllib.request.Request(analyzer.endpoint, data=payload,
                                 headers=headers, method="POST")

    for attempt in (1, 2, 3):
        try:
            return _stream_once(req, timeout)
        except urllib.error.HTTPError as e:
            if attempt < 3 and e.code in _RETRY_HTTP_CODES:
                logger.info("complete_text: %s transitorio, reintentando", e.code)
                time.sleep(2 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                logger.info("complete_text: gateway inalcanzable, reintentando")
                time.sleep(2 * attempt)
                continue
            raise
    return ""
