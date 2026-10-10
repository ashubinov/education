"""Слоты: правила выплат (на заданных сетках), отдача, API, идемпотентность, гонки, изоляция пользователей, удаление данных.
Запуск (чистый сервер, LQ_DB — путь к его базе, SLOTS_MIN_INTERVAL=0): LQ_DB=<tmp.db> python tests/test_slots.py http://127.0.0.1:8011"""
import os
import random
import sqlite3
import sys
import threading
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from fastapi import HTTPException  # noqa: E402

from server import slots  # noqa: E402

S = slots
BET = 20
LB = BET // len(S.LINES)  # ставка на линию


def blank():
    """Сетка без единой выигрышной комбинации."""
    return [[S.ITEMS[(c + r * 2) % 4] for r in range(S.ROWS)] for c in range(S.REELS)]


def lp(ev, line_idx):
    """Выплата именно по этой линии (соседние линии с wild могут давать свои выигрыши, они нас здесь не интересуют)."""
    return sum(w["pay"] for w in ev["lines"] if w["line"] == line_idx)


def with_line(line_idx, symbols):
    g = blank()
    for c, sym in enumerate(symbols):
        g[c][S.LINES[line_idx][c]] = sym
    return g


# ---------- 1. правила выплат ----------
assert S.evaluate(blank(), BET)["payout"] == 0, "контрольная сетка не должна выигрывать"
x = "bone"
ev = S.evaluate(with_line(0, ["bulldog", "bulldog", "bulldog", x, "ball"]), BET)
assert ev["payout"] == S.PAYS["bulldog"][0] * LB and ev["lines"][0]["count"] == 3 and ev["lines"][0]["positions"] == [[0, 1], [1, 1], [2, 1]], ev  # без wild — других линий нет
ev = S.evaluate(with_line(1, ["pug"] * 4 + [x]), BET)
assert ev["payout"] == S.PAYS["pug"][1] * LB and ev["lines"][0]["line"] == 1
ev = S.evaluate(with_line(2, ["corgi"] * 5), BET)
assert ev["payout"] == S.PAYS["corgi"][2] * LB
# wild заменяет обычные символы, в том числе первым
ev = S.evaluate(with_line(0, ["wild", "bulldog", "bulldog", x, "ball"]), BET)
assert lp(ev, 0) == S.PAYS["bulldog"][0] * LB, ev
ev = S.evaluate(with_line(0, ["wild", "wild", "pug", "wild", "pug"]), BET)
assert lp(ev, 0) == S.PAYS["pug"][2] * LB, ev
ev = S.evaluate(with_line(0, ["wild"] * 5), BET)
assert lp(ev, 0) == S.PAYS["wild"][2] * LB and [w for w in ev["lines"] if w["line"] == 0][0]["symbol"] == "wild"
# wild не заменяет scatter и bonus; они не платят по линии
ev = S.evaluate(with_line(0, ["scatter", "wild", "wild", "wild", "ball"]), BET)
assert lp(ev, 0) == 0, ev  # линия, начатая со scatter, не платит: wild его не заменяет
ev = S.evaluate(with_line(0, ["bonus", "bonus", "wild", "wild", "wild"]), BET)
assert lp(ev, 0) == 0 and ev["bonus"] is None, ev
# разрыв комбинации и счёт слева направо
ev = S.evaluate(with_line(0, ["pug", "pug", x, "pug", "pug"]), BET)
assert ev["payout"] == 0, ev  # две подряд — не комбинация
ev = S.evaluate(with_line(0, [x, "pug", "pug", "pug", "pug"]), BET)
assert lp(ev, 0) == 0, "комбинация должна начинаться с первого барабана"
# scatter: за любое расположение, не по линиям
g = blank()
for c, r in ((0, 0), (2, 2), (4, 1)):
    g[c][r] = "scatter"
