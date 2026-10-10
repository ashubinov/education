"""Аватарка-картинка, смена логина, удаление пользователя администратором.
Запуск (чистый сервер, LQ_DB — путь к его базе): LQ_DB=<tmp.db> python tests/test_account.py http://127.0.0.1:8011"""
import os
import sqlite3
import sys
import time

import httpx
import pymupdf

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
tag = str(int(time.time()) % 100000)


def reg(name):
    r = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{tag}", "password": "secret1"}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    return httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


def image(w, h, fmt="png", cs=pymupdf.csRGB, alpha=False, gray=140):
    pix = pymupdf.Pixmap(cs, pymupdf.IRect(0, 0, w, h), alpha)
    pix.clear_with(gray)
    return pix.tobytes("jpeg") if fmt == "jpeg" else pix.tobytes("png")


def put(c, data, name="a.png"):
    return c.put("/api/me/avatar", files={"file": (name, data, "application/octet-stream")})


adm, au = reg("adm")
a, ua = reg("ava")
b, ub = reg("bro")
assert au["is_admin"] and ua["avatar_url"] is None

# ---------- аватарка
r = put(a, image(600, 400))
assert r.status_code == 200, r.text
url1 = r.json()["avatar_url"]
assert url1.startswith("/api/avatars/")
img = httpx.get(BASE + url1)  # без входа: картинку грузит тег <img>
assert img.status_code == 200 and img.headers["content-type"] == "image/png" and "immutable" in img.headers["cache-control"]
assert img.headers.get("x-content-type-options") == "nosniff"
pix = pymupdf.Pixmap(img.content)
assert max(pix.width, pix.height) == 256 and pix.width > pix.height, (pix.width, pix.height)  # уменьшена с сохранением пропорций
url2 = put(a, image(100, 100, "jpeg")).json()["avatar_url"]
assert url2 != url1 and httpx.get(BASE + url1).status_code == 404, "после замены старый адрес перестаёт работать"
assert httpx.get(BASE + url2).headers["content-type"] == "image/jpeg"
assert put(a, image(80, 80, cs=pymupdf.csGRAY)).status_code == 200      # серая картинка приводится к RGB
assert put(a, image(80, 80, alpha=True)).json()["avatar_url"] and httpx.get(BASE + a.get("/api/me").json()["avatar_url"]).headers["content-type"] == "image/png"
for bad, why in ((b"GIF89a" + b"0" * 500, "не PNG/JPEG"), (b"just text" * 100, "не картинка"), (b"\x89PNG\r\n\x1a\n" + b"broken" * 50, "битый PNG"),
                 (image(8, 8), "слишком маленькая"), (image(3000, 100), "слишком большая по размеру"), (image(200, 200) + b"0" * 1_100_000, "больше 1 МБ")):
    r = put(a, bad)
    assert r.status_code == 400 and r.json()["detail"], (why, r.status_code, r.text)
assert httpx.put(BASE + "/api/me/avatar", files={"file": ("a.png", image(50, 50))}).status_code == 401
assert httpx.get(BASE + "/api/avatars/not-a-real-key").status_code == 404
print("OK: загрузка аватарки (проверка, пересборка, уменьшение, замена, отказы)")

# друзья видят картинку
b.post("/api/friends/request", json={"username": ua["username"]})
a.post(f"/api/friends/{ub['id']}/accept")
assert b.get("/api/friends").json()["friends"][0]["avatar_url"] == a.get("/api/me").json()["avatar_url"]
assert b.get(f"/api/friends/{ua['id']}").json()["avatar_url"]
assert [u for u in adm.get("/api/admin/users").json() if u["id"] == ua["id"]][0]["avatar_url"]
cur = a.get("/api/me").json()["avatar_url"]
assert a.delete("/api/me/avatar").json()["avatar_url"] is None and httpx.get(BASE + cur).status_code == 404
print("OK: друзья и админ видят аватарку, удаление возвращает эмодзи")

# ---------- оформление (тема) ----------
assert ua["theme_extra"] == {"preset": "", "bg": "glow", "radius": "normal", "motion": "full"}, ua["theme_extra"]
r = a.put("/api/me", json={"theme_color": "#06b6d4", "theme_extra": {"preset": "ocean", "bg": "tint", "radius": "round", "motion": "reduced"}})
assert r.status_code == 200 and r.json()["theme_extra"] == {"preset": "ocean", "bg": "tint", "radius": "round", "motion": "reduced"}, r.text
assert a.get("/api/me").json()["theme_extra"]["bg"] == "tint" and a.get("/api/me").json()["theme_color"] == "#06b6d4", "тема сохраняется на сервере"
assert a.put("/api/me", json={"theme_extra": {"bg": "solid"}}).json()["theme_extra"] == {"preset": "", "bg": "solid", "radius": "normal", "motion": "full"}, "не указанное берётся по умолчанию"
for bad in ({"bg": "neon"}, {"radius": "huge"}, {"motion": "none"}, {"preset": "Bad Name!"}, {"preset": "x" * 30}):
    assert a.put("/api/me", json={"theme_extra": bad}).status_code == 422, bad
assert a.get("/api/me").json()["theme_extra"]["bg"] == "solid", "неверные значения не меняют сохранённую тему"
assert b.get("/api/me").json()["theme_extra"]["bg"] == "glow", "тема у каждого пользователя своя"
a.put("/api/me", json={"theme_color": "#7c5cff", "theme_extra": {}})
print("OK: оформление хранится на сервере и проверяется")

