"""Viajes: seguimiento de una persona entre camaras y etiquetado retroactivo.

Un viaje agrupa las visitas de la misma persona a lo largo del recorrido (Garage -> Acceso Site -> Cocina...). Se enlaza cada visita cerrada con el viaje
mas compatible segun tiempo, cuerpo (ReID), ropa e identidad por rostro. Cuando cualquier visita del viaje tiene rostro identificado, las demas (de
espalda, sin cara) quedan como "probable <nombre>" para aceptarlas de una vez; nunca se asignan solas.
La camara Tuya del garage anticipa la entrada: su movimiento abre una ventana en la que se espera una visita en Garage frontal; si no llega, se
registra como entrada probable sin captura.
"""
import json
import logging
import math
import os
import sqlite3
import threading
import time
from typing import Optional

import numpy as np
import yaml

from app.vision import bodyid

logger = logging.getLogger(__name__)

TOPOLOGY = os.environ.get("TOPOLOGY_CONFIG", "/app/config/topology.yml")
PRE_S = float(os.environ.get("TUYA_PRE_S", "20"))       # un aviso de entrada puede llegar despues de que la camara ya vio a la persona
ENABLED = os.environ.get("JOURNEYS_ENABLED", "true").lower() == "true"
LINK_MIN = float(os.environ.get("JOURNEY_LINK_MIN", "0.6"))
EVERY_S = 20.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journeys (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL NOT NULL, last_ts REAL NOT NULL, last_cam TEXT, subject_id INTEGER,
  origin TEXT, tuya_lead_s REAL, n_visits INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open');
CREATE INDEX IF NOT EXISTS idx_journeys_last ON journeys(last_ts);
CREATE TABLE IF NOT EXISTS tuya_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, alias TEXT, cam_id TEXT, visit_id INTEGER, journey_id INTEGER, state TEXT NOT NULL DEFAULT 'waiting');
"""


def load_topology(path: str = TOPOLOGY) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("journeys: sin topologia (%s)", exc)
        return {}


def connected(topo: dict, a: str, b: str) -> bool:
    """Dos camaras estan conectadas si ambas estan en el mapa (inmueble o calle): el tiempo y el cuerpo deciden el enlace."""
    known = set(topo.get("building") or []) | set(topo.get("street") or [])
    return a == b or (a in known and b in known)


def time_score(gap: float) -> float:
    return 1.0 if gap <= 45 else max(0.0, 1.0 - (gap - 45) / 135.0)


def body_score(a: Optional[np.ndarray], embs: list) -> float:
    if a is None or not embs:
        return 0.0
    cos = max(float(a @ e) for e in embs)
    return float(min(1.0, max(0.0, (cos - 0.35) / 0.30)))


def link_score(topo: dict, j: dict, v: dict) -> Optional[float]:
    """Puntaje 0..1 de que la visita `v` continua el viaje `j`; None si es imposible (tiempo, camara o identidad distinta)."""
    gap = v["start_ts"] - j["last_ts"]
    same_cam = v["cam_id"] == j["last_cam"]
    if gap < (0 if same_cam else -8) or gap > (topo.get("fragment_gap_s", 30) if same_cam else topo.get("max_gap_s", 180)):
        return None
    if not connected(topo, j["last_cam"], v["cam_id"]):
        return None
    if v["subject_id"] is not None and j["subject_id"] is not None:
        return 1.0 if v["subject_id"] == j["subject_id"] else None
    colors = bodyid.compatible(v["attrs"], j["attrs"]) if (v["attrs"] and j["attrs"]) else None
    s_col = 0.5 if colors is None else (1.0 if colors else 0.0)
    return 0.45 * body_score(v["emb"], j["embs"]) + 0.35 * time_score(max(0.0, gap)) + 0.20 * s_col


class JourneyLinker:
    def __init__(self, db, snapshots=None):
        self.db, self.snapshots = db, snapshots
        self.topo = load_topology()
        self.stats = {"linked": 0, "new": 0, "ambiguous": 0, "retro": 0, "tuya_matched": 0, "tuya_orphan": 0}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        with db._lock:
            db._conn.executescript(_SCHEMA)
            try:
                db._conn.execute("ALTER TABLE person_visits ADD COLUMN journey_id INTEGER")
            except sqlite3.OperationalError:
                pass
            db._conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_journey ON person_visits(journey_id)")
            db._conn.commit()

    def start(self) -> None:
        if ENABLED:
            threading.Thread(target=self._loop, daemon=True, name="journeys").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(EVERY_S):
            try:
                self.run_once()
            except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                logger.warning("journeys: %s", exc, exc_info=True)

    # ---- Tuya: aviso de entrada
    def tuya_event(self, alias: str, ts: float) -> None:
        cam = self._entry_cams(alias)[:1]
        cam = cam[0] if cam else None
        if not cam or not ENABLED:
            return
        with self.db._lock:
            last = self.db._conn.execute("SELECT MAX(ts) FROM tuya_entries WHERE alias=?", (alias,)).fetchone()[0] or 0
            if ts - last < 20:
                return
            self.db._conn.execute("INSERT INTO tuya_entries (ts, alias, cam_id) VALUES (?,?,?)", (ts, alias, cam))
            self.db._conn.commit()

    def _entry_cams(self, alias: str) -> list:
        v = (self.topo.get("tuya_entries") or {}).get(alias)
        return [v] if isinstance(v, str) else list(v or [])

    def _resolve_tuya(self, now: float) -> None:
        win = float(self.topo.get("tuya_window_s", 60))
        with self.db._lock:
            waiting = self.db._conn.execute("SELECT id, ts, cam_id, alias FROM tuya_entries WHERE state='waiting'").fetchall()
        for tid, ts, cam, alias in waiting:
            cams = self._entry_cams(alias) or [cam]
            with self.db._lock:       # visitas que empiezan hasta `win` s despues del aviso, o que ya estaban en cuadro hasta 20 s antes
                v = self.db._conn.execute(
                    f"SELECT id FROM person_visits WHERE cam_id IN ({','.join('?' * len(cams))}) AND fp=0 AND static=0 AND start_ts<=? AND end_ts>=? ORDER BY start_ts LIMIT 1",
                    (*cams, ts + win, ts - PRE_S)).fetchone()
            if v:
                with self.db._lock:
                    self.db._conn.execute("UPDATE tuya_entries SET state='matched', visit_id=? WHERE id=?", (v[0], tid))
                    self.db._conn.commit()
                self.stats["tuya_matched"] += 1
            elif now - ts > win + 5:
                with self.db._lock:
                    self.db._conn.execute("UPDATE tuya_entries SET state='orphan' WHERE id=?", (tid,))
                    self.db._conn.commit()
                self.stats["tuya_orphan"] += 1
                if str(alias).startswith("tuya-"):
                    continue                     # el movimiento de una camara Tuya ya tiene su propia alerta (y suele ser una mascota o una sombra): sin persona no es una entrada sin captura
                nombres = ", ".join(self.cam_names.get(c) or c for c in cams) if getattr(self, "cam_names", None) else ", ".join(cams)
                payload = {"schema": 2, "source": "journeys", "people": 0, "persons": [], "vehicles": 0,
                           "activity": f"Aviso de {alias} sin persona en ninguna de las camaras asociadas ({nombres}): entrada probable sin captura",
                           "scene": "", "relevant": True, "alerts": [f"Entrada probable sin captura ({alias})"], "alert_types": ["entrada_sin_captura"],
                           "severity": "low", "confidence": "low", "tuya_entry_ts": ts}
                try:
                    self.db.insert_event("nemotron", cam, payload, people=0, has_alert=True)
                except sqlite3.Error as exc:
                    logger.warning("journeys: no se pudo guardar la entrada sin captura: %s", exc)

    # ---- enlace de visitas
    def _active(self, now: float) -> list:
        since = now - float(self.topo.get("max_gap_s", 180)) - 60
        with self.db._lock:
            js = self.db._conn.execute("SELECT id, last_ts, last_cam, subject_id FROM journeys WHERE last_ts>=? AND status='open'", (since,)).fetchall()
            out = []
            for jid, last_ts, last_cam, sid in js:
                vs = self.db._conn.execute("SELECT body_emb, attrs FROM person_visits WHERE journey_id=? AND body_emb IS NOT NULL ORDER BY start_ts DESC LIMIT 4", (jid,)).fetchall()
                embs, attrs = [], {}
                for blob, at in vs:
                    e = np.frombuffer(blob, np.float32)
                    if e.size:
                        embs.append(e)
                    if not attrs and at:
                        try:
                            attrs = json.loads(at)
                        except ValueError:
                            attrs = {}
                out.append({"id": jid, "last_ts": last_ts, "last_cam": last_cam, "subject_id": sid, "embs": embs, "attrs": attrs})
        return out

    def run_once(self, now: Optional[float] = None, window_s: float = 1800.0) -> int:
        now = now or time.time()
        self._resolve_tuya(now)
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT id, cam_id, start_ts, COALESCE(end_ts,start_ts), subject_id, body_emb, attrs FROM person_visits "
                "WHERE journey_id IS NULL AND status='closed' AND fp=0 AND static=0 AND start_ts>=? AND COALESCE(end_ts,start_ts)<=? ORDER BY start_ts",
                (now - window_s, now - 8)).fetchall()
        n = 0
        for vid, cam, a, b, sid, emb, at in rows:
            try:
                attrs = json.loads(at) if at else {}
            except ValueError:
                attrs = {}
            v = {"id": vid, "cam_id": cam, "start_ts": a, "end_ts": b, "subject_id": sid,
                 "emb": (np.frombuffer(emb, np.float32) if emb else None), "attrs": attrs}
            self._link(v)
            n += 1
        return n

    def _link(self, v: dict) -> None:
        cands = []
        for j in self._active(v["start_ts"] + 1):
            s = link_score(self.topo, j, v)
            if s is not None and s >= LINK_MIN:
                cands.append((s, j))
        cands.sort(key=lambda t: -t[0])
        jid = None
        if cands:
            best, jb = cands[0]
            second = cands[1] if len(cands) > 1 else None
            ambiguous = second is not None and second[0] >= best - 0.08 and best < 0.999
            if ambiguous:
                self.stats["ambiguous"] += 1               # dos personas igual de compatibles: no se enlaza a ciegas
            else:
                jid = jb["id"]
        with self._lock:
            if jid is None:
                origin = "entrada" if v["cam_id"] in (self.topo.get("entry_cams") or []) else "suelto"
                with self.db._lock:
                    lead = self.db._conn.execute("SELECT ts FROM tuya_entries WHERE cam_id=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                                                 (v["cam_id"], v["start_ts"] - float(self.topo.get("tuya_window_s", 60)), v["start_ts"])).fetchone()
                    cur = self.db._conn.execute("INSERT INTO journeys (start_ts, last_ts, last_cam, subject_id, origin, tuya_lead_s, n_visits) VALUES (?,?,?,?,?,?,1)",
                                                (v["start_ts"], v["end_ts"], v["cam_id"], v["subject_id"], "tuya" if lead else origin,
                                                 round(v["start_ts"] - lead[0], 1) if lead else None))
                    jid = cur.lastrowid
                    self.db._conn.execute("UPDATE person_visits SET journey_id=? WHERE id=?", (jid, v["id"]))
                    self.db._conn.commit()
                self.stats["new"] += 1
            else:
                with self.db._lock:
                    self.db._conn.execute("UPDATE journeys SET last_ts=MAX(last_ts,?), last_cam=?, n_visits=n_visits+1, subject_id=COALESCE(subject_id,?) WHERE id=?",
                                          (v["end_ts"], v["cam_id"], v["subject_id"], jid))
                    self.db._conn.execute("UPDATE person_visits SET journey_id=? WHERE id=?", (jid, v["id"]))
                    self.db._conn.commit()
                self.stats["linked"] += 1
        self._retro_label(jid)

    def _retro_label(self, jid: int) -> None:
        """Si el viaje tiene identidad por rostro, las visitas sin identidad quedan como 'probable' (sugerencia)."""
        with self.db._lock:
            row = self.db._conn.execute("SELECT j.subject_id, COALESCE(s.name,'Persona #'||s.id), s.named FROM journeys j LEFT JOIN subjects s ON s.id=j.subject_id WHERE j.id=?", (jid,)).fetchone()
            if not row or row[0] is None:
                return
            vs = self.db._conn.execute("SELECT id, attrs FROM person_visits WHERE journey_id=? AND subject_id IS NULL", (jid,)).fetchall()
            for vid, at in vs:
                try:
                    a = json.loads(at) if at else {}
                except ValueError:
                    a = {}
                if (a.get("journey") or {}).get("sid") == row[0]:
                    continue
                a["journey"] = {"jid": jid, "sid": row[0], "name": row[1]}
                self.db._conn.execute("UPDATE person_visits SET attrs=? WHERE id=?", (json.dumps(a, ensure_ascii=False), vid))
                self.stats["retro"] += 1
            self.db._conn.commit()
