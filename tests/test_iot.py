"""Eventos IoT por MQTT (solo lectura): estado, alertas de puerta abierta, abierta mucho tiempo y bateria baja."""
import json
import sqlite3
import threading
import time

from app.iot import listener as li


class FakeDB:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:")
        self.events = []

    def insert_event(self, type, cam_id, payload, people=None, has_alert=False):
        self.events.append((type, cam_id, payload, has_alert))
        return len(self.events)


class FakeSnaps:
    def __init__(self):
        self.saved = []

    def save(self, cam_id, frame, trigger, event_id=None):
        self.saved.append((cam_id, trigger, event_id))


class FakeBuf:
    def peek_latest(self):
        return object()


def make():
    db, sn = FakeDB(), FakeSnaps()
    m = li.IotListener(db, sn, {"cam-113": FakeBuf()}, {"cam-113": "Escaleras entrada"})
    m.cfg = {"alert_open_minutes": 10, "low_battery_pct": 15}
    m.devices = {"D1": {"name": "Recepcion Puerta", "kind": "door", "cam": "cam-113", "confirmed": False, "open_code": "doorcontact_state", "open_value": True},
                 "C1": {"name": "Camara", "kind": "camera", "cam": None}}
    return m, db, sn


def msg(dev, changes, ts=None):
    return json.dumps({"device_id": dev, "ts": ts or time.time(), "changes": changes}).encode()


def test_repo_map_parses_and_has_the_sensors():
    cfg = li.load_map("config/iot.yml")
    assert cfg["devices"]["ebd84e19bf42731470j1ao"]["kind"] == "door" and cfg["alert_open_minutes"] == 0


def test_is_open():
    d = {"open_code": "doorcontact_state", "open_value": True}
    assert li.is_open(d, {"doorcontact_state": True}) is True and li.is_open(d, {"doorcontact_state": False}) is False
    assert li.is_open(d, {"battery_percentage": 50}) is None


def test_startup_state_and_retained_never_alert_but_are_stored():
    m, db, sn = make()
    m.handle("oxm/tuya/D1/last", msg("D1", {"doorcontact_state": True}), retained=True)
    m.handle("oxm/tuya/D1/status", msg("D1", {"doorcontact_state": True}))            # primer estado conocido
    assert db.events == [] and m.state["D1"]["open"] is True
    assert {r["source"] for r in m.recent()} == {"sync", "event"}


def test_opening_creates_alert_with_snapshot_and_closing_has_one_too():
    m, db, sn = make()
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": False}))
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": True}))
    assert len(db.events) == 1
    typ, cam, p, has_alert = db.events[0]
    assert typ == "nemotron" and cam == "cam-113" and has_alert and p["source"] == "iot"
    assert p["alert_types"] == ["puerta_abierta"] and p["iot"]["state"] == "open" and p["iot"]["cam_confirmed"] is False
    assert sn.saved == [("cam-113", "puerta", 1)]
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": False}))                  # cerrarse deja una entrada informativa (severidad none) con su captura
    assert len(db.events) == 2 and m.state["D1"]["open"] is False and db.events[1][2]["severity"] == "none" and db.events[1][2]["iot"]["state"] == "closed"
    assert len(sn.saved) == 2 and sn.saved[1][1] == "puerta_cierre"
    assert "s)" in db.events[1][2]["activity"]                                          # menos de un minuto se muestra en segundos


def test_open_too_long_alerts_once_per_interval():
    m, db, sn = make()
    t0 = 1_000_000.0
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": False}, t0))
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": True}, t0 + 10))
    n = len(db.events)
    m.check_open_too_long(t0 + 10 + 300)                                                # 5 min: aun no
    assert len(db.events) == n
    m.check_open_too_long(t0 + 10 + 11 * 60)
    assert len(db.events) == n + 1 and "min abierta" in db.events[-1][2]["activity"]
    m.check_open_too_long(t0 + 10 + 12 * 60)                                            # no repite enseguida
    assert len(db.events) == n + 1


def test_low_battery_is_not_an_alert_and_unknown_devices_are_ignored():
    m, db, sn = make()
    m.handle("t/D1/status", msg("D1", {"battery_percentage": 1}))
    m.handle("t/D1/status", msg("D1", {"battery_percentage": 1}))
    assert db.events == []                    # la bateria se ve en la tarjeta del sensor, no como alerta
    m.handle("t/XX/status", msg("XX", {"doorcontact_state": True}))
    assert "XX" not in m.state


def test_garbage_messages_are_ignored():
    m, db, _ = make()
    m.handle("t/D1/status", b"no es json")
    m.handle("t/D1/status", msg("D1", {}))
    assert db.events == []


def test_usage_alert_once_per_threshold_and_never_from_retained():
    m, db, _ = make()
    msg = lambda t, p: json.dumps({"ts": time.time(), "pct": p, "threshold": t, "quota_gb": 5}).encode()
    m.handle("oxm/tuya/_usage", msg(50, 51.0), retained=True)          # estado viejo al arrancar: no alerta
    assert db.events == []
    m.handle("oxm/tuya/_usage", msg(80, 82.0))
    m.handle("oxm/tuya/_usage", msg(80, 83.0))                         # mismo umbral: una sola vez
    assert [e[2]["alert_types"] for e in db.events] == [["consumo_video_tuya"]] and db.events[0][2]["severity"] == "medium"


def _im(cmd="ipc_motion", t=1791154788):
    import base64
    return base64.b64encode(json.dumps({"v": "4.0", "cmd": cmd, "time": t, "alarm": True}).encode()).decode()


