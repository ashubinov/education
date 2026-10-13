"""ОТПЕТУНИВАНИЕ — мини-игра по образцу «Клубничного вора», но без жетонов: ставка — минута «домашнего ареста».

Игрок выбирает друга и нажимает «Отпетунить». Друг получает уведомление и один раз выбирает из своих друзей, кто это сделал.
• Угадал — блокировки нет (а если нападающий ещё и «украл» жетоны, они возвращаются жертве, плюс штраф вору).
• Не угадал (или не ответил за ANSWER_SECONDS) — на LOCK_SECONDS «отпетунен»: ему доступны только курсы и выход из аккаунта,
  а украденные жетоны достаются нападающему. Если друг не отвечал и был не в сети, минута начинается, когда он в следующий раз откроет сайт.
• Воровство жетонов — как в «Клубничном воре» и по желанию (amount=0 — просто отпетунить): от 1 до MAX_STEAL_PERCENT % свободного баланса друга;
  сумма замораживается до ответа; если друг угадал, вор платит штраф CATCH_PENALTY_PERCENT % (не больше своего баланса).

Ограничения (как у вора, плюс «раз в 5 минут»):
• у одной жертвы не больше одной неразгаданной атаки; а нападающий может отпетунить сразу нескольких (не дожидаясь ответа предыдущего);
• одного и того же игрока можно отпетунить не чаще, чем раз в VICTIM_COOLDOWN секунд (кем бы ни был нападающий);
• отпетуненного (идёт блокировка) отпетунить нельзя;

Блокировка действует на сервере: пока она активна, `auth.require_user` пропускает только курсы, занятия, профиль (GET /api/me) и выход.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, chat, minigames, slots, social
from .db import db

router = APIRouter(prefix="/api/minigames/petun", tags=["Мини-игры · Отпетунивание"])
LOCK_SECONDS = 60          # на сколько отпетунен проигравший
ANSWER_SECONDS = 3600      # сколько времени у друга, чтобы угадать (потом считается «не угадал»)
VICTIM_COOLDOWN = 5 * 60   # одного и того же игрока можно отпетунить не чаще, чем раз в 5 минут
MIN_STEAL = minigames.MIN_STEAL                          # воровство жетонов — как в «Клубничном воре»
MAX_STEAL_PERCENT = minigames.MAX_STEAL_PERCENT
CATCH_PENALTY_PERCENT = minigames.CATCH_PENALTY_PERCENT


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttackIn(Strict):
    victim_id: int = Field(ge=1)
    amount: int = Field(default=0, ge=0, le=9_000_000_000_000_000, strict=True)   # 0 — просто отпетунить, без воровства жетонов
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


class GuessIn(Strict):
    attack_id: int = Field(ge=1)
    suspect_id: int = Field(ge=1)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS petun_attacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thief_id INTEGER NOT NULL,
            victim_id INTEGER NOT NULL,
            thief_username TEXT NOT NULL,
            victim_username TEXT NOT NULL,
            candidates_json TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            expires_ts INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','caught','escaped','cancelled')),
            guessed_id INTEGER,
            guess_request_id TEXT,
            resolved_ts INTEGER,
            UNIQUE(thief_id, request_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_petun_pending_victim ON petun_attacks(victim_id) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS ix_petun_victim_recent ON petun_attacks(victim_id, created_ts DESC);
        CREATE INDEX IF NOT EXISTS ix_petun_expiry ON petun_attacks(status, expires_ts);
        CREATE TABLE IF NOT EXISTS petun_locks (
            user_id INTEGER PRIMARY KEY,
            attack_id INTEGER,
            state TEXT NOT NULL CHECK(state IN ('armed','active')),
            until_ts INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL
        );
        """)
        db.conn.execute("DROP INDEX IF EXISTS ux_petun_pending_thief")   # раньше нападающий мог иметь только одну неразгаданную атаку
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(petun_attacks)")}
        for col in ("amount", "fee"):   # украденные жетоны и штраф пойманному вору (добавлено после первой версии игры)
            if col not in cols:
                db.conn.execute(f"ALTER TABLE petun_attacks ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")


def _user(request: Request) -> dict:
    return auth.require_user(request)


# ----------------------------------------------------------------------------- блокировка
def is_locked(uid: int, now: int | None = None) -> bool:
    """Идёт ли минута «отпетунивания» (только чтение; вызывается на каждый запрос к API)."""
    now = int(time.time()) if now is None else now
    try:
        return bool(db.val("SELECT 1 FROM petun_locks WHERE user_id=? AND state='active' AND until_ts>?", (uid, now), 0))
    except Exception:  # таблицы ещё нет (например, тест отдельного модуля)
        return False


def _lock_view(row: dict, now: int) -> dict:
    return {"until": row["until_ts"], "seconds_left": max(0, row["until_ts"] - now), "server_now": now,
            "total": LOCK_SECONDS}


