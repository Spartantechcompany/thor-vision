import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

SCENES_PATH = os.environ.get("SCENES_PATH", "/app/config/scenes.yml")

SEVERITIES = ("none", "low", "medium", "high")
CONFIDENCES = ("low", "medium", "high")

SCHEMA_INSTRUCTIONS = (
    'Responde SOLO con este JSON (todos los campos, sin comentarios):\n'
    '{"people": <entero: personas realmente visibles>,'
    ' "persons": [{"desc": "ropa/rasgos breves", "action": "que hace", "where": "zona del encuadre"}],'
    ' "vehicles": <entero: vehiculos visibles en movimiento o recien llegados>,'
    ' "activity": "UNA frase factual SIEMPRE descriptiva del estado actual de la escena. Con personas o vehiculos: que hacen (ej: Una persona parada junto al muro). Sin ellos: describe el lugar y su estado visible (ej: Sala vacia con luces encendidas, sillas ordenadas; Patio oscuro, sin movimiento, camioneta blanca estacionada). NUNCA respondas solo Sin actividad relevante",'
    ' "scene": "iluminacion/visibilidad en 8 palabras o menos",'
    ' "relevant": <true solo si ocurre algo de LO QUE IMPORTA>,'
    ' "alerts": ["frase corta y especifica"],'
    ' "alert_types": ["codigo permitido"],'
    ' "severity": "none|low|medium|high",'
    ' "confidence": "low|medium|high"}\n'
    'Reglas: cuenta solo lo que ves en la imagen; si esta oscura o borrosa escribe '
    '"no distinguible" en vez de inventar rasgos; "alerts" queda vacio si nada es '
    'relevante y JAMAS alertes por algo de la lista IGNORA; no menciones detectores '
    'ni conteos externos en tu respuesta; severity high solo para persona en el suelo, '
    'acceso forzado o fuego/humo; usa confidence low si la imagen no permite estar seguro.'
)


@dataclass
class SceneContext:
    cam_id: str
    name: str
    where: str
    focus: list
    ignore: list
    alert_types: frozenset
    night_person: str
    vehicles: bool
    catalog: dict = field(default_factory=dict)
    night_hours: tuple = (22, 6)

    def is_night(self, now: Optional[float] = None) -> bool:
        h = time.localtime(now or time.time()).tm_hour
        a, b = self.night_hours
        return (h >= a or h < b) if a > b else (a <= h < b)

    def render(self, now: Optional[float] = None, video: bool = False) -> str:
        now = now or time.time()
        night = self.is_night(now)
        lt = time.strftime("%H:%M", time.localtime(now))
        lines = [
            f"CAMARA: {self.name} ({self.cam_id})",
            f"UBICACION: {' '.join(self.where.split())}",
            f"HORA LOCAL: {lt} ({'NOCTURNO' if night else 'diurno'})",
            "LO QUE IMPORTA:",
            *[f"- {x}" for x in self.focus],
            "IGNORA (es normal, NO generes alerta):",
            *[f"- {x}" for x in self.ignore],
            "CODIGOS DE ALERTA PERMITIDOS (usa solo estos en alert_types):",
            *[f"- {c}: {self.catalog.get(c, c)}" for c in sorted(self.alert_types)],
        ]
        if night and self.night_person == "alert":
            lines.append("REGLA NOCTURNA: aqui no deberia haber nadie de noche; "
                         "cualquier persona visible es alerta persona_nocturna.")
        elif night:
            lines.append("REGLA NOCTURNA: de noche una persona solo de paso NO es alerta; "
                         "alerta solo si se detiene, merodea o toca algo.")
        if self.vehicles:
            lines.append("Cuenta vehiculos solo si se mueven, llegan o se detienen; "
                         "los ya estacionados no cuentan.")
        head = "\n".join(lines)
        media = ("Analiza esta secuencia de video de unos segundos; en activity describe "
                 "quien entra/sale/se mueve y hacia donde.\n" if video
                 else "Analiza este frame.\n")
        return f"{head}\n\n{media}{SCHEMA_INSTRUCTIONS}"


class Scenes:
    def __init__(self, path: str = SCENES_PATH):
        self._by_cam: dict = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("scenes: no se pudo cargar %s: %s (prompt generico)", path, e)
            return
        catalog = raw.get("alert_catalog") or {}
        nh = tuple(raw.get("night_hours") or (22, 6))
        for cam_id, c in (raw.get("cameras") or {}).items():
            self._by_cam[cam_id] = SceneContext(
                cam_id=cam_id,
                name=c.get("name", cam_id),
                where=c.get("where", ""),
                focus=list(c.get("focus") or []),
                ignore=list(c.get("ignore") or []),
                alert_types=frozenset(t for t in (c.get("alert_types") or []) if t in catalog),
                night_person=c.get("night_person", "note"),
                vehicles=bool(c.get("vehicles", False)),
                catalog=catalog,
                night_hours=nh,
            )
        logger.info("scenes: %d camaras con contexto de escena", len(self._by_cam))

    def get(self, cam_id: str) -> Optional[SceneContext]:
        return self._by_cam.get(cam_id)


def _clip(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def normalize_result(result: dict, scene: SceneContext, yolo_people: Optional[int] = None) -> dict:
    """Valida y sanea la respuesta del VLM contra el esquema por camara."""
    if not isinstance(result, dict) or result.get("activity") == "error":
        return result

    people = result.get("people")
    people = people if isinstance(people, int) and 0 <= people <= 99 else 0
    result["people"] = people

    persons = []
    for p in (result.get("persons") or [])[:6]:
        if isinstance(p, dict):
            persons.append({k: _clip(p.get(k), 90) for k in ("desc", "action", "where")})
        elif isinstance(p, str):
            persons.append({"desc": _clip(p, 90), "action": "", "where": ""})
    result["persons"] = persons

    v = result.get("vehicles")
    result["vehicles"] = v if isinstance(v, int) and 0 <= v <= 50 else 0
    act = _clip(result.get("activity"), 240)
    sc = _clip(result.get("scene"), 80)
    if not act or act.strip().lower().rstrip(".") in ("sin actividad relevante", "sin informacion relevante"):
        act = "Sin personas ni movimiento en cuadro" + (f" - {sc}" if sc else "")
    result["activity"] = act
    result["scene"] = _clip(result.get("scene"), 80)

    alerts = []
    for a in (result.get("alerts") or []):
        t = _clip(a, 160)
        low = t.lower()
        if not t or "yolo" in low or "discrepancia" in low or low in ("ninguna", "ninguno", "n/a", "none"):
            continue
        alerts.append(t)
    types = [t for t in (result.get("alert_types") or []) if t in scene.alert_types]

    relevant = result.get("relevant") is True
    if not relevant and not types:
        alerts = []          # nada relevante: no hay alerta aunque el modelo escriba una
    result["alerts"] = alerts[:4]
    result["alert_types"] = types if alerts else []
    result["relevant"] = bool(relevant or alerts)

    sev = str(result.get("severity", "none")).lower()
    sev = sev if sev in SEVERITIES else "none"
    if not result["alerts"]:
        sev = "none"
    elif sev == "none":
        sev = "medium"
    result["severity"] = sev

    conf = str(result.get("confidence", "medium")).lower()
    result["confidence"] = conf if conf in CONFIDENCES else "medium"

    if yolo_people is not None:
        result["yolo_people"] = yolo_people
        result["people_mismatch"] = people != yolo_people
    result["schema"] = 2
    return result
