"""Operador ONVIF: sondeo de cada equipo (info, hora, perfiles, usuarios) y escucha continua de eventos (timbre, puerta, movimiento)."""
import json
import logging
import os
import sqlite3
import threading
import time

from app.onvif import client as oc
from app.onvif import dahua_events as de
from app.onvif import registry

logger = logging.getLogger(__name__)

NOISE_CODES = {"Heartbeat", "SIPRegisterResult", "NTPAdjustTime"}      # latidos del equipo: no son eventos de la puerta
RETENTION_DAYS = int(os.environ.get("ONVIF_EVENTS_RETENTION_DAYS", "90"))
_SCHEMA = """
CREATE TABLE IF NOT EXISTS onvif_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, endpoint TEXT NOT NULL, topic TEXT NOT NULL, data TEXT, state TEXT);
CREATE INDEX IF NOT EXISTS idx_onvif_ts ON onvif_events(ts);
CREATE INDEX IF NOT EXISTS idx_onvif_ep ON onvif_events(endpoint, ts);
"""
_VALUE_KEYS = ("State", "IsMotion", "LogicalState", "IsPresent")


class OnvifManager:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self._probes: dict = {}
        self._threads: dict = {}
        self._stop = threading.Event()
        self._status: dict = {}
        self.dahua_cb = None          # callable(code, action, data): alertas de timbre (lo fija main.py)
        with db._lock:
            db._conn.executescript(_SCHEMA)

    # ---- sondeo
    def _creds(self, ep_id: str) -> tuple:
        return registry.auth(ep_id)

    def probe(self, ep_id: str) -> dict:
        ep = registry.get(self.config, ep_id)
        if ep is None:
            raise KeyError(ep_id)
        host, port = ep["host"], int(ep.get("port", 80))
        res = {"ts": time.time(), "ok": False, "error": "", "reachable": False, "authenticated": False}
        try:
            res["datetime"] = oc.get_datetime(host, port)
            res["reachable"] = True
        except oc.OnvifError as exc:
            res["error"] = str(exc)
            self._probes[ep_id] = res
            return res
        off = res["datetime"]["skew_s"]
        user, pw = self._creds(ep_id)
        if not user:
            res["error"] = "sin usuario ONVIF"
            self._probes[ep_id] = res
            return res
        try:
            res["info"] = oc.get_device_info(host, port, user, pw, off)
            res["authenticated"] = True
            services = oc.get_services(host, port, user, pw, off)
            res["services"] = sorted(services)
            media = oc._rewrite(services.get("media", ""), host, port) if services.get("media") else ""
            res["profiles"] = []
            if media:
                for p in oc.get_profiles(media, user, pw, off):
                    try:
                        p["uri"] = oc.get_stream_uri(media, p["token"], user, pw, off)
                    except oc.OnvifError:
                        p["uri"] = ""
                    res["profiles"].append(p)
            try:
                res["users"] = oc.get_users(host, port, user, pw, off)
            except oc.OnvifError:
                res["users"] = []
            res["events"] = "events" in services
            res["ok"] = True
        except oc.OnvifError as exc:
            res["error"] = str(exc)
        self._probes[ep_id] = res
        return res

    def last_probe(self, ep_id: str) -> dict:
        return self._probes.get(ep_id) or {}

    def live_events(self, ep_id: str, seconds: int = 10) -> list:
        ep = registry.get(self.config, ep_id)
        user, pw = self._creds(ep_id)
        host, port = ep["host"], int(ep.get("port", 80))
        off = oc.get_datetime(host, port)["skew_s"]
        services = oc.get_services(host, port, user, pw, off)
        ev = oc._rewrite(services["events"], host, port)
        return oc.pull_events(ev, user, pw, min(max(seconds, 2), 20), off)

    # ---- escucha continua
    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True, name="onvif-supervisor").start()

    def stop(self) -> None:
        self._stop.set()

    def status(self, ep_id: str) -> dict:
        s = dict(self._status.get(ep_id, {}))
        d = self._status.get(ep_id + ":dahua")
        if d is not None:
            s["dahua"] = d
        return s

    def _supervise(self) -> None:
        last_purge = 0.0
        while not self._stop.is_set():
            for ep in registry.endpoints(self.config):
                t = self._threads.get(ep["id"])
                if ep.get("kind") == "vto" and ep.get("listen") and registry.has_auth(ep["id"]):
                    td = self._threads.get(ep["id"] + ":dahua")
                    if td is None or not td.is_alive():
                        th = threading.Thread(target=self._listen_dahua, args=(ep["id"],), daemon=True, name=f"dahua-{ep['id']}")
                        self._threads[ep["id"] + ":dahua"] = th
                        th.start()
                if ep.get("listen") and registry.has_auth(ep["id"]) and (t is None or not t.is_alive()):
                    th = threading.Thread(target=self._listen, args=(ep["id"],), daemon=True, name=f"onvif-{ep['id']}")
                    self._threads[ep["id"]] = th
                    th.start()
            if time.time() - last_purge > 86400:
                last_purge = time.time()
                with self.db._lock:
                    self.db._conn.execute("DELETE FROM onvif_events WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
                    self.db._conn.execute("DELETE FROM onvif_events WHERE topic IN ('dahua/SIPRegisterResult','dahua/NTPAdjustTime')")
                    self.db._conn.commit()
            self._stop.wait(15)

    def _store(self, ep_id: str, topic: str, data: dict, state: str) -> None:
        try:
            with self.db._lock:
                self.db._conn.execute("INSERT INTO onvif_events (ts, endpoint, topic, data, state) VALUES (?,?,?,?,?)",
                                      (time.time(), ep_id, topic, json.dumps(data, ensure_ascii=False), state))
                self.db._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("onvif: no se pudo guardar evento: %s", exc)

    def _listen_dahua(self, ep_id: str) -> None:
        """Eventos nativos de Dahua (timbre, llamada, puerta). Si la cuenta no tiene permiso queda el error en el estado."""
        backoff = 5
        key = ep_id + ":dahua"
        while not self._stop.is_set():
            ep = registry.get(self.config, ep_id)
            if not ep or not ep.get("listen") or not registry.has_auth(ep_id):
                self._status[key] = {"listening": False}
                return
            user, pw = self._creds(ep_id)

            def on_event(code, action, index, data):
                if code in NOISE_CODES:
                    return
                self._store(ep_id, f"dahua/{code}", {**data, "index": index}, action)
                if self.dahua_cb is not None:
                    self.dahua_cb(code, action, data)
            try:
                self._status[key] = {"listening": True, "since": time.time()}
                de.stream_events(ep["host"], user, pw, self._stop.is_set, on_event)
            except de.DahuaError as exc:
                self._status[key] = {"listening": False, "error": str(exc)[:120]}
                logger.warning("dahua[%s]: %s (reintento en %ds)", ep_id, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def _listen(self, ep_id: str) -> None:
        last: dict = {}
        backoff = 5
        while not self._stop.is_set():
            ep = registry.get(self.config, ep_id)
            if not ep or not ep.get("listen") or not registry.has_auth(ep_id):
                self._status[ep_id] = {"listening": False}
                return
            user, pw = self._creds(ep_id)
            host, port = ep["host"], int(ep.get("port", 80))
            try:
                off = oc.get_datetime(host, port)["skew_s"]
                services = oc.get_services(host, port, user, pw, off)
                ev = oc._rewrite(services["events"], host, port)
                while not self._stop.is_set():
                    pp = oc.open_pullpoint(ev, user, pw, off, ttl_s=120)
                    t_end = time.time() + 100
                    self._status[ep_id] = {"listening": True, "since": time.time()}
                    while time.time() < t_end and not self._stop.is_set():
                        for e in oc.pull(pp, user, pw, 10, off):
                            keys = tuple(sorted((k, v) for k, v in e["data"].items() if k not in _VALUE_KEYS))
                            state = next((e["data"][k] for k in _VALUE_KEYS if k in e["data"]), "")
                            if last.get((e["topic"], keys)) != state:
                                last[(e["topic"], keys)] = state
                                self._store(ep_id, e["topic"], e["data"], state)
                    backoff = 5
            except (oc.OnvifError, KeyError) as exc:
                self._status[ep_id] = {"listening": False, "error": str(exc)[:120]}
                logger.warning("onvif[%s]: %s (reintento en %ds)", ep_id, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def recent_events(self, ep_id=None, limit: int = 100, since: float = 0.0) -> list:
        sql, args = "SELECT id, ts, endpoint, topic, data, state FROM onvif_events WHERE ts>=?", [since]
        if ep_id:
            sql += " AND endpoint=?"
            args.append(ep_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        with self.db._lock:
            rows = self.db._conn.execute(sql, args).fetchall()
        out = []
        for i, ts, ep, topic, data, state in rows:
            try:
                d = json.loads(data or "{}")
            except ValueError:
                d = {}
            out.append({"id": i, "ts": ts, "endpoint": ep, "topic": topic, "data": d, "state": state})
        return out
