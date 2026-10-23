"""Пасека: данные, замок по уровню, цветы со склада, цикл мёда, рамки/предел, открытие видов, продажа, улучшения, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_apiary.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import apiary as A  # noqa: E402
from server import apiary_data as D  # noqa: E402

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


def age(uid, slot, cycles, flower="clover", queen=0):
    """Состарить улей: прошло `cycles` циклов."""
    sql("UPDATE apiary_hives SET base_at=? WHERE user_id=? AND slot=?", (time.time() - D.cycle_time(flower, queen) * cycles - 5, uid, slot))


# ----------------------------------------------------------------------------- данные
assert set(D.FLOWERS) == {"clover", "sunflower", "strawberry", "pumpkin"} and all(f["honey"] in D.HONEY for f in D.FLOWERS.values())
assert D.FLOWERS["clover"]["item"] is None and D.FLOWERS["clover"]["lasts"] == 0, "луговые травы бесплатны и бесконечны"
pr = [D.HONEY[f["honey"]]["price"] for f in D.FLOWERS.values()]
assert pr == sorted(pr), "мёд с более дорогих цветов дороже"
for u in D.UPGRADES.values():
    assert u["costs"] == sorted(u["costs"]) and u["max"] == len(u["costs"])
assert D.hives_count(6) == 8 and D.cap_of(2) == D.BASE_CAP + 2 * D.CAP_STEP and D.cycle_time("clover", 5) < D.cycle_time("clover", 0)
print("OK: данные — цветы, мёд, улучшения")

adm, au = reg("apadm")
a, ua = reg("apa")
b, ub = reg("apb")
assert httpx.get(BASE + "/api/apiary").status_code == 401
for path in ("flowers", "collect", "sell", "upgrade"):
    assert httpx.post(f"{BASE}/api/apiary/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/apiary").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/apiary/flowers", json={"hive": 0, "flower": "clover"}))
sql("UPDATE farm SET xp=?, coins=100000 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/apiary").json()
assert s["unlocked"] and len(s["hives"]) == D.START_HIVES and s["coins"] == 100000 and s["cap"] == D.BASE_CAP
print("OK: замок — открывается на нужном уровне")

# ----------------------------------------------------------------------------- цветы
assert a.post("/api/apiary/flowers", json={"hive": 0, "flower": "nonsense"}).status_code == 400
assert a.post("/api/apiary/flowers", json={"hive": 99, "flower": "clover"}).status_code == 422
assert a.post("/api/apiary/flowers", json={"hive": 0, "flower": "clover", "x": 1}).status_code == 422
assert "нет" in err(a.post("/api/apiary/flowers", json={"hive": 0, "flower": "sunflower"}))
assert "нет" in err(a.post("/api/apiary/flowers", json={"hive": 5, "flower": "clover"}))
s = a.post("/api/apiary/flowers", json={"hive": 0, "flower": "clover"}).json()
assert s["hives"][0]["state"] == "working" and s["hives"][0]["left"] is None and s["hives"][1]["state"] == "empty"
give(ua["id"], "sunflower", 5)
s = a.post("/api/apiary/flowers", json={"hive": 1, "flower": "sunflower"}).json()
assert next(f for f in s["flowers"] if f["id"] == "sunflower")["have"] == 4 and s["hives"][1]["left"] == D.FLOWERS["sunflower"]["lasts"]
assert "работают" in err(a.post("/api/apiary/flowers", json={"hive": 1, "flower": "clover"}))
assert "Мёда пока нет" in err(a.post("/api/apiary/collect", json={"hive": None}))
print("OK: цветы — склад, бесплатные луга, занятый улей")

# ----------------------------------------------------------------------------- мёд: предел рамок, открытие вида, конечные цветы
age(ua["id"], 0, 30)
age(ua["id"], 1, 30, "sunflower")
s = a.get("/api/apiary").json()
assert s["hives"][0]["state"] == "ready" and s["hives"][0]["jars"] == D.BASE_CAP and s["hives"][0]["full"], "больше предела рамок не копится"
assert s["hives"][1]["jars"] == D.BASE_CAP
c0 = s["coins"]
assert "Сначала забери" in err(a.post("/api/apiary/flowers", json={"hive": 0, "flower": "clover"}))
s = a.post("/api/apiary/collect", json={"hive": 0}).json()
assert s["inv"]["clover"] == D.BASE_CAP and s["hives"][0]["state"] == "working"
disc = [e for e in s["events"] if e["type"] == "discover"]
assert disc and disc[0]["honey"] == "clover" and disc[0]["bonus"] == D.HONEY["clover"]["price"] * D.DISCOVERY_MULT and s["coins"] == c0 + disc[0]["bonus"]
s = a.post("/api/apiary/collect", json={"hive": None}).json()
assert s["inv"]["sunflower"] == D.BASE_CAP and s["hives"][1]["left"] == D.FLOWERS["sunflower"]["lasts"] - D.BASE_CAP
age(ua["id"], 1, 30, "sunflower")
s = a.post("/api/apiary/collect", json={"hive": 1}).json()
assert s["hives"][1]["state"] == "idle" and s["inv"]["sunflower"] == 2 * D.BASE_CAP, "цветы кончились — улей простаивает"
assert not [e for e in s["events"] if e["type"] == "discover"]
s = a.post("/api/apiary/flowers", json={"hive": 1, "flower": "sunflower"}).json()
assert s["hives"][1]["state"] == "working"
print("OK: мёд — предел рамок, открытие вида, цветы кончаются")

# ----------------------------------------------------------------------------- продажа
st = a.get("/api/apiary").json()
price = {h["id"]: h["price"] for h in st["honey"]}
c0 = st["coins"]
assert a.post("/api/apiary/sell", json={"honey": "nonsense"}).status_code == 400
assert "нет мёда" in err(a.post("/api/apiary/sell", json={"honey": "pumpkin"})).lower()
s = a.post("/api/apiary/sell", json={"honey": "clover"}).json()
assert s["sold"] == {"qty": D.BASE_CAP, "coins": price["clover"] * D.BASE_CAP} and s["coins"] == c0 + price["clover"] * D.BASE_CAP
s = a.post("/api/apiary/sell", json={"honey": None}).json()
assert s["sold"]["qty"] == 2 * D.BASE_CAP and s["inv"] == {}
base = D.HONEY["clover"]["price"]
assert int(base * .85) <= price["clover"] <= int(base * 1.31) + 1
print("OK: продажа — цены и монеты")

# ----------------------------------------------------------------------------- улучшения: ульи, рамки, пыльца
st = a.get("/api/apiary").json()
cost = next(u for u in st["upgrades"] if u["key"] == "hives")["cost"]
s = a.post("/api/apiary/upgrade", json={"key": "hives"}).json()
assert len(s["hives"]) == D.START_HIVES + 1 and s["coins"] == st["coins"] - cost
assert a.post("/api/apiary/upgrade", json={"key": "nonsense"}).status_code == 400
a.post("/api/apiary/upgrade", json={"key": "frames"})
assert a.get("/api/apiary").json()["cap"] == D.BASE_CAP + D.CAP_STEP
for _ in range(10):
    r = a.post("/api/apiary/upgrade", json={"key": "frames"})
    if r.status_code != 200:
        assert "максимуме" in r.json()["detail"]
        break
else:
    raise AssertionError("рамки должны дойти до максимума")
sql("UPDATE farm SET coins=1 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/apiary/upgrade", json={"key": "jelly"}))
sql("UPDATE farm SET coins=100000 WHERE user_id=?", (ua["id"],))
sql("INSERT OR REPLACE INTO apiary_upgrades(user_id,key,level) VALUES(?,?,5)", (ua["id"], "pollen"))
sql("INSERT OR REPLACE INTO apiary_upgrades(user_id,key,level) VALUES(?,?,5)", (ua["id"], "jelly"))
extra = jelly = 0
cap = D.cap_of(5)
for _ in range(12):
    a.post("/api/apiary/flowers", json={"hive": 0, "flower": "clover"})
    sql("UPDATE apiary_hives SET base_at=? WHERE user_id=? AND slot=0", (time.time() - D.cycle_time("clover", 0) * (cap + 3), ua["id"]))
    s = a.post("/api/apiary/collect", json={"hive": 0}).json()
    got = sum(e["qty"] for e in s["events"] if e["type"] == "honey" and e["honey"] == "clover")
    extra += got - cap
    jelly += sum(e["qty"] for e in s["events"] if e["type"] == "honey" and e["honey"] == "jelly")
    a.post("/api/apiary/sell", json={"honey": None})
assert extra > 0, "пыльца 5: лишние баночки бывают"
assert jelly > 0, "молочко 5: маточное молочко выпадает"
print("OK: улучшения — ульи, рамки, пыльца, молочко")

# ----------------------------------------------------------------------------- изоляция, публичные данные и удаление
sb = b.get("/api/apiary").json()
assert sb["unlocked"] is False and sb["inv"] == {}
assert A.ready_count(ua["id"]) >= 0
A.delete_user_data(ua["id"])
for t in ("apiary_hives", "apiary_honey", "apiary_seen", "apiary_upgrades"):
    assert not sql(f"SELECT 1 FROM {t} WHERE user_id=?", (ua["id"],)), t
print("OK: изоляция игроков и удаление данных")
print("ALL OK")
