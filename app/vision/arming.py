"""Estado de armado del sistema. 'normal' = reglas por camara y horario; 'absoluto' = toda persona, a cualquier hora y en cualquier camara,
es alerta (sin excepciones por personas conocidas ni mascotas), y cada visita que cierra genera su propia alerta con captura.
El estado se guarda en un archivo para sobrevivir reinicios."""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
PATH = os.environ.get("ARMING_FILE", "/app/data/arming.json")
MODES = ("normal", "absoluto")
_lock = threading.Lock()
_state = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                d = json.load(f)
            _state = {"mode": d["mode"] if d.get("mode") in MODES else "normal", "since": float(d.get("since") or 0), "by": str(d.get("by") or "")}
        except (OSError, ValueError, KeyError, TypeError):
            _state = {"mode": "normal", "since": 0.0, "by": ""}
    return _state


def get() -> dict:
    with _lock:
        return dict(_load())


def is_absolute() -> bool:
    return get()["mode"] == "absoluto"


def set_mode(mode: str, by: str = "") -> dict:
    if mode not in MODES:
        raise ValueError("modo desconocido")
    with _lock:
        st = _load()
        if st["mode"] != mode:
            st.update(mode=mode, since=time.time(), by=by[:60])
            try:
                os.makedirs(os.path.dirname(PATH), exist_ok=True)
                with open(PATH, "w", encoding="utf-8") as f:
                    json.dump(st, f)
            except OSError as exc:
                logger.warning("arming: no se pudo guardar el estado: %s", exc)
        return dict(st)


def reset_for_tests() -> None:
    global _state
    _state = None
