"""Tracker por camara, una visita por persona y sujetos (identidad) por embedding facial.

Flujo: cada inferencia de YOLO/InsightFace llega a VisitManager.update(). Las cajas de
persona se asocian a tracks (IoU/centroide, tolerante a los 0.5 fps); un track confirmado
crea una fila en person_visits que se actualiza con la mejor cara/cuerpo; al cerrarse el
track se resuelve el sujeto comparando el embedding promedio de la visita (coseno) con los
sujetos conocidos, o se crea uno nuevo.
"""
import base64
import logging
import math
import os
import queue
import threading
import time
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


MIN_HITS = int(_f("TRACK_MIN_HITS", 2))
VLM_MIN_S = float(os.environ.get("VISIT_VLM_MIN_S", "10"))
VLM_COOLDOWN_S = float(os.environ.get("VISIT_VLM_COOLDOWN_S", "30"))
MAX_GAP_S = _f("TRACK_MAX_GAP_S", 20.0)
STATIC_S = _f("TRACK_STATIC_S", 600.0)
STATIC_IOU = 0.85
FACE_MIN_AREA = _f("FACE_MIN_AREA", 750.0)
FACE_MIN_DET = _f("FACE_MIN_DET", 0.7)
FACE_BLUR_MIN = _f("FACE_BLUR_MIN", 20.0)
MATCH_THR = _f("SUBJECT_MATCH_THRESHOLD", 0.45)
KNOWN_MIN_CONF = _f("SUBJECT_KNOWN_MIN_CONF", 0.45)
FLUSH_S = 10.0
EMB_CAP_OLD, EMB_CAP_NEW = 30, 5
FACE_MAX_YAW = _f("FACE_MAX_YAW", 0.45)
SNAP_ENABLED = os.environ.get("FACE_SNAPSHOT_ENABLED", "true").lower() == "true"
SNAP_MIN_INTERVAL_S = _f("FACE_SNAPSHOT_INTERVAL_S", 1.5)
SNAP_MAX_PER_VISIT = int(_f("FACE_SNAPSHOT_MAX", 6))
SNAP_MIN_BODY_H = _f("FACE_SNAPSHOT_MIN_BODY_H", 120.0)
HUNT_MAX_S = _f("FACE_HUNT_MAX_S", 30.0)


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def sharpness(img, side: int = 64) -> float:
    """Varianza del Laplaciano sobre la imagen normalizada a `side` px (0 si falla)."""
    try:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        k = side / max(g.shape[:2])
        g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        return float(cv2.Laplacian(g, cv2.CV_64F).var())
    except Exception:
        return 0.0


def _jpeg(img, max_side: int, quality: int) -> Optional[bytes]:
    try:
        k = max_side / max(img.shape[:2])
        if k < 1.0:
            img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def face_yaw(kps) -> float:
    """Giro horizontal aprox. de la cabeza: desplazamiento de la nariz respecto al punto medio de
    los ojos, normalizado por la distancia entre ojos (0 = frontal, >0.5 = perfil). 1.0 si no hay datos."""
    try:
        k = np.asarray(kps, dtype=np.float32).reshape(-1, 2)
        le, re_, nose = k[0], k[1], k[2]
        d = float(abs(re_[0] - le[0]))
        if d < 1.0:
            return 1.0
        return float((nose[0] - (le[0] + re_[0]) / 2.0) / d)
    except Exception:
        return 1.0


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class Track:
    def __init__(self, tid: int, bbox: tuple, now: float):
        self.id = tid
        self.bbox = bbox
        self.anchor, self.anchor_ts = bbox, now
        self.first_ts = self.last_ts = now
        self.hits = 0
        self.visit_id: Optional[int] = None
        self.subject_id: Optional[int] = None
        self.face: Optional[bytes] = None
        self.face_q = 0.0
        self.body: Optional[bytes] = None
        self.body_q = 0.0
        self.scene: Optional[bytes] = None
        self.emb_sum: Optional[np.ndarray] = None
        self.n_emb = 0
        self.n_face = 0
        self.names: list = []
        self.dirty = False
        self.last_flush = 0.0
        self.static = False
        self.good_face = False
        self.snap_n = 0
        self.last_snap = 0.0
        self.q_src: dict = {}
        self.live_bbox = bbox
        self.pre_n = 0
        self.face_src = None

    def observe(self, bbox: tuple, now: float) -> None:
        self.bbox = bbox
        self.last_ts = max(self.last_ts, now)
        self.first_ts = min(self.first_ts, now)
        self.hits += 1
        if _iou(bbox, self.anchor) > STATIC_IOU:
            if now - self.anchor_ts >= STATIC_S and not self.static:
                self.static, self.dirty = True, True
        else:
            self.anchor, self.anchor_ts = bbox, now
            if self.static:
                self.static, self.dirty = False, True


