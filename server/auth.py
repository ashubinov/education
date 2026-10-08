"""Регистрация, вход и JWT-токены (заголовок Authorization: Bearer ...).

Токен — HS256, срок жизни 30 дней, в нём id пользователя и версия токенов (token_version):
«выйти на всех устройствах» увеличивает версию и обесценивает все выданные токены.
Секрет — переменная JWT_SECRET; если не задана, генерируется один раз и хранится в БД (в облаке БД лежит в /data и переживает перезапуски).
"""
import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import HTTPException, Request

from . import config
from .db import db

TOKEN_TTL = 60 * 60 * 24 * 30
SYSTEM_USERNAME = "catalog"  # служебный владелец готовых курсов, войти под ним нельзя
_attempts: dict[str, list[float]] = {}
_reg_attempts: dict[str, list[float]] = {}


def secret() -> str:
    s = config.env("JWT_SECRET")
    if s:
        return s
    s = db.get_setting("jwt_secret")
    if not s:
        s = secrets.token_urlsafe(48)
        db.set_setting("jwt_secret", s)
    return s


def client_ip(request: Request) -> str:
    if config.env("TRUST_PROXY", "1") != "0":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32).hex()


def _throttle(store: dict, key: str, limit: int, window: int, msg: str):
    hist = [t for t in store.get(key, []) if time.time() - t < window]
    if len(hist) >= limit:
        raise HTTPException(429, msg)
    hist.append(time.time())
    store[key] = hist


def is_admin_name(username: str) -> bool:
    admin = config.env("ADMIN_USERNAME").strip().lower()
    return bool(admin) and username.strip().lower() == admin


def register(username: str, password: str, display_name: str = "", ip: str = "") -> int:
    if config.env("ALLOW_REGISTRATION", "1").lower() in ("0", "false", "no") and db.val("SELECT COUNT(*) FROM users WHERE username<>?", (SYSTEM_USERNAME,), default=0):
        raise HTTPException(403, "Регистрация закрыта администратором")
    _throttle(_reg_attempts, ip or "-", 15, 3600, "Слишком много регистраций с этого адреса. Попробуйте позже.")
    username = username.strip()
    if username.lower() == SYSTEM_USERNAME or db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPException(400, "Такой логин уже занят")
    salt = secrets.token_hex(16)
    if config.env("ADMIN_USERNAME").strip():
        admin = 1 if is_admin_name(username) else 0
    else:  # без ADMIN_USERNAME администратор — первый зарегистрированный (удобно для локального запуска)
        admin = 0 if db.val("SELECT COUNT(*) FROM users WHERE username<>?", (SYSTEM_USERNAME,), default=0) else 1
    return db.x("INSERT INTO users(username,display_name,pass_hash,salt,is_admin) VALUES(?,?,?,?,?)",
                (username, (display_name or username).strip()[:40], _hash(password, salt), salt, admin))


def login(username: str, password: str, ip: str = "") -> int:
    key = f"{ip}|{username.lower()}"
    hist = [t for t in _attempts.get(key, []) if time.time() - t < 300]
    if len(hist) >= 8:
        raise HTTPException(429, "Слишком много попыток входа. Подождите пару минут.")
    u = db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not u or u["username"].lower() == SYSTEM_USERNAME or not hmac.compare_digest(_hash(password, u["salt"]), u["pass_hash"]):
        hist.append(time.time())
        _attempts[key] = hist
        raise HTTPException(400, "Неверный логин или пароль")
    _attempts.pop(key, None)
    if is_admin_name(u["username"]) and not u["is_admin"]:
        db.x("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
    return u["id"]


def change_password(user_id: int, current: str, new: str, ip: str = ""):
    """Сменить пароль: проверяет текущий, обновляет соль и хеш, отзывает все прежние токены (вход на других устройствах)."""
    u = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    _throttle(_attempts, f"pwd|{user_id}|{ip}", 8, 300, "Слишком много попыток. Подождите пару минут.")
    if not u or not hmac.compare_digest(_hash(current, u["salt"]), u["pass_hash"]):
        raise HTTPException(400, "Текущий пароль неверный")
    if hmac.compare_digest(current.encode(), new.encode()):
        raise HTTPException(400, "Новый пароль должен отличаться от текущего")
    salt = secrets.token_hex(16)
    db.x("UPDATE users SET pass_hash=?, salt=?, token_version=COALESCE(token_version,0)+1 WHERE id=?", (_hash(new, salt), salt, user_id))


def make_token(user_id: int) -> str:
    u = db.one("SELECT token_version FROM users WHERE id=?", (user_id,))
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "tv": u["token_version"] or 0, "iat": now, "exp": now + TOKEN_TTL}, secret(), algorithm="HS256")


def revoke_all(user_id: int):
    db.x("UPDATE users SET token_version=COALESCE(token_version,0)+1 WHERE id=?", (user_id,))


def user_from_request(request: Request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    try:
        data = jwt.decode(h[7:].strip(), secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]})
        u = db.one("SELECT * FROM users WHERE id=?", (int(data["sub"]),))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    if not u or (u["token_version"] or 0) != data.get("tv", 0) or u["username"] == SYSTEM_USERNAME:
        return None
    if is_admin_name(u["username"]) and not u["is_admin"]:  # ADMIN_USERNAME добавили/изменили уже после регистрации
        db.x("UPDATE users SET is_admin=1 WHERE id=?", (u["id"],))
        u["is_admin"] = 1
    return u


def require_user(request: Request) -> dict:
    u = user_from_request(request)
    if not u:
        raise HTTPException(401, "Нужно войти")
    return u
