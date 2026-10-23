"""Шахта (локация фермы): штреки, смены с едой со склада, руда, коллекция, улучшения. Правила — в mine_data.py.

Открывается на уровне фермы 12. Монеты — монеты фермы (общие с остальными локациями). Что добыто (и был ли обвал), решается в момент отправки смены и хранится
в записи смены, поэтому «пересобрать» добычу повторными нажатиями нельзя. Время считается по меткам (ready_at), фоновых задач нет.
"""
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, farm, loc_common
from . import gamification as gm
from .db import db
from .farm_data import ITEMS, price_mult
from .mine_data import (DISCOVERY_MULT, LAMP_STEP, LOOT, ORES, PICK_STEP, RARE_FROM, TIERS, UNLOCK_FARM_LEVEL, UPGRADES, cart_cap, price_bonus, risk_of,
                        shafts_count, tier_open)

router = APIRouter(prefix="/api/mine", tags=["Шахта"])
_rng = secrets.SystemRandom()
SLOTS = 12
T_UP = "mine_upgrades"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendIn(Strict):
    shaft: int = Field(ge=0, lt=SLOTS)
    tier: int = Field(ge=1, le=len(TIERS))


class CollectIn(Strict):
    shaft: int | None = Field(default=None, ge=0, lt=SLOTS)  # None — со всех готовых


class SellIn(Strict):
    ore: str | None = Field(default=None, min_length=1, max_length=20)  # None — вся руда


