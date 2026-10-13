"""Кланы: любой игрок может создать свой клан или вступить в любой открытый; клан виден в профилях, у друзей и в чате.

Правила (все проверяет сервер):
- один игрок состоит максимум в одном клане; вступление свободное, пока в клане меньше MEMBERS_MAX человек;
- создатель — лидер: меняет эмодзи и описание, исключает участников, передаёт лидерство, распускает клан. Распустить клан может и администратор;
- если лидер уходит, лидерство получает самый давний участник; если участников не осталось, клан исчезает;
- рейтинг кланов — по XP участников за последние 7 дней (при равенстве — по всем XP и числу участников);
- название (3–24 символа) и тег (2–5 символов) уникальны без учёта регистра; эмодзи — только из готового набора.
Заблокированные пользователи в составе и очках не показываются. Фоновых задач нет: всё считается при запросе.
"""
import re
import sqlite3
import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, avatars
from . import gamification as gm
from .db import db

router = APIRouter(prefix="/api/clans", tags=["Кланы"])

MEMBERS_MAX = 30
NAME_MIN, NAME_MAX = 3, 24
TAG_MIN, TAG_MAX = 2, 5
DESC_MAX = 80
CREATE_LIMIT, CREATE_WINDOW = 2, 24 * 3600   # не больше двух новых кланов от одного игрока за сутки (защита от спама)
LIST_LIMIT = 100
EMOJIS = ("🛡️", "⚔️", "🏰", "🐉", "🦁", "🐺", "🦅", "🔥", "⚡", "🌟", "🚀", "🎯", "👑", "💎", "🌊", "🌲",
          "🦊", "🐻", "🐼", "🦄", "🍀", "☄️", "🎮", "🧠")
DEFAULT_EMOJI = EMOJIS[0]

