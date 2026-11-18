"""Registro local (SQLite) de los equipos Tuya y de sus cambios en la nube.

Fuente: el servicio Tuya (oapc-devops) lee de la nube la lista de equipos controlables (GET /registry, POST /registry/sync).
Aquí se compara con la lista de equipos de la casa (thor-vision, iot.yml) y se guarda:
  - dispositivos: cada equipo conocido, su casa, si está en la nube y dentro del cupo de control, en línea o no;
  - cambios: lo que se detectó entre una sincronización y la siguiente (nuevo, renombrado, salió de la nube, otra casa...).

Estado de nube por equipo (campo `nube`):
  cupo       en la lista de la nube: las órdenes por la nube funcionan
  sin_cupo   en iot.yml pero fuera de la lista: la nube rechaza sus órdenes («pool quota is insufficient»)
  retirado   marcado retired en iot.yml (ya no existe en la nube; era el «permission deny»)
             (un equipo con cloud:false pero no retirado, como la cortina y el gateway, cuenta como sin_cupo: «sin permiso en la nube»)
  desconocido  aún no se ha sincronizado
Filtro por casa: solo cuenta la casa PATCC_TUYA_HOME (Patricio 10). Un equipo de otra casa nunca se muestra ni se controla.
"""
import os
import sqlite3
import threading
import time

import httpx

from patcc import consola, files

SVC = os.environ.get("TUYA_SERVICE_URL", "http://192.168.0.194:8765").rstrip("/")
HOME = os.environ.get("PATCC_TUYA_HOME", "153519480")             # Patricio 10; Coworking540 = 195128033 (nunca entra)
EVERY_S = float(os.environ.get("PATCC_SYNC_MIN", "10")) * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispositivos (
  id TEXT PRIMARY KEY, nombre TEXT, casa TEXT, categoria TEXT, nube TEXT, online INTEGER,
  primero_ts REAL, visto_ts REAL, cambio_ts REAL);
