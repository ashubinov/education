"""Арена: петушиные бои. Драться могут только петухи (питомец вида «петух»), бой — до двух выигранных раундов.

Противник — друг с петухом (ему не нужно быть онлайн) или бот-спарринг. Сила петуха = стадия роста × бодрость (сытость): голодный бьёт слабее,
после боя сытость падает, так что петуха нужно кормить. Победа и поражение немного растят петуха.
Ставка в жетонах: выиграл — получаешь столько же сверху (призовой фонд арены), проиграл — ставка уходит другу-противнику (боту — сгорает).
Лимиты: 5 боёв в сутки, с одним и тем же другом не больше 2 боёв в сутки. Считается всё на сервере, в одной транзакции.
"""
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, pet, slots, social
from . import gamification as gm
from .db import db

router = APIRouter(prefix="/api/pet/arena", tags=["Арена"])
_rng = secrets.SystemRandom()

SPECIES = "rooster"
BETS = (0, 10, 25, 50)
FIGHTS_PER_DAY = 5
PAIR_PER_DAY = 2
MIN_FULLNESS = 20
FULLNESS_COST = 12
XP_WIN, XP_LOSE = 15, 5
NPCS = {
    "npc_novice": {"name": "Новичок", "power": 1.2, "note": "Только вышел на арену"},
    "npc_boris": {"name": "Чемпион Борис", "power": 5.0, "note": "Бьёт сильнее взрослого петуха"},
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FightIn(Strict):
    opponent_id: int | None = Field(default=None, ge=1, le=10**9)
    npc: str | None = Field(default=None, max_length=20)
    bet: int = Field(default=0, ge=0, le=max(BETS))


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS pet_fights(
          id INTEGER PRIMARY KEY AUTOINCREMENT, attacker_id INTEGER NOT NULL, defender_id INTEGER, npc TEXT, bet INTEGER NOT NULL, won INTEGER NOT NULL,
          rounds TEXT NOT NULL, day TEXT NOT NULL, at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_fights_att ON pet_fights(attacker_id, day);
        CREATE INDEX IF NOT EXISTS ix_fights_def ON pet_fights(defender_id, at);
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    db.x("DELETE FROM pet_fights WHERE attacker_id=? OR defender_id=?", (user_id, user_id))


# ----------------------------------------------------------------------------- сила и шансы
def power(row: dict, now: float) -> float:
    """Сила = (1 + опыт/300) × (0.6…1.0 по сытости). Малыш ≈ 1, сытый взрослый ≈ 4."""
    f = pet._fullness(row, now)
    return round((1 + row["xp"] / 300) * (.6 + .4 * f / 100), 3)


def round_chance(a: float, b: float) -> float:
    return min(.8, max(.2, a / (a + b)))


def match_chance(a: float, b: float) -> float:
    """Шанс выиграть бой до двух раундов."""
    p = round_chance(a, b)
    return p * p * (3 - 2 * p)


def _rooster(user_id: int) -> dict | None:
    row = pet._get(user_id)
    return row if row and row["species"] == SPECIES else None


def _record(user_id: int) -> dict:
    w = db.val("SELECT COUNT(*) FROM pet_fights WHERE (attacker_id=? AND won=1) OR (defender_id=? AND won=0)", (user_id, user_id)) or 0
    n = db.val("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? OR defender_id=?", (user_id, user_id)) or 0
    return {"wins": w, "losses": n - w}


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int) -> dict:
    now = time.time()
    day = gm.today()
    with db.tx():
        bal = slots.ensure_account(user_id)["balance"]
    mine = _rooster(user_id)
    my_pet = pet._get(user_id)
    my_power = power(mine, now) if mine else None
    used = db.val("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? AND day=?", (user_id, day)) or 0
    opponents = []
    for k, n in NPCS.items():
        opponents.append({"kind": "npc", "key": k, "name": n["name"], "note": n["note"], "power": n["power"], "chance": round(match_chance(my_power, n["power"]) * 100) if mine else None})
    for r in db.q("SELECT * FROM friendships WHERE status='accepted' AND (requester_id=? OR addressee_id=?)", (user_id, user_id)):
        oid = r["addressee_id"] if r["requester_id"] == user_id else r["requester_id"]
        u = db.one("SELECT * FROM users WHERE id=?", (oid,))
        orow = _rooster(oid)
        if not orow or not social._visible(u):
            continue
        pw = power(orow, now)
        pair_used = db.val("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? AND defender_id=? AND day=?", (user_id, oid, day)) or 0
        opponents.append({"kind": "user", "id": oid, "name": u["display_name"] or u["username"], "pet": pet._view(orow, now), "power": pw, "record": _record(oid),
                          "chance": round(match_chance(my_power, pw) * 100) if mine else None, "pair_left": max(0, PAIR_PER_DAY - pair_used)})
    recent = []
    for r in db.q("SELECT * FROM pet_fights WHERE attacker_id=? OR defender_id=? ORDER BY id DESC LIMIT 10", (user_id, user_id)):
        me_att = r["attacker_id"] == user_id
        other_id = r["defender_id"] if me_att else r["attacker_id"]
        if r["npc"]:
            name = NPCS.get(r["npc"], {}).get("name", "Бот")
        else:
            ou = db.one("SELECT * FROM users WHERE id=?", (other_id,))
            name = (ou["display_name"] or ou["username"]) if ou else "Игрок"
        won = bool(r["won"]) if me_att else not r["won"]
        delta = (r["bet"] if won else -r["bet"]) if me_att else (r["bet"] if won else 0)
        recent.append({"id": r["id"], "at": r["at"], "role": "attacker" if me_att else "defender", "opponent": name, "won": won, "bet": r["bet"], "delta": delta, "rounds": json.loads(r["rounds"])})
    return {"has_pet": bool(my_pet), "pet_species": my_pet["species"] if my_pet else None, "rooster": pet._view(mine, now) if mine else None, "power": my_power, "record": _record(user_id),
            "fights_left": max(0, FIGHTS_PER_DAY - used), "fights_per_day": FIGHTS_PER_DAY, "bets": list(BETS), "min_fullness": MIN_FULLNESS, "fullness_cost": FULLNESS_COST,
            "balance": bal, "opponents": opponents, "recent": recent}


# ----------------------------------------------------------------------------- бой
def fight(user_id: int, opponent_id: int | None, npc: str | None, bet: int) -> dict:
    if (opponent_id is None) == (npc is None):
        raise HTTPException(400, "Выбери одного соперника")
    if bet not in BETS:
        raise HTTPException(400, "Такой ставки нет")
    now = time.time()
    day = gm.today()
    with db.tx():
        mine = _rooster(user_id)
        if not mine:
            raise HTTPException(400, "Для боёв нужен петух")
        f = pet._fullness(mine, now)
        if f < MIN_FULLNESS:
            raise HTTPException(400, f"{mine['name']} слишком голоден для боя — покорми его")
        if (db.val("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? AND day=?", (user_id, day)) or 0) >= FIGHTS_PER_DAY:
            raise HTTPException(400, f"На сегодня боёв хватит ({FIGHTS_PER_DAY} в сутки). Приходи завтра")
        if npc is not None:
            if npc not in NPCS:
                raise HTTPException(400, "Такого соперника нет")
            their, opp_name, def_id = NPCS[npc]["power"], NPCS[npc]["name"], None
        else:
            if opponent_id == user_id:
                raise HTTPException(400, "С самим собой драться нельзя")
            u = social.require_friend(user_id, opponent_id)
            orow = _rooster(opponent_id)
            if not orow:
                raise HTTPException(404, "У друга нет петуха")
            if (db.val("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? AND defender_id=? AND day=?", (user_id, opponent_id, day)) or 0) >= PAIR_PER_DAY:
                raise HTTPException(400, f"С этим другом уже {PAIR_PER_DAY} боя сегодня — приходи завтра")
            their, opp_name, def_id = power(orow, now), u["display_name"] or u["username"], opponent_id
        sa = slots.ensure_account(user_id)
        if bet > sa["balance"]:
            raise HTTPException(400, "Недостаточно жетонов для такой ставки")
        mine_power = power(mine, now)
        p = round_chance(mine_power, their)
        rounds, a, b = [], 0, 0
        while a < 2 and b < 2:
            win = _rng.random() < p
            rounds.append(1 if win else 0)
            a, b = a + win, b + (not win)
        won = a == 2
        delta = bet if won else -bet
        if delta:
            db.x("UPDATE slot_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (delta, user_id))
        if bet and not won and def_id:
            slots.ensure_account(def_id)
            db.x("UPDATE slot_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (bet, def_id))
        gain = XP_WIN if won else XP_LOSE
        db.x("UPDATE pets SET fullness=?, fed_at=?, xp=xp+? WHERE user_id=?", (max(0.0, f - FULLNESS_COST), now, gain, user_id))
        db.x("INSERT INTO pet_fights(attacker_id, defender_id, npc, bet, won, rounds, day, at) VALUES(?,?,?,?,?,?,?,?)", (user_id, def_id, npc, bet, int(won), json.dumps(rounds), day, now))
    out = get_state(user_id)
    out["result"] = {"won": won, "rounds": rounds, "bet": bet, "delta": delta, "opponent": opp_name, "chance": round(match_chance(mine_power, their) * 100), "xp": gain,
                     "my_power": mine_power, "their_power": their}
    return out


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/fight")
async def api_fight(request: Request, body: FightIn):
    return await run_in_threadpool(fight, _me(request)["id"], body.opponent_id, body.npc, body.bet)
