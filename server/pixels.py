"""Пиксел баттл: общая доска 128×128, как r/place, для всей компании.

Каждый игрок ставит один пиксел раз в минуту (пикселы копятся до 5, пока не заходишь) или докупает лишний за жетоны. Доска и журнал живут в SQLite:
доска — только занятые клетки (x, y, цвет, кто и когда), журнал — каждое действие (для обновления у остальных, спора «кто тут рисовал» и будущего таймлапса).
Всё проверяет сервер: лимит времени, границы доски, цвет, баланс жетонов. Пикселы копятся лениво (по времени последнего учёта), фоновых задач нет.
"""
import base64
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth, avatars, config, slots
from .db import db

router = APIRouter(prefix="/api/pixels", tags=["Пиксел баттл"])

W = H = 128
EMPTY = 255
# 16 цветов палитры (индекс = номер цвета на доске)
PALETTE = ("#ffffff", "#e4e4e4", "#888888", "#222222", "#ffa7d1", "#e50000", "#e59500", "#a06a42",
           "#e5d900", "#94e044", "#02be01", "#00d3dd", "#0083c7", "#0000ea", "#cf6ee4", "#820080")
MAX_CHARGES = 5          # сколько пикселов можно накопить
START_CHARGES = 1
BUY_PRICE = 20           # жетонов за один дополнительный пиксел
CHANGES_LIMIT = 5000
HISTORY_LEN = 5


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceIn(Strict):
    x: int = Field(ge=0, lt=W)
    y: int = Field(ge=0, lt=H)
    color: int = Field(ge=0, lt=len(PALETTE))


class ClearIn(Strict):
    x1: int = Field(ge=0, lt=W)
    y1: int = Field(ge=0, lt=H)
    x2: int = Field(ge=0, lt=W)
    y2: int = Field(ge=0, lt=H)


def cooldown() -> float:
    return max(0.0, float(config.env("PIXELS_COOLDOWN", "60") or 60))


