"""Общий чат LearnQuest: свободная публикация без премодерации или админского удаления.

Только авторизованные пользователи. История + упорядоченный журнал событий,
который фронтенд получает короткими опросами без перезагрузки страницы.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import auth, avatars, chat_gifs, chat_images, config
from .db import db

router = APIRouter(prefix="/api/chat", tags=["Общий чат"])
MAX_TEXT = 1000
HISTORY_SIZE = 50
UPDATES_SIZE = 100
MIN_GAP_SECONDS = 2
PER_MINUTE_LIMIT = 15


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


def init_schema() -> None:
    """Идемпотентная инициализация схемы на запуске приложения.

    Триггер удаляет сообщения при удалении аккаунта, т.к. foreign_keys в
    существующей БД выключены. Это НЕ модерация контента.
    """
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS ix_chat_messages_recent ON chat_messages(id DESC);
        CREATE INDEX IF NOT EXISTS ix_chat_messages_rate ON chat_messages(user_id, created_ts DESC);
        CREATE TABLE IF NOT EXISTS chat_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('sent', 'deleted')),
            created_ts INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS tr_chat_user_removed
        BEFORE DELETE ON users
        BEGIN
            INSERT INTO chat_events(message_id,kind,created_ts)
                SELECT id, 'deleted', CAST(strftime('%s','now') AS INTEGER)
                  FROM chat_messages WHERE user_id=OLD.id;
            DELETE FROM chat_messages WHERE user_id=OLD.id;
        END;
        """)
        # Compatible with existing chat_messages created before mini-games.
        columns = {r[1] for r in db.conn.execute("PRAGMA table_info(chat_messages)")}
        if "system_kind" not in columns:
            db.conn.execute("ALTER TABLE chat_messages ADD COLUMN system_kind TEXT")
    chat_gifs.init_schema()
    chat_images.init_schema()


def _identity(request: Request) -> dict:
    if config.env("CHAT_ENABLED", "1").lower() in ("0", "false", "no"):
        raise HTTPException(503, "Общий чат временно недоступен")
    return auth.require_user(request)


_SELECT = """SELECT m.id, m.user_id, m.text, m.created_at, m.system_kind, m.gif_key, m.image_key,
                  u.display_name, u.username, u.avatar,
                  (SELECT c.tag FROM clan_members cm JOIN clans c ON c.id=cm.clan_id WHERE cm.user_id=m.user_id) AS clan_tag
             FROM chat_messages m JOIN users u ON u.id=m.user_id
             WHERE COALESCE(u.banned,0)=0"""  # сообщения заблокированных пользователей в чате не показываются


def _public(r: dict) -> dict:
    return {
        "id": r["id"], "user_id": r["user_id"],
        "name": r["display_name"] or r["username"],
        "avatar": r["avatar"] or "👤",
        "avatar_url": avatars.url(r["user_id"]),
        "text": r["text"], "created_at": r["created_at"], "system_kind": r["system_kind"],
        "clan_tag": r.get("clan_tag"),
        "gif_url": chat_gifs.url(r.get("gif_key")),
        "image_url": chat_images.url(r.get("image_key")),
    }


def history(before_id: int | None = None) -> dict:
    # Считываем границу событий ДО истории: всё более позднее клиент получит через updates.
    cursor = db.val("SELECT COALESCE(MAX(id), 0) FROM chat_events", default=0)
    if before_id is None:
        rows = db.q(_SELECT + " ORDER BY m.id DESC LIMIT ?", (HISTORY_SIZE,))
    else:
        rows = db.q(_SELECT + " AND m.id<? ORDER BY m.id DESC LIMIT ?", (before_id, HISTORY_SIZE))
    return {"messages": [_public(r) for r in reversed(rows)], "cursor": cursor,
            "has_more": len(rows) == HISTORY_SIZE}


def updates(after: int) -> dict:
    events = db.q("SELECT id, message_id, kind FROM chat_events WHERE id>? ORDER BY id ASC LIMIT ?",
                  (after, UPDATES_SIZE))
    result = []
    for e in events:
        event = {"seq": e["id"], "kind": e["kind"], "message_id": e["message_id"]}
        if e["kind"] == "sent":
            row = db.one(_SELECT + " AND m.id=?", (e["message_id"],))
            if row:
                event["message"] = _public(row)
            else:
                event["kind"] = "deleted"  # аккаунт удалён после отправки
        result.append(event)
    return {"events": result, "cursor": events[-1]["id"] if events else after,
            "has_more": len(events) == UPDATES_SIZE}


