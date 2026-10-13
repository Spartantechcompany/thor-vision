#!/usr/bin/env python3
"""Versionado: app/release_notes.json es la fuente unica (version, dashboard y CHANGELOG salen de ahi).

  python scripts/release.py write            regenera CHANGELOG.md
  python scripts/release.py check [--tag vX] falla si CHANGELOG esta desfasado o el tag no coincide con la version
  python scripts/release.py notes [X.Y.Z]    imprime las notas de una version (para el GitHub Release)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES = ROOT / "app" / "release_notes.json"
CHANGELOG = ROOT / "CHANGELOG.md"
HEADER = ("# Changelog\n\nGenerado desde `app/release_notes.json` (la misma fuente que muestra el dashboard). "
          "Versionado semántico: mayor = cambia el modelo de datos o el flujo; menor = funciones nuevas; parche = correcciones.\n")


def load() -> list:
    return json.loads(NOTES.read_text(encoding="utf-8"))


def render_one(r: dict, heading: bool = True) -> str:
    out = []
    if heading:
        out.append(f"## v{r['version']} — {r['name']} ({r['date']})")
        out.append("")
    out.append(r.get("summary", ""))
    out.append("")
    for h in r.get("highlights", []):
        out.append(f"- **{h['area']} · {h['title']}**: {h['text']}")
    if r.get("fixes"):
        out.append("")
        out.append("Correcciones:")
        out += [f"- {x}" for x in r["fixes"]]
    if r.get("notes"):
        out.append("")
        out.append("Notas:")
        out += [f"- {x}" for x in r["notes"]]
    return "\n".join(out).rstrip() + "\n"


def render_all() -> str:
    return HEADER + "\n" + "\n".join(render_one(r) for r in load())


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    data = load()
    if cmd == "write":
        CHANGELOG.write_text(render_all(), encoding="utf-8")
        print("CHANGELOG.md regenerado")
        return 0
    if cmd == "notes":
        ver = argv[2].lstrip("v") if len(argv) > 2 else data[0]["version"]
        r = next((x for x in data if x["version"] == ver), None)
        if r is None:
            print(f"version {ver} no existe en release_notes.json", file=sys.stderr)
            return 1
        print(render_one(r, heading=False))
        return 0
    errs = []
    sys.path.insert(0, str(ROOT))
    from app import version
    if version.VERSION != data[0]["version"]:
        errs.append(f"app/version.py ({version.VERSION}) != primera entrada de release_notes.json ({data[0]['version']})")
    if CHANGELOG.read_text(encoding="utf-8") != render_all():
        errs.append("CHANGELOG.md desfasado: corre `python scripts/release.py write` y commitea")
    seen = [r["version"] for r in data]
    if len(set(seen)) != len(seen):
        errs.append("versiones repetidas en release_notes.json")
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1].lstrip("v")
        if tag != data[0]["version"]:
            errs.append(f"el tag v{tag} no coincide con la version {data[0]['version']} de release_notes.json")
    for e in errs:
        print("ERROR:", e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
