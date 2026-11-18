"""Escenas (grupos de órdenes a Home Assistant y Tuya) y horarios que las disparan a una hora fija o al amanecer/atardecer.

Escena:  {"id", "name", "actions": [...]}, cada acción es una de:
  {"t": "ha", "domain", "service", "entity_id", "data": {...}}
  {"t": "tuya", "device_id", "code", "value"}
  {"t": "focos", "ids": "all" | [ids], "action": {on, brightness, colour, white}}
  {"t": "espera", "s": 1-60}
Horario: {"id", "name", "when": "HH:MM" | "atardecer" | "amanecer" (con +N / -N minutos), "days": [0-6] (0 = lunes),
          "scene_id", "enabled", "last_run"}
"""
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from patcc import files

SCENES = "escenas.json"
SCHEDULES = "horarios.json"
WHEN = re.compile(r"^(?:(?P<hh>[01]\d|2[0-3]):(?P<mm>[0-5]\d)|(?P<sun>amanecer|atardecer)(?P<off>[+-]\d{1,3})?)$")
LATE_S = 180                   # si el panel estaba caído, un horario se ejecuta hasta 3 min tarde; después se salta ese día
MAX_ACTIONS = 40


def _id() -> str:
    return uuid.uuid4().hex[:10]


def validate_action(a: dict) -> dict:
    t = (a or {}).get("t")
    if t == "ha":
        dom, svc, ent = str(a.get("domain") or ""), str(a.get("service") or ""), a.get("entity_id")
        if not dom or not svc or not ent:
            raise ValueError("acción de Home Assistant incompleta")
        data = a.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("data debe ser un objeto")
        return {"t": "ha", "domain": dom, "service": svc, "entity_id": ent, "data": data}
    if t == "tuya":
        return {"t": "tuya", "device_id": str(a.get("device_id") or ""), "code": str(a.get("code") or ""), "value": a.get("value")}
    if t == "focos":
        ids = a.get("ids", "all")
        act = a.get("action") or {}
        if not isinstance(act, dict) or not act:
            raise ValueError("acción de focos vacía")
        return {"t": "focos", "ids": ids if ids == "all" else [str(i) for i in (ids or [])], "action": act}
    if t == "espera":
        s = float(a.get("s") or 0)
        if not 0 < s <= 60:
            raise ValueError("espera de 1 a 60 segundos")
        return {"t": "espera", "s": s}
    raise ValueError(f"tipo de acción desconocido: {t}")


def validate_scene(s: dict) -> dict:
    name = str((s or {}).get("name") or "").strip()[:60]
    if not name:
        raise ValueError("la escena necesita nombre")
    acts = (s or {}).get("actions") or []
    if not isinstance(acts, list) or not acts or len(acts) > MAX_ACTIONS:
        raise ValueError(f"la escena necesita de 1 a {MAX_ACTIONS} acciones")
    return {"id": str(s.get("id") or _id()), "name": name, "icon": str(s.get("icon") or "")[:4], "actions": [validate_action(a) for a in acts]}


def validate_schedule(h: dict, scene_ids: set) -> dict:
    when = str((h or {}).get("when") or "").strip().lower()
    if not WHEN.match(when):
        raise ValueError("hora inválida: HH:MM, amanecer o atardecer (con +N o -N minutos)")
    days = sorted({int(d) for d in (h.get("days") or range(7)) if 0 <= int(d) <= 6})
    if not days:
        raise ValueError("elige al menos un día")
    if h.get("scene_id") not in scene_ids:
        raise ValueError("la escena no existe")
    return {"id": str(h.get("id") or _id()), "name": str(h.get("name") or "").strip()[:60] or when, "when": when, "days": days,
            "scene_id": h["scene_id"], "enabled": bool(h.get("enabled", True)), "last_run": h.get("last_run")}


def target_today(when: str, now: datetime, sun: dict | None):
    """Hora de hoy (datetime local, sin zona) en que toca el horario, o None si no se puede saber (sin datos del sol)."""
    m = WHEN.match(when)
    if not m:
        return None
    if m.group("hh"):
        return now.replace(hour=int(m.group("hh")), minute=int(m.group("mm")), second=0, microsecond=0)
    key = "next_setting" if m.group("sun") == "atardecer" else "next_rising"
    iso = (sun or {}).get(key)
    if not iso:
        return None
    try:
        ev = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None
    ev = datetime.combine(now.date(), ev.time())        # Home Assistant da el próximo (hoy o mañana); entre días cambia ~1 min
    return ev + timedelta(minutes=int(m.group("off") or 0))


def due(h: dict, now: datetime, sun: dict | None) -> bool:
    if not h.get("enabled") or now.weekday() not in h.get("days", []):
        return False
    t = target_today(h["when"], now, sun)
    if t is None:
        return False
    key = t.strftime("%Y-%m-%d %H:%M")
    return t <= now and (now - t).total_seconds() <= LATE_S and h.get("last_run") != key


