#!/usr/bin/env python3
"""Trinquete de fiabilidad: cuenta hallazgos de ruff por regla y falla si alguno AUMENTA respecto a .ci/ruff-baseline.json.

Reglas: except genericos y silenciosos (esconden fallos), raise sin causa. La base actual es deuda conocida;
solo se permite que baje. Para fijar una base nueva: python scripts/ruff_ratchet.py --update
"""
import collections
import json
import pathlib
import subprocess
import sys

SELECT = "BLE001,S110,S112,B904"
BASE = pathlib.Path(__file__).resolve().parent.parent / ".ci" / "ruff-baseline.json"


def current() -> dict:
    out = subprocess.run(["ruff", "check", "app", "--select", SELECT, "--output-format", "json", "--exit-zero"],
                         capture_output=True, text=True, check=True).stdout
    return dict(collections.Counter(d["code"] for d in json.loads(out or "[]")))


def main() -> int:
    now = current()
    if "--update" in sys.argv:
        BASE.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
        print("base actualizada:", now)
        return 0
    base = json.loads(BASE.read_text())
    bad = {k: (base.get(k, 0), v) for k, v in now.items() if v > base.get(k, 0)}
    for k, (b, v) in sorted(bad.items()):
        print(f"AUMENTO {k}: {b} -> {v} (no agregues except genericos/silenciosos; atrapa lo especifico o registra el error)")
    better = {k: (base[k], now.get(k, 0)) for k in base if now.get(k, 0) < base[k]}
    for k, (b, v) in sorted(better.items()):
        print(f"mejora {k}: {b} -> {v}  (corre --update para fijar la nueva base)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
