"""Команды общего чата: только одобренные серверные действия, без исполнения произвольных API.

Команды создаёт администратор. Действие MVP: начисление виртуальных слот-жетонов.
Ограничение частоты персонально для каждой пары (пользователь, команда).
Вся операция (проверка таймаута, жетоны, аудит, сообщение в чат) атомарна.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, config, slots
from .db import db

router = APIRouter(prefix="/api/chat/commands", tags=["Команды общего чата"])

MAX_AMOUNT = 10000  # ограничение ОДНОЙ активации; администратор выбирает фактический лимит
MAX_COOLDOWN = 30 * 24 * 3600
MAX_COMMANDS = 100
COMMAND_PATTERN = re.compile(r"^/[a-zа-яё0-9_-]{2,32}$", re.IGNORECASE)
UNITS = {"seconds": 1, "minutes": 60, "hours": 3600}


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CommandCreateIn(StrictBody):
    command: str = Field(min_length=3, max_length=33)
    description: str = Field(min_length=3, max_length=200)
    action: Literal["add_chips"] = "add_chips"
    amount: int = Field(ge=1, le=MAX_AMOUNT)
    cooldown_value: int = Field(ge=1, le=MAX_COOLDOWN)
    cooldown_unit: Literal["seconds", "minutes", "hours"]


class CommandUseIn(StrictBody):
    command: str = Field(min_length=3, max_length=33)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,64}$")


def init_schema():
    """Устанавливается после chat.init_schema() при старте FastAPI."""
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('add_chips')),
            amount INTEGER NOT NULL CHECK(amount BETWEEN 1 AND 10000),
            cooldown_seconds INTEGER NOT NULL CHECK(cooldown_seconds BETWEEN 1 AND 2592000),
            cooldown_value INTEGER NOT NULL,
            cooldown_unit TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_command_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            used_ts INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            message_id INTEGER,
            UNIQUE(user_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS ix_chat_command_cooldown
            ON chat_command_uses(user_id, command_id, used_ts DESC);
        CREATE TRIGGER IF NOT EXISTS tr_chat_command_user_cleanup
        BEFORE DELETE ON users
        BEGIN
            DELETE FROM chat_command_uses WHERE user_id=OLD.id;
        END;
        """)


def _user(request: Request):
    if config.env("CHAT_ENABLED", "1").lower() in ("0", "false", "no"):
        raise HTTPException(503, "Общий чат отключён")
    return auth.require_user(request)


def _admin(request: Request):
    user = _user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Создавать и управлять командами может только администратор")
    return user


def _normalize(name: str) -> str:
    name = name.strip().casefold()
    if not COMMAND_PATTERN.fullmatch(name):
        raise HTTPException(422, "Команда должна начинаться с / и содержать 2–32 буквы, цифры, - или _ без пробелов")
    return name


def _remaining(user_id: int, command_id: int, cooldown: int, now: int) -> int:
    used = db.val("SELECT MAX(used_ts) FROM chat_command_uses WHERE user_id=? AND command_id=?",
                  (user_id, command_id))
    return max(0, cooldown - (now - int(used))) if used is not None else 0


def list_commands(user_id: int, admin: bool):
    now = int(time.time())
    where = "" if admin else "WHERE enabled=1"
    rows = db.q(f"SELECT * FROM chat_commands {where} ORDER BY enabled DESC, id DESC LIMIT ?", (MAX_COMMANDS,))
    return {"commands": [
        {"id": r["id"], "command": r["command"], "description": r["description"],
         "action": r["action"], "amount": r["amount"],
         "cooldown_seconds": r["cooldown_seconds"], "cooldown_value": r["cooldown_value"],
         "cooldown_unit": r["cooldown_unit"], "remaining_seconds": _remaining(user_id, r["id"], r["cooldown_seconds"], now),
         "enabled": bool(r["enabled"])}
        for r in rows], "is_admin": bool(admin), "server_ts": now}


def create(user_id: int, body: CommandCreateIn):
    name = _normalize(body.command)
    seconds = body.cooldown_value * UNITS[body.cooldown_unit]
    if seconds > MAX_COOLDOWN:
        raise HTTPException(422, "Максимальный таймаут — 30 суток")
    desc = body.description.strip()
    if len(desc) < 3:
        raise HTTPException(422, "Добавь описание действия")
    with db.tx():
        if db.val("SELECT COUNT(*) FROM chat_commands", default=0) >= MAX_COMMANDS:
            raise HTTPException(400, "Достигнут лимит в 100 команд")
        if db.one("SELECT id FROM chat_commands WHERE command=? COLLATE NOCASE", (name,)):
            raise HTTPException(409, "Команда с таким текстом уже существует")
        cid = db.x("""INSERT INTO chat_commands(command, description, action, amount,
                   cooldown_seconds, cooldown_value, cooldown_unit, created_by)
                   VALUES(?,?,?,?,?,?,?,?)""",
                   (name, desc, body.action, body.amount, seconds, body.cooldown_value, body.cooldown_unit, user_id))
    return {"id": cid, "command": name, "ok": True}


