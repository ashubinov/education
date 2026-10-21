"""Озеро (рыбалка у фермы): удочки, наживка со склада, улов, рыбный атлас, улучшения. Правила — в lake_data.py.

Открывается на уровне фермы 10. Монеты — монеты фермы (общие с farm.py и lab.py), поэтому ферма, лаборатория и озеро — одна экономика.
Клёв считается по меткам (ready_at), фоновых задач нет; что именно поймалось, решает сервер в момент, когда удочку поднимают.
Каждое действие — одна транзакция: сначала всё проверяется, потом пишется.
"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, farm
from . import gamification as gm
from .db import db
from .farm_data import ITEMS, price_mult
from .lake_data import (BAITS, CHEST_CHANCE, CHEST_COINS, DISCOVERY_MULT, DOUBLE_STEP, FISH, JUNK_CHANCE, PEARL_CHANCE, TIER_NAMES, TIERS, UNLOCK_FARM_LEVEL,
                        UP_TIER_CHANCE, UPGRADES, bite_time, keep_cap, legend_chance, max_tier, price_bonus, rods_count)

router = APIRouter(prefix="/api/lake", tags=["Озеро"])
_rng = secrets.SystemRandom()
MAX_RODS_SLOTS = 12


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CastIn(Strict):
    rod: int = Field(ge=0, lt=MAX_RODS_SLOTS)
    bait: str = Field(min_length=1, max_length=20)


class CollectIn(Strict):
    rod: int | None = Field(default=None, ge=0, lt=MAX_RODS_SLOTS)  # None — со всех готовых


class SellIn(Strict):
    fish: str | None = Field(default=None, min_length=1, max_length=20)  # None — всю рыбу


class UpgradeIn(Strict):
    key: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS lake_rods(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, bait TEXT NOT NULL, cast_at REAL NOT NULL, ready_at REAL NOT NULL, PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lake_fish(user_id INTEGER NOT NULL, fish TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, fish)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lake_seen(user_id INTEGER NOT NULL, fish TEXT NOT NULL, first_at REAL NOT NULL, PRIMARY KEY(user_id, fish)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lake_upgrades(user_id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(user_id, key)) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("lake_rods", "lake_fish", "lake_seen", "lake_upgrades"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники (внутри db.tx())
def _levels(user_id: int) -> dict:
    lv = {k: 0 for k in UPGRADES}
    for r in db.q("SELECT key, level FROM lake_upgrades WHERE user_id=?", (user_id,)):
        if r["key"] in lv:
            lv[r["key"]] = r["level"]
    return lv


def _seen(user_id: int) -> set:
    return {r["fish"] for r in db.q("SELECT fish FROM lake_seen WHERE user_id=?", (user_id,))}


def _tiers_done(seen: set) -> int:
    return sum(1 for ids in TIERS.values() if all(f in seen for f in ids))


def _inv(user_id: int) -> dict:
    return {r["fish"]: r["qty"] for r in db.q("SELECT fish, qty FROM lake_fish WHERE user_id=? AND qty>0", (user_id,))}


def _need_open(user_id: int) -> dict:
    f = farm._farm(user_id)
    if farm._level(f) < UNLOCK_FARM_LEVEL:
        raise HTTPException(400, f"Озеро откроется на уровне фермы {UNLOCK_FARM_LEVEL}")
    return f


def _price(fish: str, lv: dict, tiers_done: int, day: str) -> int:
    return max(1, int(FISH[fish]["price"] * price_mult(fish, day) * (1 + price_bonus(lv["appraiser"], tiers_done))))


def _tier_open(tier: int, boat: int) -> bool:
    return tier <= 2 or (tier == 3 and boat >= 1) or (tier == 4 and boat >= 2)


def _roll(bait: str, lv: dict) -> dict:
    """Что клюнуло. Возвращает {'kind': 'chest'|'fish', ...}."""
    x = _rng.random()
    if x < CHEST_CHANCE:
        return {"kind": "chest", "coins": _rng.randint(*CHEST_COINS)}
    if x < CHEST_CHANCE + PEARL_CHANCE:
        return {"kind": "fish", "fish": "pearl", "qty": 1}
    if x < CHEST_CHANCE + PEARL_CHANCE + JUNK_CHANCE:
        return {"kind": "fish", "fish": "boot", "qty": 1}
    if _rng.random() < legend_chance(lv["boat"], lv["sonar"]):
        sp = _rng.choice(TIERS[4])
    else:
        t = BAITS[bait]["tier"]
        if _rng.random() >= UP_TIER_CHANCE:
            t -= 1
        sp = _rng.choice(TIERS[max(1, min(t, max_tier(lv["boat"])))])
    return {"kind": "fish", "fish": sp, "qty": 2 if _rng.random() < DOUBLE_STEP * lv["net"] else 1}


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = farm._farm(user_id)
        lvl = farm._level(f)
    unlocked = lvl >= UNLOCK_FARM_LEVEL
    lv = _levels(user_id)
    seen = _seen(user_id)
    done = _tiers_done(seen)
    day = gm.today()
    inv = _inv(user_id)
    cap = keep_cap(lv["keep"])
    stock = farm._stock(user_id)
    rows = {r["slot"]: r for r in db.q("SELECT * FROM lake_rods WHERE user_id=?", (user_id,))}
    rods = []
    for i in range(rods_count(lv["rods"])):
        r = rows.get(i)
        if not r:
            rods.append({"slot": i, "state": "empty"})
        else:
            total = max(1.0, r["ready_at"] - r["cast_at"])
            left = max(0.0, r["ready_at"] - now)
            rods.append({"slot": i, "state": "ready" if left <= 0 else "fishing", "bait": r["bait"], "left": round(left), "total": round(total), "progress": round(1 - left / total, 3)})
    fish = []
    for k, s in FISH.items():
        open_ = s["tier"] == 0 or _tier_open(s["tier"], lv["boat"])
        fish.append({"id": k, "name": s["name"], "icon": s["icon"], "tier": s["tier"], "price": _price(k, lv, done, day), "base_price": s["price"],
                     "discovered": k in seen, "locked": not open_})
    baits = [{"id": b, "name": ITEMS[b]["name"], "icon": ITEMS[b]["icon"], "tier": v["tier"], "time": bite_time(b, lv["reel"]), "have": stock.get(b, 0)} for b, v in BAITS.items()]
    ups = []
    for k, u in UPGRADES.items():
        cur = lv[k]
        ups.append({"key": k, "name": u["name"], "icon": u["icon"], "desc": u["desc"], "level": cur, "max": u["max"], "cost": u["costs"][cur] if cur < u["max"] else None})
    return {"unlocked": unlocked, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "rods": rods, "max_rods": rods_count(UPGRADES["rods"]["max"]),
            "baits": baits, "fish": fish, "inv": inv, "keep_used": sum(inv.values()), "keep_cap": cap, "upgrades": ups, "boat": lv["boat"],
            "tiers": {str(t): {"name": TIER_NAMES[t], "total": len(ids), "found": sum(1 for i in ids if i in seen)} for t, ids in TIERS.items()},
            "tiers_done": done, "bonus": round(price_bonus(lv["appraiser"], done), 3), "legend_chance": legend_chance(lv["boat"], lv["sonar"]),
            "double_chance": round(DOUBLE_STEP * lv["net"], 2), "events": events or []}


def public_stats(user_id: int) -> dict | None:
    seen = db.val("SELECT COUNT(*) FROM lake_seen WHERE user_id=?", (user_id,)) or 0
    return {"found": seen, "total": sum(len(v) for v in TIERS.values())} if seen else None


# ----------------------------------------------------------------------------- действия
def cast(user_id: int, rod: int, bait: str) -> dict:
    if bait not in BAITS:
        raise HTTPException(400, "Такой наживки нет")
    now = time.time()
    with db.tx():
        _need_open(user_id)
        lv = _levels(user_id)
        if rod >= rods_count(lv["rods"]):
            raise HTTPException(400, "Такой удочки у тебя нет")
        if db.one("SELECT 1 FROM lake_rods WHERE user_id=? AND slot=?", (user_id, rod)):
            raise HTTPException(400, "Удочка уже закинута")
        have = farm._stock(user_id).get(bait, 0)
        if have < 1:
            raise HTTPException(400, f"Нужна наживка: {ITEMS[bait]['name'].lower()}. На складе фермы её нет")
        db.x("UPDATE farm_stock SET qty=qty-1 WHERE user_id=? AND item=?", (user_id, bait))
        db.x("INSERT INTO lake_rods(user_id, slot, bait, cast_at, ready_at) VALUES(?,?,?,?,?)", (user_id, rod, bait, now, now + bite_time(bait, lv["reel"])))
    return get_state(user_id)


def collect(user_id: int, rod: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        f = _need_open(user_id)
        lv = _levels(user_id)
        seen = _seen(user_id)
        done_before = _tiers_done(seen)
        inv = _inv(user_id)
        used, cap = sum(inv.values()), keep_cap(lv["keep"])
        rows = db.q("SELECT * FROM lake_rods WHERE user_id=? AND ready_at<=? ORDER BY slot", (user_id, now))
        if rod is not None:
            rows = [r for r in rows if r["slot"] == rod]
        if not rows:
            raise HTTPException(400, "Клёва пока нет")
        bonus = got = full = 0
        for r in rows:
            res = _roll(r["bait"], lv)
            if res["kind"] == "chest":
                db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (res["coins"], user_id))
                ev.append({"type": "chest", "coins": res["coins"]})
            else:
                if used + res["qty"] > cap:
                    full += 1
                    continue
                used += res["qty"]
                fid = res["fish"]
                db.x("INSERT INTO lake_fish(user_id, fish, qty) VALUES(?,?,?) ON CONFLICT(user_id, fish) DO UPDATE SET qty=qty+?", (user_id, fid, res["qty"], res["qty"]))
                ev.append({"type": "catch", "fish": fid, "qty": res["qty"]})
                if FISH[fid]["tier"] and fid not in seen:
                    seen.add(fid)
                    db.x("INSERT INTO lake_seen(user_id, fish, first_at) VALUES(?,?,?)", (user_id, fid, now))
                    b = FISH[fid]["price"] * DISCOVERY_MULT
                    bonus += b
                    ev.append({"type": "discover", "fish": fid, "bonus": b, "legend": FISH[fid]["tier"] == 4})
            db.x("DELETE FROM lake_rods WHERE user_id=? AND slot=?", (user_id, r["slot"]))
            got += 1
        if not got:
            raise HTTPException(400, f"В садке нет места ({used}/{cap}). Продай рыбу или расширь садок")
        if full:
            ev.append({"type": "full", "left": full})
        if bonus:
            db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (bonus, user_id))
        if _tiers_done(seen) > done_before:
            ev.append({"type": "tier", "tiers": _tiers_done(seen)})
    return get_state(user_id, ev)


def sell(user_id: int, fish: str | None) -> dict:
    day = gm.today()
    with db.tx():
        _need_open(user_id)
        lv = _levels(user_id)
        done = _tiers_done(_seen(user_id))
        inv = _inv(user_id)
        if fish and fish not in FISH:
            raise HTTPException(400, "Такой рыбы нет")
        ids = [fish] if fish else list(inv)
        total = n = 0
        for k in ids:
            q = inv.get(k, 0)
            if q < 1:
                continue
            total += _price(k, lv, done, day) * q
            n += q
            db.x("UPDATE lake_fish SET qty=0 WHERE user_id=? AND fish=?", (user_id, k))
        if not n:
            raise HTTPException(400, "В садке нет рыбы на продажу")
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (total, user_id))
    out = get_state(user_id)
    out["sold"] = {"qty": n, "coins": total}
    return out


def upgrade(user_id: int, key: str) -> dict:
    u = UPGRADES.get(key)
    if not u:
        raise HTTPException(400, "Такого улучшения нет")
    with db.tx():
        f = _need_open(user_id)
        cur = _levels(user_id)[key]
        if cur >= u["max"]:
            raise HTTPException(400, "Улучшение уже на максимуме")
        farm._pay(user_id, f, u["costs"][cur])
        db.x("INSERT INTO lake_upgrades(user_id, key, level) VALUES(?,?,1) ON CONFLICT(user_id, key) DO UPDATE SET level=level+1", (user_id, key))
    return get_state(user_id)


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/cast")
async def api_cast(request: Request, body: CastIn):
    return await run_in_threadpool(cast, _me(request)["id"], body.rod, body.bait)


@router.post("/collect")
async def api_collect(request: Request, body: CollectIn):
    return await run_in_threadpool(collect, _me(request)["id"], body.rod)


@router.post("/sell")
async def api_sell(request: Request, body: SellIn):
    return await run_in_threadpool(sell, _me(request)["id"], body.fish)


@router.post("/upgrade")
async def api_upgrade(request: Request, body: UpgradeIn):
    return await run_in_threadpool(upgrade, _me(request)["id"], body.key)