class Automations:
    def __init__(self, ha, tuya):
        self.ha, self.tuya = ha, tuya
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ── datos ──────────────────────────────────────────────────────
    def scenes(self) -> list:
        return files.load(SCENES, []) or []

    def schedules(self) -> list:
        return files.load(SCHEDULES, []) or []

    def save_scene(self, s: dict) -> dict:
        s = validate_scene(s)
        with self._lock:
            lst = [x for x in self.scenes() if x["id"] != s["id"]] + [s]
            files.save(SCENES, lst)
        return s

    def delete_scene(self, sid: str) -> bool:
        with self._lock:
            lst = self.scenes()
            if not any(x["id"] == sid for x in lst):
                return False
            files.save(SCENES, [x for x in lst if x["id"] != sid])
            files.save(SCHEDULES, [h for h in self.schedules() if h.get("scene_id") != sid])     # sus horarios ya no tienen qué ejecutar
        return True

    def save_schedule(self, h: dict) -> dict:
        h = validate_schedule(h, {s["id"] for s in self.scenes()})
        with self._lock:
            old = next((x for x in self.schedules() if x["id"] == h["id"]), None)
            if old:
                h["last_run"] = old.get("last_run")
            files.save(SCHEDULES, [x for x in self.schedules() if x["id"] != h["id"]] + [h])
        return h

    def delete_schedule(self, hid: str) -> bool:
        with self._lock:
            lst = self.schedules()
            if not any(x["id"] == hid for x in lst):
                return False
            files.save(SCHEDULES, [x for x in lst if x["id"] != hid])
        return True

    # ── ejecución ──────────────────────────────────────────────────
    def run_scene(self, sid: str, who: str) -> list:
        sc = next((s for s in self.scenes() if s["id"] == sid), None)
        if sc is None:
            raise KeyError(sid)
        results, last_dev = [], {}
        for a in sc["actions"]:
            try:
                if a["t"] == "ha":
                    self.ha.call(a["domain"], a["service"], {**a.get("data", {}), "entity_id": a["entity_id"]})
                    results.append({"ok": True, "what": f'{a["domain"]}.{a["service"]} {a["entity_id"]}'})
                elif a["t"] == "tuya":
                    wait = 1.1 - (time.time() - last_dev.get(a["device_id"], 0))
                    if wait > 0:
                        time.sleep(wait)                      # el servicio Tuya rechaza dos órdenes al mismo equipo en menos de 1 s
                    r = self.tuya.control(a["device_id"], a["code"], a["value"])
                    last_dev[a["device_id"]] = time.time()
                    results.append({"ok": bool(r.get("ok")), "what": f'{a["device_id"]} {a["code"]}={a["value"]}', "detail": r.get("result")})
                elif a["t"] == "focos":
                    r = self.tuya.group(a["ids"], a["action"])
                    results.append({"ok": bool(r.get("ok")), "what": "focos", "detail": r.get("result")})
                elif a["t"] == "espera":
                    time.sleep(a["s"])
            except Exception as exc:          # noqa: BLE001 - una orden fallida no detiene el resto de la escena
                results.append({"ok": False, "what": a.get("t"), "detail": str(exc)[:160]})
        ok = sum(1 for r in results if r["ok"])
        files.log(who, f"escena «{sc['name']}»", f"{ok} de {len(results)} órdenes")
        return results

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="patcc-horarios").start()

    def _sun(self):
        try:
            st = self.ha.state("sun.sun")
            return (st or {}).get("attributes")
        except Exception:                     # noqa: BLE001 - sin Home Assistant los horarios por hora fija siguen funcionando
            return None

    def tick(self, now: datetime | None = None) -> list:
        now = now or datetime.now()
        lst = self.schedules()
        sun = self._sun() if any(not h["when"][:1].isdigit() for h in lst) else None
        fired = []
        for h in lst:
            if not due(h, now, sun):
                continue
            t = target_today(h["when"], now, sun)
            h["last_run"] = t.strftime("%Y-%m-%d %H:%M")
            with self._lock:
                cur = self.schedules()
                for x in cur:
                    if x["id"] == h["id"]:
                        x["last_run"] = h["last_run"]
                files.save(SCHEDULES, cur)
            try:
                self.run_scene(h["scene_id"], f"horario «{h['name']}»")
                fired.append(h["id"])
            except KeyError:
                files.log("horarios", h["name"], "la escena ya no existe")
        return fired

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                  # noqa: BLE001 - el ciclo de horarios nunca debe morir
                files.log("horarios", "error", str(exc)[:200])
            self._stop.wait(20)

    def next_run(self, now: datetime | None = None):
        now = now or datetime.now()
        sun = self._sun()
        best = None
        for h in self.schedules():
            if not h.get("enabled"):
                continue
            for k in range(8):
                d = now + timedelta(days=k)
                if d.weekday() not in h["days"]:
                    continue
                t = target_today(h["when"], d, sun)
                if t is None or t <= now:
                    continue
                if best is None or t < best[0]:
                    best = (t, h)
                break
        return {"name": best[1]["name"], "at": best[0].strftime("%a %d %H:%M")} if best else None
