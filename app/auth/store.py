"""Cuentas en SQLite (data/auth.db, dentro del volumen ./data). Sin contraseñas en claro ni secretos en logs."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
ROLES = ("admin", "operador")


def db_path() -> str:
    return os.environ.get("THOR_AUTH_DB", "/app/data/auth.db")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""")
    columnas = {f["name"] for f in c.execute("PRAGMA table_info(users)")}
    if "role" not in columnas:  # migración: cuentas creadas antes de los roles quedan como operador
        c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'operador'")
    if "sin_2fa" not in columnas:  # cuentas exentas del código de 6 dígitos (solo se asigna desde la terminal)
        c.execute("ALTER TABLE users ADD COLUMN sin_2fa INTEGER NOT NULL DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ip TEXT NOT NULL,
        action TEXT NOT NULL, detail TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ip_blocks (
        ip TEXT PRIMARY KEY, motivo TEXT NOT NULL, creado TEXT NOT NULL, hasta TEXT NOT NULL)""")
    return c


def get_user(username: str) -> dict | None:
    with _lock, _conn() as c:
        fila = c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    return dict(fila) if fila else None


def create_user(username: str, password_hash: str, role: str = "operador") -> None:
    if role not in ROLES:
        raise ValueError("rol desconocido")
    with _lock, _conn() as c:
        c.execute("INSERT INTO users (username, password_hash, totp_secret, totp_enabled, active, created_at, role) "
                  "VALUES (?, ?, NULL, 0, 1, ?, ?)",
                  (username, password_hash, _ahora().isoformat(), role))


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


def set_role(username: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError("rol desconocido")
    with _lock, _conn() as c:
        c.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))


def admins_activos_sin(username: str) -> int:
    """Cuántos administradores activos quedarían si se quita a «username» (sin contarlo)."""
    with _lock, _conn() as c:
        fila = c.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 "
                         "AND username <> ? COLLATE NOCASE", (username,)).fetchone()
    return fila["n"]


def list_users() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(f) for f in c.execute(
            "SELECT username, role, totp_enabled, active, created_at FROM users ORDER BY username COLLATE NOCASE")]


def block_ip(ip: str, motivo: str, segundos: int) -> None:
    ahora = _ahora()
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO ip_blocks (ip, motivo, creado, hasta) VALUES (?, ?, ?, ?)",
                  (ip, motivo[:120], ahora.isoformat(), (ahora + timedelta(seconds=segundos)).isoformat()))


def ip_block(ip: str) -> dict | None:
    """Bloqueo vigente de una IP, o None."""
    with _lock, _conn() as c:
        fila = c.execute("SELECT * FROM ip_blocks WHERE ip = ? AND hasta > ?", (ip, _ahora().isoformat())).fetchone()
    return dict(fila) if fila else None


def unblock_ip(ip: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM ip_blocks WHERE ip = ?", (ip,))


def list_blocks() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(f) for f in c.execute(
            "SELECT * FROM ip_blocks WHERE hasta > ? ORDER BY creado DESC", (_ahora().isoformat(),))]


def log(ip: str, action: str, detail: str) -> None:
    """Bitácora de accesos: quién, desde dónde y qué. Nunca contraseñas ni códigos."""
    with _lock, _conn() as c:
        c.execute("INSERT INTO audit (at, ip, action, detail) VALUES (?, ?, ?, ?)",
                  (_ahora().isoformat(), ip, action, detail[:300]))


def list_audit(limite: int = 40) -> list[dict]:
    with _lock, _conn() as c:
        return [dict(f) for f in c.execute("SELECT at, ip, action, detail FROM audit ORDER BY id DESC LIMIT ?",
                                           (limite,))]
