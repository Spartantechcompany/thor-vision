"""
SQLite event store para THOR Vision.

Mismo enfoque que el Orinano (`events.db`) pero con schema adaptado:
- events: nemotron + cam_state + alert
- snapshots: índice de JPGs en disco
- chat_messages: historial del chat persistente por session_id

Diseño:
- WAL mode → lecturas concurrentes sin bloquear writes
- Lock interno (threading.Lock) → serializa writes desde N workers
- `check_same_thread=False` para uso multi-thread
- Sin dependencias externas — `sqlite3` viene en stdlib
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        REAL    NOT NULL,
  type      TEXT    NOT NULL,
  cam_id    TEXT,
  people    INTEGER,
  has_alert INTEGER NOT NULL DEFAULT 0,
  data      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cam_ts    ON events(cam_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts   ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_alerts    ON events(has_alert, ts) WHERE has_alert = 1;
-- Con retención de ~1 año (3M filas) la consulta de picos de gente haría
-- scan del rango completo; el índice parcial la deja resolver por índice.
CREATE INDEX IF NOT EXISTS idx_events_people_ts ON events(people DESC, ts DESC) WHERE people > 0;

CREATE TABLE IF NOT EXISTS snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL    NOT NULL,
  cam_id     TEXT    NOT NULL,
  path       TEXT    NOT NULL,
  trigger    TEXT    NOT NULL,
  event_id   INTEGER,
  size_bytes INTEGER,
  FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_cam_ts ON snapshots(cam_id, ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts     ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_trig   ON snapshots(trigger, ts);

CREATE TABLE IF NOT EXISTS dashboard_visits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL    NOT NULL,
  ip         TEXT    NOT NULL,
  user_agent TEXT,
  path       TEXT    NOT NULL DEFAULT '/'
);
CREATE INDEX IF NOT EXISTS idx_visits_ts ON dashboard_visits(ts);
CREATE INDEX IF NOT EXISTS idx_visits_ip_ts ON dashboard_visits(ip, ts);

CREATE TABLE IF NOT EXISTS face_sightings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL    NOT NULL,
  cam_id     TEXT    NOT NULL,
  name       TEXT    NOT NULL,
  confidence REAL,
  thumb      BLOB
);
CREATE INDEX IF NOT EXISTS idx_sightings_ts      ON face_sightings(ts);
CREATE INDEX IF NOT EXISTS idx_sightings_cam_ts  ON face_sightings(cam_id, ts);
CREATE INDEX IF NOT EXISTS idx_sightings_name_ts ON face_sightings(name, ts);

CREATE TABLE IF NOT EXISTS chat_messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT    NOT NULL,
  ts           REAL    NOT NULL,
  role         TEXT    NOT NULL,
  content      TEXT    NOT NULL,
  context_cams INTEGER,
  ms           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chat_session_ts ON chat_messages(session_id, ts);

CREATE TABLE IF NOT EXISTS reports (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_at REAL NOT NULL,
  period_start REAL NOT NULL,
  period_end   REAL NOT NULL,
  kind         TEXT NOT NULL,
  content      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(generated_at);

CREATE TABLE IF NOT EXISTS subjects (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT,
  named      INTEGER NOT NULL DEFAULT 0,
  created_ts REAL    NOT NULL,
  last_ts    REAL    NOT NULL,
  embedding  BLOB,
  n_emb      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_subjects_last ON subjects(last_ts);

CREATE TABLE IF NOT EXISTS person_visits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_id INTEGER,
  cam_id     TEXT    NOT NULL,
  track_id   INTEGER,
  start_ts   REAL    NOT NULL,
  end_ts     REAL    NOT NULL,
  hits       INTEGER NOT NULL DEFAULT 0,
  status     TEXT    NOT NULL DEFAULT 'open',
  static     INTEGER NOT NULL DEFAULT 0,
  fp         INTEGER NOT NULL DEFAULT 0,
  face       BLOB,
  face_score REAL    NOT NULL DEFAULT 0,
  body       BLOB,
  body_score REAL    NOT NULL DEFAULT 0,
  embedding  BLOB,
  n_emb      INTEGER NOT NULL DEFAULT 0,
  known_name TEXT,
  known_conf REAL,
  vlm_desc   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pv_start   ON person_visits(start_ts);
CREATE INDEX IF NOT EXISTS idx_pv_subject ON person_visits(subject_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_pv_cam     ON person_visits(cam_id, start_ts);
"""


