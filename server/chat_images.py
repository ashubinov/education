"""Фото в общем чате: пользователь отправляет PNG/JPEG, сервер проверяет файл, пересобирает его и хранит в базе (таблица chat_images).

Безопасность (по образцу chat_gifs.py и avatars.py):
• размеры читаются из заголовка до декодирования, поэтому «бомба» из огромной картинки отсекается раньше, чем попадёт в память;
• картинка всегда пересоздаётся на сервере (JPEG или PNG, не больше MAX_SIDE по большей стороне): метаданные (EXIF, GPS) и всё,
  что было спрятано внутри файла, не сохраняются, отдаётся действительно картинка;
• адрес содержит случайный ключ: <img> не умеет слать заголовок Authorization, а угадать чужой ключ нельзя;
• отдаётся с `nosniff` и запретительным CSP; ограничения: размер файла, число фото в минуту и в сутки, общий объём хранилища;
  фото старше RETENTION_DAYS удаляются (само сообщение остаётся без картинки).
Поворот по EXIF не применяется: браузер приложения перед отправкой сам рисует фото на canvas (с учётом поворота) и убирает метаданные.
"""
from __future__ import annotations

import secrets
import struct
import time

import pymupdf
from fastapi import HTTPException

from .db import db

MAX_UPLOAD = 8_000_000         # байт в загружаемом файле (фото с телефона бывают тяжёлыми)
MAX_STORED = 1_200_000         # байт после обработки
MAX_SIDE_IN = 8000             # самая большая сторона на входе
MAX_PIXELS_IN = 40_000_000
MIN_SIDE = 8
MAX_SIDE = 1600                # на сервере большую сторону уменьшаем до этого
PER_MINUTE = 5
PER_DAY = 50
STORAGE_CAP = 400_000_000      # всего байт фото в базе
RETENTION_DAYS = 30
CAPTION_MAX = 200
PNG_MAGIC, JPEG_MAGIC = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"


def init_schema() -> None:
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_images (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            size INTEGER NOT NULL,
            mime TEXT NOT NULL,
            data BLOB NOT NULL,
            created_ts INTEGER NOT NULL
        );
        """)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chat_messages)")}
        if "image_key" not in cols:
            db.conn.execute("ALTER TABLE chat_messages ADD COLUMN image_key TEXT")
        db.conn.execute("CREATE INDEX IF NOT EXISTS ix_chat_messages_image ON chat_messages(image_key) WHERE image_key IS NOT NULL")
    cleanup()


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ValueError("нет заголовка PNG")
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Найти блок SOF (кадр) и прочитать размеры, не декодируя картинку."""
    pos, n = 2, len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:                               # заполнитель
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):         # маркеры без длины
            pos += 2
            continue
        seg = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > n:
                break
            h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
            return w, h
        pos += 2 + seg
    raise ValueError("нет блока размеров JPEG")


def process(data: bytes) -> tuple[bytes, str]:
    """Проверить и пересобрать фото. Возвращает (байты, mime). Любая проблема — HTTPException 400."""
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, f"Фото тяжелее {MAX_UPLOAD // 1_000_000} МБ — выбери поменьше")
    if data.startswith(PNG_MAGIC):
        fmt, sizer = "png", _png_size
    elif data.startswith(JPEG_MAGIC):
        fmt, sizer = "jpeg", _jpeg_size
    else:
        raise HTTPException(400, "Нужно фото в формате JPEG или PNG (для анимации есть кнопка GIF)")
    try:
        w, h = sizer(data)
    except (ValueError, struct.error):
        raise HTTPException(400, "Файл повреждён: это не корректное фото")
    if w < MIN_SIDE or h < MIN_SIDE or w > MAX_SIDE_IN or h > MAX_SIDE_IN or w * h > MAX_PIXELS_IN:
        raise HTTPException(400, f"Размер фото должен быть от {MIN_SIDE} до {MAX_SIDE_IN} пикселей по стороне")
    try:
        pix = pymupdf.Pixmap(data)
        if pix.colorspace is None or pix.colorspace.n != 3:   # серые и CMYK приводим к RGB
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        k = MAX_SIDE / max(pix.width, pix.height)
        if k < 1:
            pix = pymupdf.Pixmap(pix, max(1, round(pix.width * k)), max(1, round(pix.height * k)))
        if pix.alpha:
            fmt = "png"
        out = pix.tobytes("jpeg", jpg_quality=85) if fmt == "jpeg" else pix.tobytes("png")
        if fmt == "png" and len(out) > MAX_STORED and not pix.alpha:   # тяжёлый PNG без прозрачности лучше хранить как JPEG
            out, fmt = pix.tobytes("jpeg", jpg_quality=85), "jpeg"
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Не удалось прочитать фото — попробуй другой файл")
    if len(out) > MAX_STORED:
        raise HTTPException(400, "Фото получилось слишком тяжёлым — выбери попроще")
    return out, "image/" + fmt


def cleanup(now: int | None = None) -> None:
    """Убрать фото без сообщений и просроченные (сообщение остаётся, картинка пропадает)."""
    now = now or int(time.time())
    old = now - RETENTION_DAYS * 86400
    with db.tx():
        db.x("UPDATE chat_messages SET image_key=NULL WHERE image_key IN (SELECT key FROM chat_images WHERE created_ts<?)", (old,))
        db.x("DELETE FROM chat_images WHERE created_ts<?", (old,))
        db.x("DELETE FROM chat_images WHERE key NOT IN (SELECT image_key FROM chat_messages WHERE image_key IS NOT NULL)")


def check_limits(user_id: int, size: int, now: int) -> None:
    """Вызывается внутри транзакции перед сохранением."""
    if db.val("SELECT COUNT(*) FROM chat_images WHERE user_id=? AND created_ts>=?", (user_id, now - 60), 0) >= PER_MINUTE:
        raise HTTPException(429, f"Не больше {PER_MINUTE} фото в минуту")
    if db.val("SELECT COUNT(*) FROM chat_images WHERE user_id=? AND created_ts>=?", (user_id, now - 86400), 0) >= PER_DAY:
        raise HTTPException(429, f"Не больше {PER_DAY} фото в сутки")
    if (db.val("SELECT COALESCE(SUM(size),0) FROM chat_images", default=0) or 0) + size > STORAGE_CAP:
        raise HTTPException(507, "Хранилище фото переполнено — попробуй позже")


def store(user_id: int, data: bytes, mime: str, now: int) -> str:
    key = secrets.token_urlsafe(24)
    db.x("INSERT INTO chat_images(key,user_id,size,mime,data,created_ts) VALUES(?,?,?,?,?,?)", (key, user_id, len(data), mime, data, now))
    return key


def get(key: str) -> tuple[bytes, str] | None:
    r = db.one("SELECT data, mime FROM chat_images WHERE key=?", (key,))
    return (r["data"], r["mime"]) if r else None


def url(key: str | None) -> str | None:
    return f"/api/chat/images/{key}" if key else None
