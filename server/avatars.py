"""Собственная аватарка: пользователь загружает картинку, сервер проверяет её, уменьшает до 256 px и хранит в той же базе (таблица avatars).

Адрес картинки содержит случайный ключ, который меняется при каждой замене: <img> не умеет отправлять заголовок Authorization,
поэтому сама картинка отдаётся без входа, но угадать чужой ключ нельзя, а браузер может кэшировать её навсегда.
Файл всегда пересоздаётся на сервере (PNG/JPEG), так что то, что мы отдаём, — действительно картинка, а не что-то спрятанное внутри.
"""
import secrets

import pymupdf
from fastapi import HTTPException

from .db import db

MAX_UPLOAD = 1_000_000   # байт в загружаемом файле
MAX_STORED = 250_000     # байт после обработки
MIN_SIDE, MAX_SIDE, TARGET = 16, 2048, 256
PNG_MAGIC, JPEG_MAGIC = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"


def process(data: bytes) -> tuple[bytes, str]:
    """Проверить и пересобрать картинку. Возвращает (байты, mime)."""
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, "Файл слишком большой (до 1 МБ)")
    if data.startswith(PNG_MAGIC):
        fmt = "png"
    elif data.startswith(JPEG_MAGIC):
        fmt = "jpeg"
    else:
        raise HTTPException(400, "Нужна картинка в формате PNG или JPEG")
    try:
        pix = pymupdf.Pixmap(data)
        if pix.width < MIN_SIDE or pix.height < MIN_SIDE or pix.width > MAX_SIDE or pix.height > MAX_SIDE:
            raise HTTPException(400, f"Размер картинки должен быть от {MIN_SIDE} до {MAX_SIDE} пикселей")
        if pix.colorspace is None or pix.colorspace.n != 3:  # серые и CMYK приводим к RGB
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        k = TARGET / max(pix.width, pix.height)
        if k < 1:
            pix = pymupdf.Pixmap(pix, max(1, round(pix.width * k)), max(1, round(pix.height * k)))
        if pix.alpha:
            fmt = "png"
        out = pix.tobytes("jpeg", jpg_quality=88) if fmt == "jpeg" else pix.tobytes("png")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Не удалось прочитать картинку — попробуй другой файл")
    if len(out) > MAX_STORED:
        raise HTTPException(400, "Картинка получилась слишком тяжёлой — выбери попроще")
    return out, "image/" + fmt


def save(user_id: int, data: bytes) -> str:
    out, mime = process(data)
    key = secrets.token_urlsafe(18)
    db.x("INSERT INTO avatars(user_id, key, mime, data) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET key=excluded.key, mime=excluded.mime, data=excluded.data, updated_at=CURRENT_TIMESTAMP",
         (user_id, key, mime, out))
    return key


def remove(user_id: int):
    db.x("DELETE FROM avatars WHERE user_id=?", (user_id,))


def url(user_id: int) -> str | None:
    key = db.val("SELECT key FROM avatars WHERE user_id=?", (user_id,))
    return f"/api/avatars/{key}" if key else None


def get(key: str) -> tuple[bytes, str] | None:
    r = db.one("SELECT data, mime FROM avatars WHERE key=?", (key,))
    return (r["data"], r["mime"]) if r else None
