"""Ферма — простой аналог «большой фермы»: грядки, животные, кухня, склад, рынок, заказы. Данные и правила — в farm_data.py.

Время идёт по меткам (planted_at/ready_at), состояние считается при каждом запросе — фоновых задач нет, ферма «растёт», пока игрока нет.
Каждое действие — одна транзакция: сначала всё проверяется (уровень, монеты, склад), потом пишется. Ответ на любое действие — полное состояние фермы.
Монеты фермы свои; обмен на общие жетоны (слоты, питомец, пикселы) идёт по невыгодному курсу и с суточным лимитом.
"""
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, slots
from . import gamification as gm
from .db import db
from .farm_data import (ANIMALS, CAPACITY_STEP, CROPS, EXCHANGE_IN_RATE, EXCHANGE_MAX, EXCHANGE_OUT_DAILY, EXCHANGE_OUT_RATE, ITEMS, MAX_CAPACITY, MAX_LEVEL,
                        MAX_PLOTS, ORDER_SLOTS, PEN_SIZE, RECIPES, START_CAPACITY, START_COINS, START_PLOTS, capacity_cost, level_of, plot_cost,
                        price_mult, sell_price, unlock_level, xp_for_level)

router = APIRouter(prefix="/api/farm", tags=["Ферма"])
_rng = secrets.SystemRandom()
SKIP_COST = 5


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlantIn(Strict):
    crop: str = Field(min_length=1, max_length=20)
    idx: int | None = Field(default=None, ge=0, lt=MAX_PLOTS)  # None — на все пустые


class HarvestIn(Strict):
    idx: int | None = Field(default=None, ge=0, lt=MAX_PLOTS)  # None — со всех готовых


class PenIn(Strict):
    pen: str = Field(min_length=1, max_length=20)


class CookIn(Strict):
    recipe: str = Field(min_length=1, max_length=20)


class SellIn(Strict):
    item: str = Field(min_length=1, max_length=20)
    qty: int | None = Field(default=None, ge=1, le=10000)  # None — всё


class SlotIn(Strict):
    slot: int = Field(ge=0, lt=ORDER_SLOTS)