def send(user_id: int, text: str, request_id: str) -> dict:
    text = text.strip()
    if not text:
        raise HTTPException(422, "Нельзя отправить пустое сообщение")
    if len(text) > MAX_TEXT:
        raise HTTPException(422, "Сообщение слишком длинное")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise HTTPException(422, "Недопустимые управляющие символы")
    now = int(time.time())
    with db.tx():
        # Даже если администратор удалил аккаунт уже после проверки JWT,
        # нельзя допускать записи сообщения без существующего владельца.
        user = db.one("SELECT id,banned FROM users WHERE id=?", (user_id,))
        if not user or user["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        old = db.one("SELECT id FROM chat_messages WHERE user_id=? AND request_id=?",
                     (user_id, request_id))
        if old:
            row = db.one(_SELECT + " AND m.id=?", (old["id"],))
            return {**_public(row), "replayed": True}
        recent = db.one("SELECT MAX(created_ts) AS last, COUNT(*) AS count FROM chat_messages "
                        "WHERE user_id=? AND created_ts>=?", (user_id, now - 60))
        if recent["last"] is not None and now - recent["last"] < MIN_GAP_SECONDS:
            raise HTTPException(429, "Между сообщениями должно пройти 2 секунды")
        if recent["count"] >= PER_MINUTE_LIMIT:
            raise HTTPException(429, "Не больше 15 сообщений за минуту")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mid = db.x("INSERT INTO chat_messages(user_id,request_id,text,created_ts,created_at) VALUES(?,?,?,?,?)",
                   (user_id, request_id, text, now, stamp))
        db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'sent',?)", (mid, now))
        row = db.one(_SELECT + " AND m.id=?", (mid,))
    return {**_public(row), "replayed": False}


@router.get("/messages")
async def chat_history(request: Request, before_id: int | None = Query(None, ge=1)):
    _identity(request)
    return await run_in_threadpool(history, before_id)


@router.get("/updates")
async def chat_updates(request: Request, after: int = Query(0, ge=0)):
    _identity(request)
    return await run_in_threadpool(updates, after)


@router.post("/messages")
async def chat_send(request: Request, body: MessageIn):
    user = _identity(request)
    return await run_in_threadpool(send, user["id"], body.text, body.request_id)


def send_gif(user_id: int, data: bytes, caption: str, request_id: str) -> dict:
    """Отправить GIF (с необязательной подписью) в общий чат. Лимиты сообщений — те же, что у текста, плюс свои лимиты на гифки."""
    caption = (caption or "").strip()
    if len(caption) > chat_gifs.CAPTION_MAX:
        raise HTTPException(422, f"Подпись к гифке — не больше {chat_gifs.CAPTION_MAX} символов")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in caption):
        raise HTTPException(422, "Недопустимые управляющие символы")
    gif = chat_gifs.clean(data)
    now = int(time.time())
    with db.tx():
        user = db.one("SELECT id,banned FROM users WHERE id=?", (user_id,))
        if not user or user["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        old = db.one("SELECT id FROM chat_messages WHERE user_id=? AND request_id=?", (user_id, request_id))
        if old:
            row = db.one(_SELECT + " AND m.id=?", (old["id"],))
            return {**_public(row), "replayed": True}
        recent = db.one("SELECT MAX(created_ts) AS last, COUNT(*) AS count FROM chat_messages WHERE user_id=? AND created_ts>=?", (user_id, now - 60))
        if recent["last"] is not None and now - recent["last"] < MIN_GAP_SECONDS:
            raise HTTPException(429, "Между сообщениями должно пройти 2 секунды")
        if recent["count"] >= PER_MINUTE_LIMIT:
            raise HTTPException(429, "Не больше 15 сообщений за минуту")
        chat_gifs.check_limits(user_id, len(gif), now)
        key = chat_gifs.store(user_id, gif, now)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mid = db.x("INSERT INTO chat_messages(user_id,request_id,text,created_ts,created_at,gif_key) VALUES(?,?,?,?,?,?)",
                   (user_id, request_id, caption, now, stamp, key))
        db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'sent',?)", (mid, now))
        row = db.one(_SELECT + " AND m.id=?", (mid,))
    chat_gifs.cleanup(now)
    return {**_public(row), "replayed": False}


@router.post("/gif")
async def chat_send_gif(request: Request, file: UploadFile = File(...), request_id: str = Form(..., pattern=r"^[A-Za-z0-9_-]{16,64}$"),
                        caption: str = Form("")):
    user = _identity(request)
    data = await file.read(chat_gifs.MAX_BYTES + 1)
    return await run_in_threadpool(send_gif, user["id"], data, caption, request_id)