class UpgradeIn(Strict):
    key: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS mine_jobs(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, tier INTEGER NOT NULL, started_at REAL NOT NULL, ready_at REAL NOT NULL, loot TEXT NOT NULL, cave INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS mine_ore(user_id INTEGER NOT NULL, ore TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, ore)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS mine_seen(user_id INTEGER NOT NULL, ore TEXT NOT NULL, first_at REAL NOT NULL, PRIMARY KEY(user_id, ore)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS mine_upgrades(user_id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(user_id, key)) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("mine_jobs", "mine_ore", "mine_seen", "mine_upgrades"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники
def _lv(user_id: int) -> dict:
    return loc_common.levels(T_UP, user_id, UPGRADES)


def _open(user_id: int) -> dict:
    return loc_common.need_open(user_id, UNLOCK_FARM_LEVEL, "Шахта")


def _inv(user_id: int) -> dict:
    return {r["ore"]: r["qty"] for r in db.q("SELECT ore, qty FROM mine_ore WHERE user_id=? AND qty>0", (user_id,))}


def _price(ore: str, lv: dict, day: str) -> int:
    return max(1, int(ORES[ore]["price"] * price_mult("ore_" + ore, day) * (1 + price_bonus(lv["appraiser"]))))


def roll_haul(tier: int, lv: dict) -> tuple[dict, bool]:
    """Добыча смены: словарь руда → штук и признак обвала. Вызывается один раз при отправке смены."""
    t = TIERS[tier]
    weights = {o: w * (1 + LAMP_STEP * lv["lamp"] if ORES[o]["price"] >= RARE_FROM else 1) for o, w in LOOT[tier].items()}
    names, ws = list(weights), list(weights.values())
    finds = t["finds"]
    cave = _rng.random() < risk_of(tier, lv["crib"])
    if cave:
        finds = max(1, finds // 2)
    haul: dict = {}
    for _ in range(finds):
        o = _rng.choices(names, ws)[0]
        haul[o] = haul.get(o, 0) + (2 if _rng.random() < .10 + PICK_STEP * lv["pick"] else 1)
    return haul, cave


def _job_view(r: dict, now: float) -> dict:
    total = max(1.0, r["ready_at"] - r["started_at"])
    left = max(0.0, r["ready_at"] - now)
    return {"slot": r["slot"], "state": "ready" if left <= 0 else "working", "tier": r["tier"], "left": round(left), "total": round(total), "progress": round(1 - left / total, 3)}


def ready_count(user_id: int) -> int:
    return db.val("SELECT COUNT(*) FROM mine_jobs WHERE user_id=? AND ready_at<=?", (user_id, time.time()), 0) or 0


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = farm._farm(user_id)
        lvl = farm._level(f)
    lv = _lv(user_id)
    seen = {r["ore"] for r in db.q("SELECT ore FROM mine_seen WHERE user_id=?", (user_id,))}
    day = gm.today()
    jobs = {r["slot"]: r for r in db.q("SELECT * FROM mine_jobs WHERE user_id=?", (user_id,))}
    shafts = [(_job_view(jobs[i], now) if i in jobs else {"slot": i, "state": "empty"}) for i in range(shafts_count(lv["shafts"]))]
    stock = farm._stock(user_id)
    tiers = [{"tier": k, "name": t["name"], "icon": t["icon"], "time": t["time"], "food": t["food"], "food_name": ITEMS[t["food"]]["name"], "food_icon": ITEMS[t["food"]]["icon"],
              "have": stock.get(t["food"], 0), "finds": t["finds"], "risk": round(risk_of(k, lv["crib"]), 2), "locked": not tier_open(k, lv["lift"])} for k, t in TIERS.items()]
    ores = [{"id": k, "name": o["name"], "icon": o["icon"], "price": _price(k, lv, day), "base_price": o["price"], "discovered": k in seen} for k, o in ORES.items()]
    inv = _inv(user_id)
    return {"unlocked": lvl >= UNLOCK_FARM_LEVEL, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "shafts": shafts, "max_shafts": shafts_count(UPGRADES["shafts"]["max"]),
            "tiers": tiers, "ores": ores, "inv": inv, "cart_used": sum(inv.values()), "cart_cap": cart_cap(lv["cart"]), "upgrades": loc_common.upgrade_view(UPGRADES, lv),
            "bonus": round(price_bonus(lv["appraiser"]), 3), "double_chance": round(.10 + PICK_STEP * lv["pick"], 2), "events": events or []}


# ----------------------------------------------------------------------------- действия
def send(user_id: int, shaft: int, tier: int) -> dict:
    now = time.time()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        if shaft >= shafts_count(lv["shafts"]):
            raise HTTPException(400, "Такого штрека у тебя нет")
        if not tier_open(tier, lv["lift"]):
            raise HTTPException(400, f"«{TIERS[tier]['name']}» пока недоступен: нужен лифт получше")
        if db.one("SELECT 1 FROM mine_jobs WHERE user_id=? AND slot=?", (user_id, shaft)):
            raise HTTPException(400, "В этом штреке уже идёт смена")
        t = TIERS[tier]
        if farm._stock(user_id).get(t["food"], 0) < 1:
            raise HTTPException(400, f"Шахтёрам нужна еда: {ITEMS[t['food']]['name'].lower()}. На складе фермы её нет")
        db.x("UPDATE farm_stock SET qty=qty-1 WHERE user_id=? AND item=?", (user_id, t["food"]))
        haul, cave = roll_haul(tier, lv)
        db.x("INSERT INTO mine_jobs(user_id, slot, tier, started_at, ready_at, loot, cave) VALUES(?,?,?,?,?,?,?)",
             (user_id, shaft, tier, now, now + t["time"], json.dumps(haul), int(cave)))
    return get_state(user_id)


def collect(user_id: int, shaft: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        seen = {r["ore"] for r in db.q("SELECT ore FROM mine_seen WHERE user_id=?", (user_id,))}
        inv = _inv(user_id)
        used, cap = sum(inv.values()), cart_cap(lv["cart"])
        rows = db.q("SELECT * FROM mine_jobs WHERE user_id=? AND ready_at<=? ORDER BY slot", (user_id, now))
        if shaft is not None:
            rows = [r for r in rows if r["slot"] == shaft]
        if not rows:
            raise HTTPException(400, "Смена ещё не вернулась")
        done = full = bonus = 0
        for r in rows:
            haul = json.loads(r["loot"])
            qty = sum(haul.values())
            if used + qty > cap:
                full += 1
                continue
            used += qty
            for o, q in haul.items():
                db.x("INSERT INTO mine_ore(user_id, ore, qty) VALUES(?,?,?) ON CONFLICT(user_id, ore) DO UPDATE SET qty=qty+?", (user_id, o, q, q))
                ev.append({"type": "ore", "ore": o, "qty": q})
                if o not in seen:
                    seen.add(o)
                    db.x("INSERT INTO mine_seen(user_id, ore, first_at) VALUES(?,?,?)", (user_id, o, now))
                    b = ORES[o]["price"] * DISCOVERY_MULT
                    bonus += b
                    ev.append({"type": "discover", "ore": o, "bonus": b})
            if r["cave"]:
                ev.append({"type": "cave", "tier": r["tier"]})
            db.x("DELETE FROM mine_jobs WHERE user_id=? AND slot=?", (user_id, r["slot"]))
            done += 1
        if not done:
            raise HTTPException(400, f"В вагонетках нет места ({used}/{cap}). Продай руду или купи вагонетки")
        if full:
            ev.append({"type": "full", "left": full})
        if bonus:
            db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (bonus, user_id))
    return get_state(user_id, ev)


def sell(user_id: int, ore: str | None) -> dict:
    day = gm.today()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        inv = _inv(user_id)
        if ore and ore not in ORES:
            raise HTTPException(400, "Такой руды нет")
        total = n = 0
        for k in ([ore] if ore else list(inv)):
            q = inv.get(k, 0)
            if q < 1:
                continue
            total += _price(k, lv, day) * q
            n += q
            db.x("UPDATE mine_ore SET qty=0 WHERE user_id=? AND ore=?", (user_id, k))
        if not n:
            raise HTTPException(400, "Нет руды на продажу")
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (total, user_id))
    out = get_state(user_id)
    out["sold"] = {"qty": n, "coins": total}
    return out


def upgrade(user_id: int, key: str) -> dict:
    with db.tx():
        f = _open(user_id)
        if key not in UPGRADES:
            raise HTTPException(400, "Такого улучшения нет")
        loc_common.buy_upgrade(user_id, T_UP, UPGRADES, key, f, _lv(user_id)[key])
    return get_state(user_id)


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/send")
async def api_send(request: Request, body: SendIn):
    return await run_in_threadpool(send, _me(request)["id"], body.shaft, body.tier)


@router.post("/collect")
async def api_collect(request: Request, body: CollectIn):
    return await run_in_threadpool(collect, _me(request)["id"], body.shaft)


@router.post("/sell")
async def api_sell(request: Request, body: SellIn):
    return await run_in_threadpool(sell, _me(request)["id"], body.ore)


@router.post("/upgrade")
async def api_upgrade(request: Request, body: UpgradeIn):
    return await run_in_threadpool(upgrade, _me(request)["id"], body.key)