def init_schema():
    with db.lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS pixel_board(
          x INTEGER NOT NULL, y INTEGER NOT NULL, color INTEGER NOT NULL, user_id INTEGER, placed_at REAL NOT NULL,
          PRIMARY KEY(x, y)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS ix_pixel_board_user ON pixel_board(user_id);
        CREATE TABLE IF NOT EXISTS pixel_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, x INTEGER NOT NULL, y INTEGER NOT NULL,
          color INTEGER NOT NULL, prev_color INTEGER NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_pixel_log_cell ON pixel_log(x, y, id DESC);
        CREATE INDEX IF NOT EXISTS ix_pixel_log_user ON pixel_log(user_id);
        CREATE TABLE IF NOT EXISTS pixel_users(
          user_id INTEGER PRIMARY KEY, charges INTEGER NOT NULL, charge_at REAL NOT NULL
        );
        """)
        db.conn.commit()


# ----------------------------------------------------------------------------- пикселы игрока (копятся по времени)
def _account(user_id: int, now: float) -> dict:
    db.x("INSERT OR IGNORE INTO pixel_users(user_id, charges, charge_at) VALUES(?,?,?)", (user_id, START_CHARGES, now))
    row = db.one("SELECT * FROM pixel_users WHERE user_id=?", (user_id,))
    return _regen(row, now)


def _regen(row: dict, now: float) -> dict:
    cd, c, at = cooldown(), row["charges"], row["charge_at"]
    if c >= MAX_CHARGES:
        return {"user_id": row["user_id"], "charges": MAX_CHARGES, "charge_at": now}
    k = int((now - at) // cd) if cd > 0 else MAX_CHARGES
    c2 = min(MAX_CHARGES, c + max(0, k))
    at2 = now if c2 >= MAX_CHARGES else at + max(0, k) * cd
    return {"user_id": row["user_id"], "charges": c2, "charge_at": at2}


def _next_in(acc: dict, now: float) -> float:
    if acc["charges"] >= MAX_CHARGES:
        return 0.0
    return round(max(0.0, cooldown() - (now - acc["charge_at"])), 1)


def _save(acc: dict):
    db.x("UPDATE pixel_users SET charges=?, charge_at=? WHERE user_id=?", (acc["charges"], acc["charge_at"], acc["user_id"]))


def _me_view(acc: dict, now: float) -> dict:
    return {"charges": acc["charges"], "max_charges": MAX_CHARGES, "next_in": _next_in(acc, now), "cooldown": cooldown(), "price": BUY_PRICE}


# ----------------------------------------------------------------------------- доска
def board_bytes() -> bytes:
    buf = bytearray([EMPTY]) * (W * H)
    for r in db.q("SELECT x, y, color FROM pixel_board"):
        buf[r["y"] * W + r["x"]] = r["color"]
    return bytes(buf)


def last_id() -> int:
    return db.val("SELECT COALESCE(MAX(id), 0) FROM pixel_log") or 0


def get_state(user_id: int) -> dict:
    now = time.time()
    with db.tx():
        acc = _account(user_id, now)
        _save(acc)
        bal = slots.ensure_account(user_id)["balance"]
        lid = last_id()
        board = board_bytes()
    out = {"w": W, "h": H, "palette": list(PALETTE), "empty": EMPTY, "board": base64.b64encode(board).decode(), "last_id": lid, "balance": bal}
    out.update(_me_view(acc, now))
    return out


def changes(after: int) -> dict:
    after = min(max(0, int(after)), 2**62)
    rows = db.q("SELECT id, x, y, color, user_id FROM pixel_log WHERE id>? ORDER BY id LIMIT ?", (after, CHANGES_LIMIT + 1))
    more = len(rows) > CHANGES_LIMIT
    rows = rows[:CHANGES_LIMIT]
    return {"changes": [{"id": r["id"], "x": r["x"], "y": r["y"], "c": r["color"], "u": r["user_id"]} for r in rows], "more": more,
            "last_id": rows[-1]["id"] if rows else after}


def place(user_id: int, x: int, y: int, color: int) -> dict:
    now = time.time()
    with db.tx():  # параллельные запросы одного игрока выстраиваются в очередь, пиксел не потратить дважды
        acc = _account(user_id, now)
        if acc["charges"] < 1:
            _save(acc)
            raise _too_fast(_next_in(acc, now))
        cur = db.one("SELECT color FROM pixel_board WHERE x=? AND y=?", (x, y))
        if cur and cur["color"] == color:
            _save(acc)
            raise HTTPException(400, "Здесь уже этот цвет")
        was_full = acc["charges"] >= MAX_CHARGES
        acc["charges"] -= 1
        if was_full:
            acc["charge_at"] = now  # таймер пошёл с момента, когда накопление перестало быть полным
        _save(acc)
        prev = cur["color"] if cur else EMPTY
        db.x("INSERT OR REPLACE INTO pixel_board(x, y, color, user_id, placed_at) VALUES(?,?,?,?,?)", (x, y, color, user_id, now))
        lid = db.x("INSERT INTO pixel_log(user_id, x, y, color, prev_color, created_at) VALUES(?,?,?,?,?,?)", (user_id, x, y, color, prev, now))
    out = {"id": lid, "x": x, "y": y, "color": color}
    out.update(_me_view(acc, now))
    return out


def _too_fast(wait: float) -> HTTPException:
    return HTTPException(429, f"Пиксел появится через {int(wait) + 1} с", headers={"Retry-After": str(int(wait) + 1)})


def buy(user_id: int) -> dict:
    now = time.time()
    with db.tx():
        acc = _account(user_id, now)
        if acc["charges"] >= MAX_CHARGES:
            _save(acc)
            raise HTTPException(400, f"У тебя уже полный запас — {MAX_CHARGES} пикселов")
        sa = slots.ensure_account(user_id)
        if sa["balance"] < BUY_PRICE:
            _save(acc)
            raise HTTPException(400, "Недостаточно жетонов")
        db.x("UPDATE slot_accounts SET balance=balance-?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (BUY_PRICE, user_id))
        acc["charges"] += 1
        if acc["charges"] >= MAX_CHARGES:
            acc["charge_at"] = now
        _save(acc)
    out = {"balance": sa["balance"] - BUY_PRICE}
    out.update(_me_view(acc, now))
    return out


def _person(r: dict | None) -> dict | None:
    if not r or r.get("user_id") is None:
        return None
    return {"user_id": r["user_id"], "display_name": r["display_name"] or r["username"], "avatar": r["avatar"], "avatar_url": avatars.url(r["user_id"])}


def cell_info(x: int, y: int) -> dict:
    if not (0 <= x < W and 0 <= y < H):
        raise HTTPException(400, "Клетка вне доски")
    cur = db.one("""SELECT b.color, b.placed_at, b.user_id, u.display_name, u.username, u.avatar FROM pixel_board b
                    LEFT JOIN users u ON u.id=b.user_id WHERE b.x=? AND b.y=?""", (x, y))
    hist = db.q("""SELECT l.id, l.color, l.created_at, l.user_id, u.display_name, u.username, u.avatar FROM pixel_log l
                   LEFT JOIN users u ON u.id=l.user_id WHERE l.x=? AND l.y=? ORDER BY l.id DESC LIMIT ?""", (x, y, HISTORY_LEN))
    total = db.val("SELECT COUNT(*) FROM pixel_log WHERE x=? AND y=?", (x, y)) or 0
    return {"x": x, "y": y, "color": cur["color"] if cur else EMPTY, "at": cur["placed_at"] if cur else None, "by": _person(cur) if cur else None,
            "total": total, "history": [{"id": h["id"], "color": h["color"], "at": h["created_at"], "by": _person(h)} for h in hist]}


def top() -> list[dict]:
    rows = db.q("""SELECT b.user_id, COUNT(*) n, u.display_name, u.username, u.avatar FROM pixel_board b JOIN users u ON u.id=b.user_id
                   WHERE COALESCE(u.banned,0)=0 AND u.username<>? AND b.color<>? GROUP BY b.user_id ORDER BY n DESC, b.user_id LIMIT 10""",
                (auth.SYSTEM_USERNAME, EMPTY))
    return [dict(_person(r), pixels=r["n"]) for r in rows]


def stats() -> dict:
    return {"painted": db.val("SELECT COUNT(*) FROM pixel_board") or 0, "total": W * H, "moves": last_id()}


def public_stats(user_id: int) -> dict | None:
    placed = db.val("SELECT COUNT(*) FROM pixel_log WHERE user_id=?", (user_id,)) or 0
    if not placed:
        return None
    return {"placed": placed, "owned": db.val("SELECT COUNT(*) FROM pixel_board WHERE user_id=?", (user_id,)) or 0}


def admin_clear(x1: int, y1: int, x2: int, y2: int) -> dict:
    x1, x2, y1, y2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    now = time.time()
    with db.tx():
        rows = db.q("SELECT x, y, color FROM pixel_board WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?", (x1, x2, y1, y2))
        for r in rows:
            db.x("DELETE FROM pixel_board WHERE x=? AND y=?", (r["x"], r["y"]))
            db.x("INSERT INTO pixel_log(user_id, x, y, color, prev_color, created_at) VALUES(NULL,?,?,?,?,?)", (r["x"], r["y"], EMPTY, r["color"], now))
    return {"cleared": len(rows)}


def delete_user_data(user_id: int):
    """Нарисованное остаётся на доске (картину не ломаем), но становится безымянным."""
    db.x("UPDATE pixel_board SET user_id=NULL WHERE user_id=?", (user_id,))
    db.x("UPDATE pixel_log SET user_id=NULL WHERE user_id=?", (user_id,))
    db.x("DELETE FROM pixel_users WHERE user_id=?", (user_id,))


# ----------------------------------------------------------------------------- HTTP
def _me(request: Request) -> dict:
    return auth.require_user(request)


@router.get("/state")
async def api_state(request: Request):
    return await run_in_threadpool(get_state, _me(request)["id"])


@router.get("/changes")
async def api_changes(request: Request, after: int = 0):
    _me(request)
    return await run_in_threadpool(changes, after)


@router.get("/cell")
async def api_cell(request: Request, x: int, y: int):
    _me(request)
    return await run_in_threadpool(cell_info, x, y)


@router.get("/top")
async def api_top(request: Request):
    _me(request)
    out = await run_in_threadpool(top)
    return {"top": out, "stats": await run_in_threadpool(stats)}


@router.post("/place")
async def api_place(request: Request, body: PlaceIn):
    return await run_in_threadpool(place, _me(request)["id"], body.x, body.y, body.color)


@router.post("/buy")
async def api_buy(request: Request):
    return await run_in_threadpool(buy, _me(request)["id"])


@router.post("/admin/clear")
async def api_clear(request: Request, body: ClearIn):
    if not _me(request)["is_admin"]:
        return JSONResponse({"detail": "Только для администратора"}, status_code=403)
    return await run_in_threadpool(admin_clear, body.x1, body.y1, body.x2, body.y2)
