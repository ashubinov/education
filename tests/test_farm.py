"""Ферма: данные, грядки, урожай, склад, животные, кухня, рынок, заказы, обмен монет, изоляция, гонки, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_farm.py http://127.0.0.1:8011"""
import json
import os
import sqlite3
import sys
import threading
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import farm_data as D  # noqa: E402
from server import gamification as gm  # noqa: E402

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
assert [D.level_of(x) for x in (0, 39, 40, 159, 160, 10**9)] == [1, 1, 2, 2, 3, D.MAX_LEVEL]
assert len(D.CROPS) >= 8 and len(D.ANIMALS) >= 4 and len(D.RECIPES) >= 6
for cid, c in D.CROPS.items():
    assert cid in D.ITEMS and c["seed"] > 0 and c["time"] > 0 and c["yield"] >= 1 and c["seed"] < D.ITEMS[cid]["price"] * c["yield"], f"культура {cid} должна окупаться"
for aid, a in D.ANIMALS.items():
    assert a["feed"] in D.CROPS and a["product"] in D.ITEMS and a["price"] > 0
    assert D.ITEMS[a["product"]]["price"] > D.ITEMS[a["feed"]]["price"], f"{aid}: продукт дороже корма"
for rid, r in D.RECIPES.items():
    assert rid in D.ITEMS and all(i in D.ITEMS for i in r["needs"])
    cost = sum(D.ITEMS[i]["price"] * q for i, q in r["needs"].items())
    assert D.ITEMS[rid]["price"] * r["qty"] > cost * 0.8, f"рецепт {rid} не должен быть убыточным"
assert all(D.unlock_level(i) <= D.MAX_LEVEL for i in D.ITEMS)
day = "2026-01-01"
assert all(0.85 <= D.price_mult(i, day) <= 1.3 and D.sell_price(i, day) >= 1 for i in D.ITEMS)
assert D.price_mult("egg", day) == D.price_mult("egg", day)
assert D.plot_cost(D.START_PLOTS) < D.plot_cost(D.START_PLOTS + 1)
# склад растёт до 5000 мест, каждое расширение дороже предыдущего и ведёт в точку, где ещё есть что покупать
assert D.MAX_CAPACITY == 5000 and D.capacity_cost(D.MAX_CAPACITY) is None and D.next_capacity(D.MAX_CAPACITY) is None
cap, prev, steps = D.START_CAPACITY, 0, 0
while cap < D.MAX_CAPACITY:
    cost, nxt = D.capacity_cost(cap), D.next_capacity(cap)
    assert nxt > cap and cost > prev, (cap, cost, prev)
    cap, prev, steps = nxt, cost, steps + 1
assert cap == 5000 and D.next_capacity(270) == 300 and D.next_capacity(300) > 300
# кухня: 10 очередей, первые 4 по обычной цене, шесть последних — 50 000, 100 000, ... 300 000
assert D.KITCHEN_MAX == 10 and D.KITCHEN_NORMAL == 4 and D.kitchen_cost(0) == 0 and D.kitchen_cost(10) is None
assert [D.kitchen_cost(i) for i in range(4, 10)] == [50000 * k for k in range(1, 7)]
assert [D.kitchen_cost(i) for i in range(1, 4)] == sorted(D.kitchen_cost(i) for i in range(1, 4)) and 0 < D.kitchen_cost(1) < D.kitchen_cost(3) < 50000
print("OK: данные — культуры, животные, рецепты, уровни, цены, склад и очереди кухни")

