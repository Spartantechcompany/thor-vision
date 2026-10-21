#!/usr/bin/env python3
"""Bateria fija de auditoria de Thor Vision: misma lista de mediciones antes y despues de cada cambio (idea de Harness2: medir la misma tarea dos veces).

  python scripts/audit_battery.py --base http://192.168.0.10:8080 [--logs /ruta/docker.log] [--json salida.json] [--compare anterior.json]

Mide: peso y tiempo de cada pagina (con y sin gzip), latencia de las APIs, salud de camaras, errores del log, calidad de datos (alertas sin imagen)
y recursos. Solo lectura. Imprime una tabla y devuelve codigo 1 si algun umbral falla."""
import argparse
import gzip
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

PAGES = ["/", "/tv", "/iot", "/presencia", "/wifi", "/buscar", "/zonas", "/identidades", "/camara/cam-189", "/onvif", "/reglas"]
APIS = ["/api/health", "/api/cameras", "/api/detections", "/api/pipeline", "/api/vlm", "/api/iot/devices", "/api/presence/now", "/api/clef", "/api/version",
        "/api/timeline?hours=6", "/api/clips/recent", "/api/yolo-rules", "/api/trips"]
LIMITS = {"page_ms": 1200, "api_ms": 800, "page_kb": 400}


def get(url, gz=False, timeout=25):
    h = {"Accept-Encoding": "gzip"} if gz else {}
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body = r.read()
            ms = (time.perf_counter() - t) * 1000
            return r.status, len(body), ms, r.headers, body
    except urllib.error.HTTPError as e:
        return e.code, 0, (time.perf_counter() - t) * 1000, e.headers, b""
    except (OSError, urllib.error.URLError):
        return 0, 0, (time.perf_counter() - t) * 1000, {}, b""


def med(url, n=3, gz=False):
    xs = [get(url, gz) for _ in range(n)]
    return xs[0][0], xs[0][1], statistics.median(x[2] for x in xs), xs[0][3]


def run(base, logs=None):
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "pages": {}, "apis": {}, "cams": {}, "log": {}, "data": {}}
    for p in PAGES:
        st, size, ms, hd = med(base + p)
        st2, size_gz, ms_gz, hd2 = med(base + p, gz=True)
        out["pages"][p] = {"status": st, "kb": round(size / 1024, 1), "ms": round(ms), "kb_gzip": round(size_gz / 1024, 1), "gzip": (hd2.get("Content-Encoding") == "gzip") if hd2 else False,
                           "cache": (hd.get("Cache-Control") or "-") if hd else "-"}
    for a in APIS:
        st, size, ms, _ = med(base + a)
        out["apis"][a] = {"status": st, "kb": round(size / 1024, 1), "ms": round(ms)}
    st, _, _, _, body = get(base + "/api/cameras")
    try:
        cams = json.loads(body)
        cams = cams if isinstance(cams, list) else cams.get("cameras", [])
        for c in cams:
            out["cams"][c["id"]] = {"state": c.get("state"), "fps": c.get("fps"), "latency_ms": round(c.get("latency_ms") or 0), "attempts": c.get("connect_attempts")}
    except ValueError:
        pass
    st, _, _, _, body = get(base + "/api/vlm")
    try:
        v = json.loads(body)
        out["data"]["vlm"] = {k: v.get(k) for k in ("status", "model_real", "cams_ok", "cams_total")}
    except ValueError:
        pass
    if logs:
        txt = open(logs, encoding="utf-8", errors="replace").read()
        cnt = lambda pat: len(re.findall(pat, txt))
        out["log"] = {"warnings": cnt(r"\[WARNING\]"), "errors": cnt(r"\[ERROR\]"), "tracebacks": cnt(r"Traceback"), "vlm_ok": cnt(r"VLM OK"),
                      **{f"vlm_http_{c}": cnt(rf"VLM HTTP {c}") for c in (400, 429, 502, 503, 504)}, "stalls": cnt(r"sin cuadros nuevos"), "conn_failed": cnt(r"Connection failed"),
                      "sin_captura": cnt(r"sin captura"), "context_exceeded": cnt(r"context_length_exceeded|exceeds context")}
    return out


def verdict(r):
    bad = []
    for p, d in r["pages"].items():
        if d["status"] != 200:
            bad.append(f"pagina {p}: HTTP {d['status']}")
        elif d["ms"] > LIMITS["page_ms"]:
            bad.append(f"pagina {p}: {d['ms']} ms")
        if d["status"] == 200 and d["kb"] > LIMITS["page_kb"] and not d["gzip"]:
            bad.append(f"pagina {p}: {d['kb']} KB sin gzip")
    for a, d in r["apis"].items():
        if d["status"] != 200:
            bad.append(f"api {a}: HTTP {d['status']}")
        elif d["ms"] > LIMITS["api_ms"]:
            bad.append(f"api {a}: {d['ms']} ms")
    for c, d in r["cams"].items():
        if d["state"] != "streaming" or d["latency_ms"] > 5000:
            bad.append(f"camara {c}: {d['state']} latencia {d['latency_ms']} ms")
    return bad


def show(r):
    print(f"== Bateria {r['ts']}")
    print(f"{'pagina':22s} {'KB':>7s} {'KB gzip':>8s} {'ms':>6s} gzip cache")
    for p, d in r["pages"].items():
        print(f"{p:22s} {d['kb']:7.1f} {d['kb_gzip']:8.1f} {d['ms']:6d} {'si' if d['gzip'] else 'NO':4s} {d['cache']}")
    print(f"\n{'api':26s} {'KB':>8s} {'ms':>6s}")
    for a, d in sorted(r["apis"].items(), key=lambda kv: -kv[1]["ms"]):
        print(f"{a:26s} {d['kb']:8.1f} {d['ms']:6d}  {'' if d['status'] == 200 else 'HTTP ' + str(d['status'])}")
    print("\ncamaras:", ", ".join(f"{c}={d['state'][:4]}/{d['fps']}fps/{d['latency_ms']}ms" for c, d in r["cams"].items()))
    if r["log"]:
        print("log:", r["log"])
    print("vlm:", r["data"].get("vlm"))


def compare(a, b):
    print("\n== Antes -> despues (mediana de ms)")
    for p in b["pages"]:
        x, y = a["pages"].get(p), b["pages"][p]
        if x:
            print(f"  {p:22s} {x['ms']:5d} -> {y['ms']:5d} ms   {x['kb']:7.1f} -> {y['kb_gzip'] if y['gzip'] else y['kb']:7.1f} KB sobre la red")
    for k in b["apis"]:
        x, y = a["apis"].get(k), b["apis"][k]
        if x and abs(x["ms"] - y["ms"]) >= 20:
            print(f"  {k:26s} {x['ms']:5d} -> {y['ms']:5d} ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://192.168.0.10:8080")
    ap.add_argument("--logs")
    ap.add_argument("--json")
    ap.add_argument("--compare")
    a = ap.parse_args()
    r = run(a.base.rstrip("/"), a.logs)
    show(r)
    if a.json:
        json.dump(r, open(a.json, "w"), indent=1)
    if a.compare:
        compare(json.load(open(a.compare)), r)
    bad = verdict(r)
    print("\nFALLAS:" if bad else "\nSin fallas de umbral.")
    for b in bad:
        print("  -", b)
    sys.exit(1 if bad else 0)