@router.get("/gifs/{key}")
async def chat_gif_image(key: str):
    """Сама картинка отдаётся без входа (<img> не шлёт Authorization), но адрес с длинным случайным ключом угадать нельзя."""
    data = await run_in_threadpool(chat_gifs.get, key[:80])
    if data is None:
        raise HTTPException(404, "Картинка не найдена")
    return Response(data, media_type="image/gif", headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox", "Content-Disposition": "inline"})


def send_image(user_id: int, data: bytes, caption: str, request_id: str) -> dict:
    """Отправить фото (с необязательной подписью) в общий чат. Лимиты сообщений те же, что у текста, плюс свои лимиты на фото."""
    caption = (caption or "").strip()
    if len(caption) > chat_images.CAPTION_MAX:
        raise HTTPException(422, f"Подпись к фото — не больше {chat_images.CAPTION_MAX} символов")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in caption):
        raise HTTPException(422, "Недопустимые управляющие символы")
    img, mime = chat_images.process(data)
    now = int(time.time())
    with db.tx():
        user = db.one("SELECT id,banned FROM users WHERE id=?", (user_id,))
        if not user or user["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        old = db.one("SELECT id FROM chat_messages WHERE user_id=? AND request_id=?", (user_id, request_id))
        if old:
            row = db.one(_SELECT + " AND m.id=?", (old["id"],))
            return {**_public(row), "replayed": True}
        recent = db.one("SELECT MAX(created_ts) AS last, COUNT(*) AS count FROM chat_messages WHERE user_id=? AND created_ts>=?", (user_id, now - 60))
        if recent["last"] is not None and now - recent["last"] < MIN_GAP_SECONDS:
            raise HTTPException(429, "Между сообщениями должно пройти 2 секунды")
        if recent["count"] >= PER_MINUTE_LIMIT:
            raise HTTPException(429, "Не больше 15 сообщений за минуту")
        chat_images.check_limits(user_id, len(img), now)
        key = chat_images.store(user_id, img, mime, now)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mid = db.x("INSERT INTO chat_messages(user_id,request_id,text,created_ts,created_at,image_key) VALUES(?,?,?,?,?,?)",
                   (user_id, request_id, caption, now, stamp, key))
        db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'sent',?)", (mid, now))
        row = db.one(_SELECT + " AND m.id=?", (mid,))
    chat_images.cleanup(now)
    return {**_public(row), "replayed": False}


@router.post("/image")
async def chat_send_image(request: Request, file: UploadFile = File(...), request_id: str = Form(..., pattern=r"^[A-Za-z0-9_-]{16,64}$"),
                          caption: str = Form("")):
    user = _identity(request)
    data = await file.read(chat_images.MAX_UPLOAD + 1)
    return await run_in_threadpool(send_image, user["id"], data, caption, request_id)


@router.get("/images/{key}")
async def chat_image_file(key: str):
    """Само фото отдаётся без входа (<img> не шлёт Authorization), но адрес с длинным случайным ключом угадать нельзя."""
    got = await run_in_threadpool(chat_images.get, key[:80])
    if got is None:
        raise HTTPException(404, "Картинка не найдена")
    data, mime = got
    return Response(data, media_type=mime, headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox", "Content-Disposition": "inline"})


def delete_message(message_id: int) -> dict:
    """Удаление сообщения администратором: пропадает из истории, а открытые окна чата получают событие «deleted»."""
    now = int(time.time())
    with db.tx():
        if not db.one("SELECT id FROM chat_messages WHERE id=?", (message_id,)):
            raise HTTPException(404, "Сообщение не найдено")
        gif = db.val("SELECT gif_key FROM chat_messages WHERE id=?", (message_id,))
        image = db.val("SELECT image_key FROM chat_messages WHERE id=?", (message_id,))
        db.x("DELETE FROM chat_messages WHERE id=?", (message_id,))
        if gif:
            db.x("DELETE FROM chat_gifs WHERE key=?", (gif,))
        if image:
            db.x("DELETE FROM chat_images WHERE key=?", (image,))
        db.x("UPDATE chat_command_uses SET message_id=NULL WHERE message_id=?", (message_id,))
        db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'deleted',?)", (message_id, now))
    return {"ok": True, "id": message_id}


def hide_user_messages(user_id: int) -> int:
    """При блокировке пользователя его сообщения исчезают у всех, кто сейчас в чате (в истории они скрыты запросом)."""
    now = int(time.time())
    with db.tx():
        ids = [r["id"] for r in db.q("SELECT id FROM chat_messages WHERE user_id=?", (user_id,))]
        for mid in ids:
            db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'deleted',?)", (mid, now))
    return len(ids)


@router.delete("/messages/{message_id}")
async def chat_delete(message_id: int, request: Request):
    user = _identity(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Удалять сообщения может только администратор")
    return await run_in_threadpool(delete_message, message_id)


# System announcements are written only by trusted server modules, within db.tx().
# Never expose this helper as an endpoint that accepts arbitrary user-provided text.
def post_system(text: str, source: str, kind: str = "system") -> int:
    if kind not in ("system", "strawberry", "command"):
        raise ValueError("Unknown system message kind")
    system_id = db.val("SELECT id FROM users WHERE username=?", (auth.SYSTEM_USERNAME,))
    if not system_id:
        raise RuntimeError("System user does not exist")
    request_id = "sys:" + source
    if len(request_id) > 128:
        raise ValueError("System event id too long")
    existing = db.val("SELECT id FROM chat_messages WHERE user_id=? AND request_id=?", (system_id, request_id))
    if existing:
        return existing
    now = int(time.time())
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mid = db.x("""INSERT INTO chat_messages(user_id,request_id,text,created_ts,created_at,system_kind)
               VALUES(?,?,?,?,?,?)""", (system_id,request_id,text,now,stamp,kind))
    db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'sent',?)", (mid,now))
    return mid
