"""Set de pruebas multimodal de EmbeddingGemma 2 (y Clef) con datos reales de THOR. Corre dentro del contenedor:
    docker exec thor-vision python3 /app/scripts/bench_multimodal.py [--days 7] [--quick]
Escribe data/experiments/multimodal-<fecha>/resultados.json y reporte.md.

Pruebas
  T1 escena por texto   texto -> captura completa (no solo personas); comparado con buscar en la descripcion del modelo de vision
  T2 buscar por foto    captura -> capturas parecidas (excluye la misma escena: misma camara a menos de 10 min)
  T3 video              texto -> clip de la alerta, contra texto -> captura y texto -> descripcion del mismo evento
  T4 repetidas          pares de capturas seguidas de la misma camara: cuantas llamadas al VLM se ahorrarian (embeddings y Clef)
  T5 personas           mismo empleado con nombre vs empleados distintos, por la foto del cuerpo (AUC)
  T6 audio              valida la tuberia con sonidos sinteticos conocidos y unas muestras reales del videoportero
Limites: la "verdad" de T1-T4 sale de las etiquetas que puso el propio modelo de vision (circular); T5 usa rostros confirmados; T6 no mide precision real.
"""
import argparse
import io
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import wave

import cv2
import numpy as np

sys.path.insert(0, "/app")
from PIL import Image  # noqa: E402

from app.vision import embedder  # noqa: E402

DB = "/app/data/events.db"
SNAP = "/app/data/snapshots"
random.seed(7)
# consultas por tipo de alerta (dos redacciones; espanol natural)
Q = {
    "vehiculo_detenido": ["un auto detenido frente a la entrada", "vehiculo estacionado frente al inmueble"],
    "merodeo": ["una persona parada frente a la reja mirando hacia adentro", "alguien merodeando frente a la puerta"],
    "uso_telefono_exterior": ["persona en la calle usando su celular", "alguien parado mirando el telefono en la banqueta"],
    "persona_nocturna": ["una persona de noche dentro del inmueble", "alguien caminando de noche con poca luz"],
    "acceso_no_autorizado": ["persona entrando por una puerta", "alguien intentando entrar al inmueble"],
    "puerta_abierta": ["puerta o porton abierto", "una puerta que quedo abierta"],
    "persona_en_suelo": ["persona tirada en el piso", "alguien caido en el suelo"],
    "animal": ["un perro o gato dentro de la casa", "una mascota caminando"],
    "grupo_inusual": ["grupo de varias personas juntas", "muchas personas reunidas"],
    "manipulacion_vehiculo": ["alguien abriendo la puerta de un coche", "persona revisando un auto estacionado"],
    "objeto_abandonado": ["una bolsa o paquete abandonado", "un objeto dejado en el piso"],
    "cruce_linea": ["una persona cruzando la entrada del inmueble", "alguien entrando por la puerta peatonal"],
    "merodeo_puerta": ["una persona parada frente a la puerta peatonal", "alguien esperando junto a la puerta de la calle"],
    "actividad_armado": ["persona dentro del inmueble con el sistema armado", "alguien adentro cuando no deberia haber nadie"],
}


def jac(a, b):
    w = lambda t: set(re.findall(r"[a-záéíóúñ]{4,}", (t or "").lower()))
    a, b = w(a), w(b)
    return len(a & b) / max(1, len(a | b))


def p_at(ranked, rel, k=10):
    return sum(1 for i in ranked[:k] if rel(i)) / k


def mrr(ranked, rel, k=10):
    for n, i in enumerate(ranked[:k], 1):
        if rel(i):
            return 1 / n
    return 0.0


def auc(pos, neg):
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


