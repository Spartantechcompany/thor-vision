"""Archivos de datos de OXM Hogar: JSON con escritura atómica y, para secretos, permisos 600."""
import json
import os
import tempfile
import threading
import time

DATA = os.environ.get("HOGAR_DATA", "/srv/data")
_lock = threading.Lock()


def path(name: str) -> str:
    return os.path.join(DATA, name)


def load(name: str, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name: str, value, secret: bool = False) -> None:
    os.makedirs(DATA, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o600 if secret else 0o644)
            os.replace(tmp, path(name))
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def log(who: str, what: str, result: str) -> None:
    """Bitácora de acciones (quién, qué, resultado); se recorta a las últimas ~2000 líneas."""
    os.makedirs(DATA, exist_ok=True)
    line = json.dumps({"ts": time.time(), "who": who, "what": what, "result": result}, ensure_ascii=False)
    p = path("bitacora.jsonl")
    with _lock:
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()[-1999:]
        except OSError:
            lines = []
        lines.append(line)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def recent_log(n: int = 100) -> list:
    try:
        with open(path("bitacora.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
