"""Consolidacion de identidades: une los "Persona #N" que son la misma persona.

Cada visita se compara con el promedio de cada sujeto al momento de verla; los sujetos nunca se comparan entre si, asi que la misma persona
queda partida en varios "Persona #N" (429 de 561 sujetos de una semana tenian una sola visita). Esta tarea, de noche:
  1. compara los promedios de rostro (InsightFace) de todos los sujetos con embedding;
  2. los pares parecidos (coseno >= MIN_COS) van a Clef con la mejor cara de cada uno (misma regla de dos etapas que la identidad en vivo);
  3. Clef >= AUTO_P: se fusionan; entre SUGGEST_P y AUTO_P: quedan como sugerencia en /identidades.
Candados: nunca se fusionan dos personas con nombre; un sujeto sin nombre se une al que tiene nombre o al que tiene mas visitas.
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("CONSOLIDATE_ENABLED", "true").lower() == "true"
MIN_COS = float(os.environ.get("CONSOLIDATE_MIN_COS", "0.30"))
AUTO_P = float(os.environ.get("CONSOLIDATE_AUTO_P", "0.85"))
AUTO_MIN_COS = float(os.environ.get("CONSOLIDATE_AUTO_MIN_COS", "0.40"))   # para fusionar solo, InsightFace tambien debe estar de acuerdo (21-oct: el unico error
                                                                             # visible entre 19 fusiones, Dona Vicky con Dinah, tenia coseno 0.375)
SUGGEST_P = float(os.environ.get("CONSOLIDATE_SUGGEST_P", "0.60"))
MAX_CALLS = int(os.environ.get("CONSOLIDATE_MAX_CALLS", "400"))     # llamadas a Clef por noche
HOUR = int(os.environ.get("CONSOLIDATE_HOUR", "3"))                 # hora local en que corre


def _subjects(db) -> list:
    with db._lock:
        rows = db._conn.execute(
            "SELECT s.id, s.named, s.embedding, (SELECT COUNT(*) FROM person_visits v WHERE v.subject_id=s.id), "
            "(SELECT v.face FROM person_visits v WHERE v.subject_id=s.id AND v.face IS NOT NULL ORDER BY v.face_score DESC LIMIT 1) "
            "FROM subjects s WHERE s.embedding IS NOT NULL").fetchall()
    out = []
    for sid, named, emb, nvis, face in rows:
        e = np.frombuffer(emb, np.float32)
        if e.size and face:
            out.append({"id": sid, "named": bool(named), "emb": e / max(1e-6, float(np.linalg.norm(e))), "n": nvis, "face": face})
    return out


def candidates(subs: list, min_cos: float = MIN_COS) -> list:
    """Pares (cos, a, b) de sujetos parecidos, del mas parecido al menos; nunca dos con nombre."""
    if len(subs) < 2:
        return []
    m = np.stack([s["emb"] for s in subs])
    sim = m @ m.T
    out = []
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            if sim[i, j] >= min_cos and not (subs[i]["named"] and subs[j]["named"]):
                out.append((float(sim[i, j]), subs[i], subs[j]))
    out.sort(key=lambda t: -t[0])
    return out


def _target(a: dict, b: dict) -> tuple:
    """(origen, destino): el sin nombre se une al que tiene nombre o al que tiene mas visitas."""
    if a["named"]:
        return b, a
    if b["named"]:
        return a, b
    return (a, b) if a["n"] <= b["n"] else (b, a)


def run(db, manager, clef, dry: bool = True, max_calls: int = MAX_CALLS) -> dict:
    """Una pasada. dry=True no cambia nada: solo cuenta lo que haria."""
    subs = _subjects(db)
    pairs = candidates(subs)
    stats = {"subjects": len(subs), "pairs": len(pairs), "clef_calls": 0, "merged": 0, "suggested": 0, "rejected": 0, "skipped": 0, "dry": dry, "examples": []}
    gone: set = set()
    touched: set = set()                 # una fusion por sujeto por pasada: un error no se encadena a otras identidades
    for cos, a, b in pairs:
        if stats["clef_calls"] >= max_calls:
            break
        if a["id"] in gone or b["id"] in gone:
            stats["skipped"] += 1
            continue
        p = clef.same_person(a["face"], b["face"]) if clef is not None else None
        stats["clef_calls"] += 1
        if p is None:
            continue
        src, dst = _target(a, b)
        decision = "consolidado" if p >= AUTO_P else "sugerencia" if p >= SUGGEST_P else "no"
        if decision == "consolidado" and (cos < AUTO_MIN_COS or a["id"] in touched or b["id"] in touched):
            decision = "sugerencia"          # solo Clef, o el sujeto ya se fusiono en esta pasada: lo decide una persona
        if len(stats["examples"]) < 12 and decision != "no":
            stats["examples"].append({"from": src["id"], "into": dst["id"], "cos": round(cos, 3), "clef": round(p, 3), "decision": decision})
        if decision == "consolidado":
            stats["merged"] += 1
            touched.update((src["id"], dst["id"]))
            if not dry and manager is not None and manager.merge_subjects(src["id"], dst["id"]):
                gone.add(src["id"])
                dst["n"] += src["n"]
        elif decision == "sugerencia":
            stats["suggested"] += 1
        else:
            stats["rejected"] += 1
        if not dry and decision != "no":
            try:
                with db._lock:
                    db._conn.execute("CREATE TABLE IF NOT EXISTS identity_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, visit_id INTEGER, from_sid INTEGER, "
                                     "into_sid INTEGER, sim REAL, p_clef REAL, decision TEXT)")
                    db._conn.execute("INSERT INTO identity_checks (ts, visit_id, from_sid, into_sid, sim, p_clef, decision) VALUES (?,?,?,?,?,?,?)",
                                     (time.time(), None, src["id"], dst["id"], round(cos, 3), round(p, 3), decision))
                    db._conn.commit()
            except sqlite3.Error as exc:
                logger.warning("consolidacion: %s", exc)
    logger.info("consolidacion%s: %d sujetos, %d pares, %d a Clef: %d fusiones, %d sugerencias, %d descartados",
                " (prueba)" if dry else "", stats["subjects"], stats["pairs"], stats["clef_calls"], stats["merged"], stats["suggested"], stats["rejected"])
    return stats


def start_nightly(db, manager, clef) -> Optional[threading.Event]:
    if not ENABLED or clef is None:
        return None
    stop = threading.Event()

    def loop():
        last_day = None
        while not stop.wait(300):
            lt = time.localtime()
            if lt.tm_hour == HOUR and last_day != lt.tm_yday:
                last_day = lt.tm_yday
                try:
                    run(db, manager, clef, dry=False)
                except Exception as exc:          # noqa: BLE001 - la consolidacion es opcional: nunca debe tumbar el servicio
                    logger.warning("consolidacion: %s", exc)
    threading.Thread(target=loop, daemon=True, name="consolidate").start()
    return stop
