"""Лаборатория (подвал фермы): выращивание елей и пихт, шишки, коллекция видов, улучшения. Правила — в lab_data.py.

Открывается на уровне фермы 6. Монеты — монеты фермы (общие с farm.py), поэтому ферма и лаборатория — одна экономика.
Время считается по меткам (mature_at/base_at) при каждом запросе, фоновых задач нет. Каждое действие — одна транзакция.
"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, farm
from . import gamification as gm
from .db import db
from .farm_data import price_mult
from .lab_data import (CRYSTAL, DISCOVERY_MULT, GOLD, MUTATION_CHANCE, SPECIES, TIER_NAMES, TIERS, UNLOCK_FARM_LEVEL, UPGRADES, cone_cap, cycle_time, grow_time,
                       pots_count, price_bonus, tier_unlocked)

router = APIRouter(prefix="/api/lab", tags=["Лаборатория"])
_rng = secrets.SystemRandom()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlantIn(Strict):
    slot: int = Field(ge=0, lt=12)
    species: str = Field(min_length=1, max_length=30)


class SlotIn(Strict):
    slot: int | None = Field(default=None, ge=0, lt=12)  # None — со всех


class RemoveIn(Strict):
    slot: int = Field(ge=0, lt=12)


class SellIn(Strict):
    species: str | None = Field(default=None, min_length=1, max_length=30)  # None — все шишки


class UpgradeIn(Strict):
    key: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS lab_trees(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, species TEXT NOT NULL, mutant TEXT, planted_at REAL NOT NULL, mature_at REAL NOT NULL, base_at REAL NOT NULL,
          PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lab_cones(user_id INTEGER NOT NULL, species TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, species)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lab_seen(user_id INTEGER NOT NULL, species TEXT NOT NULL, first_at REAL NOT NULL, PRIMARY KEY(user_id, species)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS lab_upgrades(user_id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(user_id, key)) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("lab_trees", "lab_cones", "lab_seen", "lab_upgrades"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники (внутри db.tx())
def _levels(user_id: int) -> dict:
    lv = {k: 0 for k in UPGRADES}
    for r in db.q("SELECT key, level FROM lab_upgrades WHERE user_id=?", (user_id,)):
        if r["key"] in lv:
            lv[r["key"]] = r["level"]
    return lv


def _seen(user_id: int) -> set:
    return {r["species"] for r in db.q("SELECT species FROM lab_seen WHERE user_id=?", (user_id,))}


def _tiers_done(seen: set) -> int:
    return sum(1 for t in TIERS.values() if all(s in seen for s in t))


def _need_open(user_id: int) -> dict:
    f = farm._farm(user_id)
    if farm._level(f) < UNLOCK_FARM_LEVEL:
        raise HTTPException(400, f"Лаборатория откроется на уровне фермы {UNLOCK_FARM_LEVEL}")
    return f


def _cones(user_id: int) -> dict:
    return {r["species"]: r["qty"] for r in db.q("SELECT species, qty FROM lab_cones WHERE user_id=? AND qty>0", (user_id,))}


def _price(species: str, lv: dict, tiers_done: int, day: str) -> int:
    return max(1, int(SPECIES[species]["price"] * price_mult(species, day) * (1 + price_bonus(lv["appraiser"], tiers_done))))


def _tree_view(r: dict, lv: dict, now: float) -> dict:
    total = max(1.0, r["mature_at"] - r["planted_at"])
    left = max(0.0, r["mature_at"] - now)
    if left > 0:
        base = SPECIES[r["species"]]
        return {"slot": r["slot"], "state": "growing", "species": r["species"], "name": base["name"], "left": round(left), "progress": round(1 - left / total, 3), "cap": cone_cap(lv["storage"])}
    sp = r["mutant"] or r["species"]
    cyc = cycle_time(sp, lv["drip"])
    cap = cone_cap(lv["storage"])
    n = min(cap, int((now - r["base_at"]) // cyc))
    nxt = 0 if n >= cap else round(cyc - (now - r["base_at"]) % cyc)
    return {"slot": r["slot"], "state": "mature", "species": sp, "name": SPECIES[sp]["name"], "mutant": bool(r["mutant"]), "cones": n, "cap": cap, "next_in": nxt, "cycle": cyc, "progress": 1.0}


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
    trees = {r["slot"]: r for r in db.q("SELECT * FROM lab_trees WHERE user_id=?", (user_id,))}
    pots = pots_count(lv["pots"])
    tv = [(_tree_view(trees[i], lv, now) if i in trees else {"slot": i, "state": "empty"}) for i in range(pots)]
    species = []
    for k, s in SPECIES.items():
        open_ = s["mutant"] and k in seen or (not s["mutant"] and tier_unlocked(s["tier"], lv["selection"]))
        species.append({"id": k, "name": s["name"], "tier": s["tier"], "sapling": s["sapling"], "grow": grow_time(k, lv["lamps"]), "cycle": cycle_time(k, lv["drip"]),
                        "price": _price(k, lv, done, day), "base_price": s["price"], "plantable": not s["mutant"] and open_, "discovered": k in seen,
                        "locked": not open_ and not s["mutant"], "mutant": s["mutant"]})
    ups = []
    for k, u in UPGRADES.items():
        cur = lv[k]
        ups.append({"key": k, "name": u["name"], "icon": u["icon"], "desc": u["desc"], "level": cur, "max": u["max"], "cost": u["costs"][cur] if cur < u["max"] else None})
    return {"unlocked": unlocked, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "trees": tv, "pots": pots, "max_pots": pots_count(UPGRADES["pots"]["max"]),
            "species": species, "tiers": {str(t): {"name": TIER_NAMES[t], "total": len(ids), "found": sum(1 for i in ids if i in seen)} for t, ids in TIERS.items()},
            "upgrades": ups, "cones": _cones(user_id), "tiers_done": done, "bonus": round(price_bonus(lv["appraiser"], done), 3),
            "mutation_chance": MUTATION_CHANCE[lv["selection"]], "events": events or []}


def public_stats(user_id: int) -> dict | None:
    seen = db.val("SELECT COUNT(*) FROM lab_seen WHERE user_id=?", (user_id,)) or 0
    trees = db.val("SELECT COUNT(*) FROM lab_trees WHERE user_id=?", (user_id,)) or 0
    return {"found": seen, "total": len(SPECIES), "trees": trees} if (seen or trees) else None


# ----------------------------------------------------------------------------- действия
def plant(user_id: int, slot: int, species: str) -> dict:
    s = SPECIES.get(species)
    if not s or s["mutant"]:
        raise HTTPException(400, "Такого саженца нет")
    now = time.time()
    with db.tx():
        f = _need_open(user_id)
        lv = _levels(user_id)
        if slot >= pots_count(lv["pots"]):
            raise HTTPException(400, "Такой кадки нет")
        if not tier_unlocked(s["tier"], lv["selection"]):
            raise HTTPException(400, f"{s['name']}: нужен уровень селекции выше")
        if db.one("SELECT 1 FROM lab_trees WHERE user_id=? AND slot=?", (user_id, slot)):
            raise HTTPException(400, "Кадка занята")
        farm._pay(user_id, f, s["sapling"])
        mutant = None
        if _rng.random() < MUTATION_CHANCE[lv["selection"]]:
            mutant = CRYSTAL if lv["selection"] >= 4 and _rng.random() < .25 else GOLD
        mature = now + grow_time(species, lv["lamps"])
        db.x("INSERT INTO lab_trees(user_id, slot, species, mutant, planted_at, mature_at, base_at) VALUES(?,?,?,?,?,?,?)", (user_id, slot, species, mutant, now, mature, mature))
    return get_state(user_id)


def remove(user_id: int, slot: int) -> dict:
    with db.tx():
        _need_open(user_id)
        if not db.one("SELECT 1 FROM lab_trees WHERE user_id=? AND slot=?", (user_id, slot)):
            raise HTTPException(400, "В этой кадке пусто")
        db.x("DELETE FROM lab_trees WHERE user_id=? AND slot=?", (user_id, slot))
    return get_state(user_id)


def harvest(user_id: int, slot: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        _need_open(user_id)
        lv = _levels(user_id)
        seen = _seen(user_id)
        done_before = _tiers_done(seen)
        rows = db.q("SELECT * FROM lab_trees WHERE user_id=? AND mature_at<=? ORDER BY slot", (user_id, now))
        if slot is not None:
            rows = [r for r in rows if r["slot"] == slot]
        got_any = False
        bonus_coins = 0
        for r in rows:
            sp = r["mutant"] or r["species"]
            cyc, cap = cycle_time(sp, lv["drip"]), cone_cap(lv["storage"])
            n = int((now - r["base_at"]) // cyc)
            if n < 1:
                continue
            take = min(n, cap)
            qty = sum(1 + (1 if _rng.random() < .08 * lv["climate"] else 0) for _ in range(take))
            new_base = now if n >= cap else r["base_at"] + n * cyc
            db.x("UPDATE lab_trees SET base_at=? WHERE user_id=? AND slot=?", (new_base, user_id, r["slot"]))
            db.x("INSERT INTO lab_cones(user_id, species, qty) VALUES(?,?,?) ON CONFLICT(user_id, species) DO UPDATE SET qty=qty+?", (user_id, sp, qty, qty))
            got_any = True
            ev.append({"type": "cones", "species": sp, "qty": qty})
            if sp not in seen:
                seen.add(sp)
                db.x("INSERT INTO lab_seen(user_id, species, first_at) VALUES(?,?,?)", (user_id, sp, now))
                b = SPECIES[sp]["price"] * DISCOVERY_MULT
                bonus_coins += b
                ev.append({"type": "discover", "species": sp, "bonus": b, "mutant": SPECIES[sp]["mutant"]})
        if not got_any:
            raise HTTPException(400, "Шишек пока нет")
        if bonus_coins:
            db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (bonus_coins, user_id))
        done_after = _tiers_done(seen)
        if done_after > done_before:
            ev.append({"type": "tier", "tiers": done_after})
    return get_state(user_id, ev)


def sell(user_id: int, species: str | None) -> dict:
    day = gm.today()
    with db.tx():
        _need_open(user_id)
        lv = _levels(user_id)
        done = _tiers_done(_seen(user_id))
        inv = _cones(user_id)
        ids = [species] if species else list(inv)
        if species and species not in SPECIES:
            raise HTTPException(400, "Такого вида нет")
        total = 0
        n = 0
        for sp in ids:
            q = inv.get(sp, 0)
            if q < 1:
                continue
            total += _price(sp, lv, done, day) * q
            n += q
            db.x("UPDATE lab_cones SET qty=0 WHERE user_id=? AND species=?", (user_id, sp))
        if not n:
            raise HTTPException(400, "Нет шишек на продажу")
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
        db.x("INSERT INTO lab_upgrades(user_id, key, level) VALUES(?,?,1) ON CONFLICT(user_id, key) DO UPDATE SET level=level+1", (user_id, key))
    return get_state(user_id)


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/plant")
async def api_plant(request: Request, body: PlantIn):
    return await run_in_threadpool(plant, _me(request)["id"], body.slot, body.species)


@router.post("/harvest")
async def api_harvest(request: Request, body: SlotIn):
    return await run_in_threadpool(harvest, _me(request)["id"], body.slot)


@router.post("/remove")
async def api_remove(request: Request, body: RemoveIn):
    return await run_in_threadpool(remove, _me(request)["id"], body.slot)


@router.post("/sell")
async def api_sell(request: Request, body: SellIn):
    return await run_in_threadpool(sell, _me(request)["id"], body.species)


@router.post("/upgrade")
async def api_upgrade(request: Request, body: UpgradeIn):
    return await run_in_threadpool(upgrade, _me(request)["id"], body.key)
