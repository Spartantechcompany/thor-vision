"""Límite de intentos: por IP (5 fallos en 5 min) y por cuenta (10 fallos en 15 min, sin revelar si existe)."""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_ip = defaultdict(deque)
_user = defaultdict(deque)
IP_MAX, IP_WIN = 5, 300
USER_MAX, USER_WIN = 10, 900


def _prune(q: deque, win: float, now: float) -> None:
    while q and now - q[0] > win:
        q.popleft()


def ip_blocked(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        q = _ip[ip]
        _prune(q, IP_WIN, now)
        return len(q) >= IP_MAX


def user_blocked(username: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        q = _user[username.lower()]
        _prune(q, USER_WIN, now)
        return len(q) >= USER_MAX


def fail(ip: str, username: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        _ip[ip].append(now)
        _user[username.lower()].append(now)


def ok(ip: str) -> None:
    with _lock:
        _ip.pop(ip, None)
