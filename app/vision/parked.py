"""Registro de vehiculos estacionados en las camaras de calle: llegada, permanencia y (cuando se pueda) placa."""
import logging
import os
import sqlite3
import threading
import time
from collections import Counter, deque

import cv2

from app.vision import plates, zones

logger = logging.getLogger(__name__)

VEHICLE_CLASSES = {"auto", "moto", "camion", "autobus"}
CAMS = {c.strip() for c in os.environ.get("PARKED_CAMS", "cam-189,cam-191").split(",") if c.strip()}
MIN_PARKED_S = float(os.environ.get("PARKED_MIN_S", "180"))     # quieto este tiempo => estacionado
GONE_S = float(os.environ.get("PARKED_GONE_S", "900"))          # sin verse este tiempo => se fue
MIN_IOU = float(os.environ.get("PARKED_IOU", "0.40"))
MIN_AREA_FRAC = 0.0015                                           # ignora cajas diminutas (ruido lejano)
RETENTION_DAYS = int(os.environ.get("PARKED_RETENTION_DAYS", "90"))
ALERT_EVERY_S = float(os.environ.get("PARKED_ALERT_EVERY_S", "3600"))   # alerta recurrente mientras siga estacionado
PRESENCE_MIN = float(os.environ.get("PARKED_PRESENCE_MIN", "0.5"))     # para registrarlo, el vehiculo debe verse en al menos esta fraccion de los cuadros
PRESENCE_GONE = float(os.environ.get("PARKED_PRESENCE_GONE", "0.15"))  # registrado que en 10 min se ve en menos de esto: era un carril con trafico (fantasma)
PRESENCE_WIN_S = 600.0
PLATE_SLOW_S = float(os.environ.get("PLATES_SLOW_S", "600"))          # tras las primeras lecturas se sigue intentando, mas despacio, mientras siga ahi
LINK_DAYS = 30                                                         # una placa se compara con las de los ultimos dias para enlazar el mismo auto

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parked_vehicles (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  cam_id     TEXT NOT NULL,
  cls        TEXT NOT NULL,
  first_ts   REAL NOT NULL,
  last_ts    REAL NOT NULL,
  left_ts    REAL,
  state      TEXT NOT NULL DEFAULT 'parked',
  box        TEXT,
  frame_w    INTEGER,
  frame_h    INTEGER,
  thumb      BLOB,
  plate      TEXT,
  plate_conf REAL,
  note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_parked_state ON parked_vehicles(state, cam_id);
CREATE INDEX IF NOT EXISTS idx_parked_first ON parked_vehicles(first_ts);
"""


def _match(a, b) -> float:
    """Puntaje 0..1 de que dos cajas son el mismo vehiculo quieto (IoU, o centros cercanos y tamano parecido)."""
    v = _iou(a, b)
    wa, ha, wb, hb = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
    cd = (((a[0] + a[2]) - (b[0] + b[2])) ** 2 + ((a[1] + a[3]) - (b[1] + b[3])) ** 2) ** 0.5 / 2
    ratio = (wa * ha) / float(max(1, wb * hb))
    if cd < 0.30 * max(wa, wb) and 0.55 < ratio < 1.8:
        v = max(v, 0.6)
    return v


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if not inter:
        return 0.0
    return inter / float((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


class ParkedTracker:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._cand: dict = {}
        self._last_gc = 0.0
        self.cam_names: dict = {}       # cam_id -> nombre (lo fija main.py)
        self.analyzer = None            # VLMAnalyzer (lo fija main.py): lectura de placa de respaldo
        self.snapshots = None           # SnapshotManager (lo fija main.py): captura de cada alerta
        self._frames: dict = {}         # cam_id -> ultimo cuadro visto (para la alerta "se fue", que no llega con cuadro)
        with db._lock:
            db._conn.executescript(_SCHEMA)
            for col, typ in (("plate_src", "TEXT"), ("plate_img", "BLOB"), ("in_zone", "INTEGER NOT NULL DEFAULT 1"),
                             ("plate_guess", "TEXT"), ("plate_guess_conf", "REAL"), ("merged_into", "INTEGER"), ("twin_id", "INTEGER")):
                try:
                    db._conn.execute(f"ALTER TABLE parked_vehicles ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass               # ya existia
            # lecturas automaticas poco seguras (umbral anterior) pasan a "por confirmar"; las del VLM tambien, salvo que el OCR coincida
            db._conn.execute("UPDATE parked_vehicles SET plate_guess=plate, plate_guess_conf=plate_conf, plate=NULL, plate_conf=NULL "
                             "WHERE plate IS NOT NULL AND ((plate_src='auto' AND COALESCE(plate_conf,0)<0.7) OR plate_src='vlm')")
            db._conn.commit()
            rows = db._conn.execute("SELECT id, cam_id, cls, first_ts, last_ts, box FROM parked_vehicles WHERE state='parked'").fetchall()
        for rid, cam, cls, first, last, box in rows:
            try:
                b = tuple(int(v) for v in box.split(","))
            except Exception:
                continue
            self._cand.setdefault(cam, []).append({"cls": Counter({cls: 5}), "anchor": b, "first": first, "last": last,
                                                   "hits": 99, "rec": rid, "saved": last,
                                                   "hours": int((last - first) // ALERT_EVERY_S)})
        self._ftimes: dict = {}         # cam_id -> momentos de cada cuadro analizado (para la presencia)
        logger.info("ParkedTracker: camaras=%s min=%.0fs, %d vehiculos abiertos recuperados", sorted(CAMS), MIN_PARKED_S, len(rows))
        try:
            self.relink_all()
        except sqlite3.Error as exc:
            logger.warning("ParkedTracker: enlace de placas: %s", exc)

    @staticmethod
    def _thumb(frame, box, max_side=360):
        try:
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = box
            pw, ph = int((x2 - x1) * .08), int((y2 - y1) * .08)
            crop = frame[max(0, y1 - ph):min(h, y2 + ph), max(0, x1 - pw):min(w, x2 + pw)]
            k = max_side / max(crop.shape[:2])
            if k < 1.0:
                crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 82])
            return buf.tobytes() if ok else None
        except Exception:
            return None

    def _presence(self, cam_id, c, now, win=PRESENCE_WIN_S) -> tuple:
        """(fraccion de cuadros de la camara en los que se vio este vehiculo, cuadros) en la ventana; un auto estacionado sale en casi todos,
        un carril con trafico solo cuando pasa alguien."""
        start = max(now - win, c["first"], c.get("mt_start", c["first"]))   # solo el tiempo observado en este proceso (tras reiniciar no hay historial)
        ft = self._ftimes.get(cam_id) or ()
        n = sum(1 for t in ft if t >= start)
        m = sum(1 for t in c.get("mt") or () if t >= start)
        return (m / n if n else 1.0), n

    def update(self, cam_id, result, frame, ts=None) -> None:
        if cam_id not in CAMS:
            return
        now = ts or time.time()
        self._frames[cam_id] = frame
        self._ftimes.setdefault(cam_id, deque(maxlen=4000)).append(now)
        fh, fw = frame.shape[:2]
        rw, rh = getattr(result, "frame_w", 0) or fw, getattr(result, "frame_h", 0) or fh
        sx, sy = fw / rw, fh / rh
        dets = []
        for o in getattr(result, "objects", None) or []:
            if o["c"] in VEHICLE_CLASSES:
                x1, y1, x2, y2 = o["b"]
                b = (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
                if (b[2] - b[0]) * (b[3] - b[1]) >= MIN_AREA_FRAC * fw * fh:
                    inz = (not zones.has(cam_id, "estacionamiento")) or zones.contains(cam_id, ("estacionamiento",), b, fw, fh)
                    dets.append((o["c"], b, inz))       # fuera del poligono: se registra como informativo (sin alertas ni placa)
        with self._lock:
            cands = self._cand.setdefault(cam_id, [])
            used = set()
            for cls, b, inz in dets:
                best, bi = 0.0, None
                for i, c in enumerate(cands):
                    if i in used:
                        continue
                    v = _match(c["anchor"], b)
                    if v > best:
                        best, bi = v, i
                if bi is not None and best >= MIN_IOU:
                    c = cands[bi]
                    used.add(bi)
                    c["last"] = now
                    c["anchor"] = tuple(int(0.85 * p + 0.15 * q) for p, q in zip(c["anchor"], b))
                    c["hits"] += 1
                    c["zin"] = c.get("zin", 0) + (1 if inz else 0)
                    c["cls"][cls] += 1
                    if "mt" not in c:
                        c["mt"], c["mt_start"] = deque(maxlen=4000), now          # recuperado de la base al arrancar: se empieza a medir aqui
                    c["mt"].append(now)
                    self._maybe_register(cam_id, c, frame, fw, fh, now)
                    self._maybe_read_plate(c, frame, fw, fh, now)
                else:
                    cands.append({"cls": Counter({cls: 1}), "anchor": b, "first": now, "last": now,
                                  "hits": 1, "rec": None, "saved": 0.0, "zin": 1 if inz else 0, "mt": deque([now], maxlen=4000), "mt_start": now})
            self._gc(cam_id, now)

    def _maybe_register(self, cam_id, c, frame, fw, fh, now) -> None:
        if c["rec"] is None:
            if now - c["first"] >= MIN_PARKED_S and c["hits"] >= 3:
                pres, n = self._presence(cam_id, c, now, MIN_PARKED_S)
                if n >= 10 and pres < PRESENCE_MIN:
                    return                   # se ve a ratos: autos distintos pasando por el mismo lugar, no uno estacionado
                cls = c["cls"].most_common(1)[0][0]
                b = c["anchor"]
                for other in self._cand.get(cam_id, []):
                    if other is not c and other["rec"] is not None and _match(other["anchor"], b) >= 0.3:
                        other["last"] = max(other["last"], c["last"])
                        other["first"] = min(other["first"], c["first"])
                        c["dup"] = True      # es el mismo vehiculo: no se registra otra vez
                        return
                with self.db._lock:
                    cur = self.db._conn.execute(
                        "INSERT INTO parked_vehicles (cam_id, cls, first_ts, last_ts, state, box, frame_w, frame_h, thumb, in_zone) "
                        "VALUES (?, ?, ?, ?, 'parked', ?, ?, ?, ?, ?)",
                        (cam_id, cls, c["first"], now, ",".join(map(str, b)), fw, fh, self._thumb(frame, b), 1 if self._in_zone(c) else 0))
                    self.db._conn.commit()
                    c["rec"] = cur.lastrowid
                c["saved"] = now
                c["hours"] = 0
                logger.info("ParkedTracker: %s estacionado en %s (id=%d, %s)", cls, cam_id, c["rec"], "en poligono" if self._in_zone(c) else "informativo")
                if self._in_zone(c):
                    self._alert(cam_id, c["rec"], cls, "arrived", c["first"], now)
        elif now - c["first"] >= (c.get("hours", 0) + 1) * ALERT_EVERY_S:
            c["hours"] = c.get("hours", 0) + 1
            if self._in_zone(c):
                self._alert(cam_id, c["rec"], c["cls"].most_common(1)[0][0], "hourly", c["first"], now)
            self._save_seen(c, now)
        elif now - c["saved"] >= 60:
            self._save_seen(c, now)

    @staticmethod
    def _in_zone(c) -> bool:
        """Dentro del poligono de estacionamiento (mayoria de las detecciones); sin poligono dibujado todo cuenta como dentro."""
        n = max(1, c.get("hits", 1))
        return c.get("zin", n) / n >= 0.5

    def _maybe_read_plate(self, c, frame, fw, fh, now) -> None:
        """Lee la placa del recorte nativo del vehiculo (solo los registrados dentro del poligono). OCR primero; si no sale, el VLM lee el recorte.
        Las primeras MAX_READS lecturas van cada EVERY_S; despues se sigue cada PLATE_SLOW_S mientras siga estacionado (cambia la luz, se quita
        el auto de enfrente). Antes se dejaba de intentar a los 20 minutos para siempre."""
        if c["rec"] is None or not self._in_zone(c) or not plates.enabled() or c.get("plate_ok") or now < c.get("plate_next", 0):
            return
        c["plate_n"] = c.get("plate_n", 0) + 1
        c["plate_next"] = now + (plates.EVERY_S if c["plate_n"] < plates.MAX_READS else PLATE_SLOW_S)
        with self.db._lock:
            row = self.db._conn.execute("SELECT plate_src FROM parked_vehicles WHERE id=?", (c["rec"],)).fetchone()
        if row and row[0] == "manual":
            c["plate_ok"] = True                     # lo escrito a mano manda
            return
        x1, y1, x2, y2 = c["anchor"]
        mx, my = int((x2 - x1) * .1), int((y2 - y1) * .1)
        crop = frame[max(0, y1 - my):min(fh, y2 + my), max(0, x1 - mx):min(fw, x2 + mx)]
        r = plates.read(crop)
        if r is None:
            if self.analyzer is not None and c["plate_n"] >= 3 and c.get("vlm_n", 0) < 4 and not c.get("vlm_busy"):
                self._vlm_plate(c, crop)
            return
        c.setdefault("reads", []).append((r["text"], r["conf"] * r["det"]))
        if r["jpeg"] and (c.get("best_conf", 0) < r["conf"]):
            c["best_conf"], c["best_img"] = r["conf"], r["jpeg"]
        win = plates.vote(c["reads"])
        if win:
            self._set_plate(c, win[0], win[1], "auto")
            logger.info("plates: vehiculo %d placa %s (conf %.2f, %d lecturas)", c["rec"], win[0], win[1], win[2])
            return
        g = plates.guess(c["reads"])
        if g:
            with self.db._lock:
                self.db._conn.execute("UPDATE parked_vehicles SET plate_guess=?, plate_guess_conf=?, plate_img=COALESCE(?, plate_img) WHERE id=? AND plate IS NULL",
                                      (g[0], g[1], c.get("best_img"), c["rec"]))
                self.db._conn.commit()

    def _set_plate(self, c, text, conf, src) -> None:
        with self.db._lock:
            self.db._conn.execute("UPDATE parked_vehicles SET plate=?, plate_conf=?, plate_src=?, plate_img=COALESCE(?, plate_img), plate_guess=NULL, plate_guess_conf=NULL "
                                  "WHERE id=? AND COALESCE(plate_src,'')<>'manual'", (text, conf, src, c.get("best_img"), c["rec"]))
            self.db._conn.commit()
        c["plate_ok"] = True
        try:
            self.link(c["rec"])
        except sqlite3.Error as exc:
            logger.warning("plates: enlace del vehiculo %s: %s", c["rec"], exc)

    def _vlm_plate(self, c, crop) -> None:
        """Respaldo del OCR: el VLM lee la placa del recorte nativo. Dos lecturas iguales del VLM quedan "por confirmar"; si ademas coinciden
        con la mejor lectura del OCR, se da por buena (en hilo aparte)."""
        c["vlm_busy"] = True
        c["vlm_n"] = c.get("vlm_n", 0) + 1
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            c["vlm_busy"] = False
            return

        def run(jpeg=buf.tobytes(), rec=c["rec"]):
            try:
                txt = self.analyzer.read_plate(jpeg)
                if txt:
                    c.setdefault("vlm_reads", []).append(txt)
                    reads = c["vlm_reads"]
                    ocr = plates.guess(c.get("reads") or [], min_conf=0.0)
                    if ocr and plates.similar(ocr[0], txt):
                        self._set_plate(c, ocr[0], max(0.7, ocr[1]), "auto+vlm")
                        logger.info("plates: vehiculo %d placa %s (OCR y VLM coinciden)", rec, ocr[0])
                    elif len(reads) >= 2 and reads[-1] == reads[-2]:
                        with self.db._lock:
                            self.db._conn.execute("UPDATE parked_vehicles SET plate_guess=?, plate_guess_conf=0.6 WHERE id=? AND plate IS NULL", (txt, rec))
                            self.db._conn.commit()
                        logger.info("plates: vehiculo %d placa %s por confirmar (2 lecturas iguales del VLM)", rec, txt)
            except (OSError, ValueError, sqlite3.Error) as exc:
                logger.debug("plates VLM: %s", exc)
            finally:
                c["vlm_busy"] = False
        threading.Thread(target=run, daemon=True, name="plate-vlm").start()

    # ── el mismo auto en varios registros ──────────────────────────────────
    def link(self, rec_id) -> None:
        """Compara la placa de un registro con las de los ultimos LINK_DAYS dias. Misma camara y tiempos encimados: es el mismo registro partido
        (se fusiona). La otra camara de calle al mismo tiempo: es el mismo auto visto desde Exterior 1 y 2 (gemelos). Otro dia: es una visita
        que se repite (se cuenta al consultar). Si dos lecturas casi iguales difieren, se queda la de mayor confianza."""
        with self.db._lock:
            me = self.db._conn.execute("SELECT id, cam_id, first_ts, last_ts, COALESCE(left_ts, last_ts), plate, plate_conf, plate_src FROM parked_vehicles "
                                       "WHERE id=? AND merged_into IS NULL", (rec_id,)).fetchone()
            if not me or not me[5]:
                return                                       # sin placa, o ya fusionado en otro registro
            others = self.db._conn.execute(
                "SELECT id, cam_id, first_ts, last_ts, COALESCE(left_ts, last_ts), plate, plate_conf, plate_src FROM parked_vehicles "
                "WHERE id<>? AND plate IS NOT NULL AND merged_into IS NULL AND first_ts>?", (rec_id, me[2] - LINK_DAYS * 86400)).fetchall()
        for o in others:
            if not plates.similar(me[5], o[5]):
                continue
            with self.db._lock:
                if self.db._conn.execute("SELECT merged_into FROM parked_vehicles WHERE id=?", (me[0],)).fetchone()[0] is not None:
                    return                                   # se fusiono en otro dentro de este mismo ciclo
                if self.db._conn.execute("SELECT merged_into FROM parked_vehicles WHERE id=?", (o[0],)).fetchone()[0] is not None:
                    continue
            overlap = me[2] <= o[4] + GONE_S and o[2] <= me[4] + GONE_S
            if not overlap:
                continue                                    # otro dia: visita repetida, no se toca
            best = me if (me[7] == "manual" or (o[7] != "manual" and (me[6] or 0) >= (o[6] or 0))) else o
            with self.db._lock:
                for x in (me, o):
                    if x[5] != best[5] and x[7] != "manual":
                        self.db._conn.execute("UPDATE parked_vehicles SET plate=?, plate_conf=? WHERE id=?", (best[5], best[6], x[0]))
                if me[1] == o[1]:
                    keep, drop = (o, me) if o[2] <= me[2] else (me, o)
                    self.db._conn.execute("UPDATE parked_vehicles SET first_ts=MIN(first_ts, ?), last_ts=MAX(last_ts, ?) WHERE id=?", (drop[2], drop[3], keep[0]))
                    self.db._conn.execute("UPDATE parked_vehicles SET merged_into=?, state='left', left_ts=COALESCE(left_ts, last_ts) WHERE id=?", (keep[0], drop[0]))
                    self.db._conn.execute("UPDATE parked_vehicles SET twin_id=? WHERE twin_id=?", (keep[0], drop[0]))
                    for cands in self._cand.values():
                        for c in cands:
                            if c.get("rec") == drop[0]:
                                c["rec"] = keep[0]
                    logger.info("plates: registros %d y %d son el mismo auto (%s): se fusionan en %d", me[0], o[0], best[5], keep[0])
                else:
                    self.db._conn.execute("UPDATE parked_vehicles SET twin_id=? WHERE id=?", (o[0], me[0]))
                    self.db._conn.execute("UPDATE parked_vehicles SET twin_id=? WHERE id=?", (me[0], o[0]))
                    logger.info("plates: %s visto a la vez en %s y %s (registros %d y %d)", best[5], me[1], o[1], me[0], o[0])
                self.db._conn.commit()

    def relink_all(self) -> None:
        with self.db._lock:
            ids = [r[0] for r in self.db._conn.execute("SELECT id FROM parked_vehicles WHERE plate IS NOT NULL AND merged_into IS NULL ORDER BY first_ts")]
        for i in ids:
            self.link(i)

    def _save_seen(self, c, now) -> None:
        with self.db._lock:
            self.db._conn.execute("UPDATE parked_vehicles SET last_ts=? WHERE id=?", (now, c["rec"]))
            self.db._conn.commit()
        c["saved"] = now

    def _alert(self, cam_id, rec_id, cls, state, first_ts, now) -> None:
        """Evento con alerta (type 'nemotron', como las del VLM) para que salga en la linea de tiempo, el chat y los reportes."""
        dur = max(0, int(now - first_ts))
        tiempo = f"{dur // 3600} h {dur % 3600 // 60} min" if dur >= 3600 else f"{dur // 60} min"
        cam = self.cam_names.get(cam_id, cam_id)
        hora = time.strftime("%H:%M", time.localtime(first_ts))
        if state == "arrived":
            msg, sev = f"{cls.capitalize()} estacionado en {cam} (llego a las {hora})", "low"
        elif state == "hourly":
            msg, sev = f"{cls.capitalize()} sigue estacionado en {cam}: lleva {tiempo} (llego a las {hora})", "medium"
        else:
            msg, sev = f"{cls.capitalize()} se fue de {cam} tras {tiempo} estacionado (llego a las {hora})", "low"
        with self.db._lock:
            row = self.db._conn.execute("SELECT plate FROM parked_vehicles WHERE id=?", (rec_id,)).fetchone()
        plate = row[0] if row and row[0] else None
        payload = {"schema": 2, "source": "parked_tracker", "people": 0, "persons": [], "vehicles": 1, "activity": msg, "scene": "",
                   "relevant": True, "alerts": [msg + (f". Placa {plate}" if plate else "")], "alert_types": ["vehiculo_detenido"],
                   "severity": sev, "confidence": "high",
                   "parked": {"vehicle_id": rec_id, "state": state, "class": cls, "duration_s": dur, "first_ts": first_ts, "plate": plate}}
        try:
            eid = self.db.insert_event("nemotron", cam_id, payload, people=0, has_alert=True)
        except sqlite3.Error as exc:          # la alerta no debe romper el seguimiento
            logger.warning("ParkedTracker: no se pudo guardar la alerta: %s", exc)
            return
        frame = self._frames.get(cam_id)
        if self.snapshots is not None and frame is not None:
            try:
                self.snapshots.save(cam_id, frame, "estacionado", event_id=eid)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("ParkedTracker: no se pudo guardar la captura: %s", exc)

    def _gc(self, cam_id, now) -> None:
        cands = self._cand.get(cam_id, [])
        keep = []
        for c in cands:
            if c.get("dup"):
                continue
            idle = now - c["last"]
            ghost = False
            if c["rec"] is not None and c.get("mt") is not None and now - c.get("mt_start", now) > PRESENCE_WIN_S:
                pres, n = self._presence(cam_id, c, now)
                ghost = n >= 30 and pres < PRESENCE_GONE
                if ghost:
                    logger.info("ParkedTracker: vehiculo %d en %s se da por ido: solo se vio en %.0f%% de los cuadros (trafico)", c["rec"], cam_id, pres * 100)
            if c["rec"] is not None and (idle > GONE_S or ghost):
                with self.db._lock:
                    self.db._conn.execute("UPDATE parked_vehicles SET state='left', left_ts=?, last_ts=? WHERE id=?",
                                          (c["last"], c["last"], c["rec"]))
                    self.db._conn.commit()
                logger.info("ParkedTracker: vehiculo %d se fue de %s tras %.0f min", c["rec"], cam_id, (c["last"] - c["first"]) / 60)
                if self._in_zone(c):
                    self._alert(cam_id, c["rec"], c["cls"].most_common(1)[0][0], "left", c["first"], c["last"])
            elif c["rec"] is None and idle > 120:
                pass
            else:
                keep.append(c)
        self._cand[cam_id] = keep
        if now - self._last_gc > 86400:
            self._last_gc = now
            with self.db._lock:
                self.db._conn.execute("DELETE FROM parked_vehicles WHERE state='left' AND left_ts < ?", (now - RETENTION_DAYS * 86400,))
                self.db._conn.commit()


def list_vehicles(db, state=None, cam=None, since=None, limit=100) -> list:
    sql = ("SELECT id, cam_id, cls, first_ts, last_ts, left_ts, state, plate, plate_conf, note, (thumb IS NOT NULL), plate_src, (plate_img IS NOT NULL), in_zone, "
           "plate_guess, plate_guess_conf, twin_id FROM parked_vehicles WHERE merged_into IS NULL")
    args: list = []
    if state in ("parked", "left"):
        sql += " AND state=?"
        args.append(state)
    if cam:
        sql += " AND cam_id=?"
        args.append(cam)
    if since:
        sql += " AND last_ts>=?"
        args.append(since)
    sql += " ORDER BY (state='parked') DESC, first_ts DESC LIMIT ?"
    args.append(min(int(limit), 500))
    now = time.time()
    with db._lock:
        rows = db._conn.execute(sql, args).fetchall()
        known = db._conn.execute("SELECT id, cam_id, plate, first_ts, twin_id FROM parked_vehicles WHERE plate IS NOT NULL AND merged_into IS NULL "
                                 "AND first_ts>?", (now - LINK_DAYS * 86400,)).fetchall()
    out = []
    for rid, cam_id, cls, first, last, left, st, plate, pc, note, has, psrc, has_pimg, inz, pg, pgc, twin in rows:
        end = (left or last) if st == "left" else now
        visits = []
        if plate:                                   # otras visitas del mismo auto (los gemelos de la otra camara no cuentan como visita aparte)
            seen = {rid, twin}
            for kid, kcam, kp, kfirst, ktwin in known:
                if kid not in seen and ktwin not in seen and plates.similar(plate, kp) and abs(kfirst - first) > GONE_S:
                    visits.append(kfirst)
                    seen.add(kid)
        out.append({"id": rid, "cam_id": cam_id, "cls": cls, "first_ts": first, "last_ts": last, "left_ts": left,
                    "state": st, "duration_s": max(0, round(end - first)), "plate": plate, "plate_conf": pc,
                    "note": note, "has_thumb": bool(has), "plate_src": psrc, "has_plate_img": bool(has_pimg), "in_zone": bool(inz),
                    "plate_guess": pg if not plate else None, "plate_guess_conf": pgc if not plate else None, "twin_id": twin,
                    "other_visits": len(visits), "last_other_visit": max(visits) if visits else None})
    return out