CREATE TABLE IF NOT EXISTS cambios (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, device_id TEXT, nombre TEXT, tipo TEXT, antes TEXT, despues TEXT);
CREATE INDEX IF NOT EXISTS cambios_ts ON cambios(ts);
CREATE TABLE IF NOT EXISTS sync (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ok INTEGER, ms INTEGER, nube INTEGER, detalle TEXT);
"""


class Registro:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = None
        self._stop = threading.Event()
        self.last = {"ts": 0.0, "ok": None, "ms": 0, "detalle": "sin sincronizar", "nube": 0}

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            os.makedirs(files.DATA, exist_ok=True)
            self._conn = sqlite3.connect(files.path("registro.db"), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT ts, ok, ms, nube, detalle FROM sync ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                self.last = dict(row)
        return self._conn

    # ── sincronización ─────────────────────────────────────────────
    def start(self, devices_fn) -> None:
        threading.Thread(target=self._loop, args=(devices_fn,), daemon=True, name="patcc-registro").start()

    def _loop(self, devices_fn) -> None:
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                self.sync(devices_fn(), manual=False)
            except Exception as exc:                  # noqa: BLE001 - el ciclo nunca debe morir
                consola.log("error", "registro", f"ciclo: {type(exc).__name__}: {exc}")
            self._stop.wait(EVERY_S)

    def sync(self, house_devices: list, manual: bool = True) -> dict:
        """house_devices: equipos de la casa (los de iot.yml, vía thor-vision). Devuelve el resumen de la sincronización."""
        t0 = time.time()
        try:
            r = httpx.post(SVC + "/registry/sync", timeout=40) if manual else httpx.get(SVC + "/registry", timeout=40)
            r.raise_for_status()
            reg = r.json()
            if reg.get("error"):
                raise ValueError(reg["error"])
        except (httpx.HTTPError, ValueError) as exc:
            return self._record(False, t0, 0, f"{type(exc).__name__}: {str(exc)[:100]}")
        cloud = {d["id"]: d for d in reg.get("devices") or []}
        with self._lock:
            db = self._db()
            now = time.time()
            known = {r_["id"]: dict(r_) for r_ in db.execute("SELECT * FROM dispositivos")}

            def change(dev_id, nombre, tipo, antes, despues):
                db.execute("INSERT INTO cambios (ts, device_id, nombre, tipo, antes, despues) VALUES (?,?,?,?,?,?)", (now, dev_id, nombre, tipo, antes, despues))
                consola.log("info", "registro", f"{tipo}: {nombre} {antes or ''} → {despues or ''}".strip())

            seen = set()
            for d in house_devices:
                i = d["device_id"]
                seen.add(i)
                c = cloud.get(i)
                nube = "retirado" if d.get("retired") else ("cupo" if c else "sin_cupo")
                casa = str(c.get("owner_id")) if c else HOME
                row = {"id": i, "nombre": d.get("name"), "casa": casa, "categoria": (c or {}).get("category"), "nube": nube,
                       "online": None if not c else int(bool(c.get("online")))}
                old = known.get(i)
                if old is None:
                    change(i, row["nombre"], "nuevo", None, nube)
                    db.execute("INSERT INTO dispositivos VALUES (?,?,?,?,?,?,?,?,?)", (i, row["nombre"], casa, row["categoria"], nube, row["online"], now, now, now))
                    continue
                if old["nube"] != nube:
                    change(i, row["nombre"], "nube", old["nube"], nube)
                    old["cambio_ts"] = now
                if old["nombre"] != row["nombre"]:
                    change(i, row["nombre"], "renombrado", old["nombre"], row["nombre"])
                    old["cambio_ts"] = now
                if row["online"] is not None and old["online"] is not None and old["online"] != row["online"]:
                    change(i, row["nombre"], "en_linea" if row["online"] else "sin_linea", None, None)
                    old["cambio_ts"] = now
                if str(old["casa"]) != casa:
                    change(i, row["nombre"], "casa", old["casa"], casa)
                    old["cambio_ts"] = now
                db.execute("UPDATE dispositivos SET nombre=?, casa=?, categoria=?, nube=?, online=COALESCE(?, online), visto_ts=?, cambio_ts=? WHERE id=?",
                           (row["nombre"], casa, row["categoria"] or old["categoria"], nube, row["online"], now, old.get("cambio_ts") or now, i))
            # equipos de la nube que la casa no tiene en iot.yml: nuevos detectados (de Patricio 10) u otra casa (se ignoran, solo se anota)
            for i, c in cloud.items():
                if i in seen:
                    continue
                tipo = "detectado" if str(c.get("owner_id")) == HOME else "otra_casa"
                row = db.execute("SELECT id FROM dispositivos WHERE id=?", (i,)).fetchone()
                if row is None:
                    change(i, c.get("name"), tipo, None, str(c.get("owner_id")))
                    db.execute("INSERT INTO dispositivos VALUES (?,?,?,?,?,?,?,?,?)",
                               (i, c.get("name"), str(c.get("owner_id")), c.get("category"), "fuera_de_lista" if tipo == "detectado" else "otra_casa", int(bool(c.get("online"))), now, now, now))
                else:
                    db.execute("UPDATE dispositivos SET visto_ts=?, online=? WHERE id=?", (now, int(bool(c.get("online"))), i))
            db.commit()
        out = self._record(True, t0, len(cloud), f"{len(cloud)} equipos en la nube")
        return out

    def _record(self, ok: bool, t0: float, n: int, detalle: str) -> dict:
        ms = round((time.time() - t0) * 1000)
        with self._lock:
            self._db().execute("INSERT INTO sync (ts, ok, ms, nube, detalle) VALUES (?,?,?,?,?)", (time.time(), int(ok), ms, n, detalle))
            self._db().execute("DELETE FROM sync WHERE id NOT IN (SELECT id FROM sync ORDER BY id DESC LIMIT 200)")
            self._db().commit()
            self.last = {"ts": time.time(), "ok": ok, "ms": ms, "nube": n, "detalle": detalle}
        consola.log("info" if ok else "error", "registro", f"sincronización {'ok' if ok else 'FALLÓ'}: {detalle} ({ms} ms)")
        return dict(self.last)

    # ── consulta ───────────────────────────────────────────────────
    def states(self) -> dict:
        """device_id -> {nube, casa, online}; vacío si nunca se sincronizó."""
        with self._lock:
            return {r["id"]: {"nube": r["nube"], "casa": r["casa"], "online": r["online"]} for r in self._db().execute("SELECT * FROM dispositivos")}

    def summary(self) -> dict:
        with self._lock:
            db = self._db()
            rows = [dict(r) for r in db.execute("SELECT * FROM dispositivos ORDER BY nombre COLLATE NOCASE")]
            cambios = [dict(r) for r in db.execute("SELECT * FROM cambios ORDER BY id DESC LIMIT 60")]
        por = {}
        for r in rows:
            por.setdefault(r["nube"], []).append(r["nombre"])
        return {"last": self.last, "home": HOME, "counts": {k: len(v) for k, v in por.items()}, "names": por, "cambios": cambios}