adm, au = reg("adm")
a, ua = reg("farma")
b, ub = reg("farmb")
assert httpx.get(BASE + "/api/farm").status_code == 401
for path in ("plant", "harvest", "sell", "cook", "exchange", "order/deliver"):
    assert httpx.post(f"{BASE}/api/farm/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- старт
s = a.get("/api/farm").json()
f = s["farm"]
assert f["level"] == 1 and f["coins"] == D.START_COINS and f["plots"] == D.START_PLOTS and f["capacity"] == D.START_CAPACITY and f["stock_total"] == 0
assert len(s["plots"]) == D.START_PLOTS and all(p["state"] == "empty" for p in s["plots"])
assert len(s["orders"]) == D.ORDER_SLOTS and all(o["items"] and o["coins"] > 0 for o in s["orders"])
assert all(D.unlock_level(i) <= 1 for o in s["orders"] for i in o["items"]), "на 1 уровне просят только то, что уже можно получить"
assert set(s["prices"]) == set(D.ITEMS) and s["exchange"]["balance"] == 1000 
print("OK: старт фермы")

# ----------------------------------------------------------------------------- посадка
assert "уровне" in err(a.post("/api/farm/plant", json={"crop": "pumpkin", "idx": 0}))
assert "нет" in err(a.post("/api/farm/plant", json={"crop": "bogus", "idx": 0}))
assert "нет" in err(a.post("/api/farm/plant", json={"crop": "wheat", "idx": 9}))
assert a.post("/api/farm/plant", json={"crop": "wheat", "idx": 99999}).status_code == 422
assert a.post("/api/farm/plant", json={"crop": "wheat", "extra": 1}).status_code == 422
s = a.post("/api/farm/plant", json={"crop": "wheat", "idx": 0}).json()
assert s["farm"]["coins"] == D.START_COINS - 4 and s["plots"][0]["state"] == "growing" and 0 < s["plots"][0]["left"] <= 180
assert "занята" in err(a.post("/api/farm/plant", json={"crop": "wheat", "idx": 0}))
assert "Нет готового" in err(a.post("/api/farm/harvest", json={}))
assert "нечего" in err(a.post("/api/farm/harvest", json={"idx": 0}))
s = a.post("/api/farm/plant", json={"crop": "carrot"}).json()   # на все пустые
assert [p["state"] for p in s["plots"]] == ["growing"] * 4 and s["farm"]["coins"] == D.START_COINS - 4 - 3 * 6
assert "Свободных" in err(a.post("/api/farm/plant", json={"crop": "wheat"}))
print("OK: посадка — уровень, занятость, массовая посадка, монеты")

# ----------------------------------------------------------------------------- урожай и опыт
sql("UPDATE farm_plots SET ready_at=? WHERE user_id=? AND idx IN (0,1)", (time.time() - 5, ua["id"]))
s = a.get("/api/farm").json()
assert [p["state"] for p in s["plots"]] == ["ready", "ready", "growing", "growing"]
s = a.post("/api/farm/harvest", json={"idx": 0}).json()
assert s["stock"] == {"wheat": 3} and s["plots"][0]["state"] == "empty" and s["farm"]["xp"] == 2
s = a.post("/api/farm/harvest", json={}).json()   # остальное готовое
assert s["stock"] == {"wheat": 3, "carrot": 3} and s["farm"]["xp"] == 5
print("OK: урожай — на склад, опыт")

# склад: место ограничено (на складе 6, готовы ещё две грядки по 3)
sql("UPDATE farm SET capacity=7 WHERE user_id=?", (ua["id"],))
sql("UPDATE farm_plots SET ready_at=? WHERE user_id=?", (time.time() - 5, ua["id"]))
assert "нет места" in err(a.post("/api/farm/harvest", json={}))
sql("UPDATE farm SET capacity=9 WHERE user_id=?", (ua["id"],))
s = a.post("/api/farm/harvest", json={}).json()
assert s["farm"]["stock_total"] == 9 and any(e["type"] == "full" and e["left"] == 1 for e in s["events"])
assert [p["state"] for p in s["plots"]].count("ready") == 1, "что не влезло — осталось расти на грядке"
sql("UPDATE farm SET capacity=? WHERE user_id=?", (D.START_CAPACITY, ua["id"]))
print("OK: склад — лимит места")

# уровень: опыт от урожая
sql("UPDATE farm SET xp=39 WHERE user_id=?", (ua["id"],))
sql("DELETE FROM farm_plots WHERE user_id=?", (ua["id"],))
a.post("/api/farm/plant", json={"crop": "wheat", "idx": 0})
sql("UPDATE farm_plots SET ready_at=? WHERE user_id=?", (time.time() - 5, ua["id"]))
s = a.post("/api/farm/harvest", json={"idx": 0}).json()
assert s["farm"]["level"] == 2 and any(e["type"] == "level" and e["level"] == 2 for e in s["events"])
print("OK: повышение уровня")

# ----------------------------------------------------------------------------- грядки и склад — покупки
sql("UPDATE farm SET coins=1000 WHERE user_id=?", (ua["id"],))
cost = D.plot_cost(D.START_PLOTS)
s = a.post("/api/farm/plot/buy").json()
assert s["farm"]["plots"] == D.START_PLOTS + 1 and s["farm"]["coins"] == 1000 - cost and len(s["plots"]) == D.START_PLOTS + 1
cap_cost = D.capacity_cost(D.START_CAPACITY)
s = a.post("/api/farm/storage/upgrade").json()
assert s["farm"]["capacity"] == D.START_CAPACITY + D.CAPACITY_STEP and s["farm"]["coins"] == 1000 - cost - cap_cost
sql("UPDATE farm SET coins=0 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/farm/plot/buy")) and "монет" in err(a.post("/api/farm/storage/upgrade"))
sql("UPDATE farm SET coins=100000, plots=?, capacity=? WHERE user_id=?", (D.MAX_PLOTS, D.MAX_CAPACITY, ua["id"]))
assert "не бывает" in err(a.post("/api/farm/plot/buy")) and "самый" in err(a.post("/api/farm/storage/upgrade"))
sql("UPDATE farm SET coins=1000, plots=?, capacity=? WHERE user_id=?", (D.START_PLOTS, D.START_CAPACITY, ua["id"]))
print("OK: покупка грядок и склада, пределы")