# ---------- смена логина
new = f"newname{tag}"
assert a.put("/api/me/username", json={"username": new, "password": "wrong"}).status_code == 400
assert a.put("/api/me/username", json={"username": "a b", "password": "secret1"}).status_code == 422
assert a.put("/api/me/username", json={"username": ub["username"], "password": "secret1"}).status_code == 400  # занят
assert a.put("/api/me/username", json={"username": ub["username"].upper(), "password": "secret1"}).status_code == 400  # регистр не обходит занятость
assert a.put("/api/me/username", json={"username": "catalog", "password": "secret1"}).status_code == 400
assert a.put("/api/me/username", json={"username": ua["username"], "password": "secret1"}).status_code == 400  # тот же
r = a.put("/api/me/username", json={"username": new, "password": "secret1"})
assert r.status_code == 200 and r.json()["username"] == new
assert a.get("/api/me").json()["username"] == new, "прежний токен продолжает работать"
assert httpx.post(f"{BASE}/api/auth/login", json={"username": new, "password": "secret1"}).status_code == 200
assert httpx.post(f"{BASE}/api/auth/login", json={"username": ua["username"], "password": "secret1"}).status_code == 400
assert b.get("/api/friends").json()["friends"][0]["username"] == new
assert reg("reuse")[1]["username"] != new
r = httpx.post(f"{BASE}/api/auth/register", json={"username": ua["username"], "password": "secret1"})
assert r.status_code == 200, "старый логин освободился"
assert a.put("/api/me/username", json={"username": new.upper(), "password": "secret1"}).status_code == 200  # смена регистра своего логина
assert adm.put("/api/me/username", json={"username": f"boss{tag}", "password": "secret1"}).status_code == 400  # админу логин здесь не меняют
print("OK: смена логина (пароль, занятость, регистр, токены, освобождение старого)")

# ---------- удаление пользователя админом
v, uv = reg("victim")
cid = v.post("/api/courses", files=[("files", ("n.txt", ("Совсем другая тема про сети и протоколы. " * 40).encode(), "text/plain"))], data={"title": "Курс жертвы"}).json()["id"]
for _ in range(100):
    if v.get(f"/api/courses/{cid}").json()["status"] == "ready":
        break
    time.sleep(0.3)
for _ in range(16):
    nx = v.get(f"/api/courses/{cid}/next").json()
    if nx.get("state") == "ready":
        v.post(f"/api/lessons/{nx['lesson_id']}/start", json={})  # у жертвы есть начатое прохождение
        break
    time.sleep(0.5)
put(v, image(64, 64))
v.post("/api/prank/yes")
v.post("/api/friends/request", json={"username": ub["username"]})
b.post(f"/api/friends/{uv['id']}/accept")
b.put(f"/api/friends/{uv['id']}/signature", json={"text": "привет, жертва"})
v.put(f"/api/friends/{ub['id']}/signature", json={"text": "подпись от жертвы"})
adm.post("/api/admin/signatures/%d/approve" % [x for x in adm.get("/api/admin/signatures").json() if x["author"]["id"] == uv["id"]][0]["id"])
assert b.get("/api/me/wall").json()["wall"], "подпись жертвы видна у друга до удаления"
vurl = v.get("/api/me").json()["avatar_url"]
n_cat = len(adm.get("/api/catalog").json())

assert a.delete(f"/api/admin/users/{uv['id']}").status_code == 403
assert adm.delete(f"/api/admin/users/{au['id']}").status_code == 400
assert adm.delete("/api/admin/users/99999").status_code == 404
assert adm.delete("/api/admin/users/1").status_code == 404  # служебный пользователь каталога
assert adm.delete(f"/api/admin/users/{uv['id']}").status_code == 200
assert v.get("/api/me").status_code == 401, "токен удалённого больше не работает"
assert httpx.post(f"{BASE}/api/auth/login", json={"username": uv["username"], "password": "secret1"}).status_code == 400
assert uv["username"] not in [u["username"] for u in adm.get("/api/admin/users").json()]
assert uv["username"] not in [f["username"] for f in b.get("/api/friends").json()["friends"]]
assert b.get(f"/api/friends/{uv['id']}").status_code == 404
assert b.get("/api/me/wall").json()["wall"] == [] and adm.get("/api/admin/signatures").json() == []
assert httpx.get(BASE + vurl).status_code == 404
assert len(adm.get("/api/catalog").json()) == n_cat
db_path = os.environ.get("LQ_DB")
if db_path and os.path.exists(db_path):
    con = sqlite3.connect(db_path)
    for table, col in (("courses", "user_id"), ("runs", "user_id"), ("answers", "user_id"), ("activity", "user_id"), ("achievements", "user_id"),
                       ("avatars", "user_id"), ("friendships", "requester_id"), ("friendships", "addressee_id"), ("signatures", "author_id"),
                       ("signatures", "target_id"), ("users", "id")):
        assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (uv["id"],)).fetchone()[0] == 0, (table, col)
    left = con.execute("SELECT COUNT(*) FROM lessons WHERE course_id=?", (cid,)).fetchone()[0] + con.execute("SELECT COUNT(*) FROM modules WHERE course_id=?", (cid,)).fetchone()[0]
    assert left == 0, "уроки и модули удалённого курса должны исчезнуть"
    con.close()
assert httpx.post(f"{BASE}/api/auth/register", json={"username": uv["username"], "password": "secret1"}).status_code == 200
print("OK: администратор удаляет пользователя со всеми данными, остальные не затронуты")
print("ALL OK")
