"""Питомец: у каждого игрока может жить один зверёк (на выбор — породы собак, петух, кучерявый баран, бобр).

Его нужно кормить: еда покупается за жетоны (общие со слотами) и лежит в запасе. Сытость падает со временем и считается лениво — фоновых задач нет.
Еда даёт сытость и рост (любимая еда вида — на 50 % больше роста). Питомец растёт малыш → подросток → взрослый, никогда не умирает: если забросить, он грустит и не растёт.
Друзья могут зайти в гости и погладить питомца: раз в день на одного друга, за это гость получает немного жетонов.
Всё проверяет сервер; списание жетонов и запись в запас — одна транзакция.
"""
import re
import time

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, social, slots
from . import gamification as gm
from .db import db
from .pet_data import (BUY_MAX_QTY, CANT_EAT_ABOVE, DECAY_PER_HOUR, FOODS, FULL, LIKED_BONUS, MOOD_NAMES, NAME_MAX, PAT_REWARD, SPECIES, START_FULLNESS,
                       liked, mood_of, stage_of)

router = APIRouter(prefix="/api/pet", tags=["Питомец"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdoptIn(Strict):
    species: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=40)


class RenameIn(Strict):
    name: str = Field(min_length=1, max_length=40)


class BuyIn(Strict):
    food_id: str = Field(min_length=1, max_length=20)
    qty: int = Field(default=1, ge=1, le=BUY_MAX_QTY)


class FeedIn(Strict):
    food_id: str = Field(min_length=1, max_length=20)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS pets(
          user_id INTEGER PRIMARY KEY, species TEXT NOT NULL, name TEXT NOT NULL, fullness REAL NOT NULL, fed_at REAL NOT NULL,
          xp INTEGER NOT NULL DEFAULT 0, fed_total INTEGER NOT NULL DEFAULT 0, pats_total INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pet_food(
          user_id INTEGER NOT NULL, food_id TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY(user_id, food_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS pet_pats(
          visitor_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY(visitor_id, owner_id, day)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS ix_pet_pats_owner ON pet_pats(owner_id, day);
        """)
        db.conn.commit()


# ----------------------------------------------------------------------------- представление
def _fullness(row: dict, now: float) -> float:
    return max(0.0, row["fullness"] - DECAY_PER_HOUR * max(0.0, now - row["fed_at"]) / 3600)


def clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw or "").strip()
    if not name or len(name) > NAME_MAX or re.search(r"[\x00-\x1f<>]", name):
        raise HTTPException(400, f"Имя — от 1 до {NAME_MAX} символов, без служебных знаков")
    return name


def _view(row: dict, now: float) -> dict:
    f = _fullness(row, now)
    st = stage_of(row["xp"])
    sp = SPECIES[row["species"]]
    mood = mood_of(f)
    return {"user_id": row["user_id"], "species": row["species"], "species_name": sp["name"], "name": row["name"], "fullness": round(f, 1), "mood": mood,
            "mood_name": MOOD_NAMES[mood], "stage": st["id"], "stage_name": st["name"], "stage_index": st["index"], "xp": row["xp"], "xp_from": st["from"],
            "xp_next": st["next"], "next_stage": st["next_name"], "fed_total": row["fed_total"], "pats_total": row["pats_total"],
            "age_days": int((now - row["created_at"]) // 86400)}


def _foods_view(species: str | None) -> list[dict]:
    return [dict(f, liked=bool(species) and liked(species, f["id"])) for f in FOODS.values()]


def _inventory(user_id: int) -> dict:
    return {r["food_id"]: r["qty"] for r in db.q("SELECT food_id, qty FROM pet_food WHERE user_id=? AND qty>0", (user_id,))}


def _get(user_id: int) -> dict | None:
    return db.one("SELECT * FROM pets WHERE user_id=?", (user_id,))


def get_state(user_id: int) -> dict:
    now = time.time()
    with db.tx():
        bal = slots.ensure_account(user_id)["balance"]
    row = _get(user_id)
    return {"pet": _view(row, now) if row else None, "species": [dict(id=k, name=v["name"], desc=v["desc"]) for k, v in SPECIES.items()],
            "foods": _foods_view(row["species"] if row else None), "inventory": _inventory(user_id), "balance": bal, "name_max": NAME_MAX,
            "can_eat_below": CANT_EAT_ABOVE, "pat_reward": PAT_REWARD, "buy_max": BUY_MAX_QTY}


# ----------------------------------------------------------------------------- действия
def adopt(user_id: int, species: str, name: str) -> dict:
    if species not in SPECIES:
        raise HTTPException(400, "Такого животного нет")
    name = clean_name(name)
    now = time.time()
    with db.tx():
        if _get(user_id):
            raise HTTPException(400, "У тебя уже есть питомец")
        db.x("INSERT INTO pets(user_id, species, name, fullness, fed_at, created_at) VALUES(?,?,?,?,?,?)", (user_id, species, name, START_FULLNESS, now, now))
        _add_food(user_id, SPECIES[species]["starter"], 3)  # стартовый запас любимой еды
    return get_state(user_id)


def _add_food(user_id: int, food_id: str, qty: int):
    db.x("INSERT INTO pet_food(user_id, food_id, qty) VALUES(?,?,?) ON CONFLICT(user_id, food_id) DO UPDATE SET qty=qty+?", (user_id, food_id, qty, qty))


def buy(user_id: int, food_id: str, qty: int) -> dict:
    food = FOODS.get(food_id)
    if not food:
        raise HTTPException(400, "Такой еды нет")
    cost = food["price"] * qty
    with db.tx():
        sa = slots.ensure_account(user_id)
        if sa["balance"] < cost:
            raise HTTPException(400, "Недостаточно жетонов")
        db.x("UPDATE slot_accounts SET balance=balance-?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (cost, user_id))
        _add_food(user_id, food_id, qty)
    return {"balance": sa["balance"] - cost, "inventory": _inventory(user_id), "spent": cost}


def feed(user_id: int, food_id: str) -> dict:
    food = FOODS.get(food_id)
    if not food:
        raise HTTPException(400, "Такой еды нет")
    now = time.time()
    with db.tx():
        row = _get(user_id)
        if not row:
            raise HTTPException(400, "Сначала возьми питомца")
        f = _fullness(row, now)
        if f > CANT_EAT_ABOVE:
            raise HTTPException(400, f"{row['name']} сыт и не хочет есть. Подожди немного")
        have = db.val("SELECT qty FROM pet_food WHERE user_id=? AND food_id=?", (user_id, food_id)) or 0
        if have < 1:
            raise HTTPException(400, "Этой еды нет в запасе — купи в магазине")
        db.x("UPDATE pet_food SET qty=qty-1 WHERE user_id=? AND food_id=?", (user_id, food_id))
        like = liked(row["species"], food_id)
        gain = int(round(food["xp"] * (LIKED_BONUS if like else 1)))
        before = stage_of(row["xp"])["index"]
        new_f = min(float(FULL), f + food["fill"])
        db.x("UPDATE pets SET fullness=?, fed_at=?, xp=xp+?, fed_total=fed_total+1 WHERE user_id=?", (new_f, now, gain, user_id))
        row = _get(user_id)
    out = get_state(user_id)
    out["fed"] = {"food_id": food_id, "gain": gain, "liked": like, "fill": round(new_f - f, 1), "grew": stage_of(row["xp"])["index"] > before}
    return out


def rename(user_id: int, name: str) -> dict:
    name = clean_name(name)
    with db.tx():
        if not _get(user_id):
            raise HTTPException(400, "Сначала возьми питомца")
        db.x("UPDATE pets SET name=? WHERE user_id=?", (name, user_id))
    return get_state(user_id)


def release(user_id: int) -> dict:
    """Отпустить питомца на волю (можно завести нового). Запас еды остаётся."""
    with db.tx():
        if not _get(user_id):
            raise HTTPException(400, "У тебя нет питомца")
        db.x("DELETE FROM pets WHERE user_id=?", (user_id,))
        db.x("DELETE FROM pet_pats WHERE owner_id=?", (user_id,))
    return get_state(user_id)


# ----------------------------------------------------------------------------- гости
def public_view(owner_id: int, viewer_id: int | None = None) -> dict | None:
    """Питомец глазами друга: без запасов и баланса. None — у друга нет питомца."""
    row = _get(owner_id)
    if not row:
        return None
    v = _view(row, time.time())
    v["pats_today"] = db.val("SELECT COUNT(*) FROM pet_pats WHERE owner_id=? AND day=?", (owner_id, gm.today())) or 0
    v["patted_by_me"] = bool(viewer_id and db.val("SELECT 1 FROM pet_pats WHERE visitor_id=? AND owner_id=? AND day=?", (viewer_id, owner_id, gm.today())))
    for k in ("fed_total",):
        v.pop(k, None)
    return v


def pat(visitor_id: int, owner_id: int) -> dict:
    if visitor_id == owner_id:
        raise HTTPException(400, "Своего питомца можно кормить, а гладят в гостях")
    social.require_friend(visitor_id, owner_id)
    with db.tx():
        row = _get(owner_id)
        if not row:
            raise HTTPException(404, "У друга нет питомца")
        day = gm.today()
        if db.val("SELECT 1 FROM pet_pats WHERE visitor_id=? AND owner_id=? AND day=?", (visitor_id, owner_id, day)):
            raise HTTPException(400, "Сегодня ты уже гулял с этим питомцем — приходи завтра")
        db.x("INSERT INTO pet_pats(visitor_id, owner_id, day) VALUES(?,?,?)", (visitor_id, owner_id, day))
        db.x("UPDATE pets SET pats_total=pats_total+1 WHERE user_id=?", (owner_id,))
        sa = slots.ensure_account(visitor_id)
        db.x("UPDATE slot_accounts SET balance=balance+?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (PAT_REWARD, visitor_id))
    return {"reward": PAT_REWARD, "balance": sa["balance"] + PAT_REWARD, "pet": public_view(owner_id, visitor_id)}


def delete_user_data(user_id: int):
    db.x("DELETE FROM pets WHERE user_id=?", (user_id,))
    db.x("DELETE FROM pet_food WHERE user_id=?", (user_id,))
    db.x("DELETE FROM pet_pats WHERE owner_id=? OR visitor_id=?", (user_id, user_id))


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.post("/adopt")
async def api_adopt(request: Request, body: AdoptIn):
    return await run_in_threadpool(adopt, _me(request)["id"], body.species, body.name)


@router.post("/buy")
async def api_buy(request: Request, body: BuyIn):
    return await run_in_threadpool(buy, _me(request)["id"], body.food_id, body.qty)


@router.post("/feed")
async def api_feed(request: Request, body: FeedIn):
    return await run_in_threadpool(feed, _me(request)["id"], body.food_id)


@router.post("/rename")
async def api_rename(request: Request, body: RenameIn):
    return await run_in_threadpool(rename, _me(request)["id"], body.name)


@router.post("/release")
async def api_release(request: Request):
    return await run_in_threadpool(release, _me(request)["id"])


@router.post("/pat/{owner_id}")
async def api_pat(request: Request, owner_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(pat, _me(request)["id"], owner_id)