def test_camera_message_is_decoded_deduped_and_alerts_once_per_cooldown(monkeypatch):
    m, db, sn = make()
    grabbed = []
    monkeypatch.setattr(m, "_grab_frame", lambda *a: grabbed.append(a))
    t = time.time()
    for _ in range(2):                                              # la nube entrega cada mensaje duplicado
        m.handle("t/C1/status", msg("C1", {"initiative_message": _im(), "dp239": "motion"}, ts=t))
    codes = [r["code"] for r in m.recent("C1")]
    assert codes.count("ipc_motion") == 1 and codes.count("dp239") == 1
    assert len(db.events) == 1 and db.events[0][2]["alert_types"] == ["movimiento_tuya"]
    m.handle("t/C1/status", msg("C1", {"initiative_message": _im("ipc_bang")}, ts=t + 30))   # dentro del enfriamiento: sin alerta nueva
    assert len(db.events) == 1
    m.handle("t/C1/status", msg("C1", {"initiative_message": _im("ipc_bang")}, ts=t + 500))
    assert len(db.events) == 2 and db.events[1][2]["alert_types"] == ["ruido_tuya"]


def test_snapshot_accepts_frame_entry_and_listener_survives_errors():
    from types import SimpleNamespace
    m, db, sn = make()
    m.buffers = {"cam-113": SimpleNamespace(peek_latest=lambda: SimpleNamespace(frame="IMG", timestamp=1.0))}
    m._snapshot(5, "cam-113", "puerta")
    assert sn.saved == [("cam-113", "puerta", 5)]
    m._safe(lambda: 1 / 0 if False else {}["x"])              # un KeyError no se propaga


def test_door_open_notifies_journeys_and_state_exposes_related_and_online():
    m, db, sn = make()
    m.devices["D1"].update(alias="door-recepcion", related=["cam-113", "tuya-jardin"], role="Inicia el viaje")
    m.devices["C1"]["alias"] = "tuya-jardin"
    m.devices_by_alias = {"tuya-jardin": "Jardin camara"}
    got = []
    m.on_motion = lambda alias, ts: got.append(alias)
    m.handle("t/D1/last", msg("D1", {"doorcontact_state": False}), retained=True)       # estado actual de la nube: no alerta
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": True}))
    assert got == ["door-recepcion"]
    s = {d["device_id"]: d for d in m.snapshot_state()}["D1"]
    assert s["role"] == "Inicia el viaje" and [r["id"] for r in s["related"]] == ["cam-113", "tuya-jardin"] and s["open"] is True
    m.handle("t/D1/last", json.dumps({"device_id": "D1", "ts": time.time(), "changes": {"battery_percentage": 80}, "online": False}).encode(), retained=True)
    assert {d["device_id"]: d for d in m.snapshot_state()}["D1"]["online"] is False


def test_cloud_sync_messages_never_alert_or_count_as_events():
    m, db, sn = make()
    m.handle("t/D1/status", msg("D1", {"doorcontact_state": False}))
    sync = json.dumps({"device_id": "D1", "ts": time.time(), "changes": {"doorcontact_state": True}, "online": True, "source": "cloud-sync"}).encode()
    m.handle("t/D1/last", sync, retained=False)                       # llega en vivo (sin la marca de retenido) pero es una sincronizacion
    assert db.events == [] and m.state["D1"]["open"] is True
    assert m.recent("D1")[0]["source"] == "sync"


def test_lights_track_state_without_alerts():
    m, db, _ = make()
    m.devices["L1"] = {"name": "Taller", "kind": "light", "cam": None}
    m.handle("t/L1/status", msg("L1", {"switch_1": False, "switch_2": False}), retained=True)
    m.handle("t/L1/status", msg("L1", {"switch_2": True}))
    assert m.state["L1"]["open"] is True
    m.handle("t/L1/status", msg("L1", {"switch_2": False}))
    assert m.state["L1"]["open"] is False
    assert db.events == []                    # encender o apagar no es alerta


def test_best_cam_prefers_the_related_camera_that_sees_a_person():
    from types import SimpleNamespace
    m, db, sn = make()
    now = time.time()
    det = {"cam-228": SimpleNamespace(yolo_ts=now, yolo_persons=0, person_count=0), "cam-118": SimpleNamespace(yolo_ts=now, yolo_persons=2, person_count=2)}
    m.detections = SimpleNamespace(get=lambda c: det.get(c))
    dev = {"cam": "cam-228", "related": ["cam-228", "cam-118", "tuya-garaje"]}
    assert m._best_cam(dev) == "cam-118"
    det["cam-118"].yolo_persons = det["cam-118"].person_count = 0
    assert m._best_cam(dev) == "cam-228"                         # nadie a la vista: la camara asignada


def test_zone_with_several_cameras_saves_one_mosaic():
    import numpy as np
    from types import SimpleNamespace

    class Buf:
        def __init__(self, w, h):
            self.f = np.full((h, w, 3), 90, np.uint8)

        def peek_latest(self):
            return SimpleNamespace(frame=self.f)
    m, db, sn = make()
    m.buffers = {"cam-228": Buf(1280, 720), "cam-118": Buf(1280, 720), "cam-113": Buf(1080, 1920)}
    m.cam_names = {"cam-228": "Garage frontal", "cam-118": "Garage posterior", "cam-113": "Escaleras entrada"}
    shots = []
    sn.save = lambda cam, frame, trig, event_id=None: shots.append((cam, frame.shape, trig, event_id))
    dev = {"name": "Garage", "kind": "garage", "cam": "cam-228", "related": ["cam-228", "cam-118", "cam-113", "tuya-garaje"]}
    m._snapshot_zone(7, dev, "puerta")
    assert len(shots) == 1 and shots[0][0] == "cam-228" and shots[0][1] == (720, 1280, 3) and shots[0][3] == 7        # 2x2 de 640x360
