"""Слоты «Лапа удачи»: собачий автомат на виртуальных жетонах (не деньги — ни пополнения, ни вывода, ни обмена).

Вся игра считается здесь, на сервере: фронтенд получает готовый результат и только показывает его.
Формат: 5 барабанов × 3 ряда, 10 фиксированных линий выплат, WILD заменяет любые символы кроме SCATTER и BONUS,
SCATTER платит за любое расположение, BONUS (будка) даёт мгновенный бонусный приз.

Каждый пользователь имеет свой слот-аккаунт (баланс, ежедневный бонус, статистика, история). Изменение баланса и запись в историю —
одна транзакция; повторный запрос с тем же request_id возвращает уже посчитанный результат и ничего не списывает второй раз.
"""
import json
import random
import secrets
import time
from datetime import date, timedelta

from fastapi import HTTPException

from . import auth, config
from . import gamification as gm
from .db import db

# ----------------------------------------------------------------------------- настройки (позже легко вынести в конфиг)
BETS = (10, 20, 50)           # кратны числу линий
START_BALANCE = 1000
DAILY_BONUS = 100
REELS, ROWS = 5, 3
HISTORY_LIMIT = 50
LEADERBOARD_SIZE = 10

WILD, SCATTER, BONUS = "wild", "scatter", "bonus"
DOGS = ("doberman", "bulldog", "pug", "corgi", "chihuahua")
ITEMS = ("bone", "collar", "bowl", "ball")
SYMBOLS = DOGS + ITEMS + (WILD, SCATTER, BONUS)
NAMES = {"doberman": "Доберман", "bulldog": "Бульдог", "pug": "Мопс", "corgi": "Корги", "chihuahua": "Чихуахуа", "bone": "Косточка",
         "collar": "Ошейник", "bowl": "Миска", "ball": "Мячик", "wild": "WILD", "scatter": "SCATTER", "bonus": "BONUS"}

# 10 линий: номер ряда (0 — верх) на каждом из 5 барабанов
LINES = (
    (1, 1, 1, 1, 1), (0, 0, 0, 0, 0), (2, 2, 2, 2, 2), (0, 1, 2, 1, 0), (2, 1, 0, 1, 2),
    (0, 0, 1, 2, 2), (2, 2, 1, 0, 0), (1, 0, 0, 0, 1), (1, 2, 2, 2, 1), (0, 1, 1, 1, 0),
)
# выплаты линии в долях ставки на линию (ставка / число линий) за 3, 4 и 5 одинаковых подряд слева направо
PAYS = {
    "doberman": (100, 300, 1000), "bulldog": (75, 200, 750), "pug": (50, 150, 500), "corgi": (40, 100, 400), "chihuahua": (30, 75, 300),
    "bone": (20, 50, 150), "collar": (15, 40, 125), "bowl": (10, 30, 100), "ball": (10, 25, 75),
    WILD: (125, 500, 2500),
}
SCATTER_PAYS = {3: 1, 4: 5, 5: 25}              # в общих ставках, где бы ни стояли символы
BONUS_PRIZES = ((1, 40), (3, 30), (8, 18), (20, 9), (60, 3))  # (множитель общей ставки, вес); 4 и 5 будок умножают приз на 2 и 5
BONUS_COUNT_MULT = {3: 1, 4: 2, 5: 5}
TIERS = ((50, "jackpot"), (15, "mega"), (5, "big"), (0.0001, "small"))  # по отношению выплаты к ставке

# барабанные ленты: сколько раз символ встречается на каждом из 5 барабанов (порядок перемешан детерминированно)
STRIP_COUNTS = (
    {"doberman": 2, "bulldog": 3, "pug": 3, "corgi": 4, "chihuahua": 4, "bone": 5, "collar": 5, "bowl": 5, "ball": 5, WILD: 0, SCATTER: 2, BONUS: 0},
    {"doberman": 2, "bulldog": 3, "pug": 3, "corgi": 4, "chihuahua": 4, "bone": 5, "collar": 4, "bowl": 4, "ball": 4, WILD: 2, SCATTER: 2, BONUS: 2},
    {"doberman": 2, "bulldog": 3, "pug": 3, "corgi": 4, "chihuahua": 4, "bone": 5, "collar": 4, "bowl": 4, "ball": 4, WILD: 2, SCATTER: 2, BONUS: 2},
    {"doberman": 2, "bulldog": 3, "pug": 3, "corgi": 4, "chihuahua": 4, "bone": 5, "collar": 4, "bowl": 4, "ball": 4, WILD: 2, SCATTER: 2, BONUS: 2},
    {"doberman": 2, "bulldog": 3, "pug": 3, "corgi": 4, "chihuahua": 4, "bone": 5, "collar": 5, "bowl": 5, "ball": 5, WILD: 0, SCATTER: 2, BONUS: 0},
)


