"""Viajes disparados por eventos: una puerta que se abre o una visita en el videoportero abre una ventana en la que se registra TODO el recorrido.

Entrada por el garage: calle (Exterior 1/2) -> garage / Tuya del garage -> escaleras o Site. Salida: al reves. Videoportero: timbre -> Escaleras entrada o Site.
Mientras la sesion esta abierta las camaras del recorrido suben el fps de YOLO. Al cerrar (o al vencer la ventana) se reunen las visitas de esas
camaras, se agrupan por persona (los viajes que ya enlaza el sistema), se infiere la direccion y se guarda una alerta con el mosaico del recorrido.
Configuracion en config/topology.yml, clave `trips`."""
import json
import logging
import os
import threading
import time
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)
ENABLED = os.environ.get("TRIPS_ENABLED", "true").lower() == "true"
PRE_S = float(os.environ.get("TRIP_PRE_S", "30"))           # visitas que ya estaban en cuadro justo antes del disparo
MIN_PATH_S = float(os.environ.get("TRIP_MIN_PATH_S", "8"))          # duracion minima de un recorrido que pasa por una sola camara
CHAIN_GAP_S = float(os.environ.get("TRIP_CHAIN_GAP_S", "150"))   # una visita de calle cuenta si ocurre hasta tanto antes/despues de la primera/ultima de adentro

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL NOT NULL, end_ts REAL, alias TEXT, label TEXT, direction TEXT,
  n_visits INTEGER NOT NULL DEFAULT 0, journey_id INTEGER, event_id INTEGER, status TEXT NOT NULL DEFAULT 'open');