def lock_status(uid: int) -> dict | None:
    """Состояние блокировки для клиента. «Взведённая» (жертва не отвечала и была не в сети) запускается здесь —
    при первом открытии сайта. Просроченная запись удаляется."""
    now = int(time.time())
    with db.tx():
        row = db.one("SELECT * FROM petun_locks WHERE user_id=?", (uid,))
        if not row:
            return None
        if row["state"] == "armed":
            db.x("UPDATE petun_locks SET state='active', until_ts=? WHERE user_id=?", (now + LOCK_SECONDS, uid))
            row = db.one("SELECT * FROM petun_locks WHERE user_id=?", (uid,))
        if row["until_ts"] <= now:
            db.x("DELETE FROM petun_locks WHERE user_id=?", (uid,))
            return None
        return _lock_view(row, now)


def _punish(victim_id: int, attack_id: int, now: int, serve_now: bool):
    """Включить блокировку жертве (внутри db.tx). serve_now=False — «взвести», минута пойдёт при следующем входе."""
    old = db.one("SELECT * FROM petun_locks WHERE user_id=?", (victim_id,))
    if old and old["state"] == "active" and old["until_ts"] > now:
        return  # уже отпетунен: не продлеваем
    db.x("""INSERT INTO petun_locks(user_id,attack_id,state,until_ts,created_ts) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET attack_id=excluded.attack_id,state=excluded.state,
                until_ts=excluded.until_ts,created_ts=excluded.created_ts""",
         (victim_id, attack_id, "active" if serve_now else "armed", now + LOCK_SECONDS if serve_now else 0, now))