class ExchangeIn(Strict):
    direction: str = Field(pattern="^(in|out)$")
    amount: int = Field(ge=1, le=EXCHANGE_MAX)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS farm(
          user_id INTEGER PRIMARY KEY, xp INTEGER NOT NULL DEFAULT 0, coins INTEGER NOT NULL, plots INTEGER NOT NULL, capacity INTEGER NOT NULL,
          out_day TEXT NOT NULL DEFAULT '', out_used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS farm_plots(
          user_id INTEGER NOT NULL, idx INTEGER NOT NULL, crop TEXT NOT NULL, planted_at REAL NOT NULL, ready_at REAL NOT NULL, PRIMARY KEY(user_id, idx)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS farm_pens(
          user_id INTEGER NOT NULL, pen TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, fed_count INTEGER NOT NULL DEFAULT 0, ready_at REAL,
          PRIMARY KEY(user_id, pen)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS farm_stock(
          user_id INTEGER NOT NULL, item TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, item)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS farm_kitchen(
          user_id INTEGER PRIMARY KEY, recipe TEXT NOT NULL, started_at REAL NOT NULL, ready_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS farm_orders(
          user_id INTEGER NOT NULL, slot INTEGER NOT NULL, items TEXT NOT NULL, coins INTEGER NOT NULL, xp INTEGER NOT NULL, PRIMARY KEY(user_id, slot)
        ) WITHOUT ROWID;
        """)
        db.conn.commit()


def delete_user_data(user_id: int):
    for t in ("farm", "farm_plots", "farm_pens", "farm_stock", "farm_kitchen", "farm_orders"):
        db.x(f"DELETE FROM {t} WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- помощники (вызывать внутри db.tx())
def _farm(user_id: int) -> dict:
    row = db.one("SELECT * FROM farm WHERE user_id=?", (user_id,))
    if not row:
        db.x("INSERT OR IGNORE INTO farm(user_id, coins, plots, capacity, created_at) VALUES(?,?,?,?,?)", (user_id, START_COINS, START_PLOTS, START_CAPACITY, time.time()))
        row = db.one("SELECT * FROM farm WHERE user_id=?", (user_id,))
    return row


def _stock(user_id: int) -> dict:
    return {r["item"]: r["qty"] for r in db.q("SELECT item, qty FROM farm_stock WHERE user_id=? AND qty>0", (user_id,))}


def _stock_add(user_id: int, item: str, qty: int):
    db.x("INSERT INTO farm_stock(user_id, item, qty) VALUES(?,?,?) ON CONFLICT(user_id, item) DO UPDATE SET qty=qty+?", (user_id, item, qty, qty))


def _need_room(user_id: int, f: dict, qty: int):
    total = sum(_stock(user_id).values())
    if total + qty > f["capacity"]:
        raise HTTPException(400, f"На складе не хватает места ({total}/{f['capacity']}). Продай что-нибудь или расширь склад")


def _add_xp(user_id: int, n: int, ev: list):
    old = level_of(db.val("SELECT xp FROM farm WHERE user_id=?", (user_id,)))
    db.x("UPDATE farm SET xp=xp+? WHERE user_id=?", (n, user_id))
    new = level_of(db.val("SELECT xp FROM farm WHERE user_id=?", (user_id,)))
    if new > old:
        ev.append({"type": "level", "level": new})


def _pay(user_id: int, f: dict, cost: int):
    if f["coins"] < cost:
        raise HTTPException(400, f"Не хватает монет: нужно {cost}, у тебя {f['coins']}")
    db.x("UPDATE farm SET coins=coins-? WHERE user_id=?", (cost, user_id))


def _need_level(level: int, need: int, what: str):
    if level < need:
        raise HTTPException(400, f"{what} откроется на уровне {need}")


def _level(f: dict) -> int:
    return level_of(f["xp"])


def _gen_order(level: int) -> dict:
    pool = [i for i in ITEMS if unlock_level(i) <= level]
    k = 3 if level >= 5 and len(pool) >= 6 else 2
    picked = _rng.sample(pool, min(k, len(pool)))
    items = {}
    for it in picked:
        p = ITEMS[it]["price"]
        items[it] = _rng.randint(2, 6) if p < 15 else _rng.randint(1, 3) if p < 45 else _rng.randint(1, 2)
    base = sum(ITEMS[i]["price"] * q for i, q in items.items())
    coins = max(5, round(base * _rng.uniform(1.4, 1.8)))
    return {"items": items, "coins": coins, "xp": max(3, coins // 5)}


def _ensure_orders(user_id: int, level: int):
    have = {r["slot"] for r in db.q("SELECT slot FROM farm_orders WHERE user_id=?", (user_id,))}
    for s in range(ORDER_SLOTS):
        if s not in have:
            o = _gen_order(level)
            db.x("INSERT OR IGNORE INTO farm_orders(user_id, slot, items, coins, xp) VALUES(?,?,?,?,?)", (user_id, s, json.dumps(o["items"]), o["coins"], o["xp"]))


# ----------------------------------------------------------------------------- состояние
def get_state(user_id: int, events: list | None = None) -> dict:
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        lvl = _level(f)
        _ensure_orders(user_id, lvl)
        bal = slots.ensure_account(user_id)["balance"]
    day = gm.today()
    plots = {r["idx"]: r for r in db.q("SELECT * FROM farm_plots WHERE user_id=?", (user_id,))}
    plot_view = []
    for i in range(f["plots"]):
        r = plots.get(i)
        if not r:
            plot_view.append({"idx": i, "state": "empty"})
        else:
            total = max(1.0, r["ready_at"] - r["planted_at"])
            left = max(0.0, r["ready_at"] - now)
            plot_view.append({"idx": i, "crop": r["crop"], "state": "ready" if left <= 0 else "growing", "left": round(left), "progress": round(1 - left / total, 3)})
    pens = {r["pen"]: r for r in db.q("SELECT * FROM farm_pens WHERE user_id=?", (user_id,))}
    pen_view = []
    for aid, a in ANIMALS.items():
        r = pens.get(aid) or {"count": 0, "fed_count": 0, "ready_at": None}
        if r["fed_count"] <= 0:
            st, left = "idle", 0
        else:
            left = max(0.0, r["ready_at"] - now)
            st = "ready" if left <= 0 else "working"
        pen_view.append({"pen": aid, "count": r["count"], "fed_count": r["fed_count"], "state": st, "left": round(left), "total": a["time"], "locked": lvl < a["level"]})
    k = db.one("SELECT * FROM farm_kitchen WHERE user_id=?", (user_id,))
    kitchen = None
    if k:
        left = max(0.0, k["ready_at"] - now)
        kitchen = {"recipe": k["recipe"], "state": "ready" if left <= 0 else "working", "left": round(left), "total": round(k["ready_at"] - k["started_at"])}
    orders = [{"slot": r["slot"], "items": json.loads(r["items"]), "coins": r["coins"], "xp": r["xp"]} for r in db.q("SELECT * FROM farm_orders WHERE user_id=? ORDER BY slot", (user_id,))]
    stock = _stock(user_id)
    out_used = f["out_used"] if f["out_day"] == day else 0
    out = {
        "farm": {"level": lvl, "max_level": MAX_LEVEL, "xp": f["xp"], "xp_from": xp_for_level(lvl), "xp_next": xp_for_level(lvl + 1) if lvl < MAX_LEVEL else None, "coins": f["coins"],
                 "plots": f["plots"], "max_plots": MAX_PLOTS, "plot_cost": plot_cost(f["plots"]) if f["plots"] < MAX_PLOTS else None,
                 "capacity": f["capacity"], "stock_total": sum(stock.values()), "capacity_cost": capacity_cost(f["capacity"]) if f["capacity"] < MAX_CAPACITY else None,
                 "capacity_step": CAPACITY_STEP, "pen_size": PEN_SIZE},
        "plots": plot_view, "pens": pen_view, "kitchen": kitchen, "orders": orders, "stock": stock,
        "prices": {i: {"price": sell_price(i, day), "mult": price_mult(i, day)} for i in ITEMS},
        "catalog": {"crops": list(CROPS.values()), "animals": list(ANIMALS.values()), "recipes": list(RECIPES.values()), "items": ITEMS, "skip_cost": SKIP_COST},
        "exchange": {"balance": bal, "in_rate": EXCHANGE_IN_RATE, "out_rate": EXCHANGE_OUT_RATE, "out_left": max(0, EXCHANGE_OUT_DAILY - out_used), "out_daily": EXCHANGE_OUT_DAILY},
        "events": events or [],
    }
    return out


def public_stats(user_id: int) -> dict | None:
    f = db.one("SELECT * FROM farm WHERE user_id=?", (user_id,))
    if not f:
        return None
    animals = db.val("SELECT COALESCE(SUM(count),0) FROM farm_pens WHERE user_id=?", (user_id,)) or 0
    return {"level": _level(f), "plots": f["plots"], "animals": animals, "xp": f["xp"]}


# ----------------------------------------------------------------------------- грядки
def plant(user_id: int, crop_id: str, idx: int | None) -> dict:
    c = CROPS.get(crop_id)
    if not c:
        raise HTTPException(400, "Такой культуры нет")
    ev: list = []
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        _need_level(_level(f), c["level"], c["name"])
        busy = {r["idx"] for r in db.q("SELECT idx FROM farm_plots WHERE user_id=?", (user_id,))}
        if idx is not None:
            if idx >= f["plots"]:
                raise HTTPException(400, "Такой грядки у тебя нет")
            if idx in busy:
                raise HTTPException(400, "Грядка занята")
            targets = [idx]
        else:
            targets = [i for i in range(f["plots"]) if i not in busy]
        if not targets:
            raise HTTPException(400, "Свободных грядок нет")
        n = min(len(targets), f["coins"] // c["seed"])
        if n < 1:
            raise HTTPException(400, f"Не хватает монет на семена: нужно {c['seed']}, у тебя {f['coins']}")
        targets = targets[:n]
        db.x("UPDATE farm SET coins=coins-? WHERE user_id=?", (c["seed"] * n, user_id))
        for i in targets:
            db.x("INSERT INTO farm_plots(user_id, idx, crop, planted_at, ready_at) VALUES(?,?,?,?,?)", (user_id, i, crop_id, now, now + c["time"]))
    return get_state(user_id, ev)


def harvest(user_id: int, idx: int | None) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        rows = db.q("SELECT * FROM farm_plots WHERE user_id=? AND ready_at<=? ORDER BY idx", (user_id, now))
        if idx is not None:
            if idx >= f["plots"]:
                raise HTTPException(400, "Такой грядки у тебя нет")
            rows = [r for r in rows if r["idx"] == idx]
            if not rows:
                raise HTTPException(400, "Здесь пока нечего собирать")
        if not rows:
            raise HTTPException(400, "Нет готового урожая")
        total = sum(_stock(user_id).values())
        got = 0
        gained = 0
        for r in rows:
            c = CROPS[r["crop"]]
            if total + c["yield"] > f["capacity"]:
                break
            total += c["yield"]
            db.x("DELETE FROM farm_plots WHERE user_id=? AND idx=?", (user_id, r["idx"]))
            _stock_add(user_id, r["crop"], c["yield"])
            gained += c["xp"]
            got += 1
        if not got:
            raise HTTPException(400, f"На складе нет места ({total}/{f['capacity']}). Продай что-нибудь или расширь склад")
        if got < len(rows):
            ev.append({"type": "full", "left": len(rows) - got})
        _add_xp(user_id, gained, ev)
    return get_state(user_id, ev)


def buy_plot(user_id: int) -> dict:
    with db.tx():
        f = _farm(user_id)
        if f["plots"] >= MAX_PLOTS:
            raise HTTPException(400, "Больше грядок не бывает")
        _pay(user_id, f, plot_cost(f["plots"]))
        db.x("UPDATE farm SET plots=plots+1 WHERE user_id=?", (user_id,))
    return get_state(user_id)


# ----------------------------------------------------------------------------- животные
def buy_animal(user_id: int, pen: str) -> dict:
    a = ANIMALS.get(pen)
    if not a:
        raise HTTPException(400, "Такого животного нет")
    with db.tx():
        f = _farm(user_id)
        _need_level(_level(f), a["level"], a["name"])
        row = db.one("SELECT * FROM farm_pens WHERE user_id=? AND pen=?", (user_id, pen))
        if row and row["count"] >= PEN_SIZE:
            raise HTTPException(400, "Загон полон")
        _pay(user_id, f, a["price"])
        db.x("INSERT INTO farm_pens(user_id, pen, count) VALUES(?,?,1) ON CONFLICT(user_id, pen) DO UPDATE SET count=count+1", (user_id, pen))
    return get_state(user_id)


def feed(user_id: int, pen: str) -> dict:
    a = ANIMALS.get(pen)
    if not a:
        raise HTTPException(400, "Такого загона нет")
    now = time.time()
    with db.tx():
        _farm(user_id)
        row = db.one("SELECT * FROM farm_pens WHERE user_id=? AND pen=?", (user_id, pen))
        if not row or row["count"] < 1:
            raise HTTPException(400, f"Сначала купи животное: {a['name'].lower()}")
        if row["fed_count"] > 0:
            raise HTTPException(400, "Уже накормлены: сначала забери продукцию" if now >= row["ready_at"] else "Они ещё заняты — подожди")
        have = _stock(user_id).get(a["feed"], 0)
        if have < row["count"]:
            raise HTTPException(400, f"Нужно {row['count']} × {ITEMS[a['feed']]['name'].lower()}, а на складе {have}")
        db.x("UPDATE farm_stock SET qty=qty-? WHERE user_id=? AND item=?", (row["count"], user_id, a["feed"]))
        db.x("UPDATE farm_pens SET fed_count=count, ready_at=? WHERE user_id=? AND pen=?", (now + a["time"], user_id, pen))
    return get_state(user_id)


def collect(user_id: int, pen: str) -> dict:
    a = ANIMALS.get(pen)
    if not a:
        raise HTTPException(400, "Такого загона нет")
    ev: list = []
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        row = db.one("SELECT * FROM farm_pens WHERE user_id=? AND pen=?", (user_id, pen))
        if not row or row["fed_count"] < 1:
            raise HTTPException(400, "Здесь пока нечего собирать")
        if now < row["ready_at"]:
            raise HTTPException(400, "Ещё не готово")
        qty = row["fed_count"]
        _need_room(user_id, f, qty)
        _stock_add(user_id, a["product"], qty)
        db.x("UPDATE farm_pens SET fed_count=0, ready_at=NULL WHERE user_id=? AND pen=?", (user_id, pen))
        _add_xp(user_id, a["xp"] * qty, ev)
    return get_state(user_id, ev)


# ----------------------------------------------------------------------------- кухня
def cook(user_id: int, recipe_id: str) -> dict:
    r = RECIPES.get(recipe_id)
    if not r:
        raise HTTPException(400, "Такого рецепта нет")
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        _need_level(_level(f), r["level"], r["name"])
        if db.one("SELECT 1 FROM farm_kitchen WHERE user_id=?", (user_id,)):
            raise HTTPException(400, "Кухня занята: сначала доделай или забери прошлое блюдо")
        stock = _stock(user_id)
        for it, q in r["needs"].items():
            if stock.get(it, 0) < q:
                raise HTTPException(400, f"Не хватает: {ITEMS[it]['name'].lower()} ({stock.get(it, 0)}/{q})")
        for it, q in r["needs"].items():
            db.x("UPDATE farm_stock SET qty=qty-? WHERE user_id=? AND item=?", (q, user_id, it))
        db.x("INSERT INTO farm_kitchen(user_id, recipe, started_at, ready_at) VALUES(?,?,?,?)", (user_id, recipe_id, now, now + r["time"]))
    return get_state(user_id)


def cook_collect(user_id: int) -> dict:
    ev: list = []
    now = time.time()
    with db.tx():
        f = _farm(user_id)
        k = db.one("SELECT * FROM farm_kitchen WHERE user_id=?", (user_id,))
        if not k:
            raise HTTPException(400, "На кухне ничего не готовится")
        if now < k["ready_at"]:
            raise HTTPException(400, "Ещё не готово")
        r = RECIPES[k["recipe"]]
        _need_room(user_id, f, r["qty"])
        _stock_add(user_id, k["recipe"], r["qty"])
        db.x("DELETE FROM farm_kitchen WHERE user_id=?", (user_id,))
        _add_xp(user_id, r["xp"], ev)
    return get_state(user_id, ev)


# ----------------------------------------------------------------------------- рынок, заказы, склад, обмен
def sell(user_id: int, item: str, qty: int | None) -> dict:
    if item not in ITEMS:
        raise HTTPException(400, "Такого товара нет")
    with db.tx():
        _farm(user_id)
        have = _stock(user_id).get(item, 0)
        n = have if qty is None else qty
        if n < 1 or have < n:
            raise HTTPException(400, f"На складе нет столько: {ITEMS[item]['name'].lower()} — {have}")
        gain = sell_price(item, gm.today()) * n
        db.x("UPDATE farm_stock SET qty=qty-? WHERE user_id=? AND item=?", (n, user_id, item))
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (gain, user_id))
    out = get_state(user_id)
    out["sold"] = {"item": item, "qty": n, "coins": gain}
    return out


def order_deliver(user_id: int, slot: int) -> dict:
    ev: list = []
    with db.tx():
        f = _farm(user_id)
        _ensure_orders(user_id, _level(f))
        o = db.one("SELECT * FROM farm_orders WHERE user_id=? AND slot=?", (user_id, slot))
        items = json.loads(o["items"])
        stock = _stock(user_id)
        for it, q in items.items():
            if stock.get(it, 0) < q:
                raise HTTPException(400, f"Не хватает: {ITEMS[it]['name'].lower()} ({stock.get(it, 0)}/{q})")
        for it, q in items.items():
            db.x("UPDATE farm_stock SET qty=qty-? WHERE user_id=? AND item=?", (q, user_id, it))
        db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (o["coins"], user_id))
        _add_xp(user_id, o["xp"], ev)
        new = _gen_order(_level(db.one("SELECT * FROM farm WHERE user_id=?", (user_id,))))
        db.x("UPDATE farm_orders SET items=?, coins=?, xp=? WHERE user_id=? AND slot=?", (json.dumps(new["items"]), new["coins"], new["xp"], user_id, slot))
        ev.append({"type": "order", "coins": o["coins"], "xp": o["xp"]})
    return get_state(user_id, ev)


def order_skip(user_id: int, slot: int) -> dict:
    with db.tx():
        f = _farm(user_id)
        _pay(user_id, f, SKIP_COST)
        new = _gen_order(_level(f))
        db.x("UPDATE farm_orders SET items=?, coins=?, xp=? WHERE user_id=? AND slot=?", (json.dumps(new["items"]), new["coins"], new["xp"], user_id, slot))
    return get_state(user_id)


def upgrade_storage(user_id: int) -> dict:
    with db.tx():
        f = _farm(user_id)
        if f["capacity"] >= MAX_CAPACITY:
            raise HTTPException(400, "Склад уже самый большой")
        _pay(user_id, f, capacity_cost(f["capacity"]))
        db.x("UPDATE farm SET capacity=capacity+? WHERE user_id=?", (CAPACITY_STEP, user_id))
    return get_state(user_id)


def exchange(user_id: int, direction: str, amount: int) -> dict:
    day = gm.today()
    with db.tx():
        f = _farm(user_id)
        sa = slots.ensure_account(user_id)
        if direction == "in":
            if sa["balance"] < amount:
                raise HTTPException(400, "Недостаточно жетонов")
            db.x("UPDATE slot_accounts SET balance=balance-?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount, user_id))
            db.x("UPDATE farm SET coins=coins+? WHERE user_id=?", (amount * EXCHANGE_IN_RATE, user_id))
        else:  # amount — сколько жетонов хочу получить
            used = f["out_used"] if f["out_day"] == day else 0
            if used + amount > EXCHANGE_OUT_DAILY:
                raise HTTPException(400, f"За сутки можно получить не больше {EXCHANGE_OUT_DAILY} жетонов, осталось {max(0, EXCHANGE_OUT_DAILY - used)}")
            cost = amount * EXCHANGE_OUT_RATE
            if f["coins"] < cost:
                raise HTTPException(400, f"Не хватает монет: нужно {cost}, у тебя {f['coins']}")
            db.x("UPDATE farm SET coins=coins-?, out_day=?, out_used=? WHERE user_id=?", (cost, day, used + amount, user_id))
            db.x("UPDATE slot_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (amount, user_id))
    return get_state(user_id)


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/plant")
async def api_plant(request: Request, body: PlantIn):
    return await run_in_threadpool(plant, _me(request)["id"], body.crop, body.idx)


@router.post("/harvest")
async def api_harvest(request: Request, body: HarvestIn):
    return await run_in_threadpool(harvest, _me(request)["id"], body.idx)


@router.post("/plot/buy")
async def api_buy_plot(request: Request):
    return await run_in_threadpool(buy_plot, _me(request)["id"])


@router.post("/animal/buy")
async def api_buy_animal(request: Request, body: PenIn):
    return await run_in_threadpool(buy_animal, _me(request)["id"], body.pen)


@router.post("/animal/feed")
async def api_feed(request: Request, body: PenIn):
    return await run_in_threadpool(feed, _me(request)["id"], body.pen)


@router.post("/animal/collect")
async def api_collect(request: Request, body: PenIn):
    return await run_in_threadpool(collect, _me(request)["id"], body.pen)


@router.post("/cook")
async def api_cook(request: Request, body: CookIn):
    return await run_in_threadpool(cook, _me(request)["id"], body.recipe)


@router.post("/cook/collect")
async def api_cook_collect(request: Request):
    return await run_in_threadpool(cook_collect, _me(request)["id"])


@router.post("/sell")
async def api_sell(request: Request, body: SellIn):
    return await run_in_threadpool(sell, _me(request)["id"], body.item, body.qty)


@router.post("/order/deliver")
async def api_order_deliver(request: Request, body: SlotIn):
    return await run_in_threadpool(order_deliver, _me(request)["id"], body.slot)


@router.post("/order/skip")
async def api_order_skip(request: Request, body: SlotIn):
    return await run_in_threadpool(order_skip, _me(request)["id"], body.slot)


@router.post("/storage/upgrade")
async def api_upgrade(request: Request):
    return await run_in_threadpool(upgrade_storage, _me(request)["id"])


@router.post("/exchange")
async def api_exchange(request: Request, body: ExchangeIn):
    return await run_in_threadpool(exchange, _me(request)["id"], body.direction, body.amount)