ev = S.evaluate(g, BET)
assert ev["scatter"]["count"] == 3 and ev["payout"] == S.SCATTER_PAYS[3] * BET and not ev["lines"], ev
g[1][0] = g[3][2] = "scatter"
assert S.evaluate(g, BET)["scatter"]["pay"] == S.SCATTER_PAYS[5] * BET
g = blank()
g[0][0] = g[1][1] = "scatter"
assert S.evaluate(g, BET)["scatter"] is None, "два scatter не платят"
# bonus: приз из таблицы и кратен ставке
g = blank()
for c, r in ((1, 0), (2, 1), (3, 2)):
    g[c][r] = "bonus"
allowed = {m * BET for m, _ in S.BONUS_PRIZES}
for seed in range(30):
    ev = S.evaluate(g, BET, random.Random(seed))
    assert ev["bonus"]["count"] == 3 and ev["bonus"]["prize"] in allowed and ev["payout"] == ev["bonus"]["prize"], ev
assert S.tier_of(0, 10) == "none" and S.tier_of(10, 10) == "small" and S.tier_of(50, 10) == "big" and S.tier_of(150, 10) == "mega" and S.tier_of(500, 10) == "jackpot"
# ленты: допустимые символы, нет подряд одинаковых
for strip in S.STRIPS:
    assert set(strip) <= set(S.SYMBOLS) and all(strip[i] != strip[(i + 1) % len(strip)] for i in range(len(strip)))
for _ in range(2000):
    g = S.roll()
    assert len(g) == S.REELS and all(len(col) == S.ROWS and set(col) <= set(S.SYMBOLS) for col in g)
# отдача около 94 %: защита от случайной порчи таблицы выплат
rng, paid, n = random.Random(7), 0, 150_000
for _ in range(n):
    paid += S.play(10, rng)[1]["payout"]
rtp = paid / (n * 10)
assert 0.85 < rtp < 1.03, rtp
print(f"OK: правила выплат (wild, scatter, bonus, линии), RTP на {n:,} вращений = {rtp:.3f}")

# ---------- 2. ограничение частоты (на уровне модуля, на отдельном «пользователе») ----------
uid = 910001
os.environ["SLOTS_MIN_INTERVAL"] = "0.5"
S.spin(uid, 10)
try:
    S.spin(uid, 10)
    raise AssertionError("второй спин подряд должен быть отклонён")
except HTTPException as e:
    assert e.status_code == 429
time.sleep(0.6)
S.spin(uid, 10)
os.environ["SLOTS_MIN_INTERVAL"] = "0"
S.delete_user_slot_data(uid)
print("OK: ограничение частоты вращений")


# ---------- 3. API ----------
def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{int(time.time()) % 100000}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


adm, au = reg("adm")
a, ua = reg("slota")
b, ub = reg("slotb")
assert httpx.get(BASE + "/api/slots/state").status_code == 401
assert httpx.post(BASE + "/api/slots/spin", json={"bet": 10}).status_code == 401

st = a.get("/api/slots/state").json()
assert st["balance"] == S.START_BALANCE and st["bets"] == list(S.BETS) and st["daily"]["available"] and st["history"] == []
assert st["stats"] == {"spins": 0, "won_total": 0, "wagered_total": 0, "best_win": 0, "win_rate": 0.0}
assert {s["id"] for s in st["meta"]["symbols"]} == set(S.SYMBOLS) and len(st["meta"]["lines"]) == 10
assert st["min_bet"] == 10 and st["max_bet"] == 1000 and st["bet_step"] == 10
for bad in (15, 25, 5, 0, -10, 1010, 2000):  # не кратна 10 или вне допустимых пределов
    r = a.post("/api/slots/spin", json={"bet": bad})
    assert r.status_code in (400, 422), (bad, r.status_code)
assert a.post("/api/slots/spin", json={"bet": 10, "request_id": "short"}).status_code == 422
assert a.post("/api/slots/spin", json={"bet": 10, "request_id": "bad id with spaces!!"}).status_code == 422
assert a.post("/api/slots/spin", json={"bet": "много"}).status_code == 422