def set_enabled(cid: int, enabled: bool):
    with db.tx():
        cmd = db.one("SELECT id FROM chat_commands WHERE id=?", (cid,))
        if not cmd:
            raise HTTPException(404, "Команда не найдена")
        db.x("UPDATE chat_commands SET enabled=? WHERE id=?", (int(enabled), cid))
    return {"id": cid, "enabled": enabled}


def _use_view(r: dict, *, replayed: bool):
    return {"command": r["command"], "chips": r["amount"], "balance": r["balance_after"],
            "message_id": r["message_id"], "replayed": replayed,
            "cooldown_seconds": r["cooldown_seconds"]}


def use(user_id: int, name: str, request_id: str):
    name = _normalize(name)
    now = int(time.time())
    with db.tx():
        # Идемпотентность проверяется ДО проверки кулдауна/активности команды.
        # Потерянный ответ всегда можно получить повторно с тем же request_id.
        old = db.one("""SELECT u.*,c.command,c.cooldown_seconds FROM chat_command_uses u
                        JOIN chat_commands c ON c.id=u.command_id
                        WHERE u.user_id=? AND u.request_id=?""", (user_id, request_id))
        if old:
            if old["command"] != name:
                raise HTTPException(409, "Один request_id нельзя использовать для разных команд")
            return _use_view(old, replayed=True)

        u = db.one("SELECT id,banned FROM users WHERE id=?", (user_id,))
        if not u or u["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        cmd = db.one("SELECT * FROM chat_commands WHERE command=? COLLATE NOCASE", (name,))
        if not cmd or not cmd["enabled"]:
            raise HTTPException(404, "Такой активной команды нет")
        remaining = _remaining(user_id, cmd["id"], cmd["cooldown_seconds"], now)
        if remaining:
            raise HTTPException(429, f"Команда будет доступна через {remaining} сек.")
        if cmd["action"] != "add_chips":
            raise HTTPException(422, "Неизвестное действие команды")
        # Модуль слотов использует ту же SQLite-базу и тот же транзакционный DB API.
        account = slots.ensure_account(user_id)
        amount = cmd["amount"]
        if account["balance"] > 9_000_000_000_000_000 - amount:
            raise HTTPException(400, "Превышен предел игрового баланса")
        balance = account["balance"] + amount
        db.x("UPDATE slot_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
             (amount, user_id))
        # Видимое для всех сообщение о срабатывании команды; не скрытая накрутка.
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = f"🎁 {name} → +{amount} жетонов"
        mid = db.x("""INSERT INTO chat_messages(user_id,request_id,text,created_ts,created_at)
                       VALUES(?,?,?,?,?)""", (user_id, "cmd:" + request_id, text, now, stamp))
        db.x("UPDATE chat_messages SET system_kind='command' WHERE id=?", (mid,))
        db.x("INSERT INTO chat_events(message_id,kind,created_ts) VALUES(?,'sent',?)", (mid, now))
        db.x("""INSERT INTO chat_command_uses(command_id,user_id,request_id,used_ts,amount,balance_after,message_id)
                 VALUES(?,?,?,?,?,?,?)""", (cmd["id"], user_id, request_id, now, amount, balance, mid))
    return {"command": name, "chips": amount, "balance": balance,
            "message_id": mid, "replayed": False, "cooldown_seconds": cmd["cooldown_seconds"]}


@router.get("")
async def get_commands(request: Request):
    user = _user(request)
    return await run_in_threadpool(list_commands, user["id"], bool(user["is_admin"]))


@router.post("")
async def create_command(request: Request, body: CommandCreateIn):
    user = _admin(request)
    return await run_in_threadpool(create, user["id"], body)


@router.post("/use")
async def use_command(request: Request, body: CommandUseIn):
    user = _user(request)
    return await run_in_threadpool(use, user["id"], body.command, body.request_id)


@router.post("/{command_id}/enabled")
async def enable_command(command_id: int, request: Request, enabled: bool):
    _admin(request)
    return await run_in_threadpool(set_enabled, command_id, enabled)
