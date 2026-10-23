"""Караван (торговый тракт фермы): повозки, дальние города, спрос, разбойники, улучшения. Правила — в caravan_data.py.

Открывается на уровне фермы 13. Груз берётся со склада фермы, выручка — монеты фермы (общие с остальными локациями). Цена за груз и нападение разбойников
определяются при отправке повозки и хранятся в записи рейса. Время считается по меткам (ready_at), фоновых задач нет.
"""
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, farm, loc_common
from . import gamification as gm
from .caravan_data import (BANDIT_LOSS, TOWNS, UNLOCK_FARM_LEVEL, UPGRADES, WANT_MULT, bandit_chance, cargo_cap, trip_time, town_open, unit_price, wagons_count,
                           wanted)
from .db import db
from .farm_data import ITEMS

router = APIRouter(prefix="/api/caravan", tags=["Караван"])
_rng = secrets.SystemRandom()
SLOTS = 8
T_UP = "caravan_upgrades"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendIn(Strict):
    wagon: int = Field(ge=0, lt=SLOTS)
    town: str = Field(min_length=1, max_length=20)
    cargo: dict[str, int] = Field(max_length=40)


class CollectIn(Strict):
    wagon: int | None = Field(default=None, ge=0, lt=SLOTS)  # None — со всех вернувшихся


