"""Eventos nativos de Dahua y alertas del videoportero (visita con rostro y timbre)."""
import cv2
import numpy as np

from app.onvif import dahua_events as de
from app.vision import doorbell


def test_parse_single_line_event():
    blk = "Content-Type: text/plain\nContent-Length:60\n\nCode=BackKeyLight;action=Pulse;index=0;data={\"State\":1}\n"
    assert de.parse_event(blk) == ("BackKeyLight", "Pulse", 0, {"State": 1})


def test_parse_multiline_json_and_no_data():
    blk = 'Code=Invite;action=Start;index=0;data={\n   "CallID" : "1001",\n   "LockID" : 0\n}\n'
    assert de.parse_event(blk) == ("Invite", "Start", 0, {"CallID": "1001", "LockID": 0})
    assert de.parse_event("Code=DoorStatus;action=Pulse;index=1")[:3] == ("DoorStatus", "Pulse", 1)


def test_heartbeats_and_garbage_are_ignored():
    assert de.parse_event("Heartbeat") is None
    assert de.parse_event("") is None


class FakeDB:
    def __init__(self):
        self.events = []

    def insert_event(self, type, cam_id, payload, people=None, has_alert=False):
        self.events.append((type, cam_id, payload, has_alert))
        return len(self.events)


class FakeSnaps:
    def __init__(self):
        self.saved = []

    def save(self, cam_id, frame, trigger, event_id=None):
        self.saved.append((cam_id, trigger, event_id, frame is not None))


def jpeg():
    ok, buf = cv2.imencode(".jpg", np.zeros((40, 40, 3), np.uint8))
    return buf.tobytes()


def test_visit_with_face_creates_an_alert_with_snapshot():
    db, sn = FakeDB(), FakeSnaps()
    d = doorbell.DoorAlerts(db, sn, {}, "cam-vto", "Videoportero")
    d.visitor_alert({"visit_id": 7, "subject_id": 418, "name": None, "first_ts": 100.0, "last_ts": 118.0, "scene": None, "face": jpeg()})
    typ, cam, p, has_alert = db.events[0]
    assert typ == "nemotron" and cam == "cam-vto" and has_alert
    assert p["alert_types"] == ["visita_videoportero"] and p["doorbell"]["visit_id"] == 7 and "18 s" in p["activity"]
    assert sn.saved == [("cam-vto", "visita", 1, True)]


def test_very_short_visit_without_face_is_ignored():
    db = FakeDB()
    doorbell.DoorAlerts(db, None, {}).visitor_alert({"visit_id": 1, "subject_id": None, "name": None, "first_ts": 0.0, "last_ts": 0.5, "scene": None, "face": None})
    assert db.events == []


def test_ring_codes_alert_once_per_debounce_and_ignore_the_rest():
    db = FakeDB()
    d = doorbell.DoorAlerts(db, None, {})
    d.ring_alert("Invite", "Start", {"CallID": "1"})
    d.ring_alert("Invite", "Start", {"CallID": "1"})          # rebote
    d.ring_alert("VideoMotion", "Start", {})                  # no es timbre
    d.ring_alert("Invite", "Stop", {})                        # fin de llamada
    assert len(db.events) == 1 and db.events[0][2]["alert_types"] == ["timbre_videoportero"] and db.events[0][2]["severity"] == "medium"


def test_stream_reads_byte_lines_and_reports_events(monkeypatch):
    """Regresion: el equipo manda multipart sin charset y requests entrega bytes; antes el hilo moria con TypeError."""
    lines = [b"--myboundary", b"Content-Type: text/plain", b"", b"Heartbeat", b"--myboundary",
             b"Code=Invite;action=Start;index=0;data={", b'  "CallID": "7"', b"}", b"--myboundary"]

    class FakeResp:
        status_code = 200

        def iter_lines(self):
            yield from lines

        def close(self):
            pass

    monkeypatch.setattr(de.requests.Session, "get", lambda self, url, **kw: FakeResp())
    got = []
    try:
        de.stream_events("h", "u", "p", lambda: False, lambda *e: got.append(e))
    except de.DahuaError:
        pass                                              # al terminar el flujo se avisa que el equipo cerro la conexion
    assert got == [("Invite", "Start", 0, {"CallID": "7"})]


def test_dahua_noise_codes_are_not_stored():
    from app.onvif.service import NOISE_CODES
    assert {"SIPRegisterResult", "NTPAdjustTime", "Heartbeat"} <= NOISE_CODES and "CallNoAnswered" not in NOISE_CODES


def test_ring_alert_saves_snapshot_from_frame_entry():
    from types import SimpleNamespace
    from app.vision.doorbell import DoorAlerts
    saved = []

    class DB:
        def insert_event(self, *a, **k):
            return 7

    class Snaps:
        def save(self, cam, frame, trig, event_id=None):
            saved.append((cam, frame, trig, event_id))

    buf = SimpleNamespace(peek_latest=lambda: SimpleNamespace(frame="IMG"))
    d = DoorAlerts(DB(), Snaps(), {"cam-vto": buf})
    d.ring_alert("CallNoAnswered", "Start", {})
    assert saved == [("cam-vto", "IMG", "timbre", 7)]


def test_short_unknown_pass_is_traffic_not_a_visit_alert():
    from app.vision.doorbell import DoorAlerts
    ev = []

    class DB:
        def insert_event(self, *a, **k):
            ev.append(a)
            return 1

    d = DoorAlerts(DB(), None, {})
    d.visitor_alert({"visit_id": 1, "subject_id": None, "name": None, "first_ts": 100.0, "last_ts": 101.0, "scene": None, "face": b"x"})
    assert ev == [] and d.transit == 1                                   # paso 1 s sin timbre ni nombre
    d.visitor_alert({"visit_id": 2, "subject_id": 5, "name": "Neto", "first_ts": 100.0, "last_ts": 101.0, "scene": None, "face": b"x"})
    assert len(ev) == 1                                                  # con nombre si es visita
    d._last_ring["Invite"] = 150.0
    d.visitor_alert({"visit_id": 3, "subject_id": None, "name": None, "first_ts": 140.0, "last_ts": 142.0, "scene": None, "face": b"x"})
    assert len(ev) == 2                                                  # junto a un timbre tambien


def test_timeline_effective_type_splits_sensor_and_transit():
    from app.api.routes_timeline import _effective_type
    assert _effective_type({"alert_types": ["puerta_abierta"], "source": "iot"}) == "sensor_puerta"
    assert _effective_type({"alert_types": ["puerta_abierta"]}) == "puerta_abierta"
    assert _effective_type({"alert_types": ["visita_videoportero"], "source": "doorbell", "doorbell": {"duration_s": 0}, "activity": "Visita frente al videoportero: 0 s"}) == "trafico_calle"
    assert _effective_type({"alert_types": ["visita_videoportero"], "source": "doorbell", "doorbell": {"duration_s": 0}, "activity": "Visita frente al videoportero (Neto): 0 s"}) == "visita_videoportero"
