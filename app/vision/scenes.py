import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import yaml

from app.vision import arming

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
    weekday_night: Optional[tuple] = None   # horario nocturno propio de lunes a viernes
    weekend_mode: Optional[str] = None      # "note" = fin de semana sin alerta nocturna
    known_pets: list = field(default_factory=list)
    cam_night: Optional[tuple] = None       # horario nocturno propio de la camara (todos los dias)
    known_exempt: frozenset = frozenset()    # tipos que NO alertan si hay una persona conocida en cuadro

    def _rule(self, now: Optional[float] = None) -> tuple:
        lt = time.localtime(now or time.time())
        weekend = lt.tm_wday >= 5
        if arming.is_absolute():
            return True, "alert", weekend            # armado absoluto: cualquier persona, a cualquier hora, es alerta
        if weekend and self.weekend_mode == "alert_all":
            return True, "alert", True
        base = self.cam_night or self.night_hours
        a, b = (self.weekday_night if (self.weekday_night and not weekend) else base)
        h = lt.tm_hour
        night = (h >= a or h < b) if a > b else (a <= h < b)
        mode = self.weekend_mode if (weekend and self.weekend_mode) else self.night_person
        return night, mode, weekend

    def is_night(self, now: Optional[float] = None) -> bool:
        return self._rule(now)[0]

    def night_alert(self, now: Optional[float] = None) -> bool:
        night, mode, _ = self._rule(now)
        return night and mode == "alert"

    def render(self, now: Optional[float] = None, video: bool = False) -> str:
        now = now or time.time()
        night, mode, weekend = self._rule(now)
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
        if self.known_pets:
            lines.append("MASCOTAS CONOCIDAS (son de la casa, NO son alerta animal; nombralas como mascota): "
                         + "; ".join(self.known_pets))
        if weekend and self.weekend_mode == "note":
            lines.append("FIN DE SEMANA: el uso casual de esta area es normal; no generes persona_nocturna.")
        if weekend and self.weekend_mode == "alert_all":
            lines.append("FIN DE SEMANA: aqui no deberia haber actividad; cualquier persona visible es alerta persona_nocturna.")
        if night and mode == "alert":
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
        from app.vision import zones
        fixed = zones.notes(self.cam_id)
        if fixed:
            head += "\nELEMENTOS FIJOS DE ESTA ESCENA (siempre estan ahi: NO los describas ni alertes por ellos):\n" + \
                "\n".join(f"- {n + ': ' if n else ''}{t}" for n, t in fixed)
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
                weekday_night=tuple(c["weekday_night"]) if c.get("weekday_night") else None,
                weekend_mode=c.get("weekend_mode"),
                known_pets=list(raw.get("known_pets") or []),
                cam_night=tuple(c["night_hours"]) if c.get("night_hours") else None,
                known_exempt=frozenset(c.get("known_exempt") or []),
            )
        logger.info("scenes: %d camaras con contexto de escena", len(self._by_cam))

    def get(self, cam_id: str) -> Optional[SceneContext]:
        return self._by_cam.get(cam_id)


def _clip(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


# lo fija main.py: cam_id -> nombres de personas conocidas en cuadro ahora
KNOWN_PRESENT = None
PETS_KNOWN = None          # callable(cam_id) -> bool: los animales en cuadro son mascotas de la casa (clasificador de mascotas)


def normalize_result(result: dict, scene: SceneContext, yolo_people: Optional[int] = None,
                     posture: Optional[dict] = None) -> dict:
    """Valida y sanea la respuesta del VLM contra el esquema por camara."""
    if not isinstance(result, dict) or result.get("activity") == "error":
        return result

    for k in ("activity", "scene"):
        if isinstance(result.get(k), str):
            result[k] = re.sub(r"\?{2,}", "…", result[k]).strip()
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
    person_types = {"persona_nocturna", "persona_en_suelo", "merodeo", "acceso_no_autorizado", "grupo_inusual",
                    "manipulacion_vehiculo", "uso_telefono_exterior"}
    if people == 0 and any(t in person_types for t in types):
        types = [t for t in types if t not in person_types]   # alerta de persona sin persona visible
        if not types:
            alerts = []
    if "animal" in types and PETS_KNOWN is not None:
        try:
            if PETS_KNOWN(scene.cam_id):
                types.remove("animal")
                result["pet_known"] = True
                if not types:
                    alerts = []
        except (TypeError, ValueError, KeyError):
            pass
    if "animal" in types and scene.known_pets:
        blob = (act + " " + " ".join(alerts)).lower()
        if re.search(r"mascota|akamaru|mojo|gigi|shar|shiba|perro negro peque|perros? (de la casa|conocid)", blob):
            types.remove("animal")
            if not types:
                alerts = []
    objs = result.get("yolo_objects") if isinstance(result.get("yolo_objects"), dict) else {}
    if (objs.get("bicicleta") or objs.get("moto")) and re.search(r"yace|tumbad|tirad|en el suelo|acostad", (act + " " + " ".join(alerts)).lower()):
        types = [t for t in types if t not in ("persona_en_suelo", "merodeo")]       # una persona con bicicleta/moto vista desde arriba parece "tirada"
        result["bike_in_scene"] = True
        if not types:
            alerts = []
    if "persona_en_suelo" in types:
        no_person = yolo_people == 0
        not_lying = posture is not None and posture.get("max_aspect", 1.0) < 0.8
        if no_person or not_lying:
            types.remove("persona_en_suelo")
            result["floor_unconfirmed"] = True
            if not types:
                alerts = []
    if "persona_nocturna" in types and not scene.night_alert():
        types.remove("persona_nocturna")
        if not types:
            alerts = []
    if scene.known_exempt and types and KNOWN_PRESENT is not None and not arming.is_absolute():
        try:
            known = KNOWN_PRESENT(scene.cam_id)
        except Exception:
            known = []
        if known:
            result["known_people"] = known
            kept = [t for t in types if t not in scene.known_exempt]
            if len(kept) != len(types):
                types = kept
                if not types:
                    alerts = []

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
