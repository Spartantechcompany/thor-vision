#!/usr/bin/env python3
"""Genera docs/API_REFERENCE.md desde el OpenAPI del servicio (siempre al dia con el codigo).

  python scripts/gen_api_doc.py http://192.168.0.10:8080/openapi.json
  python scripts/gen_api_doc.py ruta/a/openapi.json
"""
import collections
import json
import pathlib
import sys
import urllib.request

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "API_REFERENCE.md"


def load(src: str) -> dict:
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=15) as r:
            return json.load(r)
    return json.load(open(src, encoding="utf-8"))


def group(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "(raiz)"
    return parts[1] if parts[0] == "api" and len(parts) > 1 else parts[0]


def main() -> int:
    spec = load(sys.argv[1] if len(sys.argv) > 1 else "http://192.168.0.10:8080/openapi.json")
    groups = collections.defaultdict(list)
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            params = ", ".join(f"`{p['name']}`{'*' if p.get('required') else ''}" for p in op.get("parameters", []))
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("$ref", "").split("/")[-1]
            groups[group(path)].append((method.upper(), path, op.get("summary", ""), params, body))
    lines = ["# Referencia de la API", "",
             "Generada con `scripts/gen_api_doc.py` desde `/openapi.json` (no editar a mano). `*` = parametro obligatorio.",
             "La documentacion interactiva esta en `/docs`. Guia de uso y significado de los datos: `docs/API.md`.", ""]
    for g in sorted(groups):
        lines += [f"## {g}", "", "| Metodo | Ruta | Resumen | Parametros | Cuerpo |", "|---|---|---|---|---|"]
        lines += [f"| {m} | `{p}` | {s} | {pa} | {b} |" for m, p, s, pa, b in groups[g]]
        lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{sum(len(v) for v in groups.values())} operaciones en {len(groups)} grupos -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
