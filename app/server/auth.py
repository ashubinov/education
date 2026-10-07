"""Регистрация, вход, сессии (cookie)."""
import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request

from .db import db

COOKIE = "lq_sid"
SESSION_TTL = 60 * 60 * 24 * 90
_attempts: dict[str, list[float]] = {}


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32).hex()


def register(username: str, password: str, display_name: str = "") -> int:
    username = (username or "").strip()
    if not re.fullmatch(r"[\w.\-]{3,32}", username):
        raise HTTPException(400, "Логин: 3–32 символа (буквы, цифры, . _ -)")
    if len(password or "") < 6:
        raise HTTPException(400, "Пароль должен быть не короче 6 символов")
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPException(400, "Такой логин уже занят")
    salt = secrets.token_hex(16)
    is_admin = 0 if db.val("SELECT COUNT(*) FROM users", default=0) else 1
    return db.x(
        "INSERT INTO users(username,display_name,pass_hash,salt,is_admin) VALUES(?,?,?,?,?)",
        (username, (display_name or username).strip()[:40], _hash(password, salt), salt, is_admin),
    )


def login(username: str, password: str, ip: str = "") -> int:
    key = f"{ip}|{(username or '').lower()}"
    hist = [t for t in _attempts.get(key, []) if time.time() - t < 300]
    if len(hist) >= 8:
        raise HTTPException(429, "Слишком много попыток входа. Подождите пару минут.")
    u = db.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    if not u or not hmac.compare_digest(_hash(password or "", u["salt"]), u["pass_hash"]):
        hist.append(time.time())
        _attempts[key] = hist
        raise HTTPException(400, "Неверный логин или пароль")
    _attempts.pop(key, None)
    return u["id"]


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, user_id, time.time()))
    return token


def drop_session(token: str):
    db.x("DELETE FROM sessions WHERE token=?", (token,))


def user_from_request(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    s = db.one("SELECT * FROM sessions WHERE token=?", (token,))
    if not s or time.time() - s["created_at"] > SESSION_TTL:
        return None
    return db.one("SELECT * FROM users WHERE id=?", (s["user_id"],))


def require_user(request: Request) -> dict:
    u = user_from_request(request)
    if not u:
        raise HTTPException(401, "Нужно войти")
    return u
