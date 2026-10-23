"""Пасека (локация фермы): ульи, цветы со склада, мёд, коллекция видов, улучшения. Правила — в apiary_data.py.

Открывается на уровне фермы 11. Монеты — монеты фермы (общие с farm.py, lab.py и lake.py). Время считается по меткам (base_at), фоновых задач нет:
пчёлы работают и пока тебя нет. Каждое действие — одна транзакция: сначала всё проверяется, потом пишется.
"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, farm, loc_common
from . import gamification as gm
from .apiary_data import (DISCOVERY_MULT, FLOWERS, HONEY, JELLY_STEP, POLLEN_STEP, UNLOCK_FARM_LEVEL, UPGRADES, cap_of, cycle_time, hives_count,
                          price_bonus)
from .db import db
from .farm_data import ITEMS, price_mult

router = APIRouter(prefix="/api/apiary", tags=["Пасека"])
_rng = secrets.SystemRandom()
SLOTS = 12
T_UP = "apiary_upgrades"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlowersIn(Strict):
    hive: int = Field(ge=0, lt=SLOTS)
    flower: str = Field(min_length=1, max_length=20)


class CollectIn(Strict):
    hive: int | None = Field(default=None, ge=0, lt=SLOTS)  # None — со всех готовых


class SellIn(Strict):
    honey: str | None = Field(default=None, min_length=1, max_length=20)  # None — весь мёд


class UpgradeIn(Strict):
    key: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS apiary_hives(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, flower TEXT NOT NULL, base_at REAL NOT NULL, left INTEGER NOT NULL, PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS apiary_honey(user_id INTEGER NOT NULL, honey TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, honey)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS apiary_seen(user_id INTEGER NOT NULL, honey TEXT NOT NULL, first_at REAL NOT NULL, PRIMARY KEY(user_id, honey)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS apiary_upgrades(user_id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(user_id, key)) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("apiary_hives", "apiary_honey", "apiary_seen", "apiary_upgrades"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники
def _lv(user_id: int) -> dict:
    return loc_common.levels(T_UP, user_id, UPGRADES)


def _open(user_id: int) -> dict:
    return loc_common.need_open(user_id, UNLOCK_FARM_LEVEL, "Пасека")


def _inv(user_id: int) -> dict:
    return {r["honey"]: r["qty"] for r in db.q("SELECT honey, qty FROM apiary_honey WHERE user_id=? AND qty>0", (user_id,))}


def _avail(r: dict, lv: dict, now: float) -> tuple[int, int, int]:
    """(готово баночек, сколько циклов прошло, длина цикла)."""
    cyc = cycle_time(r["flower"], lv["queen"])
    elapsed = int((now - r["base_at"]) // cyc)
    n = min(elapsed, cap_of(lv["frames"]))
    if r["left"] >= 0:
        n = min(n, r["left"])
    return max(0, n), max(0, elapsed), cyc


def _price(honey: str, lv: dict, day: str) -> int:
    return max(1, int(HONEY[honey]["price"] * price_mult("honey_" + honey, day) * (1 + price_bonus(lv["appraiser"]))))


def _hive_view(r: dict, lv: dict, now: float) -> dict:
    n, elapsed, cyc = _avail(r, lv, now)
    cap = cap_of(lv["frames"])
    base = {"slot": r["slot"], "flower": r["flower"], "honey": FLOWERS[r["flower"]]["honey"], "cap": cap, "cycle": cyc, "left": None if r["left"] < 0 else r["left"]}
    if n > 0:
        return {**base, "state": "ready", "jars": n, "full": n >= cap}
    if r["left"] == 0:
        return {**base, "state": "idle", "jars": 0}
    return {**base, "state": "working", "jars": 0, "next_in": round(cyc - (now - r["base_at"]) % cyc), "progress": round(((now - r["base_at"]) % cyc) / cyc, 3)}


def ready_count(user_id: int) -> int:
    lv, now = _lv(user_id), time.time()
    return sum(1 for r in db.q("SELECT * FROM apiary_hives WHERE user_id=?", (user_id,)) if _avail(r, lv, now)[0] > 0)


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = farm._farm(user_id)
        lvl = farm._level(f)
    lv = _lv(user_id)
    seen = {r["honey"] for r in db.q("SELECT honey FROM apiary_seen WHERE user_id=?", (user_id,))}
    day = gm.today()
    rows = {r["slot"]: r for r in db.q("SELECT * FROM apiary_hives WHERE user_id=?", (user_id,))}
    hives = [(_hive_view(rows[i], lv, now) if i in rows else {"slot": i, "state": "empty"}) for i in range(hives_count(lv["hives"]))]
    stock = farm._stock(user_id)
    flowers = [{"id": k, "name": v["name"], "icon": v["icon"], "item": v["item"], "item_name": ITEMS[v["item"]]["name"] if v["item"] else None, "cycle": cycle_time(k, lv["queen"]),
                "lasts": v["lasts"], "honey": v["honey"], "have": stock.get(v["item"], 0) if v["item"] else None} for k, v in FLOWERS.items()]
    honey = [{"id": k, "name": h["name"], "icon": h["icon"], "tier": h["tier"], "price": _price(k, lv, day), "base_price": h["price"], "discovered": k in seen} for k, h in HONEY.items()]
    return {"unlocked": lvl >= UNLOCK_FARM_LEVEL, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "hives": hives, "max_hives": hives_count(UPGRADES["hives"]["max"]),
            "flowers": flowers, "honey": honey, "inv": _inv(user_id), "upgrades": loc_common.upgrade_view(UPGRADES, lv), "cap": cap_of(lv["frames"]),
            "bonus": round(price_bonus(lv["appraiser"]), 3), "pollen_chance": round(POLLEN_STEP * lv["pollen"], 2), "jelly_chance": round(JELLY_STEP * lv["jelly"], 2),
            "events": events or []}


# ----------------------------------------------------------------------------- действия
def set_flowers(user_id: int, hive: int, flower: str) -> dict:
    fl = FLOWERS.get(flower)
    if not fl:
        raise HTTPException(400, "Таких цветов нет")
    now = time.time()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        if hive >= hives_count(lv["hives"]):
            raise HTTPException(400, "Такого улья у тебя нет")
        r = db.one("SELECT * FROM apiary_hives WHERE user_id=? AND slot=?", (user_id, hive))
        if r:
            n = _avail(r, lv, now)[0]
            if n > 0:
                raise HTTPException(400, "Сначала забери мёд из улья")
            if r["left"] != 0 and r["flower"] != "clover":
                raise HTTPException(400, "Пчёлы ещё работают на этих цветах — подожди, пока они закончатся")
        if fl["item"]:
            if farm._stock(user_id).get(fl["item"], 0) < 1:
                raise HTTPException(400, f"Нужны цветы: {ITEMS[fl['item']]['name'].lower()}. На складе фермы их нет")
            db.x("UPDATE farm_stock SET qty=qty-1 WHERE user_id=? AND item=?", (user_id, fl["item"]))
        db.x("INSERT INTO apiary_hives(user_id, slot, flower, base_at, left) VALUES(?,?,?,?,?) ON CONFLICT(user_id, slot) DO UPDATE SET flower=excluded.flower, base_at=excluded.base_at, left=excluded.left",
             (user_id, hive, flower, now, fl["lasts"] if fl["lasts"] else -1))
    return get_state(user_id)


def collect(user_id: int, hive: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        seen = {r["honey"] for r in db.q("SELECT honey FROM apiary_seen WHERE user_id=?", (user_id,))}
        rows = db.q("SELECT * FROM apiary_hives WHERE user_id=? ORDER BY slot", (user_id,))
        if hive is not None:
            rows = [r for r in rows if r["slot"] == hive]
        got: dict = {}
        bonus = 0
        for r in rows:
            n, elapsed, cyc = _avail(r, lv, now)
            if n < 1:
                continue
            cap = cap_of(lv["frames"])
            jars = sum(1 + (1 if _rng.random() < POLLEN_STEP * lv["pollen"] else 0) for _ in range(n))
            jelly = sum(1 for _ in range(n) if _rng.random() < JELLY_STEP * lv["jelly"])
            h = FLOWERS[r["flower"]]["honey"]
            got[h] = got.get(h, 0) + jars
            if jelly:
                got["jelly"] = got.get("jelly", 0) + jelly
            new_base = now if elapsed >= cap else r["base_at"] + n * cyc
            new_left = r["left"] if r["left"] < 0 else r["left"] - n
            db.x("UPDATE apiary_hives SET base_at=?, left=? WHERE user_id=? AND slot=?", (new_base, new_left, user_id, r["slot"]))
        if not got:
            raise HTTPException(400, "Мёда пока нет")
        for h, q in got.items():
            db.x("INSERT INTO apiary_honey(user_id, honey, qty) VALUES(?,?,?) ON CONFLICT(user_id, honey) DO UPDATE SET qty=qty+?", (user_id, h, q, q))
            ev.append({"type": "honey", "honey": h, "qty": q})
            if h not in seen:
                seen.add(h)
                db.x("INSERT INTO apiary_seen(user_id, honey, first_at) VALUES(?,?,?)", (user_id, h, now))
                b = HONEY[h]["price"] * DISCOVERY_MULT
                bonus += b
                ev.append({"type": "discover", "honey": h, "bonus": b})
        if bonus:
            db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (bonus, user_id))
    return get_state(user_id, ev)


def sell(user_id: int, honey: str | None) -> dict:
    day = gm.today()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        inv = _inv(user_id)
        if honey and honey not in HONEY:
            raise HTTPException(400, "Такого мёда нет")
        total = n = 0
        for k in ([honey] if honey else list(inv)):
            q = inv.get(k, 0)
            if q < 1:
                continue
            total += _price(k, lv, day) * q
            n += q
            db.x("UPDATE apiary_honey SET qty=0 WHERE user_id=? AND honey=?", (user_id, k))
        if not n:
            raise HTTPException(400, "Нет мёда на продажу")
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


@router.post("/flowers")
async def api_flowers(request: Request, body: FlowersIn):
    return await run_in_threadpool(set_flowers, _me(request)["id"], body.hive, body.flower)


@router.post("/collect")
async def api_collect(request: Request, body: CollectIn):
    return await run_in_threadpool(collect, _me(request)["id"], body.hive)


@router.post("/sell")
async def api_sell(request: Request, body: SellIn):
    return await run_in_threadpool(sell, _me(request)["id"], body.honey)


@router.post("/upgrade")
async def api_upgrade(request: Request, body: UpgradeIn):
    return await run_in_threadpool(upgrade, _me(request)["id"], body.key)
