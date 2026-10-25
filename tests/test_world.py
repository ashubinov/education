"""Карта мира: список локаций, замки по уровню фермы и значки «что-то готово».
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_world.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import airport_data, apiary_data, caravan_data, lab_data, lake_data, mine_data  # noqa: E402

db_path = os.environ.get("LQ_DB")


def sql(q, args=()):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


d = httpx.post(f"{BASE}/api/auth/register", json={"username": f"world{int(time.time()) % 100000}", "password": "secret1"}, timeout=30).json()
a = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"})
uid = d["user"]["id"]
assert httpx.get(BASE + "/api/world").status_code == 401

w = a.get("/api/world").json()
by = {x["id"]: x for x in w["locations"]}
assert [x["id"] for x in w["locations"]] == ["farm", "lab", "lake", "apiary", "mine", "caravan", "airport"]
need = {"farm": 1, "lab": lab_data.UNLOCK_FARM_LEVEL, "lake": lake_data.UNLOCK_FARM_LEVEL, "apiary": apiary_data.UNLOCK_FARM_LEVEL, "mine": mine_data.UNLOCK_FARM_LEVEL,
        "caravan": caravan_data.UNLOCK_FARM_LEVEL, "airport": airport_data.UNLOCK_FARM_LEVEL}
assert {k: v["need_level"] for k, v in by.items()} == need
assert w["farm_level"] == 1 and by["farm"]["unlocked"] and not any(by[k]["unlocked"] for k in need if k != "farm")
assert all(v["route"].startswith("#/farm") for v in by.values())
print("OK: карта — новая ферма видит только саму ферму")

# уровень растёт — локации открываются по очереди
for lvl in (6, 8, 10, 11, 12, 13):
    sql("UPDATE farm SET xp=? WHERE user_id=?", (40 * (lvl - 1) ** 2, uid))
    got = {x["id"]: x["unlocked"] for x in a.get("/api/world").json()["locations"]}
    assert got == {k: n <= lvl for k, n in need.items()}, (lvl, got)
print("OK: карта — замки по уровню фермы")

# значки «готово»
now = time.time()
sql("INSERT INTO lake_rods(user_id,slot,bait,cast_at,ready_at) VALUES(?,?,?,?,?)", (uid, 0, "wheat", now - 100, 0))
sql("INSERT INTO apiary_hives(user_id,slot,flower,base_at,left) VALUES(?,?,?,?,?)", (uid, 0, "clover", now - 10 ** 6, -1))
sql("INSERT INTO mine_jobs(user_id,slot,tier,started_at,ready_at,loot,cave) VALUES(?,?,?,?,?,?,?)", (uid, 0, 1, now - 100, 0, '{"coal": 2}', 0))
sql("INSERT INTO mine_jobs(user_id,slot,tier,started_at,ready_at,loot,cave) VALUES(?,?,?,?,?,?,?)", (uid, 1, 1, now - 100, now + 99999, '{"coal": 2}', 0))
sql("INSERT INTO caravan_trips(user_id,slot,town,sent_at,ready_at,cargo,payout,bandits) VALUES(?,?,?,?,?,?,?,?)", (uid, 0, "village", now - 100, 0, '{"wheat": 1}', 5, 0))
sql("INSERT INTO farm_plots(user_id,idx,crop,planted_at,ready_at) VALUES(?,?,?,?,?)", (uid, 0, "wheat", now - 100, 0))
sql("INSERT INTO airport_trips(user_id,flight,dest,icon,started_at,ready_at,steps,cancels,reward,comp_text) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, "КА-100", "Джемовск", "🍯", now - 100, 0, "[]", 1, 20, "x"))
by = {x["id"]: x for x in a.get("/api/world").json()["locations"]}
assert by["lake"]["ready"] == 1 and by["apiary"]["ready"] == 1 and by["mine"]["ready"] == 1 and by["caravan"]["ready"] == 1 and by["farm"]["ready"] == 1 and by["airport"]["ready"] == 1, by
print("OK: карта — значки готовности по каждой локации")
print("ALL OK")