# ----------------------------------------------------------------------------- животные
sql("UPDATE farm SET xp=0 WHERE user_id=?", (ua["id"],))
assert "уровне" in err(a.post("/api/farm/animal/buy", json={"pen": "chicken"}))
sql("UPDATE farm SET xp=40 WHERE user_id=?", (ua["id"],))
assert "нет" in err(a.post("/api/farm/animal/buy", json={"pen": "dino"}))
assert "Сначала купи" in err(a.post("/api/farm/animal/feed", json={"pen": "chicken"}))
for i in range(D.PEN_SIZE):
    s = a.post("/api/farm/animal/buy", json={"pen": "chicken"}).json()
pen = next(p for p in s["pens"] if p["pen"] == "chicken")
assert pen["count"] == D.PEN_SIZE and pen["state"] == "idle" and s["farm"]["coins"] == 1000 - 50 * D.PEN_SIZE
assert "полон" in err(a.post("/api/farm/animal/buy", json={"pen": "chicken"}))
sql("DELETE FROM farm_stock WHERE user_id=?", (ua["id"],))
assert "Нужно" in err(a.post("/api/farm/animal/feed", json={"pen": "chicken"}))
sql("INSERT INTO farm_stock(user_id, item, qty) VALUES(?, 'wheat', 5)", (ua["id"],))
s = a.post("/api/farm/animal/feed", json={"pen": "chicken"}).json()
pen = next(p for p in s["pens"] if p["pen"] == "chicken")
assert pen["state"] == "working" and pen["fed_count"] == D.PEN_SIZE and s["stock"] == {"wheat": 1}
assert "заняты" in err(a.post("/api/farm/animal/feed", json={"pen": "chicken"}))
assert "Ещё не готово" in err(a.post("/api/farm/animal/collect", json={"pen": "chicken"}))
sql("UPDATE farm_pens SET ready_at=? WHERE user_id=? AND pen='chicken'", (time.time() - 1, ua["id"]))
assert "Уже накормлены" in err(a.post("/api/farm/animal/feed", json={"pen": "chicken"}))
xp0 = a.get("/api/farm").json()["farm"]["xp"]
s = a.post("/api/farm/animal/collect", json={"pen": "chicken"}).json()
assert s["stock"] == {"wheat": 1, "egg": D.PEN_SIZE} and s["farm"]["xp"] == xp0 + D.ANIMALS["chicken"]["xp"] * D.PEN_SIZE
assert next(p for p in s["pens"] if p["pen"] == "chicken")["state"] == "idle"
assert "нечего" in err(a.post("/api/farm/animal/collect", json={"pen": "chicken"}))
print("OK: животные — покупка, корм, цикл, сбор")