class E:
    def __init__(self):
        self.m = embedder._load()
        self.t = {"img": [0, 0.0], "vid": [0, 0.0], "aud": [0, 0.0], "txt": [0, 0.0]}

    def _enc(self, kind, xs, **k):
        t0 = time.time()
        v = self.m.encode(xs, convert_to_numpy=True, normalize_embeddings=True, batch_size=8, **k)
        self.t[kind][0] += len(xs)
        self.t[kind][1] += time.time() - t0
        return np.asarray(v, np.float32)

    def text(self, qs, query=True):
        return self._enc("txt", list(qs), prompt_name="SearchQuery" if query else "Document")

    def images(self, imgs):
        out = []
        for i in range(0, len(imgs), 16):
            out.append(self._enc("img", [{"image": x} for x in imgs[i:i + 16]]))
        return np.concatenate(out) if out else np.zeros((0, 768), np.float32)

    def video(self, frames):
        return self._enc("vid", [{"video": frames}])[0]

    def audio(self, arr):
        return self._enc("aud", [{"audio": arr}])[0]


def snap_path(p):
    return p if os.path.isabs(p) else os.path.join(SNAP, p)


def load_img(p, side=640):
    im = Image.open(p).convert("RGB")
    k = side / max(im.size)
    return im.resize((int(im.width * k), int(im.height * k))) if k < 1 else im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", default="", help="T3 = solo la prueba de video")
    a = ap.parse_args()
    per_type = 40 if a.quick else 100
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    since = time.time() - a.days * 86400
    out = {"fecha": time.strftime("%Y-%m-%d %H:%M"), "dias": a.days}
    e = E()
    print("modelo cargado", flush=True)

    if a.only == "T3":
        return run_t3(a, c, e, out)
    # ── T1 / T2: capturas de alertas ──────────────────────────────────
    rows = c.execute("SELECT s.id, s.path, s.cam_id, s.ts, e.id, e.data FROM snapshots s JOIN events e ON e.id=s.event_id WHERE s.ts>? AND e.has_alert=1", (since,)).fetchall()
    by_t = {}
    items = []
    for sid, path, cam, ts, eid, data in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        types = [t for t in (d.get("alert_types") or []) if t in Q]
        if not types or not os.path.exists(snap_path(path)):
            continue
        items.append({"sid": sid, "path": snap_path(path), "cam": cam, "ts": ts, "eid": eid, "types": set(types), "desc": d.get("activity") or ""})
    random.shuffle(items)
    sel, seen = [], set()
    for it in items:
        k = sorted(it["types"])[0]
        if by_t.get(k, 0) < per_type and it["eid"] not in seen:
            by_t[k] = by_t.get(k, 0) + 1
            sel.append(it)
            seen.add(it["eid"])
    types = [t for t, n in by_t.items() if n >= 20]
    sel = [it for it in sel if it["types"] & set(types)]
    print(f"T1/T2: {len(sel)} capturas, tipos {types}", flush=True)
    imgs = e.images([load_img(it["path"]) for it in sel])
    descs = e.text([it["desc"] for it in sel], query=False)
    t1 = {}
    for t in types:
        for q in Q[t]:
            qv = e.text([q])[0]
            rel = lambda i: t in sel[i]["types"]
            ri, rd = list(np.argsort(-(imgs @ qv))), list(np.argsort(-(descs @ qv)))
            r = t1.setdefault(t, {"img_p10": [], "desc_p10": [], "img_mrr": [], "desc_mrr": [], "azar": round(sum(1 for x in sel if t in x["types"]) / len(sel), 3)})
            r["img_p10"].append(p_at(ri, rel)); r["desc_p10"].append(p_at(rd, rel)); r["img_mrr"].append(mrr(ri, rel)); r["desc_mrr"].append(mrr(rd, rel))
    avg = lambda xs: round(float(np.mean(xs)), 3) if xs else None
    out["T1_escena_por_texto"] = {"n_capturas": len(sel), "por_tipo": {t: {k: (avg(v) if isinstance(v, list) else v) for k, v in r.items()} for t, r in t1.items()},
                                  "promedio": {k: avg([avg(r[k]) for r in t1.values()]) for k in ("img_p10", "desc_p10", "img_mrr", "desc_mrr")}}
    print("T1", out["T1_escena_por_texto"]["promedio"], flush=True)

    sims = imgs @ imgs.T
    p10, same_cam, chance = [], [], []
    for qi in random.sample(range(len(sel)), min(80, len(sel))):
        q = sel[qi]
        cand = [j for j in np.argsort(-sims[qi]) if j != qi and not (sel[j]["cam"] == q["cam"] and abs(sel[j]["ts"] - q["ts"]) < 600)]
        top = cand[:10]
        p10.append(sum(1 for j in top if sel[j]["types"] & q["types"]) / 10)
        same_cam.append(sum(1 for j in top if sel[j]["cam"] == q["cam"]) / 10)
        chance.append(sum(1 for j in cand if sel[j]["types"] & q["types"]) / max(1, len(cand)))
    out["T2_buscar_por_foto"] = {"consultas": len(p10), "p10_mismo_tipo": avg(p10), "azar": avg(chance), "fraccion_misma_camara_top10": avg(same_cam)}
    print("T2", out["T2_buscar_por_foto"], flush=True)

    run_t3(a, c, e, out, final=False)
    rest(a, c, e, out, since)


