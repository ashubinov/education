"""Strawberry Thief: transactionally escrowed, private until resolved, timeout = thief wins.

All balances in slot_accounts.balance are spendable. Stolen chips and the 5% thief
collateral live only in pending strawberry_thefts and therefore cannot be spent.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, chat, social, slots
from .db import db

router = APIRouter(prefix="/api/minigames/strawberry", tags=["Мини-игры · Клубничный вор"])
MIN_STEAL = 10
MAX_STEAL = 100
STEAL_COOLDOWN = 60  # защита от спама: не более одной новой кражи в минуту
ANSWER_SECONDS = 24 * 3600
MAX_BALANCE = 9_000_000_000_000_000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TheftIn(Strict):
    victim_id: int = Field(ge=1)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


class GuessIn(Strict):
    theft_id: int = Field(ge=1)
    suspect_id: int = Field(ge=1)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS strawberry_thefts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thief_id INTEGER NOT NULL,
            victim_id INTEGER NOT NULL,
            thief_username TEXT NOT NULL,
            victim_username TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            fee INTEGER NOT NULL CHECK(fee > 0),
            candidates_json TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            expires_ts INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','caught','escaped','cancelled')),
            guessed_id INTEGER,
            guess_request_id TEXT,
            resolved_ts INTEGER,
            UNIQUE(thief_id, request_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_strawberry_pending_victim
          ON strawberry_thefts(victim_id) WHERE status='pending';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_strawberry_pending_thief
          ON strawberry_thefts(thief_id) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS ix_strawberry_expiry ON strawberry_thefts(status,expires_ts);
        CREATE INDEX IF NOT EXISTS ix_strawberry_thief_recent
          ON strawberry_thefts(thief_id,created_ts DESC);
        """)


def _user(request: Request) -> dict:
    return auth.require_user(request)


def _friends(uid: int) -> list[dict]:
    users = db.q("""SELECT u.id,u.username,u.display_name,u.avatar
      FROM friendships f JOIN users u ON u.id=CASE WHEN f.requester_id=?
        THEN f.addressee_id ELSE f.requester_id END
      WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted'
        AND COALESCE(u.banned,0)=0 AND u.username<>?
      ORDER BY lower(COALESCE(NULLIF(u.display_name,''),u.username)),u.id""",
      (uid,uid,uid,auth.SYSTEM_USERNAME))
    return [{"id":x["id"],"username":x["username"],"name":x["display_name"] or x["username"],
             "avatar":x["avatar"] or "👤"} for x in users]


