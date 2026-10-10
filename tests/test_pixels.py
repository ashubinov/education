"""Пиксел баттл: лимит времени и накопление, границы и цвета, обновления, информация о клетке, покупка, админ-очистка, гонки, удаление пользователя.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_pixels.py http://127.0.0.1:8011"""
import base64
import os
import sqlite3
import sys
import threading
import time

import httpx

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
from server import pixels as P  # noqa: E402
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


def rewind(uid, seconds):
    """Сдвигает учёт пикселов игрока в прошлое (как будто прошло столько секунд)."""
    sql("UPDATE pixel_users SET charge_at=charge_at-? WHERE user_id=?", (seconds, uid))


adm, au = reg("adm")
a, ua = reg("pxa")
b, ub = reg("pxb")
for path in ("/api/pixels/state", "/api/pixels/changes", "/api/pixels/cell?x=1&y=1", "/api/pixels/top"):
    assert httpx.get(BASE + path).status_code == 401, path
assert httpx.post(BASE + "/api/pixels/place", json={"x": 1, "y": 1, "color": 1}).status_code == 401

# ----------------------------------------------------------------------------- состояние и доска
st = a.get("/api/pixels/state").json()
assert st["w"] == st["h"] == 128 and len(st["palette"]) == 16 and st["empty"] == 255
raw = base64.b64decode(st["board"])
assert len(raw) == 128 * 128 and set(raw) == {255}, "пустая доска"
assert st["charges"] == 1 and st["max_charges"] == 5 and 0 < st["next_in"] <= 60 and st["cooldown"] == 60 and st["price"] == P.BUY_PRICE
assert st["balance"] == slots.START_BALANCE and st["last_id"] == 0
print("OK: состояние пустой доски, стартовый пиксел")

# ----------------------------------------------------------------------------- проверки ввода
for bad in ({"x": -1, "y": 0, "color": 1}, {"x": 128, "y": 0, "color": 1}, {"x": 0, "y": 128, "color": 1}, {"x": 0, "y": 0, "color": 16},
            {"x": 0, "y": 0, "color": -1}, {"x": "a", "y": 0, "color": 1}, {"x": 0, "y": 0}, {"x": 0, "y": 0, "color": 1, "user_id": 5}):
    assert a.post("/api/pixels/place", json=bad).status_code == 422, bad
assert a.get("/api/pixels/state").json()["charges"] == 1, "ошибочные запросы пиксел не тратят"
assert a.get("/api/pixels/cell?x=500&y=1").status_code == 400

# ----------------------------------------------------------------------------- постановка пиксела и лимит времени
r = a.post("/api/pixels/place", json={"x": 5, "y": 7, "color": 5})
assert r.status_code == 200, r.text
j = r.json()
assert j["x"] == 5 and j["y"] == 7 and j["color"] == 5 and j["charges"] == 0 and 0 < j["next_in"] <= 60 and j["id"] == 1
r2 = a.post("/api/pixels/place", json={"x": 6, "y": 7, "color": 5})
assert r2.status_code == 429 and "Retry-After" in r2.headers and "через" in r2.json()["detail"], r2.text
st = b.get("/api/pixels/state").json()
raw = base64.b64decode(st["board"])
assert raw[7 * 128 + 5] == 5 and raw[7 * 128 + 6] == 255 and st["last_id"] == 1
print("OK: пиксел ставится, лимит времени работает, доска общая")

# накопление: через минуту — ещё один; не больше максимума
rewind(ua["id"], 61)
assert a.get("/api/pixels/state").json()["charges"] == 1
assert a.post("/api/pixels/place", json={"x": 6, "y": 7, "color": 3}).status_code == 200
rewind(ua["id"], 60 * 20)
s2 = a.get("/api/pixels/state").json()
assert s2["charges"] == 5 and s2["next_in"] == 0, "копится не больше 5"
for i in range(5):
    assert a.post("/api/pixels/place", json={"x": 10 + i, "y": 10, "color": 2}).status_code == 200