# ----------------------------------------------------------------------------- кухня
assert "нет" in err(a.post("/api/farm/cook", json={"recipe": "bogus"}))
assert "Не хватает" in err(a.post("/api/farm/cook", json={"recipe": "bread"}))
sql("INSERT INTO farm_stock(user_id, item, qty) VALUES(?, 'wheat', 5) ON CONFLICT(user_id,item) DO UPDATE SET qty=5", (ua["id"],))
k0 = a.get("/api/farm").json()["kitchen"]
assert k0["count"] == 1 and k0["max"] == D.KITCHEN_MAX and k0["queues"] == [{"slot": 0, "state": "empty"}] and k0["next_cost"] == D.kitchen_cost(1)
s = a.post("/api/farm/cook", json={"recipe": "bread"}).json()
assert s["kitchen"]["queues"][0]["state"] == "working" and s["stock"]["wheat"] == 2
assert "заняты" in err(a.post("/api/farm/cook", json={"recipe": "bread"}))
assert "Ещё не готово" in err(a.post("/api/farm/cook/collect"))
sql("UPDATE farm_kitchen SET ready_at=? WHERE user_id=?", (time.time() - 1, ua["id"]))
assert a.get("/api/farm").json()["kitchen"]["queues"][0]["state"] == "ready" and a.get("/api/farm").json()["kitchen"]["ready"] == 1
s = a.post("/api/farm/cook/collect").json()
assert s["stock"]["bread"] == D.RECIPES["bread"]["qty"] and s["kitchen"]["queues"][0]["state"] == "empty"
assert "ничего" in err(a.post("/api/farm/cook/collect"))
sql("UPDATE farm SET xp=0 WHERE user_id=?", (ua["id"],))
assert "уровне" in err(a.post("/api/farm/cook", json={"recipe": "pie"}))
print("OK: кухня — рецепт, время, сбор")

# ----------------------------------------------------------------------------- очереди кухни: покупка, параллельная готовка, сбор
sql("UPDATE farm SET xp=160, coins=0, capacity=500 WHERE user_id=?", (ua["id"],))
assert a.post("/api/farm/cook", json={"recipe": "bread", "slot": 10}).status_code == 422 and a.post("/api/farm/cook", json={"recipe": "bread", "slot": -1}).status_code == 422
assert "нет" in err(a.post("/api/farm/cook", json={"recipe": "bread", "slot": 1})), "чужой очереди не бывает"
sql("INSERT INTO farm_stock(user_id, item, qty) VALUES(?, 'wheat', 100) ON CONFLICT(user_id,item) DO UPDATE SET qty=100", (ua["id"],))
assert "монет" in err(a.post("/api/farm/kitchen/buy"))
total_cost = sum(D.kitchen_cost(i) for i in range(1, D.KITCHEN_MAX))
sql("UPDATE farm SET coins=? WHERE user_id=?", (total_cost + 5, ua["id"]))
for n in range(1, D.KITCHEN_MAX):
    before = a.get("/api/farm").json()["farm"]["coins"]
    s = a.post("/api/farm/kitchen/buy").json()
    assert s["kitchen"]["count"] == n + 1 and len(s["kitchen"]["queues"]) == n + 1 and s["farm"]["coins"] == before - D.kitchen_cost(n), n
assert s["farm"]["coins"] == 5 and s["kitchen"]["next_cost"] is None
assert "не бывает" in err(a.post("/api/farm/kitchen/buy"))
for i in range(D.KITCHEN_MAX):
    s = a.post("/api/farm/cook", json={"recipe": "bread"}).json()
    assert [q["state"] for q in s["kitchen"]["queues"]].count("working") == i + 1, "каждое блюдо идёт в свою очередь"
assert "заняты" in err(a.post("/api/farm/cook", json={"recipe": "bread"})) and "занята" in err(a.post("/api/farm/cook", json={"recipe": "bread", "slot": 4}))
sql("UPDATE farm_kitchen SET ready_at=? WHERE user_id=? AND slot IN (0, 3, 7)", (time.time() - 1, ua["id"]))
bread0 = a.get("/api/farm").json()["stock"].get("bread", 0)
s = a.post("/api/farm/cook/collect", json={"slot": 3}).json()
assert s["kitchen"]["queues"][3]["state"] == "empty" and s["kitchen"]["ready"] == 2 and s["stock"]["bread"] == bread0 + D.RECIPES["bread"]["qty"]
assert "Ещё не готово" in err(a.post("/api/farm/cook/collect", json={"slot": 5}))
s = a.post("/api/farm/cook/collect").json()      # без номера — из всех готовых
assert s["kitchen"]["ready"] == 0 and s["kitchen"]["working"] == D.KITCHEN_MAX - 3
# не влезает на склад — забирается сколько помещается, остальное ждёт
sql("UPDATE farm_kitchen SET ready_at=? WHERE user_id=?", (time.time() - 1, ua["id"]))
used = a.get("/api/farm").json()["farm"]
sql("UPDATE farm SET capacity=? WHERE user_id=?", (used["stock_total"] + D.RECIPES["bread"]["qty"], ua["id"]))
s = a.post("/api/farm/cook/collect").json()
assert s["kitchen"]["ready"] == D.KITCHEN_MAX - 3 - 1 and any(e["type"] == "full" for e in s["events"])
assert "нет места" in err(a.post("/api/farm/cook/collect"))
sql("DELETE FROM farm_kitchen WHERE user_id=?", (ua["id"],))
sql("DELETE FROM farm_stock WHERE user_id=? AND item IN ('wheat', 'bread')", (ua["id"],))
sql("UPDATE farm SET capacity=?, coins=0, xp=0 WHERE user_id=?", (D.START_CAPACITY, ua["id"]))
print("OK: кухня — очереди: покупка по ценам, 10 одновременных блюд, сбор по номеру и всех сразу, склад")

