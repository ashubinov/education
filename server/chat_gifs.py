"""GIF-картинки в общем чате: пользователь загружает .gif, сервер проверяет структуру файла и хранит его в базе (таблица chat_gifs).

Безопасность:
• принимается только настоящий GIF (заголовок GIF87a/GIF89a, разбор блоков до завершающего байта 0x3B); всё, что дописано после него, отбрасывается,
  поэтому «картинка со спрятанным внутри» не пройдёт; число кадров, размеры и «вес» ограничены (защита от «бомб» для браузера);
• адрес содержит случайный ключ (как у аватарок): <img> не умеет слать заголовок Authorization, а угадать чужой ключ нельзя;
• отдаётся строго как image/gif с `nosniff` и запретительным CSP, браузер не станет трактовать файл как страницу;
• ограничения: размер файла, не больше N гифок в минуту и в сутки на человека, общий объём хранилища; гифки старше RETENTION_DAYS удаляются
  (само сообщение остаётся без картинки).
"""
from __future__ import annotations

import secrets
import struct
import time

from fastapi import HTTPException

from .db import db

MAX_BYTES = 3_000_000
MAX_SIDE = 2000
MAX_FRAMES = 300
MAX_PIXELS = 60_000_000        # ширина × высота × кадры
PER_MINUTE = 5
PER_DAY = 30
STORAGE_CAP = 300_000_000      # всего байт гифок в базе
RETENTION_DAYS = 30
CAPTION_MAX = 200


def init_schema() -> None:
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_gifs (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            size INTEGER NOT NULL,
            data BLOB NOT NULL,
            created_ts INTEGER NOT NULL
        );
        """)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(chat_messages)")}
        if "gif_key" not in cols:
            db.conn.execute("ALTER TABLE chat_messages ADD COLUMN gif_key TEXT")
        db.conn.execute("CREATE INDEX IF NOT EXISTS ix_chat_messages_gif ON chat_messages(gif_key) WHERE gif_key IS NOT NULL")
    cleanup()


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    while True:
        if pos >= len(data):
            raise ValueError("обрыв файла")
        n = data[pos]
        pos += 1
        if n == 0:
            return pos
        pos += n
        if pos > len(data):
            raise ValueError("обрыв файла")


def clean(data: bytes) -> bytes:
    """Проверить GIF и вернуть его байты без хвоста после завершающего блока. Любая проблема — HTTPException 400."""
    if len(data) > MAX_BYTES:
        raise HTTPException(400, f"GIF слишком тяжёлый: не больше {MAX_BYTES // 1_000_000} МБ")
    if len(data) < 20 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise HTTPException(400, "Это не GIF-файл")
    try:
        w, h = struct.unpack("<HH", data[6:10])
        if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
            raise HTTPException(400, f"Размер GIF должен быть от 1 до {MAX_SIDE} пикселей по каждой стороне")
        flags = data[10]
        pos = 13
        if flags & 0x80:
            pos += 3 * (2 << (flags & 7))
        frames = 0
        while True:
            if pos >= len(data):
                raise ValueError("нет завершающего блока")
            b = data[pos]
            pos += 1
            if b == 0x3B:
                break
            if b == 0x21:                       # расширение: метка + подблоки
                pos = _skip_sub_blocks(data, pos + 1)
            elif b == 0x2C:                     # кадр: дескриптор, палитра, LZW-данные
                if pos + 9 > len(data):
                    raise ValueError("обрыв файла")
                fl = data[pos + 8]
                pos += 9
                if fl & 0x80:
                    pos += 3 * (2 << (fl & 7))
                if pos >= len(data) or not 2 <= data[pos] <= 8:
                    raise ValueError("неверный код сжатия")
                pos = _skip_sub_blocks(data, pos + 1)
                frames += 1
                if frames > MAX_FRAMES:
                    raise HTTPException(400, f"В GIF слишком много кадров (не больше {MAX_FRAMES})")
            else:
                raise ValueError("неизвестный блок")
    except HTTPException:
        raise
    except (ValueError, IndexError, struct.error):
        raise HTTPException(400, "Файл повреждён: это не корректный GIF")
    if frames < 1:
        raise HTTPException(400, "В GIF нет ни одного кадра")
    if w * h * frames > MAX_PIXELS:
        raise HTTPException(400, "GIF слишком большой по размеру и числу кадров — сделай поменьше")
    return data[:pos]


def cleanup(now: int | None = None) -> None:
    """Убрать гифки без сообщений и просроченные (сообщение остаётся, картинка пропадает)."""
    now = now or int(time.time())
    old = now - RETENTION_DAYS * 86400
    with db.tx():
        db.x("UPDATE chat_messages SET gif_key=NULL WHERE gif_key IN (SELECT key FROM chat_gifs WHERE created_ts<?)", (old,))
        db.x("DELETE FROM chat_gifs WHERE created_ts<?", (old,))
        db.x("DELETE FROM chat_gifs WHERE key NOT IN (SELECT gif_key FROM chat_messages WHERE gif_key IS NOT NULL)")


def check_limits(user_id: int, size: int, now: int) -> None:
    """Вызывается внутри транзакции перед сохранением."""
    if db.val("SELECT COUNT(*) FROM chat_gifs WHERE user_id=? AND created_ts>=?", (user_id, now - 60), 0) >= PER_MINUTE:
        raise HTTPException(429, f"Не больше {PER_MINUTE} гифок в минуту")
    if db.val("SELECT COUNT(*) FROM chat_gifs WHERE user_id=? AND created_ts>=?", (user_id, now - 86400), 0) >= PER_DAY:
        raise HTTPException(429, f"Не больше {PER_DAY} гифок в сутки")
    if (db.val("SELECT COALESCE(SUM(size),0) FROM chat_gifs", default=0) or 0) + size > STORAGE_CAP:
        raise HTTPException(507, "Хранилище гифок переполнено — попробуй позже")


def store(user_id: int, data: bytes, now: int) -> str:
    key = secrets.token_urlsafe(24)
    db.x("INSERT INTO chat_gifs(key,user_id,size,data,created_ts) VALUES(?,?,?,?,?)", (key, user_id, len(data), data, now))
    return key


def get(key: str) -> bytes | None:
    r = db.one("SELECT data FROM chat_gifs WHERE key=?", (key,))
    return r["data"] if r else None


def url(key: str | None) -> str | None:
    return f"/api/chat/gifs/{key}" if key else None
