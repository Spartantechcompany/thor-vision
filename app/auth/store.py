"""Cuentas en SQLite (data/auth.db, dentro del volumen ./data). Sin contraseñas en claro ni secretos en logs."""

import os
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.Lock()


def db_path() -> str:
    return os.environ.get("THOR_AUTH_DB", "/app/data/auth.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ip TEXT NOT NULL,
        action TEXT NOT NULL, detail TEXT NOT NULL)""")
    return c


def get_user(username: str) -> dict | None:
    with _lock, _conn() as c:
        fila = c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    return dict(fila) if fila else None


def create_user(username: str, password_hash: str) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO users (username, password_hash, totp_secret, totp_enabled, active, created_at) "
                  "VALUES (?, ?, NULL, 0, 1, ?)",
                  (username, password_hash, datetime.now(timezone.utc).isoformat()))


def set_password(username: str, password_hash: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))


def set_pending_secret(username: str, secret: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE users SET totp_secret = ? WHERE username = ? AND totp_enabled = 0", (secret, username))


def enable_totp(username: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE users SET totp_enabled = 1 WHERE username = ?", (username,))


def set_active(username: str, active: bool) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE users SET active = ? WHERE username = ?", (1 if active else 0, username))


def list_users() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(f) for f in c.execute("SELECT username, totp_enabled, active, created_at FROM users")]


def log(ip: str, action: str, detail: str) -> None:
    """Bitácora de accesos: quién, desde dónde y qué. Nunca contraseñas ni códigos."""
    with _lock, _conn() as c:
        c.execute("INSERT INTO audit (at, ip, action, detail) VALUES (?, ?, ?, ?)",
                  (datetime.now(timezone.utc).isoformat(), ip, action, detail[:300]))
