"""Reconocimiento de las mascotas: caracteristicas de una red ImageNet + clasificador entrenado con tus etiquetas.

Con pocas fotos por mascota un ajuste fino completo se sobreajusta; esto entrena rapido, mide con validacion
cruzada y sugiere etiqueta para el resto de los recortes (aprendizaje activo). El ajuste fino real llega con >=60 etiquetas.
"""
import logging
import os
import pickle
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)

ROOT = os.environ.get("PET_DIR", "/app/data/pets")
MODEL_PATH = os.path.join(ROOT, "model.pkl")
_lock = threading.Lock()
_net = None
_dev = None
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


def _backbone():
    global _net, _dev
    if _net is None:
        import torch
        import torchvision
        _dev = "cuda" if torch.cuda.is_available() else "cpu"
        m = torchvision.models.resnet50(weights="IMAGENET1K_V2")
        m.fc = torch.nn.Identity()
        _net = m.eval().to(_dev)
    return _net


def _prep(img_bgr, size=224):
    h, w = img_bgr.shape[:2]
    s = size / max(h, w)
    im = cv2.resize(img_bgr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    canvas = np.full((size, size, 3), 128, np.uint8)
    y0, x0 = (size - im.shape[0]) // 2, (size - im.shape[1]) // 2
    canvas[y0:y0 + im.shape[0], x0:x0 + im.shape[1]] = im
    rgb = canvas[:, :, ::-1].astype(np.float32) / 255.0
    return ((rgb - _MEAN) / _STD).transpose(2, 0, 1)


def embed(images, bs=32) -> np.ndarray:
    import torch
    net = _backbone()
    out = []
    with torch.no_grad():
        for i in range(0, len(images), bs):
            x = torch.from_numpy(np.stack([_prep(im) for im in images[i:i + bs]])).to(_dev)
            e = net(x).float().cpu().numpy()
            out.append(e / (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9))
    return np.concatenate(out) if out else np.zeros((0, 2048), np.float32)


def _load(path):
    im = cv2.imread(path)
    return im if im is not None else np.full((64, 64, 3), 128, np.uint8)


def train_and_suggest(db) -> dict:
    """Entrena con las etiquetas actuales y guarda pred_label/pred_conf en los recortes sin etiqueta."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    with _lock:
        with db._lock:
            lab = db._conn.execute("SELECT id, file, label FROM pet_crops WHERE label IS NOT NULL").fetchall()
            unl = db._conn.execute("SELECT id, file FROM pet_crops WHERE label IS NULL").fetchall()
        classes = sorted({r[2] for r in lab})
        counts = {c: sum(1 for r in lab if r[2] == c) for c in classes}
        usable = [c for c in classes if counts[c] >= 2]
        if len(usable) < 2:
            return {"ok": False, "reason": "se necesitan al menos 2 clases con 2 o mas etiquetas", "counts": counts}
        t0 = time.time()
        rows = [r for r in lab if r[2] in usable]
        X = embed([_load(os.path.join(ROOT, r[1])) for r in rows])
        y = np.array([r[2] for r in rows])
        clf = LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced")
        report = {"counts": counts, "usable": usable}
        k = min(5, min(counts[c] for c in usable))
        if k >= 2:
            pred = cross_val_predict(LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced"), X, y,
                                     cv=StratifiedKFold(k, shuffle=True, random_state=0))
            report["cv_accuracy"] = round(float((pred == y).mean()), 3)
            report["cv_per_class"] = {c: round(float((pred[y == c] == c).mean()), 3) for c in usable}
        clf.fit(X, y)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump({"clf": clf, "classes": list(clf.classes_), "n": len(rows), "ts": time.time(), "report": report}, f)
        n_sug = 0
        if unl:
            XU = embed([_load(os.path.join(ROOT, r[1])) for r in unl])
            P = clf.predict_proba(XU)
            with db._lock:
                for (rid, _), p in zip(unl, P):
                    j = int(p.argmax())
                    db._conn.execute("UPDATE pet_crops SET pred_label=?, pred_conf=? WHERE id=?", (str(clf.classes_[j]), float(p[j]), rid))
                    n_sug += 1
                db._conn.commit()
        report.update({"ok": True, "trained_on": len(rows), "suggested": n_sug, "seconds": round(time.time() - t0, 1)})
        logger.info("pet_model: entrenado con %d etiquetas %s, %d sugerencias en %.1fs", len(rows), counts, n_sug, report["seconds"])
        return report


def load_model():
    try:
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def classify(img_bgr, model=None):
    """(etiqueta, probabilidad) para un recorte en vivo, o (None, 0) si aun no hay modelo."""
    m = model or load_model()
    if not m:
        return None, 0.0
    p = m["clf"].predict_proba(embed([img_bgr]))[0]
    j = int(p.argmax())
    return str(m["classes"][j]), float(p[j])
