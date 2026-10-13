"""Ventana ancha de la linea de tiempo: no se pierde lo mas viejo, se agrupa."""
import json
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_timeline as rt


def _app(n_per_day=60, days=4):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, cam_id TEXT, type TEXT, people INT, has_alert INT, review_label TEXT, data TEXT);"
                       "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, event_id INT);"
                       "CREATE TABLE person_visits (id INTEGER PRIMARY KEY, subject_id INT, cam_id TEXT, start_ts REAL, end_ts REAL, hits INT, fp INT, static INT);"
                       "CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT, named INT, category TEXT);"
                       "CREATE TABLE onvif_events (ts REAL, endpoint TEXT, topic TEXT, state TEXT);"
                       "CREATE TABLE iot_events (ts REAL, name TEXT, kind TEXT, code TEXT, value TEXT, source TEXT);")
    base, i = 1_700_000_000.0, 0
    for d in range(days):
        for k in range(n_per_day):
            i += 1
            conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (i, base + d * 86400 + k * 600, "cam-1", "nemotron", 1, 1, None,
                                                                         json.dumps({"alert_types": ["persona_nocturna"], "activity": "x", "severity": "low"})))
    app = FastAPI()
    app.state.db = SimpleNamespace(_conn=conn, _lock=threading.RLock())
    app.state.config = SimpleNamespace(cameras=[SimpleNamespace(id="cam-1", name="C1", zone="interior")])
    app.include_router(rt.router)
    return app, base


def test_wide_window_keeps_old_days_and_counts_everything(monkeypatch):
    monkeypatch.setattr(rt, "_MAX_EVENTS", 100)
    app, base = _app()
    d = TestClient(app).get(f"/api/timeline?since={base - 10}&until={base + 4 * 86400}").json()
    days_seen = {int((e["ts"] - base) // 86400) for e in d["events"]}
    assert days_seen == {0, 1, 2, 3}                                   # antes solo salian los dias nuevos
    assert d["thinned"] and d["total"] == 240 and sum(e.get("n", 1) for e in d["events"]) == 240


def test_small_window_is_unchanged(monkeypatch):
    app, base = _app()
    d = TestClient(app).get(f"/api/timeline?since={base - 10}&until={base + 3600}").json()
    assert not d["thinned"] and all("n" not in e for e in d["events"]) and len(d["events"]) == 7
