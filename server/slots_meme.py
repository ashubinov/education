"""Мемный автомат LearnQuest: те же барабаны, линии, шансы и таблица выплат, что у собачьего («Лапа удачи»).

Отличаются только картинки/названия символов и отдельная история вращений. Баланс slot_accounts и статистика
всех вращений ОБЩИЕ. Сервер вычисляет всё сам (через slots.play); видео не влияет на выплаты.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, config, slots
from .db import db

router = APIRouter(prefix="/api/slots/meme", tags=["Мемные слоты"])

# Шансы и выплаты полностью совпадают с собачьим автоматом: никаких коэффициентов, подкруток и «добавочных» выигрышей.
# Игровые идентификаторы намеренно отличаются от собачьих.
REMAP = {
    "doberman": "rage", "bulldog": "hype", "pug": "shocked",
    "corgi": "smile", "chihuahua": "suspicious",
    "bone": "quiet", "collar": "facepalm", "bowl": "risky",
    "ball": "laugh", "wild": "wild", "scatter": "scatter", "bonus": "bonus",
}
NAMES = {
    "rage": "Ярость", "hype": "Эйфория", "shocked": "Шок",
    "smile": "Улыбка", "suspicious": "Подозрение", "quiet": "Молчание",
    "facepalm": "Разочарование", "risky": "Риск", "laugh": "Смех",
    "wild": "WILD", "scatter": "SCATTER", "bonus": "BONUS",
}

class SpinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bet: int = Field(ge=slots.MIN_BET, le=slots.MAX_BET)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


def init_schema() -> None:
    """Безопасно повторяется при каждом старте; старые слоты не мигрируют."""
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS meme_slot_spins (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            bet INTEGER NOT NULL,
            reels TEXT NOT NULL,
            lines TEXT NOT NULL DEFAULT '{}',
            payout INTEGER NOT NULL,
            net INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            win_tier TEXT NOT NULL DEFAULT 'none',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS ix_meme_slot_spins_user
          ON meme_slot_spins(user_id, id DESC);
        CREATE TRIGGER IF NOT EXISTS tr_meme_slots_delete_account
        AFTER DELETE ON slot_accounts
        BEGIN
          DELETE FROM meme_slot_spins WHERE user_id=OLD.user_id;
        END;
        CREATE TRIGGER IF NOT EXISTS tr_meme_slots_delete_user
        BEFORE DELETE ON users
        BEGIN
          DELETE FROM meme_slot_spins WHERE user_id=OLD.id;
        END;
        """)



def play_meme(bet: int, rng=None):
    """Тот же розыгрыш, что и у собачьего автомата (slots.play): барабанные ленты, 10 линий, выплаты и бонус одинаковые."""
    return slots.play(bet, rng)


def _map_grid(grid):
    return [[REMAP[s] for s in col] for col in grid]


def _map_details(details):
    d = dict(details)
    d["lines"] = [{**l, "symbol": REMAP[l["symbol"]]} for l in details.get("lines", [])]
    return d


def _view(row, replayed=False):
    ev = json.loads(row["lines"] or "{}")
    return {
        "id": row["id"], "machine_id": "meme", "bet": row["bet"],
        "reels": _map_grid(json.loads(row["reels"])),
        "lines": [{**l, "symbol": REMAP[l["symbol"]]} for l in ev.get("lines", [])],
        "scatter": ev.get("scatter"), "bonus": ev.get("bonus"),
        "payout": row["payout"], "net": row["net"],
        "balance": row["balance_after"], "tier": row["win_tier"],
        "created_at": row["created_at"], "replayed": replayed,
    }