def _build_strips() -> list[list[str]]:
    rnd = random.Random(20261010)  # фиксированный порядок: ленты одинаковы при каждом запуске
    strips = []
    for counts in STRIP_COUNTS:
        strip = [s for s, n in counts.items() for _ in range(n)]
        for _ in range(200):
            rnd.shuffle(strip)
            if all(strip[i] != strip[(i + 1) % len(strip)] for i in range(len(strip))):  # одинаковые символы рядом не стоят
                break
        strips.append(strip)
    return strips


STRIPS = _build_strips()
_rng = secrets.SystemRandom()  # криптостойкий генератор: результат нельзя предсказать по предыдущим вращениям
_last_spin: dict[int, float] = {}


# ----------------------------------------------------------------------------- игровая логика (чистые функции)
def roll(rng=None) -> list[list[str]]:
    """Крутит барабаны. Возвращает сетку grid[барабан][ряд]."""
    rng = rng or _rng
    grid = []
    for strip in STRIPS:
        stop = rng.randrange(len(strip))
        grid.append([strip[(stop + r) % len(strip)] for r in range(ROWS)])
    return grid


def tier_of(payout: int, bet: int) -> str:
    if payout <= 0:
        return "none"
    ratio = payout / bet
    for threshold, name in TIERS:
        if ratio >= threshold:
            return name
    return "small"


def evaluate(grid: list[list[str]], bet: int, rng=None) -> dict:
    """Считает выплаты по сетке. bet кратна числу линий."""
    rng = rng or _rng
    line_bet = bet // len(LINES)
    wins, payout = [], 0
    for li, rows in enumerate(LINES):
        seq = [grid[c][rows[c]] for c in range(REELS)]
        base = next((s for s in seq if s != WILD), WILD)
        best = None
        if base not in (SCATTER, BONUS):
            n = 0
            for s in seq:
                if s == base or s == WILD:
                    n += 1
                else:
                    break
            if n >= 3:
                best = (base, n, PAYS[base][n - 3] * line_bet)
        w = 0
        while w < REELS and seq[w] == WILD:
            w += 1
        if w >= 3 and (best is None or PAYS[WILD][w - 3] * line_bet > best[2]):
            best = (WILD, w, PAYS[WILD][w - 3] * line_bet)
        if best:
            sym, n, pay = best
            wins.append({"line": li, "symbol": sym, "count": n, "pay": pay, "positions": [[c, rows[c]] for c in range(n)]})
            payout += pay
    out = {"lines": wins, "scatter": None, "bonus": None}
    sc = [[c, r] for c in range(REELS) for r in range(ROWS) if grid[c][r] == SCATTER]
    if len(sc) >= 3:
        pay = SCATTER_PAYS[min(len(sc), 5)] * bet
        out["scatter"] = {"count": len(sc), "pay": pay, "positions": sc}
        payout += pay
    bn = [[c, r] for c in range(REELS) for r in range(ROWS) if grid[c][r] == BONUS]
    if len(bn) >= 3:
        mult = rng.choices([m for m, _ in BONUS_PRIZES], weights=[w for _, w in BONUS_PRIZES])[0]
        prize = mult * BONUS_COUNT_MULT[min(len(bn), 5)] * bet
        out["bonus"] = {"count": len(bn), "prize": prize, "positions": bn}
        payout += prize
    out["payout"] = payout
    out["tier"] = tier_of(payout, bet)
    return out


def play(bet: int, rng=None) -> tuple[list[list[str]], dict]:
    grid = roll(rng)
    return grid, evaluate(grid, bet, rng)


# ----------------------------------------------------------------------------- аккаунты, ежедневный бонус, вращение
def ensure_account(user_id: int) -> dict:
    """Слот-аккаунт создаётся при первом обращении (ленивая инициализация)."""
    db.x("INSERT OR IGNORE INTO slot_accounts(user_id, balance) VALUES(?,?)", (user_id, START_BALANCE))
    return db.one("SELECT * FROM slot_accounts WHERE user_id=?", (user_id,))


def _next_daily_day() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def can_claim_daily(acc: dict) -> bool:
    return acc["last_daily_day"] != gm.today()


def claim_daily(user_id: int) -> dict:
    with db.tx():
        acc = ensure_account(user_id)
        if not can_claim_daily(acc):
            raise HTTPException(400, "Сегодняшний бонус уже получен — приходи завтра")
        db.x("UPDATE slot_accounts SET balance=balance+?, last_daily_day=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
             (DAILY_BONUS, gm.today(), user_id))
    return {"amount": DAILY_BONUS, "balance": acc["balance"] + DAILY_BONUS, "next_day": _next_daily_day()}


def _spin_view(row: dict) -> dict:
    ev = json.loads(row["lines"] or "{}")
    return {"id": row["id"], "bet": row["bet"], "reels": json.loads(row["reels"]), "lines": ev.get("lines", []), "scatter": ev.get("scatter"),
            "bonus": ev.get("bonus"), "payout": row["payout"], "net": row["net"], "balance": row["balance_after"], "tier": row["win_tier"],
            "created_at": row["created_at"]}