def _credit(uid: int, amount: int):
    if amount <= 0:
        return
    current = slots.ensure_account(uid)
    if current["balance"] > MAX_BALANCE - amount:
        raise HTTPException(409, "Превышен предел баланса")
    db.x("UPDATE slot_accounts SET balance=balance+?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount,uid))


def _finish(row: dict, outcome: str, now: int, guessed_id=None, guess_req=None):
    """Called inside db.tx only. Escrow is disbursed exactly once."""
    if row["status"] != "pending":
        return
    thief = db.one("SELECT id FROM users WHERE id=?", (row["thief_id"],))
    victim = db.one("SELECT id FROM users WHERE id=?", (row["victim_id"],))
    if outcome == "caught":
        if not victim:
            raise HTTPException(409, "Получатель недоступен")
        _credit(row["victim_id"], row["amount"] + row["fee"])
        headline = f"{row['thief_username'].upper()} СЛОВИЛ КРЕМПАЙ!!!!!"
    elif outcome == "escaped":
        if not thief:
            raise HTTPException(409, "Получатель недоступен")
        _credit(row["thief_id"], row["amount"] + row["fee"])
        headline = f"{row['victim_username'].upper()} ОТКЛУБНИЧЕН ПО ПОЛНОЙ!!!!!"
    elif outcome == "cancelled":
        # The deleted party can no longer claim coins. Return escrow to survivor.
        if thief and not victim:
            _credit(row["thief_id"], row["amount"] + row["fee"])
        elif victim and not thief:
            _credit(row["victim_id"], row["amount"] + row["fee"])
        headline = None
    else:
        raise ValueError("Недопустимый исход")
    db.x("""UPDATE strawberry_thefts SET status=?,guessed_id=?,guess_request_id=?,resolved_ts=?
        WHERE id=? AND status='pending'""", (outcome,guessed_id,guess_req,now,row["id"]))
    if headline:
        chat.post_system(headline, f"strawberry:{row['id']}", kind="strawberry")


def expire_thefts(now: int | None = None) -> int:
    """Called in background and on API requests; timeout grants chips to thief."""
    now = int(time.time()) if now is None else now
    with db.tx():
        rows = db.q("SELECT * FROM strawberry_thefts WHERE status='pending' AND expires_ts<=? ORDER BY id", (now,))
        for row in rows:
            _finish(row, "escaped", now)
    return len(rows)


async def expiry_loop():
    """Multiple workers are safe: all settlement is serialized by db.tx()."""
    while True:
        try:
            await run_in_threadpool(expire_thefts)
        except Exception as exc:
            print("[strawberry] expiry error:", repr(exc))
        await asyncio.sleep(30)


def _cooldown(uid: int, now: int) -> int:
    last = db.val("SELECT MAX(created_ts) FROM strawberry_thefts WHERE thief_id=?", (uid,))
    return max(0, STEAL_COOLDOWN - (now - int(last))) if last is not None else 0


def _pending(uid: int) -> list[dict]:
    rows = db.q("""SELECT id,amount,fee,candidates_json,created_at,expires_ts
        FROM strawberry_thefts WHERE victim_id=? AND status='pending' ORDER BY id DESC""", (uid,))
    return [{"id":r["id"],"amount":r["amount"],"fee":r["fee"],
             "created_at":r["created_at"],"expires_ts":r["expires_ts"],
             "candidates":json.loads(r["candidates_json"])} for r in rows]


def status(uid: int) -> dict:
    expire_thefts()
    rows = db.q("SELECT id FROM strawberry_thefts WHERE victim_id=? AND status='pending' ORDER BY id DESC", (uid,))
    return {"pending_count":len(rows),"latest_pending_id":rows[0]["id"] if rows else None}


def overview(uid: int) -> dict:
    expire_thefts()
    now = int(time.time())
    acc = slots.ensure_account(uid)
    held = db.val("SELECT COALESCE(SUM(amount),0) FROM strawberry_thefts WHERE thief_id=? AND status='pending'", (uid,), 0)
    reserve = db.val("SELECT COALESCE(SUM(fee),0) FROM strawberry_thefts WHERE thief_id=? AND status='pending'", (uid,), 0)
    return {"friends":_friends(uid),"pending":_pending(uid),"balance":acc["balance"],
            "frozen":held,"collateral":reserve,"has_active_theft":bool(held),
            "cooldown_remaining":_cooldown(uid,now),"cooldown_seconds":STEAL_COOLDOWN,
            "min_steal":MIN_STEAL,"max_steal":MAX_STEAL,"answer_seconds":ANSWER_SECONDS,
            "rules":"Украденные жетоны заморожены до ответа или истечения 24 часов. "
                    "Если жертва угадает — ей вернут сумму и 5% компенсации из заранее зарезервированных средств вора. "
                    "Если не угадает или не ответит за 24 часа — замороженные жетоны достанутся вору."}


def steal(thief_id: int, victim_id: int, request_id: str) -> dict:
    if thief_id == victim_id:
        raise HTTPException(400, "Нельзя отклубничить самого себя")
    expire_thefts()
    with db.tx():
        old = db.one("SELECT * FROM strawberry_thefts WHERE thief_id=? AND request_id=?", (thief_id,request_id))
        if old:
            if old["victim_id"] != victim_id:
                raise HTTPException(409, "Один request_id нельзя использовать для разных целей")
            return {"id":old["id"],"amount":old["amount"],"reserved_fee":old["fee"],"frozen":old["status"]=="pending",
                    "replayed":True,"balance":slots.ensure_account(thief_id)["balance"]}
        thief = db.one("SELECT id,username,banned FROM users WHERE id=?", (thief_id,))
        if not thief or thief["banned"]:
            raise HTTPException(403, "Аккаунт недоступен")
        victim = social.require_friend(thief_id,victim_id)
        candidates = _friends(victim_id)
        if not any(x["id"]==thief_id for x in candidates):
            raise HTTPException(409, "Дружба изменилась")
        if db.one("SELECT id FROM strawberry_thefts WHERE status='pending' AND thief_id=?", (thief_id,)):
            raise HTTPException(409, "Сначала дождись завершения текущей кражи")
        if db.one("SELECT id FROM strawberry_thefts WHERE status='pending' AND victim_id=?", (victim_id,)):
            raise HTTPException(409, "Друг уже разгадывает другую кражу")
        now = int(time.time())
        cd = _cooldown(thief_id,now)
        if cd:
            raise HTTPException(429, f"Следующая кража через {cd} сек.")
        ta,va = slots.ensure_account(thief_id),slots.ensure_account(victim_id)
        if va["balance"] <= 0:
            raise HTTPException(400, "У друга нет доступных жетонов")
        if ta["balance"] < 1:
            raise HTTPException(400, "Нужен минимум 1 свой жетон для резерва компенсации")
        upper = min(MAX_STEAL,va["balance"],ta["balance"]*20)
        lower = min(MIN_STEAL,upper)
        amount = secrets.randbelow(upper-lower+1)+lower
        fee = (amount+19)//20  # 5% вверх до целого жетона
        if fee > ta["balance"]:
            raise HTTPException(400, "Не хватает жетонов для резерва компенсации")
        # Spendable balances drop immediately. The escrow value is recorded only
        # in pending theft; no API can spend it before resolution.
        db.x("UPDATE slot_accounts SET balance=balance-?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount,victim_id))
        db.x("UPDATE slot_accounts SET balance=balance-?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (fee,thief_id))
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rid = db.x("""INSERT INTO strawberry_thefts(thief_id,victim_id,thief_username,victim_username,
            amount,fee,candidates_json,request_id,created_ts,expires_ts,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (thief_id,victim_id,thief["username"],victim["username"],amount,fee,
             json.dumps(candidates,ensure_ascii=False),request_id,now,now+ANSWER_SECONDS,stamp))
        return {"id":rid,"amount":amount,"frozen":True,"reserved_fee":fee,
                "replayed":False,"balance":ta["balance"]-fee}


def _view(row: dict, replayed: bool):
    return {"theft_id":row["id"],"caught":row["status"]=="caught",
            "amount":row["amount"],"compensation":row["fee"] if row["status"]=="caught" else 0,
            "status":row["status"],"replayed":replayed}


def guess(victim_id: int, theft_id: int, suspect_id: int, request_id: str) -> dict:
    # Atomic expiry and guess decision: a late answer never beats the deadline.
    with db.tx():
        row = db.one("SELECT * FROM strawberry_thefts WHERE id=? AND victim_id=?", (theft_id,victim_id))
        if not row:
            raise HTTPException(404, "Кража не найдена")
        if row["status"] != "pending":
            if row["guess_request_id"]==request_id and row["guessed_id"]==suspect_id:
                return _view(row,True)
            raise HTTPException(409, "Эта кража уже завершена")
        now = int(time.time())
        if now >= row["expires_ts"]:
            _finish(row,"escaped",now)
            row = db.one("SELECT * FROM strawberry_thefts WHERE id=?", (theft_id,))
            return {**_view(row,False), "timed_out":True}
        ids = {u["id"] for u in json.loads(row["candidates_json"])}
        if suspect_id not in ids:
            raise HTTPException(422, "Выбери подозреваемого из списка")
        if not db.one("SELECT id FROM users WHERE id=?", (row["thief_id"],)):
            raise HTTPException(409,"Аккаунт вора был удалён")
        outcome = "caught" if suspect_id==row["thief_id"] else "escaped"
        _finish(row,outcome,now,suspect_id,request_id)
        row = db.one("SELECT * FROM strawberry_thefts WHERE id=?", (theft_id,))
        return _view(row,False)


def on_user_delete(uid: int):
    """Call before slots.delete_user_slot_data(uid), for both parties' escrow."""
    with db.tx():
        now = int(time.time())
        rows = db.q("SELECT * FROM strawberry_thefts WHERE status='pending' AND (victim_id=? OR thief_id=?)", (uid,uid))
        for row in rows:
            if row["thief_id"]==uid:
                # Future deletion of thief: stolen principal goes back to victim.
                _credit(row["victim_id"],row["amount"]+row["fee"])
            else:
                _credit(row["thief_id"],row["amount"]+row["fee"])
            db.x("UPDATE strawberry_thefts SET status='cancelled',resolved_ts=? WHERE id=?", (now,row["id"]))
        # Keep records without holding data about a deleted user.
        db.x("DELETE FROM strawberry_thefts WHERE thief_id=? OR victim_id=?", (uid,uid))


@router.get("/status")
async def get_status(request: Request):
    return await run_in_threadpool(status,_user(request)["id"])


@router.get("/state")
async def get_state(request: Request):
    return await run_in_threadpool(overview,_user(request)["id"])


@router.post("/steal")
async def post_steal(request: Request, body: TheftIn):
    return await run_in_threadpool(steal,_user(request)["id"],body.victim_id,body.request_id)


@router.post("/guess")
async def post_guess(request: Request, body: GuessIn):
    return await run_in_threadpool(guess,_user(request)["id"],body.theft_id,body.suspect_id,body.request_id)