_LETTERS = "A-Za-zА-Яа-яЁё0-9"
_NAME_RE = re.compile(rf"^[{_LETTERS}][{_LETTERS} _.\-]*[{_LETTERS}]$")
_TAG_RE = re.compile(rf"^[{_LETTERS}]+$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    tag: str = Field(min_length=1, max_length=20)
    emoji: str = Field(default=DEFAULT_EMOJI, max_length=16)
    description: str = Field(default="", max_length=400)


class UpdateIn(Strict):
    emoji: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=400)


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS clans(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, tag TEXT NOT NULL, tag_key TEXT NOT NULL UNIQUE,
          emoji TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', leader_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS clan_members(
          user_id INTEGER PRIMARY KEY, clan_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'member', joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_clan_members_clan ON clan_members(clan_id, joined_at);
        CREATE TABLE IF NOT EXISTS clan_creations(user_id INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_clan_creations_user ON clan_creations(user_id, created_at);
        """)


# ----------------------------------------------------------------------------- проверка ввода
def _key(s: str) -> str:
    return s.casefold().replace("ё", "е")


def clean_name(text: str) -> str:
    t = re.sub(r"\s+", " ", _CTRL.sub(" ", text or "")).strip()
    if not NAME_MIN <= len(t) <= NAME_MAX:
        raise HTTPException(400, f"Название клана — от {NAME_MIN} до {NAME_MAX} символов")
    if not _NAME_RE.match(t):
        raise HTTPException(400, "В названии можно использовать буквы, цифры, пробел и знаки - _ . (начинается и заканчивается буквой или цифрой)")
    return t


def clean_tag(text: str) -> str:
    t = (text or "").strip()
    if not TAG_MIN <= len(t) <= TAG_MAX:
        raise HTTPException(400, f"Тег клана — от {TAG_MIN} до {TAG_MAX} символов")
    if not _TAG_RE.match(t):
        raise HTTPException(400, "В теге можно использовать только буквы и цифры")
    return t.upper()


def clean_desc(text: str) -> str:
    t = re.sub(r"\s+", " ", _CTRL.sub(" ", text or "")).strip()
    if len(t) > DESC_MAX:
        raise HTTPException(400, f"Описание длиннее {DESC_MAX} символов")
    return t


def clean_emoji(text: str) -> str:
    if text not in EMOJIS:
        raise HTTPException(400, "Выбери эмодзи из списка")
    return text


# ----------------------------------------------------------------------------- выборки
_VISIBLE = "COALESCE(u.banned,0)=0 AND u.username<>?"


def _week_start() -> str:
    return (date.today() - timedelta(days=6)).isoformat()


def _clan(clan_id: int) -> dict:
    c = db.one("SELECT * FROM clans WHERE id=?", (clan_id,))
    if not c:
        raise HTTPException(404, "Клан не найден")
    return c


def _membership(user_id: int) -> dict | None:
    return db.one("SELECT * FROM clan_members WHERE user_id=?", (user_id,))


def of_user(user_id: int) -> dict | None:
    """Краткие данные клана игрока для профилей, списка друзей и чата (None — без клана)."""
    try:
        r = db.one("SELECT c.id, c.name, c.tag, c.emoji FROM clan_members m JOIN clans c ON c.id=m.clan_id WHERE m.user_id=?", (user_id,))
    except sqlite3.OperationalError:   # таблиц ещё нет (например, модуль вызвали до запуска сервера)
        return None
    return r


def _member_count(clan_id: int) -> int:
    return db.val(f"SELECT COUNT(*) FROM clan_members m JOIN users u ON u.id=m.user_id WHERE m.clan_id=? AND {_VISIBLE}", (clan_id, auth.SYSTEM_USERNAME), default=0)


def _stats() -> dict[int, dict]:
    """Участники и XP каждого клана (всего и за неделю) одним проходом."""
    stats: dict[int, dict] = {}
    for r in db.q(f"SELECT m.clan_id, COUNT(*) AS n FROM clan_members m JOIN users u ON u.id=m.user_id WHERE {_VISIBLE} GROUP BY m.clan_id", (auth.SYSTEM_USERNAME,)):
        stats[r["clan_id"]] = {"members": r["n"], "total_xp": 0, "week_xp": 0}
    for r in db.q(f"""SELECT m.clan_id, SUM(a.xp) AS total, SUM(CASE WHEN a.day>=? THEN a.xp ELSE 0 END) AS week
                      FROM clan_members m JOIN users u ON u.id=m.user_id JOIN activity a ON a.user_id=m.user_id
                      WHERE {_VISIBLE} GROUP BY m.clan_id""", (_week_start(), auth.SYSTEM_USERNAME)):
        s = stats.setdefault(r["clan_id"], {"members": 0, "total_xp": 0, "week_xp": 0})
        s["total_xp"], s["week_xp"] = r["total"] or 0, r["week"] or 0
    return stats


def _person(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"], "avatar": u["avatar"], "avatar_url": avatars.url(u["id"])}


def _card(c: dict, st: dict | None, rank: int | None) -> dict:
    st = st or {"members": 0, "total_xp": 0, "week_xp": 0}
    leader = db.one("SELECT * FROM users WHERE id=?", (c["leader_id"],))
    return {"id": c["id"], "name": c["name"], "tag": c["tag"], "emoji": c["emoji"], "description": c["description"], "created_at": c["created_at"],
            "members": st["members"], "max_members": MEMBERS_MAX, "week_xp": st["week_xp"], "total_xp": st["total_xp"], "rank": rank,
            "leader": _person(leader) if leader else None}


def _ranked() -> tuple[list[dict], dict[int, dict], dict[int, int]]:
    clans = db.q("SELECT * FROM clans")
    stats = _stats()
    empty = {"members": 0, "total_xp": 0, "week_xp": 0}
    clans.sort(key=lambda c: (-stats.get(c["id"], empty)["week_xp"], -stats.get(c["id"], empty)["total_xp"], -stats.get(c["id"], empty)["members"], c["id"]))
    return clans, stats, {c["id"]: i + 1 for i, c in enumerate(clans)}


def my_card(user_id: int) -> dict | None:
    m = _membership(user_id)
    if not m:
        return None
    clans, stats, ranks = _ranked()
    c = next((x for x in clans if x["id"] == m["clan_id"]), None)
    return _card(c, stats.get(c["id"]), ranks[c["id"]]) if c else None


def listing(user_id: int, q: str = "", sort: str = "week") -> dict:
    clans, stats, ranks = _ranked()
    q = _key(q.strip())
    if q:
        clans = [c for c in clans if q in _key(c["name"]) or q in _key(c["tag"])]
    empty = {"members": 0, "total_xp": 0, "week_xp": 0}
    if sort == "total":
        clans.sort(key=lambda c: (-stats.get(c["id"], empty)["total_xp"], c["id"]))
    elif sort == "members":
        clans.sort(key=lambda c: (-stats.get(c["id"], empty)["members"], -stats.get(c["id"], empty)["week_xp"], c["id"]))
    elif sort == "new":
        clans.sort(key=lambda c: -c["id"])
    m = _membership(user_id)
    return {"clans": [_card(c, stats.get(c["id"]), ranks[c["id"]]) for c in clans[:LIST_LIMIT]], "mine": m["clan_id"] if m else None,
            "can_create": m is None, "max_members": MEMBERS_MAX, "emojis": list(EMOJIS),
            "limits": {"name": [NAME_MIN, NAME_MAX], "tag": [TAG_MIN, TAG_MAX], "description": DESC_MAX}}


def detail(clan_id: int, user: dict) -> dict:
    c = _clan(clan_id)
    clans, stats, ranks = _ranked()
    week = _week_start()
    members = []
    for r in db.q(f"""SELECT u.*, m.role, m.joined_at FROM clan_members m JOIN users u ON u.id=m.user_id
                      WHERE m.clan_id=? AND {_VISIBLE} ORDER BY m.joined_at, u.id""", (clan_id, auth.SYSTEM_USERNAME)):
        xp = gm.total_xp(r["id"])
        members.append({**_person(r), "role": r["role"], "joined_at": r["joined_at"], "level": gm.level_info(xp)["level"], "xp": xp,
                        "week_xp": db.val("SELECT SUM(xp) FROM activity WHERE user_id=? AND day>=?", (r["id"], week), 0) or 0,
                        "streak": gm.streak_info(r["id"])["current"]})
    members.sort(key=lambda x: (-x["week_xp"], -x["xp"], x["id"]))
    mine = _membership(user["id"])
    is_leader = c["leader_id"] == user["id"]
    return {**_card(c, stats.get(clan_id), ranks.get(clan_id)), "members_list": members,
            "me": {"in_this": bool(mine and mine["clan_id"] == clan_id), "in_other": bool(mine and mine["clan_id"] != clan_id), "is_leader": is_leader,
                   "can_manage": is_leader or bool(user["is_admin"]), "is_admin": bool(user["is_admin"])},
            "emojis": list(EMOJIS), "limits": {"description": DESC_MAX}}


# ----------------------------------------------------------------------------- действия
def create(user: dict, name: str, tag: str, emoji: str, description: str) -> dict:
    name, tag, emoji, description = clean_name(name), clean_tag(tag), clean_emoji(emoji), clean_desc(description)
    with db.tx():
        m = _membership(user["id"])
        if m:
            raise HTTPException(400, "Ты уже состоишь в клане — сначала выйди из него")
        since = time.time() - CREATE_WINDOW
        if db.val("SELECT COUNT(*) FROM clan_creations WHERE user_id=? AND created_at>?", (user["id"], since), 0) >= CREATE_LIMIT:
            raise HTTPException(429, "Слишком много новых кланов за сутки — попробуй завтра")
        if db.one("SELECT 1 AS x FROM clans WHERE name_key=?", (_key(name),)):
            raise HTTPException(400, "Клан с таким названием уже есть")
        if db.one("SELECT 1 AS x FROM clans WHERE tag_key=?", (_key(tag),)):
            raise HTTPException(400, "Такой тег уже занят")
        cid = db.x("INSERT INTO clans(name, name_key, tag, tag_key, emoji, description, leader_id) VALUES(?,?,?,?,?,?,?)",
                   (name, _key(name), tag, _key(tag), emoji, description, user["id"]))
        db.x("INSERT INTO clan_members(user_id, clan_id, role) VALUES(?,?, 'leader')", (user["id"], cid))
        db.x("INSERT INTO clan_creations(user_id, created_at) VALUES(?,?)", (user["id"], time.time()))
    return detail(cid, user)


def join(user: dict, clan_id: int) -> dict:
    with db.tx():
        c = _clan(clan_id)
        m = _membership(user["id"])
        if m:
            raise HTTPException(400, "Ты уже в этом клане" if m["clan_id"] == clan_id else "Ты уже состоишь в клане — сначала выйди из него")
        if _member_count(clan_id) >= MEMBERS_MAX:
            raise HTTPException(400, f"В клане уже {MEMBERS_MAX} участников — он заполнен")
        db.x("INSERT INTO clan_members(user_id, clan_id, role) VALUES(?,?, 'member')", (user["id"], c["id"]))
    return detail(clan_id, user)


def _disband(clan_id: int):
    db.x("DELETE FROM clan_members WHERE clan_id=?", (clan_id,))
    db.x("DELETE FROM clans WHERE id=?", (clan_id,))


def _leave(user_id: int) -> bool:
    """Убрать игрока из клана (внутри транзакции). True — клан распущен, потому что остаться больше некому."""
    m = _membership(user_id)
    if not m:
        raise HTTPException(400, "Ты не состоишь в клане")
    c = db.one("SELECT * FROM clans WHERE id=?", (m["clan_id"],))
    db.x("DELETE FROM clan_members WHERE user_id=?", (user_id,))
    if not c:
        return False
    if c["leader_id"] == user_id:
        heir = db.one(f"""SELECT u.id FROM clan_members m JOIN users u ON u.id=m.user_id WHERE m.clan_id=? AND {_VISIBLE}
                          ORDER BY m.joined_at, u.id LIMIT 1""", (c["id"], auth.SYSTEM_USERNAME))
        if not heir:
            _disband(c["id"])
            return True
        db.x("UPDATE clans SET leader_id=? WHERE id=?", (heir["id"], c["id"]))
        db.x("UPDATE clan_members SET role='leader' WHERE user_id=?", (heir["id"],))
    return False


def leave(user: dict) -> dict:
    with db.tx():
        disbanded = _leave(user["id"])
    return {"ok": True, "disbanded": disbanded}


def _manage(user: dict, clan_id: int) -> dict:
    c = _clan(clan_id)
    if c["leader_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это может сделать только лидер клана")
    return c


def kick(user: dict, clan_id: int, target_id: int) -> dict:
    with db.tx():
        c = _manage(user, clan_id)
        if target_id == c["leader_id"]:
            raise HTTPException(400, "Лидера исключить нельзя — он может передать лидерство или выйти сам")
        if not db.one("SELECT 1 AS x FROM clan_members WHERE user_id=? AND clan_id=?", (target_id, clan_id)):
            raise HTTPException(404, "Такого участника в клане нет")
        db.x("DELETE FROM clan_members WHERE user_id=?", (target_id,))
    return detail(clan_id, user)


def transfer(user: dict, clan_id: int, target_id: int) -> dict:
    with db.tx():
        c = _clan(clan_id)
        if c["leader_id"] != user["id"]:
            raise HTTPException(403, "Передать лидерство может только лидер")
        if target_id == user["id"]:
            raise HTTPException(400, "Ты уже лидер")
        t = db.one(f"SELECT u.id FROM clan_members m JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND m.clan_id=? AND {_VISIBLE}",
                   (target_id, clan_id, auth.SYSTEM_USERNAME))
        if not t:
            raise HTTPException(404, "Такого участника в клане нет")
        db.x("UPDATE clans SET leader_id=? WHERE id=?", (target_id, clan_id))
        db.x("UPDATE clan_members SET role='member' WHERE user_id=?", (user["id"],))
        db.x("UPDATE clan_members SET role='leader' WHERE user_id=?", (target_id,))
    return detail(clan_id, user)


def update(user: dict, clan_id: int, emoji: str | None, description: str | None) -> dict:
    with db.tx():
        _manage(user, clan_id)
        if emoji is not None:
            db.x("UPDATE clans SET emoji=? WHERE id=?", (clean_emoji(emoji), clan_id))
        if description is not None:
            db.x("UPDATE clans SET description=? WHERE id=?", (clean_desc(description), clan_id))
    return detail(clan_id, user)


def disband(user: dict, clan_id: int) -> dict:
    with db.tx():
        _manage(user, clan_id)
        _disband(clan_id)
    return {"ok": True}


def delete_user_data(user_id: int):
    """При удалении аккаунта: игрок уходит из клана (лидерство переходит дальше, пустой клан исчезает), записи о созданиях стираются."""
    with db.tx():
        if _membership(user_id):
            _leave(user_id)
        db.x("DELETE FROM clan_creations WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("")
async def api_list(request: Request, q: str = Query(default="", max_length=40), sort: str = Query(default="week", pattern="^(week|total|members|new)$")):
    return await run_in_threadpool(listing, _me(request)["id"], q, sort)


@router.get("/mine")
async def api_mine(request: Request):
    u = _me(request)
    m = await run_in_threadpool(_membership, u["id"])
    return {"clan": await run_in_threadpool(detail, m["clan_id"], u) if m else None}


@router.post("")
async def api_create(request: Request, body: CreateIn):
    return await run_in_threadpool(create, _me(request), body.name, body.tag, body.emoji, body.description)


@router.post("/leave")
async def api_leave(request: Request):
    return await run_in_threadpool(leave, _me(request))


@router.get("/{clan_id}")
async def api_detail(request: Request, clan_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(detail, clan_id, _me(request))


@router.post("/{clan_id}/join")
async def api_join(request: Request, clan_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(join, _me(request), clan_id)


@router.put("/{clan_id}")
async def api_update(request: Request, body: UpdateIn, clan_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(update, _me(request), clan_id, body.emoji, body.description)


@router.delete("/{clan_id}")
async def api_disband(request: Request, clan_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(disband, _me(request), clan_id)


@router.post("/{clan_id}/kick/{user_id}")
async def api_kick(request: Request, clan_id: int = Path(ge=1, le=10**9), user_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(kick, _me(request), clan_id, user_id)


@router.post("/{clan_id}/leader/{user_id}")
async def api_transfer(request: Request, clan_id: int = Path(ge=1, le=10**9), user_id: int = Path(ge=1, le=10**9)):
    return await run_in_threadpool(transfer, _me(request), clan_id, user_id)
