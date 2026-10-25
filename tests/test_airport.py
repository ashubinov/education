"""Аэропорт («Клубничные авиалинии»): данные и табло, замок по уровню, покупка билета, объявления по времени, возврат, компенсация, гонки, изоляция, удаление.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_airport.py http://127.0.0.1:8011"""
import json
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
from server import airport_data as D  # noqa: E402

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
assert D.AIRLINE == "Клубничные авиалинии" and D.TICKET_PRICE > 0
assert (D.COMP_MIN + D.COMP_MAX) / 2 < D.TICKET_PRICE, "компенсация в среднем меньше билета: аэропорт не источник монет"
assert D.rank_of(0) == D.RANKS[0][1] and D.rank_of(10 ** 6) == D.RANKS[-1][1] and D.next_rank(10 ** 6) is None and D.next_rank(0)[0] == D.RANKS[1][0]
b1 = D.board(1_700_000_000)
assert b1 == D.board(1_700_000_000 + 60), "табло одно на всех и меняется раз в 15 минут"
assert len(b1) == 7 and all(r["no"].startswith(D.CODE + "-") and r["status"] in ("cancel", "moved", "phantom", "wait") for r in b1)
many = [r for k in range(60) for r in D.board(1_700_000_000 + k * 900)]
assert sum(r["status"] == "cancel" for r in many) > len(many) * .35, "рейсы в основном отменяются"
assert not any(r["status"] == "ok" for r in many), "нормальных рейсов нет вообще"
assert all("new_sched" in r and r["new_sched"] > r["sched"] for r in many if r["status"] == "moved")
for seed in range(300):
    tr = D.roll_trip(random.Random(seed))
    st = tr["steps"]
    assert st[0]["at"] == 0 and st[0]["kind"] == "info" and st[-1]["kind"] == "land" and tr["total"] == st[-1]["at"]
    assert [s["at"] for s in st] == sorted(s["at"] for s in st), "объявления идут по порядку"
    assert tr["cancels"] == sum(s["kind"] == "cancel" for s in st) >= 1, "хотя бы одна отмена за путь"
    assert D.COMP_MIN <= tr["reward"] <= D.COMP_MAX and st[-1]["flight"] == tr["flight"]
    assert 2 * 3600 <= tr["total"] <= 8 * 3600, "летят долго"
    assert all(s["flight"].startswith(D.CODE + "-") for s in st)
print("OK: данные — табло, рейсы, отмены, компенсация")

adm, au = reg("airadm")
a, ua = reg("aira")
b, ub = reg("airb")
assert httpx.get(BASE + "/api/airport").status_code == 401
for path in ("ticket", "claim", "refund"):
    assert httpx.post(f"{BASE}/api/airport/{path}", json={}).status_code == 401