def run_t3(a, c, e, out, final=True):
    avg = lambda xs: round(float(np.mean(xs)), 3) if xs else None
    # ── T3: video de alertas ─────────────────────────────────────────
    clips = c.execute("SELECT cl.path, cl.event_id, e.data, (SELECT s.path FROM snapshots s WHERE s.event_id=e.id LIMIT 1) FROM clips cl JOIN events e ON e.id=cl.event_id "
                      "WHERE cl.ts>?", (time.time() - max(a.days, 14) * 86400,)).fetchall()
    vs, vimgs, vdesc, vmeta = [], [], [], []
    for path, eid, data, sp in clips[: (60 if a.quick else 220)]:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        tt = set(t for t in (d.get("alert_types") or []) if t in Q)
        path = path if os.path.isabs(path) else os.path.join("/app/data/clips", path)
        if not tt or not os.path.exists(path) or not sp or not os.path.exists(snap_path(sp)):
            continue
        cap = cv2.VideoCapture(path)
        fr = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            fr.append(f)
        if len(fr) < 4:
            continue
        pick = [cv2.cvtColor(cv2.resize(fr[round(i * (len(fr) - 1) / 15)], (448, 336)), cv2.COLOR_BGR2RGB) for i in range(16)]
        vs.append(e.video(pick))
        vimgs.append(load_img(snap_path(sp)))
        vdesc.append(d.get("activity") or "")
        vmeta.append(tt)
    if len(vs) >= 15:
        V, I, D = np.stack(vs), e.images(vimgs), e.text(vdesc, query=False)
        res = {"video": [], "captura": [], "descripcion": []}
        ttypes = sorted({t for s in vmeta for t in s if sum(1 for x in vmeta if t in x) >= 5})
        for t in ttypes:
            for q in Q[t]:
                qv = e.text([q])[0]
                rel = lambda i: t in vmeta[i]
                for k, M in (("video", V), ("captura", I), ("descripcion", D)):
                    res[k].append(p_at(list(np.argsort(-(M @ qv))), rel, 5))
        out["T3_video"] = {"clips": len(vs), "tipos": ttypes, "p5_promedio": {k: avg(v) for k, v in res.items()}}
    else:
        out["T3_video"] = {"clips": len(vs), "nota": "muy pocos clips con alerta etiquetada"}
    print("T3", out["T3_video"], flush=True)
    if final:
        d = f"/app/data/experiments/multimodal-{time.strftime('%Y%m%d-%H%M')}-T3"
        os.makedirs(d, exist_ok=True)
        json.dump(out, open(f"{d}/resultados.json", "w"), ensure_ascii=False, indent=1)
        print("LISTO", d, flush=True)