CREATE INDEX IF NOT EXISTS idx_trips_start ON trips(start_ts);
"""


class Trips:
    def __init__(self, db, snapshots, topo: dict, cam_names: dict):
        self.db, self.snapshots, self.cam_names = db, snapshots, cam_names
        cfg = (topo or {}).get("trips") or {}
        self.window_s = float(cfg.get("window_s", 300))
        self.triggers = cfg.get("triggers") or {}
        self.street = set((topo or {}).get("street") or [])
        self._active: list = []
        self._lock = threading.Lock()
        with db._lock:
            db._conn.executescript(_SCHEMA)
            db._conn.commit()

    # ── disparo ────────────────────────────────────────────────────────
    def start(self, alias: str, ts: Optional[float] = None) -> Optional[int]:
        trg = self.triggers.get(alias)
        if not ENABLED or not trg:
            return None
        ts = ts or time.time()
        with self._lock:
            for s in self._active:
                if s["alias"] == alias and ts - s["start"] < float(trg.get("min_gap_s", 20)):          # el mismo evento repetido
                    return s["id"]
            with self.db._lock:
                cur = self.db._conn.execute("INSERT INTO trips (start_ts, alias, label) VALUES (?,?,?)", (ts, alias, trg.get("label") or alias))
                self.db._conn.commit()
            sess = {"id": cur.lastrowid, "alias": alias, "start": ts, "end": ts + float(trg.get("window_s", self.window_s)),
                    "cams": list(trg.get("cams") or []), "inner": list(trg.get("inner") or []), "label": trg.get("label") or alias, "quiet": bool(trg.get("quiet_if_empty"))}
            self._active.append(sess)
        logger.info("viaje %d abierto por %s (%d camaras, %.0f s)", sess["id"], alias, len(sess["cams"]), sess["end"] - ts)
        return sess["id"]

    def attending(self, cam_id: str) -> bool:
        """True si alguna sesion abierta sigue esta camara (sube el fps de YOLO)."""
        now = time.time()
        return any(cam_id in s["cams"] and now < s["end"] for s in self._active)

    def cams_for(self, alias: str) -> list:
        return list((self.triggers.get(alias) or {}).get("cams") or [])

    # ── cierre ─────────────────────────────────────────────────────────
    def sweep(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self._lock:
            done = [s for s in self._active if now >= s["end"]]
            self._active = [s for s in self._active if now < s["end"]]
        for s in done:
            try:
                self._finish(s)
            except Exception as exc:        # noqa: BLE001 - un viaje con error no debe frenar a los demas
                logger.warning("viaje %s: %s", s["id"], exc)

    def _visits(self, s: dict) -> list:
        q = ",".join("?" * len(s["cams"]))
        with self.db._lock:
            return self.db._conn.execute(
                f"SELECT id, cam_id, start_ts, end_ts, body, scene, subject_id, known_name, journey_id FROM person_visits WHERE cam_id IN ({q}) AND fp=0 AND static=0 "
                f"AND start_ts<=? AND end_ts>=? ORDER BY start_ts", (*s["cams"], s["end"], s["start"] - PRE_S)).fetchall()

    def _direction(self, keep: list) -> str:
        out = lambda v: v[1] in self.street or v[1] == "cam-vto"
        first_out, last_out = out(keep[0]), out(keep[-1])
        return "entrada" if first_out and not last_out else "salida" if last_out and not first_out else "recorrido"

    def _paths(self, s: dict, vs: list) -> list:
        """Recorridos por persona: se respetan los viajes que el enlazador ya arma (cuerpo, ropa, rostro); varias personas a la vez no se funden en una."""
        groups: dict = {}
        for v in vs:
            groups.setdefault(v[8] if v[8] is not None else ("v", v[0]), []).append(v)
        paths = []
        for g in groups.values():
            inner = [v for v in g if v[1] in s["inner"]]
            if not inner:
                continue                                      # solo calle: es gente que pasa, no un recorrido hacia adentro
            t0, t1 = inner[0][2], max(v[3] for v in inner)
            keep = sorted([v for v in g if v[1] in s["inner"] or t0 - CHAIN_GAP_S <= v[3] <= t0 or t1 <= v[2] <= t1 + CHAIN_GAP_S], key=lambda v: v[2])
            if len({v[1] for v in keep}) < 2 and sum(v[3] - v[2] for v in keep) < MIN_PATH_S:
                continue                                      # una sola camara y de segundos: parpadeo, no un recorrido
            paths.append(keep)
        return sorted(paths, key=lambda p: p[0][2])

    def _text(self, keep: list) -> str:
        names = [self.cam_names.get(v[1], v[1]) for v in keep]
        return " > ".join(n for i, n in enumerate(names) if i == 0 or n != names[i - 1])

    def _finish(self, s: dict) -> None:
        paths = self._paths(s, self._visits(s))
        flat = sorted({v[0]: v for p in paths for v in p}.values(), key=lambda v: v[2])
        if not paths and s.get("quiet"):                  # timbre sin nadie que entre: no se alerta (solo queda el registro del viaje)
            with self.db._lock:
                self.db._conn.execute("UPDATE trips SET end_ts=?, direction=?, n_visits=0, status='closed' WHERE id=?", (s["end"], "sin personas captadas", s["id"]))
                self.db._conn.commit()
            return
        dirs = [self._direction(p) for p in paths]
        if not paths:
            direction, txt = "sin personas captadas", f"{s['label']}: sin personas captadas · ninguna cámara vio a alguien"
        elif len(paths) == 1:
            direction = dirs[0]
            txt = f"{s['label']}: {direction} · {self._text(paths[0])}"
        else:
            pri = [(d, p) for d, p in zip(dirs, paths) if d != "recorrido"]      # los que vienen de la calle o van a ella primero; el resto es movimiento interno
            shown = pri[:3] if pri else list(zip(dirs, paths))[:3]
            direction = pri[0][0] if len(pri) == 1 else "varios"
            rest = len(paths) - len(shown)
            txt = (f"{s['label']}: " + " | ".join(f"{d}: {self._text(p)}" for d, p in shown)
                   + (f" | +{rest} movimientos más dentro" if rest > 0 else ""))
        who = sorted({v[7] for v in flat if v[7]})
        if who:
            txt += " · " + ", ".join(who)
        jid = next((v[8] for v in flat if v[8] is not None), None)
        sev = "medium" if (not paths or "entrada" in dirs) and not who else "low"
        first = [p[0] for p in paths]                      # una miniatura por recorrido
        eid = self.db.insert_event("nemotron", (flat[0][1] if flat else (s["cams"] or [None])[0]),
                                   {"schema": 2, "source": "viaje", "people": len(paths), "persons": [], "vehicles": 0, "activity": txt, "scene": "",
                                    "relevant": True, "alerts": [txt], "alert_types": ["viaje_puerta"], "severity": sev, "confidence": "high",
                                    "trip": {"trip_id": s["id"], "alias": s["alias"], "direction": direction, "journey_id": jid, "visits": [v[0] for v in flat],
                                             "paths": [[v[0] for v in p] for p in paths]}}, people=len(paths), has_alert=True)
        mosaic = self._mosaic(first if len(paths) > 1 else paths[0] if paths else [])
        if mosaic is not None and self.snapshots is not None:
            try:
                self.snapshots.save(flat[0][1], mosaic, "viaje", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("viaje %s: sin captura: %s", s["id"], exc)
        with self.db._lock:
            self.db._conn.execute("UPDATE trips SET end_ts=?, direction=?, n_visits=?, journey_id=?, event_id=?, status='closed' WHERE id=?",
                                  (s["end"], direction, len(flat), jid, eid, s["id"]))
            self.db._conn.commit()
        logger.info("viaje %d cerrado: %s", s["id"], txt)

    def _mosaic(self, keep: list):
        tiles = []
        for v in keep[:6]:
            img = v[5] or v[4]
            im = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR) if img else None
            if im is None:
                continue
            h, w = im.shape[:2]
            k = 360 / max(h, w)
            im = cv2.resize(im, (max(1, int(w * k)), max(1, int(h * k))))
            t = np.zeros((400, 360, 3), np.uint8)
            t[:im.shape[0], :im.shape[1]] = im
            cv2.rectangle(t, (0, 372), (359, 399), (0, 0, 0), -1)
            cv2.putText(t, f"{time.strftime('%H:%M:%S', time.localtime(v[2]))} {self.cam_names.get(v[1], v[1])}"[:30], (6, 392), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1, cv2.LINE_AA)
            tiles.append(t)
        if not tiles:
            return None
        while len(tiles) % 3 and len(tiles) > 3:
            tiles.append(np.zeros_like(tiles[0]))
        cols = min(3, len(tiles))
        return np.vstack([np.hstack(tiles[i:i + cols] + [np.zeros_like(tiles[0])] * (cols - len(tiles[i:i + cols]))) for i in range(0, len(tiles), cols)])

    def start_sweeper(self) -> threading.Event:
        stop = threading.Event()

        def loop():
            while not stop.wait(10):
                self.sweep()
        threading.Thread(target=loop, daemon=True, name="trips").start()
        return stop