def _history(user_id, limit=15):
    rows = db.q("SELECT * FROM meme_slot_spins WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit))
    return [_view(r) for r in rows]


def _stats(user_id):
    s = db.one("""SELECT COUNT(*) AS spins, COALESCE(SUM(payout),0) AS won_total,
                     COALESCE(SUM(CASE WHEN payout>bet THEN 1 ELSE 0 END),0) AS wins_count,
                     COALESCE(MAX(payout),0) AS best_win
                  FROM meme_slot_spins WHERE user_id=?""", (user_id,))
    return {"spins": s["spins"], "won_total": s["won_total"],
            "best_win": s["best_win"],
            "win_rate": round(s["wins_count"] / s["spins"], 3) if s["spins"] else 0}


def _meta():
    base = slots.meta()
    return {**base, "machine_id": "meme", "symbols": [
        {**s, "id": REMAP[s["id"]], "name": NAMES[REMAP[s["id"]]]}
        for s in base["symbols"]
    ]}


def state(user_id):
    with db.tx():
        acc = slots.ensure_account(user_id)
        return {
            "machine_id": "meme", "balance": acc["balance"],
            "exchange": slots._exchange(acc),
            "bets": list(slots.BETS), "min_bet": slots.MIN_BET,
            "max_bet": slots.MAX_BET, "bet_step": slots.BET_STEP,
            "daily": {"available": slots.can_claim_daily(acc), "amount": slots.DAILY_BONUS,
                      "next_day": slots._next_daily_day()},
            "stats": _stats(user_id), "history": _history(user_id), "meta": _meta(),
        }


def spin(user_id, bet, request_id):
    if bet % slots.BET_STEP or not slots.MIN_BET <= bet <= slots.MAX_BET:
        raise HTTPException(400, f"Ставка от {slots.MIN_BET} до {slots.MAX_BET} кратно {slots.BET_STEP}")
    interval = float(config.env("SLOTS_MIN_INTERVAL", "0.4") or 0)
    with db.tx():
        now = time.monotonic()
        acc = slots.ensure_account(user_id)
        old = db.one("SELECT * FROM meme_slot_spins WHERE user_id=? AND request_id=?",
                     (user_id, request_id))
        if old:
            if old["bet"] != bet:
                raise HTTPException(409, "request_id уже использован с другой ставкой")
            return _view(old, replayed=True)
        if now - slots._last_spin.get(user_id, -1e9) < interval:
            raise HTTPException(429, "Не так быстро — дождись окончания вращения")
        if acc["balance"] < bet:
            raise HTTPException(400, "Недостаточно жетонов для такой ставки")
        grid, details = play_meme(bet)
        payout = details["payout"]
        if acc["balance"] - bet + payout > 9_000_000_000_000_000:
            raise HTTPException(400, "Превышен предел игрового баланса")
        balance = acc["balance"] - bet + payout
        db.x("""UPDATE slot_accounts SET balance=?, spins_total=spins_total+1,
                 won_total=won_total+?, wagered_total=wagered_total+?,
                 wins_count=wins_count+?, best_win=MAX(best_win,?),
                 updated_at=CURRENT_TIMESTAMP WHERE user_id=?""",
             (balance, payout, bet, int(payout > 0), payout, user_id))
        sid = db.x("""INSERT INTO meme_slot_spins(user_id,request_id,bet,reels,lines,payout,
                   net,balance_after,win_tier) VALUES(?,?,?,?,?,?,?,?,?)""",
                   (user_id, request_id, bet, json.dumps(grid),
                    json.dumps({"lines": details["lines"], "scatter": details["scatter"],
                                "bonus": details["bonus"]}, ensure_ascii=False),
                    payout, payout - bet, balance, details["tier"]))
        slots._last_spin[user_id] = now
        return _view(db.one("SELECT * FROM meme_slot_spins WHERE id=?", (sid,)))


@router.get("/state")
async def get_state(request: Request):
    u = auth.require_user(request)
    return await run_in_threadpool(state, u["id"])


@router.post("/spin")
async def post_spin(request: Request, body: SpinBody):
    u = auth.require_user(request)
    return await run_in_threadpool(spin, u["id"], body.bet, body.request_id)
