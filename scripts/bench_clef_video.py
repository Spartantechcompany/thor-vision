"""Prueba de Clef con video sobre clips etiquetados a mano. Uso: clefvid_test.py <puerto> <nombre> [cuadros]"""
import sys, json, base64, urllib.request, time, cv2, numpy as np
port, name = sys.argv[1], sys.argv[2]; NF = int(sys.argv[3]) if len(sys.argv) > 3 else 8
D = "/app/data/experiments/clefvid/"; meta = {m["n"]: m for m in json.load(open(D + "meta.json"))}; L = json.load(open(D + "labels.json"))
truth = {n: 1 for k in "PVA" for n in L[k]}; truth.update({n: 0 for n in L["N"]})
Q = {"relevant": {"type": "noul", "instructions": "Does a real person, vehicle or animal enter, leave or move through the scene in this camera footage? Answer false if the only changes are lighting, sun, shadows, infrared noise, compression, reflections or fixed objects, or if the only moving things are far away cars on a distant street."}}
def frames(path, n):
    cap = cv2.VideoCapture("/app/data/clips/" + path); t = int(cap.get(7)); out = []
    for k in range(n):
        cap.set(1, int(k * (t - 1) / max(1, n - 1))); ok, f = cap.read()
        if ok:
            h, w = f.shape[:2]; s = 448 / max(h, w); f = cv2.resize(f, (int(w * s), int(h * s)))
            out.append(base64.b64encode(cv2.imencode(".jpg", f, [1, 85])[1].tobytes()).decode())
    return out
def ask(body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}/decide", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=300).read())
def auc(sc):
    pos = [s for n, s in sc.items() if truth[n]]; neg = [s for n, s in sc.items() if not truth[n]]
    return sum((p > q) + .5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))
res = {"video": {}, "fotos": {}}; ms = {"video": [], "fotos": []}
for n in truth:
    fr = frames(meta[n]["path"], NF)
    state = {"task": "Security camera footage; decide whether anything worth describing happened.", "camera": meta[n]["cam"]}
    for mode, body in (("video", {"videos": [fr]}), ("fotos", {"images": [fr[0], fr[len(fr) // 2], fr[-1]]})):
        try:
            o = ask({"state": state, "questions": Q, **body}); res[mode][n] = float(o["answers"]["relevant"]["noul"]); ms[mode].append(o.get("ms", 0))
        except Exception as e:
            print("fallo", n, mode, str(e)[:100]); res[mode][n] = None
out = {"modelo": name, "cuadros": NF, "clips": len(truth), "positivos": sum(truth.values())}
for m in ("video", "fotos"):
    sc = {n: v for n, v in res[m].items() if v is not None}
    best = max((( (sum(1 for n, s in sc.items() if (s >= t) == bool(truth[n])) / len(sc)), t) for t in [i / 20 for i in range(1, 20)]))
    thr = 0.5; tp = sum(1 for n, s in sc.items() if s >= thr and truth[n]); fn = sum(1 for n, s in sc.items() if s < thr and truth[n]); fp = sum(1 for n, s in sc.items() if s >= thr and not truth[n]); tn = sum(1 for n, s in sc.items() if s < thr and not truth[n])
    out[m] = {"auc": round(auc(sc), 3), "ms_med": int(np.median(ms[m])) if ms[m] else None, "mejor_exactitud": [round(best[0], 3), best[1]], "umbral_0.5": {"se_pierde_relevante": fn, "relevantes_ok": tp, "ruido_que_pasa": fp, "ruido_descartado": tn}}
print(json.dumps(out, ensure_ascii=False, indent=1)); json.dump({"out": out, "res": res}, open(D + f"result-{name}-{NF}.json", "w"))
