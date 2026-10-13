"""Version de THOR Vision. Fuente unica: la primera entrada de app/release_notes.json (la misma que muestra el dashboard)."""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "release_notes.json")


def _top() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)[0]
    except (OSError, ValueError, IndexError):
        return {"version": "0.0.0", "date": "", "name": "sin notas"}


_T = _top()
VERSION = _T["version"]
RELEASED = _T.get("date", "")
NAME = _T.get("name", "")


def build_info() -> dict:
    """Commit desplegado (lo escribe scripts/stamp_build.py al desplegar); vacio si no se estampo."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "build_info.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