class UpgradeIn(Strict):
    key: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS caravan_trips(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, town TEXT NOT NULL, sent_at REAL NOT NULL, ready_at REAL NOT NULL, cargo TEXT NOT NULL,
          payout INTEGER NOT NULL, bandits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS caravan_upgrades(user_id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(user_id, key)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS caravan_stats(user_id INTEGER PRIMARY KEY, trips INTEGER NOT NULL DEFAULT 0, earned INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("caravan_trips", "caravan_upgrades", "caravan_stats"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники
def _lv(user_id: int) -> dict:
    return loc_common.levels(T_UP, user_id, UPGRADES)


def _open(user_id: int) -> dict:
    return loc_common.need_open(user_id, UNLOCK_FARM_LEVEL, "Торговый тракт")


def _trip_view(r: dict, now: float) -> dict:
    total = max(1.0, r["ready_at"] - r["sent_at"])
    left = max(0.0, r["ready_at"] - now)
    return {"slot": r["slot"], "state": "ready" if left <= 0 else "away", "town": r["town"], "left": round(left), "total": round(total), "progress": round(1 - left / total, 3),
            "cargo": json.loads(r["cargo"]), "payout": r["payout"]}


def ready_count(user_id: int) -> int:
    return db.val("SELECT COUNT(*) FROM caravan_trips WHERE user_id=? AND ready_at<=?", (user_id, time.time()), 0) or 0


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = farm._farm(user_id)
        lvl = farm._level(f)
    lv = _lv(user_id)
    day = gm.today()
    trips = {r["slot"]: r for r in db.q("SELECT * FROM caravan_trips WHERE user_id=?", (user_id,))}
    wagons = [(_trip_view(trips[i], now) if i in trips else {"slot": i, "state": "home"}) for i in range(wagons_count(lv["wagons"]))]
    stock = farm._stock(user_id)
    towns = []
    for k, t in TOWNS.items():
        towns.append({"id": k, "name": t["name"], "icon": t["icon"], "time": trip_time(k, lv["horse"]), "mult": t["mult"], "risk": round(bandit_chance(k, lv["guard"]), 2),
                      "locked": not town_open(k, lv["roads"]), "wanted": wanted(k, day), "price": {i: unit_price(i, k, day, lv["merchant"]) for i in ITEMS}})
    st = db.one("SELECT trips, earned FROM caravan_stats WHERE user_id=?", (user_id,)) or {"trips": 0, "earned": 0}
    return {"unlocked": lvl >= UNLOCK_FARM_LEVEL, "need_level": UNLOCK_FARM_LEVEL, "farm_level": lvl, "coins": f["coins"], "wagons": wagons, "max_wagons": wagons_count(UPGRADES["wagons"]["max"]),
            "towns": towns, "stock": stock, "items": ITEMS, "cargo_cap": cargo_cap(lv["cart"]), "want_mult": WANT_MULT, "bandit_loss": BANDIT_LOSS,
            "upgrades": loc_common.upgrade_view(UPGRADES, lv), "stats": st, "events": events or []}


# ----------------------------------------------------------------------------- действия
def send(user_id: int, wagon: int, town: str, cargo: dict) -> dict:
    if town not in TOWNS:
        raise HTTPException(400, "Такого города нет")
    cargo = {k: v for k, v in cargo.items() if v}
    if not cargo or any(k not in ITEMS or v < 1 for k, v in cargo.items()):
        raise HTTPException(400, "Выбери, что везти")
    now = time.time()
    day = gm.today()
    with db.tx():
        _open(user_id)
        lv = _lv(user_id)
        if wagon >= wagons_count(lv["wagons"]):
            raise HTTPException(400, "Такой повозки у тебя нет")
        if not town_open(town, lv["roads"]):
            raise HTTPException(400, f"Дорога в «{TOWNS[town]['name']}» ещё не построена")
        if db.one("SELECT 1 FROM caravan_trips WHERE user_id=? AND slot=?", (user_id, wagon)):
            raise HTTPException(400, "Эта повозка ещё в пути")
        total = sum(cargo.values())
        if total > cargo_cap(lv["cart"]):
            raise HTTPException(400, f"В повозку помещается не больше {cargo_cap(lv['cart'])} штук")
        stock = farm._stock(user_id)
        for k, v in cargo.items():
            if stock.get(k, 0) < v:
                raise HTTPException(400, f"На складе нет столько: {ITEMS[k]['name'].lower()} ({stock.get(k, 0)}/{v})")
        for k, v in cargo.items():
            db.x("UPDATE farm_stock SET qty=qty-? WHERE user_id=? AND item=?", (v, user_id, k))
        payout = sum(unit_price(k, town, day, lv["merchant"]) * v for k, v in cargo.items())
        bandits = _rng.random() < bandit_chance(town, lv["guard"])
        db.x("INSERT INTO caravan_trips(user_id, slot, town, sent_at, ready_at, cargo, payout, bandits) VALUES(?,?,?,?,?,?,?,?)",
             (user_id, wagon, town, now, now + trip_time(town, lv["horse"]), json.dumps(cargo), payout, int(bandits)))
    return get_state(user_id)


def collect(user_id: int, wagon: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        _open(user_id)
        rows = db.q("SELECT * FROM caravan_trips WHERE user_id=? AND ready_at<=? ORDER BY slot", (user_id, now))
        if wagon is not None:
            rows = [r for r in rows if r["slot"] == wagon]
        if not rows:
            raise HTTPException(400, "Повозки ещё не вернулись")
        total = 0
        for r in rows:
            got = int(r["payout"] * (1 - BANDIT_LOSS)) if r["bandits"] else r["payout"]
            total += got
            ev.append({"type": "trade", "town": r["town"], "coins": got, "lost": r["payout"] - got, "bandits": bool(r["bandits"])})
            db.x("DELETE FROM caravan_trips WHERE user_id=? AND slot=?", (user_id, r["slot"]))
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (total, user_id))
        db.x("INSERT INTO caravan_stats(user_id, trips, earned) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET trips=trips+?, earned=earned+?", (user_id, len(rows), total, len(rows), total))
    return get_state(user_id, ev)


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
    return await run_in_threadpool(send, _me(request)["id"], body.wagon, body.town, body.cargo)


@router.post("/collect")
async def api_collect(request: Request, body: CollectIn):
    return await run_in_threadpool(collect, _me(request)["id"], body.wagon)


@router.post("/upgrade")
async def api_upgrade(request: Request, body: UpgradeIn):
    return await run_in_threadpool(upgrade, _me(request)["id"], body.key)
