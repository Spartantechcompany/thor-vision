#!/usr/bin/env python3
"""Escribe app/build_info.json con el commit que se despliega (lo muestra /api/version y el pie del dashboard).
Correr justo antes de copiar app/ a Thor: python scripts/stamp_build.py"""
import json
import pathlib
import subprocess
import time

root = pathlib.Path(__file__).resolve().parent.parent


def git(*a: str) -> str:
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=False).stdout.strip()


info = {"commit": git("rev-parse", "--short", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "describe": git("describe", "--tags", "--always", "--dirty"), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
(root / "app" / "build_info.json").write_text(json.dumps(info, indent=2) + "\n")
print(info)
