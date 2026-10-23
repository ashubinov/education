"""Шахта: данные, замок по уровню, еда со склада, смены, добыча (решается при отправке), обвалы, вагонетки, продажа, лифт, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_mine.py http://127.0.0.1:8011"""
import json
import os
import sqlite3
import sys
import time
from collections import Counter

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import farm_data as FD  # noqa: E402
from server import mine as M  # noqa: E402
from server import mine_data as D  # noqa: E402

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


def give(uid, item, qty):
    sql("INSERT INTO farm_stock(user_id,item,qty) VALUES(?,?,?) ON CONFLICT(user_id,item) DO UPDATE SET qty=?", (uid, item, qty, qty))


# ----------------------------------------------------------------------------- данные
assert set(D.LOOT) == set(D.TIERS) == {1, 2, 3, 4}
assert all(o in D.ORES for t in D.LOOT.values() for o in t) and all(t["food"] in FD.ITEMS for t in D.TIERS.values())
tt = [D.TIERS[i]["time"] for i in (1, 2, 3, 4)]
assert tt == sorted(tt), "чем глубже ярус, тем дольше смена"
for u in D.UPGRADES.values():
    assert u["costs"] == sorted(u["costs"]) and u["max"] == len(u["costs"])
assert D.tier_open(1, 0) and not D.tier_open(2, 0) and D.tier_open(2, 1) and not D.tier_open(4, 2) and D.tier_open(4, 3)
assert D.risk_of(4, 0) > D.risk_of(4, 3) >= 0 and D.risk_of(1, 0) == 0 and D.shafts_count(4) == 6 and D.cart_cap(2) == D.BASE_CART + 2 * D.CART_STEP
print("OK: данные — ярусы, руды, улучшения")


# ----------------------------------------------------------------------------- розыгрыш добычи (без сервера)
def sample(tier, lamp=0, crib=0, pick=0, n=4000):
    lv = {"lamp": lamp, "crib": crib, "pick": pick}
    caves, ores, sizes = 0, Counter(), []
    for _ in range(n):
        haul, cave = M.roll_haul(tier, lv)
        caves += cave
        ores.update(haul)
        sizes.append((sum(haul.values()), cave))
    return caves, ores, sizes


