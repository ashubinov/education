"""Озеро (рыбалка): данные, замок по уровню, наживка со склада, клёв по времени, улов, садок, продажа, атлас, улучшения, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_lake.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time
from collections import Counter

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import lake as L  # noqa: E402
from server import lake_data as D  # noqa: E402

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


def ready_all(uid):
    sql("UPDATE lake_rods SET ready_at=0 WHERE user_id=?", (uid,))


# ----------------------------------------------------------------------------- данные
assert len(D.FISH) == 14 and set(D.TIERS) == {1, 2, 3, 4} and all(len(v) == 3 for v in D.TIERS.values())
tp = [max(D.FISH[i]["price"] for i in D.TIERS[t]) for t in (1, 2, 3, 4)]
assert tp == sorted(tp), "рыба каждой следующей ступени дороже"
assert D.FISH["pearl"]["tier"] == 0 and D.FISH["boot"]["tier"] == 0
for u in D.UPGRADES.values():
    assert u["costs"] == sorted(u["costs"]) and u["max"] == len(u["costs"])
assert D.rods_count(4) == 6 and D.keep_cap(2) == D.BASE_KEEP + 2 * D.KEEP_STEP
assert D.bite_time("wheat", 5) < D.bite_time("wheat", 0) and D.bite_time("corn", 0) > D.bite_time("wheat", 0) and D.bite_time("strawberry", 0) > D.bite_time("corn", 0)
assert D.legend_chance(0, 5) == 0 and D.legend_chance(1, 5) == 0 and D.legend_chance(2, 0) == D.LEGEND_BASE and D.legend_chance(2, 5) == D.LEGEND_BASE * 6
assert D.max_tier(0) == 2 and D.max_tier(1) == 3
print("OK: данные — рыбы, цены, улучшения")

# ----------------------------------------------------------------------------- розыгрыш улова (без сервера, по десяткам тысяч бросков)
def sample(bait, boat=0, sonar=0, net=0, n=6000):
    lv = {"boat": boat, "sonar": sonar, "net": net}
    kinds, fish, qty2 = Counter(), Counter(), 0
    for _ in range(n):
        r = L._roll(bait, lv)
        kinds[r["kind"]] += 1
        if r["kind"] == "fish":
            fish[r["fish"]] += 1
            qty2 += r["qty"] == 2
        else:
            assert D.CHEST_COINS[0] <= r["coins"] <= D.CHEST_COINS[1]
    return kinds, fish, qty2


k, f, d2 = sample("wheat")
assert {D.FISH[x]["tier"] for x in f} <= {0, 1}, "на пшеницу без лодки — только пруд"
assert 0 < k["chest"] < 6000 * .03 and d2 == 0
k, f, _ = sample("corn")
assert {D.FISH[x]["tier"] for x in f} <= {0, 1, 2} and any(D.FISH[x]["tier"] == 2 for x in f)
k, f, _ = sample("strawberry", boat=0)
assert max(D.FISH[x]["tier"] for x in f) <= 2, "глубина закрыта без лодки"
k, f, _ = sample("strawberry", boat=1)
assert any(D.FISH[x]["tier"] == 3 for x in f) and not any(D.FISH[x]["tier"] == 4 for x in f), "лодка 1: глубина есть, легенд нет"
k, f, _ = sample("strawberry", boat=2, sonar=5, n=8000)
assert any(D.FISH[x]["tier"] == 4 for x in f), "лодка 2 и эхолот: легенды выпадают"
k, f, d2 = sample("wheat", net=5, n=8000)
assert d2 > 8000 * .2, "сеть 5: двойной улов заметно чаще"
print("OK: розыгрыш — ступени, лодка, легенды, сундуки, сеть")

adm, au = reg("lkadm")
a, ua = reg("lka")
b, ub = reg("lkb")
assert httpx.get(BASE + "/api/lake").status_code == 401
for path in ("cast", "collect", "sell", "upgrade"):
    assert httpx.post(f"{BASE}/api/lake/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/lake").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"}))
sql("UPDATE farm SET xp=?, coins=100000 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/lake").json()
assert s["unlocked"] and len(s["rods"]) == D.START_RODS and s["coins"] == 100000 and s["keep_cap"] == D.BASE_KEEP
assert {x["id"] for x in s["baits"]} == set(D.BAITS) and all(x["have"] == 0 for x in s["baits"])
print("OK: замок — открывается на нужном уровне")

# ----------------------------------------------------------------------------- заброс
assert a.post("/api/lake/cast", json={"rod": 0, "bait": "nonsense"}).status_code == 400
assert a.post("/api/lake/cast", json={"rod": 99, "bait": "wheat"}).status_code == 422
assert a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat", "x": 1}).status_code == 422
assert "наживка" in err(a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"})).lower()
give(ua["id"], "wheat", 200)
give(ua["id"], "corn", 50)
give(ua["id"], "strawberry", 50)
assert "нет" in err(a.post("/api/lake/cast", json={"rod": 5, "bait": "wheat"}))
s = a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"}).json()
assert s["rods"][0]["state"] == "fishing" and s["rods"][0]["left"] > 0 and s["rods"][0]["bait"] == "wheat" and s["coins"] == 100000
assert next(x for x in s["baits"] if x["id"] == "wheat")["have"] == 199, "наживка списалась со склада фермы"
assert "закинута" in err(a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"}))
assert "Клёва пока нет" in err(a.post("/api/lake/collect", json={"rod": None}))
print("OK: заброс — наживка со склада, проверки")

# ----------------------------------------------------------------------------- улов, открытие вида, бонус
ready_all(ua["id"])
s0 = a.get("/api/lake").json()
assert s0["rods"][0]["state"] == "ready"
s = a.post("/api/lake/collect", json={"rod": 0}).json()
assert s["rods"][0]["state"] == "empty" and s["keep_used"] >= 0
caught = [e for e in s["events"] if e["type"] == "catch"]
chest = [e for e in s["events"] if e["type"] == "chest"]
assert caught or chest, s["events"]
if caught:
    fid = caught[0]["fish"]
    assert s["inv"][fid] == caught[0]["qty"]
    disc = [e for e in s["events"] if e["type"] == "discover"]
    assert bool(disc) == (D.FISH[fid]["tier"] > 0), "бонус открытия — только за настоящую рыбу"
    if disc:
        assert disc[0]["bonus"] == D.FISH[fid]["price"] * D.DISCOVERY_MULT and s["coins"] == 100000 + disc[0]["bonus"]
assert "Клёва пока нет" in err(a.post("/api/lake/collect", json={"rod": 0}))
print("OK: улов — рыба в садок, бонус открытия")

# ----------------------------------------------------------------------------- много забросов: только ожидаемая рыба, садок ограничен
for rnd in range(6):
    for r in range(D.START_RODS):
        a.post("/api/lake/cast", json={"rod": r, "bait": "wheat"})
    ready_all(ua["id"])
    s = a.post("/api/lake/collect", json={"rod": None}).json()
    assert all(D.FISH[k]["tier"] <= 1 for k in s["inv"]), s["inv"]
    assert s["keep_used"] <= s["keep_cap"]
seen = {r[0] for r in sql("SELECT fish FROM lake_seen WHERE user_id=?", (ua["id"],))}
assert seen and all(D.FISH[x]["tier"] in (1,) for x in seen)
print("OK: много забросов — пруд и жемчужина/хлам, садок не переполняется")

# ----------------------------------------------------------------------------- садок: переполнение
sql("DELETE FROM lake_fish WHERE user_id=?", (ua["id"],))
sql("INSERT INTO lake_fish(user_id,fish,qty) VALUES(?,?,?)", (ua["id"], "crucian", D.BASE_KEEP))
sql("DELETE FROM lake_rods WHERE user_id=?", (ua["id"],))
a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"})
ready_all(ua["id"])
for _ in range(40):   # сундук (монеты) не занимает место; иногда выпадает — тогда удочка освободилась, закинем снова
    r = a.post("/api/lake/collect", json={"rod": 0})
    if r.status_code == 400:
        assert "нет места" in r.json()["detail"]
        assert a.get("/api/lake").json()["rods"][0]["state"] == "ready", "удочка остаётся с уловом, пока не освободишь место"
        break
    a.post("/api/lake/cast", json={"rod": 0, "bait": "wheat"})
    ready_all(ua["id"])
else:
    raise AssertionError("не удалось проверить переполнение садка")
print("OK: садок — переполнение не теряет улов")

# ----------------------------------------------------------------------------- продажа
sql("DELETE FROM lake_rods WHERE user_id=?", (ua["id"],))
sql("DELETE FROM lake_fish WHERE user_id=?", (ua["id"],))
sql("INSERT INTO lake_fish(user_id,fish,qty) VALUES(?,?,?)", (ua["id"], "crucian", 4))
sql("INSERT INTO lake_fish(user_id,fish,qty) VALUES(?,?,?)", (ua["id"], "perch", 3))
st = a.get("/api/lake").json()
price = {x["id"]: x["price"] for x in st["fish"]}
c0 = st["coins"]
assert a.post("/api/lake/sell", json={"fish": "nonsense"}).status_code == 400
assert "нет рыбы" in err(a.post("/api/lake/sell", json={"fish": "salmon"}))
s = a.post("/api/lake/sell", json={"fish": "crucian"}).json()
assert s["sold"] == {"qty": 4, "coins": price["crucian"] * 4} and s["coins"] == c0 + price["crucian"] * 4 and "crucian" not in s["inv"]
s = a.post("/api/lake/sell", json={"fish": None}).json()
assert s["sold"]["qty"] == 3 and s["inv"] == {} and s["keep_used"] == 0
assert "нет рыбы" in err(a.post("/api/lake/sell", json={"fish": None}))
base = D.FISH["crucian"]["price"]
assert int(base * .85) <= price["crucian"] <= int(base * 1.31 * (1 + D.price_bonus(0, 0))) + 1, "цена гуляет в пределах рынка"
print("OK: продажа — цены, монеты, пустой садок")

# ----------------------------------------------------------------------------- улучшения
st = a.get("/api/lake").json()
assert st["boat"] == 0
rods_cost = next(u for u in st["upgrades"] if u["key"] == "rods")["cost"]
s = a.post("/api/lake/upgrade", json={"key": "rods"}).json()
assert len(s["rods"]) == D.START_RODS + 1 and s["coins"] == st["coins"] - rods_cost
assert a.post("/api/lake/upgrade", json={"key": "nonsense"}).status_code == 400
for _ in range(10):
    r = a.post("/api/lake/upgrade", json={"key": "boat"})
    if r.status_code != 200:
        assert "максимуме" in r.json()["detail"]
        break
else:
    raise AssertionError("лодка должна дойти до максимума")
assert a.get("/api/lake").json()["boat"] == 2
sql("UPDATE farm SET coins=1 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/lake/upgrade", json={"key": "sonar"}))
sql("UPDATE farm SET coins=100000 WHERE user_id=?", (ua["id"],))
s = a.get("/api/lake").json()
assert s["legend_chance"] == D.LEGEND_BASE and next(f for f in s["fish"] if f["id"] == "koi")["locked"] is False
print("OK: улучшения — цена, максимум, лодка открывает глубины и легенды")

# ----------------------------------------------------------------------------- изоляция, публичная статистика, удаление
sb = b.get("/api/lake").json()
assert sb["unlocked"] is False and sb["inv"] == {} and sb["boat"] == 0
assert err(b.post("/api/lake/sell", json={"fish": None})) and L.public_stats(ua["id"])["total"] == 12
L.delete_user_data(ua["id"])
for t in ("lake_rods", "lake_fish", "lake_seen", "lake_upgrades"):
    assert not sql(f"SELECT 1 FROM {t} WHERE user_id=?", (ua["id"],)), t
print("OK: изоляция игроков и удаление данных")
print("ALL OK")