def spin(user_id: int, bet: int, request_id: str | None = None) -> dict:
    if bet not in BETS:
        raise HTTPException(400, "Недопустимая ставка. Доступно: " + ", ".join(map(str, BETS)))
    min_gap = float(config.env("SLOTS_MIN_INTERVAL", "0.4") or 0)
    now = time.monotonic()
    with db.tx():  # блокировка БД: параллельные вращения одного пользователя выстраиваются в очередь
        acc = ensure_account(user_id)
        if request_id:
            old = db.one("SELECT * FROM slot_spins WHERE user_id=? AND request_id=?", (user_id, request_id))
            if old:
                out = _spin_view(old)
                out["replayed"] = True  # повтор из-за сбоя сети: возвращаем прежний результат без нового списания
                return out
        if now - _last_spin.get(user_id, -1e9) < min_gap:
            raise HTTPException(429, "Не так быстро — дождись окончания вращения")
        if acc["balance"] < bet:
            raise HTTPException(400, "Недостаточно жетонов для такой ставки")
        grid, ev = play(bet)
        payout = ev["payout"]
        balance = acc["balance"] - bet + payout
        db.x("""UPDATE slot_accounts SET balance=?, spins_total=spins_total+1, won_total=won_total+?, wagered_total=wagered_total+?,
                wins_count=wins_count+?, best_win=MAX(best_win, ?), updated_at=CURRENT_TIMESTAMP WHERE user_id=?""",
             (balance, payout, bet, 1 if payout > 0 else 0, payout, user_id))
        sid = db.x("INSERT INTO slot_spins(user_id, request_id, bet, reels, lines, payout, net, balance_after, win_tier) VALUES(?,?,?,?,?,?,?,?,?)",
                   (user_id, request_id, bet, json.dumps(grid), json.dumps({"lines": ev["lines"], "scatter": ev["scatter"], "bonus": ev["bonus"]}, ensure_ascii=False),
                    payout, payout - bet, balance, ev["tier"]))
        _last_spin[user_id] = now
        row = db.one("SELECT * FROM slot_spins WHERE id=?", (sid,))
    out = _spin_view(row)
    out["replayed"] = False
    return out


def history(user_id: int, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), HISTORY_LIMIT))
    return [_spin_view(r) for r in db.q("SELECT * FROM slot_spins WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]


def stats_of(acc: dict) -> dict:
    spins = acc["spins_total"]
    return {"spins": spins, "won_total": acc["won_total"], "wagered_total": acc["wagered_total"], "best_win": acc["best_win"],
            "win_rate": round(acc["wins_count"] / spins, 3) if spins else 0.0}


def meta() -> dict:
    """Описание автомата для фронтенда (названия, выплаты, линии), чтобы не дублировать числа на клиенте."""
    return {"symbols": [{"id": s, "name": NAMES[s], "pays": PAYS.get(s)} for s in SYMBOLS], "lines": [list(l) for l in LINES],
            "scatter_pays": SCATTER_PAYS, "bonus_count_mult": BONUS_COUNT_MULT, "reels": REELS, "rows": ROWS}


def get_state(user_id: int) -> dict:
    with db.tx():
        acc = ensure_account(user_id)
    return {"balance": acc["balance"], "bets": list(BETS), "min_bet": min(BETS),
            "daily": {"available": can_claim_daily(acc), "amount": DAILY_BONUS, "next_day": _next_daily_day()},
            "stats": stats_of(acc), "history": history(user_id, 10), "meta": meta()}


def leaderboard() -> list[dict]:
    """Таблица лучших выигрышей: только имя и аватарка, без деталей вращений."""
    out = []
    rows = db.q("""SELECT a.user_id, a.best_win, a.won_total, a.spins_total, u.display_name, u.username, u.avatar FROM slot_accounts a
                   JOIN users u ON u.id=a.user_id WHERE a.best_win>0 AND COALESCE(u.banned,0)=0 AND u.username<>?
                   ORDER BY a.best_win DESC, a.won_total DESC LIMIT ?""", (auth.SYSTEM_USERNAME, LEADERBOARD_SIZE))
    from . import avatars
    for r in rows:
        out.append({"user_id": r["user_id"], "display_name": r["display_name"] or r["username"], "avatar": r["avatar"], "avatar_url": avatars.url(r["user_id"]),
                    "best_win": r["best_win"], "won_total": r["won_total"], "spins": r["spins_total"]})
    return out


def delete_user_slot_data(user_id: int):
    db.x("DELETE FROM slot_spins WHERE user_id=?", (user_id,))
    db.x("DELETE FROM slot_accounts WHERE user_id=?", (user_id,))
    _last_spin.pop(user_id, None)