class EventDB:
    """SQLite event store thread-safe."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._lock   = threading.Lock()
        self._conn   = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,    # autocommit; controlamos transacciones manualmente
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("EventDB ready — %s", db_path)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migracion idempotente: columnas de triage agregadas despues de
            # que `events` ya existia en produccion (ALTER, no CREATE, para
            # no perder las filas existentes).
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)").fetchall()}
            if "review_label" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN review_label TEXT")
            if "reviewed_at" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN reviewed_at REAL")
            scols = {r[1] for r in self._conn.execute("PRAGMA table_info(face_sightings)").fetchall()}
            if "body" not in scols:
                self._conn.execute("ALTER TABLE face_sightings ADD COLUMN body BLOB")

    # ── Inserts ───────────────────────────────────────────────────────────

    def insert_event(self, type: str, cam_id: Optional[str], payload: dict,
                     people: Optional[int] = None,
                     has_alert: bool = False) -> int:
        """Inserta un evento y retorna su id."""
        ts = time.time()
        data_json = json.dumps(payload, default=str, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, type, cam_id, people, has_alert, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, type, cam_id, people, 1 if has_alert else 0, data_json),
            )
            return cur.lastrowid

    def set_event_review(self, event_id: int, label: Optional[str]) -> bool:
        """Triage manual de un evento (tipicamente una alerta).
        `label` en {'important', 'noise'}; None = quitar la revision."""
        if label is not None and label not in ("important", "noise"):
            raise ValueError(f"label invalido: {label!r}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET review_label = ?, reviewed_at = ? WHERE id = ?",
                (label, time.time() if label is not None else None, event_id),
            )
            return cur.rowcount > 0

    def insert_report(self, kind: str, period_start: float, period_end: float,
                      content: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reports (generated_at, period_start, period_end, kind, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), period_start, period_end, kind, content),
            )
            return cur.lastrowid

    def query_reports(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports ORDER BY generated_at DESC LIMIT ?",
                (min(limit, 200),),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_snapshot(self, cam_id: str, path: str, trigger: str,
                        event_id: Optional[int] = None,
                        size_bytes: Optional[int] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (ts, cam_id, path, trigger, event_id, size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), cam_id, path, trigger, event_id, size_bytes),
            )
            return cur.lastrowid

    def insert_face_sighting(self, cam_id: str, name: str,
                             confidence: Optional[float],
                             thumb: Optional[bytes],
                             body: Optional[bytes] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO face_sightings (ts, cam_id, name, confidence, thumb, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), cam_id, name, confidence, thumb, body),
            )
            return cur.lastrowid

    def insert_chat(self, session_id: str, role: str, content: str,
                    context_cams: Optional[int] = None,
                    ms: Optional[int] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO chat_messages "
                "(session_id, ts, role, content, context_cams, ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, time.time(), role, content, context_cams, ms),
            )
            return cur.lastrowid

    def insert_dashboard_visit(self, ip: str, user_agent: Optional[str] = None,
                               path: str = '/') -> int:
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO dashboard_visits (ts, ip, user_agent, path) '
                'VALUES (?, ?, ?, ?)',
                (time.time(), ip, user_agent, path),
            )
            return cur.lastrowid

    # ── Queries ───────────────────────────────────────────────────────────

    def query_events(self, cam_id: Optional[str] = None,
                     type: Optional[str] = None,
                     since: Optional[float] = None,
                     until: Optional[float] = None,
                     has_alert: Optional[bool] = None,
                     review_label: Optional[str] = None,
                     limit: int = 200) -> list[dict]:
        sql   = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if cam_id is not None:
            sql += " AND cam_id = ?"; args.append(cam_id)
        if type is not None:
            sql += " AND type = ?"; args.append(type)
        if since is not None:
            sql += " AND ts >= ?"; args.append(since)
        if until is not None:
            sql += " AND ts <= ?"; args.append(until)
        if has_alert is not None:
            sql += " AND has_alert = ?"; args.append(1 if has_alert else 0)
        if review_label is not None:
            sql += " AND review_label = ?"; args.append(review_label)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(min(limit, 2000))

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_event(r) for r in rows]

    def query_snapshots(self, cam_id: Optional[str] = None,
                        since: Optional[float] = None,
                        until: Optional[float] = None,
                        trigger: Optional[str] = None,
                        limit: int = 100) -> list[dict]:
        sql   = "SELECT * FROM snapshots WHERE 1=1"
        args: list[Any] = []
        if cam_id is not None:
            sql += " AND cam_id = ?"; args.append(cam_id)
        if since is not None:
            sql += " AND ts >= ?"; args.append(since)
        if until is not None:
            sql += " AND ts <= ?"; args.append(until)
        if trigger is not None:
            sql += " AND trigger = ?"; args.append(trigger)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(min(limit, 1000))

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_snapshot(self, snapshot_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return dict(row) if row else None

    def query_chat_history(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, ts, context_cams, ms "
                "FROM chat_messages WHERE session_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (session_id, min(limit, 200)),
            ).fetchall()
        # Devolver en orden cronológico (más viejo primero)
        return list(reversed([dict(r) for r in rows]))

    def query_dashboard_visits(self, ip: Optional[str] = None,
                               since: Optional[float] = None,
                               until: Optional[float] = None,
                               limit: int = 200) -> list[dict]:
        sql = 'SELECT * FROM dashboard_visits WHERE 1=1'
        args: list[Any] = []
        if ip is not None:
            sql += ' AND ip = ?'; args.append(ip)
        if since is not None:
            sql += ' AND ts >= ?'; args.append(since)
        if until is not None:
            sql += ' AND ts <= ?'; args.append(until)
        sql += ' ORDER BY ts DESC LIMIT ?'; args.append(min(limit, 2000))

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def query_face_sightings(self, cam_id: Optional[str] = None,
                             name: Optional[str] = None,
                             since: Optional[float] = None,
                             until: Optional[float] = None,
                             limit: int = 100) -> list[dict]:
        """No trae el BLOB — solo `has_thumb`, para que listar no pese varios
        cientos de KB. El thumb se pide aparte via get_face_sighting_thumb()."""
        sql = ("SELECT id, ts, cam_id, name, confidence, "
               "(thumb IS NOT NULL) AS has_thumb, (body IS NOT NULL) AS has_body "
               "FROM face_sightings WHERE 1=1")
        args: list[Any] = []
        if cam_id is not None:
            sql += " AND cam_id = ?"; args.append(cam_id)
        if name is not None:
            sql += " AND name = ?"; args.append(name)
        if since is not None:
            sql += " AND ts >= ?"; args.append(since)
        if until is not None:
            sql += " AND ts <= ?"; args.append(until)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(min(limit, 500))

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["has_thumb"] = bool(d.get("has_thumb"))
            d["has_body"] = bool(d.get("has_body"))
            out.append(d)
        return out

    def get_face_sighting_body(self, sighting_id: int) -> Optional[bytes]:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM face_sightings WHERE id = ?", (sighting_id,)
            ).fetchone()
        return row["body"] if row and row["body"] is not None else None

    def get_face_sighting_thumb(self, sighting_id: int) -> Optional[bytes]:
        with self._lock:
            row = self._conn.execute(
                "SELECT thumb FROM face_sightings WHERE id = ?", (sighting_id,)
            ).fetchone()
        return row["thumb"] if row and row["thumb"] is not None else None

    def query_significant_events(self, since: float, until: Optional[float] = None,
                                 type: str = "nemotron", limit: int = 60,
                                 require_activity_change: bool = False) -> list[dict]:
        """
        Eventos "significativos" filtrados en SQL con window functions.

        Existe aparte de `query_events` porque ese topa en 2000 filas y, con el
        ritmo real de ~8.5k eventos/día, 2000 filas cubren apenas unas horas —
        pedir una ventana de 24h por esa vía devolvía datos de ~1h en silencio.

        Significativo = tiene alerta, o hay personas, o las personas se fueron.
        `require_activity_change` suma "la descripción cambió vs la observación
        anterior de la misma cámara"; va apagado por default porque `activity`
        es texto libre (40k+ valores distintos) y hace pasar >90% de las filas,
        que es justamente lo que ahoga la señal real.
        """
        until = time.time() if until is None else until
        sql = """
        SELECT ts, cam_id, people, has_alert, activity, alerts FROM (
          SELECT ts, cam_id, COALESCE(people, 0) AS people, has_alert,
                 COALESCE(json_extract(data, '$.activity'), '') AS activity,
                 json_extract(data, '$.alerts') AS alerts,
                 LAG(json_extract(data, '$.activity')) OVER w AS prev_act,
                 LAG(COALESCE(people, 0))              OVER w AS prev_ppl
          FROM events
          WHERE type = ? AND ts >= ? AND ts <= ?
          WINDOW w AS (PARTITION BY cam_id ORDER BY ts)
        )
        WHERE has_alert = 1
           OR people > 0
           OR (prev_ppl > 0 AND people = 0)
           OR (? AND prev_act IS NOT NULL AND activity <> '' AND activity <> prev_act)
        ORDER BY ts DESC
        LIMIT ?
        """
        args = (type, since, until,
                1 if require_activity_change else 0,
                min(limit, 500))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            d["alerts"]    = self._parse_alerts(d.get("alerts"))
            d["has_alert"] = bool(d.get("has_alert"))
            out.append(d)
        return out

    def aggregate_events(self, since: float, until: Optional[float] = None,
                         type: str = "nemotron",
                         bucket: Optional[str] = None) -> dict:
        """
        Rollup agregado resuelto íntegramente en SQL — nunca trae el rango
        completo a memoria. Para ventanas largas, donde listar evento por evento
        no cabe en el contexto del modelo.

        `bucket` se elige solo según el rango (hora si <=48h, día si no).
        """
        until = time.time() if until is None else until
        hours = max((until - since) / 3600.0, 0.0)
        if bucket is None:
            bucket = "hour" if hours <= 48 else "day"
        width = 3600 if bucket == "hour" else 86400
        # Offset local para que los buckets caigan en horas/días de pared, sin
        # depender de strftime('localtime') ni de la tz del contenedor.
        tzoff = time.localtime().tm_gmtoff or 0

        with self._lock:
            per_cam = [dict(r) for r in self._conn.execute("""
                SELECT cam_id,
                       count(*)                 AS n,
                       sum(has_alert)           AS alerts,
                       max(COALESCE(people, 0)) AS max_people,
                       round(avg(COALESCE(people, 0)), 2) AS avg_people,
                       sum(CASE WHEN COALESCE(people, 0) > 0 THEN 1 ELSE 0 END)
                                                AS n_with_people,
                       min(ts) AS first_ts,
                       max(ts) AS last_ts
                FROM events
                WHERE type = ? AND ts >= ? AND ts <= ?
                GROUP BY cam_id
                ORDER BY n DESC
            """, (type, since, until)).fetchall()]

            buckets = [dict(r) for r in self._conn.execute("""
                SELECT CAST((ts + ?) / ? AS INTEGER) AS b,
                       count(*)                 AS n,
                       max(COALESCE(people, 0)) AS max_people,
                       sum(COALESCE(people, 0)) AS sum_people,
                       sum(has_alert)           AS alerts
                FROM events
                WHERE type = ? AND ts >= ? AND ts <= ?
                GROUP BY b
                ORDER BY b
            """, (tzoff, width, type, since, until)).fetchall()]

            # Único json_extract sobre filtro selectivo: pega contra idx_events_alerts
            alert_rows = [dict(r) for r in self._conn.execute("""
                SELECT ts, cam_id, COALESCE(people, 0) AS people,
                       json_extract(data, '$.alerts') AS alerts
                FROM events
                WHERE type = ? AND has_alert = 1 AND ts >= ? AND ts <= ?
                ORDER BY ts DESC
                LIMIT 30
            """, (type, since, until)).fetchall()]

            peaks = [dict(r) for r in self._conn.execute("""
                SELECT ts, cam_id, people
                FROM events
                -- `people > 0` (no COALESCE) para que calce con el
                -- predicado del índice parcial idx_events_people_ts
                WHERE type = ? AND ts >= ? AND ts <= ? AND people > 0
                ORDER BY people DESC, ts DESC
                LIMIT 20
            """, (type, since, until)).fetchall()]

            # Rango realmente disponible: la retención (rotate) recorta por atrás,
            # así que el prompt debe poder decir "hay datos desde X" y no inventar.
            data_first_ts = self._conn.execute(
                "SELECT min(ts) FROM events WHERE type = ?", (type,)
            ).fetchone()[0]

        for a in alert_rows:
            a["alerts"] = self._parse_alerts(a.get("alerts"))

        return {
            "window": {
                "since":          since,
                "until":          until,
                "hours":          round(hours, 2),
                "bucket":         bucket,
                "bucket_width_s": width,
                "tz_offset_s":    tzoff,
            },
            "totals": {
                "events":        sum(c["n"] for c in per_cam),
                "alerts":        sum((c["alerts"] or 0) for c in per_cam),
                "max_people":    max([c["max_people"] or 0 for c in per_cam], default=0),
                "cams":          len(per_cam),
                "data_first_ts": data_first_ts,
            },
            "per_cam": per_cam,
            "buckets": buckets,
            "alerts":  alert_rows,
            "peaks":   peaks,
        }

    @staticmethod
    def _parse_alerts(raw) -> list:
        """`alerts` viaja como JSON dentro de `data`; nunca debe tumbar la query."""
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _row_event(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            d["data"] = json.loads(d["data"])
        except Exception:
            pass
        d["has_alert"] = bool(d.get("has_alert"))
        return d

    # ── Stats & Retention ─────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            n_events    = self._conn.execute("SELECT count(*) FROM events").fetchone()[0]
            n_snaps     = self._conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
            n_chat      = self._conn.execute("SELECT count(*) FROM chat_messages").fetchone()[0]
            oldest, newest = self._conn.execute(
                "SELECT min(ts), max(ts) FROM events"
            ).fetchone()
        try:
            db_bytes = os.path.getsize(self.db_path)
        except OSError:
            db_bytes = 0
        return {
            "events":        n_events,
            "snapshots":     n_snaps,
            "chat_messages": n_chat,
            "db_mb":         round(db_bytes / (1024 * 1024), 2),
            "oldest_ts":     oldest,
            "newest_ts":     newest,
        }

    # ── Personas: visitas y sujetos ───────────────────────────────────────

    _PV_COLS = frozenset({"end_ts", "hits", "status", "static", "fp", "face", "face_score",
                          "body", "body_score", "embedding", "n_emb", "known_name",
                          "known_conf", "vlm_desc", "subject_id"})

    def insert_person_visit(self, cam_id: str, track_id: int, start_ts: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO person_visits (cam_id, track_id, start_ts, end_ts) VALUES (?,?,?,?)",
                (cam_id, track_id, start_ts, start_ts))
            return cur.lastrowid

    def update_person_visit(self, vid: int, **fields) -> None:
        cols = [k for k in fields if k in self._PV_COLS]
        if not cols:
            return
        sql = "UPDATE person_visits SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE id=?"
        with self._lock:
            self._conn.execute(sql, [fields[c] for c in cols] + [vid])

    def create_subject(self, emb: bytes, n: int, ts: float,
                       name: Optional[str] = None, named: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO subjects (name, named, created_ts, last_ts, embedding, n_emb) "
                "VALUES (?,?,?,?,?,?)", (name, named, ts, ts, emb, n))
            return cur.lastrowid

    def update_subject_emb(self, sid: int, emb: bytes, n: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE subjects SET embedding=?, n_emb=? WHERE id=?", (emb, n, sid))

    def touch_subject(self, sid: int, ts: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE subjects SET last_ts=MAX(last_ts, ?) WHERE id=?", (ts, sid))

    def load_subjects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, named, embedding, n_emb FROM subjects").fetchall()
        return [dict(r) for r in rows]

    def rename_subject(self, sid: int, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE subjects SET name=?, named=1 WHERE id=?", (name, sid))
            return cur.rowcount > 0

    def merge_subjects(self, src: int, dst: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE person_visits SET subject_id=? WHERE subject_id=?", (dst, src))
            self._conn.execute(
                "UPDATE subjects SET last_ts=MAX(last_ts, (SELECT last_ts FROM subjects WHERE id=?)), "
                "created_ts=MIN(created_ts, (SELECT created_ts FROM subjects WHERE id=?)) WHERE id=?",
                (src, src, dst))
            self._conn.execute("DELETE FROM subjects WHERE id=?", (src,))

    def delete_subject(self, sid: int) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM person_visits WHERE subject_id=?", (sid,))
            cur = self._conn.execute("DELETE FROM subjects WHERE id=?", (sid,))
            return cur.rowcount > 0

    def list_subjects(self, since: Optional[float] = None, limit: int = 100) -> list[dict]:
        sql = ("SELECT s.id, COALESCE(s.name, 'Persona #' || s.id) AS name, s.named, "
               "s.created_ts, s.last_ts, "
               "(SELECT COUNT(*) FROM person_visits v WHERE v.subject_id=s.id AND v.fp=0) AS visits, "
               "(SELECT MIN(start_ts) FROM person_visits v WHERE v.subject_id=s.id) AS first_ts, "
               "(SELECT group_concat(DISTINCT cam_id) FROM person_visits v WHERE v.subject_id=s.id) AS cams, "
               "(SELECT id FROM person_visits v WHERE v.subject_id=s.id AND v.face IS NOT NULL "
               "   ORDER BY face_score DESC LIMIT 1) AS face_visit, "
               "(SELECT id FROM person_visits v WHERE v.subject_id=s.id AND v.body IS NOT NULL "
               "   ORDER BY body_score DESC LIMIT 1) AS body_visit "
               "FROM subjects s WHERE 1=1")
        args: list[Any] = []
        if since is not None:
            sql += " AND s.last_ts >= ?"; args.append(since)
        sql += " ORDER BY s.last_ts DESC LIMIT ?"; args.append(min(limit, 500))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["named"] = bool(d["named"])
            d["cams"] = (d.get("cams") or "").split(",") if d.get("cams") else []
            out.append(d)
        return out

    def query_person_visits(self, cam_id: Optional[str] = None, subject_id: Optional[int] = None,
                            since: Optional[float] = None, until: Optional[float] = None,
                            limit: int = 100, include_hidden: bool = False,
                            unassigned: bool = False) -> list[dict]:
        sql = ("SELECT v.id, v.subject_id, COALESCE(s.name, CASE WHEN v.subject_id IS NOT NULL "
               "THEN 'Persona #' || v.subject_id END) AS subject_name, v.cam_id, v.start_ts, v.end_ts, "
               "v.hits, v.status, v.static, v.fp, v.known_name, v.known_conf, v.vlm_desc, v.n_emb, "
               "(v.face IS NOT NULL) AS has_face, (v.body IS NOT NULL) AS has_body "
               "FROM person_visits v LEFT JOIN subjects s ON s.id = v.subject_id WHERE 1=1")
        args: list[Any] = []
        if not include_hidden:
            sql += " AND v.static=0 AND v.fp=0"
        if cam_id is not None:
            sql += " AND v.cam_id=?"; args.append(cam_id)
        if subject_id is not None:
            sql += " AND v.subject_id=?"; args.append(subject_id)
        if unassigned:
            sql += " AND v.subject_id IS NULL"
        if since is not None:
            sql += " AND v.start_ts>=?"; args.append(since)
        if until is not None:
            sql += " AND v.start_ts<=?"; args.append(until)
        sql += " ORDER BY v.start_ts DESC LIMIT ?"; args.append(min(limit, 500))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("has_face", "has_body", "static", "fp"):
                d[k] = bool(d[k])
            out.append(d)
        return out

    def get_visit_blob(self, vid: int, col: str) -> Optional[bytes]:
        assert col in ("face", "body")
        with self._lock:
            row = self._conn.execute(f"SELECT {col} FROM person_visits WHERE id=?", (vid,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_subject_blob(self, sid: int, col: str) -> Optional[bytes]:
        assert col in ("face", "body")
        with self._lock:
            row = self._conn.execute(
                f"SELECT {col} FROM person_visits WHERE subject_id=? AND {col} IS NOT NULL "
                f"ORDER BY {col}_score DESC LIMIT 1", (sid,)).fetchone()
        return row[0] if row else None

    def rotate(self, max_events: int = 200_000,
               max_chat_per_session: int = 1_000,
               max_visits: int = 200_000,
               max_face_sightings: int = 100_000,
               max_person_visits: int = 50_000) -> dict:
        """
        Mantiene los últimos `max_events` rows.
        Borra mensajes de chat más viejos que los `max_chat_per_session` por sesión.
        Borra filas de `snapshots` cuyo archivo ya no existe en disco.
        """
        result: dict = {}

        with self._lock:
            # 1) Truncar events
            cur = self._conn.execute("SELECT count(*) FROM events").fetchone()
            n = cur[0]
            if n > max_events:
                excess = n - max_events
                self._conn.execute(
                    "DELETE FROM events WHERE id IN "
                    "(SELECT id FROM events ORDER BY ts ASC LIMIT ?)",
                    (excess,),
                )
                result["events_deleted"] = excess

            # 2) Truncar chat (por sesión)
            sessions = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT session_id FROM chat_messages"
            ).fetchall()]
            chat_del = 0
            for sid in sessions:
                cnt = self._conn.execute(
                    "SELECT count(*) FROM chat_messages WHERE session_id = ?",
                    (sid,),
                ).fetchone()[0]
                if cnt > max_chat_per_session:
                    excess = cnt - max_chat_per_session
                    self._conn.execute(
                        "DELETE FROM chat_messages WHERE id IN "
                        "(SELECT id FROM chat_messages WHERE session_id = ? "
                        " ORDER BY ts ASC LIMIT ?)",
                        (sid, excess),
                    )
                    chat_del += excess
            if chat_del:
                result["chat_deleted"] = chat_del

            # 3) Truncar dashboard_visits
            cur = self._conn.execute("SELECT count(*) FROM dashboard_visits").fetchone()
            n = cur[0]
            if n > max_visits:
                excess = n - max_visits
                self._conn.execute(
                    "DELETE FROM dashboard_visits WHERE id IN "
                    "(SELECT id FROM dashboard_visits ORDER BY ts ASC LIMIT ?)",
                    (excess,),
                )
                result["visits_deleted"] = excess

            # 4) Truncar face_sightings
            cur = self._conn.execute("SELECT count(*) FROM face_sightings").fetchone()
            n = cur[0]
            if n > max_face_sightings:
                excess = n - max_face_sightings
                self._conn.execute(
                    "DELETE FROM face_sightings WHERE id IN "
                    "(SELECT id FROM face_sightings ORDER BY ts ASC LIMIT ?)",
                    (excess,),
                )
                result["sightings_deleted"] = excess

            # 5) Truncar person_visits y sujetos sin visitas (los nombrados se conservan)
            n = self._conn.execute("SELECT count(*) FROM person_visits").fetchone()[0]
            if n > max_person_visits:
                self._conn.execute(
                    "DELETE FROM person_visits WHERE id IN "
                    "(SELECT id FROM person_visits ORDER BY start_ts ASC LIMIT ?)",
                    (n - max_person_visits,))
                result["person_visits_deleted"] = n - max_person_visits
                self._conn.execute(
                    "DELETE FROM subjects WHERE named=0 AND id NOT IN "
                    "(SELECT DISTINCT subject_id FROM person_visits WHERE subject_id IS NOT NULL)")

            # 6) TTL por tiempo para datos biometricos de personas sin nombre
            ttl_days = float(os.environ.get("UNNAMED_TTL_DAYS", "30"))
            if ttl_days > 0:
                cut = time.time() - ttl_days * 86400
                c1 = self._conn.execute(
                    "DELETE FROM person_visits WHERE start_ts < ? AND (subject_id IS NULL OR "
                    "subject_id IN (SELECT id FROM subjects WHERE named=0))", (cut,)).rowcount
                self._conn.execute(
                    "DELETE FROM subjects WHERE named=0 AND id NOT IN "
                    "(SELECT DISTINCT subject_id FROM person_visits WHERE subject_id IS NOT NULL)")
                c2 = self._conn.execute(
                    "DELETE FROM face_sightings WHERE ts < ? AND (name IS NULL OR name IN (Desconocido,Sin rostro))",
                    (cut,)).rowcount
                if c1 or c2:
                    result["ttl_deleted"] = {"person_visits": c1, "face_sightings": c2}

            # VACUUM completo es caro — usar WAL checkpoint en su lugar
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        return result

    def delete_snapshot_row(self, snapshot_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM snapshots WHERE id = ?", (snapshot_id,)
            )
            return cur.rowcount > 0

    def all_snapshot_paths(self) -> list[tuple[int, str]]:
        """Para retention: lista (id, path) de todos los snapshots indexados."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path FROM snapshots"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
        except Exception:
            pass
