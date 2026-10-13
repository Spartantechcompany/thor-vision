"""La version, el CHANGELOG y las notas del dashboard salen de una sola fuente."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))


def test_version_comes_from_first_release_entry():
    from app import version
    top = json.load(open(os.path.join(ROOT, "app", "release_notes.json"), encoding="utf-8"))[0]
    assert version.VERSION == top["version"] and version.RELEASED == top["date"]


def test_changelog_in_sync_and_tag_check():
    ok = subprocess.run([sys.executable, "scripts/release.py", "check"], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout
    bad = subprocess.run([sys.executable, "scripts/release.py", "check", "--tag", "v0.0.1"], cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode == 1 and "no coincide" in bad.stdout


def test_notes_for_version():
    r = subprocess.run([sys.executable, "scripts/release.py", "notes"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip()