caves, ores, sizes = sample(1)
assert caves == 0 and set(ores) <= {"coal", "copper", "iron"}, "верхний штрек безопасен и небогат"
caves, ores, sizes = sample(4)
assert .09 < caves / 4000 < .22 and set(ores) <= set(D.LOOT[4])
assert all(s >= D.TIERS[4]["finds"] // 2 for s, _ in sizes) and all(s >= D.TIERS[4]["finds"] for s, c in sizes if not c), "обвал режет добычу вдвое, без обвала — не меньше находок"
c5, _, _ = sample(4, crib=5)
assert c5 / 4000 < caves / 4000 / 1.8, "крепь заметно снижает обвалы"
_, o0, _ = sample(3, lamp=0)
_, o5, _ = sample(3, lamp=5)
rare = lambda o: (o["gold"] + o["amethyst"]) / sum(o.values())  # noqa: E731
assert rare(o5) > rare(o0) + .05, "лампы повышают долю редких руд"
print("OK: розыгрыш — риск обвала, крепь, лампы")

adm, au = reg("mnadm")
a, ua = reg("mna")
b, ub = reg("mnb")
assert httpx.get(BASE + "/api/mine").status_code == 401
for path in ("send", "collect", "sell", "upgrade"):
    assert httpx.post(f"{BASE}/api/mine/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/mine").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/mine/send", json={"shaft": 0, "tier": 1}))
sql("UPDATE farm SET xp=?, coins=100000 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/mine").json()
assert s["unlocked"] and len(s["shafts"]) == D.START_SHAFTS and s["cart_cap"] == D.BASE_CART
assert [t["locked"] for t in s["tiers"]] == [False, True, True, True]
print("OK: замок — открывается на нужном уровне")

# ----------------------------------------------------------------------------- отправка смены
assert a.post("/api/mine/send", json={"shaft": 0, "tier": 9}).status_code == 422
assert a.post("/api/mine/send", json={"shaft": 99, "tier": 1}).status_code == 422
assert a.post("/api/mine/send", json={"shaft": 0, "tier": 1, "x": 1}).status_code == 422
assert "еда" in err(a.post("/api/mine/send", json={"shaft": 0, "tier": 1}))
give(ua["id"], "bread", 10)
give(ua["id"], "omelette", 10)
assert "лифт" in err(a.post("/api/mine/send", json={"shaft": 0, "tier": 2}))
assert "нет" in err(a.post("/api/mine/send", json={"shaft": 5, "tier": 1}))
s = a.post("/api/mine/send", json={"shaft": 0, "tier": 1}).json()
assert s["shafts"][0]["state"] == "working" and s["shafts"][0]["left"] > 0 and next(t for t in s["tiers"] if t["tier"] == 1)["have"] == 9
assert "уже идёт" in err(a.post("/api/mine/send", json={"shaft": 0, "tier": 1}))
assert "ещё не вернулась" in err(a.post("/api/mine/collect", json={"shaft": None}))
loot = json.loads(sql("SELECT loot FROM mine_jobs WHERE user_id=? AND slot=0", (ua["id"],))[0][0])
assert sum(loot.values()) >= D.TIERS[1]["finds"] and set(loot) <= {"coal", "copper", "iron"}
print("OK: смена — еда со склада, добыча уже определена")

# ----------------------------------------------------------------------------- сбор, открытие руды, переполнение вагонеток
sql("UPDATE mine_jobs SET ready_at=0 WHERE user_id=?", (ua["id"],))
c0 = a.get("/api/mine").json()["coins"]
s = a.post("/api/mine/collect", json={"shaft": 0}).json()
assert s["inv"] == loot and s["shafts"][0]["state"] == "empty", "в вагонетки попадает ровно то, что было определено при отправке"
disc = [e for e in s["events"] if e["type"] == "discover"]
assert {e["ore"] for e in disc} == set(loot) and s["coins"] == c0 + sum(e["bonus"] for e in disc) and all(e["bonus"] == D.ORES[e["ore"]]["price"] * D.DISCOVERY_MULT for e in disc)
sql("DELETE FROM mine_ore WHERE user_id=?", (ua["id"],))
sql("INSERT INTO mine_ore(user_id,ore,qty) VALUES(?,?,?)", (ua["id"], "coal", D.BASE_CART))
a.post("/api/mine/send", json={"shaft": 1, "tier": 1})
sql("UPDATE mine_jobs SET ready_at=0 WHERE user_id=?", (ua["id"],))
assert "нет места" in err(a.post("/api/mine/collect", json={"shaft": None}))
assert a.get("/api/mine").json()["shafts"][1]["state"] == "ready", "смена остаётся, пока не освободишь вагонетки"
print("OK: сбор — добыча, бонус открытия, вагонетки не переполняются")

# ----------------------------------------------------------------------------- продажа
st = a.get("/api/mine").json()
price = {o["id"]: o["price"] for o in st["ores"]}
assert a.post("/api/mine/sell", json={"ore": "nonsense"}).status_code == 400
assert "нет руды" in err(a.post("/api/mine/sell", json={"ore": "diamond"})).lower()
c0 = st["coins"]
s = a.post("/api/mine/sell", json={"ore": "coal"}).json()
assert s["sold"] == {"qty": D.BASE_CART, "coins": price["coal"] * D.BASE_CART} and s["coins"] == c0 + price["coal"] * D.BASE_CART
assert "нет руды" in err(a.post("/api/mine/sell", json={"ore": None})).lower()
print("OK: продажа — цены и монеты")

# ----------------------------------------------------------------------------- улучшения: лифт открывает ярусы
s = a.post("/api/mine/upgrade", json={"key": "lift"}).json()
assert [t["locked"] for t in s["tiers"]] == [False, False, True, True]
a.post("/api/mine/collect", json={"shaft": None})
s = a.post("/api/mine/send", json={"shaft": 0, "tier": 2}).json()
assert s["shafts"][0]["tier"] == 2
sh = a.post("/api/mine/upgrade", json={"key": "shafts"}).json()
assert len(sh["shafts"]) == D.START_SHAFTS + 1
for _ in range(10):
    r = a.post("/api/mine/upgrade", json={"key": "cart"})
    if r.status_code != 200:
        assert "максимуме" in r.json()["detail"]
        break
else:
    raise AssertionError("вагонетки должны дойти до максимума")
sql("UPDATE farm SET coins=1 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/mine/upgrade", json={"key": "lamp"}))
assert a.post("/api/mine/upgrade", json={"key": "nonsense"}).status_code == 400
print("OK: улучшения — лифт, штреки, вагонетки")

# ----------------------------------------------------------------------------- изоляция и удаление
sb = b.get("/api/mine").json()
assert sb["unlocked"] is False and sb["inv"] == {}
assert M.ready_count(ua["id"]) >= 0
M.delete_user_data(ua["id"])
for t in ("mine_jobs", "mine_ore", "mine_seen", "mine_upgrades"):
    assert not sql(f"SELECT 1 FROM {t} WHERE user_id=?", (ua["id"],)), t
print("OK: изоляция игроков и удаление данных")
print("ALL OK")
