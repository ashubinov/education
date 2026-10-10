"""Лаборатория: данные, замок по уровню, посадка, рост, шишки, коллекция, улучшения, продажа, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_lab.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import lab_data as D  # noqa: E402

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


# ----------------------------------------------------------------------------- данные
assert len(D.SPECIES) == 11 and set(D.TIERS) == {1, 2, 3, 4} and len(D.TIERS[4]) == 2
for k, s in D.SPECIES.items():
    assert s["price"] > 0 and s["grow"] > 0 and s["cycle"] > 0
    if not s["mutant"]:
        assert s["sapling"] > 0 and s["price"] > s["sapling"], f"{k}: шишка дороже саженца"
tp = [max(D.SPECIES[i]["price"] for i in D.TIERS[t]) for t in (1, 2, 3, 4)]
assert tp == sorted(tp) and D.SPECIES[D.CRYSTAL]["price"] > D.SPECIES[D.GOLD]["price"]
for u in D.UPGRADES.values():
    assert u["costs"] == sorted(u["costs"]) and u["max"] == len(u["costs"])
assert D.tier_unlocked(1, 0) and not D.tier_unlocked(2, 0) and D.tier_unlocked(2, 1) and not D.tier_unlocked(3, 2) and D.tier_unlocked(3, 3)
assert D.grow_time("spruce_common", 5) < D.grow_time("spruce_common", 0) and D.cone_cap(2) == D.BASE_CONE_CAP + 4 and D.pots_count(4) == 6
assert D.MUTATION_CHANCE == sorted(D.MUTATION_CHANCE)
print("OK: данные — виды, цены, улучшения")

adm, au = reg("adm")
a, ua = reg("laba")
b, ub = reg("labb")
assert httpx.get(BASE + "/api/lab").status_code == 401
for path in ("plant", "harvest", "sell", "upgrade", "remove"):
    assert httpx.post(f"{BASE}/api/lab/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/lab").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/lab/plant", json={"slot": 0, "species": "spruce_common"}))
sql("UPDATE farm SET xp=?, coins=100000 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/lab").json()
assert s["unlocked"] and s["pots"] == D.START_POTS and len(s["trees"]) == D.START_POTS and s["coins"] == 100000
print("OK: замок — открывается на нужном уровне")

# ----------------------------------------------------------------------------- посадка
assert a.post("/api/lab/plant", json={"slot": 0, "species": "nonsense"}).status_code == 400
assert a.post("/api/lab/plant", json={"slot": 0, "species": D.GOLD}).status_code == 400   # мутантов не купить
assert a.post("/api/lab/plant", json={"slot": 99, "species": "spruce_common"}).status_code == 422
assert "кадки" in err(a.post("/api/lab/plant", json={"slot": 5, "species": "spruce_common"}))
assert "селекции" in err(a.post("/api/lab/plant", json={"slot": 0, "species": "spruce_blue"}))
assert a.post("/api/lab/plant", json={"slot": 0, "species": "spruce_common", "x": 1}).status_code == 422
s = a.post("/api/lab/plant", json={"slot": 0, "species": "spruce_common"}).json()
assert s["coins"] == 100000 - D.SPECIES["spruce_common"]["sapling"] and s["trees"][0]["state"] == "growing" and s["trees"][0]["left"] > 0
assert "занята" in err(a.post("/api/lab/plant", json={"slot": 0, "species": "spruce_common"}))
assert "Шишек пока нет" in err(a.post("/api/lab/harvest", json={"slot": None}))
print("OK: посадка — проверки, списание монет")

# ----------------------------------------------------------------------------- рост и шишки
cyc = D.SPECIES["spruce_common"]["cycle"]
now = time.time()
sql("UPDATE lab_trees SET mature_at=?, base_at=? WHERE user_id=? AND slot=0", (now - 10, now - 10, ua["id"]))   # только что созрела
s = a.get("/api/lab").json()
assert s["trees"][0]["state"] == "mature" and s["trees"][0]["cones"] == 0
assert "Шишек пока нет" in err(a.post("/api/lab/harvest", json={"slot": 0}))
sql("UPDATE lab_trees SET mature_at=?, base_at=? WHERE user_id=? AND slot=0", (now - 5 * cyc - 100, now - 5 * cyc - 100, ua["id"]))
s = a.get("/api/lab").json()
assert s["trees"][0]["cones"] == D.BASE_CONE_CAP, s["trees"][0]            # лимит хранилища на дереве
s = a.post("/api/lab/harvest", json={"slot": 0}).json()
assert s["cones"]["spruce_common"] == D.BASE_CONE_CAP and s["trees"][0]["cones"] == 0
ev = {e["type"]: e for e in s["events"]}
assert ev["discover"]["bonus"] == D.SPECIES["spruce_common"]["price"] * D.DISCOVERY_MULT
assert s["coins"] == 100000 - D.SPECIES["spruce_common"]["sapling"] + ev["discover"]["bonus"]
assert s["tiers"]["1"]["found"] == 1
# повторный сбор — ничего, второй сбор через цикл — одна шишка, бонус открытия не повторяется
assert "Шишек пока нет" in err(a.post("/api/lab/harvest", json={"slot": 0}))
sql("UPDATE lab_trees SET base_at=base_at-? WHERE user_id=? AND slot=0", (cyc + 5, ua["id"]))
s = a.post("/api/lab/harvest", json={"slot": None}).json()
assert s["cones"]["spruce_common"] == D.BASE_CONE_CAP + 1 and not [e for e in s["events"] if e["type"] == "discover"]
print("OK: шишки — лимит на дереве, сбор, бонус открытия один раз")

# ----------------------------------------------------------------------------- продажа
before = s["coins"]
price = next(x["price"] for x in s["species"] if x["id"] == "spruce_common")
assert 100 <= price <= 160
r = a.post("/api/lab/sell", json={"species": "spruce_common"}).json()
assert r["sold"]["qty"] == D.BASE_CONE_CAP + 1 and r["sold"]["coins"] == price * (D.BASE_CONE_CAP + 1) and r["coins"] == before + r["sold"]["coins"] and not r["cones"]
assert "Нет шишек" in err(a.post("/api/lab/sell", json={"species": None}))
assert a.post("/api/lab/sell", json={"species": "zzz"}).status_code == 400
print("OK: продажа шишек за монеты фермы")

# ----------------------------------------------------------------------------- улучшения
sql("UPDATE farm SET coins=1000000 WHERE user_id=?", (ua["id"],))
assert a.post("/api/lab/upgrade", json={"key": "nope"}).status_code == 400
for k, u in D.UPGRADES.items():
    for lvl in range(u["max"]):
        r = a.post("/api/lab/upgrade", json={"key": k})
        assert r.status_code == 200, (k, lvl, r.text)
    assert "максимум" in err(a.post("/api/lab/upgrade", json={"key": k}))
s = a.get("/api/lab").json()
assert s["pots"] == 6 and all(u["level"] == u["max"] and u["cost"] is None for u in s["upgrades"]) and s["mutation_chance"] == D.MUTATION_CHANCE[5]
assert next(x for x in s["species"] if x["id"] == "spruce_common")["grow"] < D.SPECIES["spruce_common"]["grow"]
assert all(x["plantable"] for x in s["species"] if not x["mutant"]) and abs(s["bonus"] - .20) < 1e-9
sql("UPDATE farm SET coins=5 WHERE user_id=?", (ua["id"],))
assert "Не хватает" in err(a.post("/api/lab/plant", json={"slot": 1, "species": "fir_korean"}))
sql("UPDATE farm SET coins=1000000 WHERE user_id=?", (ua["id"],))
print("OK: улучшения — все уровни, максимум, эффекты")

# ----------------------------------------------------------------------------- мутанты: при максимуме селекции хоть один из многих саженцев мутирует
for i in range(1, 6):
    assert a.post("/api/lab/plant", json={"slot": i, "species": "fir_nordmann"}).status_code == 200
sql("UPDATE lab_trees SET mutant=? WHERE user_id=? AND slot=2", (D.GOLD, ua["id"]))
sql("UPDATE lab_trees SET mutant=? WHERE user_id=? AND slot=3", (D.CRYSTAL, ua["id"]))
now = time.time()
sql("UPDATE lab_trees SET mature_at=?, base_at=? WHERE user_id=?", (now - 100 * D.H, now - 100 * D.H, ua["id"]))
s = a.post("/api/lab/harvest", json={"slot": None}).json()
assert s["cones"].get(D.GOLD, 0) >= 1 and s["cones"].get(D.CRYSTAL, 0) >= 1 and any(e["type"] == "discover" and e["mutant"] for e in s["events"])
assert s["tiers"]["4"]["found"] == 2 and s["tiers"]["3"]["found"] == 1
# мутантные шишки сами есть в продаже и стоят очень дорого
cr = next(x for x in s["species"] if x["id"] == D.CRYSTAL)
assert cr["price"] > 8000 and cr["discovered"] and not cr["plantable"]
# удаление дерева
r = a.post("/api/lab/remove", json={"slot": 1}).json()
assert r["trees"][1]["state"] == "empty"
assert "пусто" in err(a.post("/api/lab/remove", json={"slot": 1}))
print("OK: мутанты, коллекция, удаление дерева")

# ----------------------------------------------------------------------------- изоляция, друзья
sb = b.get("/api/lab").json()
assert sb["unlocked"] is False and not [t for t in sb["trees"] if t["state"] != "empty"] and not sb["cones"]
assert httpx.post(f"{BASE}/api/friends/request", json={"username": ub["username"]}, headers=a.headers).status_code == 200
b.post(f"/api/friends/{ua['id']}/accept")
p = b.get(f"/api/friends/{ua['id']}").json()
assert p["lab"] and p["lab"]["found"] >= 3 and p["lab"]["total"] == len(D.SPECIES) and "coins" not in p["lab"]
print("OK: изоляция, коллекция друга")

# ----------------------------------------------------------------------------- удаление пользователя
for t in ("lab_trees", "lab_seen", "lab_upgrades"):
    assert sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (ua["id"],))[0][0] > 0, t
assert adm.delete(f"/api/admin/users/{ua['id']}").status_code == 200
for t in ("lab_trees", "lab_cones", "lab_seen", "lab_upgrades"):
    assert sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (ua["id"],))[0][0] == 0, t
print("OK: удаление пользователя чистит лабораторию")
print("ALL OK")