# ----------------------------------------------------------------------------- склад до 5000 мест
f0 = a.get("/api/farm").json()["farm"]
assert f0["capacity"] == D.START_CAPACITY and f0["capacity_next"] == D.START_CAPACITY + D.CAPACITY_STEP and f0["max_capacity"] == 5000
sql("UPDATE farm SET coins=0 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/farm/storage/upgrade"))
sql("UPDATE farm SET coins=10000000 WHERE user_id=?", (ua["id"],))
caps = [f0["capacity"]]
while True:
    r = a.post("/api/farm/storage/upgrade")
    if r.status_code != 200:
        assert "самый большой" in err(r)
        break
    caps.append(r.json()["farm"]["capacity"])
assert caps[-1] == 5000 and caps == sorted(set(caps)) and 300 in caps and caps[caps.index(300) + 1] > 300
fin = a.get("/api/farm").json()["farm"]
assert fin["capacity_cost"] is None and fin["capacity_next"] is None
sql("UPDATE farm SET capacity=?, coins=0 WHERE user_id=?", (D.START_CAPACITY, ua["id"]))
print("OK: склад — расширяется ступенями до 5000 и дальше не растёт")

# ----------------------------------------------------------------------------- рынок
sql("UPDATE farm SET xp=40, coins=10 WHERE user_id=?", (ua["id"],))
today = gm.today()
price = D.sell_price("egg", today)
s = a.post("/api/farm/sell", json={"item": "egg", "qty": 2}).json()
assert s["sold"] == {"item": "egg", "qty": 2, "coins": price * 2} and s["farm"]["coins"] == 10 + price * 2 and s["stock"]["egg"] == D.PEN_SIZE - 2
s = a.post("/api/farm/sell", json={"item": "egg"}).json()    # всё
assert "egg" not in s["stock"]
assert "нет" in err(a.post("/api/farm/sell", json={"item": "egg"}))
assert "нет" in err(a.post("/api/farm/sell", json={"item": "bogus"}))
assert a.post("/api/farm/sell", json={"item": "egg", "qty": 0}).status_code == 422
print("OK: рынок — цена дня, продажа")

# ----------------------------------------------------------------------------- заказы
o = a.get("/api/farm").json()["orders"][0]
for it, q in o["items"].items():
    sql("INSERT INTO farm_stock(user_id, item, qty) VALUES(?,?,0) ON CONFLICT(user_id,item) DO NOTHING", (ua["id"], it))
sql("DELETE FROM farm_stock WHERE user_id=?", (ua["id"],))
assert "Не хватает" in err(a.post("/api/farm/order/deliver", json={"slot": 0}))
for it, q in o["items"].items():
    sql("INSERT INTO farm_stock(user_id, item, qty) VALUES(?,?,?)", (ua["id"], it, q + 1))
before = a.get("/api/farm").json()["farm"]
s = a.post("/api/farm/order/deliver", json={"slot": 0}).json()
assert s["farm"]["coins"] == before["coins"] + o["coins"] and s["farm"]["xp"] == before["xp"] + o["xp"]
assert all(s["stock"].get(it, 0) == 1 for it in o["items"]) and len(s["orders"]) == D.ORDER_SLOTS
assert any(e["type"] == "order" for e in s["events"])
assert a.post("/api/farm/order/deliver", json={"slot": 7}).status_code == 422
c0 = s["farm"]["coins"]
s = a.post("/api/farm/order/skip", json={"slot": 1}).json()
assert s["farm"]["coins"] == c0 - 5
sql("UPDATE farm SET coins=1 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/farm/order/skip", json={"slot": 1}))
print("OK: заказы — сдача, награда, замена, пропуск")

