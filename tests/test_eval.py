"""Conjunto de evaluacion de YOLO: el informe calcula precision y recall a partir de las marcas."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_eval as ev


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "DIR", str(tmp_path))
    monkeypatch.setattr(ev, "POOL", str(tmp_path / "pool.jsonl"))
    monkeypatch.setattr(ev, "LABELS", str(tmp_path / "labels.jsonl"))
    (tmp_path / "pool.jsonl").write_text("".join(json.dumps(p) + "\n" for p in [
        {"id": "a1", "cam": "cam-1", "ts": 1.0, "boxes": [[0, 0, .1, .2], [.5, .5, .6, .9], [.7, .1, .8, .3]]},
        {"id": "b2", "cam": "cam-2", "ts": 2.0, "boxes": [[0, 0, .2, .4]]}]))
    app = FastAPI()
    app.include_router(ev.router)
    return TestClient(app)


def test_next_label_and_report(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/eval/next").json()["pending"] == 2
    c.post("/api/eval/label", json={"id": "a1", "wrong": [1], "missed": [[.2, .2, .3, .5]]})        # 3 cajas, 1 falsa, 1 persona no vista
    c.post("/api/eval/label", json={"id": "b2", "wrong": []})
    r = c.get("/api/eval/report").json()["total"]
    assert (r["frames"], r["boxes"], r["wrong"], r["missed"]) == (2, 4, 1, 1)
    assert r["precision"] == 0.75 and r["recall"] == 0.75
    assert c.get("/api/eval/next").json()["item"] is None


def test_skipped_frames_do_not_count_and_bad_ids_are_rejected(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    c.post("/api/eval/label", json={"id": "a1", "skip": True})
    assert c.get("/api/eval/report").json()["total"]["frames"] == 0
    assert c.get("/api/eval/image/..%2Fetc").status_code in (400, 404)
    assert c.post("/api/eval/label", json={"id": "zzz"}).status_code == 404


def test_track_recorder_writes_only_frames_with_people(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(ev, "DIR", str(tmp_path))
    hook = ev.make_track_recorder()
    hook("cam-1", SimpleNamespace(person_bboxes=[], frame_w=1, frame_h=1), None)
    hook("cam-1", SimpleNamespace(person_bboxes=[(1.0, 2.0, 3.0, 4.0)], frame_w=640, frame_h=360), None)
    lines = [json.loads(x) for f in tmp_path.glob("tracks-*.jsonl") for x in f.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["c"] == "cam-1" and lines[0]["b"] == [[1.0, 2.0, 3.0, 4.0]] and lines[0]["w"] == 640
