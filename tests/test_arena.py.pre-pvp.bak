"""Арена (петушиные бои): допуск только петухов, шансы, ставки, лимиты, голод, друзья, перевод ставки, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_arena.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import arena as A  # noqa: E402

db_path = os.environ.get("LQ_DB")


def sql(q, args=()):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def reg(name):
    d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{int(time.time()) % 100000}", "password": "secret1"}, timeout=30).json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


def err(r, code=400):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()["detail"]


def bal(c):
    return c.get("/api/pet").json()["balance"]


def reset_day(uid):
    sql("UPDATE pet_fights SET day='2000-01-01' WHERE attacker_id=?", (uid,))


# ----------------------------------------------------------------------------- формулы
assert abs(A.match_chance(1, 1) - .5) < 1e-9 and A.match_chance(4, 1) > .89 and A.match_chance(1, 9) < .11 and A.round_chance(100, 1) == .8 and A.round_chance(1, 100) == .2
assert A.match_chance(2, 1) > A.match_chance(1, 1) > A.match_chance(1, 2)
print("OK: формулы — сила, шансы")

adm, au = reg("adm")
a, ua = reg("arena_a")
b, ub = reg("arena_b")
c, uc = reg("arena_c")
assert httpx.get(BASE + "/api/pet/arena").status_code == 401
assert httpx.post(BASE + "/api/pet/arena/fight", json={"npc": "npc_novice"}).status_code in (401, 422)

# ----------------------------------------------------------------------------- допуск
s = a.get("/api/pet/arena").json()
assert s["has_pet"] is False and s["rooster"] is None and s["fights_left"] == A.FIGHTS_PER_DAY and {o["key"] for o in s["opponents"] if o["kind"] == "npc"} == set(A.NPCS)
assert "петух" in err(a.post("/api/pet/arena/fight", json={"npc": "npc_novice"}))
a.post("/api/pet/adopt", json={"species": "pug", "name": "Мопс"})
s = a.get("/api/pet/arena").json()
assert s["has_pet"] and s["pet_species"] == "pug" and s["rooster"] is None
assert "петух" in err(a.post("/api/pet/arena/fight", json={"npc": "npc_novice"}))
a.post("/api/pet/release")
a.post("/api/pet/adopt", json={"species": "rooster", "name": "Рокки"})
s = a.get("/api/pet/arena").json()
assert s["rooster"]["name"] == "Рокки" and 0 < s["power"] < 1.1 and all(o["chance"] is not None for o in s["opponents"])
for bad in ({}, {"npc": "x"}, {"npc": "npc_novice", "opponent_id": ub["id"]}, {"npc": "npc_novice", "bet": 7}, {"npc": "npc_novice", "bet": 51}, {"npc": "npc_novice", "bet": -1}, {"npc": "npc_novice", "z": 1}):
    r = a.post("/api/pet/arena/fight", json=bad)
    assert r.status_code in (400, 422), (bad, r.status_code)
print("OK: допуск — только петухи, проверка тела запроса")

# ----------------------------------------------------------------------------- бой с ботом, ставки, голод, лимит
before = bal(a)
r = a.post("/api/pet/arena/fight", json={"npc": "npc_novice", "bet": 10}).json()
res = r["result"]
assert res["rounds"] in ([1, 1], [0, 0], [1, 0, 1], [1, 0, 0], [0, 1, 1], [0, 1, 0]) and res["won"] == (sum(res["rounds"]) == 2) and res["delta"] == (10 if res["won"] else -10)
assert bal(a) == before + res["delta"] and res["opponent"] == "Новичок" and 0 < res["chance"] < 100
assert r["rooster"]["xp"] == (A.XP_WIN if res["won"] else A.XP_LOSE) and 74 - A.FULLNESS_COST - 1 <= r["rooster"]["fullness"] <= 75 - A.FULLNESS_COST
assert r["fights_left"] == A.FIGHTS_PER_DAY - 1 and r["record"]["wins"] + r["record"]["losses"] == 1 and len(r["recent"]) == 1 and r["recent"][0]["role"] == "attacker"
sql("UPDATE pets SET fullness=?, fed_at=? WHERE user_id=?", (A.MIN_FULLNESS - 1, time.time(), ua["id"]))
assert "голоден" in err(a.post("/api/pet/arena/fight", json={"npc": "npc_novice"}))
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
sql("UPDATE slot_accounts SET balance=5 WHERE user_id=?", (ua["id"],))
assert "жетонов" in err(a.post("/api/pet/arena/fight", json={"npc": "npc_novice", "bet": 10}))
sql("UPDATE slot_accounts SET balance=1000 WHERE user_id=?", (ua["id"],))
for i in range(A.FIGHTS_PER_DAY - 1):
    sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
    assert a.post("/api/pet/arena/fight", json={"npc": "npc_boris"}).status_code == 200
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
assert "хватит" in err(a.post("/api/pet/arena/fight", json={"npc": "npc_boris"}))
reset_day(ua["id"])
assert a.post("/api/pet/arena/fight", json={"npc": "npc_boris"}).status_code == 200
print("OK: бот — ставка, голод, баланс, суточный лимит")

# ----------------------------------------------------------------------------- друзья
b.post("/api/pet/adopt", json={"species": "rooster", "name": "Борода"})
c.post("/api/pet/adopt", json={"species": "ram", "name": "Баран"})
assert "Друг не найден" in err(a.post("/api/pet/arena/fight", json={"opponent_id": ub["id"]}), 404)  # ещё не друзья
for fr, fu in ((b, ub), (c, uc)):
    assert a.post("/api/friends/request", json={"username": fu["username"]}).status_code == 200
    fr.post(f"/api/friends/{ua['id']}/accept")
s = a.get("/api/pet/arena").json()
users = {o["id"]: o for o in s["opponents"] if o["kind"] == "user"}
assert set(users) == {ub["id"]} and users[ub["id"]]["pet"]["name"] == "Борода" and users[ub["id"]]["pair_left"] == A.PAIR_PER_DAY and "balance" not in users[ub["id"]]
assert "нет петуха" in err(a.post("/api/pet/arena/fight", json={"opponent_id": uc["id"]}), 404)
assert "самим собой" in err(a.post("/api/pet/arena/fight", json={"opponent_id": ua["id"]}))
print("OK: друзья — в списке только друзья с петухом")

# ----------------------------------------------------------------------------- ставка уходит другу при поражении, при победе добавляется из фонда
sql("UPDATE pets SET xp=900 WHERE user_id=?", (ub["id"],))   # сильный защитник против слабого нападающего
sql("UPDATE pets SET xp=0 WHERE user_id=?", (ua["id"],))
lost = False
for _ in range(30):
    reset_day(ua["id"])
    sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id IN (?,?)", (time.time(), ua["id"], ub["id"]))
    sql("UPDATE slot_accounts SET balance=1000 WHERE user_id IN (?,?)", (ua["id"], ub["id"]))
    r = a.post("/api/pet/arena/fight", json={"opponent_id": ub["id"], "bet": 50}).json()
    if not r["result"]["won"]:
        assert bal(a) == 950 and bal(b) == 1050, (bal(a), bal(b))
        rb = b.get("/api/pet/arena").json()
        assert rb["recent"][0]["role"] == "defender" and rb["recent"][0]["won"] is True and rb["recent"][0]["delta"] == 50 and rb["record"]["wins"] >= 1
        lost = True
        break
assert lost, "слабый петух должен проигрывать сильному"
sql("UPDATE pets SET xp=900 WHERE user_id=?", (ua["id"],))
sql("UPDATE pets SET xp=0 WHERE user_id=?", (ub["id"],))
won = False
for _ in range(30):
    reset_day(ua["id"])
    sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id IN (?,?)", (time.time(), ua["id"], ub["id"]))
    sql("UPDATE slot_accounts SET balance=1000 WHERE user_id IN (?,?)", (ua["id"], ub["id"]))
    r = a.post("/api/pet/arena/fight", json={"opponent_id": ub["id"], "bet": 50}).json()
    if r["result"]["won"]:
        assert bal(a) == 1050 and bal(b) == 1000, (bal(a), bal(b))
        won = True
        break
assert won
# не больше двух боёв в сутки с одним другом
reset_day(ua["id"])
for _ in range(A.PAIR_PER_DAY):
    sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
    assert a.post("/api/pet/arena/fight", json={"opponent_id": ub["id"]}).status_code == 200
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
assert "завтра" in err(a.post("/api/pet/arena/fight", json={"opponent_id": ub["id"]}))
assert a.post("/api/pet/arena/fight", json={"npc": "npc_novice"}).status_code == 200  # с ботом можно
print("OK: ставка — перевод другу, призовой фонд, лимит на пару")
assert a.post("/api/pet/arena/fight", json={"opponent_id": 10**30}).status_code == 422 and a.post("/api/pet/arena/fight", json={"opponent_id": 0}).status_code == 422, "огромные id не должны ронять сервер"

# ----------------------------------------------------------------------------- удаление
assert sql("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? OR defender_id=?", (ub["id"], ub["id"]))[0][0] > 0
assert adm.delete(f"/api/admin/users/{ub['id']}").status_code == 200
assert sql("SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? OR defender_id=?", (ub["id"], ub["id"]))[0][0] == 0
print("ALL OK")
