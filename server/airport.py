"""Аэропорт (шуточная локация фермы): единственная авиакомпания — «Клубничные авиалинии», рейсы которой вечно отменяются и переносятся. Правила — в airport_data.py.

Открывается на уровне фермы 8. Билет стоит монеты фермы; весь путь рейса (отмены, переносы, неудобный полёт, компенсация) бросается один раз при покупке
и лежит в базе, а игрок видит только то, что «уже объявили». Время идёт по меткам (ready_at), фоновых задач нет. Каждое действие — одна транзакция.
"""
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import auth, farm
from .airport_data import AIRLINE, REFUND, TICKET_PRICE, UNLOCK_FARM_LEVEL, SPEED, board, next_rank, rank_of, roll_trip
from .db import db
from .loc_common import need_open

router = APIRouter(prefix="/api/airport", tags=["Аэропорт"])
_rng = secrets.SystemRandom()


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS airport_trips(
          user_id INTEGER PRIMARY KEY, flight TEXT NOT NULL, dest TEXT NOT NULL, icon TEXT NOT NULL, started_at REAL NOT NULL, ready_at REAL NOT NULL,
          steps TEXT NOT NULL, cancels INTEGER NOT NULL, reward INTEGER NOT NULL, comp_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS airport_stats(
          user_id INTEGER PRIMARY KEY, trips INTEGER NOT NULL DEFAULT 0, cancels INTEGER NOT NULL DEFAULT 0, seconds INTEGER NOT NULL DEFAULT 0,
          spent INTEGER NOT NULL DEFAULT 0, comp INTEGER NOT NULL DEFAULT 0
        );
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("airport_trips", "airport_stats"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


def ready_count(user_id: int) -> int:
    """Сколько прилетело и ждёт компенсацию (для значка на карте мира)."""
    return db.val("SELECT COUNT(*) FROM airport_trips WHERE user_id=? AND ready_at<=?", (user_id, time.time()), 0) or 0


def _stats(user_id: int) -> dict:
    return db.one("SELECT * FROM airport_stats WHERE user_id=?", (user_id,)) or {"trips": 0, "cancels": 0, "seconds": 0, "spent": 0, "comp": 0}


def _trip_view(t: dict, now: float) -> dict:
    elapsed = now - t["started_at"]
    total = t["ready_at"] - t["started_at"]
    steps = json.loads(t["steps"])
    nxt = next((s for s in steps if s["at"] > elapsed), None)
    log = [s for s in steps if s["at"] <= elapsed]
    landed = elapsed >= total
    return {"flight": log[-1]["flight"], "dest": t["dest"], "icon": t["icon"], "state": "landed" if landed else "waiting",
            "left": round(max(0.0, total - elapsed)), "total": round(total), "next_in": round(nxt["at"] - elapsed) if nxt else 0,
            "log": log, "reward": t["reward"] if landed else None, "comp_text": t["comp_text"] if landed else None}


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = farm._farm(user_id)
        lvl = farm._level(f)
    unlocked = lvl >= UNLOCK_FARM_LEVEL
    t = db.one("SELECT * FROM airport_trips WHERE user_id=?", (user_id,))
    st = _stats(user_id)
    nxt = next_rank(st["cancels"])
    return {"unlocked": unlocked, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "airline": AIRLINE, "ticket_price": TICKET_PRICE,
            "refund": int(TICKET_PRICE * REFUND), "trip": _trip_view(t, now) if t and unlocked else None, "board": board(now), "now": round(now),
            "stats": {"trips": st["trips"], "cancels": st["cancels"], "hours": round(st["seconds"] * SPEED / 3600, 1), "spent": st["spent"], "comp": st["comp"],
                      "rank": rank_of(st["cancels"]), "next_rank": nxt[1] if nxt else None, "next_need": nxt[0] if nxt else None},
            "events": events or []}


# ----------------------------------------------------------------------------- действия
def buy_ticket(user_id: int) -> dict:
    now = time.time()
    with db.tx():
        f = need_open(user_id, UNLOCK_FARM_LEVEL, "Аэропорт")
        if db.one("SELECT 1 FROM airport_trips WHERE user_id=?", (user_id,)):
            raise HTTPException(400, "У тебя уже есть билет: сначала долети (или вернись) на прошлом рейсе")
        farm._pay(user_id, f, TICKET_PRICE)
        tr = roll_trip(_rng)
        db.x("INSERT INTO airport_trips(user_id, flight, dest, icon, started_at, ready_at, steps, cancels, reward, comp_text) VALUES(?,?,?,?,?,?,?,?,?,?)",
             (user_id, tr["flight"], tr["dest"], tr["icon"], now, now + tr["total"], json.dumps(tr["steps"], ensure_ascii=False), tr["cancels"], tr["reward"], tr["comp_text"]))
        db.x("INSERT INTO airport_stats(user_id, spent) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET spent=spent+?", (user_id, TICKET_PRICE, TICKET_PRICE))
    return get_state(user_id, [{"type": "ticket", "dest": tr["dest"], "icon": tr["icon"]}])


def claim(user_id: int) -> dict:
    now = time.time()
    with db.tx():
        need_open(user_id, UNLOCK_FARM_LEVEL, "Аэропорт")
        t = db.one("SELECT * FROM airport_trips WHERE user_id=?", (user_id,))
        if not t:
            raise HTTPException(400, "У тебя нет билета")
        if now < t["ready_at"]:
            raise HTTPException(400, "Рейс ещё не прилетел. Если он вообще вылетит")
        db.x("DELETE FROM airport_trips WHERE user_id=?", (user_id,))
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (t["reward"], user_id))
        secs = round(t["ready_at"] - t["started_at"])
        db.x("INSERT INTO airport_stats(user_id, trips, cancels, seconds, comp) VALUES(?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET trips=trips+1, cancels=cancels+?, seconds=seconds+?, comp=comp+?",
             (user_id, t["cancels"], secs, t["reward"], t["cancels"], secs, t["reward"]))
    return get_state(user_id, [{"type": "landed", "dest": t["dest"], "reward": t["reward"], "text": t["comp_text"], "cancels": t["cancels"]}])


def refund(user_id: int) -> dict:
    """Вернуть билет, пока рейс не прилетел: возвращается десятая часть цены (комиссия за возврат — остальное)."""
    now = time.time()
    back = int(TICKET_PRICE * REFUND)
    with db.tx():
        need_open(user_id, UNLOCK_FARM_LEVEL, "Аэропорт")
        t = db.one("SELECT * FROM airport_trips WHERE user_id=?", (user_id,))
        if not t:
            raise HTTPException(400, "У тебя нет билета")
        if now >= t["ready_at"]:
            raise HTTPException(400, "Рейс уже прилетел: забери компенсацию")
        elapsed = now - t["started_at"]
        seen = sum(1 for s in json.loads(t["steps"]) if s["at"] <= elapsed and s["kind"] == "cancel")
        db.x("DELETE FROM airport_trips WHERE user_id=?", (user_id,))
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (back, user_id))
        db.x("UPDATE airport_stats SET cancels=cancels+?, seconds=seconds+? WHERE user_id=?", (seen, round(elapsed), user_id))
    return get_state(user_id, [{"type": "refund", "coins": back}])


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/ticket")
async def api_ticket(request: Request):
    return await run_in_threadpool(buy_ticket, _me(request)["id"])


@router.post("/claim")
async def api_claim(request: Request):
    return await run_in_threadpool(claim, _me(request)["id"])


@router.post("/refund")
async def api_refund(request: Request):
    return await run_in_threadpool(refund, _me(request)["id"])