class VisitManager:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._tracks: dict = {}
        self._next_tid: dict = {}
        self._subjects: dict = {}     # id -> {emb, n, name, named}
        self._analyzer = None
        self._vlm_q: queue.Queue = queue.Queue(maxsize=30)
        self._vlm_last: dict = {}
        self.stats = {"visits_opened": 0, "visits_closed": 0, "subjects_created": 0,
                      "subject_matches": 0, "vlm_described": 0, "vlm_false_positive": 0,
                      "snap_requests": 0, "snap_faces": 0, "snap_wins": 0,
                      "vlm_skipped": 0, "snap_fail": 0, "stream_good": 0, "snap_good": 0,
                      "pre_bursts": 0, "pre_frames": 0, "best_pre": 0, "best_snap": 0, "best_stream": 0}
        self.burst_cb = None    # callable(cam_id, reason): pide analisis retroactivo del pre-roll
        self._models = None
        self._face_db = None
        self._grabbers: dict = {}
        self._snap_q: queue.Queue = queue.Queue(maxsize=8)
        for s in db.load_subjects():
            emb = np.frombuffer(s["embedding"], dtype=np.float32).copy() if s["embedding"] else None
            self._subjects[s["id"]] = {"emb": emb, "n": s["n_emb"] or 0,
                                       "name": s["name"], "named": bool(s["named"])}
        logger.info("VisitManager: %d sujetos cargados (min_hits=%d gap=%.0fs match>=%.2f)",
                    len(self._subjects), MIN_HITS, MAX_GAP_S, MATCH_THR)

    def set_analyzer(self, analyzer) -> None:
        if self._analyzer is None:
            threading.Thread(target=self._vlm_loop, daemon=True, name="visit-vlm").start()
        self._analyzer = analyzer

    def set_models(self, models, face_db, cam_urls: dict) -> None:
        if not SNAP_ENABLED:
            return
        from app.capture.snapshot_grabber import SnapshotGrabber
        self._models, self._face_db = models, face_db
        self._grabbers = {cid: SnapshotGrabber(cid, url) for cid, url in cam_urls.items() if url}
        threading.Thread(target=self._snap_loop, daemon=True, name="visit-snap").start()
        logger.info("Snapshots de rostro: %d camaras (intervalo %.1fs, max %d/visita, yaw<=%.2f)",
                    len(self._grabbers), SNAP_MIN_INTERVAL_S, SNAP_MAX_PER_VISIT, FACE_MAX_YAW)

    def needs_faces(self, cam_id: str) -> bool:
        """True si hay una persona reciente en la camara sin un buen rostro todavia (sube el fps de YOLO)."""
        now = time.time()
        for tr in list(self._tracks.get(cam_id, ())):
            if (not tr.good_face and not tr.static and now - tr.last_ts < 6.0
                    and now - tr.first_ts < HUNT_MAX_S):
                return True
        return False

    # ── entrada principal (hilo de VisionQueue) ─────────────────────────
    def update(self, cam_id: str, result, frame, ts=None, src: str = "stream") -> None:
        now = time.time()
        ots = now if ts is None else ts
        persons = [tuple(p) for p in (getattr(result, "person_bboxes", None) or [])]
        new_track = False
        with self._lock:
            tracks = self._tracks.setdefault(cam_id, [])
            tid0 = self._next_tid.get(cam_id, 0)
            det_tracks = self._match(cam_id, tracks, persons, ots)
            new_track = self._next_tid.get(cam_id, 0) != tid0
            for tr, pb in zip(det_tracks, persons):
                tr.observe(pb, ots)
                if src == "pre":
                    tr.pre_n += 1
                else:
                    tr.live_bbox = pb
                if frame is not None:
                    self._body_candidate(tr, frame, pb)
                    if src != "pre":
                        self._maybe_snapshot(cam_id, tr, pb, frame, now)
            for face in (result.faces or []):
                tr = self._track_for_face(face.bbox, persons, det_tracks)
                if tr is not None:
                    self._face_obs(tr, face, src)
            self._housekeeping(cam_id, tracks, now)
        if new_track and src == "stream" and self.burst_cb is not None:
            try:
                self.burst_cb(cam_id, "person")
            except Exception as exc:
                logger.debug("burst_cb: %s", exc)

    def burst_end(self, cam_id: str) -> None:
        """Tras reprocesar el pre-roll (del mas nuevo al mas viejo) devuelve cada track a su posicion viva."""
        with self._lock:
            for tr in self._tracks.get(cam_id, ()):
                tr.bbox = tr.live_bbox

    def _match(self, cam_id, tracks, persons, now):
        pairs = []
        for ti, t in enumerate(tracks):
            scale = max(t.bbox[2] - t.bbox[0], t.bbox[3] - t.bbox[1], 1)
            tcx, tcy = (t.bbox[0] + t.bbox[2]) / 2, (t.bbox[1] + t.bbox[3]) / 2
            for di, d in enumerate(persons):
                cd = math.hypot((d[0] + d[2]) / 2 - tcx, (d[1] + d[3]) / 2 - tcy)
                iou = _iou(t.bbox, d)
                if iou >= 0.1 or cd <= scale:
                    pairs.append((iou + max(0.0, 1 - cd / scale) * 0.5, ti, di))
        pairs.sort(reverse=True)
        used, out = set(), [None] * len(persons)
        for _, ti, di in pairs:
            if ti in used or out[di] is not None:
                continue
            out[di] = tracks[ti]
            used.add(ti)
        for di, d in enumerate(persons):
            if out[di] is None:
                tid = self._next_tid[cam_id] = self._next_tid.get(cam_id, 0) + 1
                out[di] = Track(tid, d, now)
                tracks.append(out[di])
        return out

    @staticmethod
    def _track_for_face(fb, persons, det_tracks):
        cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
        for margin in (0.10, 0.25):
            best = None
            for i, pb in enumerate(persons):
                mx, my = (pb[2] - pb[0]) * margin, (pb[3] - pb[1]) * margin
                if pb[0] - mx <= cx <= pb[2] + mx and pb[1] - my <= cy <= pb[3] + my:
                    area = (pb[2] - pb[0]) * (pb[3] - pb[1])
                    if best is None or area < best[0]:
                        best = (area, i)
            if best is not None:
                return det_tracks[best[1]]
        return None

    # ── mejores capturas ───────────────────────────────────────────────
    @staticmethod
    def _body_candidate(tr: Track, frame, pb) -> None:
        if tr.static and tr.body:
            return
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = pb
        bw, bh = x2 - x1, y2 - y1
        if bw < 24 or bh < 48:
            return
        crop = frame[max(0, int(y1 - bh * .06)):min(h, int(y2 + bh * .06)),
                     max(0, int(x1 - bw * .12)):min(w, int(x2 + bw * .12))]
        if crop.size == 0:
            return
        edge = 0.7 if (x1 <= 2 or y1 <= 2 or x2 >= w - 2 or y2 >= h - 2) else 1.0
        q = math.sqrt(bw * bh) * edge * max(0.3, min(sharpness(crop, 128), 150.0) / 150.0)
        if q <= tr.body_q * 1.15:
            return
        jpg = _jpeg(crop, 288, 75)
        if jpg:
            tr.body, tr.body_q, tr.dirty = jpg, q, True
            tr.scene = _jpeg(frame, 960, 70)

    def _maybe_snapshot(self, cam_id, tr: Track, pb, frame, now: float) -> None:
        g = self._grabbers.get(cam_id)
        if (g is None or not g.usable() or tr.good_face or tr.static
                or tr.snap_n >= SNAP_MAX_PER_VISIT or now - tr.last_snap < SNAP_MIN_INTERVAL_S
                or (pb[3] - pb[1]) < SNAP_MIN_BODY_H):
            return
        try:
            self._snap_q.put_nowait((cam_id, tr, pb, frame.shape[1], frame.shape[0]))
        except queue.Full:
            return
        tr.last_snap, tr.snap_n = now, tr.snap_n + 1
        self.stats["snap_requests"] += 1

    def _snap_loop(self) -> None:
        from types import SimpleNamespace
        while True:
            cam_id, tr, pb, fw, fh = self._snap_q.get()
            try:
                g = self._grabbers.get(cam_id)
                img = g.grab() if g else None
                if img is None:
                    self.stats["snap_fail"] += 1
                    continue
                H, W = img.shape[:2]
                sx, sy = W / float(fw), H / float(fh)
                bw, bh = (pb[2] - pb[0]) * sx, (pb[3] - pb[1]) * sy
                x1 = max(0, int(pb[0] * sx - bw * .15)); x2 = min(W, int(pb[2] * sx + bw * .15))
                y1 = max(0, int(pb[1] * sy - bh * .10)); y2 = min(H, int(pb[3] * sy + bh * .10))
                crop = img[y1:y2, x1:x2]
                if crop.size == 0 or self._models is None or self._models._face_app is None:
                    continue
                best = None
                for face in self._models._face_app.get(crop):
                    score = float(face.det_score) if face.det_score is not None else 1.0
                    if score < 0.6:
                        continue
                    bb = [int(v) for v in face.bbox.tolist()]
                    fx1, fy1 = max(0, bb[0]), max(0, bb[1])
                    fcrop = crop[fy1:max(fy1 + 1, bb[3]), fx1:max(fx1 + 1, bb[2])]
                    area = float(max(0, bb[2] - bb[0]) * max(0, bb[3] - bb[1]))
                    if best is None or area > best[0]:
                        best = (area, face, bb, fcrop, score)
                if best is None:
                    continue
                area, face, bb, fcrop, score = best
                mx, my = (bb[2] - bb[0]) * .35, (bb[3] - bb[1]) * .35
                th = crop[max(0, int(bb[1] - my)):int(bb[3] + my), max(0, int(bb[0] - mx)):int(bb[2] + mx)]
                jpg = _jpeg(th, 128, 85) if th.size else None
                name, conf = (self._face_db.recognize(face.embedding) if self._face_db else (None, 0.0))
                f = SimpleNamespace(bbox=tuple(bb), name=name, confidence=conf, embedding=face.embedding,
                                    det_score=score, sharpness=sharpness(fcrop), area=area,
                                    yaw=face_yaw(getattr(face, 'kps', None)),
                                    thumb_b64=base64.b64encode(jpg).decode() if jpg else None)
                with self._lock:
                    self._face_obs(tr, f, "snap")
                self.stats["snap_faces"] += 1
            except Exception as exc:
                logger.warning("snapshot cam=%s: %s", cam_id, exc)

    @staticmethod
    def _face_obs(tr: Track, f, src: str = "stream") -> None:
        tr.n_face += 1
        tr.dirty = True
        if f.name and f.name != "Desconocido":
            tr.names.append((f.name, float(f.confidence or 0.0)))
        bb = f.bbox
        area = float(f.area or (bb[2] - bb[0]) * (bb[3] - bb[1]))
        yaw = abs(float(getattr(f, "yaw", 0.0)))
        q = (float(f.det_score) * math.sqrt(area) * max(0.2, min(f.sharpness / 100.0, 1.5))
             * max(0.15, 1.0 - yaw / 0.6))
        tr.q_src[src] = max(tr.q_src.get(src, 0.0), q)
        if f.thumb_b64 and q > tr.face_q * 1.1:
            tr.face, tr.face_q, tr.face_src = base64.b64decode(f.thumb_b64), q, src
        if (f.embedding is not None and area >= FACE_MIN_AREA
                and f.det_score >= FACE_MIN_DET and f.sharpness >= FACE_BLUR_MIN
                and yaw <= FACE_MAX_YAW):
            tr.good_face = True
            e = _unit(np.asarray(f.embedding, dtype=np.float32)) * q
            tr.emb_sum = e if tr.emb_sum is None else tr.emb_sum + e
            tr.n_emb += 1

    # ── ciclo de vida de la visita ─────────────────────────────────────
    def _housekeeping(self, cam_id, tracks, now) -> None:
        keep = []
        for tr in tracks:
            closed = now - tr.last_ts > MAX_GAP_S
            try:
                if tr.visit_id is None and (tr.hits >= MIN_HITS or tr.n_face >= 1):
                    self._open_visit(cam_id, tr)
                if tr.visit_id is not None:
                    if closed:
                        self._close_visit(cam_id, tr)
                    elif tr.dirty and now - tr.last_flush >= FLUSH_S:
                        self._flush(tr, now, "open")
            except Exception as exc:
                logger.warning("visita cam=%s track=%s: %s", cam_id, tr.id, exc)
            if not closed:
                keep.append(tr)
        tracks[:] = keep

    def _open_visit(self, cam_id, tr: Track) -> None:
        tr.visit_id = self.db.insert_person_visit(cam_id, tr.id, tr.first_ts)
        tr.dirty = True
        self.stats["visits_opened"] += 1
        self._flush(tr, time.time(), "open")

    def _close_visit(self, cam_id, tr: Track) -> None:
        if tr.face_src:
            self.stats["best_" + tr.face_src] = self.stats.get("best_" + tr.face_src, 0) + 1
        if tr.snap_n or tr.pre_n:
            qs, qn, qp = (tr.q_src.get(k, 0.0) for k in ("stream", "snap", "pre"))
            self.stats["snap_wins"] += int(qn > qs)
            logger.info("VISIT_AB cam=%s visit=%s stream_q=%.1f snap_q=%.1f pre_q=%.1f snaps=%d pre_n=%d best=%s",
                        cam_id, tr.visit_id, qs, qn, qp, tr.snap_n, tr.pre_n, tr.face_src)
        self._flush(tr, time.time(), "closed")
        self.stats["visits_closed"] += 1
        if tr.body and not tr.static and self._analyzer is not None and self._vlm_worth(cam_id, tr):
            try:
                self._vlm_q.put_nowait((cam_id, tr.visit_id, tr.body, tr.face, tr.scene))
            except queue.Full:
                pass

    def _vlm_worth(self, cam_id, tr: Track) -> bool:
        if tr.n_face >= 1:
            return True
        now = time.time()
        if (tr.last_ts - tr.first_ts) < VLM_MIN_S or now - self._vlm_last.get(cam_id, 0.0) < VLM_COOLDOWN_S:
            self.stats["vlm_skipped"] = self.stats.get("vlm_skipped", 0) + 1
            return False
        self._vlm_last[cam_id] = now
        return True

    @staticmethod
    def _known_name(tr: Track):
        if not tr.names:
            return None, None
        counts: dict = {}
        for n, c in tr.names:
            counts.setdefault(n, []).append(c)
        name = max(counts, key=lambda k: len(counts[k]))
        conf = float(np.median(counts[name]))
        return (name, conf) if conf >= KNOWN_MIN_CONF else (None, None)

    def _flush(self, tr: Track, now: float, status: str) -> None:
        self._assign_subject(tr, final=(status == "closed"))
        emb = None
        if tr.emb_sum is not None:
            emb = _unit(tr.emb_sum).astype(np.float32).tobytes()
        known, kconf = self._known_name(tr)
        self.db.update_person_visit(
            tr.visit_id, end_ts=tr.last_ts, hits=tr.hits, status=status,
            static=int(tr.static), face=tr.face, face_score=tr.face_q,
            body=tr.body, body_score=tr.body_q, embedding=emb, n_emb=tr.n_emb,
            known_name=known, known_conf=kconf, subject_id=tr.subject_id)
        if tr.subject_id is not None:
            self.db.touch_subject(tr.subject_id, tr.last_ts)
        tr.dirty, tr.last_flush = False, now

    # ── identidad ──────────────────────────────────────────────────────
    def _assign_subject(self, tr: Track, final: bool) -> None:
        if tr.subject_id is not None or tr.n_emb == 0 or (tr.n_emb < 2 and not final):
            return
        emb = _unit(tr.emb_sum)
        known, _ = self._known_name(tr)
        sid, sim = None, -1.0
        if known:
            for k, s in self._subjects.items():
                if s["named"] and s["name"] == known:
                    sid, sim = k, 1.0
                    break
        if sid is None:
            for k, s in self._subjects.items():
                if s["emb"] is None:
                    continue
                v = float(np.dot(emb, s["emb"]))
                if v > sim:
                    sid, sim = k, v
            if sim < MATCH_THR:
                sid = None
        if sid is not None:
            s = self._subjects[sid]
            if s["emb"] is None or sim >= MATCH_THR:
                mixed = (s["emb"] if s["emb"] is not None else 0) * min(s["n"], EMB_CAP_OLD) \
                    + emb * min(tr.n_emb, EMB_CAP_NEW)
                s["emb"] = _unit(np.asarray(mixed, dtype=np.float32))
                s["n"] += tr.n_emb
                self.db.update_subject_emb(sid, s["emb"].astype(np.float32).tobytes(), s["n"])
            self.stats["subject_matches"] += 1
        else:
            sid = self.db.create_subject(emb.astype(np.float32).tobytes(), tr.n_emb, tr.first_ts,
                                         name=known, named=1 if known else 0)
            self._subjects[sid] = {"emb": emb, "n": tr.n_emb, "name": known, "named": bool(known)}
            self.stats["subjects_created"] += 1
        tr.subject_id = sid

    # ── operaciones de administracion (API) ────────────────────────────
    def rename_subject(self, sid: int, name: str) -> bool:
        with self._lock:
            if not self.db.rename_subject(sid, name):
                return False
            if sid in self._subjects:
                self._subjects[sid].update(name=name, named=True)
            return True

    def merge_subjects(self, src: int, dst: int) -> bool:
        with self._lock:
            if src == dst or src not in self._subjects or dst not in self._subjects:
                return False
            a, b = self._subjects[src], self._subjects[dst]
            if a["emb"] is not None and b["emb"] is not None:
                mixed = a["emb"] * min(a["n"], EMB_CAP_OLD) + b["emb"] * min(b["n"], EMB_CAP_OLD)
                b["emb"] = _unit(np.asarray(mixed, dtype=np.float32))
            b["n"] += a["n"]
            if b["emb"] is not None:
                self.db.update_subject_emb(dst, b["emb"].astype(np.float32).tobytes(), b["n"])
            self.db.merge_subjects(src, dst)
            del self._subjects[src]
            for tracks in self._tracks.values():
                for tr in tracks:
                    if tr.subject_id == src:
                        tr.subject_id = dst
            return True

    def delete_subject(self, sid: int) -> bool:
        with self._lock:
            self._subjects.pop(sid, None)
            for tracks in self._tracks.values():
                for tr in tracks:
                    if tr.subject_id == sid:
                        tr.subject_id = None
            return self.db.delete_subject(sid)

    def snapshot(self) -> dict:
        with self._lock:
            active = {c: sum(1 for t in ts if t.visit_id is not None) for c, ts in self._tracks.items()}
            return {**self.stats, "subjects": len(self._subjects), "active_tracks": active}

    # ── descripcion de la visita con el VLM (una vez al cerrar) ─────────
    def _vlm_loop(self) -> None:
        while True:
            cam_id, vid, body, face, scene = self._vlm_q.get()
            try:
                res = self._analyzer.describe_visit(cam_id, body, face, scene)
                if not isinstance(res, dict) or res.get("activity") == "error":
                    continue
                visible = res.get("visible")
                desc = " ".join(str(res.get("desc") or "").split())[:200]
                act = " ".join(str(res.get("action") or "").split())[:120]
                text = (desc + (f" — {act}" if act else "")).strip()
                fp = 1 if visible is False else 0
                self.db.update_person_visit(vid, vlm_desc=text or None, fp=fp)
                self.stats["vlm_described"] += 1
                self.stats["vlm_false_positive"] += fp
            except Exception as exc:
                logger.warning("describe_visit visita=%s: %s", vid, exc)
