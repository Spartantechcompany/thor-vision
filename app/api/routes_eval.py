"""Conjunto de evaluacion de YOLO: cuadros reales de las camaras con las cajas de persona que dio el detector.
Tu marcas cuales cajas estan mal y que personas faltaron; con eso se calculan precision y recall por camara,
y sirve de verdad de terreno para comparar trackers o reentrenar. Los cuadros se muestrean solos cada EVAL_SAMPLE_EVERY_S."""
import json
import logging
import os
import threading
import time
import uuid

import cv2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

DIR = os.environ.get("EVAL_DIR", "/app/data/eval")
POOL, LABELS = os.path.join(DIR, "pool.jsonl"), os.path.join(DIR, "labels.jsonl")
EVERY_S = float(os.environ.get("EVAL_SAMPLE_EVERY_S", "1200"))
POOL_MAX = int(os.environ.get("EVAL_POOL_MAX", "1500"))
EMPTY_KEEP = int(os.environ.get("EVAL_EMPTY_ONE_IN", "4"))      # 1 de cada N cuadros sin personas (sirven para ver personas que YOLO no vio)
_lock = threading.Lock()
_empty_n = 0


def _read(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def sample_once(app) -> int:
    """Guarda un cuadro por camara con las cajas de YOLO de ese instante."""
    global _empty_n
    mgr, store = getattr(app.state, "capture_manager", None), getattr(app.state, "detection_store", None)
    if mgr is None or store is None:
        return 0
    os.makedirs(os.path.join(DIR, "frames"), exist_ok=True)
    n = 0
    for cam in app.state.config.cameras:
        frame, det = mgr.get_frame(cam.id), store.get(cam.id)
        if frame is None or det is None or not det.frame_w or time.time() - (det.yolo_ts or 0) > 5:
            continue
        boxes = [list(map(float, b)) for b in (det.person_bboxes or [])]
        if not boxes:
            with _lock:
                _empty_n += 1
                if _empty_n % EMPTY_KEEP:
                    continue
        h, w = frame.shape[:2]
        k = 1280 / w if w > 1280 else 1.0
        img = cv2.resize(frame, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else frame
        iid = uuid.uuid4().hex[:12]
        cv2.imwrite(os.path.join(DIR, "frames", iid + ".jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        sx, sy = 1.0 / det.frame_w, 1.0 / (det.frame_h or int(det.frame_w * h / w))
        rec = {"id": iid, "cam": cam.id, "ts": time.time(), "boxes": [[b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy] for b in boxes]}   # coordenadas 0..1
        with _lock, open(POOL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        n += 1
    _trim()
    return n


def _trim() -> None:
    with _lock:
        pool, done = _read(POOL), {x["id"] for x in _read(LABELS)}
        if len(pool) <= POOL_MAX:
            return
        drop = [p for p in pool if p["id"] not in done][: len(pool) - POOL_MAX]
        gone = {p["id"] for p in drop}
        for i in gone:
            try:
                os.remove(os.path.join(DIR, "frames", i + ".jpg"))
            except OSError:
                pass
        with open(POOL, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(p) + "\n" for p in pool if p["id"] not in gone)


def start_sampler(app) -> threading.Event:
    stop = threading.Event()

    def loop():
        stop.wait(120)
        while not stop.is_set():
            try:
                sample_once(app)
            except (OSError, ValueError, cv2.error) as exc:
                logger.warning("eval: %s", exc)
            stop.wait(EVERY_S)
    threading.Thread(target=loop, daemon=True, name="eval-sampler").start()
    return stop


class Label(BaseModel):
    id: str
    wrong: list[int] = []                 # indices de cajas que NO son una persona
    missed: list[list[float]] = []        # personas que YOLO no marco, x1,y1,x2,y2 en 0..1
    skip: bool = False


@router.post("/api/eval/sample")
def sample_now(request: Request):
    return {"added": sample_once(request.app)}


@router.get("/api/eval/next")
def nxt(cam: str = ""):
    done = {x["id"] for x in _read(LABELS)}
    todo = [p for p in _read(POOL) if p["id"] not in done and (not cam or p["cam"] == cam)]
    # primero las camaras con menos cuadros etiquetados, para que el conjunto quede parejo
    per = {}
    for x in _read(LABELS):
        per[x["cam"]] = per.get(x["cam"], 0) + 1
    todo.sort(key=lambda p: (per.get(p["cam"], 0), p["ts"]))
    return {"item": todo[0] if todo else None, "pending": len(todo)}


@router.get("/api/eval/image/{iid}")
def image(iid: str):
    if not iid.isalnum() or len(iid) > 32:
        raise HTTPException(400, "id invalido")
    p = os.path.join(DIR, "frames", iid + ".jpg")
    if not os.path.exists(p):
        raise HTTPException(404, "no existe")
    return FileResponse(p, media_type="image/jpeg")


@router.post("/api/eval/label")
def label(body: Label):
    item = next((p for p in _read(POOL) if p["id"] == body.id), None)
    if item is None:
        raise HTTPException(404, "cuadro desconocido")
    rec = {"id": body.id, "cam": item["cam"], "ts": item["ts"], "n_boxes": len(item["boxes"]), "skip": body.skip,
           "wrong": sorted(set(i for i in body.wrong if 0 <= i < len(item["boxes"]))), "missed": body.missed[:20], "labeled_at": time.time()}
    with _lock, open(LABELS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return {"ok": True}


@router.get("/api/eval/report")
def report():
    last = {}
    for x in _read(LABELS):
        last[x["id"]] = x                      # si se reetiqueta, vale la ultima
    out, tot = {}, {"frames": 0, "boxes": 0, "wrong": 0, "missed": 0}
    for x in last.values():
        if x.get("skip"):
            continue
        o = out.setdefault(x["cam"], {"frames": 0, "boxes": 0, "wrong": 0, "missed": 0})
        for d in (o, tot):
            d["frames"] += 1
            d["boxes"] += x["n_boxes"]
            d["wrong"] += len(x["wrong"])
            d["missed"] += len(x["missed"])

    def met(d):
        tp = d["boxes"] - d["wrong"]
        p = tp / d["boxes"] if d["boxes"] else None
        r = tp / (tp + d["missed"]) if (tp + d["missed"]) else None
        return {**d, "precision": round(p, 3) if p is not None else None, "recall": round(r, 3) if r is not None else None}
    return {"total": met(tot), "per_cam": {c: met(d) for c, d in sorted(out.items())}, "pool": len(_read(POOL))}


TRACK_KEEP_DAYS = int(os.environ.get("EVAL_TRACKS_KEEP_DAYS", "7"))


def make_track_recorder():
    """Hook por cuadro en vivo: guarda las cajas de persona (con su tiempo) para repetir otros trackers fuera de linea con los mismos datos."""
    state = {"day": "", "f": None}

    def hook(cam_id, result, frame):
        boxes = getattr(result, "person_bboxes", None)
        if not boxes:
            return
        day = time.strftime("%Y%m%d")
        if day != state["day"]:
            if state["f"]:
                state["f"].close()
            os.makedirs(DIR, exist_ok=True)
            state["f"], state["day"] = open(os.path.join(DIR, f"tracks-{day}.jsonl"), "a", encoding="utf-8"), day
            old = time.time() - TRACK_KEEP_DAYS * 86400
            for n in os.listdir(DIR):
                if n.startswith("tracks-") and os.path.getmtime(os.path.join(DIR, n)) < old:
                    os.remove(os.path.join(DIR, n))
        state["f"].write(json.dumps({"t": round(time.time(), 2), "c": cam_id, "w": getattr(result, "frame_w", 0), "h": getattr(result, "frame_h", 0),
                                      "b": [[round(float(v), 1) for v in b] for b in boxes]}) + "\n")
        state["f"].flush()
    return hook
