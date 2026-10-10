"""Сброс пароля администратором: временный пароль показывается один раз, старые входы отзываются, пользователь обязан задать свой пароль.
Запуск на чистом тестовом сервере (первый зарегистрированный — администратор): python tests/test_password_reset.py http://127.0.0.1:8011"""
import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"


def reg(prefix, pw="oldpass1"):
    name = prefix + uuid.uuid4().hex[:6]
    r = httpx.post(BASE + "/api/auth/register", json={"username": name, "password": pw}, timeout=30).json()
    return name, httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + r["token"]}, timeout=30), r["user"]


def login(name, pw):
    return httpx.post(BASE + "/api/auth/login", json={"username": name, "password": pw}, timeout=30)


an, adm, au = reg("pradm")
un, usr, uu = reg("prusr")
wn, other, ou = reg("prother")
assert au["is_admin"] and uu["must_change_password"] is False

assert usr.post(f"/api/admin/users/{ou['id']}/reset-password").status_code == 403, "обычный пользователь сбросить не может"
assert httpx.post(BASE + f"/api/admin/users/{uu['id']}/reset-password").status_code == 401
assert adm.post("/api/admin/users/999999/reset-password").status_code == 404
assert adm.post("/api/admin/users/1/reset-password").status_code == 404, "служебный пользователь каталога"
assert adm.post(f"/api/admin/users/{au['id']}/reset-password").status_code == 400, "свой пароль здесь не сбрасывается"

# ---------- сброс ----------
r = adm.post(f"/api/admin/users/{uu['id']}/reset-password")
assert r.status_code == 200, r.text
temp = r.json()["password"]
assert len(temp) == 14 and temp.count("-") == 2 and all(len(g) == 4 for g in temp.split("-")), temp
assert not set("0OIl1") & set(temp), "в пароле нет похожих символов"
assert usr.get("/api/me").status_code == 401, "прежний вход отозван"
assert login(un, "oldpass1").status_code == 400, "старый пароль больше не работает"
assert adm.get("/api/admin/users").status_code == 200 and temp not in adm.get("/api/admin/users").text, "пароль нигде не хранится и не показывается повторно"
r = login(un, temp)
assert r.status_code == 200, r.text
d = r.json()
assert d["user"]["must_change_password"] is True
u2 = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + d["token"]}, timeout=30)
print("OK: сброс пароля, старые входы и пароль отозваны, временный пароль работает")

# ---------- пока пароль временный, пользоваться сайтом нельзя ----------
assert u2.get("/api/me").json()["must_change_password"] is True
for path in ("/api/courses", "/api/friends", "/api/slots/state", "/api/chat/messages", "/api/stats"):
    x = u2.get(path)
    assert x.status_code == 403 and "временный пароль" in x.json()["detail"], (path, x.status_code, x.text)
assert u2.post("/api/slots/spin", json={"bet": 10}).status_code == 403
assert u2.put("/api/me/password", json={"current_password": temp, "new_password": "x"}).status_code in (404, 405)
# ---------- смена на свой пароль ----------
assert u2.post("/api/me/password", json={"current_password": "wrong-one", "new_password": "mynewpass1"}).status_code == 400
assert u2.post("/api/me/password", json={"current_password": temp, "new_password": temp}).status_code == 400, "новый пароль должен отличаться от временного"
assert u2.post("/api/me/password", json={"current_password": temp, "new_password": "123"}).status_code == 422
ch = u2.post("/api/me/password", json={"current_password": temp, "new_password": "mynewpass1"})
assert ch.status_code == 200, ch.text
u3 = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + ch.json()["token"]}, timeout=30)
me = u3.get("/api/me").json()
assert me["must_change_password"] is False
assert u3.get("/api/courses").status_code == 200 and u3.get("/api/slots/state").status_code == 200, "после смены пароля всё работает"
assert u2.get("/api/me").status_code == 401, "токен с временным паролем отозван"
assert login(un, temp).status_code == 400 and login(un, "mynewpass1").status_code == 200 and login(un, "mynewpass1").json()["user"]["must_change_password"] is False
print("OK: до смены пароля доступен только профиль и смена, затем всё работает")

# ---------- повторный сброс, заблокированный, администратор ----------
t2 = adm.post(f"/api/admin/users/{uu['id']}/reset-password").json()["password"]
t3 = adm.post(f"/api/admin/users/{uu['id']}/reset-password").json()["password"]
assert t2 != t3 and login(un, t2).status_code == 400 and login(un, t3).status_code == 200, "действует только последний временный пароль"
adm.post(f"/api/admin/users/{ou['id']}/ban", json={"reason": "тест"})
assert adm.post(f"/api/admin/users/{ou['id']}/reset-password").status_code == 200, "заблокированному пароль тоже можно сбросить"
assert other.get("/api/me").status_code in (401, 403)
print("OK: повторный сброс, заблокированные, защита администратора")
print("ALL OK")
