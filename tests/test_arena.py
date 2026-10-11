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
# Legacy offline player fights are disabled. Bot-only sparring is covered above.
b.post('/api/pet/adopt', json={'species': 'rooster', 'name': 'Борода'})
assert a.post('/api/friends/request', json={'username': ub['username']}).status_code == 200
assert b.post(f"/api/friends/{ua['id']}/accept").status_code == 200
before_a, before_b = bal(a), bal(b)
assert 'Live PvP' in err(a.post('/api/pet/arena/fight', json={'opponent_id': ub['id'], 'bet': 10}))
assert bal(a) == before_a and bal(b) == before_b
assert sql('SELECT COUNT(*) FROM pet_fights WHERE attacker_id=? AND defender_id=?', (ua['id'], ub['id']))[0][0] == 0
print('OK: офлайн-PvP запрещено, жетоны не меняются')

# ----------------------------------------------------------------------------- удаление
assert adm.delete(f"/api/admin/users/{ub['id']}").status_code == 200
print('ALL OK')