assert a.post("/api/pixels/place", json={"x": 20, "y": 10, "color": 2}).status_code == 429
s3 = a.get("/api/pixels/state").json()
assert s3["charges"] == 0 and 0 < s3["next_in"] <= 60
rewind(ua["id"], 135)  # 2 пиксела + остаток
s4 = a.get("/api/pixels/state").json()
assert s4["charges"] == 2 and 0 < s4["next_in"] <= 60, s4
print("OK: накопление пикселов по времени, потолок 5")

# тот же цвет на той же клетке — отказ без траты
before = a.get("/api/pixels/state").json()["charges"]
assert a.post("/api/pixels/place", json={"x": 5, "y": 7, "color": 5}).status_code == 400
assert a.get("/api/pixels/state").json()["charges"] == before
# перекраска чужого
rewind(ub["id"], 1)
assert b.post("/api/pixels/place", json={"x": 5, "y": 7, "color": 9}).status_code == 200
assert base64.b64decode(a.get("/api/pixels/state").json()["board"])[7 * 128 + 5] == 9

# ----------------------------------------------------------------------------- поток изменений
ch = a.get("/api/pixels/changes?after=0").json()
assert ch["more"] is False and ch["changes"][0] == {"id": 1, "x": 5, "y": 7, "c": 5, "u": ua["id"]}
assert a.get("/api/pixels/changes?after=99999999999999999999").status_code == 200 and a.get("/api/pixels/changes?after=-5").status_code == 200, "огромный after не должен ронять сервер"
assert [c["id"] for c in ch["changes"]] == sorted(c["id"] for c in ch["changes"])
last = ch["last_id"]
assert a.get(f"/api/pixels/changes?after={last}").json() == {"changes": [], "more": False, "last_id": last}
rewind(ub["id"], 61)
b.post("/api/pixels/place", json={"x": 50, "y": 50, "color": 12})
nx = a.get(f"/api/pixels/changes?after={last}").json()
assert len(nx["changes"]) == 1 and nx["changes"][0]["x"] == 50 and nx["changes"][0]["u"] == ub["id"] and nx["last_id"] == last + 1
print("OK: поток изменений")

# ----------------------------------------------------------------------------- «кто тут рисовал»
ci = a.get("/api/pixels/cell?x=5&y=7").json()
assert ci["color"] == 9 and ci["by"]["user_id"] == ub["id"] and ci["total"] == 2 and len(ci["history"]) == 2
assert ci["history"][0]["by"]["user_id"] == ub["id"] and ci["history"][1]["by"]["user_id"] == ua["id"] and ci["history"][1]["color"] == 5
empty = a.get("/api/pixels/cell?x=100&y=100").json()
assert empty["color"] == 255 and empty["by"] is None and empty["history"] == []
print("OK: информация о клетке и история споров")

# ----------------------------------------------------------------------------- покупка пиксела за жетоны
c, uc = reg("pxc")
c.get("/api/pixels/state")
assert c.post("/api/pixels/place", json={"x": 1, "y": 1, "color": 4}).status_code == 200
bal0 = c.get("/api/slots/state").json()["balance"]
rb = c.post("/api/pixels/buy").json()
assert rb["balance"] == bal0 - P.BUY_PRICE == c.get("/api/slots/state").json()["balance"] and rb["charges"] == 1
assert c.post("/api/pixels/place", json={"x": 2, "y": 1, "color": 4}).status_code == 200
for _ in range(5):
    c.post("/api/pixels/buy")
assert c.get("/api/pixels/state").json()["charges"] == 5
bal1 = c.get("/api/slots/state").json()["balance"]
r = c.post("/api/pixels/buy")
assert r.status_code == 400 and "полный" in r.json()["detail"] and c.get("/api/slots/state").json()["balance"] == bal1, "при полном запасе жетоны не списываются"
sql("UPDATE slot_accounts SET balance=5 WHERE user_id=?", (uc["id"],))
c.post("/api/pixels/place", json={"x": 3, "y": 1, "color": 4})
r = c.post("/api/pixels/buy")
assert r.status_code == 400 and "жетон" in r.json()["detail"] and c.get("/api/slots/state").json()["balance"] == 5
print("OK: покупка пиксела за жетоны")

