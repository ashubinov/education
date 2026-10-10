"""Питомец: данные, усыновление, магазин еды, кормление, рост, голод со временем, гости (погладить), удаление, изоляция.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_pet.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import threading
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import pet_data as D  # noqa: E402
from server import slots  # noqa: E402

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


# ----------------------------------------------------------------------------- данные
assert set(D.SPECIES) == {'rooster', 'ram', 'beaver', 'doberman', 'bulldog', 'pug', 'corgi', 'chihuahua'} and len(D.FOODS) >= 10 and D.SPECIES['ram']['name'] == 'Кучерявый баран'
for sid, sp in D.SPECIES.items():
    assert sp["starter"] in D.FOODS and sp["likes"] and all(any(f["cat"] == c for f in D.FOODS.values()) for c in sp["likes"]), sid
    assert any(D.liked(sid, f) for f in D.FOODS), "у каждого вида есть любимая еда"
assert all(f["price"] > 0 and f["fill"] > 0 and f["xp"] > 0 for f in D.FOODS.values())
assert [D.stage_of(x)["id"] for x in (0, 219, 220, 899, 900, 99999)] == ["baby", "baby", "teen", "teen", "adult", "adult"]
assert D.stage_of(900)["next"] is None and D.stage_of(0)["next"] == 220
assert [D.mood_of(x) for x in (100, 60, 59, 30, 29, 1, 0)] == ["happy", "happy", "ok", "ok", "hungry", "hungry", "starving"]
print("OK: данные — виды, еда, этапы, настроение")

adm, au = reg("adm")
a, ua = reg("peta")
b, ub = reg("petb")
for path in ("/api/pet",):
    assert httpx.get(BASE + path).status_code == 401
for path, body in (("/api/pet/adopt", {"species": "doberman", "name": "Х"}), ("/api/pet/buy", {"food_id": "carrot"}), ("/api/pet/feed", {"food_id": "carrot"}),
                   ("/api/pet/rename", {"name": "Х"}), ("/api/pet/release", {}), (f"/api/pet/pat/{ua['id']}", {})):
    assert httpx.post(BASE + path, json=body).status_code == 401, path

# ----------------------------------------------------------------------------- состояние без питомца
st = a.get("/api/pet").json()
assert st["pet"] is None and {s["id"] for s in st["species"]} == set(D.SPECIES) and len(st["foods"]) == len(D.FOODS) and st["inventory"] == {}
assert st["balance"] == slots.START_BALANCE
for bad in ({"species": "unicorn", "name": "Бублик"}, {"species": "doberman", "name": ""}, {"species": "doberman", "name": "x" * 17}, {"species": "doberman", "name": "<b>"},
            {"species": "doberman", "name": "  "}):
    assert a.post("/api/pet/adopt", json=bad).status_code in (400, 422), bad
assert a.post("/api/pet/adopt", json={"species": "doberman", "name": "Бублик", "x": 1}).status_code == 422
assert a.get("/api/pet").json()["pet"] is None
assert a.post("/api/pet/feed", json={"food_id": "bone"}).status_code == 400, "без питомца кормить нельзя"

# ----------------------------------------------------------------------------- усыновление
r = a.post("/api/pet/adopt", json={"species": "doberman", "name": "  Бублик  "})
assert r.status_code == 200, r.text
p = r.json()["pet"]
assert p["name"] == "Бублик" and p["species"] == "doberman" and p["stage"] == "baby" and p["xp"] == 0 and 74 <= p["fullness"] <= 75 and p["mood"] == "happy"
assert r.json()["inventory"] == {"bone": 3}, "стартовый запас любимой еды"
assert a.post("/api/pet/adopt", json={"species": "pug", "name": "Мурка"}).status_code == 400, "второго питомца нет"
print("OK: усыновление, стартовый запас")

# ----------------------------------------------------------------------------- магазин
bal0 = a.get("/api/pet").json()["balance"]
rb = a.post("/api/pet/buy", json={"food_id": "carrot", "qty": 4}).json()
assert rb["spent"] == 4 * D.FOODS["carrot"]["price"] and rb["balance"] == bal0 - rb["spent"] == a.get("/api/slots/state").json()["balance"] and rb["inventory"]["carrot"] == 4
assert a.post("/api/pet/buy", json={"food_id": "nope"}).status_code == 400
for bad in ({"food_id": "carrot", "qty": 0}, {"food_id": "carrot", "qty": 21}, {"food_id": "carrot", "qty": -1}, {"food_id": "carrot", "price": 0}):
    assert a.post("/api/pet/buy", json=bad).status_code == 422, bad
sql("UPDATE slot_accounts SET balance=10 WHERE user_id=?", (ua["id"],))
r = a.post("/api/pet/buy", json={"food_id": "steak"})
assert r.status_code == 400 and "жетон" in r.json()["detail"] and a.get("/api/slots/state").json()["balance"] == 10 and "steak" not in a.get("/api/pet").json()["inventory"]
print("OK: покупка еды за жетоны, проверки")

# ----------------------------------------------------------------------------- кормление и рост
sql("UPDATE slot_accounts SET balance=100000 WHERE user_id=?", (ua["id"],))
sql("UPDATE pets SET fullness=40, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
r = a.post("/api/pet/feed", json={"food_id": "bone"})
assert r.status_code == 200, r.text
j = r.json()
fd = D.FOODS["bone"]
assert j["fed"]["liked"] is True and j["fed"]["gain"] == round(fd["xp"] * D.LIKED_BONUS) and j["pet"]["xp"] == j["fed"]["gain"] and j["pet"]["fed_total"] == 1
assert 69 < j["pet"]["fullness"] <= 70 and j["inventory"]["bone"] == 2
sql("UPDATE pets SET fullness=40, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
j2 = a.post("/api/pet/feed", json={"food_id": "carrot"}).json()
assert j2["fed"]["liked"] is False and j2["fed"]["gain"] == D.FOODS["carrot"]["xp"], "нелюбимая еда — без бонуса"
assert a.post("/api/pet/feed", json={"food_id": "steak"}).status_code == 400, "еды нет в запасе"
assert a.post("/api/pet/feed", json={"food_id": "zzz"}).status_code == 400
# сытый отказывается
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
inv_before = a.get("/api/pet").json()["inventory"]
r = a.post("/api/pet/feed", json={"food_id": "carrot"})
assert r.status_code == 400 and "сыт" in r.json()["detail"] and a.get("/api/pet").json()["inventory"] == inv_before, "отказ не тратит еду"
# рост по этапам
a.post("/api/pet/buy", json={"food_id": "steak", "qty": 15})
a.post("/api/pet/buy", json={"food_id": "steak", "qty": 15})
grew = []
for _ in range(24):
    sql("UPDATE pets SET fullness=10, fed_at=? WHERE user_id=?", (time.time(), ua["id"]))
    f = a.post("/api/pet/feed", json={"food_id": "steak"}).json()
    grew.append((f["pet"]["stage"], f["fed"]["grew"]))
assert grew[0][0] in ("baby", "teen") and grew[-1][0] == "adult" and sum(1 for g in grew if g[1]) == 2, grew
assert a.get("/api/pet").json()["pet"]["next_stage"] is None
print("OK: кормление, любимая еда, отказ сытого, рост малыш → подросток → взрослый")

# ----------------------------------------------------------------------------- голод со временем
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time() - 3600 * 5, ua["id"]))
h = a.get("/api/pet").json()["pet"]
assert abs(h["fullness"] - (100 - 5 * D.DECAY_PER_HOUR)) < 0.5 and h["mood"] == "happy"
sql("UPDATE pets SET fullness=100, fed_at=? WHERE user_id=?", (time.time() - 3600 * 100, ua["id"]))
h = a.get("/api/pet").json()["pet"]
assert h["fullness"] == 0 and h["mood"] == "starving" and h["stage"] == "adult", "голодный питомец не умирает и не уменьшается"
assert a.post("/api/pet/feed", json={"food_id": "steak"}).status_code == 200, "голодного можно накормить"
print("OK: сытость падает со временем, питомец не умирает")

# ----------------------------------------------------------------------------- переименование и отпускание
assert a.post("/api/pet/rename", json={"name": "Шарик"}).json()["pet"]["name"] == "Шарик"
assert a.post("/api/pet/rename", json={"name": ""}).status_code in (400, 422)
assert b.post("/api/pet/rename", json={"name": "Х"}).status_code == 400, "чужого питомца переименовать нельзя"
assert b.get("/api/pet").json()["pet"] is None and b.get("/api/pet").json()["inventory"] == {}, "изоляция игроков"

# ----------------------------------------------------------------------------- гости
assert b.post(f"/api/pet/pat/{ua['id']}").status_code == 404, "не друг — не видно"
b.post("/api/friends/request", json={"username": ua["username"]})
a.post(f"/api/friends/{ub['id']}/accept")
pv = b.get(f"/api/friends/{ua['id']}").json()["pet"]
assert pv["name"] == "Шарик" and pv["species"] == "doberman" and pv["patted_by_me"] is False and "fed_total" not in pv and "inventory" not in pv
assert a.get(f"/api/friends/{ub['id']}").json()["pet"] is None, "у друга нет питомца — pet: null"
assert a.post(f"/api/pet/pat/{ua['id']}").status_code == 400, "своего питомца гладить в гостях нельзя"
assert a.post("/api/pet/pat/99999999999999999999999").status_code == 422 and a.post("/api/pet/pat/0").status_code == 422, "огромные id не должны ронять сервер"
bb = b.get("/api/slots/state").json()["balance"]
r = b.post(f"/api/pet/pat/{ua['id']}")
assert r.status_code == 200 and r.json()["reward"] == D.PAT_REWARD and r.json()["balance"] == bb + D.PAT_REWARD == b.get("/api/slots/state").json()["balance"]
assert r.json()["pet"]["patted_by_me"] is True and r.json()["pet"]["pats_today"] == 1
r = b.post(f"/api/pet/pat/{ua['id']}")
assert r.status_code == 400 and b.get("/api/slots/state").json()["balance"] == bb + D.PAT_REWARD, "раз в день, повтор не награждает"
assert a.get("/api/pet").json()["pet"]["pats_total"] == 1
# гонка: один гость, много запросов — одна награда
c, uc = reg("petc")
c.post("/api/friends/request", json={"username": ua["username"]})
a.post(f"/api/friends/{uc['id']}/accept")
c0 = c.get("/api/slots/state").json()["balance"]
res, lock = [], threading.Lock()


def go():
    cl = httpx.Client(base_url=BASE, timeout=60, headers=c.headers)
    r = cl.post(f"/api/pet/pat/{ua['id']}")
    with lock:
        res.append(r.status_code)


ths = [threading.Thread(target=go) for _ in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
assert sorted(res) == [200] + [400] * 7, res
assert c.get("/api/slots/state").json()["balance"] == c0 + D.PAT_REWARD
print("OK: гости гладят раз в день, награда одна, гонки безопасны")

# ----------------------------------------------------------------------------- отпустить и удалить
assert a.post("/api/pet/release").json()["pet"] is None
assert a.post("/api/pet/release").status_code == 400
assert b.get(f"/api/friends/{ua['id']}").json()["pet"] is None
assert sql("SELECT COUNT(*) FROM pet_pats WHERE owner_id=?", (ua["id"],))[0][0] == 0
assert a.post("/api/pet/adopt", json={"species": "beaver", "name": "Огонёк"}).json()["pet"]["species"] == "beaver", "после отпускания можно завести нового"
cnt = lambda t, u: sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (u,))[0][0]  # noqa: E731
assert cnt("pets", ua["id"]) == 1 and cnt("pet_food", ua["id"]) >= 1
assert adm.delete(f"/api/admin/users/{ua['id']}").status_code == 200
assert cnt("pets", ua["id"]) == 0 and cnt("pet_food", ua["id"]) == 0, "данные питомца удалённого игрока исчезают"
assert sql("SELECT COUNT(*) FROM pet_pats WHERE visitor_id=? OR owner_id=?", (ua["id"], ua["id"]))[0][0] == 0
print("OK: отпустить, завести нового, удаление пользователя")
print("ALL OK")
