"""Друзья, подписи с модерацией, список пользователей и бан.
Запуск: tests/restart_test_server.sh clean && python tests/test_social.py http://127.0.0.1:8011  (первый зарегистрированный — администратор)"""
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
tag = str(int(time.time()) % 100000)


def reg(name):
    r = httpx.post(f"{BASE}/api/auth/register", json={"username": f"{name}{tag}", "password": "secret1"}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    return httpx.Client(base_url=BASE, timeout=30, headers={"Authorization": f"Bearer {d['token']}"}), d["user"]


adm, adm_u = reg("adm")
assert adm_u["is_admin"] and adm_u["moderation_pending"] == 0
b, bu = reg("bob")
c, cu = reg("cat")
d, du = reg("dan")
assert not bu["is_admin"] and "moderation_pending" not in bu

# ---- заявки в друзья
assert b.post("/api/friends/request", json={"username": "no_such_user_x"}).status_code == 404
assert b.post("/api/friends/request", json={"username": bu["username"]}).status_code == 400
assert b.post("/api/friends/request", json={"username": "catalog"}).status_code == 404  # служебный пользователь невидим
r = b.post("/api/friends/request", json={"username": "@" + cu["username"].upper()}).json()
assert r["status"] == "pending"
assert b.post("/api/friends/request", json={"username": cu["username"]}).status_code == 400  # повторная заявка
ov = c.get("/api/friends").json()
assert [x["username"] for x in ov["incoming"]] == [bu["username"]] and not ov["friends"]
assert [x["username"] for x in b.get("/api/friends").json()["outgoing"]] == [cu["username"]]
assert b.get(f"/api/friends/{cu['id']}").status_code == 404  # до принятия прогресс не виден
assert d.post(f"/api/friends/{cu['id']}/accept").status_code == 404  # чужую заявку принять нельзя
assert c.post(f"/api/friends/{bu['id']}/accept").status_code == 200
assert [x["username"] for x in b.get("/api/friends").json()["friends"]] == [cu["username"]]
assert b.post("/api/friends/request", json={"username": cu["username"]}).status_code == 400  # уже друзья
print("OK: заявки в друзья")

# встречная заявка = согласие; отклонение и отзыв
assert d.post("/api/friends/request", json={"username": bu["username"]}).status_code == 200
assert b.post("/api/friends/request", json={"username": du["username"]}).json()["status"] == "accepted"
assert d.delete(f"/api/friends/{bu['id']}").status_code == 200 and not d.get("/api/friends").json()["friends"]
assert d.post("/api/friends/request", json={"username": cu["username"]}).status_code == 200
assert d.post(f"/api/friends/{cu['id']}/decline").status_code == 200 and not c.get("/api/friends").json()["incoming"]  # отзыв исходящей
print("OK: встречная заявка, отзыв, удаление из друзей")

# ---- прогресс друга; скрытое достижение не показывается
c.post("/api/prank/yes")
p = b.get(f"/api/friends/{cu['id']}").json()
assert p["username"] == cu["username"] and "level_full" in p and len(p["week"]) == 7 and "totals" in p and p["courses"] == []
assert p["achievements"] and not any(a["key"] == "sosun" for a in p["achievements"]), "скрытое достижение не должно быть видно друзьям"
assert d.get(f"/api/friends/{cu['id']}").status_code == 404  # не друг
print("OK: прогресс друга виден только друзьям, скрытые достижения скрыты")

# ---- подписи
assert b.put(f"/api/friends/{cu['id']}/signature", json={"text": "   "}).status_code in (400, 422)
assert b.put(f"/api/friends/{cu['id']}/signature", json={"text": "я" * 41}).status_code == 400
assert b.put(f"/api/friends/{cu['id']}/signature", json={"text": "я" * 40}).status_code == 200  # ровно 40 допустимо
assert d.put(f"/api/friends/{cu['id']}/signature", json={"text": "Привет"}).status_code == 404  # d не друг c
s = b.put(f"/api/friends/{cu['id']}/signature", json={"text": "Лучший   напарник\n по учёбе"}).json()
assert s["status"] == "pending" and s["text"] == "Лучший напарник по учёбе", s
mine = b.get(f"/api/friends/{cu['id']}").json()
assert mine["my_signature"]["status"] == "pending" and mine["wall"] == [], "до модерации подпись на страничке не видна"
assert c.get("/api/me/wall").json()["wall"] == []
q = adm.get("/api/admin/signatures").json()
assert any(x["id"] == s["id"] and x["author"]["username"] == bu["username"] and x["target"]["username"] == cu["username"] for x in q), q
assert adm.get("/api/me").json()["moderation_pending"] >= 1
assert b.get("/api/admin/signatures").status_code == 403 and b.post(f"/api/admin/signatures/{s['id']}/approve").status_code == 403
assert adm.post(f"/api/admin/signatures/{s['id']}/approve").json()["status"] == "approved"
assert [w["text"] for w in c.get("/api/me/wall").json()["wall"]] == ["Лучший напарник по учёбе"]
assert b.get(f"/api/friends/{cu['id']}").json()["my_signature"]["status"] == "approved"
# повторная правка: одна подпись от автора, снова на модерации
s2 = b.put(f"/api/friends/{cu['id']}/signature", json={"text": "Новая подпись"}).json()
assert s2["id"] == s["id"] and s2["status"] == "pending"
assert c.get("/api/me/wall").json()["wall"] == []
assert adm.post(f"/api/admin/signatures/{s['id']}/reject").json()["status"] == "rejected"
assert b.get(f"/api/friends/{cu['id']}").json()["my_signature"]["status"] == "rejected"
assert not [x for x in adm.get("/api/admin/signatures").json() if x["id"] == s["id"]]
print("OK: подписи — лимит 40 символов, модерация, одна подпись от автора")

# владелец странички может убрать подпись; автор может отозвать свою; удаление из друзей убирает подписи
b.put(f"/api/friends/{cu['id']}/signature", json={"text": "Ещё раз"})
adm.post(f"/api/admin/signatures/{s['id']}/approve")
assert c.delete(f"/api/me/wall/{s['id']}").status_code == 200 and c.get("/api/me/wall").json()["wall"] == []
assert c.delete(f"/api/me/wall/{s['id']}").status_code == 404
b.put(f"/api/friends/{cu['id']}/signature", json={"text": "Снова"})
assert b.delete(f"/api/friends/{cu['id']}/signature").status_code == 200 and b.get(f"/api/friends/{cu['id']}").json()["my_signature"] is None
b.put(f"/api/friends/{cu['id']}/signature", json={"text": "Останется?"})
c.delete(f"/api/friends/{bu['id']}")
assert not b.get("/api/friends").json()["friends"] or all(f["username"] != cu["username"] for f in b.get("/api/friends").json()["friends"])
assert adm.get("/api/admin/signatures").json() == [] or all(x["text"] != "Останется?" for x in adm.get("/api/admin/signatures").json())
print("OK: удаление подписей и дружбы")

# подпись администратора публикуется сразу
adm.post("/api/friends/request", json={"username": cu["username"]})
c.post(f"/api/friends/{adm_u['id']}/accept")
sa = adm.put(f"/api/friends/{cu['id']}/signature", json={"text": "Привет от админа"}).json()
assert sa["status"] == "approved" and c.get("/api/me/wall").json()["wall"][0]["text"] == "Привет от админа"
print("OK: подпись администратора без модерации")

# ---- пользователи и бан
users = adm.get("/api/admin/users").json()
names = [u["username"] for u in users]
assert bu["username"] in names and "catalog" not in names
assert all({"xp", "level", "courses", "banned", "last_active"} <= set(u) for u in users)
assert [u["username"] for u in adm.get("/api/admin/users?q=" + bu["username"][:6]).json()] == [bu["username"]]
assert b.get("/api/admin/users").status_code == 403 and b.post(f"/api/admin/users/{cu['id']}/ban").status_code == 403
assert adm.post(f"/api/admin/users/{adm_u['id']}/ban").status_code == 400  # себя нельзя
assert adm.post("/api/admin/users/99999/ban").status_code == 404
assert b.post("/api/friends/request", json={"username": cu["username"]}).status_code == 200  # висящая заявка забаненного не должна быть видна
assert adm.post(f"/api/admin/users/{bu['id']}/ban", json={"reason": "спам в подписях"}).status_code == 200
r = b.get("/api/me")
assert r.status_code == 403 and "спам в подписях" in r.json()["detail"], r.text
assert b.get("/api/courses").status_code == 403
r = httpx.post(f"{BASE}/api/auth/login", json={"username": bu["username"], "password": "secret1"})
assert r.status_code == 403 and "заблокирован" in r.json()["detail"]
assert httpx.post(f"{BASE}/api/auth/login", json={"username": bu["username"], "password": "wrong"}).status_code == 400  # пароль проверяется раньше
assert all(f["username"] != bu["username"] for f in d.get("/api/friends").json()["friends"] + c.get("/api/friends").json()["friends"])
assert not [x for x in c.get("/api/friends").json()["incoming"] if x["username"] == bu["username"]]
assert [x for x in adm.get("/api/admin/users").json() if x["id"] == bu["id"]][0]["banned"]
assert adm.post(f"/api/admin/users/{bu['id']}/unban").status_code == 200
r = httpx.post(f"{BASE}/api/auth/login", json={"username": bu["username"], "password": "secret1"})
assert r.status_code == 200
print("OK: список пользователей, бан и разбан")
print("ALL OK")
