"""Prueba A/B del prompt por camara: las MISMAS imagenes y clips con el prompt actual (dos veces, para medir el ruido del propio modelo) y con el compacto.
Corre dentro del contenedor:  docker exec thor-vision python3 /app/scripts/prompt_ab.py [--cams cam-215,cam-189] [--shots 2]
Por camara usa N cuadros limpios en vivo (go2rtc) y el ultimo clip que guardo el analizador (data/debug/nemotron/latest/<cam>.mp4). No escribe eventos en la base.
Metricas: mismo conteo de personas, mismo conjunto de alert_types, misma severidad y parecido de palabras de activity. El ruido (A1 contra A2) es el techo:
si A contra B coincide casi tanto como A1 contra A2, el compacto no cambia lo que dice el modelo."""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/app")
import cv2
import numpy as np

from app.vision import scenes, vlm_analyzer as va

LATEST = "/app/data/debug/nemotron/latest"


def words(t):
    return set(re.findall(r"[a-záéíóúñ]{4,}", (t or "").lower()))


def jac(a, b):
    a, b = words(a), words(b)
    return len(a & b) / max(1, len(a | b))


def snap(cam):
    """Cuadro limpio desde go2rtc. NO usar /api/snapshot: dibuja encima los chips de deteccion y el texto del ultimo analisis, y el modelo los lee."""
    return cv2.imdecode(np.frombuffer(urllib.request.urlopen(f"http://127.0.0.1:1984/api/frame.jpeg?src={cam}", timeout=20).read(), np.uint8), 1)


def clip_frames(cam):
    p = f"{LATEST}/{cam}.mp4"
    if not os.path.isfile(p):
        return None, 0.0
    c = cv2.VideoCapture(p)
    fr = []
    while True:
        ok, f = c.read()
        if not ok:
            break
        fr.append(f)
    return fr, c.get(cv2.CAP_PROP_FPS) or 1.0


def run(an, sc, kind, media, compact):
    scenes.COMPACT = compact
    with va.priority(True):
        if kind == "img":
            return an.analyze(media[0], None, tag="ab", scene=sc)
        return an.analyze_video(media[0], media[1], None, tag="ab", scene=sc)


def key(r):
    return {"people": r.get("people"), "types": tuple(sorted(r.get("alert_types") or [])), "sev": r.get("severity"), "act": r.get("activity", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="")
    ap.add_argument("--shots", type=int, default=2)
    a = ap.parse_args()
    sc = scenes.Scenes()
    cams = [c for c in a.cams.split(",") if c] or sorted(sc._by_cam)
    cams = [c for c in cams if sc.get(c) and c != "cam-vto"]
    an = va.VLMAnalyzer(os.environ["NEMOTRON_ENDPOINT"], os.environ.get("NEMOTRON_MODEL", "thor-vision"), max_tokens=420, timeout=45, video_timeout=90,
                        api_key=os.environ.get("NEMOTRON_API_KEY"), max_width=1280, max_height=720)
    jobs = []
    for cam in cams:
        for i in range(a.shots):
            try:
                jobs.append((cam, f"img{i}", "img", (snap(cam),)))
            except OSError as e:
                print("sin captura", cam, e)
            if i + 1 < a.shots:
                time.sleep(3)
        fr, fps = clip_frames(cam)
        if fr and len(fr) >= 2:
            jobs.append((cam, "clip", "vid", (fr, fps)))
    # tokens del texto de cada prompt (solo conteo, 1 token de salida)
    ep, hd = os.environ["NEMOTRON_ENDPOINT"].rstrip("/") + "/v1/chat/completions", {"Authorization": "Bearer " + os.environ["NEMOTRON_API_KEY"], "Content-Type": "application/json"}

    def ptok(cp, cam):
        scenes.COMPACT = cp
        st, dy = sc.get(cam).render_parts(video=True, compact=cp)
        body = {"model": "thor-vision", "max_tokens": 1, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": scenes.SYSTEM_COMPACT if cp else va.SYSTEM_PROMPT}, {"role": "user", "content": st + "\n" + dy}]}
        for _ in range(5):
            try:
                return json.load(urllib.request.urlopen(urllib.request.Request(ep, data=json.dumps(body).encode(), headers=hd), timeout=60))["usage"]["prompt_tokens"]
            except OSError:
                time.sleep(4)
        return None
    toks = {cam: (ptok(False, cam), ptok(True, cam)) for cam in cams}

    def one(job):
        cam, name, kind, media = job
        s = sc.get(cam)
        out = {}
        for tag, cp in (("A1", False), ("A2", False), ("B", True)):
            for _ in range(3):
                r = run(an, s, kind, media, cp)
                if r.get("activity") != "error":
                    break
                time.sleep(6)
            out[tag] = key(r)
        if kind == "img" and (out["A1"]["people"] != out["B"]["people"] or out["A1"]["types"] != out["B"]["types"]):
            os.makedirs("/app/data/experiments", exist_ok=True)
            cv2.imwrite(f"/app/data/experiments/ab-{cam}-{name}.jpg", media[0])      # la imagen exacta donde difieren, para revisarla a ojo
        return cam, name, out

    res = []
    with ThreadPoolExecutor(2) as ex:
        for cam, name, out in ex.map(one, jobs):
            res.append((cam, name, out))
            print(f"{cam:16s} {name:5s} A1={out['A1']['people']}/{out['A1']['types']} A2={out['A2']['people']}/{out['A2']['types']} B={out['B']['people']}/{out['B']['types']}", flush=True)
    scenes.COMPACT = False
    ok = [(c, n, o) for c, n, o in res if all(o[t]["act"] != "error" for t in o)]

    def agree(x, y):
        n = len(ok)
        return {"personas": sum(o[x]["people"] == o[y]["people"] for _, _, o in ok) / n, "alertas": sum(o[x]["types"] == o[y]["types"] for _, _, o in ok) / n,
                "severidad": sum(o[x]["sev"] == o[y]["sev"] for _, _, o in ok) / n, "activity": sum(jac(o[x]["act"], o[y]["act"]) for _, _, o in ok) / n}
    print(f"\nentradas validas: {len(ok)} de {len(res)}")
    for lbl, x, y in (("ruido A1 vs A2", "A1", "A2"), ("A1 vs compacto", "A1", "B"), ("A2 vs compacto", "A2", "B")):
        print(f"{lbl:16s}", {k: round(v, 2) for k, v in agree(x, y).items()})
    ta = [t[0] for t in toks.values() if t[0]]
    tb = [t[1] for t in toks.values() if t[1]]
    print("tokens del texto por camara (actual -> compacto):", {c: t for c, t in toks.items()})
    print("promedio:", sum(ta) // max(1, len(ta)), "->", sum(tb) // max(1, len(tb)))
    print("\nDIFERENCIAS DE ALERTA (A1 vs compacto):")
    for c, n, o in ok:
        if o["A1"]["types"] != o["B"]["types"] or o["A1"]["people"] != o["B"]["people"]:
            print(f"- {c} {n}: A1={o['A1']['types']} p={o['A1']['people']} | A2={o['A2']['types']} p={o['A2']['people']} | B={o['B']['types']} p={o['B']['people']}\n    A1: {o['A1']['act'][:110]}\n    B : {o['B']['act'][:110]}")
    os.makedirs("/app/data/experiments", exist_ok=True)
    with open(f"/app/data/experiments/prompt-ab-{time.strftime('%Y%m%d-%H%M')}.json", "w") as f:
        json.dump([{"cam": c, "in": n, **o} for c, n, o in res], f, ensure_ascii=False, indent=1, default=list)


if __name__ == "__main__":
    main()
