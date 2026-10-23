"""Караван: данные, замок по уровню, груз со склада, цена и разбойники (определены при отправке), возвращение, дороги, улучшения, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_caravan.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import caravan as C  # noqa: E402
from server import caravan_data as D  # noqa: E402
from server import farm_data as FD  # noqa: E402

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
tt = [D.TOWNS[k]["time"] for k in D.TOWNS]
mm = [D.TOWNS[k]["mult"] for k in D.TOWNS]
rr = [D.TOWNS[k]["risk"] for k in D.TOWNS]
assert tt == sorted(tt) and mm == sorted(mm) and rr == sorted(rr), "чем дальше город, тем дольше, дороже и опаснее"
day = "2026-10-01"
for t in D.TOWNS:
    w = D.wanted(t, day)
    assert len(w) == 2 and len(set(w)) == 2 and all(i in FD.ITEMS for i in w) and w == D.wanted(t, day), "ходовые товары: два разных, одинаково весь день"
t0 = "village"
w0 = D.wanted(t0, day)[0]
other = next(i for i in FD.ITEMS if i not in D.wanted(t0, day))
assert D.unit_price(w0, t0, day, 0) == int(FD.ITEMS[w0]["price"] * D.TOWNS[t0]["mult"] * D.WANT_MULT)
assert D.unit_price(other, t0, day, 0) == int(FD.ITEMS[other]["price"] * D.TOWNS[t0]["mult"])
assert D.unit_price(other, t0, day, 5) > D.unit_price(other, t0, day, 0)
assert D.town_open("village", 0) and D.town_open("harbor", 0) and not D.town_open("fort", 0) and D.town_open("fort", 1) and not D.town_open("capital", 1) and D.town_open("capital", 2)
assert D.trip_time("fort", 5) < D.trip_time("fort", 0) and D.bandit_chance("fort", 4) < D.bandit_chance("fort", 0) and D.bandit_chance("village", 5) == 0
assert D.wagons_count(3) == 4 and D.cargo_cap(2) == D.BASE_CARGO + 2 * D.CARGO_STEP
for u in D.UPGRADES.values():
    assert u["costs"] == sorted(u["costs"]) and u["max"] == len(u["costs"])
print("OK: данные — города, спрос, цены, дороги")

adm, au = reg("cvadm")
a, ua = reg("cva")
b, ub = reg("cvb")
assert httpx.get(BASE + "/api/caravan").status_code == 401
for path in ("send", "collect", "upgrade"):
    assert httpx.post(f"{BASE}/api/caravan/{path}", json={}).status_code in (401, 422)

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/caravan").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/caravan/send", json={"wagon": 0, "town": "village", "cargo": {"wheat": 1}}))
sql("UPDATE farm SET xp=?, coins=100000 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/caravan").json()
assert s["unlocked"] and len(s["wagons"]) == D.START_WAGONS and s["cargo_cap"] == D.BASE_CARGO
assert {t["id"]: t["locked"] for t in s["towns"]} == {"village": False, "harbor": False, "fort": True, "capital": True}
print("OK: замок — открывается на нужном уровне")

# ----------------------------------------------------------------------------- отправка
give(ua["id"], "wheat", 40)
give(ua["id"], "pie", 5)
ok = {"wagon": 0, "town": "village", "cargo": {"wheat": 10}}
assert a.post("/api/caravan/send", json={**ok, "town": "nonsense"}).status_code == 400
assert a.post("/api/caravan/send", json={**ok, "cargo": {}}).status_code == 400
assert a.post("/api/caravan/send", json={**ok, "cargo": {"nonsense": 3}}).status_code == 400
assert a.post("/api/caravan/send", json={**ok, "cargo": {"wheat": -1}}).status_code == 400
assert "помещается" in err(a.post("/api/caravan/send", json={**ok, "cargo": {"wheat": D.BASE_CARGO + 1}}))
assert "нет столько" in err(a.post("/api/caravan/send", json={**ok, "cargo": {"corn": 2}}))
assert "не построена" in err(a.post("/api/caravan/send", json={**ok, "town": "fort"}))
assert "нет" in err(a.post("/api/caravan/send", json={**ok, "wagon": 3}))
assert a.post("/api/caravan/send", json={**ok, "x": 1}).status_code == 422
s = a.post("/api/caravan/send", json={**ok, "cargo": {"wheat": 10, "pie": 2}}).json()
w = s["wagons"][0]
expect = D.unit_price("wheat", "village", day := __import__("datetime").date.today().isoformat(), 0) * 10 + D.unit_price("pie", "village", day, 0) * 2
assert w["state"] == "away" and w["left"] > 0 and w["payout"] == expect and s["stock"].get("wheat") == 30 and s["stock"].get("pie") == 3, "груз списан со склада, выручка зафиксирована"
assert "в пути" in err(a.post("/api/caravan/send", json=ok))
assert "ещё не вернулись" in err(a.post("/api/caravan/collect", json={"wagon": None}))
print("OK: рейс — груз со склада, цена зафиксирована")

# ----------------------------------------------------------------------------- возвращение, разбойники
sql("UPDATE caravan_trips SET ready_at=0, bandits=0 WHERE user_id=?", (ua["id"],))
c0 = a.get("/api/caravan").json()["coins"]
s = a.post("/api/caravan/collect", json={"wagon": 0}).json()
assert s["coins"] == c0 + expect and s["wagons"][0]["state"] == "home" and s["stats"] == {"trips": 1, "earned": expect}
ev = s["events"][0]
assert ev["type"] == "trade" and ev["coins"] == expect and not ev["bandits"] and ev["lost"] == 0
give(ua["id"], "wheat", 20)
a.post("/api/caravan/send", json={"wagon": 0, "town": "village", "cargo": {"wheat": 20}})
pay = sql("SELECT payout FROM caravan_trips WHERE user_id=?", (ua["id"],))[0][0]
sql("UPDATE caravan_trips SET ready_at=0, bandits=1 WHERE user_id=?", (ua["id"],))
c0 = a.get("/api/caravan").json()["coins"]
s = a.post("/api/caravan/collect", json={"wagon": None}).json()
ev = s["events"][0]
assert ev["bandits"] and ev["coins"] == int(pay * (1 - D.BANDIT_LOSS)) and ev["lost"] == pay - ev["coins"] and s["coins"] == c0 + ev["coins"]
assert "ещё не вернулись" in err(a.post("/api/caravan/collect", json={"wagon": None}))
print("OK: возвращение — выручка, нападение разбойников")

# ----------------------------------------------------------------------------- улучшения
s = a.post("/api/caravan/upgrade", json={"key": "roads"}).json()
assert {t["id"]: t["locked"] for t in s["towns"]}["fort"] is False
give(ua["id"], "wheat", 5)
s = a.post("/api/caravan/send", json={"wagon": 0, "town": "fort", "cargo": {"wheat": 5}}).json()
assert s["wagons"][0]["town"] == "fort"
s = a.post("/api/caravan/upgrade", json={"key": "wagons"}).json()
assert len(s["wagons"]) == D.START_WAGONS + 1
assert a.post("/api/caravan/upgrade", json={"key": "nonsense"}).status_code == 400
for _ in range(10):
    r = a.post("/api/caravan/upgrade", json={"key": "cart"})
    if r.status_code != 200:
        assert "максимуме" in r.json()["detail"]
        break
else:
    raise AssertionError("грузоподъёмность должна дойти до максимума")
sql("UPDATE farm SET coins=1 WHERE user_id=?", (ua["id"],))
assert "монет" in err(a.post("/api/caravan/upgrade", json={"key": "guard"}))
print("OK: улучшения — дороги, повозки, грузоподъёмность")

# ----------------------------------------------------------------------------- изоляция и удаление
sb = b.get("/api/caravan").json()
assert sb["unlocked"] is False and all(w["state"] == "home" for w in sb["wagons"])
assert C.ready_count(ua["id"]) >= 0
C.delete_user_data(ua["id"])
for t in ("caravan_trips", "caravan_upgrades", "caravan_stats"):
    assert not sql(f"SELECT 1 FROM {t} WHERE user_id=?", (ua["id"],)), t
print("OK: изоляция игроков и удаление данных")
print("ALL OK")