# ----------------------------------------------------------------------------- замок по уровню
s = a.get("/api/airport").json()
assert s["unlocked"] is False and s["need_level"] == D.UNLOCK_FARM_LEVEL and s["trip"] is None and len(s["board"]) == 7
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/airport/ticket"))
assert str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/airport/claim")) and str(D.UNLOCK_FARM_LEVEL) in err(a.post("/api/airport/refund"))
sql("UPDATE farm SET xp=?, coins=0 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ua["id"]))
s = a.get("/api/airport").json()
assert s["unlocked"] is True and s["trip"] is None and s["stats"]["rank"] == D.RANKS[0][1]
print("OK: замок по уровню фермы")

# ----------------------------------------------------------------------------- билет
assert "монет" in err(a.post("/api/airport/ticket"))
assert "нет билета" in err(a.post("/api/airport/claim")) and "нет билета" in err(a.post("/api/airport/refund"))
sql("UPDATE farm SET coins=1000 WHERE user_id=?", (ua["id"],))
s = a.post("/api/airport/ticket").json()
assert s["coins"] == 1000 - D.TICKET_PRICE and any(e["type"] == "ticket" for e in s["events"])
t = s["trip"]
assert t["state"] == "waiting" and len(t["log"]) == 1 and t["log"][0]["kind"] == "info" and t["reward"] is None and t["comp_text"] is None, "награда и будущие объявления скрыты"
assert t["left"] >= 2 * 3600 and t["next_in"] > 0 and t["dest"] and t["flight"].startswith(D.CODE)
assert "уже есть билет" in err(a.post("/api/airport/ticket")) and a.get("/api/airport").json()["coins"] == 1000 - D.TICKET_PRICE
assert "ещё не прилетел" in err(a.post("/api/airport/claim"))
row = sql("SELECT steps, reward, cancels FROM airport_trips WHERE user_id=?", (ua["id"],))[0]
steps, reward = json.loads(row[0]), row[1]
assert steps[-1]["kind"] == "land" and D.COMP_MIN <= reward <= D.COMP_MAX
print("OK: билет — цена, один на игрока, будущее скрыто")

# ----------------------------------------------------------------------------- объявления идут по времени
def shift(sec):
    sql("UPDATE airport_trips SET started_at=started_at-?, ready_at=ready_at-? WHERE user_id=?", (sec, sec, ua["id"]))


shift(steps[2]["at"] + 1)
t = a.get("/api/airport").json()["trip"]
assert t["state"] == "waiting" and [x["at"] for x in t["log"]] == [x["at"] for x in steps[:3]] and t["reward"] is None
assert t["flight"] == steps[2]["flight"], "номер рейса — по последнему объявлению"
assert 0 < t["next_in"] <= steps[3]["at"] - steps[2]["at"]
# вернуть билет можно, пока не прилетел: возвращается десятая часть
c0 = a.get("/api/airport").json()["coins"]
s = a.post("/api/airport/refund").json()
assert s["trip"] is None and s["coins"] == c0 + int(D.TICKET_PRICE * D.REFUND) == c0 + s["refund"] and any(e["type"] == "refund" for e in s["events"])
assert "нет билета" in err(a.post("/api/airport/refund"))
assert sql("SELECT cancels FROM airport_stats WHERE user_id=?", (ua["id"],))[0][0] == sum(x["kind"] == "cancel" for x in steps[:3])
print("OK: объявления по времени, возврат билета")

# ----------------------------------------------------------------------------- полный путь и компенсация
s = a.post("/api/airport/ticket").json()
steps = json.loads(sql("SELECT steps FROM airport_trips WHERE user_id=?", (ua["id"],))[0][0])
reward, cancels = sql("SELECT reward, cancels FROM airport_trips WHERE user_id=?", (ua["id"],))[0]
shift(steps[-1]["at"] + 5)
t = a.get("/api/airport").json()["trip"]
assert t["state"] == "landed" and t["reward"] == reward and len(t["log"]) == len(steps) and t["left"] == 0 and t["comp_text"]
assert a.get("/api/world").json()["locations"][-1] == {"id": "airport", "name": "Аэропорт", "icon": "✈️", "route": "#/farm/airport", "need_level": D.UNLOCK_FARM_LEVEL, "unlocked": True, "ready": 1}
assert "уже прилетел" in err(a.post("/api/airport/refund"))
c0 = a.get("/api/airport").json()["coins"]
res = []
def go():
    res.append(httpx.post(f"{BASE}/api/airport/claim", headers=a.headers, timeout=30).status_code)
th = [threading.Thread(target=go) for _ in range(6)]
[x.start() for x in th]
[x.join() for x in th]
s = a.get("/api/airport").json()
assert res.count(200) == 1 and s["coins"] == c0 + reward and s["trip"] is None, (res, s["coins"], c0, reward)
assert s["stats"]["trips"] == 1 and s["stats"]["comp"] == reward and s["stats"]["cancels"] >= cancels and s["stats"]["spent"] == D.TICKET_PRICE * 2
assert a.get("/api/world").json()["locations"][-1]["ready"] == 0
print("OK: полёт — компенсация один раз, статистика, значок на карте")

# ----------------------------------------------------------------------------- должность растёт с отменами
sql("UPDATE airport_stats SET cancels=? WHERE user_id=?", (D.RANKS[2][0], ua["id"]))
s = a.get("/api/airport").json()["stats"]
assert s["rank"] == D.RANKS[2][1] and s["next_rank"] == D.RANKS[3][1] and s["next_need"] == D.RANKS[3][0]

# ----------------------------------------------------------------------------- изоляция и удаление
sb = b.get("/api/airport").json()
assert sb["unlocked"] is False and sb["trip"] is None and sb["stats"]["trips"] == 0
sql("UPDATE farm SET xp=?, coins=500 WHERE user_id=?", (40 * (D.UNLOCK_FARM_LEVEL - 1) ** 2, ub["id"]))
assert b.post("/api/airport/ticket").status_code == 200 and a.get("/api/airport").json()["trip"] is None
assert sql("SELECT COUNT(*) FROM airport_trips WHERE user_id=?", (ub["id"],))[0][0] == 1
assert adm.delete(f"/api/admin/users/{ub['id']}").status_code == 200
for tb in ("airport_trips", "airport_stats"):
    assert sql(f"SELECT COUNT(*) FROM {tb} WHERE user_id=?", (ub["id"],))[0][0] == 0, tb
print("OK: изоляция, удаление игрока чистит аэропорт")
print("ALL OK")