def rest(a, c, e, out, since):
    avg = lambda xs: round(float(np.mean(xs)), 3) if xs else None

    # ── T4: capturas repetidas (ahorro de llamadas al VLM) ───────────
    seq = c.execute("SELECT s.path, s.cam_id, s.ts, e.data FROM snapshots s JOIN events e ON e.id=s.event_id WHERE s.ts>? AND e.type='nemotron' ORDER BY s.cam_id, s.ts", (since,)).fetchall()
    pairs = []
    for (p1, c1, t1_, d1), (p2, c2, t2_, d2) in zip(seq, seq[1:]):
        if c1 != c2 or not (0 < t2_ - t1_ <= 180):
            continue
        try:
            a1, a2 = json.loads(d1), json.loads(d2)
        except ValueError:
            continue
        same = (set(a1.get("alert_types") or []) == set(a2.get("alert_types") or []) and a1.get("people") == a2.get("people") and jac(a1.get("activity"), a2.get("activity")) >= 0.4)
        changed = a1.get("people") != a2.get("people") or set(a1.get("alert_types") or []) != set(a2.get("alert_types") or [])
        if same or changed:
            pairs.append((snap_path(p1), snap_path(p2), "repetida" if same else "cambio", c1))
    random.shuffle(pairs)
    rep = [p for p in pairs if p[2] == "repetida"][: (80 if a.quick else 200)]
    chg = [p for p in pairs if p[2] == "cambio"][: (80 if a.quick else 200)]
    pp = [p for p in rep + chg if os.path.exists(p[0]) and os.path.exists(p[1])]
    A = e.images([load_img(p[0]) for p in pp])
    B = e.images([load_img(p[1]) for p in pp])
    cs = (A * B).sum(1)
    pos = [float(x) for x, p in zip(cs, pp) if p[2] == "repetida"]
    neg = [float(x) for x, p in zip(cs, pp) if p[2] == "cambio"]
    th = {}
    for t in (0.90, 0.93, 0.95, 0.97, 0.98):
        th[str(t)] = {"se_saltan_repetidas_pct": round(100 * sum(x >= t for x in pos) / max(1, len(pos)), 1), "se_saltan_por_error_pct": round(100 * sum(x >= t for x in neg) / max(1, len(neg)), 1)}
    t4 = {"pares_repetida": len(pos), "pares_cambio": len(neg), "auc_embeddings": round(auc(pos, neg), 3) if pos and neg else None, "umbrales": th,
          "pares_disponibles": {"repetida": sum(1 for p in pairs if p[2] == "repetida"), "cambio": sum(1 for p in pairs if p[2] == "cambio")}}
    try:
        from app.vision.clef_client import ClefClient
        cl = ClefClient()
        Qc = {"same": {"type": "noul", "instructions": "These are two frames from the same security camera a few seconds or minutes apart. Do they show the same situation with nothing new: "
                       "the same people in the same places doing the same thing, the same vehicles, no one arriving or leaving? Ignore small changes in light, compression or pose."}}
        sub = random.sample([p for p in pp if p[2] == "repetida"], min(40, len(pos))) + random.sample([p for p in pp if p[2] == "cambio"], min(40, len(neg)))
        cp, cn = [], []
        t0 = time.time()
        for p in sub:
            b1, b2 = (cv2.imencode(".jpg", cv2.resize(cv2.imread(x), (640, 360)))[1].tobytes() for x in p[:2])
            r = cl.decide({"task": "Two frames from the same camera; decide if anything new happened."}, [b1, b2], Qc)
            if r:
                (cp if p[2] == "repetida" else cn).append(float(r["answers"]["same"]["noul"]))
        t4["clef"] = {"pares": len(cp) + len(cn), "auc": round(auc(cp, cn), 3) if cp and cn else None, "seg_por_par": round((time.time() - t0) / max(1, len(sub)), 2),
                      "p>=0.7": {"se_saltan_repetidas_pct": round(100 * sum(x >= .7 for x in cp) / max(1, len(cp)), 1), "se_saltan_por_error_pct": round(100 * sum(x >= .7 for x in cn) / max(1, len(cn)), 1)}}
    except Exception as exc:  # noqa: BLE001 - Clef es opcional en la prueba
        t4["clef"] = {"error": str(exc)[:120]}
    out["T4_repetidas"] = t4
    print("T4", t4, flush=True)

    # ── T5: seguimiento de personas ──────────────────────────────────
    vrows = c.execute("SELECT v.subject_id, v.start_ts, v.body FROM person_visits v JOIN subjects s ON s.id=v.subject_id WHERE s.named=1 AND v.body IS NOT NULL AND v.fp=0").fetchall()
    by = {}
    for sid, ts, body in vrows:
        by.setdefault(sid, []).append((ts, body))
    subs = [k for k, v in by.items() if len(v) >= 3]
    vis = []
    for k in subs:
        for ts, body in random.sample(by[k], min(25, len(by[k]))):
            vis.append((k, ts, Image.open(io.BytesIO(body)).convert("RGB")))
    if len(subs) >= 3:
        X = e.images([v[2] for v in vis])
        same, diff = [], []
        for _ in range(4000):
            i, j = random.sample(range(len(vis)), 2)
            s = float(X[i] @ X[j])
            if vis[i][0] == vis[j][0]:
                if abs(vis[i][1] - vis[j][1]) > 600:
                    same.append(s)
            else:
                diff.append(s)
        out["T5_personas"] = {"empleados": len(subs), "recortes": len(vis), "pares_misma": len(same), "pares_distinta": len(diff), "auc_embeddinggemma": round(auc(same, diff), 3),
                              "referencia": {"reid_actual_youtureid": 0.67, "clef_cuerpo": 0.84, "insightface_cara": 0.93}}
    print("T5", out.get("T5_personas"), flush=True)

    # ── T6: audio ────────────────────────────────────────────────────
    sr = 16000
    tt = np.arange(int(sr * 4)) / sr
    env = np.exp(-3 * (tt % 1.0))
    sounds = {
        "silencio": np.zeros_like(tt),
        "ruido de estatica": 0.2 * np.random.randn(len(tt)),
        "timbre de campanilla": 0.4 * env * (np.sin(2 * math.pi * 880 * tt) * (tt % 1 < .5) + np.sin(2 * math.pi * 660 * tt) * (tt % 1 >= .5)),
        "claxon de auto": 0.35 * np.sign(np.sin(2 * math.pi * 420 * tt)) * ((tt % 1.2) < 0.8),
        "pitido agudo de alarma": 0.35 * np.sin(2 * math.pi * 2800 * tt) * ((tt * 4).astype(int) % 2),
    }
    labels = list(sounds)
    L = e.text(labels)
    hits = []
    for name, wav in sounds.items():
        v = e.audio(wav.astype(np.float32))
        sc = L @ v
        hits.append({"sonido": name, "etiqueta_top": labels[int(np.argmax(sc))], "acierta": labels[int(np.argmax(sc))] == name})
    real = []
    labels_r = ["silencio", "voces de personas hablando", "trafico de autos en la calle", "timbre de campanilla", "perro ladrando", "pasos y ruido de puerta"]
    LR = e.text(labels_r)
    for k in range(0 if a.quick else 3):
        f = f"/tmp/vto_bench_{k}.wav"
        if os.path.exists(f):
            w = wave.open(f)
            arr = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
            sc = LR @ e.audio(arr)
            real.append({"muestra": k, "rms": round(float(np.sqrt((arr ** 2).mean())), 4), "top3": [labels_r[i] for i in np.argsort(-sc)[:3]]})
    out["T6_audio"] = {"sinteticos": hits, "aciertos_sinteticos": f"{sum(h['acierta'] for h in hits)}/{len(hits)}", "videoportero_real": real}
    print("T6", out["T6_audio"], flush=True)

    out["tiempos"] = {k: {"n": v[0], "seg_por_item": round(v[1] / max(1, v[0]), 3)} for k, v in e.t.items()}
    d = f"/app/data/experiments/multimodal-{time.strftime('%Y%m%d-%H%M')}"
    os.makedirs(d, exist_ok=True)
    json.dump(out, open(f"{d}/resultados.json", "w"), ensure_ascii=False, indent=1)
    print("LISTO", d, flush=True)


if __name__ == "__main__":
    main()
