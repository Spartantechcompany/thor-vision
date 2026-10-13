"""Repite trackers sobre las cajas de persona grabadas (data/eval/tracks-*.jsonl) y mide fragmentacion sin necesidad de etiquetas.
Metricas por camara: pistas creadas, 'visitas' (>=4 detecciones), pistas simultaneas maximas (cota inferior de personas reales),
razon visitas/personas-maximas (1.0 = sin fragmentar) y % de visitas que se solapan en el tiempo con otra de caja parecida (duplicados).
Uso: python scripts/track_replay.py [archivo.jsonl ...]   (dentro del contenedor: python3 /app/scripts/track_replay.py)"""
import collections
import glob
import json
import sys
import types

import os
import time

import numpy as np

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))


def load(paths):
    by = collections.defaultdict(list)
    for p in paths:
        for line in open(p, encoding="utf-8"):
            d = json.loads(line)
            by[d["c"]].append((d["t"], [tuple(b) for b in d["b"]], d.get("w") or 1, d.get("h") or 1))
    for v in by.values():
        v.sort(key=lambda r: r[0])
    return by


def _iou(a, b):
    iw, ih = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    i = iw * ih
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)


def run_actual(frames, cam):
    """El tracker de produccion (VisitManager._match + dedupe + reenlace), sin base de datos."""
    from app.vision import visits as V
    vm = V.VisitManager.__new__(V.VisitManager)
    vm._next_tid = {}
    tracks, hist = [], {}
    for t, boxes, _, _ in frames:
        tracks[:] = [x for x in tracks if t - x.last_ts <= V.MAX_GAP_S]          # como _housekeeping: una pista sin verse mas de MAX_GAP_S se cierra
        persons = V._dedupe(boxes)
        for tr, pb in zip(vm._match(cam, tracks, persons, t), persons):
            tr.observe(pb, t)
            hist.setdefault(tr.id, []).append((t, pb))
    return hist


def run_byte(frames, cam, buffer=30, match=0.8, high=0.25, new=0.25):
    from ultralytics.trackers.byte_tracker import BYTETracker
    args = types.SimpleNamespace(track_high_thresh=high, track_low_thresh=0.1, new_track_thresh=new, track_buffer=buffer, match_thresh=match, fuse_score=False)
    tr = BYTETracker(args)
    hist = {}

    from ultralytics.engine.results import Boxes
    for t, boxes, w, h in frames:
        arr = np.array([[*b, 0.6, 0.0] for b in boxes], np.float32).reshape(-1, 6)
        out = tr.update(Boxes(arr, (max(1, h), max(1, w))), None)
        for row in out:                                     # x1,y1,x2,y2,id,conf,cls,idx
            hist.setdefault(int(row[4]), []).append((t, tuple(row[:4])))
    return hist


def metrics(hist, frames):
    vis = {i: h for i, h in hist.items() if len(h) >= 4}
    maxc = max((len(b) for _, b, _, _ in frames), default=0)
    span = {i: (h[0][0], h[-1][0]) for i, h in vis.items()}
    ids = sorted(vis)
    dup = 0
    for n, i in enumerate(ids):
        for j in ids[n + 1:]:
            lo, hi = max(span[i][0], span[j][0]), min(span[i][1], span[j][1])
            if hi - lo < 1.0:
                continue
            ti = {round(t, 1): b for t, b in vis[i]}
            sj = [(t, b) for t, b in vis[j] if lo <= t <= hi]
            if sj and np.mean([max(_iou(b, bi) for tt, bi in vis[i] if abs(tt - t) < 2.0) if any(abs(tt - t) < 2.0 for tt, _ in vis[i]) else 0 for t, b in sj]) > 0.3:
                dup += 1
                break
    return {"pistas": len(hist), "visitas": len(vis), "max_simultaneas": maxc, "visitas_por_persona": round(len(vis) / maxc, 1) if maxc else None,
            "duplicadas": dup, "largo_medio": round(float(np.mean([len(h) for h in vis.values()])), 1) if vis else 0}


if __name__ == "__main__":
    hours = float(os.environ.get("HOURS", "5"))                         # solo las ultimas horas (la actividad reciente de la oficina)
    paths = sys.argv[1:] or sorted(glob.glob("/app/data/eval/tracks-*.jsonl"))
    by = load(paths)
    for cam in list(by):
        by[cam] = [r for r in by[cam] if r[0] >= time.time() - hours * 3600]
    for cam, frames in sorted(by.items()):
        if len(frames) < 50:
            continue
        print(f"\n== {cam}: {len(frames)} cuadros con personas, {(frames[-1][0] - frames[0][0]) / 3600:.1f} h")
        for name, fn in (("actual", run_actual), ("bytetrack buffer30", lambda f, c: run_byte(f, c, 30)), ("bytetrack buffer90 match.9", lambda f, c: run_byte(f, c, 90, 0.9))):
            try:
                print(f"  {name:28s}", metrics(fn(frames, cam), frames))
            except Exception as exc:                       # una variante que falla no debe tumbar el resto
                print(f"  {name:28s} ERROR {type(exc).__name__}: {exc}")