r = a.post("/api/slots/spin", json={"bet": 20, "request_id": "req-aaaa-0001"})
assert r.status_code == 200, r.text
sp = r.json()
assert len(sp["reels"]) == 5 and all(len(c) == 3 for c in sp["reels"]) and sp["bet"] == 20 and sp["replayed"] is False
assert sp["net"] == sp["payout"] - 20 and sp["balance"] == S.START_BALANCE - 20 + sp["payout"]
assert sp["tier"] in ("none", "small", "big", "mega", "jackpot") and (sp["payout"] > 0) == (sp["tier"] != "none")
# повтор того же запроса (сбой сети): тот же результат, второго списания нет
again = a.post("/api/slots/spin", json={"bet": 20, "request_id": "req-aaaa-0001"}).json()
assert again["replayed"] is True and again["id"] == sp["id"] and again["reels"] == sp["reels"] and again["balance"] == sp["balance"]
now = a.get("/api/slots/state").json()
assert now["balance"] == sp["balance"] and now["stats"]["spins"] == 1 and len(now["history"]) == 1
assert now["stats"]["wagered_total"] == 20 and now["stats"]["won_total"] == sp["payout"] and now["stats"]["best_win"] == sp["payout"]
print("OK: состояние, проверка ставки, спин, идемпотентность по request_id")

# ежедневный бонус
d1 = a.post("/api/slots/daily").json()
assert d1["amount"] == S.DAILY_BONUS and d1["balance"] == sp["balance"] + S.DAILY_BONUS
assert a.post("/api/slots/daily").status_code == 400 and not a.get("/api/slots/state").json()["daily"]["available"]
print("OK: ежедневный бонус один раз в день")

# изоляция пользователей
assert b.get("/api/slots/state").json()["balance"] == S.START_BALANCE and b.get("/api/slots/history").json() == []
assert len(a.get("/api/slots/history").json()) == 1
assert b.post("/api/slots/spin", json={"bet": 10, "request_id": "req-aaaa-0001"}).json()["replayed"] is False, "request_id действует только внутри одного пользователя"
assert b.post("/api/slots/spin", json={"bet": 10, "user_id": ua["id"]}).status_code == 200  # лишние поля игнорируются, баланс чужого не трогается
assert a.get("/api/slots/state").json()["balance"] == sp["balance"] + S.DAILY_BONUS
print("OK: аккаунты пользователей изолированы")

# недостаточно жетонов (баланс правим напрямую в базе теста)
db_path = os.environ.get("LQ_DB")
if db_path:
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("UPDATE slot_accounts SET balance=5 WHERE user_id=?", (ub["id"],))
    con.commit()
    r = b.post("/api/slots/spin", json={"bet": 10})
    assert r.status_code == 400 and "жетон" in r.json()["detail"]
    assert b.get("/api/slots/state").json()["balance"] == 5, "при отказе баланс не меняется"
    con.close()

# гонки: одинаковый request_id из многих потоков — одно списание; разные — каждое учитывается и баланс сходится
c, uc = reg("slotc")
res, lock = [], threading.Lock()


def go(rid):
    cl = httpx.Client(base_url=BASE, timeout=60, headers=c.headers)
    r = cl.post("/api/slots/spin", json={"bet": 10, "request_id": rid})
    with lock:
        res.append((rid, r.status_code, r.json()))