# ----------------------------------------------------------------------------- гонки: один пиксел — один успех
d, ud = reg("pxd")
d.get("/api/pixels/state")
res, lock = [], threading.Lock()


def go(i):
    cl = httpx.Client(base_url=BASE, timeout=60, headers=d.headers)
    r = cl.post("/api/pixels/place", json={"x": 60 + i, "y": 60, "color": 3})
    with lock:
        res.append(r.status_code)


ths = [threading.Thread(target=go, args=(i,)) for i in range(10)]
[t.start() for t in ths]
[t.join() for t in ths]
assert sorted(res) == [200] + [429] * 9, res
print("OK: гонки — пиксел не тратится дважды")

# ----------------------------------------------------------------------------- рейтинг и профиль друга
tp = a.get("/api/pixels/top").json()
assert tp["stats"]["painted"] > 5 and tp["stats"]["total"] == 128 * 128
assert tp["top"] == sorted(tp["top"], key=lambda x: -x["pixels"]) and {"display_name", "pixels", "user_id"} <= set(tp["top"][0])
e, ue = reg("pxe")
assert e.get("/api/pixels/state").status_code == 200
e.post("/api/friends/request", json={"username": ua["username"]})
a.post(f"/api/friends/{ue['id']}/accept")
pub = e.get(f"/api/friends/{ua['id']}").json()["pixels"]
assert pub["placed"] == sql("SELECT COUNT(*) FROM pixel_log WHERE user_id=?", (ua["id"],))[0][0] and pub["owned"] <= pub["placed"]
assert a.get(f"/api/friends/{ue['id']}").json()["pixels"] is None, "кто не рисовал — pixels: null"
print("OK: рейтинг и профиль друга")

# ----------------------------------------------------------------------------- админ-очистка
assert a.post("/api/pixels/admin/clear", json={"x1": 0, "y1": 0, "x2": 20, "y2": 20}).status_code == 403
last = a.get("/api/pixels/state").json()["last_id"]
rc = adm.post("/api/pixels/admin/clear", json={"x1": 20, "y1": 20, "x2": 0, "y2": 0}).json()
assert rc["cleared"] >= 7
raw = base64.b64decode(a.get("/api/pixels/state").json()["board"])
assert raw[7 * 128 + 5] == 255 and raw[10 * 128 + 10] == 255 and raw[50 * 128 + 50] == 12, "вне области — не тронуто"
cl = a.get(f"/api/pixels/changes?after={last}").json()["changes"]
assert len(cl) == rc["cleared"] and all(x["c"] == 255 and x["u"] is None for x in cl), "очистка попадает в поток изменений"
assert adm.post("/api/pixels/admin/clear", json={"x1": 0, "y1": 0, "x2": 200, "y2": 5}).status_code == 422
print("OK: очистка области администратором")

# ----------------------------------------------------------------------------- удаление пользователя
cnt = lambda q, u: sql(q, (u,))[0][0]  # noqa: E731
assert cnt("SELECT COUNT(*) FROM pixel_board WHERE user_id=?", ub["id"]) >= 1 and cnt("SELECT COUNT(*) FROM pixel_users WHERE user_id=?", ub["id"]) == 1
assert adm.delete(f"/api/admin/users/{ub['id']}").status_code == 200
assert cnt("SELECT COUNT(*) FROM pixel_users WHERE user_id=?", ub["id"]) == 0
assert cnt("SELECT COUNT(*) FROM pixel_board WHERE user_id=?", ub["id"]) == 0 and cnt("SELECT COUNT(*) FROM pixel_log WHERE user_id=?", ub["id"]) == 0
raw = base64.b64decode(a.get("/api/pixels/state").json()["board"])
assert raw[50 * 128 + 50] == 12, "нарисованное остаётся на доске"
ci = a.get("/api/pixels/cell?x=50&y=50").json()
assert ci["color"] == 12 and ci["by"] is None, "но становится безымянным"
print("OK: удаление игрока — картина остаётся, авторство стирается")
print("ALL OK")
