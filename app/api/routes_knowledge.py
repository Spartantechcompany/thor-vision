"""Conocimiento de la casa: un markdown editable (mascotas, personas, reglas) mas datos vivos (camaras, zonas).

Es la fuente unica de lo que el Vision Agent "sabe" de la casa: se sirve como resultado de busqueda con prioridad, asi no hay copias
viejas escritas a mano en prompts. Se edita en /conocimiento o con PUT /api/knowledge.
"""
import os
import re
import time

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()
PATH = os.environ.get("KNOWLEDGE_PATH", "/app/data/knowledge.md")
MAX_CHARS = 20000

DEFAULT = """# Conocimiento de la casa

## Mascotas
- **Akamaru**: shiba inu de color cafe. Es el papa.
- **Mojo-jojo**: perro negro pequeno.
- **Gigi**: shar pei, mas grande que Akamaru (no es un pug).
- Las mascotas conocidas no son una alerta. Las sombras y las plantas fijas a veces se detectan como "perro" por error.

## Personas
- Cada persona detectada empieza en "Por revisar" y se clasifica como Empleado o Invitado en /identidades.
- Los empleados tienen reporte de asistencia (camaras interiores y Garage frontal); de los invitados solo se cuentan visitas y capturas.
- Las capturas de rostro y cuerpo se conservan 90 dias.

## Reglas de las camaras
- Exterior 1 y Exterior 2 reconocen a quien ya esta registrado, pero no crean identidades nuevas; su actividad queda como alertas.
- En Exterior 1 y 2 se registran los vehiculos estacionados solo dentro de las zonas dibujadas, con alertas al llegar, cada hora y al irse.
- El videoportero (cam-vto) solo detecta rostros; cada visita con rostro genera una alerta.
"""


def _text() -> str:
    try:
        with open(PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return DEFAULT


def _live(request: Request) -> str:
    cfg = request.app.state.config
    lines = ["## Camaras (datos vivos)"]
    for c in cfg.cameras:
        lines.append(f"- {c.name} ({c.id}): zona {c.zone}" + (", solo rostros" if getattr(c, "mode", "full") == "faces" else ""))
    try:
        from app.vision import zones
        pk = [f"{c.name}: {sum(1 for z in zones.get(c.id) if z['type'] == 'estacionamiento')} zona(s)" for c in cfg.cameras if zones.has(c.id, "estacionamiento")]
        if pk:
            lines.append("- Zonas de estacionamiento dibujadas: " + "; ".join(pk))
    except (ImportError, KeyError, TypeError):
        pass
    return "\n".join(lines)


def sections(request: Request) -> list:
    """[(titulo, texto)] del markdown editable mas los datos vivos."""
    out = []
    for blk in re.split(r"\n(?=## )", _text() + "\n" + _live(request)):
        m = re.match(r"##\s+(.+)", blk)
        if m:
            out.append((m.group(1).strip(), blk.strip()))
    return out


@router.get("/api/knowledge")
def get_knowledge(request: Request):
    return {"markdown": _text(), "live": _live(request), "path": PATH, "exists": os.path.exists(PATH)}


@router.put("/api/knowledge")
def put_knowledge(payload: dict = Body(...)):
    md = str(payload.get("markdown", ""))
    if not md.strip() or len(md) > MAX_CHARS:
        raise HTTPException(400, f"el texto debe tener entre 1 y {MAX_CHARS} caracteres")
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, PATH)
    return {"ok": True, "saved_at": time.time()}