# ----------------------------------------------------------------------------- обмен на жетоны
sql("UPDATE farm SET coins=10000 WHERE user_id=?", (ua["id"],))
bal0 = a.get("/api/farm").json()["exchange"]["balance"]
s = a.post("/api/farm/exchange", json={"direction": "out", "amount": 100}).json()
assert D.EXCHANGE_OUT_RATE == 1 and D.EXCHANGE_IN_RATE == 1, "курс обмена — 1 к 1 в обе стороны"
assert s["exchange"]["balance"] == bal0 + 100 and s["farm"]["coins"] == 10000 - 100
assert "out_left" not in s["exchange"] and "out_daily" not in s["exchange"], "суточного лимита нет"
# выводить можно сколько угодно: много раз подряд и сразу большой суммой, без «лимита в сутки»
for _ in range(4):
    s = a.post("/api/farm/exchange", json={"direction": "out", "amount": 300}).json()
assert s["exchange"]["balance"] == bal0 + 100 + 1200 and s["farm"]["coins"] == 10000 - 1300
s = a.post("/api/farm/exchange", json={"direction": "out", "amount": 8700}).json()
assert s["farm"]["coins"] == 0 and s["exchange"]["balance"] == bal0 + 10000, "можно вывести все монеты разом"
sql("UPDATE farm SET coins=10000 WHERE user_id=?", (ua["id"],))
s = a.post("/api/farm/exchange", json={"direction": "in", "amount": 50}).json()
assert s["exchange"]["balance"] == bal0 + 10000 - 50 and s["farm"]["coins"] == 10000 + 50 * D.EXCHANGE_IN_RATE
assert "Недостаточно" in err(a.post("/api/farm/exchange", json={"direction": "in", "amount": 10**8}))
sql("UPDATE farm SET coins=3 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/farm/exchange", json={"direction": "out", "amount": 5}))
assert a.post("/api/farm/exchange", json={"direction": "sideways", "amount": 5}).status_code == 422
assert a.post("/api/farm/exchange", json={"direction": "in", "amount": -5}).status_code == 422
print("OK: обмен — курс 1 к 1, без суточного лимита")

# ----------------------------------------------------------------------------- гонка: двойной сбор не удваивает урожай
sql("DELETE FROM farm_plots WHERE user_id=?", (ub["id"],))
b.get("/api/farm")
sql("UPDATE farm SET coins=1000 WHERE user_id=?", (ub["id"],))
b.post("/api/farm/plant", json={"crop": "wheat"})
sql("UPDATE farm_plots SET ready_at=? WHERE user_id=?", (time.time() - 5, ub["id"]))
res = []
def go():
    res.append(httpx.post(f"{BASE}/api/farm/harvest", json={}, headers=b.headers, timeout=30).status_code)
th = [threading.Thread(target=go) for _ in range(6)]
[t.start() for t in th]
[t.join() for t in th]
st = b.get("/api/farm").json()
assert st["stock"].get("wheat") == 3 * D.START_PLOTS and res.count(200) == 1, (res, st["stock"])
print("OK: гонка — урожай собирается один раз")

# ----------------------------------------------------------------------------- изоляция и друзья
sa = a.get("/api/farm").json(); sb = b.get("/api/farm").json()
assert sa["stock"] != sb["stock"] and sa["farm"]["xp"] != sb["farm"]["xp"]
assert httpx.post(f"{BASE}/api/friends/request", json={"username": ub["username"]}, headers=a.headers).status_code == 200
b.post(f"/api/friends/{ua['id']}/accept")
p = a.get(f"/api/friends/{ub['id']}").json()
assert p["farm"] and p["farm"]["plots"] == D.START_PLOTS and p["farm"]["level"] == 1 and "coins" not in p["farm"]
assert adm.get(f"/api/friends/{ub['id']}").status_code in (403, 404)  # чужие не видят
print("OK: изоляция, ферма друга на его странице")

# ----------------------------------------------------------------------------- удаление
for t in ("farm", "farm_stock", "farm_orders"):
    assert sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (ub["id"],))[0][0] > 0, t
assert adm.delete(f"/api/admin/users/{ub['id']}").status_code == 200
for t in ("farm", "farm_plots", "farm_pens", "farm_stock", "farm_kitchen", "farm_orders"):
    assert sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (ub["id"],))[0][0] == 0, t
print("OK: удаление игрока чистит ферму")
print("ALL OK")