# ----------------------------------------------------------------------------- атаки
def _finish(row: dict, outcome: str, now: int, guessed_id=None, guess_req=None, victim_online: bool = False):
    """Внутри db.tx. caught — жертва угадала; escaped — не угадала или не ответила (тогда она «отпетунена»)."""
    if row["status"] != "pending":
        return
    thief = db.one("SELECT id,username,display_name FROM users WHERE id=?", (row["thief_id"],))
    victim = db.one("SELECT id,username,display_name FROM users WHERE id=?", (row["victim_id"],))
    amount, penalty = row["amount"], 0
    if outcome == "escaped" and victim:
        _punish(row["victim_id"], row["id"], now, serve_now=victim_online)
    # Жетоны под замком (как у клубничного вора): угадал — вернуть жертве и взять штраф с вора; не угадал — добыча вору.
    if amount:
        if outcome == "caught":
            if victim:
                minigames._credit(row["victim_id"], amount)
            if thief and victim:
                penalty = minigames._catch_penalty({"amount": amount, "thief_id": row["thief_id"]})
                if penalty:
                    db.x("UPDATE slot_accounts SET balance=balance-?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (penalty, row["thief_id"]))
                    minigames._credit(row["victim_id"], penalty)
        elif outcome == "escaped":
            if thief:
                minigames._credit(row["thief_id"], amount)
            elif victim:
                minigames._credit(row["victim_id"], amount)
    db.x("""UPDATE petun_attacks SET status=?,guessed_id=?,guess_request_id=?,resolved_ts=?,fee=?
            WHERE id=? AND status='pending'""", (outcome, guessed_id, guess_req, now, penalty, row["id"]))
    # Объявление в общем чате (как у «Клубничного вора»): запись и очередь Telegram — в той же транзакции.
    if outcome == "caught" and thief and victim:
        text = (f"{minigames._announcement_name(thief, row['thief_username'])} СЛОВИЛ КРЕМПАЙ ОТ "
                f"{minigames._announcement_name(victim, row['victim_username'])}!!!!")
    elif outcome == "escaped" and victim:
        text = f"{minigames._announcement_name(victim, row['victim_username'])} ОТПЕТУНИН ПО ПОЛНОЙ!!!!!"
    else:
        text = None
    if text:
        chat.post_system(text, f"petun:{row['id']}", kind="strawberry")


def expire_attacks(now: int | None = None) -> int:
    """Просроченные без ответа: считаются «не угадал»; минута начнётся, когда жертва откроет сайт."""
    now = int(time.time()) if now is None else now
    with db.tx():
        rows = db.q("SELECT * FROM petun_attacks WHERE status='pending' AND expires_ts<=? ORDER BY id", (now,))
        for row in rows:
            _finish(row, "escaped", now, victim_online=False)
    return len(rows)


async def expiry_loop():
    while True:
        try:
            await run_in_threadpool(expire_attacks)
        except Exception as exc:
            print("[petun] expiry error:", repr(exc))
        await asyncio.sleep(30)


def _pending_for(uid: int) -> list[dict]:
    rows = db.q("SELECT id,thief_id,amount,created_ts,expires_ts,candidates_json FROM petun_attacks WHERE victim_id=? AND status='pending' ORDER BY id DESC", (uid,))
    return [{"id": r["id"], "amount": r["amount"], "created_ts": r["created_ts"], "expires_ts": r["expires_ts"],
             "candidates": minigames._candidate_presentation(uid, r["thief_id"], json.loads(r["candidates_json"]))} for r in rows]


def status(uid: int) -> dict:
    expire_attacks()
    rows = db.q("SELECT id,amount FROM petun_attacks WHERE victim_id=? AND status='pending' ORDER BY id DESC", (uid,))
    return {"pending_count": len(rows), "latest_pending_id": rows[0]["id"] if rows else None,
            "latest_pending_amount": rows[0]["amount"] if rows else 0}


def _wait_for(victim_id: int, now: int) -> int:
    """Через сколько секунд этого игрока снова можно отпетунить (0 — можно)."""
    last = db.val("SELECT MAX(created_ts) FROM petun_attacks WHERE victim_id=?", (victim_id,))
    return max(0, VICTIM_COOLDOWN - (now - int(last))) if last is not None else 0


def _victim_locked(victim_id: int, now: int) -> bool:
    return bool(db.val("SELECT 1 FROM petun_locks WHERE user_id=? AND (state='armed' OR until_ts>?)", (victim_id, now), 0))


def overview(uid: int) -> dict:
    expire_attacks()
    now = int(time.time())
    friends = minigames._friends(uid)
    for f in friends:
        f["wait"] = _wait_for(f["id"], now)
        f["busy"] = bool(db.val("SELECT 1 FROM petun_attacks WHERE victim_id=? AND status='pending'", (f["id"],), 0))
        f["locked"] = _victim_locked(f["id"], now)
        f["max_steal"] = slots.ensure_account(f["id"])["balance"] * MAX_STEAL_PERCENT // 100
    mine = db.q("SELECT id,victim_username,amount,expires_ts FROM petun_attacks WHERE thief_id=? AND status='pending' ORDER BY id", (uid,))
    acc = slots.ensure_account(uid)
    recent = db.q("""SELECT a.victim_username,a.status,a.amount,a.fee,a.resolved_ts,COALESCE(NULLIF(u.display_name,''),a.victim_username) AS name
                     FROM petun_attacks a LEFT JOIN users u ON u.id=a.victim_id
                     WHERE a.thief_id=? AND a.status IN ('caught','escaped') AND a.resolved_ts>=? ORDER BY a.resolved_ts DESC, a.id DESC LIMIT 5""",
                  (uid, now - 24 * 3600))
    return {"friends": friends, "pending": _pending_for(uid),
            "my_attacks": [{"id": r["id"], "victim": r["victim_username"], "amount": r["amount"], "expires_ts": r["expires_ts"]} for r in mine],
            "balance": acc["balance"], "frozen": sum(r["amount"] for r in mine),
            "min_steal": MIN_STEAL, "max_steal_percent": MAX_STEAL_PERCENT, "catch_penalty_percent": CATCH_PENALTY_PERCENT,
            "recent": [{"name": r["name"], "caught": r["status"] == "caught", "amount": r["amount"], "penalty": r["fee"], "at": r["resolved_ts"]} for r in recent],
            "lock_seconds": LOCK_SECONDS, "answer_seconds": ANSWER_SECONDS, "cooldown_seconds": VICTIM_COOLDOWN, "server_now": now}


def attack(thief_id: int, victim_id: int, request_id: str, amount: int = 0) -> dict:
    if thief_id == victim_id:
        raise HTTPException(400, "Нельзя отпетунить самого себя")
    expire_attacks()
    with db.tx():
        old = db.one("SELECT * FROM petun_attacks WHERE thief_id=? AND request_id=?", (thief_id, request_id))
        if old:
            if old["victim_id"] != victim_id or old["amount"] != amount:
                raise HTTPException(409, "Один request_id нельзя использовать для разных целей")
            return {"id": old["id"], "replayed": True, "victim": old["victim_username"], "amount": old["amount"]}
        thief = db.one("SELECT id,username,banned FROM users WHERE id=?", (thief_id,))
        if not thief or thief["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        victim = social.require_friend(thief_id, victim_id)
        candidates = minigames._friends(victim_id)
        if not any(x["id"] == thief_id for x in candidates):
            raise HTTPException(409, "Дружба изменилась")
        if db.one("SELECT id FROM petun_attacks WHERE status='pending' AND victim_id=?", (victim_id,)):
            raise HTTPException(409, "Этого друга уже отпетунили, он ещё выбирает, кто это сделал")
        now = int(time.time())
        if _victim_locked(victim_id, now):
            raise HTTPException(409, "Этот друг сейчас отпетунен: подожди, пока блокировка закончится")
        wait = _wait_for(victim_id, now)
        if wait:
            raise HTTPException(429, f"Этого друга можно отпетунить снова через {max(1, (wait + 59) // 60)} мин.")
        if amount:
            max_allowed = slots.ensure_account(victim_id)["balance"] * MAX_STEAL_PERCENT // 100
            if max_allowed < MIN_STEAL:
                raise HTTPException(400, "У друга недостаточно жетонов, чтобы их украсть: отпетунь без воровства")
            if amount < MIN_STEAL or amount > max_allowed:
                raise HTTPException(400, f"Максимальная сумма сейчас — {max_allowed} жетонов ({MAX_STEAL_PERCENT}% свободного баланса друга)")
            # как у клубничного вора: жетоны сразу уходят из свободного баланса жертвы и лежат «под замком» до ответа
            db.x("UPDATE slot_accounts SET balance=balance-?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount, victim_id))
        aid = db.x("""INSERT INTO petun_attacks(thief_id,victim_id,thief_username,victim_username,candidates_json,request_id,created_ts,expires_ts,amount)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (thief_id, victim_id, thief["username"], victim["username"], json.dumps(candidates, ensure_ascii=False),
                    request_id, now, now + ANSWER_SECONDS, amount))
        # Объявляем только жертву: вор не раскрывается, пока друг не ответит.
        chat.post_system(f"{minigames._announcement_name(victim, victim['username'])}, ТЕБЯ ПЕТУНЯТ ПРЯМО СЕЙЧАС!!!!!",
                         f"petun-start:{aid}", kind="strawberry")
        return {"id": aid, "replayed": False, "victim": victim["username"], "amount": amount}


def _view(row: dict, replayed: bool, lock: dict | None = None) -> dict:
    return {"attack_id": row["id"], "caught": row["status"] == "caught", "status": row["status"], "replayed": replayed, "lock": lock,
            "amount": row["amount"], "compensation": row["fee"] if row["status"] == "caught" else 0}


def guess(victim_id: int, attack_id: int, suspect_id: int, request_id: str) -> dict:
    with db.tx():
        row = db.one("SELECT * FROM petun_attacks WHERE id=? AND victim_id=?", (attack_id, victim_id))
        if not row:
            raise HTTPException(404, "Отпетунивание не найдено")
        now = int(time.time())
        if row["status"] != "pending":
            if row["guess_request_id"] == request_id and row["guessed_id"] == suspect_id:
                return _view(row, True, _lock_now(victim_id, now))
            raise HTTPException(409, "Это отпетунивание уже завершено")
        if now >= row["expires_ts"]:
            _finish(row, "escaped", now, victim_online=True)
            row = db.one("SELECT * FROM petun_attacks WHERE id=?", (attack_id,))
            return {**_view(row, False, _lock_now(victim_id, now)), "timed_out": True}
        ids = {u["id"] for u in json.loads(row["candidates_json"])}
        if suspect_id not in ids:
            raise HTTPException(422, "Выбери подозреваемого из списка")
        outcome = minigames._outcome_for(row, suspect_id)
        _finish(row, outcome, now, suspect_id, request_id, victim_online=True)
        row = db.one("SELECT * FROM petun_attacks WHERE id=?", (attack_id,))
        return _view(row, False, _lock_now(victim_id, now))


def _lock_now(uid: int, now: int) -> dict | None:
    row = db.one("SELECT * FROM petun_locks WHERE user_id=? AND state='active' AND until_ts>?", (uid, now))
    return _lock_view(row, now) if row else None


def on_user_delete(uid: int):
    """Перед удалением аккаунта: убрать его атаки и блокировку."""
    with db.tx():
        for row in db.q("SELECT * FROM petun_attacks WHERE status='pending' AND amount>0 AND (victim_id=? OR thief_id=?)", (uid, uid)):
            other = row["victim_id"] if row["thief_id"] == uid else row["thief_id"]   # замороженное достаётся тому, кто остаётся
            if db.one("SELECT 1 AS x FROM users WHERE id=?", (other,)):
                minigames._credit(other, row["amount"])
        db.x("DELETE FROM petun_attacks WHERE thief_id=? OR victim_id=?", (uid, uid))
        db.x("DELETE FROM petun_locks WHERE user_id=?", (uid,))


@router.get("/status")
async def get_status(request: Request):
    return await run_in_threadpool(status, _user(request)["id"])


@router.get("/state")
async def get_state(request: Request):
    return await run_in_threadpool(overview, _user(request)["id"])


@router.post("/attack")
async def post_attack(request: Request, body: AttackIn):
    return await run_in_threadpool(attack, _user(request)["id"], body.victim_id, body.request_id, body.amount)


@router.post("/guess")
async def post_guess(request: Request, body: GuessIn):
    return await run_in_threadpool(guess, _user(request)["id"], body.attack_id, body.suspect_id, body.request_id)