ths = [threading.Thread(target=go, args=("same-request-1",)) for _ in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
assert all(code == 200 for _, code, _ in res), res
assert len({j["id"] for _, _, j in res}) == 1, "один и тот же request_id должен дать одну запись"
assert sum(1 for _, _, j in res if not j["replayed"]) == 1
assert len(c.get("/api/slots/history").json()) == 1
res.clear()
ths = [threading.Thread(target=go, args=(f"parallel-req-{i:03d}",)) for i in range(12)]
[t.start() for t in ths]
[t.join() for t in ths]
assert all(code == 200 for _, code, _ in res), [x[:2] for x in res]
hist = c.get("/api/slots/history?limit=50").json()
assert len(hist) == 13
state = c.get("/api/slots/state").json()
assert state["balance"] == S.START_BALANCE - sum(h["bet"] for h in hist) + sum(h["payout"] for h in hist), "баланс должен точно сходиться с историей"
assert state["stats"]["spins"] == 13
balances = [h["balance"] for h in reversed(hist)]
for i, h in enumerate(reversed(hist)):
    prev = S.START_BALANCE if i == 0 else balances[i - 1]
    assert h["balance"] == prev - h["bet"] + h["payout"], "каждая запись истории продолжает предыдущую: гонок нет"
print("OK: параллельные запросы (повторы и разные) не ломают баланс")

# ---------- покупка жетонов за XP ----------
d, ud = reg("slotd")
ex = d.get("/api/slots/state").json()["exchange"]
assert ex["xp_per_chip"] == S.XP_PER_CHIP and [p["chips"] for p in ex["packs"]] == list(S.PACKS) and all(p["xp"] == p["chips"] * S.XP_PER_CHIP for p in ex["packs"])
assert ex["xp_available"] == 0
assert d.post("/api/slots/buy", json={"chips": 50}).status_code == 400, "без XP купить нельзя"
assert d.post("/api/slots/buy", json={"chips": 33}).status_code == 400, "только фиксированные пакеты"
assert d.post("/api/slots/buy", json={"chips": 50, "request_id": "x"}).status_code == 422
assert httpx.post(BASE + "/api/slots/buy", json={"chips": 50}).status_code == 401
if db_path:
    con = sqlite3.connect(db_path, timeout=30)

    def grant_xp(uid, n):
        con.execute("INSERT INTO activity(user_id, course_id, day, xp, lessons) VALUES(?,?,?,?,0) ON CONFLICT(user_id, course_id, day) DO UPDATE SET xp=xp+?",
                    (uid, 0, time.strftime("%Y-%m-%d"), n, n))
        con.commit()

    grant_xp(ud["id"], 500)
    lvl_before = d.get("/api/me").json()["level"]
    r = d.post("/api/slots/buy", json={"chips": 50, "request_id": "buy-req-0001"})
    assert r.status_code == 200, r.text  # жетонов много (1000), и это не мешает: ограничений по балансу нет
    b1 = r.json()
    price = 50 * S.XP_PER_CHIP
    assert b1["balance"] == S.START_BALANCE + 50 and b1["xp"] == price and b1["exchange"]["xp_available"] == 500 - price and b1["exchange"]["bought_today"] == 50
    again = d.post("/api/slots/buy", json={"chips": 50, "request_id": "buy-req-0001"}).json()
    assert again["replayed"] is True and again["balance"] == b1["balance"] and d.get("/api/slots/state").json()["exchange"]["xp_available"] == 500 - price, "повтор запроса не списывает XP второй раз"
    assert d.get("/api/me").json()["level"] == lvl_before, "траты XP не меняют уровень"
    r = d.post("/api/slots/buy", json={"chips": 100})
    assert r.status_code == 400 and "XP" in r.json()["detail"], "100 жетонов стоят дороже, чем осталось XP"
    grant_xp(ud["id"], 5000)
    for chips in (100, 200, 200, 50):  # сколько угодно, пока хватает XP
        assert d.post("/api/slots/buy", json={"chips": chips}).status_code == 200
    st = d.get("/api/slots/state").json()
    assert st["balance"] == S.START_BALANCE + 50 + 550 and st["exchange"]["xp_spent"] == 600 * S.XP_PER_CHIP and st["exchange"]["xp_total"] == 5500
    assert st["exchange"]["daily_limit"] is None and st["exchange"]["remaining_today"] is None
    # гонка: параллельные покупки не уводят XP в минус — проходит ровно столько, на сколько хватает XP
    e, ue = reg("slote")
    grant_xp(ue["id"], 3 * 50 * S.XP_PER_CHIP)
    out = []

    def buy_it(i):
        cl = httpx.Client(base_url=BASE, timeout=60, headers=e.headers)
        out.append(cl.post("/api/slots/buy", json={"chips": 50, "request_id": f"race-buy-{i:03d}"}).status_code)

    ths = [threading.Thread(target=buy_it, args=(i,)) for i in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert out.count(200) == 3 and out.count(400) == 5, out
    ste = e.get("/api/slots/state").json()
    assert ste["balance"] == S.START_BALANCE + 150 and ste["exchange"]["xp_available"] == 0 and ste["exchange"]["xp_spent"] == 150 * S.XP_PER_CHIP
    con.close()
# необязательные ограничения (по умолчанию выключены) на уровне модуля
S.DAILY_BUY_LIMIT, S.BUY_ONLY_BELOW = 100, 300
try:
    uid2 = 910002
    S.ensure_account(uid2)
    if db_path:
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("INSERT INTO activity(user_id, course_id, day, xp, lessons) VALUES(?,?,?,?,1)", (uid2, 0, time.strftime("%Y-%m-%d"), 100000))
        con.commit()
        con.close()
        try:
            S.buy_chips(uid2, 50)
            raise AssertionError("при включённом пороге 300 покупка при балансе 1000 должна быть отклонена")
        except HTTPException as ex_:
            assert "меньше" in ex_.detail
        S.BUY_ONLY_BELOW = None
        S.buy_chips(uid2, 50)
        S.buy_chips(uid2, 50)
        try:
            S.buy_chips(uid2, 50)
            raise AssertionError("дневной лимит 100 должен сработать")
        except HTTPException as ex_:
            assert "лимит" in ex_.detail
finally:
    S.DAILY_BUY_LIMIT, S.BUY_ONLY_BELOW = None, None
    S.delete_user_slot_data(910002)
print("OK: покупка жетонов за XP — фиксированные цены, повторы, гонки, уровень не страдает, лимиты (выключены по умолчанию)")

# ---------- своя ставка ----------
f, uf = reg("slotf")
c70 = f.post("/api/slots/spin", json={"bet": 70, "request_id": "custom-bet-070"}).json()
assert c70["bet"] == 70 and c70["net"] == c70["payout"] - 70 and c70["balance"] == S.START_BALANCE - 70 + c70["payout"], c70
c130 = f.post("/api/slots/spin", json={"bet": 130}).json()
assert c130["bet"] == 130 and c130["balance"] == c70["balance"] - 130 + c130["payout"]
r = f.post("/api/slots/spin", json={"bet": 1000})
assert (r.status_code == 200) == (c130["balance"] >= 1000), r.text  # при нехватке жетонов — отказ, баланс не трогается
if r.status_code == 400:
    assert f.get("/api/slots/state").json()["balance"] == c130["balance"]
print("OK: своя ставка (кратная 10, от 10 до 1000)")

# таблица лидеров и удаление пользователя
lb = a.get("/api/slots/leaderboard").json()
assert isinstance(lb, list) and all({"display_name", "best_win"} <= set(x) and "user_id" in x for x in lb)
assert not [x for x in lb if x["best_win"] <= 0]
assert httpx.get(BASE + "/api/slots/leaderboard").status_code == 401
if db_path:
    con = sqlite3.connect(db_path, timeout=30)
    cnt = lambda t, u: con.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (u,)).fetchone()[0]  # noqa: E731
    assert cnt("slot_accounts", uc["id"]) == 1 and cnt("slot_spins", uc["id"]) == 13
    assert cnt("slot_purchases", ud["id"]) == 5
    assert adm.delete(f"/api/admin/users/{uc['id']}").status_code == 200
    assert cnt("slot_accounts", uc["id"]) == 0 and cnt("slot_spins", uc["id"]) == 0, "данные слотов удалённого пользователя должны исчезнуть"
    assert adm.delete(f"/api/admin/users/{ud['id']}").status_code == 200 and cnt("slot_purchases", ud["id"]) == 0 and cnt("slot_accounts", ud["id"]) == 0
    assert cnt("slot_accounts", ua["id"]) == 1, "чужие данные не затронуты"
    con.close()
print("OK: таблица лидеров, удаление слот-данных вместе с пользователем")
print("ALL OK")
